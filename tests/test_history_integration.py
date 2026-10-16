"""Usage history wired through the real collector: fetches record samples
and attempts, and the next poll plan follows the burn estimate."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_swap import oauth, poll_policy
from tests.test_autoswitch import EngineHarness, _usage

ID1 = ("a@example.com", "")
ID2 = ("b@example.com", "")


@pytest.fixture
def h(temp_home, monkeypatch):
    monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
    harness = EngineHarness(temp_home)
    harness.seed(1, "a@example.com")
    harness.seed(2, "b@example.com")
    harness.make_live("a@example.com", 1)
    monkeypatch.setattr(harness.switcher, "_live_session_pids", lambda *a: [])
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
        yield harness


def collect(h, usage_by_num, errors_by_num=None, fetch=None):
    """One scheduled collector pass with faked HTTP; returns slots fetched."""
    fetched: list[str] = []

    def fake(num, email, creds, is_active=False, persist_credentials=None, **kw):
        fetched.append(num)
        error = (errors_by_num or {}).get(num)
        if error:
            return oauth.UsageOutcome(None, error=error)
        value = usage_by_num.get(num)
        return oauth.UsageOutcome(dict(value) if value else None)

    with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=fake):
        h.switcher.usage_entries_by_account(
            fetch=set(fetch or usage_by_num), scheduled=True
        )
    return fetched


def plan_of(h, num, ident):
    e = h.switcher._usage_store.entries({num: ident})[num]
    return e.next_poll_at, e.poll_interval_s


def advance_to_plan(h, num, ident):
    next_at, _ = plan_of(h, num, ident)
    h.clock.now = max(h.clock.now, next_at)


class TestRecording:
    def test_fetch_records_samples_and_attempts(self, h):
        t0 = h.clock.now
        assert sorted(collect(h, {"1": _usage(40), "2": _usage(10)})) == ["1", "2"]
        series = h.switcher.usage_history.series({"1": ID1, "2": ID2})
        assert [p.pct for p in series["1"]["5h"]] == [40.0]
        assert [p.pct for p in series["2"]["5h"]] == [10.0]
        assert series["1"]["5h"][0].t == t0
        assert h.switcher.usage_history.attempts("1", ID1, 0) == [t0]

    def test_failure_adds_attempt_but_no_sample(self, h):
        collect(h, {"1": _usage(40)}, errors_by_num={"1": "timeout"})
        assert h.switcher.usage_history.series({"1": ID1}) == {"1": {}}
        assert len(h.switcher.usage_history.attempts("1", ID1, 0)) == 1

    def test_served_from_store_adds_nothing(self, h):
        collect(h, {"1": _usage(40)})
        h.clock.advance(30)  # fresh: no fetch
        assert collect(h, {"1": _usage(41)}) == []
        assert len(h.switcher.usage_history.series({"1": ID1})["1"]["5h"]) == 1
        assert len(h.switcher.usage_history.attempts("1", ID1, 0)) == 1


class TestPlanFollowsEstimate:
    def test_plan_shrinks_as_the_burn_approaches_the_threshold(self, h):
        intervals = []
        exact = 40.0
        for _ in range(8):
            collect(h, {"1": _usage(int(exact))}, fetch={"1"})  # integer pct, as fetched
            intervals.append(plan_of(h, "1", ID1)[1])
            advance_to_plan(h, "1", ID1)
            exact = min(exact + 0.03 * intervals[-1], 89.0)  # 108%/h
        # First plan has a single point: the baseline. Then the ETA shrinks
        # the interval monotonically as the crossing approaches.
        assert intervals[0] == poll_policy.ACTIVE_INTERVAL_S
        assert intervals[-1] < poll_policy.ACTIVE_INTERVAL_S
        assert intervals[-1] < poll_policy.ACTIVE_INTERVAL_S
        assert all(b <= a for a, b in zip(intervals[1:], intervals[2:]))

    def test_flat_usage_stays_at_baseline(self, h):
        for _ in range(4):
            collect(h, {"1": _usage(40)}, fetch={"1"})
            assert plan_of(h, "1", ID1)[1] == poll_policy.ACTIVE_INTERVAL_S
            advance_to_plan(h, "1", ID1)

    def test_ledger_budget_caps_the_next_poll(self, h):
        now = h.clock.now
        # 19 earlier attempts this hour, the oldest at now - 3000.
        for i in range(poll_policy.HOURLY_BUDGET - 1):
            h.switcher.usage_history.record_attempts(["1"], {"1": ID1}, now - 3000 + i)
        collect(h, {"1": _usage(40)}, fetch={"1"})  # the 20th attempt this hour
        next_at, _ = plan_of(h, "1", ID1)
        assert next_at == pytest.approx(now - 3000 + 3600)

    def test_cadence_settings_drive_the_plan(self, h):
        (h.switcher.backup_dir / "settings.json").write_text(
            json.dumps({"autoswitch": {"pollActiveSeconds": 200}})
        )
        collect(h, {"1": _usage(40)}, fetch={"1"})
        assert plan_of(h, "1", ID1)[1] == 200.0


class TestBestEffort:
    def test_history_write_failure_never_fails_the_fetch(self, h, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("history broken")

        monkeypatch.setattr(h.switcher.usage_history, "record", boom)
        monkeypatch.setattr(h.switcher.usage_history, "record_attempts", boom)
        collect(h, {"1": _usage(40)}, fetch={"1"})
        e = h.switcher._usage_store.entries({"1": ID1})["1"]
        assert e.last_good["five_hour"]["pct"] == 40
        assert e.poll_interval_s == poll_policy.ACTIVE_INTERVAL_S

    def test_history_read_failure_degrades_to_baseline(self, h, monkeypatch):
        collect(h, {"1": _usage(40)}, fetch={"1"})
        advance_to_plan(h, "1", ID1)

        def boom(*a, **k):
            raise RuntimeError("history broken")

        monkeypatch.setattr(h.switcher.usage_history, "series", boom)
        monkeypatch.setattr(h.switcher.usage_history, "attempts", boom)
        collect(h, {"1": _usage(70)}, fetch={"1"})  # would otherwise tighten
        e = h.switcher._usage_store.entries({"1": ID1})["1"]
        assert e.last_good["five_hour"]["pct"] == 70
        assert e.poll_interval_s == poll_policy.ACTIVE_INTERVAL_S

    def test_corrupt_history_file_is_tolerated(self, h):
        path = h.switcher.usage_history.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{garbage")
        collect(h, {"1": _usage(40)}, fetch={"1"})
        assert [p.pct for p in h.switcher.usage_history.series({"1": ID1})["1"]["5h"]] == [40.0]
