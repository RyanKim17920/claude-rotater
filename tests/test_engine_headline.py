"""The engine publishes its headline for the view-only windows beside it."""

from __future__ import annotations

from unittest.mock import patch

from claude_rotater.autoswitch import (
    DECISION_FILENAME,
    NoSwitchEvent,
    PollEvent,
    SwitchEvent,
    read_published_decision,
)
from tests.test_autoswitch import EngineHarness

STAY = NoSwitchEvent(reason="stay", detail="stay: #1's 7d resets soonest")


def test_a_stay_verdict_is_published_and_read_back(temp_home):
    h = EngineHarness(temp_home)
    h.engine._emit(STAY)
    decision = read_published_decision(h.switcher.backup_dir)
    assert decision is not None
    assert (decision.action, decision.line) == ("stay", "stay: #1's 7d resets soonest")


def test_a_switch_replaces_it(temp_home):
    h = EngineHarness(temp_home)
    h.engine._emit(STAY)
    h.engine._emit(SwitchEvent(trigger="pace", from_ref={"number": 1}, to_ref={"number": 2},
                               reason="switch → #2: #2 needs using first"))
    decision = read_published_decision(h.switcher.backup_dir)
    assert (decision.action, decision.target) == ("switch", "2")


def test_machine_outcomes_and_repeats_write_nothing(temp_home):
    h = EngineHarness(temp_home)
    h.engine._emit(STAY)
    with patch("claude_rotater.autoswitch.atomic_write_json") as write:
        h.engine._emit(NoSwitchEvent(reason="cooldown"))
        h.engine._emit(PollEvent(active=None, headroom={}, threshold=95.0))
        h.engine._emit(NoSwitchEvent(reason="stay", detail="stay: #1's 7d resets soonest"))
    write.assert_not_called()


def test_a_dry_run_publishes_nothing(temp_home):
    h = EngineHarness(temp_home)
    h._make_engine(dry_run=True)._emit(STAY)
    assert not (h.switcher.backup_dir / DECISION_FILENAME).exists()


def test_garbage_reads_as_none(temp_home):
    h = EngineHarness(temp_home)
    (h.switcher.backup_dir / DECISION_FILENAME).write_text("{nope")
    assert read_published_decision(h.switcher.backup_dir) is None
