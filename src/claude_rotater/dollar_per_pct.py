"""Dollars of local spend per 1% of a rate-limit window, window by window.

If the 5h and 7d limits shrink, a 1% of them buys less: the API-equivalent
spend behind each 1% falls. So every completed window of an account gives
one measurement, its spend up to its peak reading divided by that peak, and
the measurements over time are the trend.

Each window is graded, because the local transcripts are not the whole
story:

- **used elsewhere.** A rise in the reading with (almost) no local spend
  around it happened on another machine or on claude.ai, where ccusage
  cannot see it. That share of the rise is ``elsewhere``; above
  :data:`ELSEWHERE_LOW` the window is unsure, above :data:`ELSEWHERE_OUT`
  it is left out (its ratio would read far too low).
- **too small.** Under :data:`MIN_PCT` the whole-percent reading rounds
  the ratio by more than 5%.

Windows that are done are kept in a small file shared by every profile
(:data:`STORE_FILENAME` in the shared cache), so the trend outlives the
30 days of priced transcripts. Pure functions apart from that file.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from claude_rotater.settings import atomic_write_json

_logger = logging.getLogger(__name__)

DAY_S = 86400.0
HOUR_S = 3600.0
SLOT_S = 900.0  # costs.SLOT_S: the spend series' resolution

MIN_PCT = 10.0  # below this, whole-percent rounding swamps the ratio
MIN_USD = 0.5
LOCAL_PER_PCT = 0.10  # under $0.10 of local spend per 1% risen: not risen here
ELSEWHERE_LOW = 0.10
ELSEWHERE_OUT = 0.50
MATCH_S = 120.0  # a reading's reset within this of a window's is that window

RECENT_S = 14 * DAY_S  # the current figure comes from windows this recent
SURE_WINDOWS = {"5h": 3, "7d": 1}  # sure windows needed for a sure figure
TREND_S = 35 * DAY_S
TREND_MIN_N = 3
TREND_MIN_SPAN_S = 14 * DAY_S  # shorter extrapolates a few noisy days to a month
TREND_HORIZON_S = 28 * DAY_S  # the change is reported per 4 weeks
FLAT_CHANGE = 0.05  # under 5% per 4 weeks reads steady, noise or not

STORE_FILENAME = "dollar_per_pct.json"
STORE_SCHEMA = 1
STORE_MAX_AGE_S = 400 * DAY_S
STORE_MAX_WINDOWS = 3000  # per account and window kind

Spend = Sequence[tuple[int, float]]  # (slot start, usd), slots ascending


class _Point(Protocol):  # a usage_history.Point
    t: float
    pct: float
    resets_at: float | None


@dataclass(frozen=True)
class WindowCost:
    """One completed window: local spend up to its peak, the peak, and the
    share of its rise that happened with no local spend around it."""

    resets_at: float
    usd: float
    pct: float
    elsewhere: float  # 0..1

    @property
    def ratio(self) -> float:
        return self.usd / self.pct if self.pct > 0 else 0.0

    @property
    def grade(self) -> str:
        """``sure``, ``unsure`` (partly used elsewhere) or ``out``."""
        if self.pct < MIN_PCT or self.usd < MIN_USD or self.elsewhere > ELSEWHERE_OUT:
            return "out"
        return "unsure" if self.elsewhere > ELSEWHERE_LOW else "sure"

    def to_json(self) -> list:
        return [round(self.resets_at), round(self.usd, 4), round(self.pct, 1),
                round(self.elsewhere, 3)]

    @classmethod
    def from_json(cls, item: object) -> "WindowCost | None":
        if not (isinstance(item, list) and len(item) == 4
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in item)):
            return None
        return cls(*(float(x) for x in item))


class _Spending:
    """Spend between two times, from a slot series (prefix sums)."""

    def __init__(self, spend: Spend) -> None:
        self.slots = [s for s, _ in spend]
        self.prefix = [0.0]
        for _, usd in spend:
            self.prefix.append(self.prefix[-1] + usd)

    def between(self, a: float, b: float) -> float:
        """Spend in the slots starting in ``[a, b)``."""
        return (self.prefix[bisect.bisect_left(self.slots, b)]
                - self.prefix[bisect.bisect_left(self.slots, a)])


def _window_points(
    full: Sequence[_Point], hourly: Sequence[_Point], start: float, resets_at: float
) -> list[tuple[float, float]]:
    """``[(t, pct)]`` of one window, oldest first.

    Exact readings when there are any; else the hourly peaks of the
    long-term history, each dated at its hour's end (the peak was reached
    by then) and only for hours wholly inside the window.
    """
    exact = [(p.t, p.pct) for p in full
             if start <= p.t < resets_at
             and (p.resets_at is None or abs(p.resets_at - resets_at) <= MATCH_S)]
    if exact:
        return sorted(exact)
    return sorted((p.t + HOUR_S, p.pct) for p in hourly
                  if p.t >= start and p.t + HOUR_S <= resets_at)


def window_costs(
    spend: Spend,
    full: Sequence[_Point],
    hourly: Sequence[_Point],
    resets: Iterable[float],
    duration: float,
    lo: float,
    now: float,
    covered: Callable[[float, float], bool] = lambda a, b: True,
) -> list[WindowCost]:
    """A :class:`WindowCost` for every window that is done, starts at or
    after ``lo`` (where the priced spend begins) and is ``covered`` (every
    profile that held the account has priced its spend up to the peak)."""
    spending = _Spending(spend)
    out: list[WindowCost] = []
    for resets_at in resets:
        start = resets_at - duration
        if resets_at > now or start < lo:
            continue
        points = _window_points(full, hourly, start, resets_at)
        if not points:
            continue
        peak = max(pct for _, pct in points)
        if peak <= 0:
            continue
        t_peak = next(t for t, pct in points if pct >= peak)
        if not covered(start, t_peak):
            continue
        risen = elsewhere = 0.0
        prev_t, prev_pct = start, 0.0
        for t, pct in points:
            if t > t_peak:
                break
            rise = pct - prev_pct
            if rise > 0:
                risen += rise
                if spending.between(prev_t - SLOT_S, t) < LOCAL_PER_PCT * rise:
                    elsewhere += rise
                prev_t, prev_pct = t, pct
        out.append(WindowCost(resets_at, spending.between(start, t_peak), peak,
                              elsewhere / risen if risen else 0.0))
    return out


# -- what the view shows ----------------------------------------------------------


@dataclass(frozen=True)
class Fit:
    """A weighted fit of log $ per 1% over time: one account, one window kind.

    ``steady`` when the slope is within two standard errors of none (or
    under :data:`FLAT_CHANGE` per 4 weeks): the line is then flat at the
    mean, a noisy slope read as no change. ``provisional`` when it rests on
    too little (under :data:`TREND_MIN_N` windows or :data:`TREND_MIN_SPAN_S`)
    or on windows partly used elsewhere.
    """

    t0: float  # the weighted mean time
    level: float  # log $ per 1% at t0
    slope: float  # log change per day
    steady: bool
    provisional: bool
    n: int
    span_s: float

    @property
    def change(self) -> float:
        """Change per 4 weeks (0.1 = +10%); 0 when steady."""
        return 0.0 if self.steady else math.exp(self.slope * TREND_HORIZON_S / DAY_S) - 1

    def at(self, t: float) -> float:
        """$ per 1% on the line at ``t``."""
        return math.exp(self.level + (0.0 if self.steady else self.slope * (t - self.t0) / DAY_S))


def fit(windows: Iterable[WindowCost], now: float) -> Fit | None:
    """The trend over the last :data:`TREND_S`: sure windows when there are
    two, else every window not left out (provisional); None under two."""
    recent = sorted((w for w in windows
                     if w.resets_at >= now - TREND_S and w.ratio > 0 and w.grade != "out"),
                    key=lambda w: w.resets_at)
    sure = [w for w in recent if w.grade == "sure"]
    pts = sure if len(sure) >= 2 else recent
    if len(pts) < 2:
        return None
    n = len(pts)
    total = sum(w.pct for w in pts)
    v = [w.pct * n / total for w in pts]  # weights averaging 1
    x = [w.resets_at / DAY_S for w in pts]
    y = [math.log(w.ratio) for w in pts]
    xm = sum(vi * xi for vi, xi in zip(v, x)) / n
    ym = sum(vi * yi for vi, yi in zip(v, y)) / n
    sxx = sum(vi * (xi - xm) ** 2 for vi, xi in zip(v, x))
    slope = (sum(vi * (xi - xm) * (yi - ym) for vi, xi, yi in zip(v, x, y)) / sxx
             if sxx > 0 else 0.0)
    resid = sum(vi * (yi - ym - slope * (xi - xm)) ** 2 for vi, xi, yi in zip(v, x, y))
    se = math.sqrt(resid / (n - 2) / sxx) if n > 2 and sxx > 0 else math.inf
    change = math.exp(slope * TREND_HORIZON_S / DAY_S) - 1
    span = pts[-1].resets_at - pts[0].resets_at
    return Fit(
        t0=xm * DAY_S,
        level=ym,
        slope=slope,
        steady=abs(slope) <= 2 * se or abs(change) < FLAT_CHANGE,
        provisional=(pts is not sure or n < TREND_MIN_N or span < TREND_MIN_SPAN_S),
        n=n,
        span_s=span,
    )


Point = tuple[float, float, bool]  # (resets_at, $ per 1%, sure)


@dataclass(frozen=True)
class PerPct:
    """An account's $ per 1% of one window kind, and how it moves."""

    dollars: float
    sure: bool  # enough recent windows not used elsewhere
    fit: Fit | None = None
    points: tuple[Point, ...] = ()  # every window of the trend span not left out

    @property
    def trend(self) -> float | None:
        """Change per 4 weeks of a fit that is not provisional, else None."""
        return None if self.fit is None or self.fit.provisional else self.fit.change

    @property
    def steady(self) -> bool:
        return self.fit is None or self.fit.provisional or self.fit.steady


