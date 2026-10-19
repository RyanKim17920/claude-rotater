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

Nothing pops up: questions, text entry and every result go through the main
page's prompt line (:mod:`claude_rotater.tui.prompt`), never a modal or a
toast.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from functools import partial
from typing import Callable, Protocol

from textual.app import App, SuspendNotSupported
from textual.binding import Binding
from textual.reactive import reactive
from textual.worker import WorkerState

from claude_rotater import clipboard, legacy_import, printer
from claude_rotater.autoswitch import (
    ALERT_REASONS,
    STATE_FILENAME,
    AutoSwitchEvent,
    SwitchEvent,
)
from claude_rotater.estimator import AccountEstimate, estimate_fleet
from claude_rotater import statusline_setup
from claude_rotater.exceptions import ClaudeSwitchError, LoginError
from claude_rotater.instance import EngineLock, warn_if_not_owner
from claude_rotater.login import StagedLogin
from claude_rotater.logging_config import redact
from claude_rotater.token_refresh import RefreshEvent, TokenRefresher
from claude_rotater.models import AccountsSnapshot
from claude_rotater.settings import (
    AutoSwitchSettings,
    UiSettings,
    load_accounts_settings,
    load_settings,
    load_ui_settings,
    parse_model_names,
    set_setting,
)
from claude_rotater.poll_policy import load_cadence
from claude_rotater.snapshot_source import account_identity
from claude_rotater.switcher import ClaudeAccountSwitcher
from claude_rotater.tui.config import ConfigView
from claude_rotater.tui.data import ActionResult, SnapshotSource, compact_duration, run_action
from claude_rotater.tui.main import (
    LONG_5H_SPAN_S,
    LONG_7D_SPAN_S,
    AutoStatus,
    LongTerm,
    MainScreen,
    header_decision,
    same_verdict,
    switch_flash,
)
from claude_rotater.tui.prompt import Choice, PromptLine
from claude_rotater.tui.theme import ROTA_DARK, ROTA_LIGHT
from claude_rotater.usage_history import Point


_logger = logging.getLogger("rota")


def _log_login_failure(e: BaseException) -> None:
    """Record why a login did not land: the exception's class and its
    message, with emails and tokens masked."""
    _logger.warning("Login failed: %s: %s", type(e).__name__, redact(e))


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


def default_cost_source_factory(switcher):
    """The costs view's data: ccusage over this profile's transcripts, on
    demand (the view opening, or ``r``)."""
    from claude_rotater.cost_report import CostSource
    from claude_rotater.paths import get_base_claude_config_home

    home = get_base_claude_config_home(switcher.backup_dir / "sessions")
    return CostSource(
        switcher.backup_dir,
        getattr(switcher, "usage_history", None),
        [home],
        active=switcher.current_account_number,
    )


