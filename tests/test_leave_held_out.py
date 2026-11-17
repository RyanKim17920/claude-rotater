"""The account on duty is taken out of rotation (disabled, removed): the
engine moves off it at once, as a forced move, or says why it cannot."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_rotater.autoswitch import (
    LEAVE_TRIGGER,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    leave_account,
)
from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED
from tests.test_autoswitch import EngineHarness, _entry_for, _usage


@pytest.fixture
def harness(temp_home):
    h = EngineHarness(temp_home)
    for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        h.seed(n, email)
    h.make_live("a@example.com", 1)
    return h


def _make(temp_home, **settings):
    h = EngineHarness(temp_home, **settings)
    for n, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        h.seed(n, email)
    h.make_live("a@example.com", 1)
    return h


def _disable(h, num: str) -> None:
    data = h.switcher._get_sequence_data()
    data["accounts"][num]["disabled"] = True
    h.switcher._write_json(h.switcher.sequence_file, data)


def _switch(h):
    return next((e for e in h.events if isinstance(e, SwitchEvent)), None)


def _reasons(h):
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


class TestEngineLeavesDisabledCurrent:
    def test_healthy_disabled_current_is_left_for_the_best_target(self, harness):
        _disable(harness, "1")
        outcome = harness.tick_with_usage({"1": _usage(10), "2": _usage(60), "3": _usage(20)})
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3
        assert _switch(harness).trigger == LEAVE_TRIGGER

    def test_leave_ignores_cooldown_and_manual_hold(self, temp_home):
        h = _make(temp_home, strategy="pace")
        from claude_rotater.autoswitch import note_manual_switch

        note_manual_switch(h.switcher.backup_dir, "1", clock=h.clock)
        _disable(h, "1")
        assert h.tick_with_usage({"1": _usage(5), "2": _usage(30), "3": _usage(50)}) is TickOutcome.SWITCHED
        assert h.active_number() in (2, 3)

    def test_leave_in_manual_mode_too(self, temp_home):
        h = _make(temp_home, mode="manual")
        _disable(h, "1")
        assert h.tick_with_usage({"1": _usage(5), "2": _usage(30), "3": _usage(50)}) is TickOutcome.SWITCHED

    def test_never_lands_on_a_disabled_target(self, harness):
        _disable(harness, "1")
        _disable(harness, "3")
        harness.tick_with_usage({"1": _usage(10), "2": _usage(60), "3": _usage(0)})
        assert harness.active_number() == 2

    def test_unreadable_target_is_still_better_than_staying(self, harness):
        _disable(harness, "1")
        _disable(harness, "3")
        assert harness.tick_with_usage({"1": _usage(10), "2": None}) is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_dead_or_exhausted_targets_are_not_eligible(self, harness):
        _disable(harness, "1")
        outcome = harness.tick_with_usage(
            {"1": _usage(10), "2": USAGE_RELOGIN_REQUIRED, "3": _usage(100)}
        )
        assert outcome is TickOutcome.BLOCKED, harness.events
        assert harness.active_number() == 1
        assert _reasons(harness) == ["held-out-no-target"]
        detail = next(e.detail for e in harness.events if isinstance(e, NoSwitchEvent))
        assert "disabled" in detail and "no other account" in detail

    def test_no_eligible_account_stays_and_says_so(self, harness):
        for n in ("1", "2", "3"):
            _disable(harness, n)
        assert harness.tick_with_usage({"1": _usage(10)}) is TickOutcome.BLOCKED
        assert harness.active_number() == 1
        assert _reasons(harness) == ["held-out-no-target"]

    def test_enabled_current_below_threshold_stays(self, harness):
        _disable(harness, "3")
        assert harness.tick_with_usage({"1": _usage(10), "2": _usage(60)}) is TickOutcome.NO_ACTION
        assert harness.active_number() == 1


class TestLeaveAccount:
    """The synchronous path the TUI's disable / remove takes."""

    def _leave(self, h, num, why="disabled", usage=None):
        usage = usage or {"1": _usage(10), "2": _usage(60), "3": _usage(20)}
        entries = {n: _entry_for(v, h.clock.now) for n, v in usage.items()}
        with patch.object(h.switcher, "usage_entries_by_account", return_value=entries):
            return leave_account(h.switcher, h.settings, num, why)

    def test_moves_off_the_current_account(self, harness):
        _disable(harness, "1")
        payload = self._leave(harness, "1")
        assert payload["switched"] is True
        assert payload["to"]["number"] == 3
        assert harness.active_number() == 3

    def test_moves_off_an_enabled_account_being_removed(self, harness):
        payload = self._leave(harness, "1", why="being removed")
        assert payload["switched"] is True and harness.active_number() == 3

    def test_not_on_duty_is_a_no_op(self, harness):
        payload = self._leave(harness, "2")
        assert payload == {"switched": False, "reason": ""}
        assert harness.active_number() == 1

    def test_nowhere_to_go_reports_why(self, harness):
        for n in ("1", "2", "3"):
            _disable(harness, n)
        payload = self._leave(harness, "1")
        assert payload["switched"] is False
        assert "no other account can take over" in payload["reason"]
        assert harness.active_number() == 1

    def test_a_second_leaver_finds_it_already_left(self, harness):
        _disable(harness, "1")
        assert self._leave(harness, "1")["switched"] is True
        again = self._leave(harness, "1")
        assert again == {"switched": False, "reason": ""}
        assert harness.active_number() == 3