def _ratio(windows: Iterable[WindowCost]) -> float:
    windows = list(windows)
    pct = sum(w.pct for w in windows)
    return sum(w.usd for w in windows) / pct if pct else 0.0


def summarize(windows: Iterable[WindowCost], label: str, now: float) -> PerPct | None:
    """The figure to show: sure recent windows if there are enough, else
    every recent window not left out (marked unsure); None without any."""
    ordered = sorted(windows, key=lambda w: w.resets_at)
    usable = [w for w in ordered if w.grade != "out"]
    recent_sure = [w for w in usable if w.grade == "sure" and w.resets_at >= now - RECENT_S]
    recent = [w for w in usable if w.resets_at >= now - RECENT_S]
    if not recent:
        return None
    is_sure = len(recent_sure) >= SURE_WINDOWS.get(label, 1)
    points = tuple((w.resets_at, w.ratio, w.grade == "sure")
                   for w in usable if w.resets_at >= now - TREND_S)
    return PerPct(_ratio(recent_sure if is_sure else recent), is_sure, fit(usable, now), points)


# -- the store ----------------------------------------------------------------------


Store = dict[str, dict[str, dict[float, WindowCost]]]  # identity key -> label -> resets -> cost


def identity_key(identity: tuple[str, str]) -> str:
    return f"{identity[0]}|{identity[1]}"


