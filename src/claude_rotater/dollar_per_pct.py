"""Dollars of local spend per 1% of a rate-limit window, measured densely.

If the 5h and 7d limits shrink, a 1% of them buys less: the API-equivalent
spend behind each 1% falls. Every stretch of a window in which the reading
climbs by :data:`SEGMENT_PCT` points gives one measurement, the local spend
over that stretch divided by the climb, dated at its middle. A busy day
gives dozens of them, so the trend shows within days, not weeks.

The readings are whole percents, so one 1-point step carries up to 100%
rounding error; a stretch of ``k`` points carries at most ``1/k``. The
segment sizes were picked on real data as the smallest whose spread stops
shrinking (see :data:`SEGMENT_PCT`).

Each sample is graded, because the local transcripts are not the whole
story:

- **used elsewhere.** A rise in the reading with (almost) no local spend
  around it happened on another machine or on claude.ai, where ccusage
  cannot see it. That share of the climb is ``elsewhere``; above
  :data:`ELSEWHERE_LOW` the sample is unsure, above :data:`ELSEWHERE_OUT`
  it is left out (its ratio would read far too low).
- **lagging.** A reading lower than an earlier one of the same window is a
  stale copy (a long turn's statusline, a slow poll), never a climb; it is
  skipped, and readings of another window never mix in.

Samples are kept in a small file shared by every profile
(:data:`STORE_FILENAME` in the shared cache), so the trend outlives the
30 days of priced transcripts; past the trend span they fold into one per
day. Pure functions apart from that file.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from claude_rotater.settings import atomic_write_json

_logger = logging.getLogger(__name__)

DAY_S = 86400.0
HOUR_S = 3600.0
SLOT_S = 900.0  # costs.SLOT_S: the spend series' resolution

SEGMENT_PCT = {"5h": 4.0, "7d": 6.0}  # the climb one sample spans
LOCAL_PER_PCT = 0.10  # under $0.10 of local spend per 1% risen: not risen here
ELSEWHERE_LOW = 0.10
ELSEWHERE_OUT = 0.50
MATCH_S = 120.0  # a reading's reset within this of a window's is that window

RECENT_S = 14 * DAY_S  # the current figure comes from samples this recent
SURE_PCT = {"5h": 30.0, "7d": 10.0}  # sure climb needed for a sure figure
TREND_S = 35 * DAY_S
BIN_S = DAY_S  # the fit reads one robust value per day
TREND_MIN_N = 3  # days
TREND_MIN_SPAN_S = 14 * DAY_S  # shorter extrapolates a few noisy days to a month
TREND_HORIZON_S = 28 * DAY_S  # the change is reported per 4 weeks
FLAT_CHANGE = 0.05  # under 5% per 4 weeks reads steady, noise or not

STORE_FILENAME = "dollar_per_pct.json"
STORE_SCHEMA = 2
STORE_MAX_AGE_S = 400 * DAY_S
STORE_MAX_SAMPLES = 5000  # per account and window kind
FOLDED = 0.0  # resets_at of a day folded into one sample: no window to recompute

Spend = Sequence[tuple[int, float]]  # (slot start, usd), slots ascending


class _Point(Protocol):  # a usage_history.Point
    t: float
    pct: float
    resets_at: float | None


@dataclass(frozen=True)
class Sample:
    """One climb of a window: local spend over it, the climb, and the share
    of the climb that happened with no local spend around it."""

    t: float  # the middle of the stretch
    resets_at: float  # its window (:data:`FOLDED` for a folded day)
    usd: float
    pct: float
    elsewhere: float  # 0..1

    @property
    def ratio(self) -> float:
        return self.usd / self.pct if self.pct > 0 else 0.0

    @property
    def grade(self) -> str:
        """``sure``, ``unsure`` (partly used elsewhere) or ``out``."""
        if self.usd <= 0 or self.elsewhere > ELSEWHERE_OUT:
            return "out"
        return "unsure" if self.elsewhere > ELSEWHERE_LOW else "sure"

    def to_json(self) -> list:
        return [round(self.t), round(self.resets_at), round(self.usd, 4),
                round(self.pct, 1), round(self.elsewhere, 3)]

    @classmethod
    def from_json(cls, item: object) -> "Sample | None":
        if not (isinstance(item, list) and len(item) == 5
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in item)):
            return None
        return cls(*(float(x) for x in item))


class _Spending:
    """Spend between two times, from a slot series (prefix sums)."""

    def __init__(self, spend: Spend) -> None:
        self.slots = [s for s, _ in spend]
        self.usd = [u for _, u in spend]
        self.prefix = [0.0]
        for usd in self.usd:
            self.prefix.append(self.prefix[-1] + usd)

    def between(self, a: float, b: float) -> float:
        """Spend in the slots starting in ``[a, b)``."""
        return (self.prefix[bisect.bisect_left(self.slots, b)]
                - self.prefix[bisect.bisect_left(self.slots, a)])

    def prorated(self, a: float, b: float) -> float:
        """Spend in ``[a, b)``, a slot cut by either end counted by the share
        of it inside (its spend taken as even over its quarter hour)."""
        if b <= a:
            return 0.0
        i = bisect.bisect_right(self.slots, a - SLOT_S)  # first slot ending after a
        j = bisect.bisect_left(self.slots, b)  # past the last slot starting before b
        total = self.prefix[j] - self.prefix[i]
        for k in {i, j - 1} if i < j else ():
            start = self.slots[k]
            inside = min(b, start + SLOT_S) - max(a, start)
            total -= self.usd[k] * (1 - inside / SLOT_S)
        return total


def _window_points(
    full: Sequence[_Point], hourly: Sequence[_Point], start: float, resets_at: float,
    end: float,
) -> list[tuple[float, float]]:
    """``[(t, pct)]`` of one window, read before ``end``, oldest first,
    never falling.

    Exact readings when there are any; else the hourly peaks of the
    long-term history, each dated at its hour's end (the peak was reached
    by then) and only for hours wholly inside the window. A reading lower
    than one before it is a lagging copy and is skipped.
    """
    exact = [(p.t, p.pct) for p in full
             if start <= p.t < end
             and (p.resets_at is None or abs(p.resets_at - resets_at) <= MATCH_S)]
    points = sorted(exact) if exact else sorted(
        (p.t + HOUR_S, p.pct) for p in hourly if p.t >= start and p.t + HOUR_S <= end)
    out: list[tuple[float, float]] = []
    for t, pct in points:
        if not out or pct >= out[-1][1]:
            out.append((t, pct))
    return out


def samples(
    spend: Spend,
    full: Sequence[_Point],
    hourly: Sequence[_Point],
    resets: Iterable[float],
    duration: float,
    lo: float,
    now: float,
    covered: Callable[[float, float], bool] = lambda a, b: True,
    segment: float = SEGMENT_PCT["5h"],
) -> tuple[set[float], list[Sample]]:
    """``(windows read, their samples)``: every window that starts at or
    after ``lo`` (where the priced spend begins), has readings and is
    ``covered`` (every profile that held the account has priced its spend
    up to its last reading), cut into climbs of ``segment`` points from its
    start at 0%. A running window gives the climbs it has finished so far.
    """
    spending = _Spending(spend)
    read: set[float] = set()
    out: list[Sample] = []
    for resets_at in resets:
        start = resets_at - duration
        if start < lo or start >= now:
            continue
        points = _window_points(full, hourly, start, resets_at, min(resets_at, now + 1))
        if not points or not covered(start, points[-1][0]):
            continue
        read.add(resets_at)
        seg_t, seg_pct = start, 0.0  # where the current climb began
        rise_t, rise_pct = start, 0.0  # the last reading that rose
        elsewhere = 0.0
        for t, pct in points:
            rise = pct - rise_pct
            if rise <= 0:
                continue
            if spending.prorated(rise_t - SLOT_S, t) < LOCAL_PER_PCT * rise:
                elsewhere += rise
            rise_t, rise_pct = t, pct
            climb = pct - seg_pct
            if climb >= segment:
                out.append(Sample((seg_t + t) / 2, resets_at, spending.prorated(seg_t, t),
                                  climb, elsewhere / climb))
                seg_t, seg_pct, elsewhere = t, pct, 0.0
    return read, out


# -- what the view shows ----------------------------------------------------------


@dataclass(frozen=True)
class Fit:
    """A robust fit of log $ per 1% over time: one account, one window kind.

    The samples are pooled into one value per :data:`BIN_S` (the climb
    weighted median), and the line through those is a Theil-Sen fit, so a
    few wild samples cannot tilt it. ``steady`` when the slope is within
    two standard errors of none (or under :data:`FLAT_CHANGE` per 4 weeks):
    the line is then flat at the median, a noisy slope read as no change.
    ``provisional`` when it rests on too little (under :data:`TREND_MIN_N`
    days or :data:`TREND_MIN_SPAN_S`) or on samples partly used elsewhere.
    """

    t0: float  # the weighted mean time
    level: float  # log $ per 1% at t0
    slope: float  # log change per day
    steady: bool
    provisional: bool
    n: int  # days with samples
    span_s: float

    @property
    def change(self) -> float:
        """Change per 4 weeks (0.1 = +10%); 0 when steady."""
        return 0.0 if self.steady else math.exp(self.slope * TREND_HORIZON_S / DAY_S) - 1

    @property
    def span_change(self) -> float:
        """Change over the days it saw; 0 when steady. What a provisional
        fit can honestly say: per 4 weeks it would stretch a few days."""
        return 0.0 if self.steady else math.exp(self.slope * self.span_s / DAY_S) - 1

    def at(self, t: float) -> float:
        """$ per 1% on the line at ``t``."""
        return math.exp(self.level + (0.0 if self.steady else self.slope * (t - self.t0) / DAY_S))


def weighted_median(pairs: Iterable[tuple[float, float]]) -> float:
    """The value at half the total weight of ``[(value, weight)]``."""
    ordered = sorted(pairs)
    half = sum(w for _, w in ordered) / 2
    run = 0.0
    for value, w in ordered:
        run += w
        if run >= half:
            return value
    raise ValueError("no weight")


def _bins(pts: Sequence[Sample]) -> list[tuple[float, float, float]]:
    """``[(day, log $ per 1%, weight)]``: per bin the climb weighted median,
    dated at the climb weighted mean time, weighted by the climb."""
    groups: dict[int, list[Sample]] = {}
    for s in pts:
        groups.setdefault(int(s.t // BIN_S), []).append(s)
    out = []
    for group in groups.values():
        pct = sum(s.pct for s in group)
        x = sum(s.t * s.pct for s in group) / pct / DAY_S
        out.append((x, weighted_median((math.log(s.ratio), s.pct) for s in group), pct))
    return sorted(out)


def fit(found: Iterable[Sample], now: float) -> Fit | None:
    """The trend over the last :data:`TREND_S`: sure samples when they cover
    two days, else every sample not left out (provisional); None under two
    days."""
    recent = [s for s in found if s.t >= now - TREND_S and s.grade != "out"]
    sure = [s for s in recent if s.grade == "sure"]
    bins = _bins(sure)
    if len(bins) < 2:
        sure, bins = [], _bins(recent)
    n = len(bins)
    if n < 2:
        return None
    total = sum(w for _, _, w in bins)
    v = [w * n / total for _, _, w in bins]  # weights averaging 1
    x = [b[0] for b in bins]
    y = [b[1] for b in bins]
    xm = sum(vi * xi for vi, xi in zip(v, x)) / n
    slope = statistics.median((y[j] - y[i]) / (x[j] - x[i])
                              for i in range(n) for j in range(i + 1, n) if x[j] > x[i])
    level = weighted_median((yi - slope * (xi - xm), vi) for xi, yi, vi in zip(x, y, v))
    sxx = sum(vi * (xi - xm) ** 2 for vi, xi in zip(v, x))
    resid = sum(vi * (yi - level - slope * (xi - xm)) ** 2 for vi, xi, yi in zip(v, x, y))
    se = math.sqrt(resid / (n - 2) / sxx) if n > 2 and sxx > 0 else math.inf
    change = math.exp(slope * TREND_HORIZON_S / DAY_S) - 1
    steady = abs(slope) <= 2 * se or abs(change) < FLAT_CHANGE
    span = (x[-1] - x[0]) * DAY_S
    return Fit(
        t0=xm * DAY_S,
        level=weighted_median(zip(y, v)) if steady else level,
        slope=slope,
        steady=steady,
        provisional=(not sure or n < TREND_MIN_N or span < TREND_MIN_SPAN_S),
        n=n,
        span_s=span,
    )


Point = tuple[float, float, bool]  # (t, $ per 1%, sure)


@dataclass(frozen=True)
class PerPct:
    """An account's $ per 1% of one window kind, and how it moves."""

    dollars: float
    sure: bool  # enough recent climb not used elsewhere
    fit: Fit | None = None
    points: tuple[Point, ...] = ()  # every sample of the trend span not left out

    @property
    def trend(self) -> float | None:
        """Change per 4 weeks of a fit that is not provisional, else None."""
        return None if self.fit is None or self.fit.provisional else self.fit.change

    @property
    def steady(self) -> bool:
        return self.fit is None or self.fit.provisional or self.fit.steady


