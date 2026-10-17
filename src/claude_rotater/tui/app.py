"""The rota Textual application.

Owns the snapshot poll loop, the always-on auto-switch engine, the usage
estimates, and every mutating action (switch/add/remove), so the main page
and the config page drive the same code paths. Blocking switcher work always
runs in thread workers: the UI loop never touches file locks, keychain
subprocesses, the history file, or the network.

The engine is the only scheduled fetcher. It runs LIVE for the whole session
(no dry-run gate in the TUI; its own guards: threshold, hysteresis, shared
cooldown, quarantine, disabled accounts). The 3 s poller therefore only reads
the store; ``f`` asks for one normal pass, which the store still paces.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from functools import partial
from typing import Callable, Protocol

from textual.app import App, SuspendNotSupported
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import WorkerState

from claude_rotater import legacy_import, printer
from claude_rotater.autoswitch import STATE_FILENAME, AutoSwitchEvent
from claude_rotater.estimator import AccountEstimate, estimate_account
from claude_rotater import statusline_setup
from claude_rotater.exceptions import ClaudeSwitchError, LoginError
from claude_rotater.login import StagedLogin
from claude_rotater.token_refresh import RefreshEvent, TokenRefresher
from claude_rotater.models import AccountsSnapshot
from claude_rotater.settings import (
    AutoSwitchSettings,
    UiSettings,
    load_settings,
    load_ui_settings,
    parse_model_names,
    set_setting,
)
from claude_rotater.poll_policy import load_cadence
from claude_rotater.snapshot_source import account_identity
from claude_rotater.switcher import ClaudeAccountSwitcher
from claude_rotater.tui.config import ConfigScreen
from claude_rotater.tui.data import ActionResult, SnapshotSource, compact_duration, run_action
from claude_rotater.tui.main import (
    LONG_5H_SPAN_S,
    LONG_7D_SPAN_S,
    AutoStatus,
    LongTerm,
    MainScreen,
)
from claude_rotater.tui.modals import AddModal, AddTokenModal, ConfirmModal, OutputModal, TokenForm
from claude_rotater.tui.theme import ROTA_DARK, ROTA_LIGHT
from claude_rotater.usage_history import Point


class EngineLike(Protocol):
    """What the app needs from an auto-switch engine (tests inject fakes)."""

    def run_loop(self) -> int: ...
    def stop(self) -> None: ...
    def wake(self) -> None: ...
    def apply_threshold(self, threshold: float) -> None: ...


EngineFactory = Callable[
    [ClaudeAccountSwitcher, AutoSwitchSettings, Callable[[AutoSwitchEvent], None]],
    EngineLike,
]


def default_engine_factory(switcher, settings, on_event) -> EngineLike:
    """A LIVE engine: the TUI never passes ``dry_run``. It ingests the
    statusline feed, so the active account goes unpolled while it is fresh."""
    from claude_rotater.autoswitch import AutoSwitchEngine
    from claude_rotater.feed_ingest import FeedIngestor

    return AutoSwitchEngine(switcher, settings, on_event, feed=FeedIngestor(switcher))


def default_refresher_factory(switcher, on_event) -> TokenRefresher:
    """Keeps idle accounts' tokens fresh for as long as the app runs."""
    return TokenRefresher(switcher, on_event=on_event)