def load_store(path: Path) -> Store:
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
            for w in map(WindowCost.from_json, items if isinstance(items, list) else ()):
                if w is not None:
                    out.setdefault(key, {}).setdefault(label, {})[w.resets_at] = w
    return out


def merge(store: Store, key: str, label: str, fresh: Iterable[WindowCost], now: float) -> bool:
    """Put ``fresh`` into ``store`` (a recomputed window replaces its old
    entry); drop what is too old or too many. Whether anything changed."""
    kept = store.setdefault(key, {}).setdefault(label, {})
    before = dict(kept)
    for w in fresh:
        kept[w.resets_at] = w
    for resets_at in sorted(kept)[:-STORE_MAX_WINDOWS]:
        del kept[resets_at]
    for resets_at in [r for r in kept if r < now - STORE_MAX_AGE_S]:
        del kept[resets_at]
    return kept != before


def save_store(path: Path, store: Store) -> None:
    data = {
        "schemaVersion": STORE_SCHEMA,
        "accounts": {
            key: {label: [w.to_json() for _, w in sorted(windows.items())]
                  for label, windows in labels.items() if windows}
            for key, labels in store.items()
        },
    }
    try:
        atomic_write_json(path, data, compact=True)
    except OSError as e:
        _logger.warning(f"$ per 1%: store not written: {e!r}")
