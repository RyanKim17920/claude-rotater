"""Tests for the pure burn-rate estimator."""

from __future__ import annotations

import math
import random

import pytest

from claude_rotater import estimator as est
from claude_rotater.estimator import estimate_account, estimate_window
from claude_rotater.usage_history import Point

NOW = 1_800_000_000.0
RESET = NOW + 2 * 3600  # 5h window resets two hours from now


def line(rate_pct_h: float, *, start_pct: float, n: int, step_s: float,
         end_t: float = NOW, resets_at: float | None = RESET,
         quantize: bool = False) -> list[Point]:
    """n points ending at end_t, spaced step_s, on a line of rate_pct_h."""
    pts = []
    for i in range(n):
        t = end_t - (n - 1 - i) * step_s
        pct = start_pct + rate_pct_h * (t - pts_start(end_t, n, step_s)) / 3600.0
        pts.append(Point(t, float(round(pct)) if quantize else pct, resets_at))
    return pts


def pts_start(end_t: float, n: int, step_s: float) -> float:
    return end_t - (n - 1) * step_s


class TestRate:
    def test_exact_slope_on_a_synthetic_line(self):
        pts = line(12.0, start_pct=30.0, n=10, step_s=300)
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.rate_pct_per_h == pytest.approx(12.0)
        assert w.last_pct == pytest.approx(pts[-1].pct)
        assert w.last_t == NOW
        assert w.resets_at == RESET
        assert w.points == tuple(pts[-len(w.points):])

    def test_integer_quantized_noise_stays_within_20pct(self):
        rng = random.Random(7)
        for _ in range(50):
            rate = rng.uniform(6.0, 40.0)
            offset = rng.uniform(0, 1)
            pts = line(rate, start_pct=10.0 + offset, n=18, step_s=300, quantize=True)
            w = estimate_window(pts, window="5h", now=NOW)
            assert abs(w.rate_pct_per_h - rate) <= 0.2 * rate, rate

    def test_negative_slope_clamps_to_zero(self):
        pts = [Point(NOW - 600, 50.0, RESET), Point(NOW - 300, 49.0, RESET),
               Point(NOW, 48.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.rate_pct_per_h == 0.0

    def test_integer_jitter_down_is_not_a_reset(self):
        pts = [Point(NOW - 900, 60.0, RESET), Point(NOW - 600, 61.0, RESET),
               Point(NOW - 300, 60.0, RESET), Point(NOW, 62.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert len(w.points) == 4
        assert w.rate_pct_per_h > 0

    def test_rate_cap(self):
        pts = [Point(NOW - 300, 0.0, RESET), Point(NOW, 50.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.rate_pct_per_h == est.MAX_RATE_PCT_PER_H

    def test_unsorted_input(self):
        pts = line(10.0, start_pct=20.0, n=6, step_s=300)
        w = estimate_window(list(reversed(pts)), window="5h", now=NOW)
        assert w.rate_pct_per_h == pytest.approx(10.0)


class TestSegmentation:
    def test_reset_detected_by_pct_drop(self):
        old = [Point(NOW - 1800, 80.0, None), Point(NOW - 1500, 85.0, None)]
        new = [Point(NOW - 1200, 2.0, None), Point(NOW - 600, 4.0, None),
               Point(NOW, 6.0, None)]
        w = estimate_window(old + new, window="5h", now=NOW)
        assert w.points == tuple(new)
        assert w.rate_pct_per_h == pytest.approx(12.0)

    def test_reset_detected_by_resets_at_change(self):
        old = [Point(NOW - 1200, 3.0, RESET - 5 * 3600),
               Point(NOW - 900, 3.0, RESET - 5 * 3600)]
        new = [Point(NOW - 600, 3.0, RESET), Point(NOW, 5.0, RESET)]
        w = estimate_window(old + new, window="5h", now=NOW)
        assert w.points == tuple(new)

    def test_resets_at_jitter_within_tolerance_is_one_window(self):
        pts = [Point(NOW - 600, 10.0, RESET - 60), Point(NOW - 300, 11.0, RESET + 59),
               Point(NOW, 12.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert len(w.points) == 3

    def test_lookback_drops_old_points(self):
        pts = line(10.0, start_pct=5.0, n=30, step_s=300)  # 2.4 h of points
        w = estimate_window(pts, window="5h", now=NOW)
        assert all(p.t >= NOW - est.PARAMS_5H.lookback_s for p in w.points)
        assert len(w.points) < 30

    def test_week_lookback_is_longer(self):
        pts = line(1.0, start_pct=5.0, n=30, step_s=300, resets_at=NOW + 86400)
        w = estimate_window(pts, window="7d", now=NOW)
        assert len(w.points) == 30

    def test_lagging_reading_in_the_same_window_is_not_a_reset(self):
        # Same reset time: a lower, later reading lags (a chat behind, a
        # reading dated too early). It is left out, not read as a reset.
        pts = [Point(NOW - 900, 30.0, RESET), Point(NOW - 600, 40.0, RESET),
               Point(NOW - 300, 34.0, RESET), Point(NOW, 45.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert [p.pct for p in w.points] == [30.0, 40.0, 45.0]

    def test_lagging_newest_reading_leaves_the_window_where_it_got(self):
        pts = [Point(NOW - 600, 40.0, RESET), Point(NOW - 300, 47.0, RESET),
               Point(NOW, 42.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert (w.last_pct, w.last_t) == (47.0, NOW - 300)

    def test_drop_without_reset_times_still_marks_a_reset(self):
        pts = [Point(NOW - 600, 40.0, None), Point(NOW - 300, 47.0, None),
               Point(NOW, 42.0, None)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.points == (Point(NOW, 42.0, None),)

    def test_window_already_rolled_over(self):
        pts = [Point(NOW - 600, 70.0, NOW - 10), Point(NOW - 300, 80.0, NOW - 10)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.last_pct == 0.0
        assert w.rate_pct_per_h is None
        assert w.confidence == "none"
        assert w.points == ()
        assert w.projected_pct(NOW) is None
        assert w.eta_s(90, NOW) is None


class TestFallback:
    def test_no_points(self):
        assert estimate_window([], window="5h", now=NOW) is None

    def test_single_point_window_average(self):
        # 5h window resets in 2h: 3h elapsed, 30% used → 10%/h average.
        w = estimate_window([Point(NOW, 30.0, RESET)], window="5h", now=NOW)
        assert w.rate_pct_per_h == pytest.approx(10.0)
        assert w.confidence == "low"

    def test_span_below_min_uses_window_average(self):
        pts = [Point(NOW - 120, 10.0, RESET), Point(NOW, 30.0, RESET)]
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.rate_pct_per_h == pytest.approx(10.0)  # not the 600%/h two-point slope
        assert w.confidence == "low"

    def test_single_point_without_reset_is_unknown(self):
        w = estimate_window([Point(NOW, 30.0, None)], window="5h", now=NOW)
        assert w.rate_pct_per_h is None
        assert w.confidence == "none"

    def test_window_just_opened_is_unknown(self):
        w = estimate_window([Point(NOW, 3.0, NOW + 5 * 3600 - 300)], window="5h", now=NOW)
        assert w.rate_pct_per_h is None
        assert w.confidence == "none"

    def test_weekly_window_average(self):
        reset = NOW + 6 * 86400  # one day into the week
        w = estimate_window([Point(NOW, 24.0, reset)], window="7d", now=NOW)
        assert w.rate_pct_per_h == pytest.approx(1.0)

    def test_scoped_window_uses_week_length(self):
        reset = NOW + 6 * 86400
        w = estimate_window([Point(NOW, 24.0, reset)], window="Fable", now=NOW)
        assert w.rate_pct_per_h == pytest.approx(1.0)


class TestConfidence:
    def test_high(self):
        pts = line(10.0, start_pct=20.0, n=8, step_s=300)  # 35 min span
        assert estimate_window(pts, window="5h", now=NOW).confidence == "high"

    def test_high_needs_low_residuals(self):
        # A monotone staircase (no drop, so one segment) fits a line badly.
        stair = [Point(NOW - (7 - i) * 300, 20.0 if i < 4 else 30.0, RESET)
                 for i in range(8)]
        w = estimate_window(stair, window="5h", now=NOW)
        assert len(w.points) == 8
        assert w.confidence == "medium"

    def test_medium(self):
        pts = line(10.0, start_pct=20.0, n=3, step_s=300)  # 10 min span
        assert estimate_window(pts, window="5h", now=NOW).confidence == "medium"

    def test_low_two_points(self):
        pts = line(10.0, start_pct=20.0, n=2, step_s=300)
        assert estimate_window(pts, window="5h", now=NOW).confidence == "low"


class TestProjectionAndEta:
    def _w(self, rate=10.0, last=50.0, resets_at=RESET, conf="high"):
        return est.WindowEstimate(window="5h", last_pct=last, last_t=NOW,
                                  resets_at=resets_at, rate_pct_per_h=rate,
                                  confidence=conf, points=())

    def test_projection_anchored_on_last_observation(self):
        w = self._w()
        assert w.projected_pct(NOW) == 50.0
        assert w.projected_pct(NOW + 1800) == pytest.approx(55.0)
        assert w.projected_pct(NOW - 999) == 50.0

    def test_projection_capped_at_100(self):
        assert self._w(rate=100.0).projected_pct(NOW + 3600 * 1.9) == 100.0

    def test_projection_none_after_reset_or_unknown_rate(self):
        assert self._w().projected_pct(RESET) is None
        assert self._w(rate=None).projected_pct(NOW) is None

    def test_projection_without_reset(self):
        assert self._w(resets_at=None).projected_pct(NOW + 3600) == pytest.approx(60.0)

    def test_eta(self):
        assert self._w().eta_s(60.0, NOW) == pytest.approx(3600.0)
        assert self._w().eta_s(60.0, NOW + 600) == pytest.approx(3000.0)

    def test_eta_zero_when_already_over(self):
        assert self._w(last=95.0).eta_s(90.0, NOW) == 0.0
        assert self._w().eta_s(52.0, NOW + 3600) == 0.0  # projected past it

    def test_eta_zero_when_observed_over_even_without_rate(self):
        assert self._w(last=95.0, rate=None).eta_s(90.0, NOW) == 0.0

    def test_eta_none_when_crossing_after_reset(self):
        # 50% + 10%/h reaches 90% in 4h; the window resets in 2h.
        assert self._w().eta_s(90.0, NOW) is None

    def test_eta_none_for_unknown_or_negligible_rate(self):
        assert self._w(rate=None).eta_s(90.0, NOW) is None
        assert self._w(rate=0.05, resets_at=None).eta_s(90.0, NOW) is None
        assert self._w(rate=0.0).eta_s(90.0, NOW) is None

    def test_eta_without_reset(self):
        assert self._w(resets_at=None).eta_s(90.0, NOW) == pytest.approx(4 * 3600)

    def test_reset_before_eta_from_real_points(self):
        pts = line(10.0, start_pct=70.0, n=8, step_s=300, resets_at=NOW + 600)
        w = estimate_window(pts, window="5h", now=NOW)
        assert w.eta_s(90.0, NOW) is None  # needs ~1.5 h, resets in 10 min


class TestSinceSwitch:
    """``since`` (the last switch): a slope belongs to whoever was on duty."""

    SWITCH = NOW - 1800

    def test_old_account_stops_projecting_its_pre_switch_burn(self):
        # Burned at 30%/h until the switch, nothing measured since.
        burning = line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH)
        before = estimate_window(burning, window="5h", now=NOW)
        assert before.rate_pct_per_h == pytest.approx(30.0)
        w = estimate_window(burning, window="5h", now=NOW, since=self.SWITCH)
        assert w.rate_pct_per_h is None and w.confidence == "none"
        assert w.projected_pct(NOW) is None and w.eta_s(90.0, NOW) is None
        assert w.last_pct == pytest.approx(burning[-1].pct)  # still measured

    def test_old_account_polled_flat_after_the_switch_reads_flat(self):
        burning = line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH)
        idle = [Point(self.SWITCH + 600, burning[-1].pct, RESET),
                Point(NOW, burning[-1].pct, RESET)]
        w = estimate_window(burning + idle, window="5h", now=NOW, since=self.SWITCH)
        assert w.rate_pct_per_h == pytest.approx(0.0, abs=1e-9)
        assert w.points == (burning[-1], *idle)  # the switch moment onwards
        assert w.eta_s(90.0, NOW) is None

    def test_new_account_rate_comes_only_from_after_the_switch(self):
        flat = [Point(self.SWITCH - 3000 + i * 600, 10.0, RESET) for i in range(5)]
        rising = line(24.0, start_pct=10.0, n=6, step_s=300, end_t=NOW)
        assert rising[0].t >= self.SWITCH
        mixed = estimate_window(flat + rising, window="5h", now=NOW)
        w = estimate_window(flat + rising, window="5h", now=NOW, since=self.SWITCH)
        assert w.rate_pct_per_h == pytest.approx(24.0)
        assert mixed.rate_pct_per_h < w.rate_pct_per_h  # the flat past diluted it

    def test_new_account_jump_without_enough_points_is_unknown(self):
        flat = [Point(self.SWITCH - 3000 + i * 600, 10.0, RESET) for i in range(5)]
        jump = [Point(NOW, 25.0, RESET)]
        w = estimate_window(flat + jump, window="5h", now=NOW, since=self.SWITCH)
        assert w.rate_pct_per_h is None

    def test_idle_account_stays_idle_across_a_switch(self):
        flat = [Point(self.SWITCH - 3000 + i * 600, 10.0, RESET) for i in range(5)]
        w = estimate_window(flat, window="5h", now=NOW, since=self.SWITCH)
        assert w.rate_pct_per_h == pytest.approx(0.0, abs=1e-9)
        assert w.confidence != "none"

    def test_no_window_average_fallback_across_a_switch(self):
        reset = NOW + 3 * 3600  # opened two hours ago
        lone = [Point(NOW, 40.0, reset)]
        assert estimate_window(lone, window="5h", now=NOW).rate_pct_per_h is not None
        w = estimate_window(lone, window="5h", now=NOW, since=NOW - 600)
        assert w.rate_pct_per_h is None

    def test_switch_older_than_the_segment_changes_nothing(self):
        pts = line(12.0, start_pct=30.0, n=10, step_s=300)
        plain = estimate_window(pts, window="5h", now=NOW)
        assert estimate_window(pts, window="5h", now=NOW, since=NOW - 86400) == plain

    def test_account_estimate_passes_since_to_every_window(self):
        five = line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH)
        seven = line(2.0, start_pct=50.0, n=8, step_s=300, end_t=self.SWITCH,
                     resets_at=NOW + 3 * 86400)
        a = estimate_account({"5h": five, "7d": seven}, models=(), threshold=90.0,
                             now=NOW, since=self.SWITCH)
        assert all(w.rate_pct_per_h is None for w in a.windows.values())
        assert a.eta_threshold_s is None


class TestOnDuty:
    """Only the account in use here carries the user's burn; an idle one
    burns only from what its recent readings show (use elsewhere)."""

    SWITCH = NOW - 1800
    WEEK_RESET = NOW + 3 * 86400

    def week(self, rate, *, start, n, step_s=600.0, end_t=NOW):
        return line(rate, start_pct=start, n=n, step_s=step_s, end_t=end_t,
                    resets_at=self.WEEK_RESET)

    def test_idle_flat_recent_readings_project_flat(self):
        # Burned 2%/h on duty here until four hours ago, flat polls since:
        # an unknown on-duty flag would still fit the old burn into a 24 h
        # lookback, an idle account reads its last hour only.
        old = self.week(2.0, start=30.0, n=12, end_t=NOW - 4 * 3600)
        flat = [Point(NOW - 3 * 3600 + i * 600, old[-1].pct, self.WEEK_RESET) for i in range(19)]
        legacy = estimate_window(old + flat, window="7d", now=NOW)
        assert legacy.rate_pct_per_h > 0.1
        w = estimate_window(old + flat, window="7d", now=NOW, on_duty=False)
        assert w.rate_pct_per_h == pytest.approx(0.0, abs=1e-9)
        assert w.eta_s(90.0, NOW) is None

    def test_idle_rising_in_recent_polls_projects_that_rise(self):
        flat = [Point(NOW - 3 * 3600 + i * 600, 30.0, self.WEEK_RESET) for i in range(12)]
        rising = self.week(3.0, start=30.0, n=7)  # used on another machine
        w = estimate_window(flat + rising, window="7d", now=NOW, on_duty=False)
        assert w.rate_pct_per_h == pytest.approx(3.0, rel=0.05)
        assert w.confidence in ("medium", "high")

    def test_idle_ignores_its_on_duty_slope_after_switching_away(self):
        burning = line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH)
        w = estimate_window(burning, window="5h", now=NOW, since=self.SWITCH, on_duty=False)
        assert w.rate_pct_per_h is None and w.projected_pct(NOW) is None

    def test_idle_gets_no_window_average(self):
        lone = [Point(NOW, 40.0, NOW + 3 * 3600)]
        assert estimate_window(lone, window="5h", now=NOW, on_duty=False).rate_pct_per_h is None

    def test_on_duty_takes_the_carried_burn_right_after_a_switch(self):
        flat = [Point(self.SWITCH - 3000 + i * 600, 10.0, RESET) for i in range(5)]
        carried = estimate_window(
            line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH),
            window="5h", now=self.SWITCH,
        )
        w = estimate_window(flat, window="5h", now=NOW, since=self.SWITCH,
                            on_duty=True, carried=carried)
        assert w.rate_pct_per_h == pytest.approx(30.0)
        assert w.confidence == carried.confidence
        assert w.last_pct == 10.0

    def test_on_duty_own_slope_wins_once_it_has_one(self):
        flat = [Point(self.SWITCH - 3000 + i * 600, 10.0, RESET) for i in range(5)]
        rising = line(6.0, start_pct=10.0, n=6, step_s=300, end_t=NOW)
        carried = estimate_window(
            line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH),
            window="5h", now=self.SWITCH,
        )
        w = estimate_window(flat + rising, window="5h", now=NOW, since=self.SWITCH,
                            on_duty=True, carried=carried)
        assert w.rate_pct_per_h == pytest.approx(6.0)

    def test_carried_burn_is_the_previous_duty_up_to_the_switch(self):
        duty = {"5h": line(30.0, start_pct=40.0, n=8, step_s=300, end_t=self.SWITCH)}
        idle = {"5h": [Point(self.SWITCH - 3000 + i * 600, 5.0, RESET) for i in range(5)]}
        carried = est.carried_burn([idle, duty], since=self.SWITCH)
        assert carried["5h"].rate_pct_per_h == pytest.approx(30.0)
        assert est.carried_burn([idle, duty], since=None) == {}

    def test_screenshot_state(self):
        """The owner's screen an hour after a pace switch #2 -> #1: #1 on
        duty, flat all along (5h 0%, 7d 89%); #2 burned while on duty here,
        then kept rising from use elsewhere. #1 is flat ("ok"), #2's
        projection is its recent outside use, not its on-duty burn."""
        week2 = NOW + 6 * 86400
        week1 = NOW + 11 * 3600
        sw = NOW - 3600
        two = {
            # On duty here: 7d +2%/h until the switch, then +0.5%/h elsewhere.
            "7d": [Point(sw - 3600 + i * 600, 20.0 + i * 0.33, week2) for i in range(7)]
                  + [Point(sw + 600 + i * 600, 22.0 + i * 0.083, week2) for i in range(6)],
        }
        one = {"7d": [Point(sw - 3600 + i * 600, 89.0, week1) for i in range(13)]}
        fleet = est.estimate_fleet({"1": one, "2": two}, active="1", models=(),
                                   threshold=99.9, now=NOW, since=sw)
        r1 = fleet["1"].windows["7d"].rate_pct_per_h
        r2 = fleet["2"].windows["7d"].rate_pct_per_h
        assert r1 == pytest.approx(0.0, abs=1e-6)  # its own flat hour wins
        assert r2 == pytest.approx(0.5, rel=0.1)  # outside use, not 2%/h

    def test_fresh_switch_carries_the_burn_to_the_new_duty(self):
        sw = NOW - 60
        one = {"5h": line(30.0, start_pct=40.0, n=12, step_s=300, end_t=sw)}
        two = {"5h": [Point(sw - 3000 + i * 600, 10.0, RESET) for i in range(5)]
                     + [Point(NOW, 10.0, RESET)]}
        fleet = est.estimate_fleet({"1": one, "2": two}, active="2", models=(),
                                   threshold=99.9, now=NOW, since=sw)
        assert fleet["2"].windows["5h"].rate_pct_per_h == pytest.approx(30.0)
        assert fleet["1"].windows["5h"].rate_pct_per_h is None  # idle, nothing since


class TestAccount:
    def test_missing_series(self):
        a = estimate_account({}, models=(), threshold=90.0, now=NOW)
        assert a.windows == {} and a.binding is None
        assert a.eta_threshold_s is None and a.eta_limit_s is None
        assert a.confidence == "none"

    def test_binding_and_min_etas(self):
        five = line(20.0, start_pct=40.0, n=8, step_s=300, resets_at=NOW + 4 * 3600)
        seven = line(1.0, start_pct=70.0, n=8, step_s=300, resets_at=NOW + 3 * 86400)
        a = estimate_account({"5h": five, "7d": seven}, models=(), threshold=90.0, now=NOW)
        # 7d reads higher (70+ vs ~51), but 5h reaches 100 first (~2.5h at
        # 20%/h, before its 4h reset; 7d needs ~29h): 5h binds.
        assert a.binding.window == "5h"
        assert a.confidence == a.binding.confidence
        eta5 = a.windows["5h"].eta_s(90.0, NOW)
        eta7 = a.windows["7d"].eta_s(90.0, NOW)
        assert a.eta_threshold_s == pytest.approx(min(eta5, eta7))
        assert a.eta_limit_s == pytest.approx(
            min(a.windows["5h"].eta_s(100.0, NOW), a.windows["7d"].eta_s(100.0, NOW))
        )

    def test_seven_day_binds_when_it_crosses_first(self):
        five = line(5.0, start_pct=40.0, n=8, step_s=300, resets_at=NOW + 4 * 3600)
        seven = line(10.0, start_pct=80.0, n=8, step_s=300, resets_at=NOW + 3 * 86400)
        a = estimate_account({"5h": five, "7d": seven}, models=(), threshold=90.0, now=NOW)
        assert a.binding.window == "7d"
        assert a.eta_limit_s == pytest.approx(a.windows["7d"].eta_s(100.0, NOW))

    def test_five_hour_crossing_after_its_reset_never_binds(self):
        # 5h reads higher and climbs faster, but would reach 100 only after
        # its reset in 20m (even the threshold only in ~25m): it starts over
        # first. 7d gets there in ~11h.
        five = line(30.0, start_pct=60.0, n=8, step_s=300, resets_at=NOW + 1200)
        seven = line(2.0, start_pct=77.0, n=8, step_s=300, resets_at=NOW + 3 * 86400)
        a = estimate_account({"5h": five, "7d": seven}, models=(), threshold=90.0, now=NOW)
        assert a.windows["5h"].eta_s(100.0, NOW) is None
        assert a.binding.window == "7d"
        assert a.eta_threshold_s == pytest.approx(a.windows["7d"].eta_s(90.0, NOW))
        assert a.confidence == a.windows["7d"].confidence

    def test_model_window_binds_when_it_crosses_first(self):
        five = line(5.0, start_pct=10.0, n=8, step_s=300, resets_at=NOW + 4 * 3600)
        seven = line(1.0, start_pct=50.0, n=8, step_s=300, resets_at=NOW + 3 * 86400)
        fable = line(10.0, start_pct=85.0, n=8, step_s=300, resets_at=NOW + 2 * 86400)
        a = estimate_account({"5h": five, "7d": seven, "Fable": fable},
                             models=("fable",), threshold=90.0, now=NOW)
        assert a.binding.window == "Fable"
        assert a.eta_threshold_s == pytest.approx(a.windows["Fable"].eta_s(90.0, NOW))

    def test_no_crossing_binds_on_the_highest_level(self):
        five = line(1.0, start_pct=10.0, n=8, step_s=300, resets_at=NOW + 4 * 3600)
        seven = [Point(NOW - 600, 40.0, RESET), Point(NOW, 40.0, RESET)]
        a = estimate_account({"5h": five, "7d": seven}, models=(), threshold=90.0, now=NOW)
        assert a.eta_limit_s is None and a.eta_threshold_s is None
        assert a.binding.window == "7d"

    def test_first_crossing_picks_the_earliest_and_skips_none(self):
        from claude_rotater.estimator import first_crossing

        fast = estimate_window(line(20.0, start_pct=50.0, n=8, step_s=300,
                                    resets_at=NOW + 4 * 3600), window="5h", now=NOW)
        slow = estimate_window(line(2.0, start_pct=50.0, n=8, step_s=300,
                                    resets_at=NOW + 3 * 86400), window="7d", now=NOW)
        flat = estimate_window([Point(NOW - 600, 5.0, RESET), Point(NOW, 5.0, RESET)],
                               window="Fable", now=NOW)
        eta, win = first_crossing([flat, slow, fast], 90.0, NOW)
        assert win is fast and eta == pytest.approx(fast.eta_s(90.0, NOW))
        assert first_crossing([flat], 90.0, NOW) is None
        assert first_crossing([], 90.0, NOW) is None

    def test_binding_uses_projection_then_last_pct(self):
        rising = line(60.0, start_pct=40.0, n=8, step_s=300, end_t=NOW - 1800,
                      resets_at=NOW + 3 * 3600)
        flat_unknown = [Point(NOW, 60.0, None)]
        a = estimate_account({"5h": rising, "7d": flat_unknown}, models=(),
                             threshold=90.0, now=NOW)
        # 5h last ≈ 75 at NOW-30min, +60%/h → ≈ 100 now, beats 7d's 60.
        assert a.binding.window == "5h"

    def test_eta_none_only_when_every_window_none(self):
        flat = [Point(NOW - 600, 10.0, RESET), Point(NOW, 10.0, RESET)]
        a = estimate_account({"5h": flat, "7d": flat}, models=(), threshold=90.0, now=NOW)
        assert a.eta_threshold_s is None

    def test_scoped_windows_only_when_in_models(self):
        scoped = line(30.0, start_pct=80.0, n=8, step_s=300, resets_at=NOW + 86400)
        five = line(1.0, start_pct=10.0, n=8, step_s=300)
        series = {"5h": five, "Fable": scoped}
        plain = estimate_account(series, models=(), threshold=90.0, now=NOW)
        assert set(plain.windows) == {"5h"}
        named = estimate_account(series, models=("fable",), threshold=90.0, now=NOW)
        assert set(named.windows) == {"5h", "Fable"}
        assert named.binding.window == "Fable"
        assert plain.eta_threshold_s is None  # 5h alone never gets there
        assert named.eta_threshold_s is not None
        assert set(estimate_account(series, models=("all",), threshold=90.0,
                                    now=NOW).windows) == {"5h", "Fable"}
        assert set(estimate_account(series, models=("Opus",), threshold=90.0,
                                    now=NOW).windows) == {"5h"}

    def test_already_over_threshold(self):
        five = line(5.0, start_pct=91.0, n=4, step_s=300)
        a = estimate_account({"5h": five}, models=(), threshold=90.0, now=NOW)
        assert a.eta_threshold_s == 0.0


def test_params_constants_match_spec():
    assert est.PARAMS_5H == est.WindowParams(5400, 1800, 1800)
    assert est.PARAMS_WEEK == est.WindowParams(86400, 21600, 10800)
    assert math.isclose(est.MIN_SPAN_S, 240.0)


class TestRealReplays:
    def test_long_turn_back_dated_reading_keeps_the_whole_duty(self):
        """Work profile, Oct 22: an account on duty from 16:29 climbed ~48%
        in an hour. A statusline reading back-dated by a long turn put 47%
        just before a poll that read 42% (same reset). Read as a reset, the
        5h fit kept two points and went untrusted: no 5h estimate, and the
        row blank. Times are seconds after the switch."""
        switch = NOW - 3866  # 16:29:34, with NOW at 17:34:00
        reset = switch + 18026  # 21:30
        readings = [
            (-1, 0), (181, 3), (699, 8), (807, 9), (1079, 14), (1967, 17),
            (2283, 22), (2317, 22), (2508, 25), (2584, 26), (2622, 27),
            (2644, 28), (2670, 29), (2718, 30), (2794, 31), (2852, 32),
            (2862, 34), (2887, 33), (3010, 34), (3027, 36), (3212, 37),
            (3217, 40), (3451, 41), (3458, 47), (3491, 42), (3813, 48),
        ]
        pts = [Point(switch + dt, float(pct), reset) for dt, pct in readings]
        w = estimate_window(pts, window="5h", now=NOW, since=switch, on_duty=True)
        assert 42.0 not in [p.pct for p in w.points]
        assert w.confidence in ("medium", "high")
        assert 40.0 <= w.rate_pct_per_h <= 60.0
        eta = w.eta_s(100.0, NOW)
        assert 50 * 60 <= eta <= 80 * 60  # ~18:40, well before the 21:30 reset


class TestTypicalBurn:
    """The user's 7d spend per wall hour over the last week, every account."""

    W1 = NOW + 3 * 86400.0
    W2 = NOW + 5 * 86400.0

    def test_sums_rises_across_accounts_over_the_hours_covered(self):
        h = 3600.0
        a = {"7d": [Point(NOW - 48 * h, 10.0, self.W1), Point(NOW - 24 * h, 30.0, self.W1),
                    Point(NOW, 40.0, self.W1)]}
        b = {"7d": [Point(NOW - 12 * h, 5.0, self.W2), Point(NOW, 15.0, self.W2)]}
        # 30 + 10 points over the 48h covered.
        assert est.typical_burn([a, b], now=NOW) == pytest.approx(40.0 / 48.0)

    def test_a_reset_is_not_negative_spend(self):
        h = 3600.0
        a = {"7d": [Point(NOW - 30 * h, 80.0, self.W1), Point(NOW - 20 * h, 90.0, self.W1),
                    Point(NOW - 10 * h, 0.0, self.W2), Point(NOW, 6.0, self.W2)]}
        assert est.typical_burn([a], now=NOW) == pytest.approx(16.0 / 30.0)

    def test_a_rise_across_a_reset_is_not_counted(self):
        h = 3600.0
        a = {"7d": [Point(NOW - 30 * h, 2.0, self.W1), Point(NOW, 9.0, self.W2)]}
        assert est.typical_burn([a], now=NOW) == 0.0

    def test_reset_jitter_is_one_window(self):
        h = 3600.0
        a = {"7d": [Point(NOW - 30 * h, 2.0, self.W1), Point(NOW, 9.0, self.W1 + 30)]}
        assert est.typical_burn([a], now=NOW) == pytest.approx(7.0 / 30.0)

    def test_needs_a_day_of_readings(self):
        h = 3600.0
        a = {"7d": [Point(NOW - 10 * h, 2.0, self.W1), Point(NOW, 9.0, self.W1)]}
        assert est.typical_burn([a], now=NOW) is None
        assert est.typical_burn([], now=NOW) is None

    def test_looks_back_a_week_only(self):
        h = 3600.0
        old = Point(NOW - 9 * 24 * h, 0.0, self.W1)
        a = {"7d": [old, Point(NOW - 48 * h, 50.0, self.W1), Point(NOW, 60.0, self.W1)]}
        assert est.typical_burn([a], now=NOW) == pytest.approx(10.0 / 48.0)


class TestLastRise:
    def test_newest_rise_of_any_window(self):
        series = {
            "5h": [Point(NOW - 300, 10.0, RESET), Point(NOW - 200, 12.0, RESET),
                   Point(NOW - 100, 12.0, RESET)],
            "7d": [Point(NOW - 900, 3.0, RESET), Point(NOW - 50, 3.0, RESET)],
        }
        assert est.last_rise(series) == NOW - 200

    def test_flat_or_falling_is_no_rise(self):
        series = {"5h": [Point(NOW - 300, 10.0, RESET), Point(NOW - 100, 4.0, RESET)]}
        assert est.last_rise(series) is None
        assert est.last_rise({}) is None
