"""Tests for the pre-emptive switch projection (preempt.py)."""

from __future__ import annotations

import pytest

from claude_rotater import preempt
from claude_rotater.preempt import Projection, next_reading_in, project, switch_point
from claude_rotater.usage_history import Point

NOW = 1_000_000.0
RESET = NOW + 3 * 3600  # the 5h window still has 3h to run
MIN = 60.0


def five_hour(*readings: tuple[float, float]) -> dict[str, list[Point]]:
    """``{"5h": [...]}`` from ``(seconds_before_now, pct)`` pairs."""
    return {"5h": [Point(NOW - ago, pct, RESET) for ago, pct in readings]}


def run(series, *, horizon_s=120.0, since=None, live=None, models=()):
    return project(
        series, live or {}, models=models, now=NOW, since=since, horizon_s=horizon_s
    )


class TestSwitchPoint:
    @pytest.mark.parametrize(
        "threshold, expected", [(99.9, 99.9), (90.0, 90.0), (100.0, 100.0), (101.0, 100.0)]
    )
    def test_threshold_or_the_hard_limit(self, threshold, expected):
        assert switch_point(threshold) == expected


class TestProject:
    def test_steady_burn_extrapolates_over_the_gap(self):
        # 1%/min for 20 minutes, one point a minute: no reading jumped more
        # than the burn, so the burn over the 3-minute gap decides.
        series = five_hour(*((20 * MIN - i * MIN, 75.0 + i) for i in range(21)))
        p = run(series, horizon_s=180.0)
        assert p.window == "5h"
        assert p.pct == 95.0
        assert p.burn_pct_per_min == pytest.approx(1.0, rel=0.05)
        assert p.projected_pct == pytest.approx(98.0, abs=0.2)
        assert not p.reaches(99.9)
        assert run(series, horizon_s=300.0).reaches(99.9)

    def test_a_sudden_large_jump_is_the_margin(self):
        # Slow burn, but one turn put on 3 points between two readings.
        series = five_hour((40 * MIN, 90.0), (30 * MIN, 91.0), (20 * MIN, 94.0),
                           (10 * MIN, 95.0), (0, 97.0))
        p = run(series, horizon_s=60.0)
        assert p.jump_pct == 3.0
        assert p.projected_pct == pytest.approx(100.0)
        assert p.reaches(99.9)

    def test_margin_is_the_larger_of_burn_and_jump_not_their_sum(self):
        series = five_hour((10 * MIN, 90.0), (5 * MIN, 92.0), (0, 94.0))
        p = run(series, horizon_s=60.0)
        rise_burn = p.burn_pct_per_min * 1.0
        assert p.projected_pct == pytest.approx(94.0 + max(rise_burn, p.jump_pct))

    def test_sparse_single_reading_falls_back_to_the_window_average(self):
        # One reading: no slope and no jump, but 98% two hours into the
        # window is 0.82%/min on average, which a 2-minute gap carries over.
        p = run(five_hour((0, 98.0)))
        assert p.burn_pct_per_min == pytest.approx(98.0 / 120.0)
        assert p.projected_pct == pytest.approx(98.0 + 2 * 98.0 / 120.0)
        assert p.reaches(99.5) and not p.reaches(99.9)

    def test_sparse_reading_after_a_mid_window_switch_says_nothing_more(self):
        # The window average includes another duty: no burn, no margin.
        p = run(five_hour((0, 98.0)), since=NOW - 60)
        assert p.projected_pct == 98.0

    def test_only_this_duty_counts(self):
        # The 5-point jump happened before the last switch (another account's
        # duty, or this one's old session): it is not this duty's margin.
        series = five_hour((60 * MIN, 90.0), (50 * MIN, 95.0), (5 * MIN, 95.0), (0, 96.0))
        p = run(series, since=NOW - 10 * MIN, horizon_s=60.0)
        assert p.jump_pct == 1.0
        assert not p.reaches(99.9)

    def test_window_resetting_before_the_next_reading_is_skipped(self):
        series = {"5h": [Point(NOW - 120, 95.0, NOW + 30), Point(NOW, 99.0, NOW + 30)]}
        assert run(series, horizon_s=60.0) is None

    def test_live_reading_newer_than_history_anchors(self):
        series = five_hour((10 * MIN, 90.0), (0, 92.0))
        p = run(series, live={"5h": (96.0, RESET)}, horizon_s=60.0)
        assert p.pct == 96.0

    def test_highest_relevant_window_wins_and_scoped_needs_models(self):
        series = {
            **five_hour((10 * MIN, 50.0), (0, 51.0)),
            "7d": [Point(NOW - 600, 97.0, NOW + 86400), Point(NOW, 99.0, NOW + 86400)],
            "Fable": [Point(NOW - 600, 10.0, NOW + 86400), Point(NOW, 99.5, NOW + 86400)],
        }
        assert run(series, horizon_s=60.0).window == "7d"
        assert run(series, horizon_s=60.0, models=("Fable",)).window == "Fable"

    def test_reason_line(self):
        p = Projection("5h", 99.0, 100.4, 170.0, 1.2, 1.0)
        assert p.reason() == "pre-empt: ~100.4% by next reading in ~3m (burn +1.2%/m)"


class TestNextReadingIn:
    def test_live_feed_uses_the_median_recent_gap(self):
        times = [NOW - g for g in (400, 340, 280, 220, 160, 100, 40)]
        assert next_reading_in(
            times, now=NOW, since=None, poll_in_s=180.0, feed_stale_s=600.0
        ) == 60.0

    def test_never_later_than_the_next_poll(self):
        times = [NOW - g for g in (1500, 1000, 500, 10)]
        assert next_reading_in(
            times, now=NOW, since=None, poll_in_s=180.0, feed_stale_s=600.0
        ) == 180.0

    def test_stale_feed_falls_back_to_the_poll(self):
        times = [NOW - g for g in (1000, 960, 920, 880)]
        assert next_reading_in(
            times, now=NOW, since=None, poll_in_s=150.0, feed_stale_s=600.0
        ) == 150.0

    def test_too_few_readings_since_the_switch_use_the_poll(self):
        times = [NOW - g for g in (300, 240, 180, 60, 30)]
        assert next_reading_in(
            times, now=NOW, since=NOW - 90, poll_in_s=170.0, feed_stale_s=600.0
        ) == 170.0

    def test_one_long_pause_does_not_move_the_median(self):
        times = [NOW - g for g in (1700, 1640, 1580, 1520, 400, 340, 280, 220)]
        assert next_reading_in(
            times, now=NOW, since=None, poll_in_s=600.0, feed_stale_s=600.0
        ) == 60.0


def test_module_constants_are_sane():
    assert preempt.MIN_GAPS >= 2 and preempt.GAP_SAMPLES >= preempt.MIN_GAPS
