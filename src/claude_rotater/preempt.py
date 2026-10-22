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
* **Burn**: the estimator's recent slope, or the burn per hour of use when
  that is higher (a slope over wall time counts the night as burn-free),
  extrapolated over that gap.
* **One long turn**: the largest rise seen between two consecutive readings
  in the last few hours, counted only while the account is in use. A
  reading's rise is either the steady burn or one big turn landing, so the
  margin is the larger of the two, not their sum (summing would count the
  burn that makes up the jump twice and switch points earlier than needed).

Readings come in whole percents. A 1-point step between two readings is
therefore the reading's resolution, not a long turn: at 0.02%/min a weekly
window still steps 98 -> 99 between two readings. Counting that step as a
turn made every account reading 99 project to 100 and switch at once, idle
or not (measured on an owner's log: three switches at 99% with the burn
under 0.1%/min). So a rise counts as a turn only when it is larger than the
resolution, and the position inside the current whole percent is read off
when the reading stepped to it: the step lands at the midpoint between the
last reading below and the first reading at it, and the burn since then
(while the account is in use, scaled by a safety factor) has carried the
value on, at most one resolution.
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
# Readings older than this say nothing about the size of today's turns.
JUMP_LOOKBACK_S = 3 * 3600.0
# An account is in use while some window of it rose this recently (longer
# for a slow burn: see ``_quiet_limit_s``).
IN_USE_S = 30 * 60.0
# Burn per hour of use is read over this much history, and needs this much
# use behind it.
USE_LOOKBACK_S = 24 * 3600.0
MIN_USE_S = 30 * 60.0
# Whole-percent readings: a value inside [p, p + 1) reads p.
WHOLE_PCT_RESOLUTION = 1.0
# The drift inside a whole percent extrapolates the burn over up to an hour,
# where a slope read off whole-percent steps runs low (measured: 0.7%/h read
# for a 0.8%/h percent). Scaling it trades a few tenths of a percent left
# behind for not running into the limit on a low estimate.
DRIFT_SAFETY = 1.5


