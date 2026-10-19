"""Turn priced slots (:mod:`claude_rotater.costs`) into the costs view's numbers.

Three things happen here, all pure functions of their inputs:

- **Attribution.** Transcripts do not record the account, so a slot of the
  main config dir is credited to whoever rota had on duty then, from the
  ``Switched from account X to Y`` lines of ``rota.log`` (the switch history
  rota already keeps). A slot a switch falls in is split by time. A session
  profile's slots (``rota run``) are its account's outright. Still an
  approximation: a running chat may keep the old login for a while after a
  switch, and usage on other machines is never in local transcripts.
- **$ per 1%.** For every 5h and 7d window in rota's usage history whose
  span lies inside the priced range, the account's spend in that window
  against the window's peak %, fitted through the origin (ratio of sums:
  big windows weigh more, as their % is the least rounded). The spread of
  the per-window ratios gives the ± shown next to it.
- **Rollups.** Today / 7 days / 30 days, the daily series by model, the
  per-model table with a trend against the week before, and per account.
"""

from __future__ import annotations

import bisect
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from claude_rotater import costs

_logger = logging.getLogger(__name__)

DAY_S = 86400.0
WEEK_S = 7 * DAY_S
WINDOW_S = {"5h": 5 * 3600.0, "7d": WEEK_S}
MIN_WINDOW_PCT = 2.0  # below this, integer rounding swamps the ratio
MIN_WINDOW_COST = 0.5  # % moved with ~no local spend: used on another machine
TREND_FLAT = 0.10  # within ±10% the arrow is flat

_SWITCH_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - INFO - Switched from account (\S+) to (\d+)\s*$"
)
_LOG_NAMES = ("rota.log.3", "rota.log.2", "rota.log.1", "rota.log")


# -- attribution ---------------------------------------------------------------


@dataclass(frozen=True)
class Switch:
    at: float
    to: str
    frm: str | None


def switch_history(backup_dir: Path) -> list[Switch]:
    """Every switch ``rota.log`` and its rotated copies still record, oldest
    first. The log stamps local time, as the switches happened here."""
    out: list[Switch] = []
    for name in _LOG_NAMES:
        try:
            text = (backup_dir / name).read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "Switched from account" not in line:
                continue
            m = _SWITCH_RE.match(line)
            if not m:
                continue
            try:
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                continue
            frm = m.group(2) if m.group(2).isdigit() else None
            out.append(Switch(at, m.group(3), frm))
    out.sort(key=lambda s: s.at)
    return out


Span = tuple[float, float, str]  # [start, end) and the account on duty


def duty_spans(switches: Sequence[Switch], active: str | None, lo: float, hi: float) -> list[Span]:
    """Who was on duty over ``[lo, hi)``.

    Before the first recorded switch, its "from" account (or, failing that,
    the account on duty now when nothing was ever recorded). After the last
    one, its target. Unknown stretches are left out.
    """
    first = switches[0].frm if switches else active
    spans: list[Span] = []
    start, who = lo, first
    for s in switches:
        if s.at >= hi:
            break
        if s.at > start:
            if who is not None:
                spans.append((start, s.at, who))
            start = s.at
        who = s.to
    if who is not None and start < hi:
        spans.append((start, hi, who))
    return spans


def shares(slot: int, spans: Sequence[Span], starts: Sequence[float]) -> list[tuple[str, float]]:
    """``[(account, fraction)]`` of the slot ``[slot, slot + SLOT_S)``."""
    end = slot + costs.SLOT_S
    i = max(0, bisect.bisect_right(starts, slot) - 1)
    out: dict[str, float] = {}
    while i < len(spans) and spans[i][0] < end:
        a, b, who = spans[i]
        overlap = min(b, end) - max(a, slot)
        if overlap > 0:
            out[who] = out.get(who, 0.0) + overlap / costs.SLOT_S
        i += 1
    return list(out.items())


def attribute(
    rows: Iterable[costs.Row], sources: Sequence[costs.Source], spans: Sequence[Span]
) -> dict[str, list[tuple[int, float]]]:
    """``{account: [(slot, usd), ...]}``, slots ascending. Spend no span
    covers (and no session profile owns) is left out."""
    fixed = {s.key: s.account for s in sources}
    starts = [s[0] for s in spans]
    out: dict[str, dict[int, float]] = {}
    for row in rows:
        owner = fixed.get(row.source)
        parts = [(owner, 1.0)] if owner else shares(row.slot, spans, starts)
        for who, frac in parts:
            per = out.setdefault(who, {})
            per[row.slot] = per.get(row.slot, 0.0) + row.cost * frac
    return {who: sorted(per.items()) for who, per in out.items()}


