"""Burn-rate estimation over usage history. Pure math, no I/O.

Given the measured points of one usage window (``usage_history.Point``), say
how fast it is filling, where it will be at a given moment, and when it will
cross a target percentage. The poll planner uses the threshold ETA to sample
more often as an account approaches its switch point; the TUI uses the same
estimate to draw projections.

Measurements are integer percentages taken minutes apart, so a slope from two
close points is mostly quantization noise. The estimator therefore fits an
exponentially weighted least-squares line over a recent lookback, refuses to
fit spans shorter than ``MIN_SPAN_S``, and reports a confidence tier the
callers use to decide how much to trust it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from claude_swap.usage_history import Point

WINDOW_LEN_S = {"5h": 5 * 3600.0, "7d": 7 * 86400.0}
SCOPED_WINDOW_LEN_S = 7 * 86400.0  # per-model windows are weekly


@dataclass(frozen=True)
class WindowParams:
    lookback_s: float
    half_life_s: float
    high_span_s: float


PARAMS_5H = WindowParams(lookback_s=90 * 60, half_life_s=30 * 60, high_span_s=30 * 60)
PARAMS_WEEK = WindowParams(lookback_s=24 * 3600, half_life_s=6 * 3600, high_span_s=3 * 3600)
MIN_SPAN_S = 240.0  # below this two points are noise (integer pct quantization)
RESET_DROP_PCT = 2.0  # a pct drop larger than this between points means the window reset
RESET_MATCH_S = 120.0  # resets_at jitter tolerated within one window
MAX_RATE_PCT_PER_H = 200.0
MIN_FALLBACK_ELAPSED_S = 600.0  # window-average burn needs this much window behind it
MIN_ETA_RATE_PCT_PER_H = 0.05  # slower than this never crosses anything useful
HIGH_RMS_PCT = 2.0

Confidence = Literal["none", "low", "medium", "high"]


def window_len_s(window: str) -> float:
    return WINDOW_LEN_S.get(window, SCOPED_WINDOW_LEN_S)


def window_params(window: str) -> WindowParams:
    return PARAMS_5H if window == "5h" else PARAMS_WEEK


@dataclass(frozen=True)
class WindowEstimate:
    window: str
    last_pct: float
    last_t: float
    resets_at: float | None
    rate_pct_per_h: float | None  # None means unknown
    confidence: Confidence
    points: tuple[Point, ...]  # the current-window segment used (for plotting)

    def projected_pct(self, t: float) -> float | None:
        """Projected utilization at ``t``, anchored on the last observation.

        None when the rate is unknown or ``t`` is at/after the reset (the
        window there is new and unknown). Times before ``last_t`` read as
        the last observation."""
        if self.rate_pct_per_h is None:
            return None
        if self.resets_at is not None and t >= self.resets_at:
            return None
        dt = max(0.0, t - self.last_t)
        return min(100.0, self.last_pct + self.rate_pct_per_h * dt / 3600.0)

    def eta_s(self, target_pct: float, now: float) -> float | None:
        """Seconds from ``now`` until the window reaches ``target_pct``.

        0 when it is already there, None when it never gets there before
        its reset (or the rate is unknown or negligible)."""
        window_live = self.resets_at is None or now < self.resets_at
        projected = self.projected_pct(now)
        if projected is not None and projected >= target_pct:
            return 0.0
        # Observed at/over the target needs no rate to be "already there".
        if window_live and self.last_pct >= target_pct:
            return 0.0
        rate = self.rate_pct_per_h
        if rate is None or rate <= MIN_ETA_RATE_PCT_PER_H:
            return None
        t_star = self.last_t + (target_pct - self.last_pct) / rate * 3600.0
        if self.resets_at is not None and t_star >= self.resets_at:
            return None
        return max(0.0, t_star - now)


def _segment(points: Sequence[Point], newest: Point, lookback_start: float) -> list[Point]:
    """The newest point plus every older point in the same window instance,
    within the lookback, oldest first."""
    seg = [newest]
    for p in reversed(points[:-1]):
        nxt = seg[0]
        if p.t < lookback_start:
            break
        if (
            p.resets_at is not None
            and newest.resets_at is not None
            and abs(p.resets_at - newest.resets_at) > RESET_MATCH_S
        ):
            break
        if p.pct > nxt.pct + RESET_DROP_PCT:
            break
        seg.insert(0, p)
    return seg


def _ewls(seg: list[Point], now: float, half_life_s: float) -> tuple[float, float] | None:
    """``(slope_pct_per_h, weighted_rms_pct)`` or None when degenerate."""
    weights = [0.5 ** ((now - p.t) / half_life_s) for p in seg]
    sw = sum(weights)
    if sw <= 0:
        return None
    t_bar = sum(w * p.t for w, p in zip(weights, seg)) / sw
    p_bar = sum(w * p.pct for w, p in zip(weights, seg)) / sw
    sxx = sum(w * (p.t - t_bar) ** 2 for w, p in zip(weights, seg))
    if sxx <= 0:
        return None
    slope = sum(w * (p.t - t_bar) * (p.pct - p_bar) for w, p in zip(weights, seg)) / sxx
    resid = [p.pct - (p_bar + slope * (p.t - t_bar)) for p in seg]
    rms = math.sqrt(sum(w * r * r for w, r in zip(weights, resid)) / sw)
    return slope * 3600.0, rms


def _clamp_rate(rate: float) -> float:
    # Usage inside one window never decreases: a negative fit is noise.
    return min(max(rate, 0.0), MAX_RATE_PCT_PER_H)


def estimate_window(
    points: Sequence[Point], *, window: str, now: float
) -> WindowEstimate | None:
    """Estimate one window from its measured points (any order). None when
    there are no points."""
    if not points:
        return None
    pts = sorted(points, key=lambda p: p.t)
    newest = pts[-1]
    if newest.resets_at is not None and newest.resets_at <= now:
        # Rolled over since the last poll: the new window is unmeasured.
        return WindowEstimate(
            window=window, last_pct=0.0, last_t=newest.t, resets_at=newest.resets_at,
            rate_pct_per_h=None, confidence="none", points=(),
        )
    params = window_params(window)
    seg = _segment(pts, newest, now - params.lookback_s)
    span = seg[-1].t - seg[0].t
    rate: float | None = None
    confidence: Confidence = "none"
    fit = _ewls(seg, now, params.half_life_s) if len(seg) >= 2 and span >= MIN_SPAN_S else None
    if fit is not None:
        slope, rms = fit
        rate = _clamp_rate(slope)
        n = len(seg)
        if n >= 4 and span >= params.high_span_s and rms <= HIGH_RMS_PCT:
            confidence = "high"
        elif n >= 3 and span >= 2 * MIN_SPAN_S:
            confidence = "medium"
        else:
            confidence = "low"
    elif newest.resets_at is not None:
        # Average burn since the window opened.
        elapsed = newest.t - (newest.resets_at - window_len_s(window))
        if elapsed >= MIN_FALLBACK_ELAPSED_S:
            rate = _clamp_rate(newest.pct / elapsed * 3600.0)
            confidence = "low"
    return WindowEstimate(
        window=window, last_pct=newest.pct, last_t=newest.t,
        resets_at=newest.resets_at, rate_pct_per_h=rate,
        confidence=confidence, points=tuple(seg),
    )


@dataclass(frozen=True)
class AccountEstimate:
    windows: dict[str, WindowEstimate]  # only relevant windows (respect `models`)
    binding: WindowEstimate | None  # max projected_pct(now)
    eta_threshold_s: float | None  # min over windows
    eta_limit_s: float | None  # min over windows to 100
    confidence: Confidence  # binding window's confidence


EMPTY_ACCOUNT_ESTIMATE = AccountEstimate(
    windows={}, binding=None, eta_threshold_s=None, eta_limit_s=None, confidence="none"
)


def is_relevant_window(label: str, models: tuple[str, ...]) -> bool:
    """Same window view as ``oauth.relevant_windows``: 5h and 7d always,
    scoped windows only when named in ``models`` (or ``all``)."""
    if label in WINDOW_LEN_S:
        return True
    wanted = {m.lower() for m in models}
    return "all" in wanted or label.lower() in wanted


def _min_or_none(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return min(known) if known else None


def estimate_account(
    series: dict[str, list[Point]],
    *,
    models: tuple[str, ...],
    threshold: float,
    now: float,
) -> AccountEstimate:
    windows: dict[str, WindowEstimate] = {}
    for label, points in series.items():
        if not is_relevant_window(label, models):
            continue
        est = estimate_window(points, window=label, now=now)
        if est is not None:
            windows[label] = est
    if not windows:
        return EMPTY_ACCOUNT_ESTIMATE

    def level(est: WindowEstimate) -> float:
        projected = est.projected_pct(now)
        return est.last_pct if projected is None else projected

    binding = max(windows.values(), key=level)
    return AccountEstimate(
        windows=windows,
        binding=binding,
        eta_threshold_s=_min_or_none([w.eta_s(threshold, now) for w in windows.values()]),
        eta_limit_s=_min_or_none([w.eta_s(100.0, now) for w in windows.values()]),
        confidence=binding.confidence,
    )
