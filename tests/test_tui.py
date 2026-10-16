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

from claude_swap.autoswitch import NoSwitchEvent, PollEvent, SleepEvent, SwitchEvent
from claude_swap.json_output import USAGE_API_KEY, USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui import data as tui_data
from claude_swap.usage_store import UsageEntry

sys.path.insert(0, str(Path(__file__).parent))
from _fake_history import FakeHistory, linear_points  # noqa: E402


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


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
    monkeypatch.setattr("claude_swap.tui.app.default_engine_factory", FakeEngine)
    return FakeEngine


def make_app(fake: FakeSwitcher):
    from claude_swap.tui.app import CswapApp

    return CswapApp(fake)


async def settle(pilot) -> None:
    """Let thread workers finish and their UI updates apply.

    The (fake) auto engine worker deliberately runs until the app stops it,
    so waiting on it would block; wait on everything else.
    """
    app = pilot.app
    pending = [w for w in app.workers if w.group != "engine"]
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

    def test_sentinel_labels_match_cswap_list(self):
        # The TUI must describe sentinel states with the exact wording `cswap
        # list` prints — owned-and-expired means Claude Code refreshes the
        # active account, not that the user must re-login.
        assert (
            tui_data.sentinel_label(USAGE_TOKEN_EXPIRED)
            == "token expired — refresh deferred this pass; retries automatically"
        )
        from claude_swap.switcher import SENTINEL_NOTES

        for sentinel, note in SENTINEL_NOTES.items():
            assert tui_data.sentinel_label(sentinel) == note
        assert tui_data.sentinel_label("unknown state") == "unknown state"

    def test_sentinel_card_shows_last_seen_like_cswap_list(self):
        # A sentinel is a live overlay — the entry can still carry the last
        # good measurement, and `cswap list` prints it as a "last seen" line.
        # The card must too (except for API-key accounts, which have no quota).
        from claude_swap.tui.widgets import account_card_text

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
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette
        from claude_swap.tui.widgets import account_card_text

        acc = make_account(1, active=True, entry=make_entry(pct5=95.0))
        text = account_card_text(acc, 100, palette=Palette.from_theme(CSWAP_LIGHT))
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
        # reservation), so every take is the same on-demand pass `cswap list`
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
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0, pct7=None)  # annual plan: no 7d window
        labels = [label for label, *_ in usage_rows(entry.last_good, time.time())]
        assert labels == ["5h"]

    def test_scoped_models_and_over_limit_marker(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(scoped=[("Fable", 100.0), ("Opus", 12.0)])
        rows = usage_rows(entry.last_good, time.time())
        labels = [label for label, *_ in rows]
        assert labels == ["5h", "7d", "Fable", "Opus"]
        fable = next(row for row in rows if row[0] == "Fable")
        assert "(!)" in fable[2]
        # the marker stays terminal in the clock-extended variant too
        assert fable[3].endswith("(!)") and " · " in fable[3]

    def test_spend_row_first_with_amounts(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(spend={"used": 12.5, "limit": 50.0, "pct": 25.0, "currency": "USD"})
        rows = usage_rows(entry.last_good, time.time())
        assert rows[0][0] == "$$"
        assert "$12.50 / $50.00" in rows[0][2]

    def test_suffix_full_extends_countdown_with_clock(self):
        from claude_swap.tui.widgets import usage_rows

        entry = make_entry(pct5=47.0)
        row5 = usage_rows(entry.last_good, time.time())[0]
        assert row5[2].startswith("resets ")
        assert row5[3].startswith(row5[2] + " · ")

    def test_spend_clock_sits_with_reset_not_after_amounts(self):
        from claude_swap.tui.widgets import usage_rows

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
        from claude_swap.tui.widgets import usage_rows

        assert usage_rows(None, time.time()) == []
        assert usage_rows({}, time.time()) == []

    def test_seven_day_ahead_of_pace_marker(self):
        # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]
        assert "(ahead of pace)" in row[3]

    def test_five_hour_never_shows_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"five_hour": {"pct": 90.0, "resets_at": _iso_in(3600 * 4)}}
        row = usage_rows(last_good, now, now)[0]
        assert "pace" not in row[2]

    def test_scoped_ahead_of_pace_marker(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(ahead of pace)" in row[2]

    def test_maxed_scoped_marker_wins_over_pace(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso_in(86400 * 6)}]}
        row = usage_rows(last_good, now, now)[0]
        assert "(!)" in row[2]
        assert "ahead of pace" not in row[2]

    def test_no_pace_marker_without_fetched_at(self):
        from claude_swap.tui.widgets import usage_rows

        now = time.time()
        last_good = {"seven_day": {"pct": 50.0, "resets_at": _iso_in(86400 * 6)}}
        row = usage_rows(last_good, now)[0]
        assert "pace" not in row[2]

    def test_card_shows_clock_only_where_it_fits(self):
        # Per-row degradation: the wide card shows every clock, a mid width
        # keeps 5h/7d clocks while the longer spend row falls back to its
        # countdown, and a narrow card is exactly the old countdown-only look.
        from claude_swap.tui.widgets import account_card_text

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

    def _layout(self, width: int = 120, windows=("5h", "7d")):
        from claude_swap.tui.main import RowLayout

        return RowLayout.for_width(width, windows)

    def _row(self, acc, est=None, *, width=120, threshold=90.0, now=None, models=()):
        from claude_swap.tui.main import account_row_text

        return account_row_text(
            acc,
            est,
            self._layout(width),
            now=now if now is not None else time.time(),
            threshold=threshold,
            models=models,
        ).plain

    def test_active_marker_and_pcts(self):
        row = self._row(make_account(1, active=True, entry=make_entry(62.0, 31.0)))
        assert row.startswith("▸ 1 ")
        assert "62%" in row and "31%" in row
        assert "━" in row  # wide layout carries bars

    def test_inactive_has_no_marker(self):
        row = self._row(make_account(2, entry=make_entry(12.0, 44.0)))
        assert row.startswith("  2 ")

    def test_disabled_tag_replaces_org_tag(self):
        row = self._row(make_account(3, disabled=True))
        assert "disabled" in row and "[personal]" not in row

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

    def test_eta_burn_and_projection_from_estimate(self):
        from claude_swap.estimator import WindowEstimate, AccountEstimate

        now = time.time()
        # measured 60% 30 min ago at 10%/h: 65% projected now, 90% in 2.5h
        win = WindowEstimate("5h", 60.0, now - 1800, now + 4 * 3600, 10.0, "medium", ())
        est = AccountEstimate({"5h": win}, win, None, None, "medium")
        acc = make_account(1, active=True, entry=make_entry(60.0, 10.0))
        row = self._row(acc, est, now=now)
        assert "→65%" in row
        assert "+10.0%/h" in row
        assert "2h 30m ~" in row

    def test_projection_hidden_below_one_point(self):
        from claude_swap.estimator import WindowEstimate, AccountEstimate

        now = time.time()
        win = WindowEstimate("5h", 60.0, now - 60, now + 3600, 10.0, "high", ())
        est = AccountEstimate({"5h": win}, win, None, None, "high")
        row = self._row(make_account(1, entry=make_entry(60.0, 10.0)), est, now=now)
        assert "→" not in row

    def test_narrow_layout_drops_bars_keeps_name(self):
        acc = make_account(1, active=True, entry=make_entry(62.0, 31.0), email="me@x.com")
        row = self._row(acc, width=70)
        assert "me@x.com" in row and "62%" in row
        assert "━" not in row
        assert len(row) <= 70

    def test_scoped_window_column(self):
        entry = make_entry(10.0, 20.0, scoped=[("Fable", 55.0)])
        from claude_swap.tui.main import RowLayout, account_row_text

        layout = RowLayout.for_width(140, ("5h", "7d", "Fable"))
        row = account_row_text(
            make_account(1, entry=entry), None, layout,
            now=time.time(), threshold=90.0, models=("Fable",),
        ).plain
        assert "55%" in row

    def test_reset_column_uses_binding_window(self):
        row = self._row(make_account(1, entry=make_entry(80.0, 10.0)))
        assert row.rstrip().endswith(("1h 59m", "2h"))