# -- $ per 1% ------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    """Dollars of local spend per 1% of a window, and how sure that is."""

    dollars: float
    rel_err: float | None  # one standard error, relative; None from one window
    n: int  # windows behind it


class _Window(Protocol):  # what estimate_per_pct reads of a usage_history.WindowSummary
    resets_at: float
    peak_pct: float


def estimate_per_pct(
    spend: Sequence[tuple[int, float]],
    windows: Iterable[_Window],
    duration: float,
    lo: float,
    now: float,
) -> Estimate | None:
    """Fit spend ≈ $/1% × peak % over the windows inside ``[lo, now]``."""
    slots = [s for s, _ in spend]
    prefix = [0.0]
    for _, usd in spend:
        prefix.append(prefix[-1] + usd)

    def cost_in(a: float, b: float) -> float:
        return prefix[bisect.bisect_left(slots, b)] - prefix[bisect.bisect_left(slots, a)]

    points: list[tuple[float, float]] = []
    for w in windows:
        start = w.resets_at - duration
        if start < lo or w.peak_pct < MIN_WINDOW_PCT:
            continue
        usd = cost_in(start, min(w.resets_at, now))
        if usd < MIN_WINDOW_COST:
            continue
        points.append((usd, w.peak_pct))
    if not points:
        return None
    total_usd = sum(c for c, _ in points)
    total_pct = sum(p for _, p in points)
    ratio = total_usd / total_pct
    if len(points) < 2:
        return Estimate(ratio, None, 1)
    var = sum(p * (c / p - ratio) ** 2 for c, p in points) / total_pct
    rel = math.sqrt(var / (len(points) - 1)) / ratio if ratio > 0 else None
    return Estimate(ratio, rel, len(points))


# -- rollups -------------------------------------------------------------------


_FAMILY_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(?:\[.*\])?$")
_OLD_RE = re.compile(r"^claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)(?:-\d{8})?$")


def model_name(raw: str) -> str:
    """``claude-opus-4-7`` -> ``Opus 4.7``; ``claude-3-5-haiku-20241022`` ->
    ``Haiku 3.5``; anything else loses its ``claude-`` prefix."""
    if m := _FAMILY_RE.match(raw):
        family, major, minor = m.groups()
        return f"{family.capitalize()} {major}{'.' + minor if minor else ''}"
    if m := _OLD_RE.match(raw):
        major, minor, family = m.groups()
        return f"{family.capitalize()} {major}{'.' + minor if minor else ''}"
    return raw.removeprefix("claude-")


@dataclass(frozen=True)
class ModelLine:
    name: str
    cost: float
    share: float  # of the range's spend, 0..1
    input: int
    output: int
    cache_write: int
    cache_read: int
    trend: float | None  # last 7 days vs the 7 before, as a fraction; None: no base


@dataclass(frozen=True)
class AccountLine:
    number: str
    cost: float  # attributed spend over the range
    per_5h: Estimate | None
    per_7d: Estimate | None


@dataclass(frozen=True)
class CostReport:
    now: float
    days: tuple[tuple[float, dict[str, float]], ...]  # (local midnight, {model: usd}), oldest first
    today: float
    week: float  # last 7 x 24h
    month: float  # the whole range
    week_change: float | None  # last 7 x 24h vs the 7 before
    models: tuple[ModelLine, ...]  # costliest first
    accounts: tuple[AccountLine, ...]  # costliest first
    unattributed: float  # spend no duty span covers
    built_at: float
    took_s: float

    @property
    def top_model(self) -> ModelLine | None:
        return self.models[0] if self.models else None


def _change(now: float, prev: float) -> float | None:
    return (now - prev) / prev if prev > 0 else None


