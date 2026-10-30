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
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_rotater.autoswitch import NoSwitchEvent, PollEvent, SleepEvent, SwitchEvent
from claude_rotater.json_output import (
    USAGE_API_KEY,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.switcher import ClaudeAccountSwitcher
from claude_rotater.tui import data as tui_data
from claude_rotater.tui.theme import SEV_CRIT, SEV_OK, SEV_WARN, Palette
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
        return str(slot or 9)

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
def same_code(monkeypatch):
    """An engine lock this test process takes runs the current code. Without
    this, a source file saved (or checked out) after the pytest worker started
    makes the holder "older code", and a view-only window shows that warning
    in place of the engine's verdict."""
    monkeypatch.setattr("claude_rotater.instance.newest_source_mtime", lambda: None)


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
            == "access token lapsed (normal) — the CLI refreshes it on next use"
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
        assert "access token lapsed (normal) — the CLI refreshes it on next use" in card
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


def styles_of(text, needle: str) -> list[str]:
    """Styles of the spans that cover the first occurrence of ``needle``."""
    start = text.plain.index(needle)
    end = start + len(needle)
    return [str(sp.style) for sp in text.spans if sp.start < end and sp.end > start]


def is_dim(text, needle: str) -> bool:
    return any("dim" in style.split() for style in styles_of(text, needle))


def has_color(text, needle: str, color: str) -> bool:
    return any(color in style for style in styles_of(text, needle))


def braille_cells(lines, start: int = 0, end: int | None = None, *, glyph: str | None = None) -> list[tuple[int, str]]:
    """(column, style) of every braille cell (or every ``glyph``) in ``lines``
    between the columns."""
    out = []
    for line in lines:
        for sp in line.spans:
            for c in range(sp.start, sp.end):
                ch = line.plain[c]
                hit = ch == glyph if glyph is not None else 0x2800 <= ord(ch) <= 0x28FF
                if start <= c < (end if end is not None else 10**6) and hit:
                    out.append((c, str(sp.style)))
    return out


def window_est(label="5h", last=60.0, rate=20.0, conf="medium", *, ago=1800.0, resets_in=7200, now=None):
    """60% measured 30 min ago at 20%/h: 90% in an hour, before ``make_entry``'s 2h reset."""
    from claude_rotater.estimator import AccountEstimate, WindowEstimate

    now = now if now is not None else time.time()
    win = WindowEstimate(label, last, now - ago, now + resets_in, rate, conf, ())
    return AccountEstimate({label: win}, win, None, None, conf), now


class TestAccountRowText:
    """The one-line account row: measured now, plus one forecast cell."""

    def _layout(self, width: int = 120, windows=("5h", "7d"), **kw):
        from claude_rotater.tui.main import RowLayout

        return RowLayout.for_width(width, windows, **kw)

    def _text(self, acc, est=None, *, width=120, threshold=90.0, now=None, windows=("5h", "7d"), **kw):
        from claude_rotater.tui.main import account_row_text

        return account_row_text(
            acc,
            est,
            self._layout(width, windows, **kw),
            now=now if now is not None else time.time(),
            threshold=threshold,
        )

    def _row(self, *args, **kw):
        return self._text(*args, **kw).plain

    def _key(self, state=None, tail="AbCd"):
        acc = make_account(
            2, kind="api_key", email="api-key-2@token.local",
            entry=make_usage_at(None, sentinel=USAGE_API_KEY),
        )
        return dataclasses.replace(acc, key_tail=tail, key_state=state)

    def test_an_api_key_shows_its_fingerprint_and_spend(self):
        from claude_rotater import api_keys

        now = time.time()
        row = self._row(self._key(), now=now, tag_need=11)
        assert "key ••AbCd" in row and "metered" in row and "api key" not in row
        state = api_keys.KeyState(spent_usd=4.2, spent_since=now - 60)
        assert "$4.20 this month" in self._row(self._key(state), now=now)

    def test_an_api_key_against_its_budget(self):
        from claude_rotater import api_keys

        now = time.time()
        budget = api_keys.Budget("topup", 50.0, now - 86400)
        state = api_keys.KeyState(budget=budget, spent_usd=37.7, spent_since=budget.since)
        row = self._row(self._key(state), now=now, width=140)
        assert "$12.30 left of $50 since" in row and has_bar(row)
        state = api_keys.KeyState(budget=budget, spent_usd=49.5, spent_since=budget.since)
        assert "budget nearly spent ($0.50 left)" in self._row(self._key(state), now=now)

    def test_an_exhausted_api_key_is_red_and_says_how_to_retry(self):
        from claude_rotater import api_keys
        from claude_rotater.tui.theme import Palette

        state = api_keys.KeyState(exhausted_at=1.0, exhausted_reason=api_keys.OUT_OF_CREDIT)
        text = self._text(self._key(state))
        assert "out of credit  ⏎ after a top-up" in text.plain
        span = next(s for s in text.spans if text.plain[s.start:s.end] == "out of credit")
        assert str(span.style) == Palette.DARK.sev_crit

    def test_an_api_keys_clipboard_line(self):
        from claude_rotater import api_keys
        from claude_rotater.tui.main import summary_line

        state = api_keys.KeyState(exhausted_at=1.0, exhausted_reason=api_keys.OUT_OF_CREDIT)
        line = summary_line(self._key(state), None, threshold=90, now=time.time())
        assert line == "api-key-2@token.local · key ••AbCd · out of credit"

    def test_active_marker_and_pcts(self):
        row = self._row(make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com"))
        assert row.startswith(" ● me@x.com")  # on duty, not under the cursor
        assert "5h  62%" in row and "7d  31%" in row
        assert has_bar(row)  # each window carries its bar

    def test_bar_tints_out_to_the_projection(self):
        from claude_rotater.tui.main import PROJECTION_TINT
        from claude_rotater.tui.theme import blend

        tint = blend(SEV_OK, Palette.DARK.track, PROJECTION_TINT)
        est, now = window_est(rate=5.0, conf="high")  # 60% → ~67% at the 2h reset
        text = self._text(make_account(1, entry=make_entry(60.0, 10.0)), est, now=now, width=160)
        five = text.plain.index("5h")
        seven = text.plain.index("7d")
        tinted = [c for c in range(len(text.plain)) if any(tint in st for st in cell_styles(text, c))]
        assert tinted and all(five < c < seven for c in tinted)  # lands well under 90%: green tint
        # the 7d window has no estimate: a solid fill only

    def test_thin_estimate_draws_no_tint(self):
        from claude_rotater.tui.main import PROJECTION_TINT
        from claude_rotater.tui.theme import blend

        est, now = window_est(conf="low")
        text = self._text(make_account(1, entry=make_entry(60.0, 10.0)), est, now=now, width=160)
        tint = blend(SEV_OK, Palette.DARK.track, PROJECTION_TINT)
        assert not any(tint in str(sp.style) for sp in text.spans)

    def test_inactive_has_no_marker(self):
        row = self._row(make_account(2, entry=make_entry(12.0, 44.0), email="work@y.com"))
        assert row.startswith("   work@y.com")

    def test_disabled_tag_is_off_personal_tag_hidden(self):
        row = self._row(make_account(3, disabled=True), tag_need=8)
        assert "off" in row and "disabled" not in row
        plain = self._row(make_account(3), tag_need=8)
        assert "personal" not in plain and "off" not in plain

    def test_email_shown_once_for_a_personal_org(self):
        acc = dataclasses.replace(
            make_account(1, email="me@x.com"), org_name="me@x.com's Organization", org_uuid="o"
        )
        row = self._row(acc, tag_need=24)
        assert row.count("me@x.com") == 1

    def test_real_org_is_tagged(self):
        acc = dataclasses.replace(make_account(1, email="me@x.com"), org_name="Acme Corp", org_uuid="o")
        assert "Acme Corp" in self._row(acc, tag_need=9)

    def test_absent_window_is_blank_not_invented(self):
        row = self._row(make_account(1, entry=make_entry(pct5=92.0, pct7=None)))
        assert "92%" in row
        assert "7d" not in row and "·" not in row

    def test_dead_login_is_a_short_tag(self):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

        acc = make_account(1, entry=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED))
        text = self._text(acc, tag_need=8)
        assert "re-login" in text.plain and "%" not in text.plain
        assert any(SEV_WARN in s for s in styles_of(text, "re-login"))

    def test_other_sentinel_is_its_tag(self):
        from claude_rotater.json_output import USAGE_NO_CREDENTIALS

        row = self._row(make_account(1, entry=make_entry(sentinel=USAGE_NO_CREDENTIALS)), tag_need=14)
        assert USAGE_NO_CREDENTIALS in row and "%" not in row

    def test_lapsed_access_token_says_it_refreshes(self):
        row = self._row(make_account(1, entry=make_entry(sentinel=USAGE_TOKEN_EXPIRED)), tag_need=16)
        assert "refreshes on use" in row and USAGE_TOKEN_EXPIRED not in row and "%" not in row

    def test_no_usage_says_unknown(self):
        assert "usage unknown" in self._row(make_account(1, entry=UsageEntry()))

    def test_countdown_only_no_clock(self):
        now = time.time()
        row = self._row(make_account(1, entry=make_entry(80.0, 10.0)), now=now, width=160)
        assert "↻ 1h59m" in row or "↻ 2h00m" in row
        assert "↻ 2d" in row  # coarse once past two days
        assert ":" not in row  # reset clocks live on the charts, not here

    def test_unused_window_drops_its_countdown(self):
        row = self._row(make_account(1, entry=make_entry(0.0, 10.0)))
        assert "5h   0%" in row and row.count("↻") == 1

    def test_stale_measurement_keeps_its_colours_and_says_its_age(self):
        text = self._text(make_account(1, entry=make_entry(62.0, 31.0, age_s=3600.0)), tag_need=11)
        assert "stale 1h00m" in text.plain
        assert has_color(text, "62%", SEV_OK) and has_color(text, "█", SEV_OK)  # never washed out

    def test_idle_account_between_spare_polls_is_not_stale(self):
        from claude_rotater.poll_policy import PollCadence

        spare = make_account(2, entry=make_entry(62.0, 31.0, age_s=9 * 60.0))
        assert "stale" not in self._row(spare, tag_need=11)  # due every 10 min: on time
        late = make_account(2, entry=make_entry(62.0, 31.0, age_s=16 * 60.0))
        assert "stale 16m" in self._row(late, tag_need=11)  # past 1.5× the spare interval
        from claude_rotater.tui.main import account_row_text

        quick = PollCadence(spare_s=120.0)
        text = account_row_text(spare, None, self._layout(120, tag_need=11), now=time.time(),
                                threshold=90.0, cadence=quick).plain
        assert "stale 9m" in text  # the account's own cadence decides

    def test_active_account_is_stale_past_the_feed_limit(self):
        from claude_rotater.poll_policy import DEFAULT_CADENCE

        limit = 1.5 * DEFAULT_CADENCE.feed_stale_s
        fresh = make_account(1, active=True, entry=make_entry(62.0, 31.0, age_s=limit - 30))
        assert "stale" not in self._row(fresh, tag_need=11)
        late = make_account(1, active=True, entry=make_entry(62.0, 31.0, age_s=limit + 60))
        assert "stale" in self._row(late, tag_need=11)

    def test_failed_poll_flags_an_aging_reading_early(self):
        entry = dataclasses.replace(make_entry(62.0, 31.0, age_s=6 * 60.0), last_error="http-429")
        assert "stale 6m" in self._row(make_account(2, entry=entry), tag_need=11)
        fresh = dataclasses.replace(make_entry(62.0, 31.0, age_s=60.0), last_error="http-429")
        assert "stale" not in self._row(make_account(2, entry=fresh), tag_need=11)

    def test_measured_cells_are_never_estimate_styled(self):
        est, now = window_est()
        text = self._text(make_account(1, entry=make_entry(60.0, 10.0)), est, now=now)
        assert not is_dim(text, "60%") and not is_dim(text, "↻")
        assert text.plain.count("~") == 1  # only the forecast carries the mark

    def test_forecast_cell_is_an_estimate(self):
        est, now = window_est()
        text = self._text(make_account(1, active=True, entry=make_entry(60.0, 10.0)), est, now=now)
        assert "~5h limit in 60m" in text.plain
        assert has_color(text, "~5h limit", SEV_CRIT) and not is_dim(text, "~5h limit")  # crosses: red
        assert "%/h" not in text.plain  # no burn rates in the table

    def test_no_estimate_is_unsure(self):
        text = self._text(make_account(1, entry=make_entry(40.0, 10.0)))
        assert text.plain.rstrip().endswith("↻ 2d23h")  # no guess: the forecast cell is blank
        assert "~" not in text.plain and "?" not in text.plain

    def test_model_window_column(self):
        entry = make_entry(10.0, 20.0, scoped=[("Fable", 55.0)])
        row = self._row(make_account(1, entry=entry), width=160, windows=("5h", "7d", "Fable"))
        assert "Fable  55%" in row and row.count("↻") == 3

    def test_model_column_without_that_window_is_blank(self):
        row = self._row(make_account(1, entry=make_entry(10.0, 20.0)), width=160, windows=("5h", "7d", "Fable"))
        assert "Fable" not in row and "·" not in row and "7d  20%" in row

    @pytest.mark.parametrize("width", [60, 80, 120, 160])
    def test_fits_the_width(self, width):
        entry = make_entry(62.0, 31.0, scoped=[("Fable", 34.0)])
        est, now = window_est()
        acc = make_account(1, active=True, entry=entry, email="someone@example.com")
        row = self._row(acc, est, now=now, width=width, windows=("5h", "7d", "Fable"), tag_need=8,
                        name_need=19, short_name_need=10)
        assert len(row) <= width
        # name, 5h % and its reset, 7d % at every width
        assert "62%" in row and "31%" in row and ("↻ 1h59m" in row or "↻ 2h00m" in row)
        assert ("~5h" in row) or width <= 60  # at 60: name, 5h, 7d and the reset
        # a long name keeps its local part: it gives way to the forecast
        assert ("someone@example.com" in row) or width < 80
        assert "someone@" in row

    def test_five_hour_reset_follows_its_pct_at_every_width(self):
        import re

        for width in (50, 60, 80, 100, 120, 160):
            row = self._row(make_account(1, entry=make_entry(62.0, 31.0)), width=width, name_need=10)
            assert re.search(r"5h  62%[^↻]*↻ \d", row), (width, row)

    def test_org_column_goes_before_the_name_shrinks_warnings_stay(self):
        from claude_rotater.tui.main import RowLayout

        windows = ("5h", "7d")
        wide = RowLayout.for_width(120, windows, name_need=20, tag_need=9, warn_tag_need=8)
        narrow = RowLayout.for_width(64, windows, name_need=20, short_name_need=7, tag_need=9, warn_tag_need=8)
        assert wide.level.org and wide.tag_w == 9
        assert not narrow.level.org and narrow.tag_w == 8  # room kept for "login 2d"
        org = dataclasses.replace(make_account(2, email="work@y.com"), org_name="Acme Corp", org_uuid="o")
        assert "Acme" not in self._row(org, width=64, name_need=20, tag_need=9, warn_tag_need=8)

    def test_email_truncates_to_its_local_part(self):
        from claude_rotater.tui.main import fit_name, short_name_len

        assert fit_name("work@example.com", 8) == "work@…  "
        assert fit_name("work@example.com", 16) == "work@example.com"
        assert fit_name("averyveryverylongname@x.com", 8) == "averyve…"
        assert short_name_len("work@example.com") == 6

    def test_rows_line_up_without_a_header(self):
        layout = self._layout(120)
        from claude_rotater.tui.main import account_row_text

        rows = [
            account_row_text(make_account(n, entry=make_entry(p5, p7), email=email), None, layout,
                             now=time.time(), threshold=90.0).plain
            for n, p5, p7, email in ((1, 62.0, 31.0, "a@x.com"), (2, 5.0, 100.0, "longer.name@y.com"))
        ]
        assert rows[0].index("7d") == rows[1].index("7d")


def cell_styles(text, col: int) -> list[str]:
    """Styles of the spans covering one cell."""
    return [str(sp.style) for sp in text.spans if sp.start <= col < sp.end]


class TestProjectionBar:
    def _bar(self, pct, projected, width=10, threshold=90.0, palette=Palette.DARK):
        from claude_rotater.tui.main import projection_bar

        return projection_bar(pct, projected, width, threshold=threshold, palette=palette)

    @staticmethod
    def _tint(color, palette=Palette.DARK):
        from claude_rotater.tui.main import PROJECTION_TINT
        from claude_rotater.tui.theme import blend

        return blend(color, palette.track, PROJECTION_TINT)

    def test_measured_fill_then_tint_then_tick(self):
        bar = self._bar(30.0, 60.0)
        assert bar.plain == "██████  ▕ "  # solid blocks: no hatch to squint at
        assert all(any(SEV_OK + " " in st for st in cell_styles(bar, c)) for c in range(3))
        tint = self._tint(SEV_OK)  # 60% lands well under: a green tint
        assert all(any(tint in st for st in cell_styles(bar, c)) for c in (3, 4, 5))
        assert tint != SEV_OK
        assert not any("dim" in str(sp.style) for sp in bar.spans)

    @pytest.mark.parametrize("projected, color", [(70.0, SEV_OK), (85.0, SEV_WARN), (100.0, SEV_CRIT)])
    def test_tint_is_green_yellow_red_by_outcome(self, projected, color):
        bar = self._bar(30.0, projected)
        assert any(self._tint(color) in st for st in cell_styles(bar, 4))

    def test_light_theme_tint_reads_on_its_track(self):
        from claude_rotater.tui.theme import ROTA_LIGHT

        light = Palette.from_theme(ROTA_LIGHT)
        bar = self._bar(30.0, 60.0, palette=light)
        tint = self._tint(light.sev_ok, light)
        assert tint not in (light.sev_ok, light.track)
        assert any(tint in st for st in cell_styles(bar, 4))

    def test_no_projection_is_measured_only(self):
        assert self._bar(30.0, None).plain == "███     ▕ "
        assert self._bar(30.0, 20.0).plain == "███     ▕ "  # never projects backwards

    def test_tint_follows_a_partial_cell(self):
        bar = self._bar(35.0, 70.0)
        assert bar.plain.startswith("███▌███")
        assert any(self._tint(SEV_OK) in st for st in cell_styles(bar, 4))

    def test_partial_cell_is_an_eighth_block(self):
        assert self._bar(35.0, None).plain.startswith("███▌")


class TestRowLayout:
    def test_bars_shrink_with_the_width(self):
        from claude_rotater.tui.main import RowLayout

        windows = ("5h", "7d", "Fable")
        widths = [RowLayout.for_width(w, windows, name_need=10, tag_need=9).bar_w for w in (160, 120, 100, 78)]
        assert widths[0] == 10 and 8 <= widths[1] <= 10
        assert widths == sorted(widths, reverse=True) and widths[-1] <= 4

    def test_degrades_in_order(self):
        from claude_rotater.tui.main import LEVELS, RowLayout

        widths = [RowLayout.for_width(w, ("5h", "7d", "Fable"), tag_need=8)
                  for w in (200, 120, 95, 80, 66, 50)]
        ranks = [LEVELS.index(lay.level) for lay in widths]
        assert ranks == sorted(ranks)
        assert widths[0].level.long_forecast and not widths[-1].level.long_forecast
        assert all(lay.name_w >= 8 for lay in widths)

    def test_names_capped(self):
        from claude_rotater.tui.main import RowLayout

        layout = RowLayout.for_width(160, ("5h", "7d"), name_need=60)
        assert layout.name_w == 32


class TestDurations:
    @pytest.mark.parametrize(
        "seconds, expected",
        [(-5, "<1m"), (30, "<1m"), (40 * 60, "40m"), (80 * 60, "1h20m"), (9 * 3600 + 59 * 60, "9h59m"),
         (23 * 3600 + 40 * 60, "23h40m"), (47 * 3600, "1d23h"), (2 * 86400 + 6 * 3600, "2d6h"),
         (3 * 86400, "3d")],
    )
    def test_countdown(self, seconds, expected):
        from claude_rotater.tui.main import countdown_text

        assert countdown_text(seconds) == expected

    @pytest.mark.parametrize(
        "seconds, expected",
        [(0, "5m"), (38 * 60, "40m"), (89 * 60, "90m"), (14 * 3600 + 56 * 60, "15h"),
         (47 * 3600, "1d23h"), (3.4 * 86400, "3d10h"), (3 * 86400 + 600, "3d")],
    )
    def test_rough_estimate(self, seconds, expected):
        from claude_rotater.tui.main import rough_duration

        assert rough_duration(seconds) == expected


class TestCompactDuration:
    @pytest.mark.parametrize(
        "seconds, expected",
        [(-5, "<1m"), (30, "<1m"), (60, "1m"), (59 * 60, "59m"), (3600, "1h00m"),
         (7140, "1h59m"), (86399, "23h59m"), (86400, "1d"), (6 * 86400 + 23 * 3600, "6d23h")],
    )
    def test_format(self, seconds, expected):
        assert tui_data.compact_duration(seconds) == expected
        assert len(tui_data.compact_duration(seconds)) <= 6


class TestStatusText:
    def _status(self, **kw):
        from claude_rotater.tui.main import AutoStatus

        return AutoStatus(**kw)

    def _text(self, status, now=None, width=120, **kw):
        from claude_rotater.tui.main import status_text

        return status_text(status, now=now or time.time(), width=width, **kw).plain

    def test_engine_state_only_no_legend(self):
        now = time.time()
        text = self._text(
            self._status(running=True, threshold=90.0, next_check_at=now + 80, last_check_at=now - 5), now
        )
        assert text.startswith(" rota ") and "auto on" in text
        assert text.rstrip().endswith("auto on") and "estimate" not in text and "~" not in text
        assert "90%" not in text and "next" not in text and "checked" not in text

    def test_stopped_engine_says_off(self):
        assert "auto off" in self._text(self._status(running=False))

    @pytest.mark.parametrize(
        "state,words",
        [("live", "● live"), ("stale", "◐ feed: no data yet"),
         ("off", "○ statusline off (s)"), ("unset", "○ feed: not set up")],
    )
    def test_feed_indicator(self, state, words):
        assert words in self._text(self._status(running=True), feed=state)

    def test_quiet_feed_shows_the_age_of_the_newest_reading(self):
        now = time.time()
        text = self._text(self._status(running=True), now, feed="idle", polled_at=now - 125)
        assert text.rstrip().endswith("↻ 2m ago")
        assert "idle" not in text and "stale" not in text and "feed" not in text

    @pytest.mark.parametrize("state", ["live", "stale", "off", "unset"])
    def test_one_indicator_only(self, state):
        now = time.time()
        assert "ago" not in self._text(self._status(running=True), now, feed=state,
                                       polled_at=now - 125)

    def test_never_polled_quiet_feed_shows_nothing(self):
        text = self._text(self._status(running=True), feed="idle")
        assert "↻" not in text and "idle" not in text

    def _age_style(self, age, late_after=1200.0):
        from claude_rotater.tui.main import status_text

        now = time.time()
        head = status_text(self._status(running=True), now=now, width=120, feed="idle",
                           polled_at=now - age, late_after=late_after)
        start = head.plain.index("↻")
        return {str(span.style) for span in head.spans if span.start <= start < span.end}

    def test_recent_reading_is_dim(self):
        assert self._age_style(125) == {Palette.DARK.muted}

    def test_late_reading_is_amber(self):
        assert self._age_style(35 * 60) == {Palette.DARK.sev_warn}
        assert self._age_style(35 * 60, late_after=3600) == {Palette.DARK.muted}

    def test_late_after_follows_the_poll_interval(self):
        from claude_rotater.tui.main import LATE_POLL_FACTOR
        from claude_rotater.poll_policy import DEFAULT_CADENCE

        assert LATE_POLL_FACTOR * DEFAULT_CADENCE.spare_s == 1200.0

    def test_warnings_are_amber(self):
        from claude_rotater.tui.main import status_text

        for state in ("stale", "off", "unset"):
            head = status_text(self._status(running=True), now=time.time(), width=120, feed=state)
            start = head.plain.rindex("   ") + 3
            styles = {str(span.style) for span in head.spans if span.start <= start < span.end}
            assert styles == {Palette.DARK.sev_warn}, state

    def test_no_feed_state_no_indicator(self):
        text = self._text(self._status(running=True))
        assert "feed" not in text and "●" not in text and "↻" not in text

    def test_cooldown_shows_only_while_it_holds_a_switch(self):
        now = time.time()
        status = self._status(
            running=True, cooldown_s=300, last_switch_at=now - 60,
            last_outcome="no switch: cooldown", last_reason="cooldown",
        )
        assert "switch held: cooldown 4m" in self._text(status, now, over_threshold=True)
        assert "cooldown" not in self._text(status, now, over_threshold=False)  # a nudge, not a block
        assert "cooldown" not in self._text(status, now + 400, over_threshold=True)

    def test_blocked_reason_shown_routine_one_hidden(self):
        blocked = self._status(running=True, last_outcome="no switch: no-candidates", last_reason="no-candidates")
        assert "no switch: no-candidates" in self._text(blocked)
        routine = self._status(running=True, last_outcome="no switch: below-threshold", last_reason="below-threshold")
        assert "no switch" not in self._text(routine)

    def test_last_switch_is_not_repeated_in_the_header(self):
        now = time.time()
        assert "switched" not in self._text(self._status(running=True, last_switch_at=now, last_switch="2 → 1"), now)

    def test_narrow_drops_the_note(self):
        status = self._status(running=True, last_outcome="no switch: no-viable-target", last_reason="no-viable-target")
        text = self._text(status, width=40, feed="live")
        assert "● live" in text
        assert "no-viable-target" not in text and len(text) <= 40

    def test_login_note_outranks_the_verdict_and_the_held_note(self):
        status = self._status(
            running=True, decision=self._decision(),
            last_outcome="no switch: no-candidates", last_reason="no-candidates",
        )
        note = "#3 login was revoked (signed in elsewhere?) — press l to log in again"
        text = self._text(status, feed="live", login_note=note, login_failed=True)
        assert note in text and "stay" not in text and "no-candidates" not in text
        assert text.endswith("● live")

    def test_failed_login_note_is_red_ending_is_amber(self):
        from claude_rotater.tui.main import status_text

        for failed, role in ((True, Palette.DARK.sev_crit), (False, Palette.DARK.sev_warn)):
            head = status_text(self._status(running=True), now=time.time(), width=120,
                               login_note="#3 login ended — press l", login_failed=failed)
            start = head.plain.index("#3")
            styles = {str(span.style) for span in head.spans if span.start <= start < span.end}
            assert styles == {f"bold {role}"}

    def test_narrow_cuts_the_login_note_but_keeps_it(self):
        note = "login ends in 2d: #3 — press l"
        text = self._text(self._status(running=True), width=36, feed="live", login_note=note)
        assert len(text) <= 36 and "login" in text and "● live" not in text

    def _decision(self, line="stay: resets soonest (19h40m), 37% headroom", action="stay"):
        from claude_rotater.autoswitch import Decision

        return Decision(action=action, line=line, ts="2026-10-16T00:00:00Z")

    def test_latest_verdict_follows_auto_on(self):
        text = self._text(self._status(running=True, decision=self._decision()), feed="live")
        assert "auto on · stay: resets soonest (19h40m), 37% headroom   ● live" in text

    def test_poll_age_sits_beside_the_verdict(self):
        now = time.time()
        status = self._status(running=True, decision=self._decision())
        for width in (120, 60):
            text = self._text(status, now, width=width, feed="idle", polled_at=now - 125)
            assert "stay:" in text and text.rstrip().endswith("↻ 2m ago"), width

    def test_verdict_is_dim(self):
        from claude_rotater.tui.main import status_text

        head = status_text(self._status(running=True, decision=self._decision()), now=time.time(), width=120)
        start = head.plain.index("stay:")
        styles = {str(span.style) for span in head.spans if span.start <= start < span.end}
        assert styles == {Palette.DARK.muted}

    def test_verdict_is_cut_to_fit_and_keeps_the_feed(self):
        text = self._text(self._status(running=True, decision=self._decision()), width=50, feed="live")
        assert len(text) <= 50 and text.endswith("● live")
        assert "· stay: resets" in text and "…" in text

    def test_too_narrow_drops_the_verdict(self):
        text = self._text(self._status(running=True, decision=self._decision()), width=34, feed="live")
        assert "stay" not in text and "·" not in text.split("rota", 1)[1] and "● live" in text

    def test_no_verdict_while_the_engine_is_off(self):
        assert "stay" not in self._text(self._status(running=False, decision=self._decision()))

    def test_held_switch_note_replaces_the_verdict(self):
        status = self._status(
            running=True, decision=self._decision(),
            last_outcome="no switch: no-candidates", last_reason="no-candidates",
        )
        text = self._text(status)
        assert "no switch: no-candidates" in text and "stay:" not in text


class TestHeaderDecision:
    def test_switch_and_explained_stay_are_shown(self):
        from claude_rotater.tui.main import header_decision

        switch = header_decision(SwitchEvent(
            trigger="expiring-first", from_ref={"number": "1"}, to_ref={"number": "2", "email": "b@x.com"},
            reason="switch → #2: #1's 7d resets in 6d, #2's in 19h",
        ))
        assert switch.action == "switch" and switch.target == "2" and switch.line.startswith("switch → #2")
        stay = header_decision(NoSwitchEvent(reason="stay", detail="stay: resets soonest (19h40m)"))
        assert stay.action == "stay" and stay.line == "stay: resets soonest (19h40m)"

    @pytest.mark.parametrize("reason", ["cooldown", "below-threshold", "no-candidates"])
    def test_machine_outcomes_keep_the_last_verdict(self, reason):
        from claude_rotater.tui.main import header_decision

        assert header_decision(NoSwitchEvent(reason=reason)) is None
        assert header_decision(PollEvent(active={"number": 1}, headroom={}, threshold=90.0)) is None

    @pytest.mark.parametrize(
        "reason,to_ref,expected",
        [
            ("switch → #2: #1's 7d resets in 6d", {"number": "2", "email": "b@x.com"}, "→ b@x.com: #1's 7d resets in 6d"),
            ("", {"number": "2", "email": "b@x.com"}, "→ b@x.com: proactive"),
            ("drained: 5h at 91%", {"number": "3"}, "→ #3: drained: 5h at 91%"),
        ],
    )
    def test_switch_flash(self, reason, to_ref, expected):
        from claude_rotater.tui.main import switch_flash

        event = SwitchEvent(trigger="proactive", from_ref={"number": "1"}, to_ref=to_ref, reason=reason)
        assert switch_flash(event) == expected

    def test_same_verdict_ignores_the_time(self):
        from claude_rotater.autoswitch import Decision
        from claude_rotater.tui.main import same_verdict

        a = Decision(action="stay", line="stay: x", ts="t1")
        assert same_verdict(dataclasses.replace(a, ts="t2"), a)
        assert not same_verdict(dataclasses.replace(a, line="stay: y"), a)
        assert not same_verdict(a, None)


class TestForecast:
    def _fc(self, est, acc=None, threshold=90.0, now=None, entry=None):
        from claude_rotater.tui.main import forecast

        acc = acc or make_account(1, entry=entry or make_entry(60.0, 10.0))
        return forecast(acc, est, threshold=threshold, now=now or time.time())

    def test_trusted_crossing_is_a_rough_estimate(self):
        est, now = window_est(conf="high")
        fc = self._fc(est, now=now)
        assert fc.estimate and fc.text == "~5h limit in 60m" and fc.short == "~5h in 60m"

    @pytest.mark.parametrize("conf", ["low", "none"])
    def test_thin_estimate_says_nothing(self, conf):
        est, now = window_est(conf=conf)
        assert self._fc(est, now=now) is None

    def test_no_estimate_says_nothing(self):
        assert self._fc(None) is None

    def test_stays_under_is_ok(self):
        est, now = window_est(rate=0.5, conf="high")  # 60% → ~61% by the reset
        fc = self._fc(est, now=now)
        assert fc.text == "ok" and not fc.estimate

    def test_ok_is_always_green_even_close_to_the_threshold(self):
        est, now = window_est(rate=12.0, conf="high", ago=0.0, resets_in=7200)  # 60% → ~84%
        fc = self._fc(est, now=now)
        assert fc.text == "ok" and fc.role == "ok"

    def test_measured_at_threshold_is_not_an_estimate(self):
        fc = self._fc(None, entry=make_entry(95.0, 10.0))
        assert fc.text == "5h at limit" and not fc.estimate and fc.role == "crit"

    def test_projected_past_threshold(self):
        est, now = window_est(last=88.0, rate=10.0, conf="high")  # 88% 30 min ago: 93% now
        fc = self._fc(est, now=now, entry=make_entry(88.0, 10.0))
        assert fc.text == "~5h at limit" and fc.estimate

    @staticmethod
    def _multi(*wins, now):
        """An estimate of several windows: ``(label, last, rate, ago, resets_in)``."""
        from claude_rotater.estimator import AccountEstimate, WindowEstimate

        ws = {
            label: WindowEstimate(label, last, now - ago, now + resets_in, rate, "high", ())
            for label, last, rate, ago, resets_in in wins
        }
        return AccountEstimate(ws, next(iter(ws.values())), None, None, "high")

    def test_both_cross_names_the_five_hour_when_it_is_first(self):
        now = time.time()
        # 5h: 60% at 30%/h, 90% in an hour; 7d: 80% at 5%/h, 90% in 2h.
        est = self._multi(("5h", 60.0, 30.0, 0.0, 7200), ("7d", 80.0, 5.0, 0.0, 3 * 86400), now=now)
        fc = self._fc(est, now=now, entry=make_entry(60.0, 80.0))
        assert fc.text == "~5h limit in 60m"

    def test_both_cross_names_the_seven_day_when_it_is_first(self):
        now = time.time()
        # 5h 90% in 1h30m; 7d: 80% at 40%/h, 90% in 15m.
        est = self._multi(("5h", 60.0, 20.0, 0.0, 7200), ("7d", 80.0, 40.0, 0.0, 3 * 86400), now=now)
        fc = self._fc(est, now=now, entry=make_entry(60.0, 80.0))
        assert fc.text == "~7d limit in 15m"

    def test_five_hour_crossing_after_its_reset_is_ignored(self):
        now = time.time()
        # 5h would reach 90% in 3h, after its 2h reset; 7d reaches it in 5h.
        est = self._multi(("5h", 60.0, 10.0, 0.0, 7200), ("7d", 80.0, 2.0, 0.0, 3 * 86400), now=now)
        fc = self._fc(est, now=now, entry=make_entry(60.0, 80.0))
        assert fc.text == "~7d limit in 5h"

    def test_model_window_first_is_named(self):
        from claude_rotater.tui.main import forecast

        now = time.time()
        est = self._multi(
            ("5h", 60.0, 20.0, 0.0, 7200), ("7d", 50.0, 1.0, 0.0, 3 * 86400),
            ("Fable", 80.0, 40.0, 0.0, 2 * 86400), now=now,
        )
        acc = make_account(1, entry=make_entry(60.0, 50.0, scoped=[("Fable", 80.0)]))
        fc = forecast(acc, est, threshold=90.0, now=now, models=("fable",))
        assert fc.text == "~Fable limit in 15m"

    def test_neither_crossing_is_ok(self):
        now = time.time()
        est = self._multi(("5h", 30.0, 2.0, 0.0, 7200), ("7d", 40.0, 0.5, 0.0, 3 * 86400), now=now)
        assert self._fc(est, now=now, entry=make_entry(30.0, 40.0)).text == "ok"

    @staticmethod
    def _graded(*wins, now):
        """Like :meth:`_multi` with a confidence per window:
        ``(label, last, rate, resets_in, confidence)``."""
        from claude_rotater.estimator import AccountEstimate, WindowEstimate

        ws = {
            label: WindowEstimate(label, last, now, now + resets_in, rate, conf, ())
            for label, last, rate, resets_in, conf in wins
        }
        return AccountEstimate(ws, next(iter(ws.values())), None, None, "high")

    def test_thin_first_crossing_leaves_the_trusted_one_named(self):
        # Real case (work profile): 5h too thin to trust but crossing first,
        # 7d trusted; the 7d chart said "limit in 12h" while the row was blank.
        now = time.time()
        est = self._graded(
            ("5h", 60.0, 30.0, 7200, "low"), ("7d", 80.0, 2.0, 3 * 86400, "medium"), now=now,
        )
        assert self._fc(est, now=now, entry=make_entry(60.0, 80.0)).text == "~7d limit in 5h"

    def test_thin_crossing_is_never_called_ok(self):
        now = time.time()
        est = self._graded(
            ("5h", 60.0, 30.0, 7200, "low"), ("7d", 40.0, 0.1, 3 * 86400, "high"), now=now,
        )
        assert self._fc(est, now=now, entry=make_entry(60.0, 40.0)) is None

    @pytest.mark.parametrize("conf5,conf7", [
        ("high", "high"), ("low", "medium"), ("medium", "low"), ("low", "low"),
    ])
    @pytest.mark.parametrize("rate5,rate7", [(30.0, 2.0), (5.0, 40.0), (2.0, 0.1)])
    def test_row_names_the_earliest_limit_any_chart_titles(self, conf5, conf7, rate5, rate7):
        from claude_rotater.tui.main import chart_blocks

        now = time.time()
        est = self._graded(
            ("5h", 60.0, rate5, 7200, conf5), ("7d", 80.0, rate7, 3 * 86400, conf7), now=now,
        )
        acc = make_account(1, entry=make_entry(60.0, 80.0))
        fc = self._fc(est, acc=acc, now=now)
        titled = [
            (label, title.plain.split("limit in ", 1)[1])
            for title, _render in chart_blocks(
                acc, est, {}, now=now, threshold=90.0, palette=Palette.DARK
            )
            if "limit in " in title.plain
            for label in [title.plain.split(" ", 1)[0]]
        ]
        if not titled:
            assert fc is None or not fc.text.startswith("~")
            return
        hit = min(
            ((est.windows[label].eta_s(90.0, now), label, when) for label, when in titled),
        )
        assert fc.text == f"~{hit[1]} limit in {hit[2]}"

    def test_nothing_to_forecast(self):
        est, now = window_est()
        assert self._fc(est, threshold=None) is None
        assert self._fc(est, acc=make_account(1, disabled=True, entry=make_entry(60.0, 10.0))) is None
        assert self._fc(est, acc=make_account(1, entry=make_entry(sentinel=USAGE_TOKEN_EXPIRED))) is None


class TestTableWindows:
    def test_unused_model_window_is_left_out_unless_it_gates(self):
        from claude_rotater.tui.main import table_windows

        unused = [make_account(1, entry=make_entry(10.0, 20.0, scoped=[("Fable", 0.0)]))]
        assert table_windows(unused, ()) == ("5h", "7d")
        assert table_windows(unused, ("fable",)) == ("5h", "7d", "Fable")
        used = unused + [make_account(2, entry=make_entry(10.0, 20.0, scoped=[("Fable", 3.0)]))]
        assert table_windows(used, ()) == ("5h", "7d", "Fable")


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


def burn_history(now: float, *, start: float = 32.0, rate: float = 15.0) -> dict:
    """Account 1 burning ``rate`` %/h on 5h for two hours (ends at start+2*rate,
    62% by default, the pct ``three_accounts`` measures), with the same
    resets as ``make_entry``."""
    return {
        "1": {
            "5h": linear_points(
                now=now, start_pct=start, rate_pct_per_h=rate,
                span_s=7200, step_s=300, resets_at=now + 7200,
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


def prompt_plain(app) -> str:
    """What the prompt line shows right now."""
    return app.screen.query_one("#prompt").render().plain


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
            assert not app.screen.query("#columns")  # no header line: cells name themselves
            first = row_plain(app, "1")
            assert first.startswith("›●") and "me@x.com" in first and "5h  62%" in first
            assert "\n" not in first
            assert "personal" not in first  # a personal account needs no tag
            assert "work@y.com" in row_plain(app, "2")
            assert "off" in row_plain(app, "3")

    async def test_active_account_is_highlighted_at_start(self, tmp_path):
        fake = FakeSwitcher(
            [make_account(1), make_account(2, active=True)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.screen.selected_number == "2"

    async def test_status_line_and_idle_prompt_line(self, tmp_path):
        from textual.widgets import Button, Footer, Header

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            status = painted(app, "#status")
            assert "rota" in status and "auto on" in status
            assert "estimate" not in status and "~" not in status  # no legend: titles say it
            assert "90%" not in status
            # no button bar, no quit button, no stock header/footer: one dim hint
            assert prompt_plain(app) == "⏎ switch · a add · m manual · $ costs · ? keys"
            for stock in (Button, Footer, Header):
                assert not app.screen.query(stock)
            assert app.focused is app.screen.query_one("#accounts")  # the list keeps the keys

    async def test_empty_account_list(self, tmp_path):
        app = make_app(FakeSwitcher([], tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "No managed accounts yet" in painted(app, "#detail")
            await pilot.press("enter", "d", "x")  # nothing to act on: no crash
            await settle(pilot)
            assert fake_calls(app) == []

    @pytest.mark.parametrize("size", [(120, 40), (80, 40)])
    async def test_charts_fill_the_spare_height(self, tmp_path, size):
        """The selected account's charts sit under the table and fill every
        spare row (no dead space below); the key strip keeps the bottom row."""
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            detail = app.screen.query_one("#detail")
            lines = painted(app, "#detail").splitlines()
            assert braille("\n".join(lines))
            assert detail.size.height == len(lines) <= app.screen.detail_budget()
            assert len(lines) == app.screen.detail_budget()
            actions = app.screen.query_one("#prompt")
            assert actions.region.y >= detail.region.bottom + 1
            assert actions.region.bottom == size[1]

    @pytest.mark.parametrize("width, height, shown", [(120, 18, True), (120, 17, False), (80, 27, True), (80, 26, False)])
    async def test_charts_hide_when_too_few_rows_are_spare(self, tmp_path, width, height, shown):
        """Three accounts leave height - 8 rows; the charts need 10 side by
        side and 19 stacked, else they are hidden rather than cramped."""
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(width, height)) as pilot:
            await settle(pilot)
            assert app.screen.charts_fit() is shown
            assert braille(painted(app, "#detail")) is shown
            assert app.screen.query_one("#prompt").region.bottom <= height

    @pytest.mark.parametrize("theme_name", ["dark", "light"])
    async def test_hidden_charts_give_their_rows_to_the_table(self, tmp_path, theme_name):
        """80x24 with many accounts: no cramped charts, and every row shows."""
        from claude_rotater.tui.main import AccountRow

        accounts = [make_account(1, active=True, entry=make_entry(62.0, 31.0))]
        accounts += [make_account(n, entry=make_entry(10.0 + n, 20.0)) for n in range(2, 16)]
        fake = FakeSwitcher(accounts, tmp_path)
        with_history(fake, burn_history(time.time()))
        from claude_rotater.settings import set_setting

        set_setting(tmp_path, "ui.theme", theme_name)
        app = make_app(fake)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            assert not braille(painted(app, "#detail"))
            listview = app.screen.query_one("#accounts")
            assert listview.size.height == 15  # not capped at 60% of the screen
            assert len(app.screen.query(AccountRow)) == 15
            assert app.screen.query_one("#prompt").region.bottom == 24

    async def test_charts_resize_with_the_terminal(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert braille(painted(app, "#detail"))
            await pilot.resize_terminal(120, 14)
            await settle(pilot)
            assert not braille(painted(app, "#detail"))

    @pytest.mark.parametrize("theme_name", ["rota-dark", "rota-light"])
    async def test_cursor_row_is_distinct_from_the_on_duty_row(self, tmp_path, theme_name):
        """The row under the cursor gets "›", a bold name and an accent-tinted
        band; the account on duty keeps its "●" wherever the cursor is."""
        from claude_rotater.tui.main import AccountRow

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.theme = theme_name
            await pilot.press("down")
            await settle(pilot)
            assert row_plain(app, "1").startswith(" ● #1 me@x.com")  # on duty, cursor elsewhere
            assert row_plain(app, "2").startswith("›  #2 work@y.com")
            rows = {r.number: r for r in app.screen.query(AccountRow)}
            band = rows["2"].styles.background
            assert band.a > 0 and band != rows["1"].styles.background
            name = rows["2"].children[0].render()
            assert any("bold" in st for st in styles_of(name, "work@y.com"))
            on_duty = rows["1"].children[0].render()
            assert not any("bold" in st for st in styles_of(on_duty, "me@x.com"))
            accent = Palette.from_theme(app.current_theme).accent
            assert has_color(on_duty, "me@x.com", accent)  # one marker: ● plus the accent name
            assert not has_color(name, "work@y.com", accent)
            # the charts say whose they are
            assert painted(app, "#detail").splitlines()[0].startswith("› #2 work@y.com")
            await pilot.hover(rows["3"])
            await pilot.pause()
            assert rows["3"].styles.background.a > 0  # hover tints too

    async def test_each_row_shows_its_number_in_the_accounts_own_color(self, tmp_path):
        """``#N`` before each name, the number the header, log and costs use,
        in a color kept by identity: the same in the charts' title."""
        from claude_rotater import account_colors
        from claude_rotater.tui.main import AccountRow

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            palette = Palette.from_theme(app.current_theme)
            rows = {r.number: r.children[0].render() for r in app.screen.query(AccountRow)}
            hues = {}
            for number, text in rows.items():
                assert f"#{number} " in text.plain
                acc = next(a for a in app.snapshot.accounts if a.number == number)
                hues[number] = palette.account(account_colors.indices([(acc.email, acc.org_uuid)])[
                    (acc.email, acc.org_uuid)])
                assert has_color(text, f"#{number}", hues[number])
            assert len(set(hues.values())) == len(hues)  # three accounts, three hues
            title = app.screen.query_one("#detail").render()
            assert has_color(title, "#1 ", hues["1"])

    @pytest.mark.parametrize("theme_name", ["dark", "light"])
    @pytest.mark.parametrize("size", [(60, 20), (80, 24), (120, 40), (160, 50)])
    async def test_question_mark_toggles_the_keys_in_place_of_the_charts(self, tmp_path, size, theme_name):
        import re

        from claude_rotater.settings import set_setting
        from claude_rotater.tui.main import MainScreen

        set_setting(tmp_path, "ui.theme", theme_name)
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)  # no popup: the same page
            assert app.screen.keys_shown
            body = painted(app, "#detail")
            for key, word in (("enter", "switch"), ("a", "add"), ("l", "log in"), ("t", "paste a token"),
                              ("d", "turn off"), ("x", "remove"), ("c", "config"), ("h", "history"),
                              ("s", "statusline"), ("f", "refresh"), ("q", "quit"), ("ctrl+t", "theme"),
                              ("?", "this list")):
                assert re.search(rf"(^|   ){re.escape(key)} +{word}", body, re.M), key
            for heading in ("accounts", "view", "setup", "app"):
                assert heading in body
            assert not braille(body)  # the keys replace the charts
            # the whole list shows, inside the terminal, the table still on screen
            detail = app.screen.query_one("#detail")
            assert detail.size.height == len(body.splitlines())
            assert all(len(line) <= size[0] - 2 for line in body.splitlines())
            assert detail.region.bottom <= app.screen.query_one("#prompt").region.y
            assert app.screen.query_one("#accounts").size.height >= 1
            await pilot.press("question_mark")
            await pilot.pause()
            assert not app.screen.keys_shown
            await pilot.press("question_mark", "escape")  # esc closes it too
            await pilot.pause()
            assert not app.screen.keys_shown

    async def test_reset_clock_appears_once_on_the_page(self, tmp_path):
        """Countdowns in the table, reset clocks on the charts: never both."""
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            acc = fake._accounts[0]
            five = tui_data.iso_epoch(acc.usage.last_good["five_hour"]["resets_at"])
            clock = time.strftime("%H:%M", time.localtime(five))
            page = "\n".join([painted(app, "#status"), *(row_plain(app, n) for n in "123"),
                              painted(app, "#detail")])
            assert page.count(clock) == 1 and clock in painted(app, "#detail")
            assert "↻ 1h59m" in row_plain(app, "1") or "↻ 2h00m" in row_plain(app, "1")

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
            assert app.screen.query_one("#prompt").region.bottom <= size[1]
            assert row_plain(app, "1").startswith("›● #1 ")  # the number logs use, always
            if size[0] >= 100:  # at 80 the number takes the model column's room
                assert "Fable  34%" in row_plain(app, "1")

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
            assert "me@x.com" in first and "62%" in first
            assert "1h59m" in first or "2h00m" in first  # the 5h reset at any width
            assert len(first) <= size[0]
            assert prompt_plain(app) == "⏎ switch · a add · m manual · $ costs · ? keys"  # whole at any width
            # too few rows for readable charts: hidden, never squeezed
            assert app.screen.query_one("#prompt").region.bottom == size[1]
            assert not braille(painted(app, "#detail"))

    async def test_no_history_says_so_instead_of_guessing(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "~" not in row_plain(app, "1") and "?" not in row_plain(app, "1")
            detail = painted(app, "#detail")
            # measured pcts still get a chart each, with a dash for the estimate
            assert detail.count("· —") == 2 and "?" not in detail
            assert "62%" not in detail  # the pct is the table's

    async def test_synthetic_history_gives_forecast_and_charts(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            first = row_plain(app, "1")
            # 62% at 15%/h → 90% in ~1h52m, before the 2h reset
            assert "~5h limit in 2h" in first, first
            assert "%/h" not in first
            title = painted(app, "#detail").splitlines()[1]
            assert title.startswith("5h · est. ~limit in 2h") and "7d · est. ~" in title
            assert "% at reset" in title
            # account 2 has no history: no forecast, rather than a guess
            assert "~" not in row_plain(app, "2") and "?" not in row_plain(app, "2")

    async def test_switch_moves_the_forecast_to_the_account_on_duty(self, tmp_path):
        """Account 1 burned until the switch, account 2 was idle until then
        and burns since: after the switch each row (and chart) shows its
        own history, not the previous duty's burn."""
        now = time.time()
        switch_at = now - 1800
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com"),
                make_account(2, entry=make_entry(32.0, 44.0), email="work@y.com"),
            ],
            tmp_path,
        )
        with_history(fake, {
            "1": {
                "5h": linear_points(now=switch_at, start_pct=32.0, rate_pct_per_h=15.0,
                                    span_s=7200, step_s=300, resets_at=now + 7200),
                "7d": linear_points(now=switch_at, start_pct=21.4, rate_pct_per_h=0.4,
                                    span_s=86400, step_s=3600, resets_at=now + 3 * 86400),
            },
            "2": {
                "5h": linear_points(now=switch_at, start_pct=12.0, rate_pct_per_h=0.0,
                                    span_s=7200, step_s=600, resets_at=now + 7200)
                + linear_points(now=now, start_pct=12.0, rate_pct_per_h=40.0,
                                span_s=1800, step_s=300, resets_at=now + 7200)[1:],
                "7d": linear_points(now=now, start_pct=44.0, rate_pct_per_h=0.0,
                                    span_s=86400, step_s=3600, resets_at=now + 3 * 86400),
            },
        })
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "~5h limit in" in row_plain(app, "1")  # on duty, burning
            await pilot.press("down", "enter")
            await settle(pilot)
            assert ("switch_to", "2") in fake_calls(app)
            # what every switch stamps (engine and manual alike)
            (tmp_path / "autoswitch_state.json").write_text(
                json.dumps({"lastSwitchAt": switch_at})
            )
            app._tick()
            await settle(pilot)
            one, two = app.estimates["1"], app.estimates["2"]
            assert all(w.rate_pct_per_h is None for w in one.windows.values())
            assert two.windows["5h"].rate_pct_per_h == pytest.approx(40.0, rel=0.05)
            # Its 7d has one reading since the switch: the user's weekly
            # burn carries over from the previous duty (account 1's 0.4%/h).
            assert two.windows["7d"].rate_pct_per_h == pytest.approx(0.4, rel=0.05)
            assert "~" not in row_plain(app, "1")  # its burn left with the duty
            assert "~5h limit in" in row_plain(app, "2"), row_plain(app, "2")
            title = painted(app, "#detail").splitlines()[1]
            assert title.startswith("5h · est. ~limit in"), title
            await pilot.press("up")
            await pilot.pause()
            assert "· —" in painted(app, "#detail")  # account 1: no projection

    @pytest.mark.parametrize("elsewhere", [True, False])
    async def test_idle_account_projects_only_its_recent_outside_use(self, tmp_path, elsewhere):
        """The owner's screen an hour after a switch #2 -> #1: #1 on duty
        and flat (5h 0%, 7d 89%); #2 burned 4%/h of 7d while on duty here.
        Since the switch #2 either rose 1%/h (used on another machine) or
        stayed flat. Its on-duty burn never projects; outside use does, and
        says so."""
        now = time.time()
        switch_at = now - 3600
        week = now + 3 * 86400
        after_rate = 1.0 if elsewhere else 0.0
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(0.0, 89.0), email="me@x.com"),
                make_account(2, entry=make_entry(13.0, 32.0 + after_rate), email="work@y.com"),
            ],
            tmp_path,
        )
        with_history(fake, {
            "1": {"7d": linear_points(now=now, start_pct=89.0, rate_pct_per_h=0.0,
                                      span_s=3 * 3600, step_s=600, resets_at=week)},
            "2": {"7d": linear_points(now=switch_at, start_pct=24.0, rate_pct_per_h=4.0,
                                      span_s=2 * 3600, step_s=600, resets_at=week)
                  + linear_points(now=now, start_pct=32.0, rate_pct_per_h=after_rate,
                                  span_s=3600, step_s=600, resets_at=week)[1:]},
        })
        (tmp_path / "autoswitch_state.json").write_text(json.dumps({"lastSwitchAt": switch_at}))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert row_plain(app, "1").endswith("ok"), row_plain(app, "1")
            rate = app.estimates["2"].windows["7d"].rate_pct_per_h
            assert rate == pytest.approx(after_rate, abs=0.05)  # never the 4%/h
            await pilot.press("down")
            await pilot.pause()
            head = painted(app, "#detail").splitlines()[0]
            if elsewhere:
                # The row itself says whose use this is, not only the chart.
                assert "elsewhere ~7d in" in row_plain(app, "2"), row_plain(app, "2")
                assert "used elsewhere" in head
                assert app.screen.summary("2").endswith("used elsewhere")
            else:
                assert "~" not in row_plain(app, "2")
                assert row_plain(app, "2").endswith("ok"), row_plain(app, "2")
                assert "used elsewhere" not in head

    async def test_cursor_on_duty_follows_a_switch_it_did_not_make(self, tmp_path):
        """An engine switch (or another rota's) moves the account on duty
        under the page: a cursor that sat on it follows, so the charts
        show the account now in use."""
        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.screen.selected_number == "1"
            fake.switch_to("2")  # not through the page
            app._tick()
            await settle(pilot)
            assert app.screen.selected_number == "2"
            assert "work@y.com" in painted(app, "#detail").splitlines()[0]

    async def test_cursor_moved_elsewhere_stays_on_a_switch(self, tmp_path):
        fake = three_accounts(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "down")  # the user looks at #3
            assert app.screen.selected_number == "3"
            fake.switch_to("2")
            app._tick()
            await settle(pilot)
            assert app.screen.selected_number == "3"

    async def test_highlight_moves_detail(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "~limit in 2h" in painted(app, "#detail")  # account 1 has history
            await pilot.press("down")
            await pilot.pause()
            assert "· —" in painted(app, "#detail")  # account 2 has none
            await pilot.press("k")
            await pilot.pause()
            assert "~limit in 2h" in painted(app, "#detail")

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
            assert "~" not in row_plain(app, "1")

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
            assert row_plain(app, "2").startswith("›●")

    async def test_d_toggles_disabled(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("d")
            await settle(pilot)
            assert ("set_disabled", "1", True) in fake_calls(app)
            assert "off" in row_plain(app, "1")
            await pilot.press("d")
            await settle(pilot)
            assert ("set_disabled", "1", False) in fake_calls(app)

    async def test_x_asks_on_the_prompt_line_then_removes(self, tmp_path):
        from claude_rotater.tui.main import AccountRow, MainScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "x")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)  # asked in place, no popup
            assert prompt_plain(app).startswith("remove work@y.com")
            assert "y remove  n no  esc" in prompt_plain(app)
            await pilot.press("n")
            await settle(pilot)
            assert not any(c[0] == "remove" for c in fake_calls(app))
            assert prompt_plain(app) == "⏎ switch · a add · m manual · $ costs · ? keys"
            await pilot.press("delete")
            await pilot.pause()
            await pilot.press("escape")  # esc cancels as well
            await settle(pilot)
            assert not any(c[0] == "remove" for c in fake_calls(app))
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("d", "a", "q")  # other keys are swallowed while it asks
            await pilot.pause()
            assert app.is_running and not any(c[0] == "set_disabled" for c in fake_calls(app))
            await pilot.press("y")
            await settle(pilot)
            assert ("remove", "2", True) in fake_calls(app)
            await settle(pilot)
            assert [r.number for r in app.screen.query(AccountRow)] == ["1", "3"]
            assert app.focused is app.screen.query_one("#accounts")  # the keys go back

    async def test_a_offers_the_ways_and_adds_the_current_login(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            assert prompt_plain(app) == "add:  b browser  c current  t token  k API key  esc"
            await pilot.press("escape")
            await pilot.pause()
            assert fake_calls(app) == [] and not app.prompt.is_open
            await pilot.press("a", "c")  # picked by key: no second question
            await settle(pilot)
            assert ("add", None, False) in fake_calls(app)
            assert not app.prompt.is_open

    async def test_add_chooser_leads_to_the_token_entry(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("a", "t")
            await pilot.pause()
            assert prompt_plain(app).startswith("token: ▌")

    async def test_t_reads_token_label_and_slot_inline(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("t")
            await pilot.pause()
            assert prompt_plain(app).startswith("token: ▌")
            await pilot.press(*"sk-ant-oat-x")
            assert "sk-ant" not in prompt_plain(app)  # a secret is drawn as dots
            assert prompt_plain(app).startswith("token: ••••••••••••")
            await pilot.press("enter")
            await pilot.pause()
            assert prompt_plain(app).startswith("email label: ▌")
            await pilot.press("enter")  # skipped
            await pilot.pause()
            assert prompt_plain(app).startswith("slot: ▌")
            await pilot.press("x", "enter")  # not a number: asked again, with why
            await pilot.pause()
            assert prompt_plain(app).startswith("slot: ▌") and "not a slot" in prompt_plain(app)
            assert not any(c[0] == "add_token" for c in fake_calls(app))
            await pilot.press("7", "enter")
            await settle(pilot)
            assert ("add_token", "sk-ant-oat-x", None, 7, True) in fake_calls(app)

    async def test_a_k_reads_an_api_key_masked_then_a_budget(self, tmp_path):
        from claude_rotater import api_keys

        key = "sk-ant-api03-" + "feedface00" * 4
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("a", "k")
            await pilot.pause()
            assert prompt_plain(app).startswith("API key: ▌")
            await pilot.press(*key)
            assert "sk-ant" not in prompt_plain(app) and key[-4:] not in prompt_plain(app)
            await pilot.press("enter")
            await pilot.pause()
            assert prompt_plain(app).startswith("budget: ▌")
            await pilot.press(*"lots", "enter")  # asked again, with why
            await pilot.pause()
            assert prompt_plain(app).startswith("budget: ▌") and "not an amount" in prompt_plain(app)
            await pilot.press(*"100/m", "enter")
            await settle(pilot)
            assert ("add_token", key, None, None, True) in fake_calls(app)
            assert api_keys.state_of(tmp_path, "9").budget == api_keys.Budget("monthly", 100.0)

    async def test_a_k_refuses_what_is_not_an_api_key(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("a", "k", *"sk-ant-oat01-xyz", "enter")
            await settle(pilot)
            assert "not an API key" in prompt_plain(app)
            assert not any(c[0] == "add_token" for c in fake_calls(app))

    async def test_b_sets_a_budget_on_a_key_row_only(self, tmp_path):
        from claude_rotater import api_keys

        fake = three_accounts(tmp_path)
        key = dataclasses.replace(
            make_account(4, kind="api_key", entry=make_usage_at(None, sentinel=USAGE_API_KEY)),
            key_tail="AbCd", key_state=api_keys.KeyState(),
        )
        fake._accounts.append(key)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("b")  # on #1, a subscription
            await pilot.pause()
            assert "API key's row" in prompt_plain(app)
            await pilot.press("down", "down", "down", "b")
            await pilot.pause()
            assert prompt_plain(app).startswith("#4 budget: ▌") and "now none" in prompt_plain(app)
            await pilot.press(*"$25", "enter")
            await settle(pilot)
            state = api_keys.state_of(tmp_path, "4")
            assert state.budget is not None and state.budget.amount == 25.0

    async def test_enter_on_an_exhausted_key_retries_it(self, tmp_path):
        from claude_rotater import api_keys

        fake = three_accounts(tmp_path)
        api_keys.mark_exhausted(tmp_path, "4", api_keys.OUT_OF_CREDIT, 1.0)
        key = dataclasses.replace(
            make_account(4, kind="api_key", entry=make_usage_at(None, sentinel=USAGE_API_KEY)),
            key_state=api_keys.state_of(tmp_path, "4"),
        )
        fake._accounts.append(key)
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "down", "down", "enter")
            await settle(pilot)
            assert ("switch_to", "4") in fake_calls(app)
            assert not api_keys.state_of(tmp_path, "4").exhausted

    async def test_a_key_on_duty_has_its_spend_priced_now_and_then(self, tmp_path):
        from claude_rotater import api_keys

        fake = three_accounts(tmp_path)
        fake._accounts = [dataclasses.replace(a, is_active=False) for a in fake._accounts]
        fake._accounts.append(dataclasses.replace(
            make_account(4, active=True, kind="api_key",
                         entry=make_usage_at(None, sentinel=USAGE_API_KEY)),
            key_state=api_keys.KeyState(),
        ))
        fake.active = "4"
        priced: list = []

        class Source:
            def refresh(self, identities, force=False):
                priced.append(sorted(identities))

        app = make_app(fake)
        app._cost_source = Source()
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app._tick()
            await settle(pilot)
            app._tick()  # within KEY_SPEND_EVERY_S: not again
            await settle(pilot)
            assert priced == [["1", "2", "3", "4"]]
            app._key_spend_at -= app.KEY_SPEND_EVERY_S
            app._tick()
            await settle(pilot)
            assert len(priced) == 2

    async def test_no_spend_pricing_without_a_key(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        app._cost_source = object()  # any use would raise
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app._tick()
            await settle(pilot)
            assert not [w for w in app.workers if w.group == "key-spend"]

    async def test_a_key_on_duty_says_what_keeps_it_from_the_chats(self, tmp_path, monkeypatch):
        from claude_rotater import api_key_mode

        key = make_account(4, active=True, kind="api_key")
        snap = AccountsSnapshot(active_number="4", accounts=(key,), taken_at=0.0)
        app = make_app(three_accounts(tmp_path))
        monkeypatch.setattr(api_key_mode, "overrides", lambda cfg: ["ANTHROPIC_API_KEY is set"])
        assert app._key_note_for(snap) == (
            "Claude Code ignores rota's API key here: ANTHROPIC_API_KEY is set"
        )
        monkeypatch.setattr(api_key_mode, "overrides", lambda cfg: [])
        monkeypatch.setattr(api_key_mode, "shared_with", lambda cfg: [])
        assert app._key_note_for(snap) == ""
        idle = AccountsSnapshot(active_number=None, accounts=(make_account(1),), taken_at=0.0)
        assert app._key_note_for(idle) == ""

    async def test_keys_behind_a_shared_settings_file_point_at_the_split(
        self, tmp_path, monkeypatch
    ):
        from claude_rotater import api_key_mode

        key = make_account(4, kind="api_key")
        snap = AccountsSnapshot(active_number="1", accounts=(make_account(1, active=True), key),
                                taken_at=0.0)
        app = make_app(three_accounts(tmp_path))
        monkeypatch.setattr(api_key_mode, "shared_with", lambda cfg: ["/x/.claude"])
        note = app._key_note_for(snap)
        assert note.startswith("keys need their own settings for ")
        assert note.endswith("— press S to split")
        no_keys = AccountsSnapshot(active_number="1", accounts=(make_account(1, active=True),),
                                   taken_at=0.0)
        assert app._key_note_for(no_keys) == ""

    async def test_split_asks_first_and_only_on_yes(self, tmp_path, monkeypatch):
        from claude_rotater import api_key_mode

        calls: list = []
        monkeypatch.setattr(api_key_mode, "shared_with", lambda cfg: ["/x/.claude"])
        monkeypatch.setattr(api_key_mode, "split_settings", lambda cfg, data: calls.append(cfg))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("S")
            await pilot.pause()
            assert prompt_plain(app).startswith("copy settings.json into ")
            await pilot.press("n")
            await settle(pilot)
            assert calls == []
            await pilot.press("S")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert len(calls) == 1

    async def test_token_paste_and_overwrite_question(self, tmp_path):
        from textual import events

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("t")
            await pilot.pause()
            app.prompt.post_message(events.Paste("sk-ant-api-" + "z" * 40 + "\n"))
            await pilot.pause()
            assert "51 chars" in prompt_plain(app)  # a long secret gives its length
            await pilot.press("enter", *"w@x.com", "enter", "2", "enter")
            await pilot.pause()
            assert prompt_plain(app).startswith("slot 2 holds work@y.com: overwrite?")
            await pilot.press("n")
            await settle(pilot)
            assert not any(c[0] == "add_token" for c in fake_calls(app))

    async def test_esc_drops_the_token_entry(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("t", *"abc", "enter", "escape")
            await settle(pilot)
            assert not app.prompt.is_open and fake_calls(app) == []
            await pilot.press("t", "enter")  # nothing typed: said so, nothing added
            await pilot.pause()
            assert "no token" in prompt_plain(app) and fake_calls(app) == []

    async def test_action_failure_is_said_inline(self, tmp_path):
        from claude_rotater.exceptions import ClaudeSwitchError

        fake = three_accounts(tmp_path)

        def refuse(number, **_kw):
            raise ClaudeSwitchError("account 2 is busy")

        fake.switch_to = refuse
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "enter")
            await settle(pilot)
            assert prompt_plain(app) == "switch to account 2 failed: account 2 is busy"
            assert app.prompt.message is not None and not app.busy

    async def test_ctrl_c_and_q_quit_without_asking(self, tmp_path):
        for keys in (("q",), ("t", "ctrl+c")):  # ctrl+c even mid-entry
            app = make_app(three_accounts(tmp_path))
            async with app.run_test(size=(120, 40)) as pilot:
                await settle(pilot)
                await pilot.press(*keys)
                await pilot.pause()
                assert not app.is_running, keys

    async def test_c_shows_config_in_place_and_back(self, tmp_path):
        from claude_rotater.tui.main import MainScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            screen = app.screen
            await pilot.press("c")
            await pilot.pause()
            assert app.screen is screen and isinstance(screen, MainScreen)  # no pushed screen
            assert screen.config_shown and not screen.query_one("#body").display
            assert screen.config.backup_dir == tmp_path
            assert app.focused is screen.config.settings_list
            assert "esc back" in prompt_plain(app)
            await pilot.press("a", "x", "d")  # account keys stand down on the settings
            await pilot.pause()
            assert not app.prompt.is_open and fake_calls(app) == []
            await pilot.press("escape")
            await pilot.pause()
            assert not screen.config_shown and screen.query_one("#body").display
            assert app.focused is screen.query_one("#accounts")
            assert prompt_plain(app) == "⏎ switch · a add · m manual · $ costs · ? keys"
            await pilot.press("c", "c")  # c toggles back as well
            await pilot.pause()
            assert not screen.config_shown


def test_no_modal_screens_remain():
    """Every question and result lives on the prompt line: no ModalScreen
    anywhere in the TUI package, and no stock Header/Footer/Button use."""
    import importlib
    import inspect
    import pkgutil

    from textual.screen import ModalScreen

    import claude_rotater.tui as tui

    for info in pkgutil.iter_modules(tui.__path__):
        module = importlib.import_module(f"claude_rotater.tui.{info.name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            assert not issubclass(obj, ModalScreen), obj
        for stock in ("ModalScreen", "Footer", "Header", "Button"):
            assert not hasattr(module, stock), (info.name, stock)  # never imported
        assert ".notify(" not in inspect.getsource(module), info.name  # no toasts


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
            assert not app.prompt.is_open

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
            assert "re-login" in row_plain(app, "2")
            await pilot.press("down", "l")
            await settle(pilot)
            assert FakeStage.instances[0].events[0] == ("authenticate", "dead@y.com")

    async def test_login_from_the_add_chooser(self, tmp_path, monkeypatch):
        app, _ = login_app(three_accounts(tmp_path), monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("b")
            await settle(pilot)
            assert FakeStage.instances and ("login_import",) in fake_calls(app)

    async def test_l_says_it_is_logging_in_at_once(self, tmp_path, monkeypatch):
        app, _ = login_app(three_accounts(tmp_path), monkeypatch, suspend_ok=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            said: list[str] = []
            say = app.say
            app.say = lambda text, role="info": (said.append(text), say(text, role))
            await pilot.press("l")
            await settle(pilot)
            assert said and said[0] == "logging in an account…", said
            assert "real terminal" in prompt_plain(app)  # then why it could not

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
            assert prompt_plain(app) == "log in failed: is the active account"

    @staticmethod
    def _rota_lines(caplog) -> list[str]:
        lines = [r.getMessage() for r in caplog.records if r.name == "rota"]
        for line in lines:  # rota.log never holds an email or a token
            assert "@" not in line and "sk-ant-" not in line, line
        return lines

    async def test_login_attempt_and_success_are_logged(self, tmp_path, monkeypatch, caplog):
        caplog.set_level("INFO", logger="rota")
        app, _ = login_app(three_accounts(tmp_path), monkeypatch)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
        lines = self._rota_lines(caplog)
        assert "Login started: any account" in lines
        assert "Login finished" in lines

    async def test_relogin_attempt_is_logged_by_slot(self, tmp_path, monkeypatch, caplog):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

        caplog.set_level("INFO", logger="rota")
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
            await pilot.press("down", "l")
            await settle(pilot)
        assert "Login started: re-login of slot 2" in self._rota_lines(caplog)

    @pytest.mark.parametrize("cancelled", [True, False], ids=["cancel", "failure"])
    async def test_authenticate_failure_is_logged(self, tmp_path, monkeypatch, caplog, cancelled):
        from claude_rotater.exceptions import LoginCancelled, LoginError

        caplog.set_level("INFO", logger="rota")
        exc = (LoginCancelled("Login cancelled") if cancelled
               else LoginError("no login for who@x.com"))
        app, _ = login_app(three_accounts(tmp_path), monkeypatch, fail=exc)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
        expected = ("Login failed: LoginCancelled: Login cancelled" if cancelled
                    else "Login failed: LoginError: no login for <email>")
        assert expected in self._rota_lines(caplog)

    async def test_no_terminal_is_logged(self, tmp_path, monkeypatch, caplog):
        caplog.set_level("INFO", logger="rota")
        app, _ = login_app(three_accounts(tmp_path), monkeypatch, suspend_ok=False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
        assert any(
            m.startswith("Login failed: SuspendNotSupported")
            for m in self._rota_lines(caplog)
        )

    async def test_import_failure_is_logged(self, tmp_path, monkeypatch, caplog):
        from claude_rotater.exceptions import ValidationError

        caplog.set_level("INFO", logger="rota")
        app, _ = login_app(three_accounts(tmp_path), monkeypatch)

        def boom(self, switcher, **_kw):
            raise ValidationError("me@x.com token sk-ant-oat01-abc rejected")

        monkeypatch.setattr(FakeStage, "import_into", boom)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("l")
            await settle(pilot)
        lines = self._rota_lines(caplog)
        assert "Login failed: ValidationError: <email> token <token> rejected" in lines
        assert "Action failed: Log in: <email> token <token> rejected" in lines
        assert "Login finished" not in lines

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
            (2.5 * 86400, 3, "login ends 2d"),
            (5 * 3600 + 330, 3, "login ends 5h05m"),
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
        layout = RowLayout.for_width(120, ("5h", "7d"), tag_need=13)
        text = account_row_text(acc, None, layout, now=time.time(), threshold=90)
        assert "login ends 1d" in text.plain and "personal" not in text.plain

    def test_row_without_warning_has_no_personal_tag(self):
        from claude_rotater.tui.main import RowLayout, account_row_text

        layout = RowLayout.for_width(120, ("5h", "7d"), tag_need=8)
        text = account_row_text(self._acc(30 * 86400), None, layout, now=time.time(), threshold=90)
        assert "personal" not in text.plain and "login" not in text.plain

    def test_dead_login_tag_beats_a_lapsing_one(self):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED
        from claude_rotater.tui.main import row_tag

        dead = dataclasses.replace(self._acc(3600), usage=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED))
        assert row_tag(dead, time.time(), 3) == ("re-login", True)
        assert row_tag(self._acc(3600), time.time(), 3)[0].startswith("login ends ")
        assert row_tag(self._acc(30 * 86400), time.time(), 3) == ("", False)


@pytest.mark.asyncio
class TestRefresherHosting:
    @pytest.fixture(autouse=True)
    def _opted_in(self, tmp_path):
        from claude_rotater.settings import set_setting

        set_setting(tmp_path, "accounts.proactiveTokenRefresh", "true")

    async def test_not_started_in_a_view_only_window(self, tmp_path, fake_refresher, fake_engine):
        from claude_rotater.instance import EngineLock

        other = EngineLock(tmp_path)
        assert other.acquire() is None
        try:
            app = make_app(three_accounts(tmp_path))
            async with app.run_test(size=(120, 40)) as pilot:
                await settle(pilot)
                assert fake_refresher.instances == []
        finally:
            other.release()

    async def test_not_started_without_the_opt_in(self, tmp_path, fake_refresher):
        from claude_rotater.settings import unset_setting

        unset_setting(tmp_path, "accounts.proactiveTokenRefresh")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert fake_refresher.instances == []

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
            assert "login ends" not in row_plain(app, "2")
            set_setting(tmp_path, "ui.loginWarnDays", "7")
            app.on_settings_changed("ui.loginWarnDays")
            await pilot.pause()
            assert app.login_warn_days == 7
            assert "login ends 4d" in row_plain(app, "2") or "login ends 5d" in row_plain(app, "2")


    async def test_header_names_a_dead_login_and_the_key(self, tmp_path):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

        dead = dataclasses.replace(make_entry(), sentinel=USAGE_RELOGIN_REQUIRED)
        fake = FakeSwitcher(
            [make_account(1, email="me@x.com"), make_account(3, active=True, email="w@y.com", entry=dead)],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            assert "#3 login was revoked (signed in elsewhere?) — press l to log in again" in painted(app, "#status")

    async def test_header_warns_of_a_login_ending_within_the_warn_days(self, tmp_path):
        fake = FakeSwitcher(
            [
                make_account(1, active=True, email="me@x.com"),
                dataclasses.replace(make_account(3, email="w@y.com"), login_expires_at=time.time() + 2 * 86400 + 600),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(pilot)
            assert "login ends in 2d: #3 — press l" in painted(app, "#status")


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
            assert app.auto_status.last_switch == "2 → 1" and app.auto_status.last_reason == ""
            assert "switched" not in painted(app, "#status")  # the log says it once
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
            assert app.auto_status.last_check_at is not None and app.auto_status.next_check_at is not None
            assert "checked" not in status and "next" not in status  # routine: not shown
            # the startup cooldown NoSwitchEvent is recorded, but the account on
            # duty is under its threshold, so nothing is held: no note
            assert app.auto_status.last_reason == "cooldown"
            assert "cooldown" not in status
            assert "no switch" not in log_plain(app)
            assert "sleeping" not in log_plain(app)

    @pytest.mark.parametrize("cols", [60, 120])
    async def test_header_shows_the_latest_verdict(self, tmp_path, fake_engine, cols):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(cols, 30)) as pilot:
            await settle(pilot)
            engine = fake_engine.instances[0]
            await emit(engine, NoSwitchEvent(reason="stay", detail="stay: resets soonest (19h40m), 37% headroom"))
            await settle(pilot)
            first = app.auto_status.decision
            status = painted(app, "#status")
            assert "auto on · stay: resets" in status and len(status) <= cols
            if cols >= 120:
                assert "stay: resets soonest (19h40m), 37% headroom" in status
            else:
                assert "…" in status
            # a machine outcome keeps the verdict; a repeat keeps the stored one
            await emit(engine, NoSwitchEvent(reason="below-threshold"))
            await emit(engine, NoSwitchEvent(reason="stay", detail="stay: resets soonest (19h40m), 37% headroom"))
            await settle(pilot)
            assert app.auto_status.decision is first
            assert "no switch" not in painted(app, "#status")

    @pytest.mark.parametrize("cols", [60, 120])
    async def test_switch_flashes_on_the_prompt_line_and_heads_the_header(self, tmp_path, fake_engine, cols):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(cols, 30)) as pilot:
            await settle(pilot)
            engine = fake_engine.instances[0]
            await emit(engine, SwitchEvent(
                trigger="expiring-first",
                from_ref={"number": 1, "email": "me@x.com"},
                to_ref={"number": 2, "email": "work@y.com"},
                reason="switch → #2: #1's 7d resets in 6d, #2's in 19h; use #2 first",
            ))
            await settle(pilot)
            prompt = prompt_plain(app)
            assert prompt.startswith("→ work@y.com: #1's 7d") and len(prompt) <= cols
            status = painted(app, "#status")
            assert "auto on · switch → #2" in status and len(status) <= cols
            assert app.prompt._timer is not None  # brief: it clears itself

    async def test_second_rota_on_a_data_dir_is_view_only_then_takes_over(self, tmp_path, fake_engine):
        from claude_rotater.instance import EngineLock

        other = EngineLock(tmp_path)
        assert other.acquire() is None  # another rota runs the engine here
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(160, 30)) as pilot:
            await settle(pilot)
            assert fake_engine.instances == []  # no second engine
            status = painted(app, "#status")
            assert "auto view-only" in status
            # Said once on the prompt line; the header keeps the verdict's
            # place for the engine's own (it runs the same code).
            assert f"another rota is running (pid {os.getpid()}" in prompt_plain(app)
            other.release()
            app._engine_claimed_at = 0.0  # the retry interval has passed
            app._tick()
            await settle(pilot)
            assert len(fake_engine.instances) == 1
            assert "auto on" in painted(app, "#status")
        assert EngineLock(tmp_path).acquire() is None  # released on exit

    async def test_view_only_window_never_starts_an_engine_on_a_settings_change(
        self, tmp_path, fake_engine
    ):
        from claude_rotater.instance import EngineLock

        other = EngineLock(tmp_path)
        assert other.acquire() is None
        try:
            app = make_app(three_accounts(tmp_path))
            async with app.run_test(size=(160, 30)) as pilot:
                await settle(pilot)
                app.on_settings_changed("autoswitch.threshold")
                await settle(pilot)
                assert fake_engine.instances == []
                assert app.engine is None
        finally:
            other.release()

    async def test_view_only_window_shows_what_the_engine_shows(self, tmp_path, fake_engine):
        """The engine's window and a view-only one on the same data dir
        render the same rows (forecasts included) and the same verdict: the
        viewer reads the headline the engine published, and computes the
        forecast from the same files."""
        from claude_rotater.autoswitch import DECISION_FILENAME, headline
        from claude_rotater.instance import EngineLock

        now = time.time()
        event = NoSwitchEvent(
            reason="stay", detail="stay: most 7d to spend: #1's week resets in 3d with 57% left (needs 0.8%/h)",
        )

        def world() -> FakeSwitcher:
            fake = three_accounts(tmp_path)
            with_history(fake, burn_history(now))
            return fake

        engine_app = make_app(world())
        async with engine_app.run_test(size=(160, 30)) as pilot:
            await settle(pilot)
            await emit(fake_engine.instances[0], event)
            await settle(pilot)
            engine_rows = [row_plain(engine_app, n) for n in ("1", "2")]
            engine_status = painted(engine_app, "#status")
            # What the real engine writes for the event (AutoSwitchEngine._emit).
            (tmp_path / DECISION_FILENAME).write_text(json.dumps(headline(event).to_json()))
        lock = EngineLock(tmp_path)
        assert lock.acquire() is None  # the engine keeps running beside the viewer

        fake_engine.instances = []
        try:
            viewer = make_app(world())
            async with viewer.run_test(size=(160, 30)) as pilot:
                await settle(pilot)
                viewer._tick()
                await settle(pilot)
                assert fake_engine.instances == []
                assert [row_plain(viewer, n) for n in ("1", "2")] == engine_rows
                assert "~5h limit in" in engine_rows[0]
                status = painted(viewer, "#status")
                assert "auto view-only" in status
                line = "stay: most 7d to spend: #1's week resets in 3d"
                assert line in engine_status and line in status
        finally:
            lock.release()

    async def test_view_only_window_beside_older_code_says_so(self, tmp_path, fake_engine, monkeypatch):
        from claude_rotater.instance import EngineLock, Holder

        monkeypatch.setattr(EngineLock, "acquire", lambda self: Holder(pid=4242, legacy=True))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(160, 30)) as pilot:
            await settle(pilot)
            assert "another rota is running (pid 4242, older code)" in painted(app, "#status")

    async def test_auto_pause_shows_in_header_and_on_the_prompt_once(self, tmp_path, fake_engine):
        line = "auto paused: too many switches (4 in the last hour); resumes in 41m"
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 30)) as pilot:
            await settle(pilot)
            engine = fake_engine.instances[0]
            said = []
            real_say = app.say
            app.say = lambda text, *a, **k: (said.append(text), real_say(text, *a, **k))
            for _ in range(3):  # the engine repeats it every tick
                await emit(engine, NoSwitchEvent(reason="auto-paused", detail=line))
            await settle(pilot)
            assert line in painted(app, "#status")
            assert said == [line]  # said once, not per tick

    async def test_no_headroom_warns_on_the_prompt_once(self, tmp_path, fake_engine):
        line = "stay: no account with headroom; #1 is about to hit its limit"
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(160, 30)) as pilot:
            await settle(pilot)
            engine = fake_engine.instances[0]
            said = []
            real_say = app.say
            app.say = lambda text, *a, **k: (said.append(text), real_say(text, *a, **k))
            for _ in range(3):  # the engine repeats it every tick
                await emit(engine, NoSwitchEvent(reason="no-headroom", detail=line))
            await settle(pilot)
            assert "no account with headroom" in painted(app, "#status")
            assert said == [line]

    async def test_relogin_stop_shows_in_the_header(self, tmp_path, fake_engine):
        line = ("stop: account 1's live login belongs to another account and its "
                "own stored login has no refresh token; re-login needed (press l).")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(200, 30)) as pilot:
            await settle(pilot)
            await emit(fake_engine.instances[0], NoSwitchEvent(reason="relogin-needed", detail=line))
            await settle(pilot)
            assert "re-login needed" in painted(app, "#status")

    async def test_header_says_why_a_switch_is_held(self, tmp_path):
        """At its threshold with a cooldown running, the header says the
        switch is held and for how long; under it, it says nothing."""
        (tmp_path / "autoswitch_state.json").write_text(json.dumps({"lastSwitchAt": time.time() - 30}))
        fake = FakeSwitcher(
            [make_account(1, active=True, entry=make_entry(95.0, 31.0)), make_account(2, entry=make_entry(5.0, 5.0))],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "switch held: cooldown 4m" in painted(app, "#status")

    async def test_cooldown_from_shared_state_file(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text(
            json.dumps({"lastSwitchAt": time.time() - 30})
        )
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.auto_status.last_switch_at is not None
            assert app.auto_status.last_switch_at + app.auto_status.cooldown_s - time.time() > 200

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
            assert app.auto_status.last_switch_at is None
            state.write_text(json.dumps({"lastSwitchAt": time.time() - 30}))
            app._tick()
            await settle(pilot)
            assert app.auto_status.last_switch_at is not None

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

    async def test_threshold_edited_on_config_page_restarts_engine(
        self, tmp_path, fake_engine
    ):
        """End to end: `c`, type a threshold, enter; the real ConfigView's
        SettingChanged message reaches the app and the engine restarts."""
        from claude_rotater.tui.config import ConfigView

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            config = app.screen.config
            assert config.settings_list.spec.dotted == "autoswitch.threshold"
            await pilot.press("8", "1", "enter")
            await pilot.pause(ConfigView.NOTIFY_DELAY_S + 0.2)
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped
            assert fake_engine.instances[1].settings.threshold == 81.0
            assert app.threshold_pct == 81.0

    async def test_holding_right_on_config_restarts_engine_once(self, tmp_path, fake_engine):
        """Every step is written, but the engine restarts once the keys stop."""
        from claude_rotater.settings import load_settings
        from claude_rotater.tui.config import ConfigView

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            await pilot.press(*["right"] * 5)
            assert load_settings(tmp_path).threshold == 95.0
            await pilot.pause(ConfigView.NOTIFY_DELAY_S + 0.2)
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

            app.on_config_view_setting_changed(Msg())
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


def braille(text: str) -> bool:
    return any(0x2800 <= ord(ch) <= 0x28FF for ch in text)


class TestChartLines:
    """The selected account's 5h and 7d charts under the table."""

    def _lines(self, width=120, height=20, *, series=None, entry=None, est="auto", threshold=90.0):
        from claude_rotater.estimator import estimate_account
        from claude_rotater.tui.main import chart_lines

        now = time.time()
        series = burn_history(now)["1"] if series is None else series
        if est == "auto":
            est = estimate_account(series, models=(), threshold=threshold, now=now)
        acc = make_account(1, active=True, entry=entry or make_entry(62.0, 31.0), email="me@x.com")
        return chart_lines(acc, est, series, now=now, threshold=threshold, width=width, height=height)

    def test_side_by_side_when_wide(self):
        lines = self._lines(120)
        assert lines[0].plain.startswith("› #1 me@x.com") and "on duty" not in lines[0].plain
        assert has_color(lines[0], "me@x.com", Palette.DARK.accent)  # on duty: the accent name
        assert lines[1].plain.startswith("5h") and "7d" in lines[1].plain
        assert all(len(line.plain) <= 120 for line in lines)

    def test_off_account_title_says_off_like_its_row(self):
        from claude_rotater.tui.main import account_title

        title = account_title(make_account(3, disabled=True, email="old@z.com"), Palette.DARK).plain
        assert "off" in title and "disabled" not in title

    def test_stacked_when_narrow(self):
        lines = [line.plain for line in self._lines(80, 19)]
        titles = [i for i, line in enumerate(lines) if line.startswith(("5h", "7d"))]
        assert len(titles) == 2 and titles[1] > titles[0] + 3
        assert all(len(line) <= 80 for line in lines)

    def test_threshold_reset_now_and_projection(self):
        from claude_rotater.tui.plot import RESET_GLYPH, THRESHOLD_GLYPH

        lines = self._lines(120)
        body = "\n".join(line.plain for line in lines)
        assert THRESHOLD_GLYPH in body and RESET_GLYPH in body and "now" in body
        # the 5h projection crosses 90% before the reset: red dots right of now,
        # drawn in their own glyph, never braille like the measured curve
        from claude_rotater.tui.plot import NOW_GLYPH, PROJECTION_GLYPH

        half = (120 - 3) // 2
        now_col = lines[-2].plain.index(NOW_GLYPH, 4)
        right = braille_cells(lines[2:-1], now_col + 1, half, glyph=PROJECTION_GLYPH)
        assert right and all(SEV_CRIT in st and "dim" not in st for _c, st in right)
        assert not braille_cells(lines[2:-1], now_col + 2, half)
        assert braille_cells(lines[2:-1], 0, now_col)  # the measured curve

    def test_titles_carry_only_the_forecast(self):
        title = self._lines(120)[1]
        five = title.plain.split("7d")[0]
        assert "est. ~limit in 2h" in five  # 62% at 15%/h, 90% before the 2h reset
        assert has_color(title, "~limit in 2h", SEV_CRIT)
        assert "62%" not in title.plain and "31%" not in title.plain  # pcts are in the table
        assert ":" not in title.plain  # the reset clock is the chart's right edge

    def test_stays_under_shows_where_it_lands(self):
        now = time.time()
        series = burn_history(now, rate=2.0)["1"]
        title = self._lines(120, series=series)[1]
        assert "% at reset" in title.plain and has_color(title, "% at reset", SEV_OK)

    def test_thin_history_draws_no_projection(self):
        lines = self._lines(120, series={}, est=None)
        title = lines[1].plain
        assert title.count("· —") == 2 and "?" not in title and "est." not in title
        from claude_rotater.tui.plot import NOW_GLYPH

        now_col = lines[-2].plain.index(NOW_GLYPH, 4)
        from claude_rotater.tui.plot import PROJECTION_GLYPH

        assert not braille_cells(lines[2:-1], now_col + 1, (120 - 3) // 2)  # nothing right of now
        assert not braille_cells(lines[2:-1], now_col + 1, (120 - 3) // 2, glyph=PROJECTION_GLYPH)

    def test_unused_and_at_limit_titles(self):
        title = self._lines(120, series={}, est=None, entry=make_entry(0.0, 95.0))[1].plain
        assert "5h · unused" in title and "7d · at limit" in title and "~" not in title

    def test_hidden_below_the_minimum_and_filling_above(self):
        from claude_rotater.tui.main import chart_min_rows

        side = chart_min_rows(120)
        assert side == 10 and chart_min_rows(80) == 19
        assert self._lines(120, side - 1) == []
        assert len(self._lines(120, side)) == side
        assert len(self._lines(120, 30)) == 30 and len(self._lines(160, 44)) == 44  # no cap
        assert len(self._lines(120, 12)) == 12  # fills what is spare
        assert self._lines(80, 18) == [] and len(self._lines(80, 19)) == 19  # stacked

    def test_dead_login_has_no_charts(self):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

        assert self._lines(120, entry=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), est=None, series={}) == []


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
            assert "~limit" not in detail  # the long-term view replaces the charts
            assert "4 windows · 1 past 90%" in detail  # 3 past + the running one
            assert app.long_term["1"].peaks_5h
            # the page still fits: the key strip stays on screen
            assert app.screen.query_one("#prompt").region.bottom <= 40
            await pilot.press("h")
            await settle(pilot)
            assert not app.long_view
            detail = painted(app, "#detail")
            assert "5h peaks" not in detail and "~limit in 2h" in detail

    async def test_history_title_and_esc_back(self, tmp_path):
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("h")
            await settle(pilot)
            title = painted(app, "#detail").splitlines()[0]
            assert title.startswith("history  #1 me@x.com") and title.rstrip().endswith("h/esc back")
            assert "for the charts" not in title
            await pilot.press("escape")
            await settle(pilot)
            assert not app.long_view
            await pilot.press("escape")  # on the charts esc does nothing
            await settle(pilot)
            assert not app.long_view

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

    async def test_header_shows_statusline_off_without_a_feed(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "○ statusline off (s)" in painted(app, "#status")

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
            assert "● live" in painted(app, "#status")

    async def test_s_confirms_then_installs(self, tmp_path, monkeypatch):
        calls = self._calls(monkeypatch, self._status("not-installed", "off"))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s")
            await pilot.pause()
            assert prompt_plain(app).startswith("feed usage to rota from Claude Code's statusline")
            assert "y set up  n no" in prompt_plain(app)
            await pilot.press("n")
            await settle(pilot)
            assert calls == []
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            assert calls == [("install", tmp_path)]

    async def test_s_when_installed_offers_removal(self, tmp_path, monkeypatch):
        calls = self._calls(monkeypatch, self._status("installed", "stale"))
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "◐ feed: no data yet" in painted(app, "#status")
            await pilot.press("s")
            await pilot.pause()
            assert prompt_plain(app).startswith("remove rota from Claude Code's statusline")
            await pilot.press("y")
            await settle(pilot)
            assert calls == [("uninstall", tmp_path)]

    async def test_install_failure_is_shown_not_raised(self, tmp_path):
        settings = self._settings()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{not json")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("s", "y")
            await settle(pilot)
            shown = app.prompt.message or ""
            assert shown.startswith("set up statusline feed failed: Claude Code's settings were not changed")
            assert "not valid JSON" in shown
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
            assert "◐ feed: no data yet" in painted(app, "#status")
            await pilot.press("s", "y")
            await settle(pilot)
            assert not app.feed_installed and app.feed_state == "off"
        assert "statusLine" not in json.loads(self._settings().read_text())

    async def test_foreign_statusline_is_wrapped_and_restored(self, tmp_path):
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
            assert "rota wraps it" in prompt_plain(app)
            await pilot.press("y")
            await settle(pilot)
            assert app.feed_install == "installed"
            await pilot.press("s", "y")
            await settle(pilot)
        assert json.loads(settings.read_text()) == original


@pytest.mark.asyncio
class TestWidths:
    @pytest.mark.parametrize("theme_name", ["dark", "light"])
    @pytest.mark.parametrize("size", [(60, 20), (80, 24), (120, 40), (160, 50)])
    async def test_clean_page_at_every_width_and_theme(self, tmp_path, size, theme_name):
        """The cleanup holds everywhere: no legend, no ~?, no 'on duty', the
        page inside the terminal, the key strip on the bottom row."""
        from claude_rotater.settings import set_setting
        from claude_rotater.tui.main import AccountRow

        set_setting(tmp_path, "ui.theme", theme_name)
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            assert app.current_theme.name == f"rota-{theme_name}"
            strips = app.screen._compositor.render_strips()
            page = ["".join(seg.text for seg in strip).rstrip() for strip in strips]
            text = "\n".join(page)
            assert len(page) == size[1] and all(len(line) <= size[0] for line in page)
            for gone in ("~ = estimate", "~?", "on duty", "not enough data", "disabled"):
                assert gone not in text, gone
            assert page[-1].strip() == "⏎ switch · a add · m manual · $ costs · ? keys"
            assert all(row.region.height == 1 for row in app.screen.query(AccountRow))

    @pytest.mark.parametrize("size", [(60, 20), (80, 24), (120, 40), (160, 50)])
    async def test_every_account_is_one_line_at_any_width(self, tmp_path, size):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED
        from claude_rotater.tui.main import AccountRow

        org = dataclasses.replace(
            make_account(2, entry=make_entry(12.0, 44.0, scoped=[("Fable", 3.0)]),
                         email="work.account@example-company.com"),
            org_name="Acme Corporation Worldwide", org_uuid="o",
        )
        fake = FakeSwitcher(
            [
                make_account(1, active=True, entry=make_entry(62.0, 31.0, scoped=[("Fable", 34.0)]),
                             email="me@x.com"),
                org,
                make_account(3, entry=make_entry(91.0, 85.0), email="solo@z.com"),
                make_account(4, disabled=True, entry=UsageEntry(), email="old@z.com"),
                make_account(5, entry=UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED), email="dead@z.com"),
                make_account(6, entry=make_entry(1.0, 0.0), email="brandnew@example.com"),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            for row in app.screen.query(AccountRow):
                assert row.region.height == 1
                assert len(row_plain(app, row.number)) <= row.region.width
            work = row_plain(app, "2")
            assert "work.account@" in work  # the local part survives any width
            assert "62%" in row_plain(app, "1") and "31%" in row_plain(app, "1")
            assert "↻" in row_plain(app, "1")  # the 5h reset, even at 60
            assert "re-login" in row_plain(app, "5") and "off" in row_plain(app, "4")
            if size[0] <= 60:
                assert "Acme" not in work  # the org goes before the name shrinks

    @pytest.mark.parametrize("size", [(120, 40), (160, 50)])
    @pytest.mark.parametrize("view", ["charts", "history"])
    async def test_large_terminal_has_no_dead_space_under_the_graphs(self, tmp_path, size, view):
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            if view == "history":
                await pilot.press("h")
                await settle(pilot)
            detail = app.screen.query_one("#detail")
            actions = app.screen.query_one("#prompt")
            assert braille(painted(app, "#detail")) or "█" in painted(app, "#detail")
            assert actions.region.y - detail.region.bottom <= 1  # graphs reach the key strip

    @pytest.mark.parametrize("size", [(80, 30), (120, 40), (160, 50)])
    async def test_main_page_fits_with_table_and_charts(self, tmp_path, size):
        fake = three_accounts(tmp_path)
        with_history(fake, long_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            width = size[0]
            status = painted(app, "#status")
            assert len(status) <= width and "rota" in status and "estimate" not in status
            for number in ("1", "2", "3"):
                assert len(row_plain(app, number)) <= width - 1
            first = row_plain(app, "1")
            assert "62%" in first and "31%" in first and ("1h59m" in first or "2h00m" in first)
            assert "~5h" in first
            detail = painted(app, "#detail").splitlines()
            assert braille("".join(detail))
            assert all(len(line) <= width for line in detail)
            assert app.screen.query_one("#prompt").region.bottom <= size[1]

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
            assert "Fable  34%" in row_plain(app, "1")
            assert "Fable" not in row_plain(app, "2")  # no Fable window: blank, not 0%
        unused = FakeSwitcher(
            [make_account(1, active=True, entry=make_entry(62.0, 31.0, scoped=[("Fable", 0.0)]))], tmp_path
        )
        plain = make_app(unused)
        async with plain.run_test(size=size) as pilot:
            await settle(pilot)
            assert "Fable" not in row_plain(plain, "1")  # unused and not gating: no column

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
            assert title.startswith("settings")
            strips = app.screen._compositor.render_strips()
            page = ["".join(seg.text for seg in strip).rstrip() for strip in strips]
            assert page[0].strip().startswith("rota") and "auto on" in page[0]  # the same header
            assert page[-1].strip().endswith("esc back")  # the hint follows the view
            assert all(len(line) <= size[0] for line in page)
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


@pytest.mark.asyncio
class TestModeToggle:
    """``m`` flips auto/manual, saved in the profile's settings; the header
    names the mode and the limit guard; a click on the label does the same."""

    async def test_m_flips_the_mode_and_back(self, tmp_path):
        from claude_rotater.settings import load_settings

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "auto on" in painted(app, "#status")
            await pilot.press("m")
            await settle(pilot)
            assert load_settings(tmp_path).mode == "manual"
            assert FakeEngine.instances[-1].settings.mode == "manual"
            assert FakeEngine.instances[0].stopped  # restarted on the new mode
            status = painted(app, "#status")
            assert "manual · limit guard" in status and "auto on" not in status
            assert "steps in only at a hard limit" in prompt_plain(app)
            assert app.screen.prompt.hint.plain == "⏎ switch · a add · m auto · $ costs · ? keys"
            await pilot.press("m")
            await settle(pilot)
            assert load_settings(tmp_path).mode == "auto"
            assert "auto on" in painted(app, "#status")

    async def test_manual_without_the_guard_says_so(self, tmp_path):
        from claude_rotater.settings import set_setting

        set_setting(tmp_path, "autoswitch.mode", "manual")
        set_setting(tmp_path, "autoswitch.manualLimitGuard", "false")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            status = painted(app, "#status")
            assert "manual" in status and "limit guard" not in status
            await pilot.press("m")
            await settle(pilot)
            await pilot.press("m")
            await settle(pilot)
            assert "rota never switches" in prompt_plain(app)

    async def test_the_manual_verdict_shows_in_the_header(self, tmp_path):
        from claude_rotater.settings import set_setting

        set_setting(tmp_path, "autoswitch.mode", "manual")
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(pilot)
            await emit(FakeEngine.instances[-1], NoSwitchEvent(
                reason="manual",
                detail="stay: you pick the account; the guard moves only at a hard limit",
            ))
            await settle(pilot)
            assert (
                "manual · limit guard · stay: you pick the account; the guard "
                "moves only at a hard limit"
            ) in painted(app, "#status")

    async def test_clicking_the_mode_label_toggles(self, tmp_path):
        from claude_rotater.settings import load_settings

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            x = painted(app, "#status").index("auto on") + 1  # + the band's padding
            await pilot.click("#status", offset=(x + 1, 0))
            await settle(pilot)
            assert load_settings(tmp_path).mode == "manual"
            await pilot.click("#status", offset=(1, 0))  # the brand chip: nothing
            await settle(pilot)
            assert load_settings(tmp_path).mode == "manual"

    async def test_m_is_listed_with_the_keys(self, tmp_path):
        import re

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert re.search(r"(^|   )m +auto / manual", painted(app, "#detail"), re.M)

    async def test_m_stands_down_on_the_settings_page(self, tmp_path):
        from claude_rotater.settings import load_settings

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c", "m")
            await settle(pilot)
            assert load_settings(tmp_path).mode == "auto"


def test_summary_line_is_plain_text():
    import re

    from claude_rotater.tui.main import summary_line

    now = time.time()
    acc = make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com")
    line = summary_line(acc, None, threshold=90.0, now=now)
    assert re.fullmatch(
        r"me@x\.com \(active\) · 5h 62% resets (2h|1h59m) · 7d 31% resets (3d|2d23h)", line
    ), line
    assert summary_line(
        make_account(2, entry=make_entry(95.0, 10.0)), None, threshold=90.0, now=now
    ).endswith("· 5h at limit")
    assert summary_line(
        make_account(3, disabled=True, entry=UsageEntry()), None, threshold=90.0, now=now
    ) == "user3@example.com (off) · no reading yet"
    assert summary_line(
        make_account(4, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED)), None,
        threshold=90.0, now=now,
    ) == "user4@example.com · re-login"


@pytest.mark.asyncio
class TestCopy:
    """``y`` copies the selected row, ``Y`` the table, as plain text; the
    clipboard command is stubbed, OSC 52 is the fallback."""

    @pytest.fixture
    def copied(self, monkeypatch):
        got: list[str] = []
        monkeypatch.setattr(
            "claude_rotater.clipboard.system_copy", lambda text: got.append(text) or True
        )
        return got

    async def test_y_copies_the_selected_row(self, tmp_path, copied):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("down", "y")
            await settle(pilot)
            assert len(copied) == 1
            assert copied[0].startswith("work@y.com · 5h 12% resets ")
            assert "7d 44% resets " in copied[0] and "\n" not in copied[0]
            assert not set(copied[0]) & set("█▏▕↻›●")  # no bars or glyphs
            assert prompt_plain(app) == "copied the row"

    async def test_shift_y_copies_the_table(self, tmp_path, copied):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("Y")
            await settle(pilot)
            lines = copied[0].splitlines()
            assert [line.split(" · ")[0] for line in lines] == [
                "me@x.com (active)", "work@y.com", "old@z.com (off)",
            ]
            assert prompt_plain(app) == "copied the table"

    async def test_falls_back_to_osc52(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_rotater.clipboard.system_copy", lambda text: False)
        app = make_app(three_accounts(tmp_path))
        sent: list[str] = []
        monkeypatch.setattr(app, "copy_to_clipboard", sent.append)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("y")
            await settle(pilot)
            assert sent and sent[0].startswith("me@x.com (active) · ")
            assert prompt_plain(app) == "copied the row (via the terminal)"

    async def test_copy_keys_are_listed(self, tmp_path, copied):
        import re

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert re.search(r"(^|   )y / Y +copy row / table", painted(app, "#detail"), re.M)

    async def test_nothing_to_copy_with_no_accounts(self, tmp_path, copied):
        app = make_app(FakeSwitcher([], tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("y", "Y")
            await settle(pilot)
            assert copied == []


def test_elsewhere_forecast_words():
    from claude_rotater.tui.main import Forecast, elsewhere_forecast

    crit = Forecast("~7d limit in 15h", "~7d in 15h", "crit", True)
    assert elsewhere_forecast(crit) == Forecast("elsewhere ~7d in 15h", "~7d in 15h", "crit", True)
    days = Forecast("~7d limit in 2d23h", "~7d in 2d23h", "crit", True)
    assert elsewhere_forecast(days).text == "elsewhere ~7d in 2d23h"
    wide = Forecast("~Fable limit in 15h", "~Fable in 15h", "crit", True)
    assert elsewhere_forecast(wide).text == "~Fable limit in 15h"  # would not fit
    assert elsewhere_forecast(Forecast("ok", "ok", "ok", False)).text == "used elsewhere"
    assert elsewhere_forecast(None).short == "elsewhere"


class TestOfflineIsNotADeadLogin:
    """A network outage reads as "offline", and a lapsed access token as
    routine: on Oct 29 a two-hour DNS outage showed a red "token expired" on
    the account on duty in both profiles, and the owner logged in again to
    two logins that were fine (the CLI refreshed both the minute the network
    came back)."""

    def _failed(self, number, *, reached, tried, error="network", active=False):
        entry = UsageEntry(
            last_good={"five_hour": {"pct": 40.0, "resets_at": _iso_in(7200)}},
            fetched_at=reached, age_s=None if reached is None else time.time() - reached,
            last_attempt_at=tried, last_error=error,
        )
        return make_account(number, active=active, entry=entry)

    def test_expired_access_token_is_a_calm_tag(self):
        from claude_rotater.json_output import USAGE_TOKEN_EXPIRED
        from claude_rotater.tui.main import status_tag

        acc = make_account(1, active=True, entry=make_entry(sentinel=USAGE_TOKEN_EXPIRED))
        assert status_tag(acc, time.time(), 3) == ("refreshes on use", False)

    def test_dead_login_tag_is_still_a_warning(self):
        from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED
        from claude_rotater.tui.main import status_tag

        acc = make_account(1, entry=make_entry(sentinel=USAGE_RELOGIN_REQUIRED))
        assert status_tag(acc, time.time(), 3) == ("re-login", True)

    def test_every_fetch_failing_on_the_network_is_offline_since_the_last_reading(self):
        from claude_rotater.tui.main import offline_since

        now = time.time()
        accs = [self._failed(1, reached=now - 3600, tried=now - 60, active=True),
                self._failed(2, reached=now - 4000, tried=now - 300, error="timeout")]
        assert offline_since(accs, now) == now - 3600

    def test_a_reading_after_the_failure_means_online(self):
        from claude_rotater.tui.main import offline_since

        now = time.time()
        accs = [self._failed(1, reached=now - 3600, tried=now - 600),
                make_account(2, entry=make_usage_at(now - 30))]
        assert offline_since(accs, now) is None

    @pytest.mark.parametrize("error", ["http-401", "http-429", "bad-response", None])
    def test_other_failures_are_not_offline(self, error):
        from claude_rotater.tui.main import offline_since

        now = time.time()
        assert offline_since([self._failed(1, reached=now - 3600, tried=now - 60, error=error)], now) is None

    def test_an_old_failure_says_nothing_about_now(self):
        from claude_rotater.tui.main import OFFLINE_RECENT_S, offline_since

        now = time.time()
        accs = [self._failed(1, reached=now - 2 * OFFLINE_RECENT_S, tried=now - OFFLINE_RECENT_S - 60)]
        assert offline_since(accs, now) is None

    def test_disabled_and_key_accounts_do_not_count(self):
        from claude_rotater.tui.main import offline_since

        now = time.time()
        off = dataclasses.replace(self._failed(1, reached=now - 3600, tried=now - 60), disabled=True)
        key = dataclasses.replace(self._failed(2, reached=now - 3600, tried=now - 60), kind="api_key")
        assert offline_since([off, key], now) is None

    def test_never_reached_is_plain_offline(self):
        from claude_rotater.tui.main import freshness_label, offline_since

        now = time.time()
        reached = offline_since([self._failed(1, reached=None, tried=now - 60)], now)
        assert reached == 0.0
        assert freshness_label("idle", None, now=now, late_after=1200, offline=reached) == (
            "○", "sev_warn", "offline")

    def test_header_says_offline_since_and_not_the_age(self):
        from claude_rotater.tui.main import status_text

        now = time.time()
        reached = now - 3600
        clock = time.strftime("%H:%M", time.localtime(reached))
        from claude_rotater.tui.main import AutoStatus

        head = status_text(AutoStatus(running=True), now=now, width=120, feed="live",
                           polled_at=reached, offline=reached).plain
        assert head.rstrip().endswith(f"○ offline since {clock}")
        assert "live" not in head and "ago" not in head and "login" not in head

    def test_replay_oct_29_outage(self):
        """Both accounts' last good reading at 18:30, every poll since then
        failing with "network", the CLI's access token lapsed at 19:30: the
        row is calm, the header says offline, no login alert fires."""
        from claude_rotater import login_alerts
        from claude_rotater.json_output import USAGE_TOKEN_EXPIRED
        from claude_rotater.tui.main import offline_since, status_tag

        now = time.time()
        active = make_account(4, active=True, entry=UsageEntry(
            sentinel=USAGE_TOKEN_EXPIRED, fetched_at=now - 7800, last_attempt_at=now - 3700,
            last_error="network"))
        spare = self._failed(1, reached=now - 7500, tried=now - 120)
        accs = [active, spare]
        assert offline_since(accs, now) == now - 7500
        assert status_tag(active, now, 3) == ("refreshes on use", False)
        assert login_alerts.collect(login_alerts.states_from_snapshot(accs), now, 3) == []