@dataclass(frozen=True)
class Projection:
    """Where one window is expected to be at the next reading."""

    window: str
    pct: float  # latest known utilization
    projected_pct: float
    horizon_s: float  # seconds until the next expected reading
    burn_pct_per_min: float  # 0.0 when the slope is unknown or the account idle
    jump_pct: float  # largest rise beyond the resolution between consecutive readings
    offset_pct: float = 0.0  # estimated position inside the current whole percent

    def reaches(self, target_pct: float) -> bool:
        return self.projected_pct >= target_pct

    @property
    def rise_pct(self) -> float:
        """The rise by the next reading: the user's burn or turn, which
        follows them onto another account (the offset stays behind)."""
        return self.projected_pct - self.pct - self.offset_pct

    def reason(self) -> str:
        """``pre-empt: ~100.4% by next reading in ~3m (burn +1.2%/h, turn 3%)``.

        Burn in %/h: a weekly burn in %/min rounds to a misleading 0.0."""
        minutes = max(1, round(self.horizon_s / 60.0))
        turn = f", turn {self.jump_pct:.0f}%" if self.jump_pct > 0 else ""
        return (
            f"pre-empt: ~{self.projected_pct:.1f}% by next reading in ~{minutes}m "
            f"(burn +{self.burn_pct_per_min * 60.0:.1f}%/h{turn})"
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


def resolution_of(points: Sequence[Point]) -> float:
    """The reading resolution: one whole percent when every reading is a
    whole number, else none (a source with decimals says where it is)."""
    if points and all(float(p.pct).is_integer() for p in points):
        return WHOLE_PCT_RESOLUTION
    return 0.0


def _largest_jump(
    points: Sequence[Point], *, since: float | None, now: float, resolution: float
) -> float:
    """The largest rise between consecutive readings in the last
    ``JUMP_LOOKBACK_S`` (and this duty), counting only rises larger than the
    resolution: a step of one resolution is the burn crossing a boundary."""
    start = now - JUMP_LOOKBACK_S
    if since is not None:
        start = max(start, since)
    pts = [p for p in points if p.t >= start]
    rises = (b.pct - a.pct for a, b in zip(pts, pts[1:]))
    return max((r for r in rises if r > resolution), default=0.0)


def _rise_times(points: Sequence[Point]) -> list[float]:
    """The times of the readings that rose over the one before."""
    pts = sorted(points, key=lambda p: p.t)
    return [b.t for a, b in zip(pts, pts[1:]) if b.pct > a.pct]


def last_rise_at(points: Sequence[Point]) -> float | None:
    """When this window last rose between two readings."""
    return max(_rise_times(points), default=None)


def burn_in_use(
    series: Mapping[str, Sequence[Point]], label: str, *, now: float, since: float | None
) -> float | None:
    """This window's burn per hour of use (%/h) over the last
    ``USE_LOOKBACK_S``: its rise divided by the time the account was in use
    (``IN_USE_S`` before each rise of any window, up to now while still in
    use). A slope over wall time counts the night and the lunch break as
    burn-free time and runs low the morning after; a whole percent then
    lasts far shorter than it predicts. None when too little use is seen."""
    start = now - USE_LOOKBACK_S
    if since is not None:
        start = max(start, since)
    pts = sorted((p for p in series.get(label, ()) if p.t >= start), key=lambda p: p.t)
    # Only the current window: a reset drops the reading back towards 0.
    for i in range(len(pts) - 1, 0, -1):
        if pts[i].pct < pts[i - 1].pct:
            pts = pts[i:]
            break
    if len(pts) < 2 or pts[-1].pct <= pts[0].pct:
        return None
    rises = sorted(r for points in series.values() for r in _rise_times(points)
                   if pts[0].t < r <= now)
    if not rises:
        return None
    active, lo, hi = 0.0, None, None
    for r in rises:
        a, b = max(r - IN_USE_S, pts[0].t), r
        if hi is None or a > hi:
            if hi is not None:
                active += hi - lo
            lo, hi = a, b
        else:
            hi = max(hi, b)
    active += hi - lo
    if now - rises[-1] <= IN_USE_S:
        active += now - rises[-1]
    if active < MIN_USE_S:
        return None
    return (pts[-1].pct - pts[0].pct) / (active / 3600.0)


def _quiet_limit_s(rate_pct_per_h: float | None, resolution: float) -> float:
    """How long a window may read flat while still in use: ``IN_USE_S``, or
    twice the time its burn takes to step one resolution (a weekly window at
    0.5%/h steps a whole percent every two hours)."""
    if not rate_pct_per_h or rate_pct_per_h <= 0.0 or resolution <= 0.0:
        return IN_USE_S
    return max(IN_USE_S, 2.0 * resolution / rate_pct_per_h * 3600.0)


def _in_use(
    windows: Mapping[str, tuple[Sequence[Point], float | None, float]], now: float
) -> bool:
    """Whether the account is in use. ``windows`` maps a label to its points,
    burn (%/h) and resolution. The window that rose and reacts fastest (the
    shortest quiet limit, normally 5h) decides: in use while it rose within
    its quiet limit. A window that never rose (a model the user is not on)
    says nothing. With no window that rose, in use only while the readings
    are too few to tell."""
    deciding: tuple[float, float] | None = None  # (quiet limit, last rise)
    covered = False
    for points, rate, resolution in windows.values():
        if len(points) >= 2:
            covered = True
        rose = last_rise_at(points)
        if rose is None:
            continue
        limit = _quiet_limit_s(rate, resolution)
        if deciding is None or limit < deciding[0]:
            deciding = (limit, rose)
    if deciding is None:
        return not covered
    limit, rose = deciding
    return now - rose <= limit


def _step_at(points: Sequence[Point], pct: float, live_newer: bool, now: float) -> float | None:
    """When the readings stepped up to ``pct``: the midpoint between the last
    reading below it and the first reading at or above it. None when no
    retained reading is below it."""
    pts = sorted(points, key=lambda p: p.t)
    if live_newer:
        # The live reading is the first at ``pct``; history's newest is below.
        return (pts[-1].t + now) / 2.0 if pts else None
    for i in range(len(pts) - 1, 0, -1):
        if pts[i - 1].pct < pct <= pts[i].pct:
            return (pts[i - 1].t + pts[i].t) / 2.0
    return None


def in_bucket_offset(
    points: Sequence[Point],
    pct: float,
    *,
    resolution: float,
    burn_pct_per_s: float,
    until: float,
    live_newer: bool = False,
    now: float,
) -> float:
    """How far past ``pct`` the value has likely moved inside its whole
    percent: the burn (scaled by ``DRIFT_SAFETY``) since the step to ``pct``
    up to ``until`` (now while in use, the last rise once idle), at most one
    resolution. Half a resolution when the step is not in the retained
    history."""
    if resolution <= 0.0:
        return 0.0
    stepped = _step_at(points, pct, live_newer, now)
    if stepped is None:
        return resolution / 2.0
    drift = DRIFT_SAFETY * burn_pct_per_s * (until - stepped)
    return min(max(0.0, drift), resolution)


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
    while the account has too few readings of its own since.

    While no window of the account rose lately (``_in_use``) the account is
    idle: nothing carries it past its estimated position before the next
    reading, so neither the burn nor a turn is added."""
    estimates = {
        label: estimate_window(
            points, window=label, now=now, since=since, on_duty=True,
            carried=(carried or {}).get(label),
        )
        for label, points in series.items() if points
    }
    in_use = _in_use(
        {
            label: (series[label], est.rate_pct_per_h, resolution_of(series[label]))
            for label, est in estimates.items() if est is not None
        },
        now,
    )
    rises = [r for r in (last_rise_at(series[label]) for label in estimates) if r is not None]
    rose_at = max(rises, default=None)
    best: Projection | None = None
    for label, est in estimates.items():
        points = series[label]
        if not is_relevant_window(label, models) or est is None or not est.points:
            continue
        live_pct, live_reset = live_pcts.get(label, (None, None))
        resets_at = live_reset if live_reset is not None else est.resets_at
        if resets_at is not None and resets_at <= now + horizon_s:
            continue
        live_newer = live_pct is not None and live_pct > est.last_pct
        pct = live_pct if live_newer else est.last_pct
        resolution = resolution_of(points)
        burn = max(
            0.0,
            est.rate_pct_per_h or 0.0,
            (burn_in_use(series, label, now=now, since=since) or 0.0) if in_use else 0.0,
        ) / 60.0
        offset = in_bucket_offset(
            points, pct, resolution=resolution, burn_pct_per_s=burn / 60.0,
            until=now if in_use else (rose_at or now), live_newer=live_newer, now=now,
        )
        if in_use:
            jump = _largest_jump(est.points, since=since, now=now, resolution=resolution)
            rise = max(burn * horizon_s / 60.0, jump)
        else:
            burn, jump, rise = 0.0, 0.0, 0.0
        candidate = Projection(
            window=label,
            pct=pct,
            projected_pct=pct + offset + rise,
            horizon_s=horizon_s,
            burn_pct_per_min=burn,
            jump_pct=jump,
            offset_pct=offset,
        )
        if best is None or candidate.projected_pct > best.projected_pct:
            best = candidate
    return best


def position(
    series: Mapping[str, Sequence[Point]],
    live_pcts: Mapping[str, tuple[float, float | None]],
    *,
    models: tuple[str, ...],
    now: float,
) -> float | None:
    """Where an account most likely is now, inside its whole percent: the
    reading plus its in-bucket offset, for the window projected highest.
    None when its history says nothing."""
    p = project(series, live_pcts, models=models, now=now, since=None, horizon_s=0.0)
    return None if p is None else p.pct + p.offset_pct
