"""Unit tests for the shared usage-poll cadence policy."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from claude_swap import poll_policy
from claude_swap.estimator import AccountEstimate, WindowEstimate, estimate_account
from claude_swap.poll_policy import PollCadence
from claude_swap.usage_history import UsageHistory

NOW = 1_000_000.0
HALF = lambda: 0.5  # noqa: E731 — rng midpoint: jitter factor exactly 1.0
ACTIVE = poll_policy.ACTIVE_INTERVAL_S
BASE = poll_policy.SPARE_INTERVAL_S  # _plan() defaults to a spare account


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _plan(**overrides):
    kwargs = dict(
        prev_interval_s=None,
        prev_usage=None,
        new_usage=_usage(10),
        is_active=False,
        threshold=90.0,
        models=(),
        recent_429=False,
        now=NOW,
        rng=HALF,
    )
    kwargs.update(overrides)
    return poll_policy.plan_after_fetch(**kwargs)


def _estimate(eta_s: float | None, *, confidence="high", last_pct=80.0,
              rate=10.0, resets_at=None) -> AccountEstimate:
    """An AccountEstimate whose threshold ETA is exactly ``eta_s``."""
    w = WindowEstimate(window="5h", last_pct=last_pct, last_t=NOW,
                       resets_at=resets_at, rate_pct_per_h=rate,
                       confidence=confidence, points=())
    return AccountEstimate(windows={"5h": w}, binding=w, eta_threshold_s=eta_s,
                           eta_limit_s=None, confidence=confidence)


class TestBaseline:
    def test_each_role_starts_at_its_baseline(self):
        assert _plan(is_active=True)[1] == ACTIVE == 180.0
        assert _plan(is_active=False)[1] == BASE == 600.0

    def test_previous_interval_and_usage_do_not_matter_without_429(self):
        _, interval = _plan(prev_interval_s=60.0, prev_usage=_usage(5),
                            new_usage=_usage(50))
        assert interval == BASE

    def test_unknown_pct_uses_the_baseline(self):
        assert _plan(prev_interval_s=600.0, new_usage=None)[1] == BASE

    def test_kept_constant_aliases(self):
        assert poll_policy.ACTIVE_MAX_INTERVAL_S == 300.0
        assert poll_policy.CANDIDATE_DEFAULT_INTERVAL_S == BASE
        assert poll_policy.CANDIDATE_MAX_INTERVAL_S == 600.0
        assert ACTIVE == poll_policy.MIN_INTERVAL_S
        assert not hasattr(poll_policy, "MOVEMENT_DELTA_PCT")
        assert poll_policy.URGENT_BUDGET == 15
        assert poll_policy.HOURLY_BUDGET == 20


class TestApproach:
    def test_eta_divided_by_polls_before_crossing(self):
        _, interval = _plan(estimate=_estimate(600.0))
        assert interval == pytest.approx(600.0 / poll_policy.POLLS_BEFORE_CROSSING)

    def test_far_eta_is_capped_at_baseline(self):
        assert _plan(estimate=_estimate(10_000.0))[1] == BASE

    def test_candidate_floor_is_min_interval(self):
        _, interval = _plan(estimate=_estimate(30.0), is_active=False)
        assert interval == poll_policy.MIN_INTERVAL_S

    def test_active_may_go_urgent(self):
        _, interval = _plan(estimate=_estimate(30.0), is_active=True)
        assert interval == poll_policy.URGENT_INTERVAL_S

    def test_active_between_floors(self):
        _, interval = _plan(estimate=_estimate(300.0), is_active=True)
        assert interval == pytest.approx(100.0)

    def test_urgent_only_under_urgent_budget(self):
        attempts = [NOW - 3500 + i * 100 for i in range(poll_policy.URGENT_BUDGET)]
        _, interval = _plan(estimate=_estimate(30.0), is_active=True, attempts=attempts)
        assert interval == poll_policy.MIN_INTERVAL_S
        _, under = _plan(estimate=_estimate(30.0), is_active=True, attempts=attempts[1:])
        assert under == poll_policy.URGENT_INTERVAL_S

    def test_attempts_older_than_an_hour_do_not_count(self):
        attempts = [NOW - 7200 + i for i in range(40)]
        _, interval = _plan(estimate=_estimate(30.0), is_active=True, attempts=attempts)
        assert interval == poll_policy.URGENT_INTERVAL_S

    def test_recent_429_suppresses_urgency_and_floors(self):
        _, interval = _plan(estimate=_estimate(30.0), is_active=True, recent_429=True)
        # No urgent 60 s; AIMD from the 180 s active baseline floors at 360.
        assert interval == poll_policy.POST_429_MIN_INTERVAL_S

    def test_confidence_none_is_ignored(self):
        assert _plan(estimate=_estimate(30.0, confidence="none"), is_active=True)[1] == ACTIVE

    def test_low_confidence_still_tightens(self):
        _, interval = _plan(estimate=_estimate(300.0, confidence="low"), is_active=True)
        assert interval == pytest.approx(100.0)

    def test_eta_none_keeps_baseline(self):
        assert _plan(estimate=_estimate(None), is_active=True)[1] == ACTIVE

    def test_eta_zero_below_limit_goes_to_floor(self):
        _, interval = _plan(estimate=_estimate(0.0), is_active=True,
                            new_usage=_usage(92))
        assert interval == poll_policy.URGENT_INTERVAL_S

    def test_exhausted_ignores_the_approach(self):
        _, interval = _plan(estimate=_estimate(0.0, last_pct=100.0), is_active=True,
                            new_usage=_usage(100))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S


class TestCadenceSettings:
    def test_fast_baseline(self):
        assert _plan(cadence=PollCadence(active_s=15.0, spare_s=15.0))[1] == 15.0

    def test_floors_never_exceed_the_baseline(self):
        cadence = PollCadence(active_s=15.0, spare_s=15.0)
        assert _plan(cadence=cadence, estimate=_estimate(30.0))[1] == 15.0
        assert _plan(cadence=cadence, estimate=_estimate(30.0), is_active=True)[1] == 15.0

    def test_custom_urgent_interval(self):
        cadence = PollCadence(urgent_s=20.0)
        _, interval = _plan(cadence=cadence, estimate=_estimate(30.0), is_active=True)
        assert interval == 20.0

    def test_custom_hourly_budget_caps(self):
        cadence = PollCadence(spare_s=15.0, hourly_budget=4)
        attempts = [NOW - 3000, NOW - 2000, NOW - 1000, NOW]
        next_poll, _ = _plan(cadence=cadence, attempts=attempts)
        assert next_poll == pytest.approx(NOW - 3000 + 3600)

    def test_urgent_budget_scales_with_hourly_budget(self):
        assert PollCadence(hourly_budget=40).urgent_budget == 30
        assert PollCadence().urgent_budget == poll_policy.URGENT_BUDGET

    def test_from_section_defaults(self):
        assert poll_policy.cadence_from_section(None) == poll_policy.DEFAULT_CADENCE
        assert poll_policy.cadence_from_section({}) == poll_policy.DEFAULT_CADENCE

    def test_from_section_values_and_clamps(self):
        c = poll_policy.cadence_from_section({
            "pollActiveSeconds": 15, "pollSpareSeconds": 120,
            "pollUrgentSeconds": 1, "pollHourlyBudget": 40.7,
        })
        assert c == PollCadence(active_s=15.0, spare_s=120.0, urgent_s=5.0,
                                hourly_budget=40)
        c = poll_policy.cadence_from_section({"pollSpareSeconds": 99999})
        assert c.spare_s == 3600.0

    @pytest.mark.parametrize("bad", ["15", True, None, float("nan"), float("inf"), [1]])
    def test_from_section_bad_values_take_default(self, bad):
        c = poll_policy.cadence_from_section({"pollActiveSeconds": bad})
        assert c.active_s == ACTIVE

    def test_load_cadence_from_settings_file(self, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({"autoswitch": {"pollActiveSeconds": 30, "threshold": 80}})
        )
        assert poll_policy.load_cadence(tmp_path).active_s == 30.0

    def test_load_cadence_missing_or_corrupt_file(self, tmp_path):
        assert poll_policy.load_cadence(tmp_path) == poll_policy.DEFAULT_CADENCE
        (tmp_path / "settings.json").write_text("{nope")
        assert poll_policy.load_cadence(tmp_path) == poll_policy.DEFAULT_CADENCE


class TestBudgetRelease:
    def test_under_budget_is_none(self):
        assert poll_policy.budget_release_at([NOW - i for i in range(19)], NOW) is None
        assert poll_policy.budget_release_at([], NOW) is None

    def test_at_budget_waits_for_the_oldest_of_the_newest(self):
        attempts = [NOW - 3000 + i * 100 for i in range(20)]
        assert poll_policy.budget_release_at(attempts, NOW) == NOW - 3000 + 3600

    def test_over_budget_unsorted(self):
        attempts = [NOW - 100 * i for i in range(25)]
        release = poll_policy.budget_release_at(list(reversed(attempts)), NOW)
        assert release == sorted(attempts)[-20] + 3600

    def test_old_attempts_ignored(self):
        attempts = [NOW - 3600 - i for i in range(30)]
        assert poll_policy.budget_release_at(attempts, NOW) is None

    def test_release_overrides_reset_pull_in(self):
        reset_ts = NOW + 30.0
        attempts = [NOW - 1000 + i * 50 for i in range(20)]
        next_poll, _ = _plan(new_usage=_usage(40, _iso(reset_ts)), attempts=attempts)
        assert next_poll == pytest.approx(NOW - 1000 + 3600)
        assert next_poll > reset_ts + poll_policy.RESET_SLACK_S

    def test_release_overrides_urgent(self):
        attempts = [NOW - 600 + i * 30 for i in range(20)]
        next_poll, _ = _plan(estimate=_estimate(30.0), is_active=True, attempts=attempts)
        assert next_poll >= NOW - 600 + 3600


class TestPost429Floor:
    def test_recent_429_floors_the_cadence(self):
        _, interval = _plan(recent_429=True, prev_usage=_usage(10))
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_slower_learned_cadence_survives_the_floor(self):
        _, interval = _plan(recent_429=True, prev_interval_s=590.0)
        assert interval == pytest.approx(590.0 * poll_policy.POST_429_BACKOFF_MULT)

    def test_429_beats_a_fast_custom_baseline(self):
        _, interval = _plan(recent_429=True, cadence=PollCadence(active_s=15.0, spare_s=15.0))
        assert interval == poll_policy.POST_429_MIN_INTERVAL_S

    def test_429_base_defaults_to_the_cadence_baseline(self):
        _, interval = _plan(recent_429=True, cadence=PollCadence(spare_s=1000.0))
        assert interval == pytest.approx(1500.0)


class TestPost429Aimd:
    """AIMD backoff on a contended token: while 429s recur, each successful
    poll multiplicatively increases the interval toward a wider 429 ceiling, so
    independent machines sharing one token each retreat and their combined poll
    rate converges under the endpoint budget (no cross-machine coordination)."""

    def test_recent_429_multiplicatively_increases_from_prev(self):
        _, interval = _plan(recent_429=True, is_active=True,
                            prev_interval_s=poll_policy.POST_429_MIN_INTERVAL_S)
        assert interval == pytest.approx(
            poll_policy.POST_429_MIN_INTERVAL_S * poll_policy.POST_429_BACKOFF_MULT
        )

    def test_recent_429_ceiling(self):
        assert poll_policy.POST_429_MAX_INTERVAL_S > poll_policy.CANDIDATE_MAX_INTERVAL_S
        _, interval = _plan(recent_429=True,
                            prev_interval_s=poll_policy.POST_429_MAX_INTERVAL_S)
        assert interval == poll_policy.POST_429_MAX_INTERVAL_S

    def test_no_429_returns_to_baseline(self):
        _, interval = _plan(recent_429=False, prev_interval_s=1800.0)
        assert interval == BASE

    def test_sustained_429_grows_the_interval_to_the_wide_ceiling(self):
        prev = None
        traj = []
        for _ in range(12):
            _, interval = _plan(recent_429=True, prev_interval_s=prev)
            traj.append(interval)
            prev = interval
        assert traj == sorted(traj)
        assert traj[-1] == poll_policy.POST_429_MAX_INTERVAL_S
        for a, b in zip(traj, traj[1:]):
            if b < poll_policy.POST_429_MAX_INTERVAL_S:
                assert b == pytest.approx(a * poll_policy.POST_429_BACKOFF_MULT)

    def test_429_beats_the_approach_shrink(self):
        _, interval = _plan(recent_429=True, prev_interval_s=600.0,
                            estimate=_estimate(30.0), is_active=True)
        assert interval == pytest.approx(900.0)

    def test_429_with_exhausted(self):
        _, interval = _plan(recent_429=True, prev_interval_s=360.0,
                            new_usage=_usage(100))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S  # max(600, 540)


class TestResetCapping:
    def test_poll_never_scheduled_past_a_future_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(40, _iso(reset_ts)))
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)
        assert interval == BASE

    def test_at_limit_keeps_bounded_polling_before_distant_reset(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, _iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)

    def test_at_limit_poll_is_pulled_to_an_imminent_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(100, _iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)

    @pytest.mark.parametrize("reset_ts", [NOW - 90.0, NOW])
    def test_at_limit_ignores_non_future_reset(self, reset_ts):
        next_poll, interval = _plan(new_usage=_usage(100, _iso(reset_ts)))
        assert next_poll == pytest.approx(NOW + interval)

    def test_active_at_limit_uses_same_bounded_recovery_probe(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, _iso(reset_ts)), is_active=True)
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)


@pytest.fixture
def real_jitter(monkeypatch):
    # conftest zeroes jitter suite-wide; these tests exercise it.
    monkeypatch.setattr("claude_swap.poll_policy.JITTER_FRAC", 0.1)


@pytest.mark.usefixtures("real_jitter")
class TestJitter:
    def test_jitter_bounds(self):
        early, _ = _plan(rng=lambda: 0.0)
        late, _ = _plan(rng=lambda: 1.0)
        assert early == pytest.approx(NOW + BASE * 0.9)
        assert late == pytest.approx(NOW + BASE * 1.1)

    def test_jitter_applies_to_approach_interval(self):
        early, interval = _plan(rng=lambda: 0.0, estimate=_estimate(600.0))
        assert early == pytest.approx(NOW + interval * 0.9)


class TestBudgetInvariants:
    """Relationships the measured rate limit (~28-30 requests per trailing
    hour per identity) demands of the default constants."""

    def test_baselines_fit_the_hourly_budget(self):
        assert 3600 / ACTIVE <= poll_policy.HOURLY_BUDGET
        assert 3600 / BASE <= poll_policy.HOURLY_BUDGET

    def test_hourly_budget_leaves_headroom_under_the_cap(self):
        assert poll_policy.HOURLY_BUDGET <= 20
        assert poll_policy.URGENT_BUDGET < poll_policy.HOURLY_BUDGET

    def test_edge_backoff_probes_slower_than_capacity_frees(self):
        assert poll_policy.EDGE_BACKOFF_S >= 300.0

    def test_post_429_floor_covers_the_saturation_horizon(self):
        assert poll_policy.RECENT_429_WINDOW_S >= 3600.0
        assert poll_policy.POST_429_MIN_INTERVAL_S >= poll_policy.MIN_INTERVAL_S


@pytest.mark.usefixtures("real_jitter")
class TestSimulation:
    """A 3 h synthetic burn: the active account crosses the threshold at
    2.5 h, two candidates idle along. Every fetch writes the real history,
    every plan reads it back, exactly as the switcher does."""

    T0 = 1_800_000_000.0
    THRESHOLD = 90.0
    CROSS_T = T0 + 2.5 * 3600

    def _pct(self, num: str, t: float) -> int:
        h = (t - self.T0) / 3600.0
        if num == "1":  # 27.5% + 25%/h → 90% at 2.5 h
            return int(min(100.0, 27.5 + 25.0 * h))
        if num == "2":
            return int(10 + 2.0 * h)
        return 5

    def _run(self, tmp_path, cadence=poll_policy.DEFAULT_CADENCE, jitter=HALF):
        clock_t = [self.T0]
        hist = UsageHistory(tmp_path / "cache", clock=lambda: clock_t[0])
        ids = {n: (f"{n}@x.com", "org") for n in ("1", "2", "3")}
        reset = _iso(self.T0 + 4 * 3600)
        next_poll = {n: self.T0 for n in ids}
        prev_int: dict[str, float | None] = {n: None for n in ids}
        polls: dict[str, list[float]] = {n: [] for n in ids}
        end = self.T0 + 3 * 3600
        while True:
            num = min(next_poll, key=next_poll.get)
            t = next_poll[num]
            if t > end:
                break
            clock_t[0] = t
            usage = _usage(self._pct(num, t), reset)
            hist.record({num: usage}, ids, t)
            hist.record_attempts([num], ids, t)
            polls[num].append(t)
            series = hist.series({num: ids[num]})[num]
            est = estimate_account(series, models=(), threshold=self.THRESHOLD, now=t)
            nxt, interval = poll_policy.plan_after_fetch(
                prev_interval_s=prev_int[num], prev_usage=None, new_usage=usage,
                is_active=(num == "1"), threshold=self.THRESHOLD, models=(),
                recent_429=False, now=t, rng=jitter, estimate=est,
                attempts=hist.attempts(num, ids[num], t - 3600), cadence=cadence,
            )
            assert nxt > t
            next_poll[num], prev_int[num] = nxt, interval
        return polls

    @staticmethod
    def _max_in_any_hour(times: list[float]) -> int:
        return max(sum(1 for u in times if t - 3600 < u <= t) for t in times)

    @pytest.mark.parametrize("jitter", [HALF, lambda: 0.0, lambda: 1.0])
    def test_budget_and_sampling_before_crossing(self, tmp_path, jitter):
        polls = self._run(tmp_path, jitter=jitter)
        for num, times in polls.items():
            assert self._max_in_any_hour(times) <= poll_policy.HOURLY_BUDGET, num
        before = [t for t in polls["1"] if self.CROSS_T - 900 <= t < self.CROSS_T]
        assert len(before) >= 2
        # The active account tightened near the crossing; idle ones did not.
        assert len(polls["1"]) > len(polls["3"])
        assert self._max_in_any_hour(polls["3"]) <= 3600 / (BASE * 0.9) + 1

    def test_fast_custom_cadence_still_capped_by_its_budget(self, tmp_path):
        # A 20 s baseline wants 180/h; the budget of 60 must hold anyway.
        cadence = PollCadence(active_s=20.0, spare_s=20.0, urgent_s=10.0,
                              hourly_budget=60)
        polls = self._run(tmp_path, cadence=cadence)
        for times in polls.values():
            assert self._max_in_any_hour(times) <= 60
        assert self._max_in_any_hour(polls["3"]) == 60
