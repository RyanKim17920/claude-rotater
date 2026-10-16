"""Long-term usage history: hourly peaks and per-window peaks, bounded."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from claude_rotater import usage_history
from claude_rotater.usage_history import (
    HOUR_S,
    LONG_MAX_AGE_S,
    Point,
    UsageHistory,
    WindowSummary,
)

T0 = 1_800_000_000.0 - (1_800_000_000.0 % (5 * HOUR_S))  # a 5h boundary
ID1 = ("a@x.com", "org-1")
ID2 = ("b@y.com", "org-2")
W5 = 5 * HOUR_S
W7 = 7 * 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def usage_at(t: float) -> dict:
    """A steady burn: each 5h window climbs 0 -> ~50%, each 7d window
    climbs 0 -> ~70%, both resetting on fixed boundaries."""
    reset5 = T0 + ((t - T0) // W5 + 1) * W5
    reset7 = T0 + ((t - T0) // W7 + 1) * W7
    pct5 = round(50.0 * (1 - (reset5 - t) / W5), 1)
    pct7 = round(70.0 * (1 - (reset7 - t) / W7), 1)
    return {
        "five_hour": {"pct": pct5, "resets_at": _iso(reset5)},
        "seven_day": {"pct": pct7, "resets_at": _iso(reset7)},
    }


def simulate(hist: UsageHistory, clock: Clock, days: float, step_s: float = 600.0,
             slots=(("1", ID1),)) -> None:
    end = clock.t + days * 86400
    while clock.t < end:
        for num, ident in slots:
            hist.record({num: usage_at(clock.t)}, {num: ident}, clock.t)
        clock.t += step_s


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def hist(tmp_path, clock):
    return UsageHistory(tmp_path / "cache", clock=clock)


class TestLongSeries:
    def test_thirty_days_hourly(self, hist, clock):
        simulate(hist, clock, 30, step_s=1800.0)
        series = hist.long_series(ID1, "5h", T0)
        assert len(series) == 30 * 24
        assert all(p.resets_at is None for p in series)
        assert [p.t for p in series] == [T0 + i * HOUR_S for i in range(30 * 24)]
        # Each hour holds its peak: the last sample before the hour ends,
        # within one 5h window.
        first = series[0]
        assert first.pct == usage_at(T0 + 1800)["five_hour"]["pct"]
        assert max(p.pct for p in series) <= 50.0
        week = hist.long_series(ID1, "7d", T0)
        assert len(week) == 30 * 24

    def test_since_filters_by_hour(self, hist, clock):
        simulate(hist, clock, 3)
        since = T0 + 48 * HOUR_S + 10
        series = hist.long_series(ID1, "5h", since)
        assert series[0].t == T0 + 48 * HOUR_S
        assert len(series) == 24

    def test_current_hour_comes_from_unfolded_samples(self, hist, clock):
        simulate(hist, clock, 1)
        clock.t += 60
        hist.record({"1": usage_at(clock.t)}, {"1": ID1}, clock.t)
        series = hist.long_series(ID1, "5h", T0)
        assert series[-1].t == (clock.t // HOUR_S) * HOUR_S
        assert series[-1].pct == usage_at(clock.t)["five_hour"]["pct"]

    def test_hour_peak_is_the_max_across_a_reset(self, hist, clock):
        # 49% just before a 5h reset, 1% just after, in the same hour.
        base = T0 + W5 - 1200
        clock.t = base
        reset = T0 + W5
        hist.record({"1": {"five_hour": {"pct": 49.0, "resets_at": _iso(reset)}}},
                    {"1": ID1}, base)
        clock.t = base + 1800
        hist.record({"1": {"five_hour": {"pct": 1.0, "resets_at": _iso(reset + W5)}}},
                    {"1": ID1}, clock.t)
        clock.t += 2 * HOUR_S
        hist.record({"1": {"five_hour": {"pct": 9.0, "resets_at": _iso(reset + W5)}}},
                    {"1": ID1}, clock.t)
        series = hist.long_series(ID1, "5h", 0)
        assert series[0] == Point((base // HOUR_S) * HOUR_S, 49.0, None)

    def test_unknown_identity_and_label(self, hist, clock):
        simulate(hist, clock, 1)
        assert hist.long_series(ID2, "5h", 0) == []
        assert hist.long_series(ID1, "Fable", 0) == []
        assert hist.windows(ID2, "5h", 0) == []


class TestWindows:
    def test_thirty_days_of_windows_with_peaks(self, hist, clock):
        simulate(hist, clock, 30, step_s=1800.0)
        fives = hist.windows(ID1, "5h", 0)
        assert len(fives) == 30 * 24 // 5
        assert fives[0].resets_at == T0 + W5
        assert all(isinstance(w, WindowSummary) for w in fives)
        # A completed window peaked at its last sample (10 min before reset).
        assert fives[0].peak_pct == pytest.approx(50.0 * (1 - 1800 / W5), abs=0.1)
        assert fives[-1].resets_at >= clock.t  # the running window last
        sevens = hist.windows(ID1, "7d", 0)
        assert [w.resets_at for w in sevens] == [T0 + i * W7 for i in range(1, 6)]

    def test_since_filters_by_reset(self, hist, clock):
        simulate(hist, clock, 2)
        since = T0 + 24 * HOUR_S
        assert all(w.resets_at >= since for w in hist.windows(ID1, "5h", since))

    def test_reset_jitter_is_one_window(self, hist, clock):
        reset = T0 + W5
        for i, jitter in enumerate((0.0, 1.0, -0.6, 30.0)):
            clock.t = T0 + i * 60
            hist.record({"1": {"five_hour": {"pct": 10.0 + i, "resets_at": _iso(reset + jitter)}}},
                        {"1": ID1}, clock.t)
        assert [w.peak_pct for w in hist.windows(ID1, "5h", 0)] == [13.0]


class TestBoundsAndStorage:
    def test_retention_is_bounded(self, hist, clock):
        simulate(hist, clock, 100, step_s=3600.0)
        series = hist.long_series(ID1, "5h", 0)
        assert series[0].t >= clock.t - LONG_MAX_AGE_S - 2 * HOUR_S
        assert len(series) <= 90 * 24 + 2
        windows = hist.windows(ID1, "5h", 0)
        assert windows[0].resets_at >= clock.t - LONG_MAX_AGE_S - HOUR_S

    def test_thirty_days_two_accounts_stay_small(self, hist, clock, tmp_path):
        simulate(hist, clock, 30, step_s=1800.0, slots=(("1", ID1), ("2", ID2)))
        cache = tmp_path / "cache"
        long_size = (cache / "usage_longterm.json").stat().st_size
        ring_size = (cache / "usage_history.json").stat().st_size
        assert long_size < 40_000, long_size
        assert ring_size < 120_000, ring_size

    def test_long_term_file_is_written_at_most_hourly(self, hist, clock, monkeypatch):
        writes: list[str] = []
        real = usage_history.atomic_write_json

        def spy(path, data, **kw):
            writes.append(path.name)
            real(path, data, **kw)

        monkeypatch.setattr(usage_history, "atomic_write_json", spy)
        simulate(hist, clock, 1, step_s=120.0)
        assert writes.count("usage_longterm.json") <= 25
        assert writes.count("usage_history.json") == 24 * 30

    def test_keyed_by_identity_not_slot(self, hist, clock):
        simulate(hist, clock, 1)
        # Slot 1 now holds another account: its history starts empty, and
        # the first account's long-term history is still readable.
        simulate(hist, clock, 1, slots=(("1", ID2),))
        assert len(hist.long_series(ID1, "5h", 0)) == 24
        assert len(hist.long_series(ID2, "5h", 0)) == 24
        assert hist.long_series(ID2, "5h", 0)[0].t == T0 + 24 * HOUR_S

    def test_corrupt_long_term_file_reads_empty_and_heals(self, hist, clock, tmp_path):
        simulate(hist, clock, 1)
        path = tmp_path / "cache" / "usage_longterm.json"
        path.write_text("{garbage")
        assert len(hist.long_series(ID1, "5h", 0)) <= 2  # only unfolded hours
        simulate(hist, clock, 0.5)
        assert json.loads(path.read_text())["schemaVersion"] == 1

    @pytest.mark.parametrize("entry", [
        {"h0": "x", "h": [1]},
        {"h0": 5, "h": "nope", "p": [[1, 2, 3], "x", [True, 1]]},
        "junk",
    ])
    def test_malformed_entries_are_ignored(self, hist, clock, tmp_path, entry):
        path = tmp_path / "cache" / "usage_longterm.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        key = f"{ID1[0]}|{ID1[1]}"
        path.write_text(json.dumps({"schemaVersion": 1,
                                    "accounts": {key: {"w": {"5h": entry}}}}))
        assert hist.long_series(ID1, "5h", 0) == []
        assert hist.windows(ID1, "5h", 0) == []
        simulate(hist, clock, 0.2)  # folding over garbage still works
        simulate(hist, clock, 0.1)

    def test_files_are_compact_json(self, hist, clock, tmp_path):
        simulate(hist, clock, 0.2)
        for name in ("usage_history.json", "usage_longterm.json"):
            assert "\n" not in (tmp_path / "cache" / name).read_text()
