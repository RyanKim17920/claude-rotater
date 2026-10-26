"""Turn priced slots (:mod:`claude_rotater.costs`) into the costs view's numbers.

Three things happen here, all pure functions of their inputs:

- **Attribution.** Transcripts do not record the account, so a slot of the
  main config dir is credited to whoever was on duty then, from the
  evidence :mod:`claude_rotater.attribution` gathers (switch logs, logins
  recorded in transcripts). A slot a switch falls in is split by time; what
  no evidence covers stays unattributed (key ``""``), never credited by
  guess. A session profile's slots (``rota run``) are its account's
  outright. Still an approximation: a running chat may keep the old login
  for a while after a switch, and usage on other machines is never in
  local transcripts.
- **$ per 1%.** An account's spend over every profile that holds it (one
  account may sit in two profiles: their transcripts are separate), climb
  by climb of its readings; :mod:`claude_rotater.dollar_per_pct` cuts the
  windows into samples, grades them and keeps them for the trend.
- **Rollups.** Today / 7 days / 30 days, the daily series by model (and
  every older day the cache folded), the last week hour by hour with who
  was on duty, the per-model table with a
  trend against the week before, and per account.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import attribution, costs, profiles
from claude_rotater import dollar_per_pct as dpp
from claude_rotater.paths import get_default_backup_root, get_shared_cache_dir

_logger = logging.getLogger(__name__)

DAY_S = 86400.0
HOUR_S = 3600.0
WEEK_S = 7 * DAY_S
WINDOW_S = {"5h": 5 * HOUR_S, "7d": WEEK_S}
RECENT_HOURS = 7 * 24  # the hourly series: the weekly and hourly charts

Identity = tuple[str, str]  # (email, organization uuid), as the usage history keys it
Cell = tuple[str, str]  # (account key, model name)



# -- attribution ---------------------------------------------------------------


Span = attribution.Span  # [start, end) and the account key on duty


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


UNATTRIBUTED = ""  # the account key of spend no evidence credits to anyone


def parts(row: costs.Row, fixed: dict[str, str | None], spans: Sequence[Span],
          starts: Sequence[float]) -> list[tuple[str, float]]:
    """``[(account, fraction)]`` of ``row``'s spend, summing to one: a
    session profile's outright, else by who was on duty in its slot, what
    nobody covers as :data:`UNATTRIBUTED`."""
    owner = fixed.get(row.source)
    if owner:
        return [(owner, 1.0)]
    out = shares(row.slot, spans, starts)
    rest = 1.0 - sum(f for _, f in out)
    if rest > 1e-9:
        out.append((UNATTRIBUTED, rest))
    return out


def attribute(
    rows: Iterable[costs.Row], sources: Sequence[costs.Source], spans: Sequence[Span]
) -> dict[str, list[tuple[int, float]]]:
    """``{account: [(slot, usd), ...]}``, slots ascending; what no span
    covers (and no session profile owns) under :data:`UNATTRIBUTED`."""
    fixed = {s.key: s.account for s in sources}
    starts = [s[0] for s in spans]
    out: dict[str, dict[int, float]] = {}
    for row in rows:
        for who, frac in parts(row, fixed, spans, starts):
            per = out.setdefault(who, {})
            per[row.slot] = per.get(row.slot, 0.0) + row.cost * frac
    return {who: sorted(per.items()) for who, per in out.items()}


def majority(spans: Sequence[Span], a: float, b: float) -> str | None:
    """The account on duty for most of ``[a, b)``, None when nobody was."""
    time_on: dict[str, float] = {}
    for start, end, who in spans:
        overlap = min(end, b) - max(start, a)
        if overlap > 0:
            time_on[who] = time_on.get(who, 0.0) + overlap
    return max(time_on, key=time_on.__getitem__) if time_on else None


# -- one account across profiles -----------------------------------------------


@dataclass(frozen=True)
class ProfileSpend:
    """What one profile knows of its accounts' spend.

    ``built_at`` and ``lo`` bound the priced transcripts; spend while an
    account was on duty outside them is not known yet, nor is spend no
    evidence credits to anyone (``spend[UNATTRIBUTED]``).
    """

    numbers: dict[Identity, str]  # account identity -> its number here
    spend: dict[str, list[tuple[int, float]]]  # account key -> (slot, usd)
    spans: tuple[Span, ...]
    built_at: float
    lo: float

    def key(self, identity: Identity) -> str:
        """The account's number here, else the key its spend from before
        it was removed is kept under."""
        return self.numbers.get(identity) or attribution.removed_key(identity)

    def holds(self, identity: Identity) -> bool:
        return identity in self.numbers or attribution.removed_key(identity) in self.spend

    def covers(self, key: str, a: float, b: float) -> bool:
        """Whether every stretch ``key`` was on duty in ``[a, b)`` is priced
        and no spend in it went to nobody (it may have been this account's)."""
        for start, end, who in self.spans:
            if who != key or end <= a or start >= b:
                continue
            if max(start, a) < self.lo or min(end, b) > self.built_at:
                return False
        unknown = self.spend.get(UNATTRIBUTED, [])
        i = bisect.bisect_left(unknown, (a - costs.SLOT_S,))
        while i < len(unknown) and unknown[i][0] < b:
            slot, usd = unknown[i]
            if slot + costs.SLOT_S > a and usd >= 0.005:
                return False
            i += 1
        return True


def read_numbers(backup_dir: Path) -> dict[Identity, str]:
    """``{identity: number}`` from a profile's ``sequence.json``."""
    return attribution.profile_keys(backup_dir)


def profile_spend(
    cache: costs.CostCache,
    sources: Sequence[costs.Source],
    spans: Sequence[Span],
    numbers: dict[Identity, str],
    now: float,
) -> ProfileSpend:
    lo = costs.range_start(now)
    rows = [r for r in cache.rows if lo <= r.slot <= now]
    return ProfileSpend(numbers, attribute(rows, sources, spans), tuple(spans),
                        cache.built_at, lo)


def profile_spans(backup_dir: Path, sources: Sequence[costs.Source], lo: float,
                  now: float) -> list[Span]:
    """The profile's duty spans from every piece of evidence it has."""
    projects = [s.path for s in sources if s.account is None]
    return attribution.timeline(backup_dir, projects, lo, now,
                                other_dirs=other_profile_dirs(backup_dir))


def other_profile_dirs(backup_dir: Path) -> list[Path]:
    """The data dirs of the other rota profiles on this machine."""
    me = os.path.realpath(backup_dir)
    seen = {me}
    out = []
    for path in [get_default_backup_root(), *(Path(p.data_dir) for p in profiles.load())]:
        real = os.path.realpath(os.path.expanduser(path))
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(Path(real))
    return out


def load_profile_spend(backup_dir: Path, now: float) -> ProfileSpend | None:
    """Another profile's spend from its own cost cache (never repriced from
    here) and switch log; None when it has never priced anything."""
    cache = costs.load_cache(backup_dir)
    numbers = read_numbers(backup_dir)
    if cache is None or not numbers:
        return None
    sources = costs.sources_from_paths(backup_dir, cache.sources)
    spans = profile_spans(backup_dir, sources, costs.range_start(now), now)
    return profile_spend(cache, sources, spans, numbers, now)


def account_per_pct(
    identity: Identity,
    held: Sequence[ProfileSpend],
    history,
    store: dpp.Store,
    now: float,
) -> tuple[dict[str, tuple[set[float], list[dpp.Sample]]], dict[str, dpp.PerPct | None]]:
    """One account's samples (fresh from its spend over every profile in
    ``held``, with the windows they were read from) and the figures from
    those plus the stored ones."""
    lo = max(p.lo for p in held)
    merged: dict[int, float] = {}
    for p in held:
        for slot, usd in p.spend.get(p.key(identity), ()):
            merged[slot] = merged.get(slot, 0.0) + usd
    spend = sorted(merged.items())

    def covered(a: float, b: float) -> bool:
        return all(p.covers(p.key(identity), a, b) for p in held)

    full = history.series({"": identity}).get("", {})
    fresh: dict[str, tuple[set[float], list[dpp.Sample]]] = {}
    shown: dict[str, dpp.PerPct | None] = {}
    key = dpp.identity_key(identity)
    for label, duration in WINDOW_S.items():
        resets = [w.resets_at for w in history.windows(identity, label, lo)]
        hourly = history.long_series(identity, label, lo)
        read, found = dpp.samples(spend, full.get(label, ()), hourly, resets, duration, lo,
                                  now, covered, dpp.SEGMENT_PCT[label])
        fresh[label] = (read, found)
        stored = [s for s in store.get(key, {}).get(label, ()) if s.resets_at not in read]
        shown[label] = dpp.summarize([*stored, *found], label, now)
    return fresh, shown


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
    per_5h: dpp.PerPct | None
    per_7d: dpp.PerPct | None


@dataclass(frozen=True)
class CostReport:
    now: float
    days: tuple[tuple[float, dict[str, float]], ...]  # (local midnight, {model: usd}), oldest first
    history: tuple[tuple[float, dict[str, float]], ...]  # every day known: archive, then days
    hours: tuple[tuple[float, dict[str, float]], ...]  # last 7 x 24 (hour start, {model: usd})
    hour_duty: tuple[str | None, ...]  # who was on duty most of each of those hours
    # as ``history`` / ``hours``, by (account key, model): "" is unattributed,
    # and the days older than the priced range never had an account
    history_cells: tuple[tuple[float, dict[Cell, float]], ...]
    hour_cells: tuple[tuple[float, dict[Cell, float]], ...]
    today: float
    week: float  # last 7 x 24h
    month: float  # the whole range
    week_change: float | None  # last 7 x 24h vs the 7 before
    models: tuple[ModelLine, ...]  # costliest first
    accounts: tuple[AccountLine, ...]  # costliest first
    unattributed: float  # spend no duty span covers
    built_at: float
    took_s: float


def _change(now: float, prev: float) -> float | None:
    return (now - prev) / prev if prev > 0 else None


def build_report(
    cache: costs.CostCache,
    sources: Sequence[costs.Source],
    spans: Sequence[Span],
    now: float,
    per_pct: dict[str, dict[str, dpp.PerPct | None]] | None = None,
) -> CostReport:
    """The view's numbers; ``per_pct`` maps account numbers to their
    ``{"5h": ..., "7d": ...}`` figures."""
    per_pct = per_pct or {}
    lo = costs.range_start(now)
    rows = [r for r in cache.rows if lo <= r.slot <= now]
    midnights = [costs.day_start(now, back) for back in range(costs.RANGE_DAYS - 1, -1, -1)]
    days: list[dict[Cell, float]] = [{} for _ in midnights]
    first_hour = (now // HOUR_S - (RECENT_HOURS - 1)) * HOUR_S
    hours: list[dict[Cell, float]] = [{} for _ in range(RECENT_HOURS)]
    per_model: dict[str, list[float]] = {}  # name -> [cost, in, out, cw, cr, last7, prev7]
    per_account: dict[str, float] = {}
    week_ago, fortnight_ago = now - WEEK_S, now - 2 * WEEK_S
    today_start = midnights[-1]
    today = week = prev_week = 0.0
    fixed = {s.key: s.account for s in sources}
    starts = [s[0] for s in spans]
    for r in rows:
        name = model_name(r.model)
        day = bisect.bisect_right(midnights, r.slot) - 1
        hour = int((r.slot - first_hour) // HOUR_S) if r.slot >= first_hour else -1
        for who, frac in parts(r, fixed, spans, starts):
            usd = r.cost * frac
            per_account[who] = per_account.get(who, 0.0) + usd
            if day >= 0:
                days[day][(who, name)] = days[day].get((who, name), 0.0) + usd
            if 0 <= hour < RECENT_HOURS:
                hours[hour][(who, name)] = hours[hour].get((who, name), 0.0) + usd
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
    accounts = []
    for key in {*per_account, *per_pct} - {UNATTRIBUTED}:
        figures = per_pct.get(key, {})
        accounts.append(AccountLine(key, per_account.get(key, 0.0),
                                    figures.get("5h"), figures.get("7d")))
    accounts.sort(key=lambda a: (-a.cost, a.number))
    hour_starts = [first_hour + i * HOUR_S for i in range(RECENT_HOURS)]
    archived: dict[float, dict[Cell, float]] = {}
    for day_start, raw in cache.archive.items():
        if day_start < lo:
            cells = archived.setdefault(day_start, {})
            for model, usd in raw.items():
                cell = (UNATTRIBUTED, model_name(model))
                cells[cell] = cells.get(cell, 0.0) + usd
    history_cells = (*sorted(archived.items()), *zip(midnights, days))
    return CostReport(
        now=now,
        days=tuple((t, by_model(c)) for t, c in zip(midnights, days)),
        history=tuple((t, by_model(c)) for t, c in history_cells),
        hours=tuple((t, by_model(c)) for t, c in zip(hour_starts, hours)),
        hour_duty=tuple(majority(spans, h, h + HOUR_S) for h in hour_starts),
        history_cells=history_cells,
        hour_cells=tuple(zip(hour_starts, hours)),
        today=today,
        week=week,
        month=month,
        week_change=_change(week, prev_week),
        models=models,
        accounts=tuple(accounts),
        unattributed=max(0.0, per_account.get(UNATTRIBUTED, 0.0)),
        built_at=cache.built_at,
        took_s=cache.took_s,
    )


def by_model(cells: dict[Cell, float]) -> dict[str, float]:
    """A bucket's spend by model, whoever it was credited to."""
    out: dict[str, float] = {}
    for (_, model), usd in cells.items():
        out[model] = out.get(model, 0.0) + usd
    return out


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
    its ``windows``, ``long_series`` and ``series`` methods).
    """

    def __init__(
        self,
        backup_dir: Path,
        history,
        config_homes: Sequence[str | Path],
        *,
        find_runner: Callable[[], list[str] | None] = costs.find_runner,
        run: Callable[[list[str], Path], dict] = costs.run_ccusage,
        clock: Callable[[], float] = time.time,
        other_profiles: Callable[[], Iterable[Path]] | None = None,
        store_path: Path | None = None,
    ) -> None:
        self.backup_dir = backup_dir
        self.history = history
        self.config_homes = list(config_homes)
        self.find_runner = find_runner
        self.run = run
        self.clock = clock
        self.other_profiles = other_profiles or (lambda: other_profile_dirs(backup_dir))
        self.store_path = store_path or get_shared_cache_dir() / dpp.STORE_FILENAME

    def _sources(self) -> list[costs.Source]:
        homes = [*self.config_homes, *costs.feed_config_dirs(self.backup_dir)]
        return costs.transcript_sources(self.backup_dir, homes)

    def _per_pct(
        self, mine: ProfileSpend, identities: dict[str, Identity], now: float
    ) -> dict[str, dict[str, dpp.PerPct | None]]:
        """Every account's figures, its spend summed over the profiles that
        hold it; their samples go into the shared store."""
        if self.history is None:
            return {}
        others = [p for p in map(lambda d: load_profile_spend(d, now), self.other_profiles())
                  if p is not None]
        store = dpp.load_store(self.store_path)
        changed = False
        out: dict[str, dict[str, dpp.PerPct | None]] = {}
        for number, identity in identities.items():
            held = [mine, *(p for p in others if p.holds(identity))]
            try:
                fresh, out[number] = account_per_pct(identity, held, self.history, store, now)
            except Exception as e:  # history is best effort
                _logger.warning(f"costs: no $ per 1% for account {number}: {e!r}")
                continue
            for label, (read, found) in fresh.items():
                changed |= dpp.merge(store, dpp.identity_key(identity), label, read, found, now)
        if changed:
            dpp.save_store(self.store_path, store)
        return out

    def _report(self, cache: costs.CostCache, sources, identities, now: float) -> CostReport:
        spans = profile_spans(self.backup_dir, sources, costs.range_start(now), now)
        numbers = {identity: number for number, identity in identities.items()}
        mine = profile_spend(cache, sources, spans, numbers, now)
        return build_report(cache, sources, spans, now, self._per_pct(mine, identities, now))

    def cached(self, identities: dict[str, Identity]) -> CostResult:
        """The last priced report, without running ccusage (None if never)."""
        cache = costs.load_cache(self.backup_dir)
        if cache is None:
            return CostResult(None)
        now = self.clock()
        return CostResult(self._report(cache, self._sources(), identities, now))

    def refresh(self, identities: dict[str, Identity], *, force: bool = False) -> CostResult:
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
