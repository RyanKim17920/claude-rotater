"""Pre-empt replayed on a real duty: a weekly window stepping through its
last percents at ~2.3%/h with whole-percent readings (an owner's work log,
2026-10-21). Each 1-point step used to count as a "long turn", so the
reading that stepped 98 -> 99 projected 100 and switched at once with the
burn under 0.1%/min; the account still had most of a percent left."""

from __future__ import annotations

import pytest

from claude_rotater.preempt import JUMP_LOOKBACK_S, project
from claude_rotater.usage_history import Point

NOW = 1_800_000_000.0
MIN = 60.0
RESET_7D = NOW + 14 * 3600 + 8 * MIN
RESET_5H = NOW + 5294.0
PREV_RESET_5H = NOW - 12714.0
SINCE = NOW - 8 * 3600 - 32 * MIN  # the switch onto this account, the night before

# (seconds before NOW, 5h %, 7d %) as the readings arrived.
DUTY = [
    (14232, 54, 90), (14051, 55, 90), (13869, 55, 90), (13688, 56, 90), (13507, 57, 90),
    (13394, 57, 90), (12886, 59, 90), (12705, 0, 91), (12510, 1, 91), (12329, 1, 91),
    (12147, 2, 91), (11966, 3, 91), (11785, 4, 91), (11603, 4, 91), (11422, 4, 91),
    (11241, 5, 91), (11059, 5, 91), (10878, 7, 92), (10697, 7, 92), (10516, 8, 92),
    (10334, 8, 92), (10153, 8, 92), (9972, 9, 92), (9620, 10, 92), (9439, 11, 92),
    (9257, 11, 92), (9076, 12, 93), (8895, 12, 93), (8714, 13, 93), (8599, 14, 93),
    (8591, 15, 93), (8243, 15, 93), (8102, 15, 93), (7921, 16, 93), (7740, 17, 93),
    (7558, 17, 93), (7377, 18, 93), (7196, 18, 94), (7014, 18, 94), (6833, 20, 94),
    (6652, 20, 94), (6471, 20, 94), (6289, 22, 94), (6108, 22, 94), (5927, 23, 94),
    (5745, 23, 94), (5701, 23, 94), (5642, 25, 94), (5609, 26, 94), (5126, 28, 95),
    (4946, 28, 95), (4933, 28, 95), (4919, 30, 95), (4906, 31, 95), (4862, 33, 96),
    (4545, 34, 96), (4430, 35, 96), (4407, 36, 96), (4327, 36, 96), (4196, 36, 96),
    (4181, 37, 96), (4088, 37, 96), (3726, 38, 97), (3546, 39, 97), (3364, 39, 97),
    (3239, 41, 97), (3117, 41, 97), (2755, 43, 98), (2638, 43, 98), (2478, 43, 98),
    (2267, 44, 98), (2262, 45, 98), (2153, 45, 98), (1973, 45, 98), (1791, 45, 98),
    (1699, 46, 98), (1168, 47, 98), (987, 47, 98), (806, 47, 98), (624, 48, 98),
    (621, 48, 98), (0, 48, 99),
]


def series(readings) -> dict[str, list[Point]]:
    five, week = [], []
    for ago, p5, p7 in readings:
        t = NOW - ago
        five.append(Point(t, float(p5), PREV_RESET_5H if ago > 12800 else RESET_5H))
        week.append(Point(t, float(p7), RESET_7D))
    return {"5h": five, "7d": week}


def at(readings, now: float, horizon_s: float = 180.0):
    return project(series(readings), {}, models=(), now=now, since=SINCE, horizon_s=horizon_s)


def test_the_step_to_99_is_not_a_turn():
    p = at(DUTY, NOW)
    assert p.window == "7d"
    assert p.jump_pct == 0.0
    assert 0.0 <= p.offset_pct < 0.4  # stepped about five minutes ago
    assert p.burn_pct_per_min * 60.0 == pytest.approx(2.3, abs=0.5)
    assert not p.reaches(99.9)


def test_the_same_burn_switches_before_the_percent_runs_out():
    # Keep the user on it: a reading every 3 minutes, 5h still climbing,
    # 7d reading 99 throughout. The switch comes once the burn since the
    # step (with the drift safety) would carry the next reading past 99.9:
    # inside the ~25 minutes this percent lasts at ~2.3%/h, not at the step.
    readings = list(DUTY)
    fired_after = None
    for k in range(1, 30):
        readings.append((-k * 180, 48 + k, 99))
        now = NOW + k * 180
        if at(readings, now).reaches(99.9):
            fired_after = k * 180
            break
    assert fired_after is not None
    assert 5 * MIN <= fired_after <= 20 * MIN


def test_an_idle_account_at_99_never_pre_empts():
    # The user stopped right after the step: 5h and 7d flat for an hour.
    readings = list(DUTY) + [(-k * 300, 48, 99) for k in range(1, 13)]
    p = at(readings, NOW + 3600)
    assert p.burn_pct_per_min == 0.0 and p.jump_pct == 0.0
    assert p.offset_pct < 0.4  # no burn accrues after the last rise
    assert not p.reaches(99.9)


def test_a_real_turn_still_counts():
    # One 3-point turn on the 5h window an hour ago, 5h now at 97.
    five = [Point(NOW - ago, pct, RESET_5H) for ago, pct in
            ((5400, 90.0), (3600, 90.0), (3420, 93.0), (600, 96.0), (0, 97.0))]
    p = project({"5h": five}, {}, models=(), now=NOW, since=None, horizon_s=120.0)
    assert p.jump_pct == 3.0
    assert p.reaches(99.9)


def test_a_turn_older_than_the_lookback_is_forgotten():
    old = JUMP_LOOKBACK_S + 600
    five = [Point(NOW - ago, pct, RESET_5H) for ago, pct in
            ((old + 300, 80.0), (old, 85.0), (1200, 95.0), (600, 95.0), (0, 96.0))]
    p = project({"5h": five}, {}, models=(), now=NOW, since=None, horizon_s=120.0)
    assert p.jump_pct == 0.0
    assert not p.reaches(99.9)


def test_decimal_readings_have_no_resolution_slack():
    # A source with decimals says where it is: no offset, and a 0.6-point
    # rise is a turn.
    five = [Point(NOW - ago, pct, RESET_5H) for ago, pct in
            ((600, 98.2), (420, 98.3), (240, 98.9), (0, 99.0))]
    p = project({"5h": five}, {}, models=(), now=NOW, since=None, horizon_s=120.0)
    assert p.offset_pct == 0.0
    assert p.jump_pct == pytest.approx(0.6)
    assert p.reaches(99.5)