class TestRowLayout:
    def test_wide_gets_full_bars(self):
        from claude_swap.tui.main import RowLayout

        layout = RowLayout.for_width(120, ("5h", "7d"))
        assert layout.bar_w == 10 and layout.show_burn and layout.show_reset

    def test_degrades_in_order(self):
        from claude_swap.tui.main import RowLayout

        widths = [RowLayout.for_width(w, ("5h", "7d")) for w in (120, 95, 80, 66, 50)]
        bars = [lay.bar_w for lay in widths]
        assert bars == sorted(bars, reverse=True)
        assert not widths[-1].show_burn and not widths[-1].show_reset

    def test_header_matches_row_columns(self):
        from claude_swap.tui.main import RowLayout, account_row_text, header_text

        layout = RowLayout.for_width(120, ("5h", "7d"))
        header = header_text(layout, threshold=90.0).plain
        row = account_row_text(
            make_account(1, entry=make_entry(62.0, 31.0)), None, layout,
            now=time.time(), threshold=90.0,
        ).plain
        col = header.index("7d")
        # the 7d bar starts exactly under its header label
        assert row[col] in "━╸─┃"
        assert "ETA→90%" in header


class TestStatusText:
    def _status(self, **kw):
        from claude_swap.tui.main import AutoStatus

        return AutoStatus(**kw)

    def _text(self, status, now=None, width=120):
        from claude_swap.tui.main import status_text

        return status_text(status, now=now or time.time(), width=width).plain

    def test_running_threshold_and_next_check(self):
        now = time.time()
        text = self._text(
            self._status(running=True, threshold=90.0, next_check_at=now + 80), now
        )
        assert "AUTO ●" in text and "thr 90%" in text and "next 1m" in text

    def test_stopped_engine_is_hollow(self):
        assert "AUTO ○" in self._text(self._status(running=False))

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
        from claude_swap.estimator import AccountEstimate, WindowEstimate

        now = time.time()
        win = WindowEstimate("5h", last, now, now + resets_in, rate, conf, ())
        return AccountEstimate({"5h": win}, win, None, None, conf), now

    @pytest.mark.parametrize("conf,glyph", [("high", ""), ("medium", "~"), ("low", "?")])
    def test_confidence_glyph(self, conf, glyph):
        from claude_swap.tui.main import eta_cell

        est, now = self._est(conf)
        text, _role = eta_cell(est, 90.0, now)
        assert text.startswith("4h") or text.startswith("3h")
        assert text.endswith(glyph) if glyph else text[-1].isalnum()

    def test_none_confidence_is_dash(self):
        from claude_swap.tui.main import eta_cell

        est, now = self._est("none", rate=None)
        assert eta_cell(est, 90.0, now) == ("—", "muted")

    def test_over_threshold_is_now(self):
        from claude_swap.tui.main import eta_cell

        est, now = self._est("high", last=95.0)
        assert eta_cell(est, 90.0, now)[0] == "now"

    def test_crossing_after_reset(self):
        from claude_swap.tui.main import eta_cell

        est, now = self._est("high", rate=1.0, resets_in=600)
        assert eta_cell(est, 90.0, now)[0] == "≥reset"

    def test_burn_cell(self):
        from claude_swap.tui.main import burn_cell

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
        from claude_swap.exceptions import ClaudeSwitchError

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
    from claude_swap.tui.main import AccountRow

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
            assert first.startswith("▸") and "me@x.com" in first and "62%" in first
            assert "\n" not in first
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
            assert "claude-swap" in status and "AUTO ●" in status
            assert "thr 90%" in status
            labels = [str(b.label) for b in app.screen.query(Button)]
            for word in ("switch", "add", "token", "disable", "remove", "config", "quit"):
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

    @pytest.mark.parametrize("size", [(80, 24), (60, 20)])
    async def test_narrow_terminal(self, tmp_path, size):
        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            first = row_plain(app, "1")
            assert "me@x" in first and "62%" in first
            assert "━" not in first  # bars dropped before names
            # every action chip stays on screen
            from textual.widgets import Button

            assert all(b.region.right <= size[0] for b in app.screen.query(Button))
            # the action bar and the plot pane still exist
            assert app.screen.query_one("#actions") is not None
            assert painted(app, "#detail")

    async def test_no_history_account_shows_card_and_dash_eta(self, tmp_path):
        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert "—" in row_plain(app, "1")
            detail = painted(app, "#detail")
            assert "no usage history yet" in detail
            assert "resets" in detail  # the full card stands in for the plot

    async def test_synthetic_history_gives_eta_and_plot(self, tmp_path):
        import re

        fake = three_accounts(tmp_path)
        with_history(fake, burn_history(time.time()))
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            first = row_plain(app, "1")
            # 62% at 12%/h → 90% in ~2h20m, before the 3h reset
            assert re.search(r"2h 1\dm|2h 20m", first), first
            assert "+12.0%/h" in first
            detail = painted(app, "#detail")
            assert "me@x.com · 5h" in detail
            assert any(0x2800 <= ord(ch) <= 0x28FF for ch in detail)  # braille plot
            assert " 7d " in detail and "resets" in detail  # other-window sparkline
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
            assert row_plain(app, "2").startswith("▸")

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
        from claude_swap.tui.modals import ConfirmModal

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
            from claude_swap.tui.main import AccountRow

            assert [r.number for r in app.screen.query(AccountRow)] == ["1", "3"]

    async def test_a_adds_current_login_after_confirm(self, tmp_path):
        from claude_swap.tui.modals import ConfirmModal, OutputModal

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

        from claude_swap.tui.modals import AddTokenModal

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
        from claude_swap.tui.config import ConfigScreen
        from claude_swap.tui.main import MainScreen

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

    from claude_swap.tui.app import CswapApp
    from textual.app import SuspendNotSupported

    FakeStage.instances = []
    app = CswapApp(fake, login_factory=lambda d: FakeStage(d, fail=fail))
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
        from claude_swap.tui.modals import OutputModal

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
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

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
        from claude_swap.exceptions import LoginCancelled, LoginError
        from claude_swap.tui.main import MainScreen

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
        from claude_swap.exceptions import ValidationError
        from claude_swap.tui.modals import OutputModal

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


