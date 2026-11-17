"""A provider-refused account end to end: detected from transcripts (and,
more cautiously, the usage endpoint), held out of rotation in every profile
holding it, polled only at a daily recheck, left when on duty, and returned
once it is seen working."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from claude_rotater import provider_block as pb
from claude_rotater.autoswitch import LEAVE_TRIGGER, SwitchEvent, TickOutcome
from claude_rotater.json_output import USAGE_PROVIDER_BLOCKED
from claude_rotater.switcher import ClaudeAccountSwitcher
from claude_rotater.usage_store import FetchRecord
from tests.test_autoswitch import EngineHarness, _usage
from tests.test_provider_block import ORG_DISABLED, PAST_DUE, api_error, turn

A, B, C = "a@example.com", "b@example.com", "c@example.com"


def _creds(num: int) -> str:
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"at-{num}", "refreshToken": f"rt-{num}",
        "expiresAt": int((time.time() + 86400) * 1000),
    }})


@pytest.fixture
def harness(temp_home):
    h = EngineHarness(temp_home)
    for n, email in ((1, A), (2, B), (3, C)):
        h.seed(n, email)
    h.make_live(A, 1)
    return h


def _identity(email: str) -> tuple[str, str]:
    return (email, "")


def _transcript(h, entries, name="s.jsonl"):
    path = h.temp_home / ".claude" / "projects" / "-p" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def _rescan(h):
    h.engine._refusal_scanned_at = None


class TestHeldOutOfRotation:
    def test_blocked_accounts_are_not_switchable(self, harness):
        pb.mark(_identity(B), pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, time.time(),
                path=harness.switcher.provider_blocks_path)
        assert harness.switcher.switchable_account_numbers() == ["1", "3"]
        assert harness.switcher.provider_block_of("2").reason == pb.PAST_DUE

    def test_a_block_is_seen_by_every_profile_holding_the_account(self, temp_home):
        work = EngineHarness(temp_home / "work")
        home = EngineHarness(temp_home / "home")
        work.seed(1, B)
        home.seed(4, B)
        home.seed(5, C)
        assert work.switcher.provider_blocks_path == home.switcher.provider_blocks_path
        work.switcher.mark_provider_block("1", pb.ORG_DISABLED, pb.SOURCE_TRANSCRIPT, 1.0)
        assert home.switcher.provider_block_of("4").reason == pb.ORG_DISABLED
        assert home.switcher.switchable_account_numbers() == ["5"]

    def test_snapshot_carries_the_reason(self, harness):
        harness.switcher.mark_provider_block("3", pb.ORG_DISABLED, pb.SOURCE_TRANSCRIPT, 1.0)
        with patch.object(harness.switcher, "_run_usage_fetches", return_value={}):
            snap = harness.switcher.accounts_snapshot(fetch=set())
        by_num = {a.number: a for a in snap.accounts}
        assert by_num["3"].provider_block == pb.ORG_DISABLED
        assert by_num["2"].provider_block == ""

    def test_picking_it_by_hand_returns_it(self, harness):
        harness.switcher.mark_provider_block("2", pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, 1.0)
        harness.switcher.switch_to("2")
        assert harness.switcher.provider_block_of("2") is None


class TestPolling:
    """No polls for a blocked account but a daily recheck; a reading well
    after the block lifts it; repeated usage 403s declare one."""

    NOW = 1_900_000_000.0

    @pytest.fixture
    def switcher(self, temp_home):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._init_sequence_file()
        data = s._get_sequence_data()
        data["accounts"]["2"] = {"email": B, "organizationUuid": ""}
        data["sequence"] = [2]
        s._write_json(s.sequence_file, data)
        s._usage_store.clock = lambda: self.NOW
        return s

    INFO = [(2, B, "", "", False, _creds(2), "")]

    def _collect(self, switcher, record):
        with patch.object(switcher, "_run_usage_fetches",
                          return_value={"2": record} if record else {}) as run:
            entries = switcher._collect_usage_entries(self.INFO)
        return entries, run

    def _attempted(self, switcher, at):
        switcher._usage_store.clock = lambda: at
        switcher._usage_store.record({"2": FetchRecord(error="network")}, {"2": _identity(B)})
        switcher._usage_store.clock = lambda: self.NOW

    def test_blocked_account_is_not_polled_before_the_recheck(self, switcher):
        self._attempted(switcher, self.NOW - 600)
        switcher.mark_provider_block("2", pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, self.NOW - 900)
        entries, run = self._collect(switcher, FetchRecord(usage=_usage(10)))
        run.assert_not_called()
        assert entries["2"].sentinel == USAGE_PROVIDER_BLOCKED

    def test_daily_recheck_reading_lifts_an_old_block(self, switcher):
        self._attempted(switcher, self.NOW - pb.RECHECK_S - 1)
        switcher.mark_provider_block("2", pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, self.NOW - pb.RECHECK_S)
        entries, run = self._collect(switcher, FetchRecord(usage=_usage(10)))
        run.assert_called_once()
        assert switcher.provider_block_of("2") is None
        assert entries["2"].sentinel is None

    def test_a_reading_right_after_the_block_does_not_lift_it(self, switcher):
        switcher.mark_provider_block("2", pb.ORG_DISABLED, pb.SOURCE_TRANSCRIPT, self.NOW - 60)
        entries, run = self._collect(switcher, FetchRecord(usage=_usage(10)))  # never polled yet
        run.assert_called_once()
        assert switcher.provider_block_of("2") is not None
        assert entries["2"].sentinel == USAGE_PROVIDER_BLOCKED

    def test_a_failed_recheck_keeps_it_blocked(self, switcher):
        self._attempted(switcher, self.NOW - pb.RECHECK_S - 1)
        switcher.mark_provider_block("2", pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, self.NOW - pb.RECHECK_S)
        entries, _ = self._collect(switcher, FetchRecord(error="http-403"))
        assert switcher.provider_block_of("2").reason == pb.PAST_DUE
        assert entries["2"].sentinel == USAGE_PROVIDER_BLOCKED

    def _forbidden(self, switcher, times):
        for at in times:
            switcher._usage_store.clock = lambda at=at: at
            switcher._usage_store.record({"2": FetchRecord(error="http-403")}, {"2": _identity(B)})
        switcher._usage_store.clock = lambda: self.NOW

    def test_repeated_usage_403s_over_half_an_hour_declare_a_block(self, switcher):
        self._forbidden(switcher, [self.NOW - 2400, self.NOW - 2370])
        # a 429 between does not break the run
        switcher._usage_store.record({"2": FetchRecord(error="http-429")}, {"2": _identity(B)})
        switcher._usage_store._mutate({"2": _identity(B)}, ["2"],
                                      lambda _n, row: row.update(backoffUntil=None, nextPollAt=None))
        self._collect(switcher, FetchRecord(error="http-403"))
        block = switcher.provider_block_of("2")
        assert block is not None and block.reason == pb.USAGE_REFUSED and block.source == "usage"

    def test_a_burst_of_403s_is_not_enough(self, switcher):
        self._forbidden(switcher, [self.NOW - 60, self.NOW - 30])
        switcher._usage_store._mutate({"2": _identity(B)}, ["2"],
                                      lambda _n, row: row.update(backoffUntil=None, nextPollAt=None))
        self._collect(switcher, FetchRecord(error="http-403"))
        assert switcher.provider_block_of("2") is None

    def test_a_success_resets_the_run(self, switcher):
        self._forbidden(switcher, [self.NOW - 4000, self.NOW - 3900])
        switcher._usage_store.record({"2": FetchRecord(usage=_usage(5))}, {"2": _identity(B)})
        entry = switcher._usage_store.entries({"2": _identity(B)})["2"]
        assert entry.forbidden_strikes == 0 and entry.first_forbidden_at is None

    @pytest.mark.parametrize("error", ["http-401", "http-429", "network", "timeout", "invalid_grant"])
    def test_other_failures_never_declare_a_block(self, switcher, error):
        for i in range(6):
            switcher._usage_store.clock = lambda i=i: self.NOW - 4000 + i * 600
            switcher._usage_store.record({"2": FetchRecord(error=error)}, {"2": _identity(B)})
        switcher._usage_store.clock = lambda: self.NOW
        switcher._usage_store._mutate({"2": _identity(B)}, ["2"],
                                      lambda _n, row: row.update(backoffUntil=None, nextPollAt=None))
        self._collect(switcher, FetchRecord(error=error))
        assert switcher.provider_block_of("2") is None


class TestEngineDetectsFromTranscripts:
    def test_refused_turn_blocks_and_leaves_the_account_on_duty(self, harness):
        _transcript(harness, [turn(time.time() - 120), ORG_DISABLED | {"timestamp": _now_iso()}])
        outcome = harness.tick_with_usage({"1": _usage(10), "2": _usage(50), "3": _usage(20)})
        assert harness.switcher.provider_block_of("1").reason == pb.ORG_DISABLED
        assert outcome is TickOutcome.SWITCHED and harness.active_number() == 3
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == LEAVE_TRIGGER and "suspended" in switch.reason

    def test_expired_login_or_rate_limit_is_not_a_block(self, harness):
        _transcript(harness, [
            api_error("Login expired · Please run /login", error="authentication_failed",
                      at=time.time()),
            api_error("You've hit your session limit", error="rate_limit", status=429,
                      at=time.time()),
        ])
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50), "3": _usage(20)})
        assert harness.switcher.provider_block_of("1") is None
        assert harness.active_number() == 1

    def test_refusals_before_this_account_went_on_duty_are_not_its(self, harness):
        _transcript(harness, [PAST_DUE | {"timestamp": _iso(time.time() - 600)}])
        harness.clock.now = time.time() - 60
        from claude_rotater.autoswitch import note_manual_switch

        note_manual_switch(harness.switcher.backup_dir, "1", clock=harness.clock)
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50)})
        assert harness.switcher.provider_block_of("1") is None

    def test_a_working_turn_after_the_block_lifts_it(self, harness):
        blocked_at = time.time() - 300
        harness.switcher.mark_provider_block("1", pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, blocked_at)
        _transcript(harness, [turn(time.time() - 10)])
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50), "3": _usage(20)})
        assert harness.switcher.provider_block_of("1") is None
        assert harness.active_number() == 1

    def test_blocked_account_on_duty_with_nowhere_to_go_stays_and_says_so(self, harness):
        for n in ("2", "3"):
            data = harness.switcher._get_sequence_data()
            data["accounts"][n]["disabled"] = True
            harness.switcher._write_json(harness.switcher.sequence_file, data)
        _transcript(harness, [ORG_DISABLED | {"timestamp": _now_iso()}])
        assert harness.tick_with_usage({"1": _usage(10)}) is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_the_scan_is_throttled(self, harness):
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50)})
        _transcript(harness, [ORG_DISABLED | {"timestamp": _now_iso()}])
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50)})
        assert harness.switcher.provider_block_of("1") is None
        _rescan(harness)
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50)})
        assert harness.switcher.provider_block_of("1") is not None

    def test_a_dry_run_never_writes_a_block(self, harness):
        _transcript(harness, [ORG_DISABLED | {"timestamp": _now_iso()}])
        harness.engine = harness._make_engine(dry_run=True)
        harness.tick_with_usage({"1": _usage(10), "2": _usage(50)})
        assert harness.switcher.provider_block_of("1") is None


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso(time.time())
