"""Replays of the job profile's real failures (rota.log, 2026-10-17).

* Overnight, 01:49 to 09:50: accounts 1 and 3 swapped about 315 times at
  1-23% usage. A process running the pre-fix build wrote every switch into
  the default profile's Keychain item while reading the work profile's own,
  so the live login never changed: each tick saw "another account's login"
  under the marked account and failed over, forever.
* 09:55: a process on the fixed build moved 1 -> 2 as a failover while
  account 1 was healthy: its repair self-switch needed the profile endpoint,
  which was answering 429, so the repair silently did nothing and the tick
  fell through to failover, onto an account whose own fetch had just 429'd.

Both must now be impossible, whatever the write/read mismatch: a foreign
login is restored or stops the engine, it never moves to another account,
and the hourly switch budget bounds anything that slips through.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from claude_rotater.autoswitch import (
    PAUSED_REASON,
    REPAIR_FAILED_REASON,
    SWITCH_BUDGET_WINDOW_S,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_rotater.json_output import USAGE_FOREIGN_CREDENTIAL
from claude_rotater.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _entry_for, _usage
from tests.test_expiring_first import usage_at

MINUTE = 60.0


def _fleet(h: EngineHarness, live_owner: str) -> dict:
    """The job fleet at 1-23%: account 1's week resets soonest, so
    expiring-first wants to be on 1. The collector reports the active row as
    foreign whenever the live login is not the marked account's."""
    now = h.clock.now
    rows = {
        "1": usage_at(now, seven=5, reset_h=10, five=1),
        "2": usage_at(now, seven=23, reset_h=120, five=3),
        "3": usage_at(now, seven=12, reset_h=100, five=2),
    }
    current = h.switcher.current_account_number()
    if current != live_owner:
        rows[current] = USAGE_FOREIGN_CREDENTIAL
    return rows


def _automatic_writes_per_hour(times: list[float]) -> int:
    """The most live-login writes inside any trailing hour."""
    return max(
        (sum(1 for u in times if t - SWITCH_BUDGET_WINDOW_S < u <= t) for t in times),
        default=0,
    )


class TestOvernightFlapReplay:
    def _harness(self, temp_home, *, marked: int):
        h = EngineHarness(temp_home, strategy="expiring-first")
        for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            h.seed(n, email)
        h.make_live(f"{'abc'[marked - 1]}@example.com", marked)
        return h

    def test_writes_that_never_reach_the_live_read_cannot_flap(self, temp_home, caplog):
        """Live login is account 3's and stays so: every live write lands
        where the live read never looks (the pre-fix Keychain item mismatch).
        Eight hours of ticks: never a failover, never a move off the marked
        account, and at most the hourly budget of repair attempts."""
        h = self._harness(temp_home, marked=1)
        writes: list[float] = []
        real_restore = h.switcher.restore_own_login

        def restore(slot):
            writes.append(h.clock.now)
            return real_restore(slot)

        with (
            patch.object(h.switcher, "_write_credentials", lambda *_a, **_k: None),
            patch.object(h.switcher, "restore_own_login", side_effect=restore),
            caplog.at_level(logging.WARNING, logger="rota"),
        ):
            for _ in range(8 * 60):
                h.tick_with_usage(_fleet(h, live_owner="3"))
                h.clock.advance(MINUTE)

        moves = [e for e in h.events if isinstance(e, SwitchEvent)]
        assert moves == []  # no failover, no proactive move, no "successful" repair
        assert h.active_number() == 1
        assert 0 < _automatic_writes_per_hour(writes) <= h.settings.max_switches_per_hour
        reasons = {e.reason for e in h.events if isinstance(e, NoSwitchEvent)}
        assert {REPAIR_FAILED_REASON, PAUSED_REASON} <= reasons
        assert any("Auto-switch paused" in r.getMessage() for r in caplog.records)

    def test_a_proactive_move_into_a_broken_store_does_not_come_back(self, temp_home):
        """Start on account 3 with its own login live (healthy). Expiring-first
        moves to account 1 at most once; the move never shows up in the live
        store, and the engine stops instead of failing back to 3 (the
        overnight 3 -> 1 -> 3 loop)."""
        h = self._harness(temp_home, marked=3)
        with patch.object(h.switcher, "_write_credentials", lambda *_a, **_k: None):
            for _ in range(8 * 60):
                h.tick_with_usage(_fleet(h, live_owner="3"))
                h.clock.advance(MINUTE)
        moves = [e for e in h.events if isinstance(e, SwitchEvent)]
        assert len(moves) <= 1
        assert all(m.trigger == "expiring-first" for m in moves)
        assert not any(m.trigger == "failover" for m in moves)


