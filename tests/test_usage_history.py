"""Tests for the per-account usage history (sample ring + attempt ledger)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from claude_rotater import usage_history
from claude_rotater.usage_history import Point, UsageHistory

NOW = 1_800_000_000.0
ID1 = ("a@x.com", "org-1")
ID2 = ("b@y.com", "org-2")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _usage(five: float, seven: float = 10.0, *, reset5: float | None = NOW + 3600,
           scoped: list | None = None) -> dict:
    u: dict = {
        "five_hour": {"pct": five, "resets_at": _iso(reset5) if reset5 else None},
        "seven_day": {"pct": seven},
    }
    if scoped is not None:
        u["scoped"] = scoped
    return u


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def hist(tmp_path, clock):
    return UsageHistory(tmp_path / "cache", clock=clock)


class TestRoundTrip:
    def test_record_then_series(self, hist):
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW - 60)
        hist.record({"1": _usage(42)}, {"1": ID1}, NOW)
        series = hist.series({"1": ID1})
        assert series["1"]["5h"] == [
            Point(NOW - 60, 40.0, NOW + 3600),
            Point(NOW, 42.0, NOW + 3600),
        ]
        assert series["1"]["7d"] == [Point(NOW - 60, 10.0, None), Point(NOW, 10.0, None)]

    def test_file_schema(self, hist):
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)
        raw = json.loads(hist.path.read_text())
        assert raw["schemaVersion"] == 2
        row = raw["accounts"]["a@x.com|org-1"]
        assert row["email"] == "a@x.com" and row["organizationUuid"] == "org-1"
        assert row["samples"] == [[NOW, {"5h": [40.0, NOW + 3600], "7d": [10.0, None]}]]

    def test_scoped_windows_always_stored(self, hist):
        scoped = [{"name": "Fable", "pct": 55.0, "resets_at": _iso(NOW + 9000)}]
        hist.record({"1": _usage(40, scoped=scoped)}, {"1": ID1}, NOW)
        assert hist.series({"1": ID1})["1"]["Fable"] == [Point(NOW, 55.0, NOW + 9000)]

    def test_none_usage_and_empty_usage_add_nothing(self, hist):
        hist.record({"1": None, "2": {}}, {"1": ID1, "2": ID2}, NOW)
        assert not hist.path.exists()
        assert hist.series({"1": ID1, "2": ID2}) == {"1": {}, "2": {}}

    def test_same_t_is_deduplicated(self, hist):
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)
        hist.record({"1": _usage(41)}, {"1": ID1}, NOW)
        assert [p.pct for p in hist.series({"1": ID1})["1"]["5h"]] == [40.0]

    def test_out_of_order_samples_are_sorted(self, hist):
        hist.record({"1": _usage(42)}, {"1": ID1}, NOW)
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW - 300)
        assert [p.t for p in hist.series({"1": ID1})["1"]["5h"]] == [NOW - 300, NOW]

    def test_batched_slots_in_one_write(self, hist):
        hist.record({"1": _usage(40), "2": _usage(5)}, {"1": ID1, "2": ID2}, NOW)
        series = hist.series({"1": ID1, "2": ID2})
        assert series["1"]["5h"][0].pct == 40.0
        assert series["2"]["5h"][0].pct == 5.0

    def test_slot_without_identity_is_ignored(self, hist):
        hist.record({"1": _usage(40)}, {}, NOW)
        assert not hist.path.exists()


class TestIdentityGuard:
    def test_mismatch_is_invisible_on_read(self, hist):
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)
        assert hist.series({"1": ID2}) == {"1": {}}
        hist.record_attempts(["1"], {"1": ID1}, NOW)
        assert hist.attempts("1", ID2, 0) == []

    def test_reused_slot_keeps_each_account_its_own_history(self, hist):
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW - 60)
        hist.record_attempts(["1"], {"1": ID1}, NOW - 60)
        hist.record({"1": _usage(5)}, {"1": ID2}, NOW)
        assert hist.series({"1": ID2})["1"]["5h"] == [Point(NOW, 5.0, NOW + 3600)]
        assert hist.attempts("1", ID2, 0) == []
        # The account that held slot 1 before is the same account wherever
        # it shows up again: its history stays its own.
        assert [p.pct for p in hist.series({"2": ID1})["2"]["5h"]] == [40.0]

    def test_missing_org_matches_empty_org(self, hist):
        hist.record({"1": _usage(40)}, {"1": ("a@x.com", "")}, NOW)
        assert hist.series({"1": ("a@x.com", "")})["1"]["5h"]


class TestRetention:
    def test_full_resolution_inside_24h(self, hist, clock):
        for i in range(10):
            hist.record({"1": _usage(i)}, {"1": ID1}, NOW - 23 * 3600 + i * 60)
        assert len(hist.series({"1": ID1})["1"]["5h"]) == 10

    def test_older_than_24h_thinned_to_one_per_hour(self, hist, clock):
        base = (NOW - 30 * 3600) // 3600 * 3600  # an hour boundary, >24h back
        for i in range(12):  # 12 samples, 5 minutes apart, inside one hour
            hist.record({"1": _usage(i)}, {"1": ID1}, base + i * 300)
        pts = hist.series({"1": ID1})["1"]["5h"]
        assert [p.t for p in pts] == [base + 11 * 300]  # newest in the bucket

    def test_thinning_keeps_one_per_distinct_hour(self, hist):
        base = (NOW - 48 * 3600) // 3600 * 3600
        for h in range(5):
            for m in (0, 600, 1200):
                hist.record({"1": _usage(h)}, {"1": ID1}, base + h * 3600 + m)
        assert len(hist.series({"1": ID1})["1"]["5h"]) == 5

    def test_older_than_8_days_dropped(self, hist):
        hist.record({"1": _usage(1)}, {"1": ID1}, NOW - 8 * 86400 - 1)
        hist.record({"1": _usage(2)}, {"1": ID1}, NOW)
        assert [p.pct for p in hist.series({"1": ID1})["1"]["5h"]] == [2.0]

    def test_hard_cap_keeps_newest(self, hist):
        rows = {"1": {"email": ID1[0], "organizationUuid": ID1[1],
                      "samples": [[NOW - 20000 + i * 30, {"5h": [1.0, None]}]
                                  for i in range(600)], "attempts": []}}
        hist.path.parent.mkdir(parents=True, exist_ok=True)
        hist.path.write_text(json.dumps({"schemaVersion": 1, "accounts": rows}))
        hist.record({"1": _usage(9)}, {"1": ID1}, NOW + 1)
        pts = hist.series({"1": ID1})["1"]["5h"]
        assert len(pts) == usage_history.MAX_SAMPLES
        assert pts[-1].t == NOW + 1 and pts[-1].pct == 9.0

    def test_retention_uses_clock_not_sample_time(self, hist, clock):
        hist.record({"1": _usage(1)}, {"1": ID1}, NOW - 100)
        clock.t = NOW + 9 * 86400
        hist.record({"1": _usage(2)}, {"1": ID1}, clock.t)
        assert [p.pct for p in hist.series({"1": ID1})["1"]["5h"]] == [2.0]


class TestAttempts:
    def test_attempts_round_trip_and_since(self, hist):
        for t in (NOW - 300, NOW - 200, NOW - 100):
            hist.record_attempts(["1"], {"1": ID1}, t)
        assert hist.attempts("1", ID1, NOW - 250) == [NOW - 200, NOW - 100]
        assert hist.attempts("1", ID1, 0) == [NOW - 300, NOW - 200, NOW - 100]

    def test_trimmed_to_trailing_hour(self, hist, clock):
        hist.record_attempts(["1"], {"1": ID1}, NOW - 3700)
        hist.record_attempts(["1"], {"1": ID1}, NOW - 100)
        assert hist.attempts("1", ID1, 0) == [NOW - 100]

    def test_sample_write_also_trims_attempts(self, hist, clock):
        hist.record_attempts(["1"], {"1": ID1}, NOW)
        clock.t = NOW + 4000
        hist.record({"1": _usage(5)}, {"1": ID1}, clock.t)
        assert hist.attempts("1", ID1, 0) == []

    def test_unknown_slot_has_no_attempts(self, hist):
        assert hist.attempts("9", ID1, 0) == []

    def test_attempts_and_samples_coexist(self, hist):
        hist.record_attempts(["1", "2"], {"1": ID1, "2": ID2}, NOW)
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)
        assert hist.attempts("1", ID1, 0) == [NOW]
        assert hist.attempts("2", ID2, 0) == [NOW]
        assert hist.series({"1": ID1})["1"]["5h"][0].pct == 40.0


class TestTolerance:
    @pytest.mark.parametrize("content", [
        "{not json", "[]", json.dumps({"schemaVersion": 2, "accounts": {}}),
        json.dumps({"schemaVersion": 1, "accounts": []}), "\xff\xfe",
    ])
    def test_bad_file_reads_empty_and_is_rewritten(self, hist, content):
        hist.path.parent.mkdir(parents=True, exist_ok=True)
        hist.path.write_text(content, encoding="latin-1")
        assert hist.series({"1": ID1}) == {"1": {}}
        assert hist.attempts("1", ID1, 0) == []
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)
        assert hist.series({"1": ID1})["1"]["5h"][0].pct == 40.0

    def test_garbage_samples_are_skipped(self, hist):
        rows = {"1": {"email": ID1[0], "organizationUuid": ID1[1],
                      "samples": [["x", {}], [NOW - 10, {"5h": ["bad", None],
                                                          "7d": [3.0, "bad"]}],
                                  [True, {"5h": [1.0, None]}], 5],
                      "attempts": ["x", True, NOW - 5]}}
        hist.path.parent.mkdir(parents=True, exist_ok=True)
        hist.path.write_text(json.dumps({"schemaVersion": 1, "accounts": rows}))
        assert hist.series({"1": ID1}) == {"1": {"7d": [Point(NOW - 10, 3.0, None)]}}
        assert hist.attempts("1", ID1, 0) == [NOW - 5]
        hist.record({"1": _usage(4)}, {"1": ID1}, NOW)  # rewrites cleanly
        assert len(hist.series({"1": ID1})["1"]["7d"]) == 2

    def test_write_failure_is_swallowed(self, hist, monkeypatch, caplog):
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(usage_history, "atomic_write_json", boom)
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)  # must not raise
        hist.record_attempts(["1"], {"1": ID1}, NOW)
        assert "Usage history write failed" in caplog.text

    def test_lock_timeout_is_swallowed(self, hist, monkeypatch):
        monkeypatch.setattr(
            usage_history.FileLock, "acquire", lambda self, timeout=None: False
        )
        hist.record({"1": _usage(40)}, {"1": ID1}, NOW)
        assert not hist.path.exists()


class TestConcurrency:
    def test_two_instances_interleave_without_loss(self, tmp_path, clock):
        a = UsageHistory(tmp_path / "cache", clock=clock)
        b = UsageHistory(tmp_path / "cache", clock=clock)

        def writer(h, slot, ident, offset):
            for i in range(20):
                h.record({slot: _usage(i)}, {slot: ident}, NOW - 2000 + i * 10 + offset)
                h.record_attempts([slot], {slot: ident}, NOW - 2000 + i * 10 + offset)

        threads = [
            threading.Thread(target=writer, args=(a, "1", ID1, 0)),
            threading.Thread(target=writer, args=(b, "2", ID2, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        series = a.series({"1": ID1, "2": ID2})
        assert len(series["1"]["5h"]) == 20
        assert len(series["2"]["5h"]) == 20
        assert len(b.attempts("1", ID1, 0)) == 20
        assert len(b.attempts("2", ID2, 0)) == 20
