"""The engine's pre-emptive switch: move before the next reading would show
the account on duty over its switch point (preempt.py does the math)."""

from __future__ import annotations

import logging

import pytest

from claude_rotater.autoswitch import (
    NO_HEADROOM_REASON,
    PAUSED_REASON,
    PREEMPT_TRIGGER,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_rotater.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _iso_at

MIN = 60.0


def usage(h: EngineHarness, pct5: float, pct7: float = 10.0) -> dict:
    now = h.clock.now
    return {
        "five_hour": {"pct": pct5, "resets_at": _iso_at(now + 3 * 3600)},
        "seven_day": {"pct": pct7, "resets_at": _iso_at(now + 4 * 86400)},
    }


def make(temp_home, **kw) -> EngineHarness:
    kw.setdefault("threshold", 99.9)
    h = EngineHarness(temp_home, **kw)
    for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        h.seed(n, email)
    h.make_live("a@example.com", 1)
    h.switcher.usage_history.clock = h.clock
    return h


def duty(h: EngineHarness, readings: list[tuple[float, float]], slot: str = "1") -> None:
    """Record this account's readings ``(seconds_before_now, pct5)`` in its
    own history, the way the feed and the poller do."""
    email = h.switcher.account_email(slot)
    now = h.clock.now
    for ago, pct in readings:
        u = usage(h, pct)
        h.switcher.usage_history.record({slot: u}, {slot: (email, "")}, now - ago)


def entry(value: dict, now: float, **kw) -> UsageEntry:
    return UsageEntry(last_good=value, fetched_at=now, age_s=0.0, **kw)


def tick(h: EngineHarness, pcts: dict[str, float], **active_kw) -> TickOutcome:
    now = h.clock.now
    entries = {
        n: entry(usage(h, p), now, **(active_kw if n == "1" else {}))
        for n, p in pcts.items()
    }
    return h.tick_with_entries(entries)


# A feed every 30 s, steady, then one 3-point turn: 96 -> 99 on the last one.
TURN = [(150, 95.0), (120, 95.0), (90, 96.0), (60, 96.0), (30, 96.0), (0, 99.0)]


def switches(h):
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def reasons(h):
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


class TestPreempt:
    def test_switches_before_the_next_reading_would_be_over(self, temp_home, caplog):
        h = make(temp_home)
        duty(h, TURN)
        with caplog.at_level(logging.WARNING, logger="rota"):
            assert tick(h, {"1": 99.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        sw = switches(h)[-1]
        assert sw.trigger == PREEMPT_TRIGGER
        assert sw.to_ref["number"] == 2
        assert sw.reason.startswith("pre-empt: ~102.0% by next reading in ~1m (burn +")
        assert any("Auto-switch (pre-empt)" in r.getMessage() for r in caplog.records)

    def test_no_projection_over_the_line_stays(self, temp_home):
        h = make(temp_home)
        duty(h, [(150, 95.0), (120, 95.0), (90, 95.0), (60, 96.0), (30, 96.0), (0, 97.0)])
        assert tick(h, {"1": 97.0, "2": 20.0, "3": 40.0}) is TickOutcome.NO_ACTION
        assert reasons(h) == ["below-threshold"]

    def test_steady_burn_switches_once_one_gap_of_burn_reaches_the_line(self, temp_home):
        # 1%/min, a reading a minute: 98 -> projected 99, stay; 99 -> 100, go.
        h = make(temp_home)
        duty(h, [(i * MIN, 98.0 - i) for i in range(10, -1, -1)])
        assert tick(h, {"1": 98.0, "2": 20.0, "3": 40.0}) is TickOutcome.NO_ACTION
        h.clock.advance(MIN)
        duty(h, [(0, 99.0)])
        assert tick(h, {"1": 99.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == PREEMPT_TRIGGER

    def test_stale_feed_projects_over_the_poll_gap(self, temp_home):
        # Readings stopped 15 minutes ago at 97% and +1%/min: the next poll
        # (3 minutes, near the line) is the next reading; 97 + 3 > 99.9.
        h = make(temp_home)
        duty(h, [(15 * MIN + i * MIN, 97.0 - i) for i in range(10, -1, -1)])
        assert tick(h, {"1": 97.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        assert "in ~3m" in switches(h)[-1].reason

    @pytest.mark.parametrize("strategy", ["best", "expiring-first", "consume-first"])
    def test_every_strategy_bypassing_cooldown_and_manual_hold(self, temp_home, strategy):
        h = make(temp_home, strategy=strategy)
        h.engine._mutate_state(lambda st: st.update(
            lastSwitchAt=h.clock.now - 200.0,  # cooldown is 300 s
            lastManualSwitchAt=h.clock.now - 200.0,
        ))
        duty(h, TURN)
        assert tick(h, {"1": 99.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == PREEMPT_TRIGGER

    def test_hourly_cap_still_holds(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        h.engine._mutate_state(lambda st: st.update(
            autoSwitchTimes=[now - 3000, now - 2000, now - 1000, now - 900]
        ))
        duty(h, TURN)
        assert tick(h, {"1": 99.0, "2": 20.0, "3": 40.0}) is TickOutcome.BLOCKED
        assert not switches(h)
        assert reasons(h)[-1] == PAUSED_REASON

    def test_no_candidate_with_headroom_holds_and_warns(self, temp_home):
        # 2 is full; 3 at 98% would be over the line by its own next reading
        # with this user's 3-point turns.
        h = make(temp_home)
        duty(h, TURN)
        assert tick(h, {"1": 99.0, "2": 100.0, "3": 98.0}) is TickOutcome.BLOCKED
        assert not switches(h)
        last = h.events[-1]
        assert last.reason == NO_HEADROOM_REASON
        assert last.human() == "stay: no account with headroom; #1 is about to hit its limit"
        assert h.engine.last_decision.line == last.human()

    def test_never_to_a_target_whose_fetch_failed(self, temp_home):
        h = make(temp_home)
        duty(h, TURN)
        now = h.clock.now
        entries = {
            "1": entry(usage(h, 99.0), now),
            "2": entry(usage(h, 5.0), now - 300, last_error="http-429"),
            "3": entry(usage(h, 40.0), now),
        }
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        assert switches(h)[-1].to_ref["number"] == 3

    def test_only_failed_targets_left_holds(self, temp_home):
        h = make(temp_home)
        duty(h, TURN)
        now = h.clock.now
        entries = {
            "1": entry(usage(h, 99.0), now),
            "2": entry(usage(h, 5.0), now - 300, last_error="http-429"),
            "3": entry(usage(h, 5.0), now - 300, last_error="network"),
        }
        assert h.tick_with_entries(entries) is TickOutcome.BLOCKED
        assert reasons(h)[-1] == NO_HEADROOM_REASON

    def test_far_below_the_line_never_reads_history(self, temp_home, monkeypatch):
        h = make(temp_home)

        def boom(*a, **k):
            raise AssertionError("history read below the escalation band")

        monkeypatch.setattr(h.switcher.usage_history, "series", boom)
        assert tick(h, {"1": 50.0, "2": 20.0, "3": 40.0}) is TickOutcome.NO_ACTION

    def test_another_accounts_history_is_never_used(self, temp_home):
        # Account 2's big turns say nothing about account 1's next reading.
        h = make(temp_home)
        duty(h, [(150, 90.0), (120, 95.0), (90, 99.0), (0, 99.0)], slot="2")
        duty(h, [(150, 96.0), (120, 96.0), (90, 96.0), (60, 97.0), (30, 97.0), (0, 97.0)])
        assert tick(h, {"1": 97.0, "2": 20.0, "3": 40.0}) is TickOutcome.NO_ACTION

    def test_at_or_over_the_threshold_is_still_a_threshold_switch(self, temp_home):
        h = make(temp_home)
        duty(h, TURN)
        assert tick(h, {"1": 100.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == "at-limit"


class TestFeedReadingSwitchesInTheSameTick:
    def test_an_over_threshold_feed_reading_switches_on_its_own_tick(
        self, temp_home, monkeypatch
    ):
        """The reading lands in the feed; the very tick that ingests it
        switches (no poll, no second tick)."""
        from unittest.mock import patch

        from claude_rotater import feed, oauth
        from claude_rotater.feed_ingest import FeedIngestor
        from tests.test_feed_ingest import NOW, collect, rec

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr("claude_rotater.switcher._FETCH_STAGGER_S", 0)
        h = make(temp_home, threshold=95.0)
        h.clock.now = NOW
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        with patch("claude_rotater.oauth.fetch_oauth_profile", return_value=None):
            u = {n: {"five_hour": {"pct": p, "resets_at": _iso_at(NOW + 4 * 3600)},
                     "seven_day": {"pct": 5.0, "resets_at": _iso_at(NOW + 5 * 86400)}}
                 for n, p in (("1", 50.0), ("2", 10.0), ("3", 10.0))}
            collect(h, u, {"1", "2", "3"})
            engine = h._make_engine(feed=FeedIngestor(h.switcher, clock=h.clock))
            h.clock.advance(5)
            feed.append_record(feed.feed_path(h.switcher.backup_dir), rec(
                t=h.clock.now, config_dir=str(h.temp_home / ".claude"), pct5=96.0,
                reset5=NOW + 4 * 3600, reset7=NOW + 5 * 86400,
            ))
            with patch("claude_rotater.oauth.try_fetch_usage_for_account",
                       side_effect=AssertionError("polled instead of using the feed")):
                assert engine.tick() is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == "proactive"
        assert oauth  # imported for the patch target


class TestDutyBurn:
    """The engine reads the account on duty the way the TUI does: the
    user's burn carries over a fresh switch, never an idle past."""

    def record(self, h, slot, readings):
        email = h.switcher.account_email(slot)
        now = h.clock.now
        for ago, pct7 in readings:
            u = usage(h, 10.0, pct7)
            h.switcher.usage_history.record({slot: u}, {slot: (email, "")}, now - ago)

    def test_pace_burn_carries_over_a_fresh_switch(self, temp_home):
        h = make(temp_home)
        # #2 burned 7d at 2%/h until a switch a minute ago; #1 is now on duty
        # with only flat readings of its own.
        self.record(h, "2", [(60 + i * 600, 50.0 - i * 0.333) for i in range(12)])
        self.record(h, "1", [(60 + i * 600, 20.0) for i in range(12)] + [(0, 20.0)])
        h.engine._mutate_state(lambda st: st.update(lastSwitchAt=h.clock.now - 60))
        assert h.engine._burn_rate("1", h.clock.now) == pytest.approx(2.0, rel=0.05)

    def test_pace_burn_ignores_the_new_duty_s_idle_past(self, temp_home):
        h = make(temp_home)
        # #1 rose 3%/h hours ago (used elsewhere), then a switch to it and
        # an hour of flat readings: the user is not burning it.
        self.record(h, "1", [(4 * 3600 + i * 600, 40.0 - i * 0.5) for i in range(12)])
        self.record(h, "1", [(i * 600, 40.0) for i in range(7)])
        h.engine._mutate_state(lambda st: st.update(lastSwitchAt=h.clock.now - 3700))
        assert h.engine._burn_rate("1", h.clock.now) == pytest.approx(0.0, abs=1e-6)
