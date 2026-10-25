"""$ per 1% climb by climb: segments of the readings, spend over each, use
elsewhere, grades, the robust trend and the store every profile shares.
Pure functions over made-up spend and readings; no transcript, no ccusage."""

from __future__ import annotations

import json
import math

import pytest

from claude_rotater import dollar_per_pct as dpp
from claude_rotater.usage_history import Point

HOUR = 3600.0
DAY = 86400.0
FIVE_H = 5 * HOUR
NOW = 1_800_000_000.0 // DAY * DAY + 12 * HOUR  # midday: a day's samples share a bin


def steady_spend(lo: float, hi: float, per_hour: float) -> list[tuple[int, float]]:
    start = int(lo // 900) * 900
    return [(s, per_hour / 4) for s in range(start, int(hi), 900)]


def readings(start: float, every: float, pcts: list[float], resets_at: float) -> list[Point]:
    return [Point(start + (i + 1) * every, pct, resets_at) for i, pct in enumerate(pcts)]


def cut(spend, points, resets, segment=4.0, *, duration=FIVE_H, hourly=(), lo=NOW - DAY,
        now=NOW, covered=lambda a, b: True):
    return dpp.samples(spend, points, list(hourly), resets, duration, lo, now, covered, segment)


# -- slots ---------------------------------------------------------------------------


class TestProrated:
    def test_a_slot_cut_by_either_end_counts_by_its_share_inside(self):
        spending = dpp._Spending([(0, 4.0), (900, 8.0), (1800, 12.0)])
        assert spending.prorated(0, 2700) == pytest.approx(24.0)
        assert spending.prorated(450, 2250) == pytest.approx(2.0 + 8.0 + 6.0)
        assert spending.prorated(1000, 1100) == pytest.approx(8.0 * 100 / 900)  # inside one slot
        assert spending.prorated(2700, 9000) == 0.0 and spending.prorated(5, 5) == 0.0

    def test_gaps_between_slots_hold_nothing(self):
        spending = dpp._Spending([(0, 9.0), (9000, 9.0)])
        assert spending.prorated(450, 9450) == pytest.approx(4.5 + 4.5)


# -- one window, cut into climbs ----------------------------------------------------


class TestSamples:
    def test_each_climb_of_the_segment_size_is_one_sample_at_its_middle(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, reset, 10.0)  # $10/h
        points = readings(start, HOUR, [2, 4, 6, 8, 9], reset)
        read, got = cut(spend, points, [reset])
        assert read == {reset}
        # 0 -> 4 over the first 2h, 4 -> 8 over the next 2h; the last 1 point waits
        assert [(s.t, s.pct) for s in got] == [(start + HOUR, 4), (start + 3 * HOUR, 4)]
        assert all(s.usd == pytest.approx(20.0) and s.ratio == pytest.approx(5.0) for s in got)
        assert all(s.elsewhere == 0 and s.grade == "sure" for s in got)

    def test_a_climb_is_cut_at_the_first_reading_that_reaches_the_size(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        points = readings(start, HOUR, [1, 7, 8, 12], reset)  # jumps past 4 then 4 more
        _, got = cut(steady_spend(start, reset, 10.0), points, [reset])
        assert [s.pct for s in got] == [7, 5]

    def test_slot_cost_is_prorated_to_reading_times(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, reset, 10.0)
        points = [Point(start + 1000, 4.0, reset)]  # read 100 s into the second slot
        _, (s,) = cut(spend, points, [reset])
        assert s.usd == pytest.approx(10.0 * 1000 / HOUR)

    def test_a_rise_with_no_local_spend_around_it_happened_elsewhere(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, start + 2.5 * HOUR, 10.0)  # only the first 2.5 hours
        points = readings(start, HOUR, [2, 4, 6, 8], reset)
        _, (first, second) = cut(spend, points, [reset])
        assert first.elsewhere == 0 and first.grade == "sure"
        # 4 -> 6 had spend; 6 -> 8 had none, not even in the quarter hour before 6 was read
        assert second.elsewhere == pytest.approx(0.5) and second.grade == "unsure"
        all_away = cut([], points, [reset])[1]
        assert all(s.grade == "out" for s in all_away)

    def test_a_lagging_lower_reading_is_skipped(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        spend = steady_spend(start, start + 2 * HOUR, 10.0)
        # 4 at 1h, a stale 1 at 2h, back to 4 at 3h, 8 at 4h
        points = readings(start, HOUR, [4, 1, 4, 8], reset)
        _, got = cut(spend, points, [reset])
        assert [(s.t, s.pct) for s in got] == [(start + HOUR / 2, 4), (start + 2.5 * HOUR, 4)]

    def test_readings_of_another_window_are_not_this_ones(self):
        reset = NOW - HOUR
        start = reset - FIVE_H
        stray = Point(start + 0.5 * HOUR, 90.0, reset + FIVE_H)  # names the next window's reset
        points = [stray, *readings(start, HOUR, [4, 8], reset)]
        _, got = cut(steady_spend(start, reset, 10.0), points, [reset])
        assert [s.pct for s in got] == [4, 4]

    def test_each_window_starts_from_zero_at_its_own_start(self):
        first = NOW - 7 * HOUR
        second = first + FIVE_H
        points = readings(first - FIVE_H, HOUR, [4], first) + readings(first, HOUR, [4], second)
        read, got = cut(steady_spend(NOW - DAY, NOW, 10.0), points, [first, second])
        assert read == {first, second}
        assert [(s.resets_at, s.pct) for s in got] == [(first, 4), (second, 4)]

    def test_a_running_window_gives_the_climbs_it_has_finished(self):
        reset = NOW + 2 * HOUR
        start = reset - FIVE_H
        points = readings(start, HOUR, [4, 6], reset)
        read, got = cut(steady_spend(start, NOW, 10.0), points, [reset])
        assert read == {reset} and [s.pct for s in got] == [4]

    def test_early_unread_or_uncovered_windows_are_skipped(self):
        spend = steady_spend(NOW - 3 * DAY, NOW, 10.0)
        lo = NOW - DAY
        early = lo + HOUR  # starts before the priced spend
        done = NOW - 2 * HOUR
        empty = NOW - 10 * HOUR
        points = readings(early - FIVE_H, HOUR, [30], early) + readings(done - FIVE_H, HOUR, [4], done)
        read, got = cut(spend, points, [early, empty, done], lo=lo)
        assert read == {done} and [s.resets_at for s in got] == [done]
        assert cut(spend, points, [done], lo=lo, covered=lambda a, b: False) == (set(), [])

    def test_hourly_peaks_stand_in_when_the_exact_readings_are_gone(self):
        reset = NOW - 3 * HOUR - 1800  # mid-hour: the hour holding the reset is ambiguous
        start = reset - FIVE_H
        first_hour = math.ceil(start / HOUR) * HOUR
        hourly = [Point(first_hour + i * HOUR, pct, None) for i, pct in enumerate([4, 8, 12, 16])]
        hourly.append(Point(first_hour + 4 * HOUR, 99.0, None))  # holds the reset: ignored
        spend = steady_spend(start, reset, 10.0)
        _, got = cut(spend, [], [reset], hourly=hourly)
        assert [s.pct for s in got] == [4, 4, 4, 4]
        # the first is dated at its hour's end: spend from the window start to then
        assert got[0].usd == pytest.approx((first_hour + HOUR - start) / HOUR * 10.0)


# -- the figure and its trend ---------------------------------------------------------


def sample(days_ago: float, ratio: float, pct: float = 4.0, elsewhere: float = 0.0,
           resets_at: float = 1.0):
    return dpp.Sample(NOW - days_ago * DAY, resets_at, ratio * pct, pct, elsewhere)


class TestSummarize:
    def test_recent_sure_climb_gives_the_figure(self):
        got = dpp.summarize([sample(1, 2.0, 20), sample(2, 2.0, 6), sample(3, 4.0, 4)], "5h", NOW)
        assert got.sure and got.dollars == pytest.approx((40 + 12 + 16) / 30)

    def test_too_little_sure_climb_falls_back_to_unsure_samples(self):
        got = dpp.summarize([sample(1, 2.0, 10), sample(2, 1.0, 10, elsewhere=0.3)], "5h", NOW)
        assert not got.sure and got.dollars == pytest.approx(1.5)
        assert dpp.summarize([sample(1, 9.0, 10)], "7d", NOW).sure  # 10 points of 7d will do

    def test_nothing_recent_or_only_left_out_samples_give_none(self):
        assert dpp.summarize([sample(20, 2.0)], "5h", NOW) is None
        assert dpp.summarize([sample(1, 0.1, elsewhere=0.9)], "5h", NOW) is None

    def test_points_are_every_usable_sample_of_the_trend_span_marked_sure_or_not(self):
        found = [sample(40, 9.0), sample(3, 2.0), sample(2, 1.0, elsewhere=0.3),
                 sample(1, 3.0, elsewhere=0.9)]  # 40 days: past the span; 0.9: left out
        got = dpp.summarize(found, "5h", NOW)
        assert got.points == ((NOW - 3 * DAY, pytest.approx(2.0), True),
                              (NOW - 2 * DAY, pytest.approx(1.0), False))

    def test_a_trend_needs_three_days_over_two_weeks(self):
        assert dpp.summarize([sample(13, 5.0), sample(7, 4.0), sample(1, 3.0)], "5h", NOW).trend is None
        assert dpp.summarize([sample(15, 5.0), sample(8, 4.0), sample(1, 3.0)], "5h", NOW).trend < 0


def daily(days: range, ratio, per_day: int = 1):
    """``per_day`` samples a day (an hour apart) at ``ratio(day)``."""
    return [sample(d - h / 24, ratio(d)) for d in days for h in range(per_day)]


class TestFit:
    def test_a_significant_fall_slopes_and_reads_its_change(self):
        found = daily(range(21, 0, -1), lambda d: 5.0 * 0.98 ** (21 - d), per_day=5)
        f = dpp.fit(found, NOW)
        assert not f.steady and not f.provisional and f.n == 21
        assert f.change == pytest.approx(0.98 ** 28 - 1, rel=1e-3)
        assert f.at(NOW - 21 * DAY) == pytest.approx(5.0, rel=1e-2)  # on the line at both ends
        assert f.at(NOW - DAY) == pytest.approx(5.0 * 0.98 ** 20, rel=1e-2)

    def test_noise_without_drift_is_a_flat_line_at_the_median(self):
        found = daily(range(20, 0, -1), lambda d: 5.0 * (1.2 if d % 2 else 0.8))
        f = dpp.fit(found, NOW)
        assert f.steady and f.change == 0.0 and f.span_change == 0.0
        assert f.at(NOW - 20 * DAY) == f.at(NOW)  # flat
        assert f.at(NOW) in (pytest.approx(4.0), pytest.approx(6.0))

    def test_wild_samples_cannot_tilt_the_line(self):
        found = daily(range(21, 0, -1), lambda d: 5.0, per_day=6)
        found += [sample(d - 0.5, 50.0) for d in range(3, 0, -1)]  # a few 10x outliers lately
        f = dpp.fit(found, NOW)
        assert f.steady and f.at(NOW) == pytest.approx(5.0)

    def test_a_tiny_but_clean_drift_still_reads_steady(self):
        # 1% per 4 weeks with no noise at all: real, but too small to call
        found = daily(range(21, 0, -1), lambda d: 5.0 * 1.01 ** ((21 - d) / 28))
        assert dpp.fit(found, NOW).steady

    def test_two_days_give_a_provisional_flat_line(self):
        f = dpp.fit([sample(5, 4.0), sample(1, 6.0)], NOW)
        assert f.provisional and f.steady and f.n == 2
        assert f.span_s == pytest.approx(4 * DAY)
        assert dpp.fit([sample(1, 6.0), sample(1.01, 9.0)], NOW) is None  # one day only

    def test_a_short_trend_is_provisional_and_reads_its_change_over_its_span(self):
        short = daily(range(8, 0, -1), lambda d: 5.0 * 0.97 ** (8 - d), per_day=4)
        f = dpp.fit(short, NOW)
        assert f.provisional and not f.steady
        assert f.span_change == pytest.approx(0.97 ** 7 - 1, rel=1e-2)
        assert f.change < f.span_change  # 4 weeks would stretch the week
        assert dpp.summarize(short, "5h", NOW).trend is None  # the table does not quote it

    def test_unsure_samples_carry_a_fit_only_when_sure_ones_cannot(self):
        unsure = [sample(d, 5.0, elsewhere=0.3) for d in (20, 10, 1)]
        assert dpp.fit(unsure, NOW).provisional
        mixed = unsure + [sample(d, 4.0) for d in (18, 9, 2)]
        f = dpp.fit(mixed, NOW)
        assert not f.provisional and f.n == 3 and f.at(NOW) == pytest.approx(4.0)

    def test_left_out_samples_never_enter_the_fit(self):
        found = [sample(d, 5.0) for d in (20, 10, 1)] + [sample(5, 0.2, elsewhere=0.9)]
        f = dpp.fit(found, NOW)
        assert f.n == 3 and f.steady


# -- the store -----------------------------------------------------------------------


class TestStore:
    def test_round_trip_and_garbage(self, tmp_path):
        path = tmp_path / dpp.STORE_FILENAME
        store: dpp.Store = {}
        assert dpp.merge(store, "a|o", "5h", set(), [sample(1, 2.0)], NOW)
        dpp.save_store(path, store)
        assert dpp.load_store(path) == store
        path.write_text("{oops")
        assert dpp.load_store(path) == {}
        path.write_text(json.dumps({"schemaVersion": 1, "accounts": {"a|o": {"5h": [[1, 2, 3, 0]]}}}))
        assert dpp.load_store(path) == {}  # the per-window file before: rebuilt
        path.write_text(json.dumps({"schemaVersion": 2, "accounts": {"a|o": {"5h": [[1, "x", 2, 3, 0]]}}}))
        assert dpp.load_store(path) == {}

    def test_a_reread_window_replaces_its_samples(self):
        store: dpp.Store = {}
        dpp.merge(store, "a|o", "5h", set(), [sample(1, 2.0, resets_at=7.0),
                                              sample(2, 2.0, resets_at=8.0)], NOW)
        assert not dpp.merge(store, "a|o", "5h", {7.0}, [sample(1, 2.0, resets_at=7.0)], NOW)
        assert dpp.merge(store, "a|o", "5h", {7.0}, [sample(1.1, 3.0, resets_at=7.0),
                                                     sample(1, 3.0, resets_at=7.0)], NOW)
        assert [(s.resets_at, s.ratio) for s in store["a|o"]["5h"]] == [
            (8.0, pytest.approx(2.0)), (7.0, pytest.approx(3.0)), (7.0, pytest.approx(3.0))]

    def test_samples_past_the_trend_span_fold_into_one_per_day(self):
        store: dpp.Store = {}
        old = [sample(50 + h / 24, r, pct=p, elsewhere=e)
               for h, r, p, e in ((1, 2.0, 4, 0.0), (2, 4.0, 4, 0.5), (3, 3.0, 8, 0.0))]
        dpp.merge(store, "a|o", "5h", set(), [*old, sample(1, 5.0)], NOW)
        folded, recent = store["a|o"]["5h"]
        assert folded.resets_at == dpp.FOLDED and folded.pct == 16
        assert folded.usd == pytest.approx(8 + 16 + 24) and folded.elsewhere == pytest.approx(2 / 16)
        assert recent == sample(1, 5.0)
        assert not dpp.merge(store, "a|o", "5h", {1.0}, [sample(1, 5.0)], NOW)  # folding is stable

    def test_too_old_or_too_many_samples_go(self, monkeypatch):
        store: dpp.Store = {}
        dpp.merge(store, "a|o", "5h", set(), [sample(500, 2.0), sample(1, 2.0)], NOW)
        assert [s.t for s in store["a|o"]["5h"]] == [NOW - DAY]  # 500 days old: dropped
        monkeypatch.setattr(dpp, "STORE_MAX_SAMPLES", 3)
        dpp.merge(store, "a|o", "5h", set(), [sample(d / 10, 2.0) for d in range(1, 6)], NOW)
        assert len(store["a|o"]["5h"]) == 3 and store["a|o"]["5h"][-1].t == NOW - DAY / 10
