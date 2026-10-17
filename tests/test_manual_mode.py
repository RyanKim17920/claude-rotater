"""Manual mode (``autoswitch.mode = "manual"``): the user picks the account
and the engine never moves for strategy reasons. With the limit guard on
(``autoswitch.manualLimitGuard``, the default) it still moves off an account
at, or about to hit, its hard limit, or with a dead login."""

from __future__ import annotations

import pytest

from claude_rotater.autoswitch import (
    MANUAL_REASON,
    PREEMPT_TRIGGER,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    decision_from_event,
)
from tests.test_autoswitch import DEAD, _usage
from tests.test_preempt_engine import TURN, duty, make, reasons, switches, tick

GUARD_LINE = "stay: you pick the account; the guard moves only at a hard limit"
NO_GUARD_LINE = "stay: you pick the account; rota never switches on its own"


def manual(temp_home, *, guard: bool = True, **kw):
    return make(temp_home, mode="manual", manual_limit_guard=guard, **kw)


class TestStrategyNeverMoves:
    @pytest.mark.parametrize("strategy", ["best", "pace", "expiring-first", "consume-first"])
    def test_over_the_threshold_but_under_the_limit_stays(self, temp_home, strategy):
        # Auto would move: 95% is over the 90% threshold.
        h = manual(temp_home, threshold=90.0, strategy=strategy)
        assert tick(h, {"1": 95.0, "2": 10.0, "3": 10.0}) is TickOutcome.NO_ACTION
        assert not switches(h)
        assert h.active_number() == 1
        last = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert (last.reason, last.detail) == (MANUAL_REASON, GUARD_LINE)

    def test_the_same_reading_in_auto_mode_switches(self, temp_home):
        h = make(temp_home, threshold=90.0)
        assert tick(h, {"1": 95.0, "2": 10.0, "3": 10.0}) is TickOutcome.SWITCHED

    def test_pace_spreading_move_is_held(self, temp_home):
        # A tight 5h window pace hands over below the threshold in auto mode
        # (two agreeing readings two minutes apart).
        h = manual(temp_home, strategy="pace")
        for _ in range(3):
            assert tick(h, {"1": 85.0, "2": 5.0, "3": 30.0}) is TickOutcome.NO_ACTION
            h.clock.advance(120)
        assert not switches(h)

    def test_header_shows_the_manual_line(self, temp_home):
        h = manual(temp_home)
        tick(h, {"1": 50.0, "2": 10.0, "3": 10.0})
        event = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert event.self_explained
        assert decision_from_event(event).line == GUARD_LINE


class TestLimitGuard:
    def test_at_the_limit_moves(self, temp_home):
        h = manual(temp_home)
        assert tick(h, {"1": 100.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == "at-limit"
        assert h.active_number() == 2

    def test_about_to_hit_the_limit_moves_early(self, temp_home):
        # 96 -> 99 in one turn: the next reading would be over 100%.
        h = manual(temp_home, threshold=90.0)
        duty(h, TURN)
        assert tick(h, {"1": 99.0, "2": 20.0, "3": 40.0}) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == PREEMPT_TRIGGER

    def test_projection_under_the_limit_stays_even_over_the_threshold(self, temp_home):
        # Steady at 96-97%: over a 90% threshold, nowhere near 100% next read.
        h = manual(temp_home, threshold=90.0)
        duty(h, [(150, 96.0), (120, 96.0), (90, 96.0), (60, 97.0), (30, 97.0), (0, 97.0)])
        assert tick(h, {"1": 97.0, "2": 20.0, "3": 40.0}) is TickOutcome.NO_ACTION
        assert reasons(h)[-1] == MANUAL_REASON

    def test_dead_login_fails_over(self, temp_home):
        h = manual(temp_home, unhealthy_ticks=1)
        assert h.tick_with_usage({"1": DEAD, "2": _usage(10), "3": _usage(50)}) is (
            TickOutcome.SWITCHED
        )
        assert switches(h)[-1].trigger == "failover"


class TestGuardOff:
    @pytest.mark.parametrize("pct", [50.0, 99.0, 100.0])
    def test_never_moves(self, temp_home, pct):
        h = manual(temp_home, guard=False)
        duty(h, TURN)
        assert tick(h, {"1": pct, "2": 20.0, "3": 40.0}) is TickOutcome.NO_ACTION
        assert not switches(h)
        last = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert (last.reason, last.detail) == (MANUAL_REASON, NO_GUARD_LINE)

    def test_not_even_off_a_dead_login(self, temp_home):
        h = manual(temp_home, guard=False, unhealthy_ticks=1)
        for _ in range(3):
            h.tick_with_usage({"1": DEAD, "2": _usage(10), "3": _usage(50)})
        assert not [e for e in h.events if isinstance(e, SwitchEvent)]
        assert h.active_number() == 1
