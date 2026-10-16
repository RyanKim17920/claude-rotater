"""A user-initiated switch starts the auto engine's cooldown.

Before this, only the engine stamped ``lastSwitchAt``, so an engine
running beside the TUI (or ``cswap auto`` beside a manual ``cswap switch``)
could proactively switch straight back off the account the user had just
chosen."""

from __future__ import annotations

import json
import threading
import time

import pytest

from claude_swap import autoswitch
from claude_swap.autoswitch import (
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    note_manual_switch,
)
from tests.test_autoswitch import _usage, harness  # noqa: F401  (fixture)


def _state(harness) -> dict:
    return harness.state()


class TestManualSwitchStampsCooldown:
    def test_switch_to_stamps_last_switch_at(self, harness):
        harness.clock.advance(1000)
        harness.switcher.switch_to("2", json_output=True)
        assert harness.active_number() == 2
        state = _state(harness)
        assert state["lastSwitchAt"] == harness.clock.now
        assert state["lastSwitchTo"] == "2"
        assert state["schemaVersion"] == autoswitch.STATE_SCHEMA_VERSION

    def test_engine_holds_off_proactive_after_manual_switch(self, harness):
        harness.switcher.switch_to("2", json_output=True)
        harness.clock.advance(10)
        # Account 2 is now hot and 1 looks better: without the stamp the
        # engine would bounce straight back.
        outcome = harness.tick_with_usage({
            "1": _usage(10), "2": _usage(95), "3": _usage(50),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 2
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["cooldown"]

    def test_cooldown_expires_after_manual_switch(self, harness):
        harness.switcher.switch_to("2", json_output=True)
        harness.clock.advance(harness.settings.cooldown_seconds + 1)
        outcome = harness.tick_with_usage({
            "1": _usage(10), "2": _usage(95), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_failover_still_ignores_the_cooldown(self, harness):
        harness.switcher.switch_to("2", json_output=True)
        harness.clock.advance(10)
        outcome = harness.tick_with_usage({
            "1": _usage(10), "2": _usage(100), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"

    def test_strategy_switch_also_stamps(self, harness):
        harness.switcher.switch(json_output=True)  # rotate to the next slot
        assert harness.active_number() == 2
        assert _state(harness)["lastSwitchTo"] == "2"

    def test_already_active_switch_stamps_nothing(self, harness):
        harness.switcher.switch_to("1", json_output=True)
        assert "lastSwitchAt" not in _state(harness)

    def test_manual_switch_clears_the_no_return_memory(self, harness):
        harness.engine._mutate_state(lambda s: s.update(
            lastSwitchAt=0.0, lastSwitchTo="1", lastSwitchFrom=2,
            leftHeadroom=5.0, leftRecoveryAt=None, leftTrigger="proactive",
            quarantine={"3": {"reason": "x"}},
        ))
        harness.switcher.switch_to("2", json_output=True)
        state = _state(harness)
        for key in ("lastSwitchFrom", "leftHeadroom", "leftRecoveryAt", "leftTrigger"):
            assert key not in state
        assert state["quarantine"] == {"3": {"reason": "x"}}  # untouched


class TestEngineSwitchIsNotManual:
    def test_engine_switch_records_its_own_state_without_deadlock(self, harness):
        start = time.monotonic()
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED
        # The nested note saw the held lock and skipped instead of waiting
        # out the 10 s lock timeout.
        assert time.monotonic() - start < 5
        state = _state(harness)
        assert state["lastSwitchTo"] == "2"
        assert str(state["lastSwitchFrom"]) == "1"  # engine snapshot kept
        assert state["leftTrigger"] == "proactive"

    def test_held_lock_set_is_cleared_after_release(self, harness):
        harness.engine._mutate_state(lambda s: None)
        assert autoswitch._held_paths() == set()


class TestNoteManualSwitch:
    def test_writes_fresh_state(self, tmp_path):
        assert note_manual_switch(tmp_path, "3", clock=lambda: 123.0)
        state = json.loads((tmp_path / autoswitch.STATE_FILENAME).read_text())
        assert state == {"schemaVersion": 1, "lastSwitchAt": 123.0, "lastSwitchTo": "3"}

    @pytest.mark.parametrize("content", ["{bad", "[]"])
    def test_corrupt_state_is_replaced(self, tmp_path, content):
        (tmp_path / autoswitch.STATE_FILENAME).write_text(content)
        assert note_manual_switch(tmp_path, "2", clock=lambda: 5.0)
        state = json.loads((tmp_path / autoswitch.STATE_FILENAME).read_text())
        assert state["lastSwitchAt"] == 5.0

    def test_skips_when_this_thread_holds_the_lock(self, tmp_path):
        lock = autoswitch.state_lock_for(tmp_path / autoswitch.STATE_FILENAME)
        with lock:
            assert note_manual_switch(tmp_path, "2") is False
        assert not (tmp_path / autoswitch.STATE_FILENAME).exists()
        assert note_manual_switch(tmp_path, "2") is True

    def test_another_thread_holding_the_lock_is_waited_for(self, tmp_path):
        lock = autoswitch.state_lock_for(tmp_path / autoswitch.STATE_FILENAME)
        lock.acquire()
        result: list[bool] = []
        t = threading.Thread(target=lambda: result.append(
            note_manual_switch(tmp_path, "2", clock=lambda: 7.0)))
        t.start()
        time.sleep(0.2)
        assert result == []  # blocked, not skipped
        lock.release()
        t.join(5)
        assert result == [True]

    def test_lock_failure_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(autoswitch._StateLock, "acquire",
                            lambda self, timeout=None: False)
        assert note_manual_switch(tmp_path, "2") is False

    def test_switch_survives_a_failing_note(self, harness, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("state dir gone")

        monkeypatch.setattr(autoswitch, "note_manual_switch", boom)
        result = harness.switcher.switch_to("2", json_output=True)
        assert result["switched"]
        assert harness.active_number() == 2
