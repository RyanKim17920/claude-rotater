"""Pre-emptive switching: will the account on duty be over its limit by the
time the next reading could say so? Pure math over usage history, no I/O.

The engine only learns usage from readings (a statusline feed record or a
poll). Acting on "the last reading is over the line" is therefore one reading
late by construction: the reading that shows 100% is the one the user's turn
already failed on. Measured on an owner's log, a 5h window went 96% -> 99%
(a 3-point turn) -> 100% one reading later, with no reading in between at
99.9%, so a 99.9% threshold could never fire before the limit did.

So at each evaluation, project the active account's usage to the moment of
its NEXT expected reading and switch now when that projection reaches the
switch point. Three ingredients, all from this account's own history since
the last switch (the burn before it was another account's duty):

* **When** the next reading comes: the typical gap between recent readings
  while they keep arriving (a live feed), never later than the next poll;
  the next poll alone once readings stopped (a stale feed).
* **Burn**: the estimator's recent slope, extrapolated over that gap.
* **One long turn**: the largest rise seen between two consecutive readings.
  A reading's rise is either the steady burn or one big turn landing, so the
  margin is the larger of the two, not their sum (summing would count the
  burn that makes up the jump twice and switch points earlier than needed).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from claude_rotater.estimator import WindowEstimate, estimate_window, is_relevant_window
from claude_rotater.usage_history import Point

# Readings older than this say nothing about the current cadence.
GAP_LOOKBACK_S = 30 * 60.0
# How many recent gaps make the "typical" one (median: one long lunch break
# or one burst of re-renders does not move it).
GAP_SAMPLES = 8
# A typical gap needs at least this many gaps behind it.
MIN_GAPS = 2


@dataclass(frozen=True)
class Projection:
    """Where one window is expected to be at the next reading."""

    window: str
    pct: float  # latest known utilization
    projected_pct: float
    horizon_s: float  # seconds until the next expected reading
    burn_pct_per_min: float  # 0.0 when the slope is unknown
    jump_pct: float  # largest rise between consecutive readings

    def reaches(self, target_pct: float) -> bool:
        return self.projected_pct >= target_pct

    def reason(self) -> str:
        """``pre-empt: ~100.4% by next reading in ~3m (burn +1.2%/m)``."""
        minutes = max(1, round(self.horizon_s / 60.0))
        return (
            f"pre-empt: ~{self.projected_pct:.1f}% by next reading in ~{minutes}m "
            f"(burn +{self.burn_pct_per_min:.1f}%/m)"
        )


def switch_point(threshold: float) -> float:
    """The level a projection must not reach: the threshold, or the hard
    limit when the threshold is set at/over it."""
    return min(threshold, 100.0)


def next_reading_in(
    times: Sequence[float],
    *,
    now: float,
    since: float | None,
    poll_in_s: float,
    feed_stale_s: float,
) -> float:
    """Seconds until the next reading is expected.

    ``times`` are this account's reading times (any order). While readings
    keep arriving (the newest within ``feed_stale_s``), the median of the
    recent gaps, capped at the next poll; otherwise the next poll."""
    start = now - GAP_LOOKBACK_S
    if since is not None:
        start = max(start, since)
    recent = sorted(t for t in times if start <= t <= now)
    if not recent or now - recent[-1] > feed_stale_s:
        return poll_in_s
    gaps = [b - a for a, b in zip(recent, recent[1:]) if b > a][-GAP_SAMPLES:]
    if len(gaps) < MIN_GAPS:
        return poll_in_s
    return min(statistics.median(gaps), poll_in_s)


def _largest_jump(points: Sequence[Point], since: float | None) -> float:
    pts = [p for p in points if since is None or p.t >= since]
    return max((b.pct - a.pct for a, b in zip(pts, pts[1:])), default=0.0)


def project(
    series: Mapping[str, Sequence[Point]],
    live_pcts: Mapping[str, tuple[float, float | None]],
    *,
    models: tuple[str, ...],
    now: float,
    since: float | None,
    horizon_s: float,
    carried: Mapping[str, WindowEstimate] | None = None,
) -> Projection | None:
    """The relevant window projected highest at ``now + horizon_s``, or None
    when no window has enough history to say anything.

    ``series`` is this account's ``{label: [Point]}`` history; ``live_pcts``
    its decision reading ``{label: (pct, resets_at_epoch | None)}``, which may
    be newer than the history. A window that resets before the next reading
    is skipped: it will be emptier then, not fuller. ``carried`` is the
    user's burn up to the last switch (``estimator.carried_burn``), taken
    while the account has too few readings of its own since."""
    best: Projection | None = None
    for label, points in series.items():
        if not is_relevant_window(label, models) or not points:
            continue
        est = estimate_window(
            points, window=label, now=now, since=since, on_duty=True,
            carried=(carried or {}).get(label),
        )
        if est is None or not est.points:
            continue
        live_pct, live_reset = live_pcts.get(label, (None, None))
        resets_at = live_reset if live_reset is not None else est.resets_at
        if resets_at is not None and resets_at <= now + horizon_s:
            continue
        pct = est.last_pct if live_pct is None else max(est.last_pct, live_pct)
        burn = (est.rate_pct_per_h or 0.0) / 60.0
        jump = max(0.0, _largest_jump(est.points, since))
        rise = max(burn * horizon_s / 60.0, jump)
        candidate = Projection(
            window=label,
            pct=pct,
            projected_pct=pct + rise,
            horizon_s=horizon_s,
            burn_pct_per_min=burn,
            jump_pct=jump,
        )
        if best is None or candidate.projected_pct > best.projected_pct:
            best = candidate
    return best