class RotaApp(App):
    """rota interactive TUI: one main page plus the config page."""

    TITLE = "rota"
    CSS_PATH = "rota.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_theme", "Theme")]

    POLL_INTERVAL_S = 3.0
    # Snapshot age stays hidden while polling is healthy (age never exceeds
    # ~POLL_INTERVAL_S); past this it surfaces as a staleness alarm.
    SNAPSHOT_AGE_NOTE_S = 60.0

    snapshot: reactive[AccountsSnapshot | None] = reactive(None)
    refresh_status: reactive[str] = reactive("")
    busy: reactive[bool] = reactive(False)
    # slot → cached estimate / history series; replaced (never mutated) by
    # the refresh worker, re-projected at render time against the clock.
    estimates: reactive[dict[str, AccountEstimate]] = reactive(dict, always_update=True)
    series: dict[str, dict[str, list[Point]]]
    auto_status: reactive[AutoStatus] = reactive(AutoStatus, always_update=True)
    # "live" / "stale" / "off" / "unset" for the statusline feed; "" until first read.
    feed_state: reactive[str] = reactive("")
    feed_installed = False
    # "not-installed" / "installed" / "foreign" (someone else's statusline).
    feed_install = statusline_setup.NOT_INSTALLED
    # The long-term view (``h``): past windows instead of the current ones.
    long_view: reactive[bool] = reactive(False)
    long_term: reactive[dict[str, LongTerm]] = reactive(dict, always_update=True)

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        detected: str | None = None,
        engine_factory: EngineFactory | None = None,
        login_factory: Callable[..., StagedLogin] | None = None,
        refresher_factory: Callable[..., TokenRefresher] | None = None,
    ) -> None:
        super().__init__()
        self.switcher = switcher
        self._detected = detected  # terminal background sensed pre-driver, or None
        self._engine_factory = engine_factory or default_engine_factory
        self._login_factory = login_factory or StagedLogin
        self._refresher_factory = refresher_factory or default_refresher_factory
        self.refresher: TokenRefresher | None = None
        self.engine: EngineLike | None = None
        self.source = SnapshotSource(switcher)
        self._normal_refreshing = False
        self._store_refreshing = False
        self._normal_started_at: float | None = None
        self._refresh_generation = 0
        self._applied_generation = 0
        self._last_refresh_error = ""
        self.series = {}
        # (slot → fetched_at) the cached estimates were computed from; the
        # history file is re-read only when one of these stamps advances.
        self._estimate_stamps: dict[str, float | None] | None = None
        self._estimate_lock = threading.Lock()
        self._load_settings()
        try:
            ui = load_ui_settings(switcher.backup_dir)
        except Exception:
            ui = UiSettings()
        self._theme_name = ui.theme
        self.login_warn_days = ui.login_warn_days
        self.cadence = load_cadence(switcher.backup_dir)  # when a reading counts as overdue
        self.main_screen = MainScreen()

    def _load_settings(self) -> None:
        try:
            self.settings = load_settings(self.switcher.backup_dir)
        except Exception:
            self.settings = AutoSwitchSettings()
        self.threshold_pct: float | None = self.settings.threshold
        self.models: tuple[str, ...] = parse_model_names(self.settings.model)

    def on_mount(self) -> None:
        self.register_theme(ROTA_DARK)
        self.register_theme(ROTA_LIGHT)
        resolved = self._resolved_theme()
        # We own the theme; $TEXTUAL_THEME is intentionally not honoured.
        self.theme = f"rota-{resolved}"
        printer.set_theme(resolved)
        self.push_screen(self.main_screen)
        self._start_engine()
        self._start_refresher()
        # The only timer: the store read. Status text is recomputed whenever
        # a refresh starts or lands, so it needs no clock of its own.
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self._tick()

    def on_unmount(self) -> None:
        self._stop_engine()
        refresher, self.refresher = self.refresher, None
        if refresher is not None:
            refresher.stop()
        try:
            self.switcher.clear_poll_policy_inputs()
        except Exception:
            pass

    # -- the always-on engine ---------------------------------------------------

    def _start_engine(self) -> None:
        engine = self._engine_factory(self.switcher, self.settings, self._emit_from_thread)
        self.engine = engine
        self.auto_status = replace(
            self.auto_status,
            running=True,
            threshold=self.settings.threshold,
            cooldown_s=self.settings.cooldown_seconds,
            next_check_at=None,
        )
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name="auto-engine",
        )

    def _stop_engine(self) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            engine.stop()

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback; runs on the engine's worker thread."""
        try:
            self.call_from_thread(self._on_engine_event, event)
        except Exception:
            pass  # app tearing down mid-tick: the event has nowhere to go

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        now = time.time()
        status = self.auto_status
        kind = event.kind
        if kind == "poll":
            status = replace(status, last_check_at=now)
        elif kind == "sleep":
            status = replace(status, next_check_at=now + float(getattr(event, "seconds", 0.0)))
        elif kind == "switch":
            src = (getattr(event, "from_ref", None) or {}).get("number", "?")
            dst = (getattr(event, "to_ref", None) or {}).get("number", "?")
            status = replace(status, last_switch_at=now, last_switch=f"{src} → {dst}", last_reason="")
        elif kind == "no-switch":
            status = replace(status, last_outcome=event.human(), last_reason=event.reason)
        elif kind in ("error", "all-exhausted"):
            status = replace(status, last_outcome=event.human(), last_reason=kind)
        self.auto_status = status
        if self.main_screen.is_attached:
            self.main_screen.write_event(event)
        if kind == "switch":
            self.request_refresh()

    # -- proactive token refresh ----------------------------------------------

    def _start_refresher(self) -> None:
        self.refresher = self._refresher_factory(self.switcher, self._refresh_event_from_thread)
        self.run_worker(
            self.refresher.run_loop,
            thread=True,
            group="refresher",
            exit_on_error=False,
            name="token-refresher",
        )

    def _refresh_event_from_thread(self, event: RefreshEvent) -> None:
        try:
            self.call_from_thread(self._on_refresh_event, event)
        except Exception:
            pass  # app tearing down

    def _on_refresh_event(self, event: RefreshEvent) -> None:
        if self.main_screen.is_attached:
            self.main_screen.write_event(event)
        if event.kind in ("refreshed", "dead"):
            self.request_refresh()  # login end / re-login state changed

    def on_settings_changed(self, dotted_key: str) -> None:
        """A setting changed on the Config page: apply it live."""
        if dotted_key.startswith("autoswitch."):
            self.cadence = load_cadence(self.switcher.backup_dir)
        if dotted_key == "ui.loginWarnDays":
            try:
                self.login_warn_days = load_ui_settings(self.switcher.backup_dir).login_warn_days
            except Exception:
                return
            self.main_screen.repaint()
            return
        if dotted_key == "ui.theme":
            try:
                theme = load_ui_settings(self.switcher.backup_dir).theme
            except Exception:
                return
            self.apply_theme(theme, persist=False)
            return
        if not dotted_key.startswith("autoswitch."):
            return
        self._load_settings()
        # Engines are single-use: stop this one, start one on fresh settings.
        self._stop_engine()
        try:
            self.switcher.set_poll_policy_inputs(self.settings.threshold, self.models)
        except Exception:
            pass
        with self._estimate_lock:
            self._estimate_stamps = None  # models may have changed
        self._start_engine()
        self.request_refresh()

    def on_config_screen_setting_changed(self, message: ConfigScreen.SettingChanged) -> None:
        self.on_settings_changed(message.key)

    def action_open_config(self) -> None:
        if isinstance(self.screen, ConfigScreen):
            return
        self.push_screen(ConfigScreen(self.switcher.backup_dir))

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """The scheduled poller only reads the store: the engine fetches."""
        self._start_store_refresh()

    def _start_normal_refresh(self, *, full: bool) -> None:
        if self._normal_refreshing:
            return
        self._normal_refreshing = True
        self._normal_started_at = time.time()
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "normal", full, False),
            thread=True,
            group="refresh-normal",
            exit_on_error=False,
            name="snapshot-refresh",
        )

    def _start_store_refresh(self) -> None:
        if self._store_refreshing:
            return
        self._store_refreshing = True
        generation = self._next_refresh_generation()
        self._update_refresh_status()
        self.run_worker(
            partial(self._refresh_blocking, generation, "store", False, True),
            thread=True,
            group="refresh-store",
            exit_on_error=False,
            name="snapshot-store-refresh",
        )

    def _next_refresh_generation(self) -> int:
        self._refresh_generation += 1
        return self._refresh_generation

    def _refresh_blocking(
        self, generation: int, lane: str, full: bool, store_only: bool
    ) -> None:
        snap = self.source.take(full=full, store_only=store_only)
        estimates = self._estimates_for(snap)
        last_switch_at = self._read_last_switch_at()
        feed = statusline_setup.status(self.switcher.backup_dir)
        self.call_from_thread(
            self._apply_snapshot, generation, lane, snap, estimates, last_switch_at, feed
        )

    def _estimates_for(
        self, snap: AccountsSnapshot
    ) -> tuple[
        dict[str, AccountEstimate],
        dict[str, dict[str, list[Point]]],
        dict[str, LongTerm] | None,
    ] | None:
        """Re-read history and re-estimate, only when a fetch stamp advanced.

        Worker thread only. None means "unchanged, keep the cached ones".
        History is best effort: any failure degrades to no estimates.
        """
        stamps = {acc.number: acc.usage.fetched_at for acc in snap.accounts}
        with self._estimate_lock:
            if stamps == self._estimate_stamps:
                return None
            self._estimate_stamps = stamps
        identities = {acc.number: (acc.email, acc.org_uuid) for acc in snap.accounts}
        history = getattr(self.switcher, "usage_history", None)
        if history is None:
            return {}, {}, self._empty_long_term(identities)
        now = time.time()
        threshold = self.settings.threshold
        try:
            series = history.series(identities)
            estimates = {
                num: estimate_account(
                    series.get(num, {}), models=self.models, threshold=threshold, now=now
                )
                for num in identities
            }
            long_term = (
                self._long_term_for(history, identities, now) if self.long_view else None
            )
        except Exception as exc:
            self.log.warning(f"usage history unavailable: {exc}")
            return {}, {}, self._empty_long_term(identities)
        return estimates, series, long_term

    def _empty_long_term(self, identities) -> dict[str, LongTerm] | None:
        """No history at all: the long-term view says so instead of loading."""
        return {num: LongTerm() for num in identities} if self.long_view else None

    @staticmethod
    def _long_term_for(history, identities, now: float) -> dict[str, LongTerm]:
        """Past windows per account for the long-term view. Worker thread only."""
        out: dict[str, LongTerm] = {}
        for num, identity in identities.items():
            peaks = history.windows(identity, "5h", now - LONG_5H_SPAN_S)
            curve = history.long_series(identity, "7d", now - LONG_7D_SPAN_S)
            out[num] = LongTerm(
                peaks_5h=tuple((w.resets_at, w.peak_pct) for w in peaks),
                series_7d=tuple(curve),
            )
        return out

    def toggle_long_view(self) -> None:
        """``h``: swap the graphs between the current windows and history.
        History is read once on the way in, then again only as polls land."""
        self.long_view = not self.long_view
        if self.long_view:
            with self._estimate_lock:
                self._estimate_stamps = None
            self.request_refresh()

    def _read_last_switch_at(self) -> float | None:
        """The engine's shared ``lastSwitchAt``: covers switches made by
        another process (``rota auto``, ``rota switch``). Lock-free read.

        A stamp no newer than the claude-swap import came over with it: it is
        claude-swap's switch, not rota's, so the header shows nothing for it.
        """
        backup_dir = self.switcher.backup_dir
        try:
            raw = json.loads((backup_dir / STATE_FILENAME).read_text(encoding="utf-8"))
            value = raw.get("lastSwitchAt")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
        except Exception:
            return None
        imported = legacy_import.imported_at(backup_dir)
        if imported is not None and value <= imported:
            return None
        return float(value)

    def _apply_snapshot(
        self,
        generation: int,
        lane: str,
        snap: AccountsSnapshot,
        estimates=None,
        last_switch_at: float | None = None,
        feed: statusline_setup.StatuslineStatus | None = None,
    ) -> None:
        if estimates is not None:
            self.series = estimates[1]
            self.estimates = estimates[0]
            if estimates[2] is not None:
                self.long_term = estimates[2]
        if feed is not None:
            self.feed_installed = feed.installed
            self.feed_install = feed.install
            self.feed_state = feed.state
        status = self.auto_status
        if last_switch_at is not None and (
            status.last_switch_at is None or last_switch_at > status.last_switch_at + 1
        ):
            # A switch this session did not see as an event: no from → to.
            self.auto_status = replace(status, last_switch_at=last_switch_at, last_switch="")
        if lane == "normal":
            self._normal_refreshing = False
            self._normal_started_at = None
        else:
            self._store_refreshing = False
        self._last_refresh_error = ""
        if generation >= self._applied_generation:
            self._applied_generation = generation
            self.snapshot = snap
        elif self.snapshot is not None:
            # A later-started store repaint owns account metadata, but the
            # older worker may have completed a genuinely newer provider fetch.
            # SnapshotSource has already rejected per-account regressions, so
            # merge its canonical usage rows without restoring stale metadata.
            current = self.snapshot
            incoming = {acc.number: acc for acc in snap.accounts}
            accounts = tuple(
                replace(acc, usage=other.usage)
                if (
                    (other := incoming.get(acc.number)) is not None
                    and account_identity(acc) == account_identity(other)
                )
                else acc
                for acc in current.accounts
            )
            self.snapshot = replace(
                current,
                accounts=accounts,
                taken_at=max(current.taken_at, snap.taken_at),
            )
        self._update_refresh_status()

    def _update_refresh_status(self) -> None:
        parts: list[str] = []
        now = time.time()
        if self.snapshot is not None:
            age = max(0.0, now - self.snapshot.taken_at)
            if age >= self.SNAPSHOT_AGE_NOTE_S:
                parts.append(f"snapshot {compact_duration(age)} ago")
        if self._normal_refreshing and self._normal_started_at is not None:
            elapsed = now - self._normal_started_at
            if elapsed >= self.POLL_INTERVAL_S:
                parts.append(f"refreshing {compact_duration(elapsed)}")
        self.refresh_status = " · ".join(parts)

    def request_refresh(self, *, full: bool = False) -> None:
        """Repaint from the store now; ``full`` also asks for one normal
        (fetch-eligible) pass, which the store's gates still pace."""
        if full:
            self._start_normal_refresh(full=True)
        self._tick()

    def on_worker_state_changed(self, event) -> None:
        if event.state is not WorkerState.ERROR:
            return
        if event.worker.group in {"refresh-normal", "refresh-store"}:
            if event.worker.group == "refresh-normal":
                self._normal_refreshing = False
                self._normal_started_at = None
            else:
                self._store_refreshing = False
            self._update_refresh_status()
            msg = str(event.worker.error)
            if msg != self._last_refresh_error:
                self._last_refresh_error = msg
                lane = "store refresh" if event.worker.group == "refresh-store" else "refresh"
                self.notify(
                    f"{lane.capitalize()} failed: {msg}", severity="warning", timeout=6
                )
        elif event.worker.group == "action":
            self.busy = False
            self.notify(f"Action failed: {event.worker.error}", severity="error")
        elif event.worker.group == "refresher":
            self.notify(
                f"Token refresher stopped: {event.worker.error}", severity="error"
            )
        elif event.worker.group == "engine":
            self.auto_status = replace(self.auto_status, running=False)
            self.notify(
                f"Auto-switch engine stopped: {event.worker.error}",
                severity="error",
            )

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(
            partial(self._action_blocking, label, fn, show_output),
            thread=True,
            group="action",
            exit_on_error=False,
            name=label,
        )

    def _action_blocking(self, label: str, fn, show_output: bool) -> None:
        result = run_action(fn)
        self.call_from_thread(self._action_done, label, result, show_output)

    def _action_done(
        self, label: str, result: ActionResult, show_output: bool
    ) -> None:
        self.busy = False
        self.request_refresh()
        if not result.ok:
            self.push_screen(OutputModal(f"{label} — failed", result.output))
            return
        payload = result.payload or {}
        if "switched" in payload:
            if payload.get("switched") and self.engine is not None:
                # Let the engine re-evaluate now so the status line reflects
                # the new active account instead of waiting out its sleep.
                self.engine.wake()
            if payload.get("switched"):
                to = payload.get("to") or {}
                target = to.get("email") or f"account {to.get('number')}"
                self.notify(f"Switched to {target}", title="Switch")
            else:
                reason = str(payload.get("reason") or "no switch performed")
                self.notify(reason, title="No switch", severity="warning")
            return
        if show_output and result.output.strip():
            self.push_screen(OutputModal(label, result.output))
        elif result.first_line:
            self.notify(result.first_line)

    # -- account operations ----------------------------------------------------

    def do_switch(self, number: str) -> None:
        self._start_action(
            f"Switch to account {number}",
            partial(self.switcher.switch_to, number, json_output=True),
        )

    def do_toggle_disabled(self, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        snap = self.snapshot
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        target = not acc.disabled
        verb = "Disable" if target else "Enable"
        self._start_action(
            f"{verb} account {number}",
            partial(self.switcher.set_account_disabled, number, target),
        )

    def confirm_remove(self, number: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Remove account {number} ({email})?\n\n"
                "Its stored credentials and config backup are deleted.",
                title="Remove account",
                yes_label="Remove",
            ),
            partial(self._on_remove_confirm, number),
        )

    def _on_remove_confirm(self, number: str, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                f"Remove account {number}",
                partial(self.switcher.remove_account, number, assume_yes=True),
            )

    def action_add(self) -> None:
        """``a``: choose how to add an account (log in, current login, token)."""
        self.push_screen(AddModal(), self._on_add_choice)

    def _on_add_choice(self, choice: str | None) -> None:
        if choice == "login":
            self.start_login()
        elif choice == "current":
            self._add_current()  # picked by name in the chooser: no second question
        elif choice == "token":
            self.action_add_token()

    def _add_current(self) -> None:
        """Back up the current Claude Code login as a managed account; an
        account already managed has its stored credentials refreshed."""
        self._start_action(
            "Add current login",
            partial(self.switcher.add_account),
            show_output=True,
        )

    LOGIN_BANNER = (
        "\nrota: logging in {who} in a separate profile.\n"
        "Your active account is not touched. Ctrl+C cancels.\n"
    )

    def start_login(self, email: str | None = None) -> None:
        """Log an account in (or re-login a dead one) without touching the
        active login. The interactive ``claude auth login`` runs with the app
        suspended so its browser / paste-code flow owns the terminal; the
        capture runs in a worker. The stage is removed on every path."""
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        # Seen at once, and still on screen when the app comes back.
        self.notify(f"Logging in {email or 'an account'}…", title="Log in", timeout=4)
        stage = self._login_factory(self.switcher.backup_dir)
        try:
            with self.suspend():
                print(self.LOGIN_BANNER.format(who=email or "an account"), flush=True)
                stage.authenticate(email=email)
        except SuspendNotSupported:
            stage.cleanup()
            self.notify(
                "Logging in needs a real terminal (the app could not suspend).",
                severity="error",
            )
            return
        except LoginError as e:
            stage.cleanup()
            self.notify(str(e), title="Log in", severity="warning")
            return
        except BaseException:
            stage.cleanup()
            raise
        self._start_action("Log in", partial(self._import_login, stage), show_output=True)

    def _import_login(self, stage: StagedLogin) -> None:
        """Worker thread: store the staged login, then always drop the stage."""
        try:
            stage.import_into(self.switcher)
        finally:
            stage.cleanup()

    def action_add_token(self) -> None:
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            "Add account from token",
            partial(
                self.switcher.add_account_from_token,
                token=form.token,
                email=form.email,
                slot=form.slot,
                assume_yes=True,
            ),
            show_output=True,
        )
        occupant = self._slot_occupant(form.slot)
        if occupant is not None:
            self.push_screen(
                ConfirmModal(
                    f"Slot {form.slot} is occupied by {occupant}. Overwrite?",
                    title="Overwrite slot",
                    yes_label="Overwrite",
                ),
                lambda confirmed: run() if confirmed else None,
            )
        else:
            run()

    def _slot_occupant(self, slot: int | None) -> str | None:
        if slot is None or self.snapshot is None:
            return None
        for acc in self.snapshot.accounts:
            if acc.number == str(slot):
                return acc.email
        return None

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    # -- statusline feed -----------------------------------------------------

    def action_statusline(self) -> None:
        """``s``: set up the statusline feed, or remove it if it is set up.
        Always behind a confirm: it edits Claude Code's settings."""
        if self.feed_installed:
            message = (
                "Remove rota from Claude Code's statusline?\n\n"
                "rota stops receiving live usage from your sessions and polls "
                "the active account instead. Your previous statusline is restored."
            )
            yes, setup, label = "Remove", statusline_setup.uninstall, "Remove statusline feed"
        else:
            if self.feed_install == statusline_setup.FOREIGN:
                keeps = (
                    "Your current statusline keeps showing: rota wraps it and "
                    "only reads what it is sent."
                )
            else:
                keeps = "rota shows a minimal statusline of its own."
            message = (
                "Add rota to Claude Code's statusline?\n\n"
                "Each response then reports the active account's usage to rota "
                f"at no API cost, so it polls less. {keeps}"
            )
            yes, setup, label = "Set up", statusline_setup.install, "Set up statusline feed"
        fn = partial(self._statusline_change, setup)
        self.push_screen(
            ConfirmModal(message, title="Statusline", yes_label=yes),
            lambda confirmed: self._start_action(label, fn) if confirmed else None,
        )

    def _statusline_change(self, setup) -> None:
        """Run an install/uninstall; its failure is shown like any action's."""
        try:
            setup(self.switcher.backup_dir)
        except statusline_setup.StatuslineSetupError as exc:
            raise ClaudeSwitchError(f"Claude Code's settings were not changed: {exc}") from exc

    # -- theme --------------------------------------------------------------

    def _resolved_theme(self) -> str:
        """Concrete 'dark'/'light' for the current setting; auto → detected → dark."""
        if self._theme_name == "auto":
            return self._detected or "dark"
        return self._theme_name

    def apply_theme(self, name: str, *, persist: bool = True) -> None:
        """Switch the live theme: TUI + captured printer output + persistence.

        ``name`` is the setting; ``auto`` resolves against the pre-driver
        detection (never re-probes mid-session)."""
        self._theme_name = name
        resolved = self._resolved_theme()
        self.theme = f"rota-{resolved}"
        printer.set_theme(resolved)
        if not persist:
            return
        try:
            set_setting(self.switcher.backup_dir, "ui.theme", name)
        except Exception as exc:  # persistence is best-effort; never crash the UI
            self.notify(f"Could not save theme: {exc}", severity="warning")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.notify(f"Theme: {nxt}")