class RotaApp(App):
    """rota interactive TUI: one screen; the settings show in its body."""

    TITLE = "rota"
    CSS_PATH = "rota.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+t", "toggle_theme", show=False),
        # Quits at once, even with a question open on the prompt line.
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

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
        self.engine_lock = EngineLock(switcher.backup_dir)
        self._engine_claimed_at = 0.0
        self.engine: EngineLike | None = None
        self.source = SnapshotSource(switcher)
        self._normal_refreshing = False
        self._store_refreshing = False
        self._normal_started_at: float | None = None
        self._refresh_generation = 0
        self._applied_generation = 0
        self._last_refresh_error = ""
        self.series = {}
        # What the cached estimates were computed from (fetch stamps, slot
        # identities, the account on duty, the last switch); the history file
        # is re-read only when one of these changes.
        self._estimate_stamps: dict | None = None
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
        self._cost_source = None

    @property
    def cost_source(self):
        """Built on first use (``$``): no transcript is read before that."""
        if self._cost_source is None:
            self._cost_source = default_cost_source_factory(self.switcher)
        return self._cost_source

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
        self._claim_engine()
        # Opt-in only (accounts.proactiveTokenRefresh): see AccountsSettings.
        if load_accounts_settings(self.switcher.backup_dir).proactive_token_refresh:
            self._start_refresher()
        # The only timer: the store read. Status text is recomputed whenever
        # a refresh starts or lands, so it needs no clock of its own.
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self._tick()

    def on_unmount(self) -> None:
        self._stop_engine()
        self.engine_lock.release()
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
            mode=self.settings.mode,
            limit_guard=self.settings.manual_limit_guard,
            # A verdict reached on the old settings no longer holds.
            decision=None,
        )
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name="auto-engine",
        )

    # How often a view-only TUI checks whether the engine's owner has gone.
    ENGINE_CLAIM_RETRY_S = 60.0

    def _claim_engine(self) -> None:
        """Run the engine if this is the only rota on the data dir; else stay
        view-only and say who runs it (once per owner, not per retry)."""
        self._engine_claimed_at = time.time()
        holder = self.engine_lock.acquire()
        if holder is None:
            if self.auto_status.view_only:
                self.say("auto on: the other rota is gone, this one runs auto now", "ok")
            self.auto_status = replace(self.auto_status, view_only="")
            self._start_engine()
            return
        if self.auto_status.view_only:
            return  # already said; keep waiting
        line = warn_if_not_owner(holder)
        self.auto_status = replace(self.auto_status, running=False, view_only=line)
        # At mount the prompt line is not composed yet: say it once it is.
        self.call_after_refresh(self.say, line, "error")

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
            reason = getattr(event, "reason", "")
            if reason in ALERT_REASONS and event.human() != status.last_outcome:
                # A stop or pause needs a person: say it once on the prompt
                # line as well as in the header (not again every tick).
                self.say(event.human(), "error")
            status = replace(status, last_outcome=event.human(), last_reason=reason)
        elif kind in ("error", "all-exhausted"):
            status = replace(status, last_outcome=event.human(), last_reason=kind)
        decision = header_decision(event)
        if decision is not None and not same_verdict(decision, status.decision):
            status = replace(status, decision=decision)
        self.auto_status = status
        if self.main_screen.is_attached:
            self.main_screen.write_event(event)
        if isinstance(event, SwitchEvent):
            self.say(switch_flash(event), "ok")
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

    def on_config_view_setting_changed(self, message: ConfigView.SettingChanged) -> None:
        self.on_settings_changed(message.key)

    # -- the prompt line ----------------------------------------------------------

    @property
    def prompt(self) -> PromptLine:
        return self.main_screen.prompt

    def say(self, text: str, role: str = "info") -> None:
        """A result or an error, on the prompt line for a few seconds."""
        if self.main_screen.is_attached:
            self.prompt.say(text, role)

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """The scheduled poller only reads the store: the engine fetches."""
        self._start_store_refresh()
        if (
            self.engine is None
            and self.auto_status.view_only
            and time.time() - self._engine_claimed_at >= self.ENGINE_CLAIM_RETRY_S
        ):
            self._claim_engine()

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
        last_switch_at = self._read_last_switch_at()
        estimates = self._estimates_for(snap, last_switch_at)
        feed = statusline_setup.status(self.switcher.backup_dir)
        self.call_from_thread(
            self._apply_snapshot, generation, lane, snap, estimates, last_switch_at, feed
        )

    def _estimates_for(
        self, snap: AccountsSnapshot, last_switch_at: float | None = None
    ) -> tuple[
        dict[str, AccountEstimate],
        dict[str, dict[str, list[Point]]],
        dict[str, LongTerm] | None,
    ] | None:
        """Re-read history and re-estimate, only when a fetch stamp advanced
        or the account on duty changed.

        Each account is estimated from its own history, its slope cut at
        ``last_switch_at``: the account on duty carries the user's burn, an
        idle one only its recent outside use (see
        :func:`claude_rotater.estimator.estimate_fleet`), so a switch
        re-estimates every row even when no fetch has landed yet.

        Worker thread only. None means "unchanged, keep the cached ones".
        History is best effort: any failure degrades to no estimates.
        """
        stamps = {
            "fetched": {acc.number: acc.usage.fetched_at for acc in snap.accounts},
            "identities": {acc.number: account_identity(acc) for acc in snap.accounts},
            "active": next((acc.number for acc in snap.accounts if acc.is_active), None),
            "switch": last_switch_at,
        }
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
            estimates = estimate_fleet(
                {num: series.get(num, {}) for num in identities},
                active=stamps["active"], models=self.models, threshold=threshold,
                now=now, since=last_switch_at,
            )
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
                self.say(f"{lane} failed: {msg}", "warn")
        elif event.worker.group == "action":
            self.busy = False
            error = event.worker.error
            _logger.error(
                "Action failed: %s: %s: %s",
                event.worker.name, type(error).__name__, redact(error),
            )
            self.say(f"action failed: {error}", "error")
        elif event.worker.group == "refresher":
            self.say(f"token refresher stopped: {event.worker.error}", "error")
        elif event.worker.group == "engine":
            self.auto_status = replace(self.auto_status, running=False)
            self.say(f"auto-switch engine stopped: {event.worker.error}", "error")

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.say("another action is still running", "warn")
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
            detail = result.first_line.removeprefix("Error:").strip() or "no output"
            _logger.warning("Action failed: %s: %s", label, redact(detail))
            self.say(f"{label.lower()} failed: {detail}", "error")
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
                self.say(f"switched to {target}", "ok")
            else:
                reason = str(payload.get("reason") or "no switch performed")
                self.say(f"no switch: {reason}", "warn")
            return
        if result.first_line:
            self.say(result.first_line, "ok" if show_output else "info")

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
        self.prompt.confirm(
            f"remove {email} and its stored login?",
            partial(self._on_remove_confirm, number),
            yes="remove",
        )

    def _on_remove_confirm(self, number: str, confirmed: bool) -> None:
        if confirmed:
            self._start_action(
                f"Remove account {number}",
                partial(self.switcher.remove_account, number, assume_yes=True),
            )

    ADD_CHOICES = (
        Choice("b", "browser login", "login"),
        Choice("c", "current login", "current"),
        Choice("t", "paste token", "token"),
    )

    def action_add(self) -> None:
        """``a``: choose how to add an account (log in, current login, token)."""
        self.prompt.ask("add:", self.ADD_CHOICES, self._on_add_choice)

    def _on_add_choice(self, choice: object | None) -> None:
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
            self.say("another action is still running", "warn")
            return
        _logger.info("Login started: %s", self._login_target(email))
        # Seen at once, and still on screen when the app comes back.
        self.say(f"logging in {email or 'an account'}…")
        stage = self._login_factory(self.switcher.backup_dir)
        try:
            with self.suspend():
                print(self.LOGIN_BANNER.format(who=email or "an account"), flush=True)
                stage.authenticate(email=email)
        except SuspendNotSupported as e:
            stage.cleanup()
            _log_login_failure(e)
            self.say("logging in needs a real terminal (the app could not suspend)", "error")
            return
        except LoginError as e:
            stage.cleanup()
            _log_login_failure(e)
            self.say(f"log in: {e}", "warn")
            return
        except BaseException as e:
            stage.cleanup()
            _log_login_failure(e)
            raise
        self._start_action("Log in", partial(self._import_login, stage), show_output=True)

    def _login_target(self, email: str | None) -> str:
        """What a login is for, by slot number only (rota.log never holds
        an email)."""
        if email is None:
            return "any account"
        snap = self.snapshot
        number = next(
            (a.number for a in (snap.accounts if snap else ()) if a.email == email),
            None,
        )
        return f"re-login of slot {number}" if number else "re-login of an account"

    def _import_login(self, stage: StagedLogin) -> None:
        """Worker thread: store the staged login, then always drop the stage."""
        try:
            stage.import_into(self.switcher)
        except BaseException as e:
            _log_login_failure(e)
            raise
        else:
            _logger.info("Login finished")
        finally:
            stage.cleanup()

    def action_add_token(self) -> None:
        """``t``: the token, then an optional email label and slot, each on
        the prompt line; enter skips an optional one, esc drops the lot."""
        self.prompt.read(
            "token:", self._on_token, secret=True, note="setup-token or API key"
        )

    def _on_token(self, token: str | None) -> None:
        if token is None:
            return
        if not token:
            self.say("no token given: nothing added", "warn")
            return
        self.prompt.read(
            "email label:", partial(self._on_token_email, token), note="optional, ⏎ skips"
        )

    def _on_token_email(self, token: str, email: str | None) -> None:
        if email is None:
            return
        self._ask_slot(token, email or None)

    def _ask_slot(self, token: str, email: str | None, note: str = "optional, ⏎ next free") -> None:
        self.prompt.read("slot:", partial(self._on_token_slot, token, email), note=note)

    def _on_token_slot(self, token: str, email: str | None, raw: str | None) -> None:
        if raw is None:
            return
        slot: int | None = None
        if raw:
            try:
                slot = int(raw)
            except ValueError:
                slot = 0
            if slot < 1:  # asked again, the reason in place of the note
                self._ask_slot(token, email, note=f"{raw!r} is not a slot: a number from 1")
                return
        run = partial(
            self._start_action,
            "Add account from token",
            partial(
                self.switcher.add_account_from_token,
                token=token,
                email=email,
                slot=slot,
                assume_yes=True,
            ),
            show_output=True,
        )
        occupant = self._slot_occupant(slot)
        if occupant is None:
            run()
            return
        self.prompt.confirm(
            f"slot {slot} holds {occupant}: overwrite?",
            lambda confirmed: run() if confirmed else None,
            yes="overwrite",
        )

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
        self.say("refreshing usage…")

    # -- statusline feed -----------------------------------------------------

    def action_statusline(self) -> None:
        """``s``: set up the statusline feed, or remove it if it is set up.
        Always asked first: it edits Claude Code's settings."""
        if self.feed_installed:
            question = "remove rota from Claude Code's statusline (your previous one comes back)?"
            yes, setup, label = "remove", statusline_setup.uninstall, "Remove statusline feed"
        else:
            if self.feed_install == statusline_setup.FOREIGN:
                keeps = "yours keeps showing, rota wraps it"
            else:
                keeps = "rota shows a minimal one"
            question = f"feed usage to rota from Claude Code's statusline ({keeps})?"
            yes, setup, label = "set up", statusline_setup.install, "Set up statusline feed"
        fn = partial(self._statusline_change, setup)
        self.prompt.confirm(
            question,
            lambda confirmed: self._start_action(label, fn) if confirmed else None,
            yes=yes,
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
            self.say(f"could not save the theme: {exc}", "warn")

    def copy_text(self, text: str, what: str) -> None:
        """Put ``text`` on the clipboard: the system's clipboard command,
        else the terminal's OSC 52; confirm on the prompt line."""
        if clipboard.system_copy(text):
            self.say(f"copied {what}", "ok")
        else:
            self.copy_to_clipboard(text)  # OSC 52: the terminal does the copy
            self.say(f"copied {what} (via the terminal)", "ok")

    def toggle_mode(self) -> None:
        """``m``: flip between auto and manual, saved in this profile's
        settings, and restart the engine on it."""
        mode = "auto" if self.settings.mode == "manual" else "manual"
        try:
            set_setting(self.switcher.backup_dir, "autoswitch.mode", mode)
        except Exception as exc:
            self.say(f"could not change the mode: {exc}", "error")
            return
        self.on_settings_changed("autoswitch.mode")
        if mode == "auto":
            self.say("auto: rota switches accounts on its own")
        elif self.settings.manual_limit_guard:
            self.say("manual: you pick; rota steps in only at a hard limit")
        else:
            self.say("manual: you pick; rota never switches")

    def action_toggle_theme(self) -> None:
        order = ("dark", "light", "auto")
        nxt = order[(order.index(self._theme_name) + 1) % len(order)]
        self.apply_theme(nxt)
        self.say(f"theme: {nxt}")