def build_report(
    cache: costs.CostCache,
    sources: Sequence[costs.Source],
    spans: Sequence[Span],
    windows_for: Callable[[str, str, float], Iterable[_Window]],
    now: float,
) -> CostReport:
    lo = costs.range_start(now)
    rows = [r for r in cache.rows if lo <= r.slot <= now]
    midnights = [costs.day_start(now, back) for back in range(costs.RANGE_DAYS - 1, -1, -1)]
    days: list[dict[str, float]] = [{} for _ in midnights]
    per_model: dict[str, list[float]] = {}  # name -> [cost, in, out, cw, cr, last7, prev7]
    week_ago, fortnight_ago = now - WEEK_S, now - 2 * WEEK_S
    today_start = midnights[-1]
    today = week = prev_week = 0.0
    for r in rows:
        name = model_name(r.model)
        i = bisect.bisect_right(midnights, r.slot) - 1
        if i >= 0:
            days[i][name] = days[i].get(name, 0.0) + r.cost
        m = per_model.setdefault(name, [0.0] * 7)
        m[0] += r.cost
        m[1] += r.input
        m[2] += r.output
        m[3] += r.cache_write
        m[4] += r.cache_read
        if r.slot >= week_ago:
            m[5] += r.cost
            week += r.cost
        elif r.slot >= fortnight_ago:
            m[6] += r.cost
            prev_week += r.cost
        if r.slot >= today_start:
            today += r.cost
    month = sum(m[0] for m in per_model.values())
    models = tuple(sorted(
        (ModelLine(name, m[0], m[0] / month if month else 0.0, int(m[1]), int(m[2]),
                   int(m[3]), int(m[4]), _change(m[5], m[6]))
         for name, m in per_model.items() if m[0] >= 0.005),
        key=lambda line: -line.cost,
    ))
    spend = attribute(rows, sources, spans)
    accounts = []
    for number, series in spend.items():
        estimates = {}
        for label, duration in WINDOW_S.items():
            try:
                estimates[label] = estimate_per_pct(
                    series, windows_for(number, label, lo), duration, lo, now
                )
            except Exception as e:  # history is best effort
                _logger.warning(f"costs: no {label} windows for account {number}: {e!r}")
                estimates[label] = None
        accounts.append(AccountLine(number, sum(c for _, c in series),
                                    estimates["5h"], estimates["7d"]))
    accounts.sort(key=lambda a: -a.cost)
    attributed = sum(a.cost for a in accounts)
    return CostReport(
        now=now,
        days=tuple(zip(midnights, days)),
        today=today,
        week=week,
        month=month,
        week_change=_change(week, prev_week),
        models=models,
        accounts=tuple(accounts),
        unattributed=max(0.0, month - attributed),
        built_at=cache.built_at,
        took_s=cache.took_s,
    )


# -- the source the view reads -----------------------------------------------


@dataclass(frozen=True)
class CostResult:
    report: CostReport | None
    note: str | None = None  # one line for the prompt: missing ccusage, an error
    ran: bool = False  # ccusage was run for this result


class CostSource:
    """What the costs view asks: the cached report now, a fresh one on demand.

    ``identities`` maps account numbers to the (email, org uuid) the usage
    history is keyed by; ``history`` is a ``UsageHistory`` (or anything with
    its ``windows`` method).
    """

    def __init__(
        self,
        backup_dir: Path,
        history,
        config_homes: Sequence[str | Path],
        *,
        active: Callable[[], str | None] = lambda: None,
        find_runner: Callable[[], list[str] | None] = costs.find_runner,
        run: Callable[[list[str], Path], dict] = costs.run_ccusage,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backup_dir = backup_dir
        self.history = history
        self.config_homes = list(config_homes)
        self.active = active
        self.find_runner = find_runner
        self.run = run
        self.clock = clock

    def _sources(self) -> list[costs.Source]:
        homes = [*self.config_homes, *costs.feed_config_dirs(self.backup_dir)]
        return costs.transcript_sources(self.backup_dir, homes)

    def _report(self, cache: costs.CostCache, sources, identities, now: float) -> CostReport:
        lo = costs.range_start(now)
        spans = duty_spans(switch_history(self.backup_dir), self.active(), lo, now)

        def windows_for(number: str, label: str, since: float):
            identity = identities.get(number)
            if identity is None or self.history is None:
                return []
            return self.history.windows(identity, label, since)

        return build_report(cache, sources, spans, windows_for, now)

    def cached(self, identities: dict[str, tuple[str, str]]) -> CostResult:
        """The last priced report, without running ccusage (None if never)."""
        cache = costs.load_cache(self.backup_dir)
        if cache is None:
            return CostResult(None)
        now = self.clock()
        return CostResult(self._report(cache, self._sources(), identities, now))

    def refresh(self, identities: dict[str, tuple[str, str]], *, force: bool = False) -> CostResult:
        """Reprice what changed (everything ``force``-d from today on) and save."""
        now = self.clock()
        sources = self._sources()
        cache = costs.load_cache(self.backup_dir)
        if not force and costs.is_current(cache, sources, now):
            return CostResult(self._report(cache, sources, identities, now))
        runner = self.find_runner()
        if runner is None:
            report = self._report(cache, sources, identities, now) if cache else None
            return CostResult(report, costs.MISSING_HINT)
        try:
            cache = costs.refresh(cache, sources, runner, now, run=self.run)
        except costs.CostError as e:
            report = self._report(cache, sources, identities, now) if cache else None
            return CostResult(report, str(e))
        costs.save_cache(self.backup_dir, cache)
        return CostResult(self._report(cache, sources, identities, now), ran=True)
