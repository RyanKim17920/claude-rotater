"""Tests for the Textual TUI: data service units + Pilot-driven app tests.

The Pilot tests run the real app headlessly against a ``FakeSwitcher`` that
implements exactly the structured surface the TUI consumes
(``accounts_snapshot``, ``switch_to``/``switch``/``remove_account``/add
flows) — no scraping, no real credentials, no network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_rotater.autoswitch import NoSwitchEvent, PollEvent, SleepEvent, SwitchEvent
from claude_rotater.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.switcher import ClaudeAccountSwitcher
from claude_rotater.tui import data as tui_data
from claude_rotater.usage_store import UsageEntry

sys.path.insert(0, str(Path(__file__).parent))
from _fake_history import FakeHistory, linear_points  # noqa: E402
from claude_rotater.usage_history import Point  # noqa: E402


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def has_bar(row: str) -> bool:
    """A usage meter is drawn: eighth-block fill or the threshold tick
    (past the "▌ 1 " prefix, whose on-duty bar is also an eighth block)."""
    from claude_rotater.tui.widgets import BAR_EIGHTHS, BAR_TICK

    return any(ch in BAR_EIGHTHS[1:] + BAR_TICK for ch in row[4:])


def make_entry(
    pct5: float | None = 25.0,
    pct7: float | None = 10.0,
    *,
    sentinel: str | None = None,
    age_s: float = 5.0,
    scoped: list[tuple[str, float]] | None = None,
    spend: dict | None = None,
) -> UsageEntry:
    """``pct5``/``pct7`` of None omit that window (e.g. annual plans lack 7d)."""
    if sentinel is not None:
        return UsageEntry(sentinel=sentinel)
    last_good: dict = {}
    if pct5 is not None:
        last_good["five_hour"] = {"pct": pct5, "resets_at": _iso_in(7200)}
    if pct7 is not None:
        last_good["seven_day"] = {"pct": pct7, "resets_at": _iso_in(86400 * 3)}
    if scoped is not None:
        last_good["scoped"] = [
            {"name": name, "pct": pct, "resets_at": _iso_in(86400 * 2)}
            for name, pct in scoped
        ]
    if spend is not None:
        last_good["spend"] = spend
    return UsageEntry(
        last_good=last_good,
        fetched_at=time.time() - age_s,
        age_s=age_s,
    )


def make_account(
    number: int | str,
    *,
    active: bool = False,
    switchable: bool = True,
    kind: str = "oauth",
    entry: UsageEntry | None = None,
    email: str | None = None,
    alias: str = "",
    disabled: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=str(number),
        email=email or f"user{number}@example.com",
        org_name="",
        org_uuid="",
        is_active=active,
        kind=kind,
        switchable=switchable,
        usage=entry if entry is not None else make_entry(),
        alias=alias,
        disabled=disabled,
    )


def make_usage_at(
    fetched_at: float | None,
    pct: float = 25.0,
    *,
    sentinel: str | None = None,
) -> UsageEntry:
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": pct, "resets_at": _iso_in(7200)}},
        fetched_at=fetched_at,
        age_s=(time.time() - fetched_at) if fetched_at is not None else None,
    )


class FakeSwitcher:
    """Structured-surface stand-in for ClaudeAccountSwitcher."""

    def __init__(self, accounts: list[AccountSnapshot], backup_dir: Path):
        self._accounts = list(accounts)
        self.backup_dir = backup_dir
        self.active = next(
            (a.number for a in accounts if a.is_active), None
        )
        self.calls: list[tuple] = []
        self.fetch_sets: list[set[str] | None] = []

    # -- surface the TUI consumes ------------------------------------------

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        return AccountsSnapshot(
            active_number=self.active,
            accounts=tuple(self._accounts),
            taken_at=time.time(),
        )

    def current_account_number(self) -> str | None:
        return self.active

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict:
        self.calls.append(("switch_to", str(identifier)))
        old = self.active
        self.active = str(identifier)
        self._accounts = [
            dataclasses.replace(a, is_active=(a.number == self.active))
            for a in self._accounts
        ]
        return {
            "switched": True,
            "from": {"number": int(old) if old else None, "email": ""},
            "to": {
                "number": int(identifier),
                "email": f"user{identifier}@example.com",
            },
            "reason": "requested",
        }

    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict:
        self.calls.append(("switch", strategy))
        return {"switched": False, "from": None, "to": None, "reason": "no-better-target"}

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        self.calls.append(("remove", str(identifier), assume_yes))
        self._accounts = [a for a in self._accounts if a.number != str(identifier)]
        print(f"Removed account {identifier}")

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        self.calls.append(("set_disabled", str(identifier), disabled))
        self._accounts = [
            dataclasses.replace(a, disabled=disabled)
            if a.number == str(identifier)
            else a
            for a in self._accounts
        ]
        verb = "Disabled" if disabled else "Enabled"
        print(f"{verb} Account-{identifier}")

    def add_account(self, slot: int | None = None, assume_yes: bool = False) -> None:
        self.calls.append(("add", slot, assume_yes))
        print("Added Account 9: fresh@example.com")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        self.calls.append(("add_token", token, email, slot, assume_yes))
        print(f"Added Account {slot or 9}")

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        self._poll_inputs_override = None


class BlockingSnapshotSwitcher(FakeSwitcher):
    """Fake switcher with independently gated normal/store snapshot lanes."""

    def __init__(
        self,
        normal_account: AccountSnapshot,
        store_account: AccountSnapshot,
        backup_dir: Path,
    ):
        super().__init__([normal_account], backup_dir)
        self.normal_account = normal_account
        self.store_account = store_account
        self.normal_started = threading.Event()
        self.normal_release = threading.Event()
        self.normal_done = threading.Event()
        self.store_started = threading.Event()
        self.store_release = threading.Event()
        self.store_done = threading.Event()
        self.block_store = False

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        self.fetch_sets.append(fetch)
        if fetch is None:
            self.normal_started.set()
            self.normal_release.wait(timeout=2)
            self.normal_done.set()
            account = self.normal_account
        else:
            self.store_started.set()
            if self.block_store:
                self.store_release.wait(timeout=2)
            self.store_done.set()
            account = self.store_account
        return AccountsSnapshot(
            active_number=account.number,
            accounts=(account,),
            taken_at=time.time(),
        )


class FakeEngine:
    """Stands in for AutoSwitchEngine: records construction, blocks until stop.

    Emits one NoSwitchEvent at start (like a real first tick in cooldown);
    tests push more through :meth:`emit`. ``crash`` makes run_loop raise.
    """

    instances: list["FakeEngine"] = []
    crash = False

    def __init__(self, switcher, settings, on_event, **kwargs):
        self.switcher = switcher
        self.settings = settings
        self.on_event = on_event
        self.kwargs = kwargs
        self.stopped = False
        self.applied_thresholds: list[float] = []
        self.wakes = 0
        self._stop = threading.Event()
        FakeEngine.instances.append(self)

    def run_loop(self) -> int:
        if FakeEngine.crash:
            raise RuntimeError("engine exploded")
        self.on_event(NoSwitchEvent(reason="cooldown"))
        self._stop.wait(30)
        return 0

    def emit(self, event) -> None:
        self.on_event(event)

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def apply_threshold(self, threshold: float) -> None:
        self.settings = dataclasses.replace(self.settings, threshold=threshold)
        self.applied_thresholds.append(threshold)

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    """Every app in this module hosts a FakeEngine: a real engine would
    fetch and switch against the FakeSwitcher on its own schedule."""
    FakeEngine.instances = []
    FakeEngine.crash = False
    monkeypatch.setattr("claude_rotater.tui.app.default_engine_factory", FakeEngine)
    return FakeEngine


class FakeRefresher:
    """Stands in for TokenRefresher: blocks until stopped, emits on demand."""

    instances: list["FakeRefresher"] = []

    def __init__(self, switcher, on_event):
        self.switcher = switcher
        self.on_event = on_event
        self.stopped = False
        self._stop = threading.Event()
        FakeRefresher.instances.append(self)

    def run_loop(self) -> int:
        self._stop.wait(30)
        return 0

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    def wake(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_refresher(monkeypatch):
    """A real refresher would read credentials off the FakeSwitcher."""
    FakeRefresher.instances = []
    monkeypatch.setattr("claude_rotater.tui.app.default_refresher_factory", FakeRefresher)
    return FakeRefresher


def make_app(fake: FakeSwitcher):
    from claude_rotater.tui.app import RotaApp

    return RotaApp(fake)


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until the app stops it,
    so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group not in ("engine", "refresher")]
    if pending:
        await app.workers.wait_for_complete(pending)
    await pilot.pause()
    await pilot.pause()


async def wait_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


# ---------------------------------------------------------------------------
# Data service units (sync)
# ---------------------------------------------------------------------------


class TestFormatting:
    def test_format_duration(self):
        assert tui_data.format_duration(42) == "42s"
        assert tui_data.format_duration(180) == "3m"
        assert tui_data.format_duration(7980) == "2h 13m"
        assert tui_data.format_duration(3600 * 26) == "1d 2h"

    def test_format_age_fresh_is_silent(self):
        # Ages inside the serve TTL are the polling cadence at work, not
        # staleness worth flagging.
        assert tui_data.format_age(3.0) is None
        assert tui_data.format_age(120) is None
        assert tui_data.format_age(None) is None
        assert tui_data.format_age(400) == "· 6m ago"

    def test_sentinel_labels_match_rota_list(self):
        # The TUI must describe sentinel states with the exact wording `rota
        # list` prints — owned-and-expired means Claude Code refreshes the
        # active account, not that the user must re-login.
        assert (
            tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            == "token expired — refresh deferred this pass; retries automatically"
        )
        from claude_rotater.switcher import SENTINEL_NOTES

        for sentinel, note in SENTINEL_NOTES.items():
            assert tui_data.sentinel_label(sentinel) == note
        assert tui_data.sentinel_label("unknown state") == "unknown state"

    def test_sentinel_card_shows_last_seen_like_rota_list(self):
        # A sentinel is a live overlay — the entry can still carry the last
        # good measurement, and `rota list` prints it as a "last seen" line.
        # The card must too (except for API-key accounts, which have no quota).
        from claude_rotater.tui.widgets import account_card_text

        entry = UsageEntry(
            sentinel=USAGE_TOKEN_EXPIRED,
            last_good={"five_hour": {"pct": 53.0}},
            fetched_at=time.time() - 720,
            age_s=720.0,
        )
        card = account_card_text(make_account(1, active=True, entry=entry), 80).plain
        assert "token expired — refresh deferred this pass; retries automatically" in card
        assert "last seen 53% used" in card

        no_history = account_card_text(
            make_account(1, entry=UsageEntry(sentinel=USAGE_TOKEN_EXPIRED)), 80
        ).plain
        assert "last seen" not in no_history

        api_key = account_card_text(
            make_account(
                1,
                kind="api_key",
                entry=dataclasses.replace(entry, sentinel=USAGE_API_KEY),
            ),
            80,
        ).plain
        assert "last seen" not in api_key

    def test_account_card_uses_light_palette_when_passed(self):
        from claude_rotater.tui.theme import ACCENT_LIGHT, ROTA_LIGHT, Palette
        from claude_rotater.tui.widgets import account_card_text

        acc = make_account(1, active=True, entry=make_entry(pct5=95.0))
        text = account_card_text(acc, 100, palette=Palette.from_theme(ROTA_LIGHT))
        styles = {str(span.style) for span in text.spans}
        assert any(ACCENT_LIGHT in s for s in styles)  # active marker uses light accent

    def test_window_helpers(self):
        entry = make_entry(pct5=47.0)
        assert tui_data.window_pct(entry.last_good, "five_hour") == 47.0
        assert tui_data.window_pct(None, "five_hour") is None
        text = tui_data.window_reset_text(entry.last_good, "five_hour", time.time())
        assert text is not None and text.startswith("resets ")
        assert tui_data.window_reset_text(None, "five_hour", time.time()) is None

    def test_reset_clock(self):
        # Same-day reset → bare HH:MM; a reset days out carries its date.
        now = time.time()
        entry = make_entry()  # 5h resets in 2h, 7d in 3d
        clock5 = tui_data.reset_clock(entry.last_good["five_hour"], now)
        assert clock5 is not None and clock5.count(":") == 1
        clock7 = tui_data.reset_clock(entry.last_good["seven_day"], now)
        import calendar

        months = list(calendar.month_abbr)[1:]
        assert clock7 is not None and any(m in clock7 for m in months)

    def test_reset_clock_unknown_or_elapsed_is_none(self):
        now = time.time()
        assert tui_data.reset_clock(None, now) is None
        assert tui_data.reset_clock({"pct": 5.0}, now) is None
        assert tui_data.reset_clock({"resets_at": "garbage"}, now) is None
        # elapsed reset: the row says "resets now" — no clock to show
        elapsed = {"resets_at": _iso_in(-60)}
        assert tui_data.reset_clock(elapsed, now) is None
        assert tui_data.reset_text(elapsed, now) == "resets now"


class TestSnapshotSource:
    def _source(self, tmp_path: Path, accounts=None):
        fake = FakeSwitcher(
            accounts
            or [make_account(1, active=True), make_account(2)],
            tmp_path,
        )
        return fake, tui_data.SnapshotSource(fake)

    def test_every_pass_is_store_governed(self, tmp_path):
        # Pacing lives in the usage store (poll plans + freshness + atomic
        # reservation), so every take is the same on-demand pass `rota list`
        # runs — including the user's explicit refresh, which cannot bypass
        # the store's per-account cadence.
        fake, source = self._source(tmp_path)
        source.take()
        source.take()
        source.take(full=True)
        assert fake.fetch_sets == [None, None, None]

    def test_store_only_never_fetches(self, tmp_path):
        fake, source = self._source(tmp_path)
        source.take(store_only=True)
        assert fake.fetch_sets == [set()]

    def test_expired_sentinel_retained_until_fetched_at_advances(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fresh_same_stamp = make_account(1, active=True, entry=make_usage_at(100.0))
        fresh_new_stamp = make_account(1, active=True, entry=make_usage_at(101.0))
        fake, source = self._source(tmp_path, [expired])

        assert source.take().accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_same_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_TOKEN_EXPIRED
        fake._accounts = [fresh_new_stamp]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_expired_sentinel_clears_on_superseding_sentinel(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        api_key = make_account(
            1,
            active=True,
            kind="api_key",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [api_key]
        assert source.take(store_only=True).accounts[0].usage.sentinel == USAGE_API_KEY

    def test_expired_sentinel_clears_on_identity_replacement(self, tmp_path):
        expired = make_account(
            1,
            active=True,
            email="old@example.com",
            entry=make_usage_at(100.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        replacement = make_account(
            1,
            active=True,
            email="new@example.com",
            entry=make_usage_at(100.0),
        )
        fake, source = self._source(tmp_path, [expired])

        source.take()
        fake._accounts = [replacement]
        assert source.take(store_only=True).accounts[0].usage.sentinel is None

    def test_late_worker_fetched_at_regression_is_rejected(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(1, active=True, entry=make_usage_at(100.0, pct=10.0))
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        snap = source.take(store_only=True)
        usage = snap.accounts[0].usage
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0

    def test_late_expired_sentinel_cannot_replace_newer_usage(self, tmp_path):
        newer = make_account(1, active=True, entry=make_usage_at(200.0, pct=80.0))
        older = make_account(
            1,
            active=True,
            entry=make_usage_at(100.0, pct=10.0, sentinel=USAGE_TOKEN_EXPIRED),
        )
        fake, source = self._source(tmp_path, [newer])

        source.take()
        fake._accounts = [older]
        usage = source.take(store_only=True).accounts[0].usage
        assert usage.sentinel is None
        assert usage.fetched_at == 200.0
        assert usage.last_good["five_hour"]["pct"] == 80.0


class TestUsageRows:
    """The card's rows must mirror the CLI's _format_usage_lines semantics."""

    def test_absent_window_produces_no_row(self):
        from claude_rotater.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0, pct7=None)  # annual plan: no 7d window
        labels = [label for label, *_ in usage_rows(entry.last_good, time.time())]
        assert labels == ["5h"]

    def test_scoped_models_and_over_limit_marker(self):
        from claude_rotater.tui.widgets import usage_rows

        entry = make_entry(scoped=[("Fable", 100.0), ("Opus", 12.0)])
        rows = usage_rows(entry.last_good, time.time())
        labels = [label for label, *_ in rows]
        assert labels == ["5h", "7d", "Fable", "Opus"]
        fable = next(row for row in rows if row[0] == "Fable")
        assert "(!)" in fable[2]
        # the marker stays terminal in the clock-extended variant too
        assert fable[3].endswith("(!)") and " · " in fable[3]

    def test_spend_row_first_with_amounts(self):
        from claude_rotater.tui.widgets import usage_rows

        entry = make_entry(spend={"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"})
        rows = usage_rows(entry.last_good, time.time())
        assert rows[0][0] == "$$"
        assert "$12.50 / $50.00" in rows[0][2]

    def test_suffix_full_extends_countdown_with_clock(self):
        from claude_rotater.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0)
        row5 = usage_rows(entry.last_good, time.time())[0]
        assert row5[2].startswith("resets ")
        assert row5[3].startswith(row5[2] + " · ")

    def test_spend_clock_sits_with_reset_not_after_amounts(self):
        from claude_rotater.tui.widgets import usage_rows

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        spend = usage_rows(entry.last_good, time.time())[0]
        assert spend[0] == "$$"
        assert " · " in spend[3]
        assert spend[3].index(" · ") < spend[3].index("$12.50")

    def test_no_data_no_rows(self):
        from claude_rotater.tui.widgets import usage_rows

        assert usage_rows(None, time.time()) == []
        assert usage_rows({}, time.time()) == []

    def test_seven_day_ahead_of_pace_marker(self):
        # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
        from claude_rotater.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]
        assert "(ahead of pace)" in row[3]

    def test_five_hour_never_shows_pace_marker(self):
        from claude_rotater.tui.widgets import usage_rows

        now = time.time()
        last_good = {"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}}
        row = usage_rows(last_good, now, now)[0]
        assert "pace" not in row[2]

    def test_scoped_ahead_of_pace_marker(self):
        from claude_rotater.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]

    def test_maxed_scoped_marker_wins_over_pace(self):
        from claude_rotater.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(!)" in row[2]
        assert "ahead of pace" not in row[2]

    def test_no_pace_marker_without_fetched_at(self):
        from claude_rotater.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now)[0]
        assert "pace" not in row[2]

    def test_card_shows_clock_only_where_it_fits(self):
        # Per-row degradation: the wide card shows every clock, a mid width
        # keeps 5h/7d clocks while the longer spend row falls back to its
        # countdown, and a narrow card is exactly the old countdown-only look.
        from claude_rotater.tui.widgets import account_card_text

        entry = make_entry(
            spend={
                "used": 12.5,
                "limit": 50.0,
                "pct": 25.0,
                "currency": "USD",
                "resets_at": _iso_in(7200),
            }
        )
        acc = make_account(1, active=True, entry=entry)

        wide = account_card_text(acc, 100).plain
        assert wide.count(" · ") == 3

        mid_lines = account_card_text(acc, 78).plain.splitlines()
        spend_line = next(line for line in mid_lines if "$12.50" in line)
        assert " · " not in spend_line
        for line in mid_lines:
            if "resets" in line and "$12.50" not in line:
                assert " · " in line

        narrow = account_card_text(acc, 40).plain
        assert " · " not in narrow


class TestAccountRowText:
    """The one-line account row and the column math it shares with the header."""

    def _layout(self, width: int = 120, windows=("5h", "7d"), **kw):
        from claude_rotater.tui.main import RowLayout

        return RowLayout.for_width(width, windows, **kw)

    def _row(self, acc, est=None, *, width=120, threshold=90.0, now=None, windows=("5h", "7d"), series=None, **kw):
        from claude_rotater.tui.main import account_row_text

        return account_row_text(
            acc,
            est,
            self._layout(width, windows, **kw),
            now=now if now is not None else time.time(),
            threshold=threshold,
            series=series,
        ).plain

    def test_active_marker_and_pcts(self):
        row = self._row(make_account(1, active=True, entry=make_entry(62.0, 31.0)))
        assert row.startswith("▌ 1 ")
        assert "62%" in row and "31%" in row
        assert has_bar(row)  # wide layout carries meters

    def test_inactive_has_no_marker(self):
        row = self._row(make_account(2, entry=make_entry(12.0, 44.0)))
        assert row.startswith("  2 ")

    def test_disabled_tag_shown_personal_tag_hidden(self):
        row = self._row(make_account(3, disabled=True), tag_need=8)
        assert "disabled" in row
        plain = self._row(make_account(3), tag_need=8)
        assert "personal" not in plain and "disabled" not in plain

    def test_email_shown_once_for_a_personal_org(self):
        acc = dataclasses.replace(
            make_account(1, email="me@x.com"), org_name="me@x.com's Organization", org_uuid="o"
        )
        row = self._row(acc, tag_need=24)
        assert row.count("me@x.com") == 1

    def test_real_org_is_tagged(self):
        acc = dataclasses.replace(make_account(1, email="me@x.com"), org_name="Acme Corp", org_uuid="o")
        assert "Acme Corp" in self._row(acc, tag_need=9)

    def test_absent_window_is_a_dot_not_invented(self):
        row = self._row(make_account(1, entry=make_entry(pct5=92.0, pct7=None)))
        assert "92%" in row
        assert "·" in row

    def test_sentinel_replaces_usage(self):
        row = self._row(make_account(1, entry=make_entry(sentinel=USAGE_TOKEN_EXPIRED)))
        assert tui_data.sentinel_label(USAGE_TOKEN_EXPIRED) in row
        assert "%" not in row

    def test_no_usage_says_unknown(self):
        row = self._row(make_account(1, entry=UsageEntry()))
        assert "usage unknown" in row

    def test_no_estimate_shows_dash_eta_and_burn(self):
        row = self._row(make_account(1, entry=make_entry(40.0, 10.0)))
        assert "—" in row
        assert "%/h" not in row

    def test_eta_and_burn_from_estimate(self):
        from claude_rotater.estimator import WindowEstimate, AccountEstimate

        now = time.time()
        # measured 60% 30 min ago at 10%/h: 90% in 2.5h
        win = WindowEstimate("5h", 60.0, now - 1800, now + 4 * 3600, 10.0, "medium", ())
        est = AccountEstimate({"5h": win}, win, None, None, "medium")
        acc = make_account(1, active=True, entry=make_entry(60.0, 10.0))
        row = self._row(acc, est, now=now)
        assert "+10.0%/h" in row
        assert "2h30m~" in row
        assert "→" not in row  # the projection lives in the strip

    def test_reset_countdown_and_clock_per_window(self):
        now = time.time()
        acc = make_account(1, entry=make_entry(80.0, 10.0))
        row = self._row(acc, now=now, width=160)
        five = tui_data.iso_epoch(acc.usage.last_good["five_hour"]["resets_at"])
        seven = tui_data.iso_epoch(acc.usage.last_good["seven_day"]["resets_at"])
        assert tui_data.compact_duration(five - now) in row
        assert time.strftime("%H:%M", time.localtime(five)) in row
        assert tui_data.compact_duration(seven - now) in row
        assert time.strftime("%a %H:%M", time.localtime(seven)) in row

    def test_narrow_layout_drops_bars_keeps_name_and_resets(self):
        acc = make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com")
        row = self._row(acc, width=70)
        assert "me@x.com" in row and "62%" in row
        assert not has_bar(row)
        assert "1h59m" in row or "2h00m" in row
        assert len(row) <= 70

    def test_model_window_column(self):
        entry = make_entry(10.0, 20.0, scoped=[("Fable", 55.0)])
        acc = make_account(1, entry=entry)
        row = self._row(acc, width=160, windows=("5h", "7d", "Fable"))
        reset = tui_data.iso_epoch(entry.last_good["scoped"][0]["resets_at"])
        assert "55%" in row
        assert "↻" + time.strftime("%a", time.localtime(reset)) in row

    def test_model_column_without_that_window_is_a_dot(self):
        row = self._row(make_account(1, entry=make_entry(10.0, 20.0)), width=160, windows=("5h", "7d", "Fable"))
        assert "·" in row

    def test_sparkline_of_the_current_5h_window(self):
        now = time.time()
        acc = make_account(1, entry=make_entry(62.0, 31.0))
        series = burn_history(now)["1"]
        row = self._row(acc, now=now, width=200, series=series)
        assert any(ch in row for ch in "▁▂▃▄▅▆▇█"[3:])


class TestRowLayout:
    def test_wide_gets_full_bars_clocks_and_trend(self):
        from claude_rotater.tui.main import RowLayout

        layout = RowLayout.for_width(160, ("5h", "7d"))
        level = layout.level
        assert layout.bar_w == 8 and level.clock == "full" and level.spark_w and level.burn and level.eta

    def test_degrades_in_order(self):
        from claude_rotater.tui.main import LEVELS, RowLayout

        widths = [RowLayout.for_width(w, ("5h", "7d", "Fable"), tag_need=9) for w in (200, 160, 120, 95, 80, 66, 50)]
        ranks = [LEVELS.index(lay.level) for lay in widths]
        assert ranks == sorted(ranks)
        assert not widths[-1].level.burn
        assert all(lay.name_w >= 16 for lay in widths)

    def test_names_never_squeezed_below_minimum(self):
        from claude_rotater.tui.main import RowLayout

        layout = RowLayout.for_width(78, ("5h", "7d", "Fable"), name_need=40, tag_need=9)
        assert 16 <= layout.name_w <= 32

    def test_header_matches_row_columns(self):
        from claude_rotater.tui.main import RowLayout, account_row_text, header_text

        layout = RowLayout.for_width(160, ("5h", "7d"))
        header = header_text(layout, threshold=90.0).plain
        row = account_row_text(
            make_account(1, entry=make_entry(62.0, 31.0)), None, layout,
            now=time.time(), threshold=90.0,
        ).plain
        col = header.index("7d")
        # the 7d pct starts exactly under its label, its meter right after
        assert row[col : col + 4] == " 31%"
        assert row[col + 5 : col + 7] == "██"
        assert "ETA→90%" in header and "burn" in header

    def test_header_names_model_columns_in_full(self):
        from claude_rotater.tui.main import RowLayout, header_text

        for width in (80, 120, 160):
            layout = RowLayout.for_width(width, ("5h", "7d", "Sonnet"))
            assert "Sonnet" in header_text(layout, threshold=90.0).plain, width

    def test_long_threshold_label_drops_eta_prefix(self):
        from claude_rotater.tui.main import RowLayout, header_text

        header = header_text(RowLayout.for_width(120, ("5h", "7d")), threshold=99.9).plain
        assert "→99.9%" in header and "ETA→99.9%" not in header


class TestCompactDuration:
    @pytest.mark.parametrize(
        "seconds, expected",
        [(-5, "<1m"), (30, "<1m"), (60, "1m"), (59 * 60, "59m"), (3600, "1h00m"),
         (7140, "1h59m"), (86399, "23h59m"), (86400, "1d0h"), (6 * 86400 + 23 * 3600, "6d23h")],
    )
    def test_format(self, seconds, expected):
        assert tui_data.compact_duration(seconds) == expected
        assert len(tui_data.compact_duration(seconds)) <= 6

    def test_reset_clock_text(self):
        from claude_rotater.tui.main import reset_clock_text

        t = time.mktime((2026, 10, 19, 17, 49, 0, 0, 0, -1))  # a Monday
        assert reset_clock_text("5h", t) == "17:49"
        assert reset_clock_text("7d", t) == "Mon 17:49"
        assert reset_clock_text("Fable", t, "short") == "Mon"


class TestStatusText:
    def _status(self, **kw):
        from claude_rotater.tui.main import AutoStatus

        return AutoStatus(**kw)

    def _text(self, status, now=None, width=120):
        from claude_rotater.tui.main import status_text

        return status_text(status, now=now or time.time(), width=width).plain

    def test_running_threshold_and_next_check(self):
        now = time.time()
        text = self._text(
            self._status(running=True, threshold=90.0, next_check_at=now + 80), now
        )
        assert "auto on" in text and "switch at 90%" in text and "next 1m" in text
        assert text.startswith(" rota ")

    def test_stopped_engine_says_off(self):
        assert "auto off" in self._text(self._status(running=False))

    def test_roster_pips_mark_duty_and_disabled(self):
        from claude_rotater.tui.main import RosterPip, status_text

        roster = [RosterPip(True, False, 62.0), RosterPip(False, False, 95.0), RosterPip(False, True, None)]
        text = status_text(self._status(running=True), now=time.time(), width=120, roster=roster)
        assert "◉●◌" in text.plain
        pip_style = next(str(sp.style) for sp in text.spans if text.plain[sp.start:sp.end] == "●")
        from claude_rotater.tui.theme import SEV_CRIT

        assert SEV_CRIT in pip_style  # the 95% account glows red in the roster

    @pytest.mark.parametrize(
        "state,words",
        [("live", "● feed live"), ("stale", "◐ feed stale"), ("off", "○ feed off"), ("unset", "○ feed: not set up")],
    )
    def test_feed_indicator(self, state, words):
        from claude_rotater.tui.main import status_text

        text = status_text(self._status(running=True), now=time.time(), width=120, feed=state).plain
        assert words in text

    def test_no_feed_state_no_indicator(self):
        assert "feed" not in self._text(self._status(running=True))

    def test_low_priority_parts_drop_first_when_narrow(self):
        from claude_rotater.tui.main import status_text

        now = time.time()
        status = self._status(
            running=True, threshold=90.0, next_check_at=now + 80, last_check_at=now - 30,
            last_outcome="no switch: cooldown",
        )
        wide = status_text(status, now=now, width=160, feed="live").plain
        assert "checked" in wide and "no switch" in wide
        narrow = status_text(status, now=now, width=60, feed="live").plain
        assert "switch at 90%" in narrow and "feed live" in narrow
        assert "checked" not in narrow and "no switch" not in narrow
        assert len(narrow) <= 60

    def test_cooldown_counts_down_then_disappears(self):
        now = time.time()
        status = self._status(running=True, cooldown_s=300, last_switch_at=now - 60)
        assert "cooldown 4m" in self._text(status, now)
        assert "cooldown" not in self._text(status, now + 400)

    def test_last_switch_right_aligned(self):
        now = time.time()
        status = self._status(running=True, last_switch_at=now, last_switch="2 → 1")
        text = self._text(status, now, width=100)
        assert text.rstrip().endswith("switched 2 → 1")
        assert len(text) == 100

    def test_outcome_shown_without_switch(self):
        text = self._text(self._status(running=True, last_outcome="no switch: cooldown"))
        assert "no switch: cooldown" in text


class TestEtaCell:
    def _est(self, conf, rate=10.0, last=50.0, resets_in=5 * 3600):
        from claude_rotater.estimator import AccountEstimate, WindowEstimate

        now = time.time()
        win = WindowEstimate("5h", last, now, now + resets_in, rate, conf, ())
        return AccountEstimate({"5h": win}, win, None, None, conf), now

    @pytest.mark.parametrize("conf,glyph", [("high", ""), ("medium", "~"), ("low", "?")])
    def test_confidence_glyph(self, conf, glyph):
        from claude_rotater.tui.main import eta_cell

        est, now = self._est(conf)
        text, _role = eta_cell(est, 90.0, now)
        assert text.startswith("4h") or text.startswith("3h")
        assert text.endswith(glyph) if glyph else text[-1].isalnum()

    def test_none_confidence_is_dash(self):
        from claude_rotater.tui.main import eta_cell

        est, now = self._est("none", rate=None)
        assert eta_cell(est, 90.0, now) == ("—", "muted")

    def test_over_threshold_is_now(self):
        from claude_rotater.tui.main import eta_cell

        est, now = self._est("high", last=95.0)
        assert eta_cell(est, 90.0, now)[0] == "now"

    def test_crossing_after_reset(self):
        from claude_rotater.tui.main import eta_cell

        est, now = self._est("high", rate=1.0, resets_in=600)
        assert eta_cell(est, 90.0, now)[0] == "≥reset"

    def test_burn_cell(self):
        from claude_rotater.tui.main import burn_cell

        est, _now = self._est("high", rate=0.01)
        assert burn_cell(est) == "0"
        assert burn_cell(None) == "—"
        est, _now = self._est("high", rate=8.0)
        assert burn_cell(est) == "+8.0%/h"


class TestRunAction:
    def test_captures_output_and_payload(self):
        def fn():
            print("hello")
            return {"switched": True}

        result = tui_data.run_action(fn)
        assert result.ok and result.payload == {"switched": True}
        assert "hello" in result.output

    def test_switch_error_is_captured_not_raised(self):
        from claude_rotater.exceptions import ClaudeSwitchError

        def fn():
            raise ClaudeSwitchError("boom")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "boom" in result.output

    def test_unexpected_input_becomes_eoferror(self):
        def fn():
            input("should not block")

        result = tui_data.run_action(fn)
        assert not result.ok
        assert "interactive input" in result.output

    def test_first_line_strips_ansi(self):
        def fn():
            print("\x1b[1mBold headline\x1b[0m")

        assert tui_data.run_action(fn).first_line == "Bold headline"



# ---------------------------------------------------------------------------
# Pilot tests (async)
# ---------------------------------------------------------------------------


def fake_calls(app) -> list[tuple]:
    return app.switcher.calls


def with_history(fake: FakeSwitcher, series: dict | None = None) -> FakeHistory:
    fake.usage_history = FakeHistory(series or {})
    return fake.usage_history


def burn_history(now: float, *, start: float = 38.0, rate: float = 12.0) -> dict:
    """Account 1 burning ``rate`` %/h on 5h for two hours (ends at start+2*rate)."""
    return {
        "1": {
            "5h": linear_points(
                now=now, start_pct=start, rate_pct_per_h=rate,
                span_s=7200, step_s=300, resets_at=now + 3 * 3600,
            ),
            "7d": linear_points(
                now=now, start_pct=20.0, rate_pct_per_h=0.4,
                span_s=86400, step_s=3600, resets_at=now + 3 * 86400,
            ),
        }
    }


def three_accounts(tmp_path) -> FakeSwitcher:
    return FakeSwitcher(
        [
            make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com"),
            make_account(2, entry=make_entry(12.0, 44.0), email="work@y.com"),
            make_account(3, disabled=True, entry=UsageEntry(), email="old@z.com"),
        ],
        tmp_path,
    )


def row_plain(app, number: str) -> str:
    from claude_rotater.tui.main import AccountRow

    row = next(r for r in app.screen.query(AccountRow) if r.number == number)
    return row.children[0].render().plain


def painted(app, widget_id: str) -> str:
    return app.screen.query_one(widget_id).render().plain


async def emit(engine, event) -> None:
    """Deliver an engine event the way the engine does: from its own thread."""
    await asyncio.to_thread(engine.emit, event)


def log_plain(app) -> str:
    from textual.widgets import RichLog

    log = app.screen.query_one("#event-log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


@pytest.mark.asyncio
class TestMainPage:
    async def test_renders_every_account_on_one_line(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            header = painted(app, "#columns")
            assert "account" in header and "5h" in header and "7d" in header
            assert "ETA→90%" in header
            first = row_plain(app, "1")
            assert first.startswith("▌") and "me@x.com" in first and "62%" in first
            assert "\n" not in first
            assert "personal" not in first  # a personal account needs no tag
            assert "work@y.com" in row_plain(app, "2")
            assert "disabled" in row_plain(app, "3")

    async def test_active_account_is_highlighted_at_start(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1), make_account(2, active=True)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.screen.selected_number == "2"

    async def test_status_line_and_action_bar(self, tmp_path):
        from textual.widgets import Button

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            status = painted(app, "#status")
            assert "rota" in status and "auto on" in status
            assert "switch at 90%" in status
            assert "◉●◌" in status  # roster: on duty, enabled, disabled
            labels = [str(b.label) for b in app.screen.query(Button)]
            for word in ("switch", "add", "token", "disable", "remove", "history", "statusline", "config", "quit"):
                assert any(word in label for label in labels)
            # chips never steal the keyboard from the list
            assert all(not b.can_focus for b in app.screen.query(Button))

    async def test_empty_account_list(self, tmp_path):
        app = make_app(FakeSwitcher([], tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "No managed accounts yet" in painted(app, "#columns")
            assert painted(app, "#detail").strip() == ""
            await pilot.press("enter", "d", "x")  # nothing to act on: no crash
            await settle(pilot)
            assert fake_calls(app) == []

    @pytest.mark.parametrize("size", [(120, 40), (80, 24)])
    async def test_page_is_as_tall_as_its_content(self, tmp_path, size):
        """No plot pads the page: the key strip follows the detail strip, and
        an empty event log takes no room."""
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            log = app.screen.query_one("#event-log")
            assert not log.display  # only a quiet no-switch event so far
            detail = app.screen.query_one("#detail")
            actions = app.screen.query_one("#actions")
            lines = painted(app, "#detail").splitlines()
            assert 2 <= len(lines) <= 4 and detail.size.height == len(lines)
            assert actions.region.y == detail.region.bottom + 1
            assert actions.region.bottom < size[1]

    @pytest.mark.parametrize("size", [(80, 24), (120, 40)])
    async def test_four_accounts_fit_without_scrolling(self, tmp_path, size):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(62.0, 31.0, scoped=[("Fable", 34.0)])),
                make_account(2, entry=make_entry(12.0, 44.0)),
                make_account(3, entry=make_entry(91.0, 85.0, scoped=[("Fable", 0.0)])),
                make_account(4, disabled=True, entry=UsageEntry()),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            listview = app.screen.query_one("#accounts")
            assert listview.max_scroll_y == 0 and len(listview.children) == 4
            assert app.screen.max_scroll_y == 0
            assert app.screen.query_one("#actions").region.bottom <= size[1]
            assert "Fable" in painted(app, "#columns")

    async def test_event_log_appears_with_first_event(self, tmp_path, fake_engine):
        from claude_rotater.autoswitch import ErrorEvent

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            engine = fake_engine.instances[0]
            await emit(engine, ErrorEvent(message="boom"))
            await settle(pilot)
            assert app.screen.query_one("#event-log").display
            assert "boom" in log_plain(app)

    @pytest.mark.parametrize("size", [(80, 24), (60, 20)])
    async def test_narrow_terminal(self, tmp_path, size):
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            first = row_plain(app, "1")
            assert "me@x" in first and "62%" in first
            assert not has_bar(first)  # meters dropped before names
            assert "1h59m" in first or "2h00m" in first  # resets kept
            # every action chip stays on screen
            from textual.widgets import Button

            assert all(b.region.right <= size[0] for b in app.screen.query(Button))
            # the action bar and the plot pane still exist
            assert app.screen.query_one("#actions") is not None
            assert painted(app, "#detail")

    async def test_no_history_account_shows_strip_and_dash_eta(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "—" in row_plain(app, "1")
            detail = painted(app, "#detail")
            # measured pcts still get a line each, flagged as having no trend
            assert detail.count("no trend yet") == 2
            assert "62%" in detail and "31%" in detail
            assert "resets" in detail and "token ok" in detail
            assert not any(0x2800 <= ord(ch) <= 0x28FF for ch in detail)  # no plots

    async def test_synthetic_history_gives_eta_and_strip(self, tmp_path):
        import re

        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            first = row_plain(app, "1")
            # 62% at 12%/h → 90% in ~2h20m, before the 3h reset
            assert re.search(r"2h1\dm|2h20m", first), first
            assert "+12.0%/h" in first
            detail = painted(app, "#detail")
            assert "me@x.com" in detail and "on duty" in detail
            assert re.search(r"5h +62%.*\+12\.0%/h +90% in 2h", detail), detail
            assert re.search(r"7d +31%.*\+0\.4%/h", detail), detail
            assert "at reset" in detail
            # account 2 has no history: its ETA is a dash
            assert "—" in row_plain(app, "2")

    async def test_highlight_moves_detail(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "me@x.com" in painted(app, "#detail")
            await pilot.press("down")
            await pilot.pause()
            assert "work@y.com" in painted(app, "#detail")
            await pilot.press("k")
            await pilot.pause()
            assert "me@x.com" in painted(app, "#detail")

    async def test_history_reread_only_when_stamp_advances(self, tmp_path):
        fake = three_accounts(tmp_path)
        history = with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app._tick()
            await settle(pilot)
            app._tick()
            await settle(pilot)
            assert history.reads == 1
            fake._accounts[0] = dataclasses.replace(
                fake._accounts[0], usage=make_entry(63.0, 31.0, age_s=1.0)
            )
            app._tick()
            await settle(pilot)
            assert history.reads == 2

    async def test_history_failure_degrades_to_no_estimates(self, tmp_path):
        fake = three_accounts(tmp_path)
        history = with_history(fake, burn_history(time.time()))
        history.fail = True
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.estimates == {}
            assert "—" in row_plain(app, "1")

    async def test_poller_reads_store_only(self, tmp_path):
        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app._tick()
            await settle(pilot)
            assert fake.fetch_sets and all(f == set() for f in fake.fetch_sets)

    async def test_f_does_one_normal_take(self, tmp_path):
        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert fake.fetch_sets.count(None) == 1

    async def test_enter_switches_highlighted_and_wakes_engine(self, tmp_path, fake_engine):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake_calls(app)
            assert fake_engine.instances[-1].wakes == 1
            await settle(pilot)
            assert row_plain(app, "2").startswith("▌")

    async def test_switch_button(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#act-switch")
            await settle(pilot)
            assert ("switch_to", "1") in fake_calls(app)

    async def test_d_toggles_disabled(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("d")
            await settle(pilot)
            assert ("set_disabled", "1", True) in fake_calls(app)
            assert "disabled" in row_plain(app, "1")
            await pilot.press("d")
            await settle(pilot)
            assert ("set_disabled", "1", False) in fake_calls(app)

    async def test_x_confirms_then_removes(self, tmp_path):
        from claude_rotater.tui.modals import ConfirmModal

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await settle(pilot)
            assert not any(c[0] == "remove" for c in fake_calls(app))
            await pilot.press("delete")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in fake_calls(app)
            await settle(pilot)
            from claude_rotater.tui.main import AccountRow

            assert [r.number for r in app.screen.query(AccountRow)] == ["1", "3"]

    async def test_a_adds_current_login_after_confirm(self, tmp_path):
        from claude_rotater.tui.modals import ConfirmModal, OutputModal

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert ("add", None, False) in fake_calls(app)
            assert isinstance(app.screen, OutputModal)

    async def test_t_adds_token(self, tmp_path):
        from textual.widgets import Input

        from claude_rotater.tui.modals import AddTokenModal

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("t")
            await pilot.pause()
            assert isinstance(app.screen, AddTokenModal)
            app.screen.query_one("#token", Input).value = "sk-ant-oat-x"
            app.screen.query_one("#slot", Input).value = "7"
            await pilot.click("#add")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat-x", None, 7, True) in fake_calls(app)

    async def test_c_opens_config_and_back(self, tmp_path):
        from claude_rotater.tui.config import ConfigScreen
        from claude_rotater.tui.main import MainScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            assert app.screen.backup_dir == tmp_path
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)


class FakeStage:
    """Stands in for login.StagedLogin; records the TUI's calls."""

    instances: list["FakeStage"] = []

    def __init__(self, backup_dir, *, fail=None):
        self.backup_dir = backup_dir
        self.fail = fail
        self.events: list = []
        FakeStage.instances.append(self)

    def authenticate(self, *, method="claudeai", email=None):
        self.events.append(("authenticate", email))
        if self.fail is not None:
            raise self.fail
        return {"loggedIn": True}

    def import_into(self, switcher, **_kw):
        self.events.append(("import",))
        switcher.calls.append(("login_import",))
        print("Added Account 4: new@example.com")

    def cleanup(self):
        self.events.append(("cleanup",))