class TestNineFiftyFiveFailoverReplay:
    def test_foreign_login_with_a_throttled_peer_is_repaired_not_failed_over(
        self, temp_home
    ):
        h = EngineHarness(temp_home, strategy="expiring-first")
        for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            h.seed(n, email)
        h.make_live("a@example.com", 1)  # live bytes are not account 1's backup
        now = h.clock.now
        throttled_2 = UsageEntry(
            last_good=_usage(5), fetched_at=now - 600, age_s=600.0,
            last_error="http-429", trust_extended=True,
        )
        entries = {
            "1": _entry_for(USAGE_FOREIGN_CREDENTIAL, now),
            "2": throttled_2,
            "3": _entry_for(usage_at(now, seven=12, reset_h=100), now),
        }
        # The profile endpoint is 429ing too: nothing may depend on it.
        with patch(
            "claude_rotater.oauth.fetch_oauth_profile", return_value=None
        ):
            outcome = h.tick_with_entries(entries)

        assert outcome is TickOutcome.SWITCHED
        moves = [e for e in h.events if isinstance(e, SwitchEvent)]
        assert [m.trigger for m in moves] == ["repair"]
        assert h.active_number() == 1
        assert h.switcher._live_matches_slot_backup("1", "a@example.com")


class TestHourlySwitchBudget:
    def _ping_pong(self, h: EngineHarness) -> dict:
        """Whatever is active is at its limit and the others are fresh."""
        current = str(h.active_number())
        return {n: _usage(100 if n == current else 10) for n in ("1", "2", "3")}

    def _harness(self, temp_home, **kw):
        h = EngineHarness(temp_home, **kw)
        for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
            h.seed(n, email)
        h.make_live("a@example.com", 1)
        return h

    def test_the_fifth_switch_in_an_hour_pauses_auto(self, temp_home, caplog):
        h = self._harness(temp_home)
        outcomes = []
        with caplog.at_level(logging.WARNING, logger="rota"):
            for _ in range(8):
                outcomes.append(h.tick_with_usage(self._ping_pong(h)))
                h.clock.advance(MINUTE)
        assert outcomes[:4] == [TickOutcome.SWITCHED] * 4
        assert set(outcomes[4:]) == {TickOutcome.BLOCKED}
        paused = [e for e in h.events
                  if isinstance(e, NoSwitchEvent) and e.reason == PAUSED_REASON]
        assert paused[-1].human().startswith("auto paused: too many switches (4 in the last hour)")
        assert h.engine.last_decision.line == paused[-1].detail
        logged = [r for r in caplog.records if "Auto-switch paused" in r.getMessage()]
        assert len(logged) == 1  # once per pause, not per tick

    def test_auto_resumes_once_the_hour_clears(self, temp_home):
        h = self._harness(temp_home)
        for _ in range(5):
            h.tick_with_usage(self._ping_pong(h))
            h.clock.advance(MINUTE)
        h.clock.advance(SWITCH_BUDGET_WINDOW_S)
        assert h.tick_with_usage(self._ping_pong(h)) is TickOutcome.SWITCHED

    def test_the_budget_is_a_setting_and_shared_through_the_state_file(self, temp_home):
        h = self._harness(temp_home, max_switches_per_hour=2)
        for _ in range(2):
            assert h.tick_with_usage(self._ping_pong(h)) is TickOutcome.SWITCHED
            h.clock.advance(MINUTE)
        # A second engine on the same data dir (or a restarted one) sees the
        # same spent budget.
        h.engine = h._make_engine()
        assert h.tick_with_usage(self._ping_pong(h)) is TickOutcome.BLOCKED
        assert h.events[-1].reason == PAUSED_REASON