def test_default_factory_builds_live_engine(monkeypatch, tmp_path):
    # Drop the autouse FakeEngine patch: this test checks the real factory.
    monkeypatch.undo()
    import claude_swap.autoswitch as autoswitch
    from claude_swap.tui.app import default_engine_factory
    from claude_swap.settings import AutoSwitchSettings

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

    async def test_settings_change_restarts_engine(self, tmp_path, fake_engine):
        from claude_swap.settings import set_setting

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
            assert "thr 80%" in painted(app, "#status")
            assert "ETA→80%" in painted(app, "#columns")

    async def test_threshold_edited_on_config_page_restarts_engine(
        self, tmp_path, fake_engine
    ):
        """End to end: `c`, type a threshold, submit; the real ConfigScreen's
        SettingChanged message reaches the app and the engine restarts."""
        from textual.widgets import Input

        from claude_swap.tui.config import ConfigScreen

        app = make_app(three_accounts(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            editor = screen.row("autoswitch.threshold").editor
            assert isinstance(editor, Input)
            editor.focus()
            editor.value = ""
            await pilot.press("8", "1", "enter")
            await settle(pilot)
            assert len(fake_engine.instances) == 2
            assert fake_engine.instances[0].stopped
            assert fake_engine.instances[1].settings.threshold == 81.0
            assert app.threshold_pct == 81.0

    @pytest.mark.parametrize("key", [
        "autoswitch.pollSpareSeconds",
        "autoswitch.pollHourlyBudget",
        "autoswitch.feedStaleSeconds",
    ])
    async def test_cadence_change_restarts_engine(self, tmp_path, fake_engine, key):
        """A cadence edit restarts the engine so its next sleep is planned
        with the new value instead of waiting out the old one."""
        from claude_swap.poll_policy import load_cadence
        from claude_swap.settings import set_setting

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
        from claude_swap.settings import set_setting

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
            assert app.theme == "cswap-dark"
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
            assert "AUTO ○" in painted(app, "#status")


class TestEventText:
    def test_switch_event_styling_and_content(self):
        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        from claude_swap.tui.main import event_text

        assert event.human() in event_text(event).plain

    def test_event_text_uses_light_accent_for_switch(self):
        from claude_swap.tui.main import event_text
        from claude_swap.tui.theme import ACCENT_LIGHT, CSWAP_LIGHT, Palette

        event = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@x.com"},
            to_ref={"number": 2, "email": "b@x.com"},
        )
        text = event_text(event, palette=Palette.from_theme(CSWAP_LIGHT))
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
        import claude_swap.cli as cli
        import claude_swap.tui as tui

        launched = {}

        def fake_run(switcher):
            launched["switcher"] = switcher
            return 0

        monkeypatch.setattr(sys, "argv", ["cswap"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(tui, "run", fake_run)
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert "switcher" in launched

    def test_bare_non_tty_keeps_usage_error(self, monkeypatch, temp_home):
        import claude_swap.cli as cli

        monkeypatch.setattr(sys, "argv", ["cswap"])
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
            assert app.theme == "cswap-light"

    async def test_auto_setting_uses_detected_light(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"

    async def test_auto_setting_no_detection_falls_back_to_dark(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "auto"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected=None)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"

    async def test_toggle_cycles_dark_light_auto(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        from claude_swap.tui.app import CswapApp
        app = CswapApp(fake, detected="light")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"          # setting dark
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → light
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-light"          # → auto, detected=light
            assert json.loads((tmp_path / "settings.json").read_text())["ui"]["theme"] == "auto"
            app.action_toggle_theme(); await pilot.pause()
            assert app.theme == "cswap-dark"           # → back to dark

    async def test_ctrl_t_cycles_theme(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "dark"}}))
        fake = FakeSwitcher([make_account("1", active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.theme == "cswap-light"