def _ratio(found: Iterable[Sample]) -> float:
    found = list(found)
    pct = sum(s.pct for s in found)
    return sum(s.usd for s in found) / pct if pct else 0.0


def summarize(found: Iterable[Sample], label: str, now: float) -> PerPct | None:
    """The figure to show: recent sure samples if they climb far enough,
    else every recent sample not left out (marked unsure); None without
    any."""
    usable = sorted((s for s in found if s.grade != "out"), key=lambda s: s.t)
    recent = [s for s in usable if s.t >= now - RECENT_S]
    if not recent:
        return None
    recent_sure = [s for s in recent if s.grade == "sure"]
    is_sure = sum(s.pct for s in recent_sure) >= SURE_PCT.get(label, SURE_PCT["5h"])
    points = tuple((s.t, s.ratio, s.grade == "sure") for s in usable if s.t >= now - TREND_S)
    return PerPct(_ratio(recent_sure if is_sure else recent), is_sure, fit(usable, now), points)


# -- the store ----------------------------------------------------------------------


Store = dict[str, dict[str, list[Sample]]]  # identity key -> label -> samples by time


def identity_key(identity: tuple[str, str]) -> str:
    return f"{identity[0]}|{identity[1]}"


def load_store(path: Path) -> Store:
    """The shared samples; empty when missing, unreadable or of another
    schema (the per-window file before it: rebuilt from the priced range)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schemaVersion") != STORE_SCHEMA:
        return {}
    out: Store = {}
    accounts = raw.get("accounts")
    for key, labels in (accounts.items() if isinstance(accounts, dict) else ()):
        if not isinstance(labels, dict):
            continue
        for label, items in labels.items():
            kept = [s for s in map(Sample.from_json, items if isinstance(items, list) else ())
                    if s is not None]
            if kept:
                out.setdefault(key, {})[label] = sorted(kept, key=lambda s: s.t)
    return out


def _fold(old: Sequence[Sample]) -> list[Sample]:
    """One sample per day for ``old``: its spend and climb summed, dated at
    the climb weighted mean, its elsewhere share climb weighted."""
    days: dict[int, list[Sample]] = {}
    for s in old:
        days.setdefault(int(s.t // DAY_S), []).append(s)
    out = []
    for day in sorted(days):
        group = days[day]
        if len(group) == 1 and group[0].resets_at == FOLDED:
            out.append(group[0])
            continue
        pct = sum(s.pct for s in group)
        out.append(Sample(sum(s.t * s.pct for s in group) / pct, FOLDED,
                          sum(s.usd for s in group), pct,
                          sum(s.elsewhere * s.pct for s in group) / pct))
    return out


def merge(store: Store, key: str, label: str, read: set[float],
          fresh: Iterable[Sample], now: float) -> bool:
    """Put ``fresh`` into ``store`` in place of every stored sample of the
    windows ``read``; fold what left the trend span into one per day and
    drop what is too old or too many. Whether anything changed."""
    before = store.get(key, {}).get(label, [])
    kept = [s for s in before if s.resets_at not in read] + list(fresh)
    kept = [s for s in kept if s.t >= now - STORE_MAX_AGE_S]
    kept.sort(key=lambda s: s.t)
    cut = bisect.bisect_left([s.t for s in kept], now - TREND_S)
    kept = (_fold(kept[:cut]) + kept[cut:])[-STORE_MAX_SAMPLES:]
    if kept == before:
        return False
    if kept:
        store.setdefault(key, {})[label] = kept
    else:
        store.get(key, {}).pop(label, None)
    return True


def save_store(path: Path, store: Store) -> None:
    data = {
        "schemaVersion": STORE_SCHEMA,
        "accounts": {
            key: {label: [s.to_json() for s in found] for label, found in labels.items() if found}
            for key, labels in store.items()
        },
    }
    try:
        atomic_write_json(path, data, compact=True)
    except OSError as e:
        _logger.warning(f"$ per 1%: store not written: {e!r}")