def login_app(fake, monkeypatch, *, fail=None, suspend_ok=True):
    import contextlib

    from claude_rotater.tui.app import RotaApp
    from textual.app import SuspendNotSupported

    FakeStage.instances = []
    app = RotaApp(fake, login_factory=lambda d: FakeStage(d, fail=fail))
    suspended: list[bool] = []

    @contextlib.contextmanager
    def suspend():
        if not suspend_ok:
            raise SuspendNotSupported("headless")
        suspended.append(True)
        yield

    monkeypatch.setattr(app, "suspend", suspend)
    return app, suspended


@pytest.mark.asyncio
class TestLoginAction:
    async def test_l_logs_in_suspended_then_imports_and_cleans(self, tmp_path, monkeypatch):
        from claude_rotater.tui.modals import OutputModal

        app, suspended = login_app(three_accounts(tmp_path), monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            stage, = FakeStage.instances
            assert suspended == [True]
            assert stage.backup_dir == tmp_path
            # a healthy row: no address pre-filled
            assert stage.events == [("authenticate", None), ("import",), ("cleanup",)]
            assert ("login_import",) in fake_calls(app)
            assert isinstance(app.screen, OutputModal)

    async def test_l_on_dead_row_prefills_its_email(self, tmp_path, monkeypatch):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(10.0, 10.0), email="me@x.com"),
                make_account(2, entry=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), email="dead@y.com"),
            ],
            tmp_path,
        )
        app, _ = login_app(fake, monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "press l" in row_plain(app, "2")
            await pilot.press("down", "l")
            await settle(pilot)
            assert FakeStage.instances[0].events[0] == ("authenticate", "dead@y.com")

    async def test_login_button(self, tmp_path, monkeypatch):
        app, _ = login_app(three_accounts(tmp_path), monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#act-login")
            await settle(pilot)
            assert FakeStage.instances and ("login_import",) in fake_calls(app)

    @pytest.mark.parametrize("cancelled", [True, False], ids=["cancel", "failure"])
    async def test_cancel_or_failure_cleans_up_and_imports_nothing(self, tmp_path, monkeypatch, cancelled):
        from claude_rotater.exceptions import LoginCancelled, LoginError
        from claude_rotater.tui.main import MainScreen

        exc = LoginCancelled("Login cancelled") if cancelled else LoginError("no login")
        app, _ = login_app(three_accounts(tmp_path), monkeypatch, fail=exc)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            assert FakeStage.instances[0].events == [("authenticate", None), ("cleanup",)]
            assert ("login_import",) not in fake_calls(app)
            assert isinstance(app.screen, MainScreen)
            assert not app.busy

    async def test_no_terminal_cleans_up(self, tmp_path, monkeypatch):
        app, _ = login_app(three_accounts(tmp_path), monkeypatch, suspend_ok=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            assert FakeStage.instances[0].events == [("cleanup",)]

    async def test_import_failure_still_cleans_up(self, tmp_path, monkeypatch):
        from claude_rotater.exceptions import ValidationError
        from claude_rotater.tui.modals import OutputModal

        app, _ = login_app(three_accounts(tmp_path), monkeypatch)

        def boom(self, switcher, **_kw):
            self.events.append(("import",))
            raise ValidationError("is the active account")

        monkeypatch.setattr(FakeStage, "import_into", boom)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
            assert FakeStage.instances[0].events[-1] == ("cleanup",)
            assert isinstance(app.screen, OutputModal)
            assert "failed" in str(app.screen.query_one(".modal-title").render())

    async def test_busy_app_does_not_start_a_login(self, tmp_path, monkeypatch):
        app, _ = login_app(three_accounts(tmp_path), monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.busy = True
            await pilot.press("l")
            await pilot.pause()
            assert FakeStage.instances == []


class TestLoginWarning:
    def _acc(self, expires_in, **kw):
        return dataclasses.replace(
            make_account(2, **kw), login_expires_at=None if expires_in is None else time.time() + expires_in
        )

    @pytest.mark.parametrize(
        "expires_in, days, expected",
        [
            (None, 3, None),
            (10 * 86400, 3, None),
            (2.5 * 86400, 3, "login 2d"),
            (5 * 3600 + 330, 3, "login 5h 5m"),
            (-10, 3, "login ended"),
            (86400, 0, None),  # warnings off
        ],
    )
    def test_tag(self, expires_in, days, expected):
        from claude_rotater.tui.main import login_warning

        assert login_warning(self._acc(expires_in), time.time(), days) == expected

    def test_row_shows_warning_in_tag_column(self):
        from claude_rotater.tui.main import RowLayout, account_row_text

        acc = self._acc(86400 + 60)
        layout = RowLayout.for_width(120, ("5h", "7d"), tag_need=8)
        text = account_row_text(acc, None, layout, now=time.time(), threshold=90)
        assert "login 1d" in text.plain and "personal" not in text.plain

    def test_row_without_warning_has_no_personal_tag(self):
        from claude_rotater.tui.main import RowLayout, account_row_text

        layout = RowLayout.for_width(120, ("5h", "7d"), tag_need=8)
        text = account_row_text(self._acc(30 * 86400), None, layout, now=time.time(), threshold=90)
        assert "personal" not in text.plain and "login" not in text.plain

    def test_strip_shows_login_end_and_dead_hint(self):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED
        from claude_rotater.tui.main import strip_lines

        def login_text(acc, width):
            lines = strip_lines(acc, None, {}, now=time.time(), threshold=90, width=width)
            return next(line.plain for line in lines if "login" in line.plain)

        far = login_text(self._acc(30 * 86400), 100)
        assert "login ends" in far and "press l" not in far
        assert "press l" in login_text(self._acc(3600), 200)
        dead = dataclasses.replace(self._acc(None), usage=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED))
        text = login_text(dead, 200)
        assert "re-login needed: press l" in text and dead.email in text


@pytest.mark.asyncio
class TestRefresherHosting:
    async def test_started_at_mount_stopped_at_unmount(self, tmp_path, fake_refresher):
        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            refresher, = fake_refresher.instances
            assert refresher.switcher is fake and not refresher.stopped
        assert refresher.stopped

    async def test_events_reach_the_log_and_refresh(self, tmp_path, fake_refresher):
        import asyncio

        from claude_rotater.token_refresh import RefreshEvent

        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            refresher, = fake_refresher.instances
            before = len(fake.fetch_sets)
            await asyncio.to_thread(refresher.on_event, RefreshEvent("dead", "2", "work@y.com", "invalid_grant"))
            await asyncio.to_thread(refresher.on_event, RefreshEvent("held", "3", "old@z.com"))
            await settle(pilot)
            log = log_plain(app)
            assert "account 2 (work@y.com) needs a re-login" in log
            assert "old@z.com" not in log  # held is routine
            assert len(fake.fetch_sets) > before

    async def test_login_warn_days_setting_applies_live(self, tmp_path):
        from claude_rotater.settings import set_setting

        fake = FakeSwitcher(
            [
                make_account(1, active=True, email="me@x.com"),
                dataclasses.replace(make_account(2, email="w@y.com"), login_expires_at=time.time() + 5 * 86400),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "login 5d" not in row_plain(app, "2")
            set_setting(tmp_path, "ui.loginWarnDays", "7")
            app.on_settings_changed("ui.loginWarnDays")
            await pilot.pause()
            assert app.login_warn_days == 7
            assert "login 4d" in row_plain(app, "2") or "login 5d" in row_plain(app, "2")


def test_default_factory_builds_live_engine(monkeypatch, tmp_path):
    # Drop the autouse FakeEngine patch: this test checks the real factory.
    monkeypatch.undo()
    import claude_rotater.autoswitch as autoswitch
    from claude_rotater.tui.app import default_engine_factory
    from claude_rotater.settings import AutoSwitchSettings

    built = {}

    def recorder(switcher, settings, on_event, **kwargs):
        built.update(kwargs=kwargs, settings=settings)
        return object()

    monkeypatch.setattr(autoswitch, "AutoSwitchEngine", recorder)
    switcher = type("S", (), {"backup_dir": tmp_path})()
    default_engine_factory(switcher, AutoSwitchSettings(), lambda e: None)
    assert built["kwargs"].get("dry_run", False) is False
    # The TUI engine ingests the statusline feed of this profile.
    assert built["kwargs"]["feed"].backup_dir == tmp_path


@pytest.mark.asyncio
class TestEngineHosting:
    async def test_factory_called_once_live(self, tmp_path, fake_engine):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert len(fake_engine.instances) == 1
            engine = fake_engine.instances[0]
            assert engine.kwargs == {}  # no dry_run, ever
            assert engine.settings.threshold == 90.0
            assert engine.switcher is app.switcher

    async def test_switch_event_logs_updates_status_and_refreshes(self, tmp_path, fake_engine):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            before = len(app.switcher.fetch_sets)
            await emit(
                fake_engine.instances[0],
                SwitchEvent(
                    trigger="proactive",
                    from_ref={"number": 2, "email": "work@y.com"},
                    to_ref={"number": 1, "email": "me@x.com"},
                )
            )
            await settle(pilot)
            assert "Switched Account-2 -> Account-1" in log_plain(app)
            assert "switched 2 → 1" in painted(app, "#status")
            assert len(app.switcher.fetch_sets) > before

    async def test_quiet_events_feed_status_not_log(self, tmp_path, fake_engine):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            engine = fake_engine.instances[0]
            await emit(engine, PollEvent(active={"number": 1, "email": "me@x.com"}, headroom={"1": 38.0}, threshold=90.0))
            await emit(engine, SleepEvent(seconds=125.0, until="later"))
            await settle(pilot)
            status = painted(app, "#status")
            assert "checked 0s ago" in status or "checked 1s ago" in status
            assert "next 2m" in status
            # the startup NoSwitchEvent is summarized on the status line only
            assert "no switch: cooldown" in status
            assert "no switch" not in log_plain(app)
            assert "sleeping" not in log_plain(app)

    async def test_cooldown_from_shared_state_file(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text(
            json.dumps({"lastSwitchAt": time.time() - 30})
        )
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            status = painted(app, "#status")
            assert "cooldown 4m" in status
            assert "last switch" in status

    async def test_imported_claude_swap_switch_is_not_rotas(self, tmp_path):
        """lastSwitchAt copied over by the claude-swap import is not shown as
        rota's last switch; a switch after the import is."""
        imported = time.time() - 3600
        (tmp_path / ".imported-from.json").write_text(
            json.dumps({"importedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(imported))})
        )
        state = tmp_path / "autoswitch_state.json"
        state.write_text(json.dumps({"lastSwitchAt": imported - 9 * 86400}))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "last switch" not in painted(app, "#status")
            assert app.auto_status.last_switch_at is None
            state.write_text(json.dumps({"lastSwitchAt": time.time() - 30}))
            app._tick()
            await settle(pilot)
            assert "last switch" in painted(app, "#status")

    async def test_settings_change_restarts_engine(self, tmp_path, fake_engine):
        from claude_rotater.settings import set_setting

        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            first = fake_engine.instances[0]
            set_setting(tmp_path, "autoswitch.threshold", "80")
            app.on_settings_changed("autoswitch.threshold")
            await settle(pilot)
            assert first.stopped
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[1].settings.threshold == 80.0
            assert app.threshold_pct == 80.0
            assert fake._poll_inputs_override == (80.0, ())
            assert "switch at 80%" in painted(app, "#status")
            assert "ETA→80%" in painted(app, "#columns")

    async def test_threshold_edited_on_config_page_restarts_engine(
        self, tmp_path, fake_engine
    ):
        """End to end: `c`, type a threshold, enter; the real ConfigScreen's
        SettingChanged message reaches the app and the engine restarts."""
        from claude_rotater.tui.config import ConfigScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            assert screen.settings_list.spec.dotted == "autoswitch.threshold"
            await pilot.press("8", "1", "enter")
            await pilot.pause(ConfigScreen.NOTIFY_DELAY_S + 0.2)
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped
            assert fake_engine.instances[1].settings.threshold == 81.0
            assert app.threshold_pct == 81.0

    async def test_holding_right_on_config_restarts_engine_once(self, tmp_path, fake_engine):
        """Every step is written, but the engine restarts once the keys stop."""
        from claude_rotater.settings import load_settings
        from claude_rotater.tui.config import ConfigScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            await pilot.press(*["right"] * 5)
            assert load_settings(tmp_path).threshold == 95.0
            await pilot.pause(ConfigScreen.NOTIFY_DELAY_S + 0.2)
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[1].settings.threshold == 95.0

    async def test_leaving_config_flushes_pending_change(self, tmp_path, fake_engine):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("left", "escape")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert app.threshold_pct == 89.0

    @pytest.mark.parametrize("key", [
        "autoswitch.pollSpareSeconds",
        "autoswitch.pollHourlyBudget",
        "autoswitch.feedStaleSeconds",
    ])
    async def test_cadence_change_restarts_engine(self, tmp_path, fake_engine, key):
        """A cadence edit restarts the engine so its next sleep is planned
        with the new value instead of waiting out the old one."""
        from claude_rotater.poll_policy import load_cadence
        from claude_rotater.settings import set_setting

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            set_setting(tmp_path, key, "90")
            app.on_settings_changed(key)
            await settle(pilot)
            assert fake_engine.instances[0].stopped
            assert len(fake_engine.instances) == 2
            cadence = load_cadence(tmp_path)
            assert 90 in (cadence.spare_s, cadence.hourly_budget, cadence.feed_stale_s)

    async def test_config_message_routes_to_settings_change(self, tmp_path, fake_engine):
        from claude_rotater.settings import set_setting

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            set_setting(tmp_path, "autoswitch.cooldownSeconds", "60")

            class Msg:
                key = "autoswitch.cooldownSeconds"

            app.on_config_screen_setting_changed(Msg())
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[1].settings.cooldown_seconds == 60.0

    async def test_theme_setting_applies_without_engine_restart(self, tmp_path, fake_engine):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
            app.on_settings_changed("ui.theme")
            await pilot.pause()
            assert app.theme == "rota-dark"
            assert len(fake_engine.instances) == 1

    async def test_unmount_stops_engine_and_clears_poll_inputs(self, tmp_path, fake_engine):
        fake = three_accounts(tmp_path)
        fake._poll_inputs_override = (90.0, ())
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
        assert fake_engine.instances[0].stopped
        assert fake._poll_inputs_override is None

    async def test_engine_crash_shows_stopped(self, tmp_path, fake_engine):
        fake_engine.crash = True
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.pause(0.1)
            assert "auto off" in painted(app, "#status")


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_rotater.tui.main import event_text

        assert event.human() in event_text(event).plain

    def test_event_text_uses_light_accent_for_switch(self):
        from claude_rotater.tui.main import event_text
        from claude_rotater.tui.theme import ACCENT_LIGHT, ROTA_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(ROTA_LIGHT))
        assert any(ACCENT_LIGHT in str(s.style) for s in text.spans)


# ---------------------------------------------------------------------------
# accounts_snapshot on the real switcher
# ---------------------------------------------------------------------------


class TestAccountsSnapshot:
    def test_one_pass_snapshot(self, temp_home, mock_claude_config):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["sequence"] = [1, 2]
        data["accounts"] = {
            "1": {"email": "test@example.com", "uuid": "test-uuid-1234"},
            "2": {"email": "other@example.com", "uuid": "uuid-2"},
        }
        switcher._write_json(switcher.sequence_file, data)

        snap = switcher.accounts_snapshot(fetch=set())  # store-only: no network
        assert snap.active_number == "1"
        assert [acc.number for acc in snap.accounts] == ["1", "2"]
        active = snap.accounts[0]
        assert active.is_active and active.email == "test@example.com"
        assert all(acc.kind == "oauth" for acc in snap.accounts)
        # No stored credential backups: nothing is switchable, and usage is
        # sentinel'd rather than fetched.
        assert all(not acc.switchable for acc in snap.accounts)
        assert all(acc.usage.sentinel is not None for acc in snap.accounts)
        assert isinstance(snap.taken_at, float)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestBareInvocation:
    def test_bare_tty_launches_tui(self, monkeypatch, temp_home):
        import claude_rotater.cli as cli
        import claude_rotater.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["rota"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home):
        import claude_rotater.cli as cli

        monkeypatch.setattr(sys, "argv", ["rota"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2  # argparse usage error


# ---------------------------------------------------------------------------
# Theme wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestThemeWiring:
    async def test_mount_selects_light_theme_from_settings(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "rota-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_rotater.tui.app import RotaApp
        app = RotaApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "rota-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_rotater.tui.app import RotaApp
        app = RotaApp(fake, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "rota-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_rotater.tui.app import RotaApp
        app = RotaApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "rota-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "rota-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "rota-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "rota-dark"           # → back to dark

    async def test_ctrl_t_cycles_theme(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.theme == "rota-light"


# ---------------------------------------------------------------------------
# Redesign: long-term view, statusline key, widths, repaint discipline
# ---------------------------------------------------------------------------


def long_history(now: float) -> dict:
    """burn_history plus past 5h windows and a previous 7d window."""
    series = burn_history(now)
    past = []
    for k, peak in enumerate((40.0, 95.0, 70.0)):
        reset = now - (k + 1) * 86400
        past += [Point(reset - 4 * 3600, peak / 2, reset), Point(reset - 3600, peak, reset)]
    prev7 = now - 4 * 86400
    series["1"]["5h"] = past + series["1"]["5h"]
    series["1"]["7d"] = [
        Point(prev7 - 6 * 86400 + h * 3600, h * 0.5, prev7) for h in range(0, 144, 6)
    ] + series["1"]["7d"]
    return series


class TestLongTermLines:
    def _acc(self):
        return make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com")

    def test_draws_peaks_and_curve_with_titles(self):
        from claude_rotater.tui.main import LongTerm, long_term_lines

        now = time.time()
        lt = LongTerm(
            peaks_5h=((now - 86400, 95.0), (now - 2 * 86400, 40.0)),
            series_7d=tuple(Point(now - 9 * 86400 + h * 3600, h / 3, None) for h in range(200)),
        )
        lines = long_term_lines(self._acc(), lt, now=now, threshold=90, width=120, height=20)
        text = "\n".join(line.plain for line in lines)
        assert "5h peaks  last 7 days · 2 windows · 1 past 90% · 2/bar" in text
        assert "7d usage  since" in text and "peak 66%" in text
        assert "█" in text  # peak columns
        assert any(0x2800 <= ord(ch) <= 0x28FF for ch in text)  # 7d braille curve
        assert len(lines) <= 20 and all(line.cell_len <= 120 for line in lines)

    def test_grouping_named_only_when_bars_hold_several_windows(self):
        from claude_rotater.tui.main import LongTerm, long_term_lines

        now = time.time()
        lt = LongTerm(peaks_5h=((now - 86400, 50.0),))
        for width, note in ((80, False), (120, True), (160, False)):
            text = "\n".join(
                l.plain for l in long_term_lines(self._acc(), lt, now=now, threshold=90, width=width, height=20)
            )
            assert ("/bar" in text) == note, width

    def test_side_by_side_when_wide_stacked_when_narrow(self):
        from claude_rotater.tui.main import LongTerm, long_term_lines

        now = time.time()
        lt = LongTerm(peaks_5h=((now - 86400, 50.0),), series_7d=(Point(now - 3600, 20.0, None),))
        wide = [l.plain for l in long_term_lines(self._acc(), lt, now=now, threshold=90, width=120, height=20)]
        assert any("5h peaks" in l and "7d usage" in l for l in wide)
        narrow = [l.plain for l in long_term_lines(self._acc(), lt, now=now, threshold=90, width=80, height=20)]
        assert not any("5h peaks" in l and "7d usage" in l for l in narrow)
        assert any("5h peaks" in l for l in narrow) and any("7d usage" in l for l in narrow)

    def test_loading_and_empty_states(self):
        from claude_rotater.tui.main import LongTerm, long_term_lines

        now = time.time()
        loading = long_term_lines(self._acc(), None, now=now, threshold=90, width=80, height=10)
        assert "loading history" in loading[-1].plain
        empty = long_term_lines(self._acc(), LongTerm(), now=now, threshold=90, width=80, height=10)
        assert "no past windows recorded yet" in empty[-1].plain
        assert long_term_lines(None, None, now=now, threshold=90, width=80, height=10) == []


class TestStripLines:
    """The detail strip: facts, then one aligned line per usage window."""

    def _lines(self, width=120, *, entry=None, series=None, max_lines=4, feed=None, acc=None):
        from claude_rotater.estimator import estimate_account
        from claude_rotater.tui.main import strip_lines

        now = time.time()
        series = burn_history(now)["1"] if series is None else series
        est = estimate_account(series, models=(), threshold=90, now=now)
        acc = acc or make_account(1, active=True, entry=entry or make_entry(62.0, 31.0), email="me@x.com")
        return [
            line.plain
            for line in strip_lines(acc, est, series, now=now, threshold=90, width=width,
                                    max_lines=max_lines, feed=feed)
        ]

    @pytest.mark.parametrize("width", [80, 120, 160])
    def test_fits_and_keeps_the_essentials(self, width):
        lines = self._lines(width)
        assert 3 <= len(lines) <= 4
        assert all(len(line) <= width for line in lines)
        assert lines[0].startswith("me@x.com") and "on duty" in lines[0]
        five = next(line for line in lines if line.startswith("5h"))
        assert "62%" in five and "+12.0%/h" in five and "resets " in five

    def test_wide_line_carries_projection_at_reset_and_clock(self):
        five = next(line for line in self._lines(160) if line.startswith("5h"))
        assert "at reset" in five and "·" in five  # "resets 1h59m · 20:23"
        narrow = next(line for line in self._lines(80) if line.startswith("5h"))
        assert "at reset" not in narrow and "resets " in narrow

    def test_model_windows_get_their_own_lines(self):
        entry = make_entry(62.0, 31.0, scoped=[("Fable", 34.0)])
        lines = self._lines(120, entry=entry)
        fable = next(line for line in lines if line.startswith("Fable"))
        assert "34%" in fable and "resets" in fable
        # labels share one column width, so the pcts line up
        five = next(line for line in lines if line.startswith("5h"))
        assert five.index("62%") == fable.index("34%")

    def test_model_window_burn_from_its_own_history(self):
        now = time.time()
        series = burn_history(now)["1"]
        series["Fable"] = linear_points(
            now=now, start_pct=10.0, rate_pct_per_h=1.0, span_s=86400, step_s=3600,
            resets_at=now + 2 * 86400,
        )
        entry = make_entry(62.0, 31.0, scoped=[("Fable", 34.0)])
        fable = next(line for line in self._lines(120, entry=entry, series=series) if line.startswith("Fable"))
        assert "+1.0%/h" in fable

    def test_no_model_windows_no_model_lines(self):
        lines = self._lines(120)
        assert [line.split()[0] for line in lines[1:]] == ["5h", "7d"]

    def test_extra_windows_fold_into_the_last_line(self):
        entry = make_entry(62.0, 31.0, scoped=[("Fable", 34.0), ("Opus", 12.0)])
        lines = self._lines(120, entry=entry)
        assert len(lines) == 4
        assert lines[-1].startswith("Fable 34%") and "Opus 12%" in lines[-1] and "↻" in lines[-1]

    def test_facts_drop_from_the_end_when_narrow(self):
        acc = dataclasses.replace(
            make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com"),
            login_expires_at=time.time() + 20 * 86400,
        )
        wide = self._lines(160, acc=acc, feed="live")[0]
        assert "login ends" in wide and "token ok" in wide and "measured" in wide and "feed live" in wide
        narrow = self._lines(60, acc=acc, feed="live")[0]
        assert "login ends" in narrow and "feed live" not in narrow and len(narrow) <= 60

    def test_fractional_threshold_is_never_cut(self):
        from claude_rotater.estimator import estimate_account
        from claude_rotater.tui.main import strip_lines

        now = time.time()
        series = burn_history(now, rate=0.5)["1"]
        est = estimate_account(series, models=(), threshold=99.9, now=now)
        acc = make_account(1, active=True, entry=make_entry(62.0, 31.0))
        lines = [line.plain for line in strip_lines(acc, est, series, now=now, threshold=99.9, width=80)]
        assert any("stays under 99.9%" in line for line in lines), lines

    def test_feed_not_set_up_shows_on_the_active_account(self):
        assert "feed: not set up" in self._lines(160, feed="unset")[0]

    def test_poll_error_replaces_token_ok(self):
        entry = dataclasses.replace(make_entry(62.0, 31.0), last_error="http-429")
        title = self._lines(160, entry=entry)[0]
        assert "last poll failed (http-429)" in title and "token ok" not in title

    def test_line_budget_is_respected(self):
        assert len(self._lines(120, max_lines=2)) == 2
        assert self._lines(120, max_lines=0) == []

    def test_unknown_usage_says_so(self):
        lines = self._lines(120, entry=UsageEntry(), series={})
        assert "usage unknown" in lines[1]

    def test_rolled_over_window_awaits_a_poll(self):
        from claude_rotater.tui.main import strip_lines

        acc = make_account(1, entry=make_entry(62.0, 31.0))
        acc.usage.last_good["five_hour"]["resets_at"] = _iso_in(-60)
        lines = [line.plain for line in strip_lines(acc, None, {}, now=time.time(), threshold=90, width=120)]
        assert any("reset, awaiting poll" in line for line in lines)


class TestHistoryLines:
    def _lines(self, width, height):
        from claude_rotater.estimator import estimate_account
        from claude_rotater.tui.main import LongTerm, history_lines

        now = time.time()
        series = burn_history(now)["1"]
        est = estimate_account(series, models=(), threshold=90, now=now)
        acc = make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com")
        lt = LongTerm(
            peaks_5h=((now - 86400, 95.0), (now - 2 * 86400, 40.0)),
            series_7d=tuple(Point(now - 9 * 86400 + h * 3600, h / 3, None) for h in range(200)),
        )
        return [
            line.plain
            for line in history_lines(acc, est, series, lt, now=now, threshold=90, width=width, height=height)
        ]

    def test_tall_pane_shows_current_graphs_and_long_term(self):
        lines = self._lines(120, 31)
        text = "\n".join(lines)
        assert any(line.startswith("5h  62%") and "7d  31%" in line for line in lines)
        assert "5h peaks" in text and "7d usage" in text
        assert len(lines) < 31  # sized to what it shows, not to the pane
        assert all(len(line) <= 120 for line in lines)

    def test_short_pane_keeps_the_long_term_view(self):
        lines = self._lines(80, 15)
        text = "\n".join(lines)
        assert "5h peaks" in text and "7d usage" in text
        assert not any(line.startswith("5h  62%") for line in lines)
        assert len(lines) <= 15

    def test_graph_heights_are_capped(self):
        from claude_rotater.tui.main import CURRENT_PLOT_MAX, LONG_PLOT_MAX

        assert len(self._lines(160, 200)) <= 1 + 2 * 1 + CURRENT_PLOT_MAX + 2 + LONG_PLOT_MAX + 2

    def test_current_graphs_have_threshold_and_projection(self):
        from claude_rotater.tui.plot import RESET_GLYPH, THRESHOLD_GLYPH

        lines = self._lines(120, 31)
        start = next(i for i, line in enumerate(lines) if line.startswith("5h  62%"))
        end = next(i for i, line in enumerate(lines) if line.startswith("5h peaks"))
        body = "\n".join(lines[start:end])
        assert THRESHOLD_GLYPH in body and RESET_GLYPH in body and "now" in body


class TestSnapshotKey:
    def test_ignores_taken_at_and_age(self):
        from claude_rotater.tui.main import snapshot_key

        acc = make_account(1, entry=make_entry(40.0, 10.0))
        a = AccountsSnapshot(active_number="1", accounts=(acc,), taken_at=1.0)
        aged = dataclasses.replace(acc, usage=dataclasses.replace(acc.usage, age_s=999.0))
        b = AccountsSnapshot(active_number="1", accounts=(aged,), taken_at=2.0)
        assert snapshot_key(a) == snapshot_key(b)

    def test_sees_new_measurement_and_state(self):
        from claude_rotater.tui.main import snapshot_key

        acc = make_account(1, entry=make_entry(40.0, 10.0))
        base = snapshot_key(AccountsSnapshot("1", (acc,), 1.0))
        moved = dataclasses.replace(acc, usage=dataclasses.replace(acc.usage, fetched_at=acc.usage.fetched_at + 60))
        assert snapshot_key(AccountsSnapshot("1", (moved,), 1.0)) != base
        assert snapshot_key(AccountsSnapshot("1", (dataclasses.replace(acc, disabled=True),), 1.0)) != base


@pytest.mark.asyncio
class TestLongTermView:
    async def test_h_toggles_history_and_back(self, tmp_path):
        fake = three_accounts(tmp_path)
        history = with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            reads = history.reads
            await pilot.press("h")
            await settle(pilot)
            assert app.long_view
            assert history.reads == reads + 1  # history read once on the way in
            detail = painted(app, "#detail")
            assert "5h peaks" in detail and "7d usage" in detail
            assert "5h  62%" in detail  # the current-window graphs moved here
            assert "4 windows · 1 past 90%" in detail  # 3 past + the running one
            assert app.long_term["1"].peaks_5h
            # the page still fits: the key strip stays on screen
            assert app.screen.query_one("#actions").region.bottom <= 40
            await pilot.press("h")
            await settle(pilot)
            assert not app.long_view
            detail = painted(app, "#detail")
            assert "5h peaks" not in detail and "token ok" in detail

    async def test_history_button(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.click("#act-history")
            await settle(pilot)
            assert app.long_view

    async def test_history_follows_highlight(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            await pilot.press("down")
            await pilot.pause()
            detail = painted(app, "#detail")
            assert "work@y.com" in detail and "no past windows recorded yet" in detail

    async def test_no_history_object_still_toggles(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            assert "no past windows recorded yet" in painted(app, "#detail")


@pytest.mark.asyncio
class TestStatuslineKey:
    """Real ``statusline_setup`` against the isolated test ``$HOME``: the
    settings file it edits is ``$HOME/.claude/settings.json`` of the test."""

    def _status(self, install: str, state: str):
        from claude_rotater import statusline_setup

        return statusline_setup.StatuslineStatus(
            install, state, Path("settings.json"), None, None
        )

    def _calls(self, monkeypatch, status):
        from claude_rotater import statusline_setup

        calls: list[tuple[str, object]] = []
        monkeypatch.setattr(statusline_setup, "status", lambda backup_dir=None, now=None: status)
        monkeypatch.setattr(statusline_setup, "install", lambda d=None: calls.append(("install", d)))
        monkeypatch.setattr(statusline_setup, "uninstall", lambda d=None: calls.append(("uninstall", d)))
        return calls

    @staticmethod
    def _settings() -> Path:
        from claude_rotater import statusline_setup

        return statusline_setup.settings_path()

    async def test_header_shows_feed_off_without_a_feed(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "○ feed off" in painted(app, "#status")

    async def test_header_shows_live_feed_from_feed_file(self, tmp_path):
        from claude_rotater import feed, statusline_setup

        assert self._settings().is_relative_to(Path.home())  # never the real one
        statusline_setup.install(tmp_path)
        path = Path(feed.feed_path(tmp_path))
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "● feed live" in painted(app, "#status")

    async def test_s_confirms_then_installs(self, tmp_path, monkeypatch):
        from claude_rotater.tui.modals import ConfirmModal

        calls = self._calls(monkeypatch, self._status("not-installed", "off"))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await settle(pilot)
            assert calls == []
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert calls == [("install", tmp_path)]

    async def test_s_when_installed_offers_removal(self, tmp_path, monkeypatch):
        from claude_rotater.tui.modals import ConfirmModal

        calls = self._calls(monkeypatch, self._status("installed", "stale"))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "◐ feed stale" in painted(app, "#status")
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert calls == [("uninstall", tmp_path)]

    async def test_install_failure_is_shown_not_raised(self, tmp_path):
        from claude_rotater.tui.modals import OutputModal

        settings = self._settings()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not json")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s", "y")
            await settle(pilot)
            assert isinstance(app.screen, OutputModal)
            shown = " ".join(str(w.render()) for w in app.screen.query("Static"))
            assert "were not changed" in shown and "not valid JSON" in shown
            assert not app.busy
        assert settings.read_text() == "{not json"

    async def test_s_installs_and_removes_for_real(self, tmp_path):
        from claude_rotater import statusline_setup

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s", "y")
            await settle(pilot)
            assert statusline_setup.status(tmp_path).installed
            assert app.feed_installed and app.feed_state == "stale"
            assert "◐ feed stale" in painted(app, "#status")
            await pilot.press("s", "y")
            await settle(pilot)
            assert not app.feed_installed and app.feed_state == "off"
        assert "statusLine" not in json.loads(self._settings().read_text())

    async def test_foreign_statusline_is_wrapped_and_restored(self, tmp_path):
        from claude_rotater.tui.modals import ConfirmModal

        settings = self._settings()
        settings.parent.mkdir(parents=True, exist_ok=True)
        original = {"statusLine": {"type": "command", "command": "my-line --x"}}
        settings.write_text(json.dumps(original))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.feed_install == "foreign"
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert "rota wraps it" in app.screen._message
            await pilot.press("y")
            await settle(pilot)
            assert app.feed_install == "installed"
            await pilot.press("s", "y")
            await settle(pilot)
        assert json.loads(settings.read_text()) == original


@pytest.mark.asyncio
class TestWidths:
    @pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 50)])
    async def test_main_page_fits_with_table_and_strip(self, tmp_path, size):
        from textual.widgets import Button

        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            width = size[0]
            status = painted(app, "#status")
            assert len(status) <= width and "rota" in status and "switch at 90%" in status
            for number in ("1", "2", "3"):
                assert len(row_plain(app, number)) <= width - 1
            first = row_plain(app, "1")
            assert "62%" in first and "31%" in first and ("1h59m" in first or "2h00m" in first)
            assert has_bar(first) == (width >= 120)
            detail = painted(app, "#detail").splitlines()
            assert 3 <= len(detail) <= 4
            assert detail[0].startswith("me@x.com")
            assert any(line.startswith("5h") and "62%" in line for line in detail)
            assert any(line.startswith("7d") and "31%" in line for line in detail)
            assert not any(0x2800 <= ord(ch) <= 0x28FF for ch in "".join(detail))
            assert all(len(line) <= width for line in detail)
            assert all(b.region.right <= width for b in app.screen.query(Button) if b.display)
            assert app.screen.query_one("#actions").region.bottom <= size[1]

    @pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 50)])
    async def test_model_columns_follow_the_data(self, tmp_path, size):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(62.0, 31.0, scoped=[("Fable", 34.0)])),
                make_account(2, entry=make_entry(12.0, 44.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            assert "Fable" in painted(app, "#columns")
            assert "34%" in row_plain(app, "1")
            assert "·" in row_plain(app, "2")  # no Fable window: a dot, not 0%
            assert any(line.startswith("Fable") for line in painted(app, "#detail").splitlines())
        plain = make_app(three_accounts(tmp_path))
        async with plain.run_test(size=size) as pilot:
            await settle(pilot)
            assert "Fable" not in painted(plain, "#columns")

    @pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 50)])
    async def test_long_term_view_fits(self, tmp_path, size):
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            detail = painted(app, "#detail").splitlines()
            assert any("5h peaks" in line for line in detail)
            assert any("7d usage" in line for line in detail)
            assert all(len(line) <= size[0] for line in detail)

    @pytest.mark.parametrize("size", [(80, 24), (120, 40), (160, 50)])
    async def test_config_page_fits(self, tmp_path, size):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            title = app.screen.query_one("#config-title").render().plain
            assert title.startswith(" rota ") and "settings" in title
            await pilot.press("right")  # ←/→ still adjust on the restyled list
            await pilot.pause()
            from claude_rotater.settings import effective_settings

            values = {spec.dotted: (v, is_set) for spec, v, is_set in effective_settings(tmp_path)}
            assert values["autoswitch.threshold"][1]


@pytest.mark.asyncio
class TestRepaintDiscipline:
    async def test_unchanged_store_reads_do_not_repaint(self, tmp_path, monkeypatch):
        from claude_rotater.tui.main import MainScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            count = {"n": 0}
            original = MainScreen.repaint

            def counting(self):
                count["n"] += 1
                original(self)

            monkeypatch.setattr(MainScreen, "repaint", counting)
            for _ in range(3):
                app._tick()
                await settle(pilot)
            assert count["n"] == 0
            fake = app.switcher
            fake._accounts[0] = dataclasses.replace(
                fake._accounts[0], usage=make_entry(70.0, 31.0, age_s=1.0)
            )
            app._tick()
            await settle(pilot)
            assert count["n"] >= 1
            assert "70%" in row_plain(app, "1")

    async def test_no_sub_second_timers(self, tmp_path):
        from claude_rotater.tui.main import MainScreen

        assert MainScreen.CLOCK_TICK_S >= 30
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            intervals = [
                t._interval for node in (app, app.screen) for t in node._timers
                if t.name != "screen_update"  # Textual's own compositor tick
            ]
            assert intervals and min(intervals) >= app.POLL_INTERVAL_S
