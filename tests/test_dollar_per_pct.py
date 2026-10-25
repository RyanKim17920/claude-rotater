"""$ per 1% window by window: spend to the peak, use elsewhere, grades, the
trend and the store every profile shares. Pure functions over made-up
spend and readings; no transcript, no ccusage."""

from __future__ import annotations

import json
import math
from datetime import datetime

import pytest

from claude_rotater import dollar_per_pct as dpp
from claude_rotater.usage_history import Point

HOUR = 3600.0
DAY = 86400.0
FIVE_H = 5 * HOUR
NOW = 1_800_000_000.0 // 900 * 900  # slot aligned


def steady_spend(lo: float, hi: float, per_hour: float) -> list[tuple[int, float]]:
    start = int(lo // 900) * 900
    return [(s, per_hour / 4) for s in range(start, int(hi), 900)]


def readings(start: float, every: float, pcts: list[float], resets_at: float) -> list[Point]:
    return [Point(start + (i + 1) * every, pct, resets_at) for i, pct in enumerate(pcts)]


# -- one window ------------------------------------------------------------------


class TestWindowCosts:
    def test_spend_up_to_the_peak_over_the_peak(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, reset, 10.0)  # $10/h
        points = readings(start, HOUR, [10, 20, 30, 40], reset)  # peak 40 at start + 4h
        (w,) = dpp.window_costs(spend, points, [], [reset], FIVE_H, NOW - DAY, NOW)
        assert w.pct == 40 and w.usd == pytest.approx(40.0)  # 4h of spend, not 5
        assert w.ratio == pytest.approx(1.0) and w.elsewhere == 0 and w.grade == "sure"

    def test_a_rise_with_no_local_spend_around_it_happened_elsewhere(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, start + 2 * HOUR, 10.0)  # only the first two hours
        points = readings(start, HOUR, [10, 20, 30, 40], reset)
        (w,) = dpp.window_costs(spend, points, [], [reset], FIVE_H, NOW - DAY, NOW)
        # 20 -> 30 still had spend in the quarter hour before 20 was read; 30 -> 40 had none
        assert w.elsewhere == pytest.approx(0.25)
        assert w.grade == "unsure"

    def test_mostly_elsewhere_is_left_out(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, start + HOUR, 10.0)
        points = readings(start, 0.75 * HOUR, [10, 20, 30, 40, 50, 60], reset)
        (w,) = dpp.window_costs(spend, points, [], [reset], FIVE_H, NOW - DAY, NOW)
        assert w.elsewhere == pytest.approx(4 / 6) and w.grade == "out"

    def test_a_lagging_lower_reading_is_no_rise(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, start + 2 * HOUR, 10.0)
        # 20 at 2h, a stale 12 at 3h, back to 20 at 4h: no spend after 2h, nothing elsewhere
        points = readings(start, HOUR, [10, 20, 12, 20], reset)
        (w,) = dpp.window_costs(spend, points, [], [reset], FIVE_H, NOW - DAY, NOW)
        assert w.pct == 20 and w.elsewhere == 0 and w.usd == pytest.approx(20.0)

    def test_running_tiny_early_or_uncovered_windows_are_skipped(self):
        spend = steady_spend(NOW - 3 * DAY, NOW, 10.0)
        lo = NOW - DAY
        running = NOW + HOUR
        early = lo + HOUR  # starts before the priced spend
        done = NOW - 2 * HOUR
        points = (readings(running - FIVE_H, HOUR, [5, 9], running)
                  + readings(early - FIVE_H, HOUR, [30], early)
                  + readings(done - FIVE_H, HOUR, [30, 40], done))
        got = dpp.window_costs(spend, points, [], [early, done, running], FIVE_H, lo, NOW)
        assert [w.resets_at for w in got] == [done]
        assert dpp.window_costs(spend, points, [], [done], FIVE_H, lo, NOW,
                                covered=lambda a, b: False) == []

    def test_readings_of_another_window_are_not_this_ones(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        stray = Point(start + HOUR, 90.0, reset + FIVE_H)  # names the next window's reset
        points = [stray, *readings(start, HOUR, [10, 20], reset)]
        (w,) = dpp.window_costs(steady_spend(start, reset, 10.0), points, [], [reset],
                                FIVE_H, NOW - DAY, NOW)
        assert w.pct == 20

    def test_hourly_peaks_stand_in_when_the_exact_readings_are_gone(self):
        reset = NOW - 3 * HOUR - 1800  # mid-hour: the hour holding the reset is ambiguous
        start = reset - FIVE_H
        first_hour = math.ceil(start / HOUR) * HOUR
        hourly = [Point(first_hour + i * HOUR, pct, None) for i, pct in enumerate([10, 20, 30, 40])]
        hourly.append(Point(first_hour + 4 * HOUR, 99.0, None))  # holds the reset: ignored
        spend = steady_spend(start, reset, 10.0)
        (w,) = dpp.window_costs(spend, [], hourly, [reset], FIVE_H, NOW - DAY, NOW)
        assert w.pct == 40
        # dated at its hour's end: spend from the window start to then
        assert w.usd == pytest.approx((first_hour + 4 * HOUR - start) / HOUR * 10.0)


# -- the figure and its trend ---------------------------------------------------------


def window(days_ago: float, ratio: float, pct: float = 50.0, elsewhere: float = 0.0):
    return dpp.WindowCost(NOW - days_ago * DAY, ratio * pct, pct, elsewhere)


class TestSummarize:
    def test_recent_sure_windows_give_the_figure(self):
        got = dpp.summarize([window(1, 2.0), window(2, 2.0, 30), window(3, 4.0, 20)], "5h", NOW)
        assert got.sure and got.dollars == pytest.approx((100 + 60 + 80) / 100)

    def test_too_few_sure_windows_fall_back_to_unsure_ones(self):
        got = dpp.summarize([window(1, 2.0), window(2, 1.0, elsewhere=0.3)], "5h", NOW)
        assert not got.sure and got.dollars == pytest.approx(1.5)
        assert dpp.summarize([window(1, 9.0)], "7d", NOW).sure  # one 7d window is enough

    def test_nothing_recent_or_only_left_out_windows_give_none(self):
        assert dpp.summarize([window(20, 2.0)], "5h", NOW) is None
        assert dpp.summarize([window(1, 2.0, pct=5)], "5h", NOW) is None
        assert dpp.summarize([window(1, 0.1, elsewhere=0.9)], "5h", NOW) is None

    def test_a_falling_ratio_is_a_shrinking_limit(self):
        # 2% less per day over three weeks: about -43% per 4 weeks
        windows = [window(d, 5.0 * 0.98 ** (21 - d)) for d in range(21, 0, -1)]
        got = dpp.summarize(windows, "5h", NOW)
        assert got.trend == pytest.approx(0.98 ** 28 - 1, rel=1e-6)
        assert not got.steady

    def test_noise_without_drift_is_steady(self):
        windows = [window(d, 5.0 * (1.2 if d % 2 else 0.8)) for d in range(20, 0, -1)]
        got = dpp.summarize(windows, "5h", NOW)
        assert got.trend is not None and got.steady

    def test_a_trend_needs_three_windows_over_two_weeks(self):
        assert dpp.summarize([window(13, 5.0), window(7, 4.0), window(1, 3.0)], "5h", NOW).trend is None
        assert dpp.summarize([window(16, 5.0), window(1, 3.0)], "5h", NOW).trend is None
        assert dpp.summarize([window(15, 5.0), window(8, 4.0), window(1, 3.0)], "5h", NOW).trend < 0

    def test_unsure_windows_stay_out_of_the_trend(self):
        windows = [window(d, 5.0) for d in (20, 14, 8, 2)] + [window(1, 0.5, elsewhere=0.3)]
        got = dpp.summarize(windows, "5h", NOW)
        assert got.trend == pytest.approx(0.0, abs=1e-9)

    def test_sparkline_is_per_day_for_5h_and_per_window_for_7d(self):
        midnight = datetime.combine(datetime.fromtimestamp(NOW).date(), datetime.min.time())
        days_ago = (NOW - midnight.timestamp()) / DAY + 1  # yesterday's midnight
        windows = [window(days_ago - 2 / 24, 2.0), window(days_ago - 3 / 24, 4.0),
                   window(days_ago - 1, 3.0)]  # two yesterday, one today
        assert dpp.summarize(windows, "5h", NOW).spark == pytest.approx((3.0, 3.0))
        assert dpp.summarize(windows, "7d", NOW).spark == pytest.approx((2.0, 4.0, 3.0))


# -- the store -----------------------------------------------------------------------


class TestStore:
    def test_round_trip_and_garbage(self, tmp_path):
        path = tmp_path / dpp.STORE_FILENAME
        store: dpp.Store = {}
        assert dpp.merge(store, "a|o", "5h", [window(1, 2.0)], NOW)
        dpp.save_store(path, store)
        assert dpp.load_store(path) == store
        path.write_text("{oops")
        assert dpp.load_store(path) == {}
        path.write_text(json.dumps({"schemaVersion": 99}))
        assert dpp.load_store(path) == {}
        path.write_text(json.dumps({"schemaVersion": 1, "accounts": {"a|o": {"5h": [[1, "x", 2, 0]]}}}))
        assert dpp.load_store(path) == {}

    def test_a_recomputed_window_replaces_its_entry_and_old_ones_go(self):
        store: dpp.Store = {}
        dpp.merge(store, "a|o", "5h", [window(1, 2.0), window(500, 2.0)], NOW)
        assert list(store["a|o"]["5h"]) == [NOW - DAY]  # 500 days old: dropped
        assert not dpp.merge(store, "a|o", "5h", [window(1, 2.0)], NOW)
        assert dpp.merge(store, "a|o", "5h", [window(1, 3.0)], NOW)
        assert store["a|o"]["5h"][NOW - DAY].ratio == pytest.approx(3.0)
