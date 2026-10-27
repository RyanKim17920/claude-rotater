"""A deterministic multi-week replay of the auto-switch policies.

Not the engine: a small model of it, close enough to compare how much weekly
quota each policy lets expire. Every ``STEP_S`` the simulated user works (or
not) on the active account, then the policy evaluates with the engine's
gates: a forced move at the threshold ranks the targets under it, a
proactive move below it needs two agreeing evaluations, the cooldown and the
hourly cap.

Quota model: a 7d window resets every week at the account's own offset; a
5h window starts at the first use after the last one ended and lasts five
hours. ``FIVE_PER_SEVEN`` 5h points cost one 7d point (a full 5h window is
about an eighth of a week). Usage over 100 is impossible: work that finds the
active account at a limit is blocked.

A switch while the user worked within the last cache lifetime
(``pace_strategy.CACHE_TTL_S``) throws the prompt caches away: with
``switch_cost`` the next turns re-write them on the new account, charged as
that many 7d points (and the matching 5h points) at the switch.

Waste is every 7d point that expired unused, counting the windows still open
when the replay ends as expiring then, so every policy is charged for the
same windows: waste is total capacity minus total use, and the only way to
waste less is to block less. Blocked hours are reported beside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from claude_rotater import expiring_first as ef
from claude_rotater import pace_strategy

H = 3600.0
WEEK_S = 7 * 24 * H
FIVE_S = 5 * H
STEP_S = 600.0
FIVE_PER_SEVEN = 8.0
THRESHOLD = 90.0
HYSTERESIS = 10.0
COOLDOWN_S = 300.0
MAX_PER_HOUR = 4
CONFIRM_S = 120.0


@dataclass
class Acct:
    number: str
    reset_at: float  # next 7d reset
    seven: float = 0.0
    five: float = 0.0
    five_reset: float | None = None

    def roll(self, now: float) -> float:
        """Roll windows past ``now``; return the 7d points that expired."""
        wasted = 0.0
        while now >= self.reset_at:
            wasted += 100.0 - self.seven
            self.seven = 0.0
            self.reset_at += WEEK_S
        if self.five_reset is not None and now >= self.five_reset:
            self.five, self.five_reset = 0.0, None
        return wasted

    def binding(self) -> float:
        return max(self.seven, self.five)

    def view(self, now: float) -> ef.AccountView:
        return ef.AccountView(
            number=self.number,
            headroom=100.0 - self.binding(),
            seven_day_pct=self.seven,
            seven_day_reset=self.reset_at,
            five_hour_pct=self.five if self.five_reset is not None else None,
            five_hour_reset=self.five_reset,
        )


@dataclass
class Result:
    wasted: float = 0.0  # 7d points expired unused
    blocked_h: float = 0.0  # hours the user wanted to work and could not
    switches: int = 0
    used: float = 0.0
    rebuild: float = 0.0  # 7d points spent re-writing caches after switches

    @property
    def lost(self) -> float:
        """7d points that did no work: expired, or spent re-writing caches
        (a rebuild uses quota, so it lowers ``wasted`` while doing nothing)."""
        return self.wasted + self.rebuild
    log: list[str] = field(default_factory=list)


def demand(t: float, rate: float) -> float:
    """7d points per hour the user wants at ``t``: three sessions a day,
    a lighter weekend, a deterministic day-to-day wobble."""
    day, hour = divmod(t / H, 24.0)
    day = int(day)
    if not (9 <= hour < 13 or 14 <= hour < 19 or 21 <= hour < 24):
        return 0.0
    wobble = 0.6 + 0.8 * ((day * 37) % 11) / 10.0
    weekend = 0.5 if day % 7 in (5, 6) else 1.0
    return rate * wobble * weekend


def simulate(
    strategy: str,
    reset_offsets_h: list[float],
    *,
    rate: float,
    weeks: int = 4,
    margin: float = 1.5,
    similar_h: float = 12.0,
    threshold: float = THRESHOLD,
    start_seven: list[float] | None = None,
    start_active: int = 0,
    switch_cost: float = 0.0,
    idle_aware: bool = False,
    min_dwell_s: float = 0.0,
) -> Result:
    """Replay ``weeks`` of work. ``start_seven`` gives each account's 7d use
    at the start (default all fresh) and ``start_active`` the account on duty.
    ``switch_cost``: 7d points a switch on warm caches costs the target.
    ``idle_aware``: tell pace whether the caches are cold. ``min_dwell_s``:
    no proactive move sooner than this after the last switch."""
    accts = [
        Acct(str(i + 1), reset_at=off * H, seven=start_seven[i] if start_seven else 0.0)
        for i, off in enumerate(reset_offsets_h)
    ]
    active = accts[start_active]
    res = Result()
    switch_times: list[float] = []
    pending: tuple[str, float] | None = None
    burn_log: list[tuple[float, float]] = []  # (t, points) for the burn estimate
    last_work: float | None = None
    wanted_since: float | None = None  # a move held for cold caches, since
    t = 0.0
    while t < weeks * WEEK_S:
        for a in accts:
            res.wasted += a.roll(t)
        want = demand(t, rate) * STEP_S / H
        if want > 0:
            room = min(100.0 - active.seven, (100.0 - active.five) / FIVE_PER_SEVEN)
            got = max(0.0, min(want, room))
            if got > 0 and active.five_reset is None:
                active.five_reset = t + FIVE_S
            active.seven += got
            active.five += got * FIVE_PER_SEVEN
            res.used += got
            burn_log.append((t, got))
            if got > 0:
                last_work = t + STEP_S
            if got < want - 1e-9:
                res.blocked_h += (1 - got / want) * STEP_S / H
        burn_log = [(bt, p) for bt, p in burn_log if t - bt < 24 * H]
        burn = sum(p for _, p in burn_log) / 24.0 or None

        cold = pace_strategy.caches_cold(last_work, t)

        def move(target: Acct) -> None:
            nonlocal active, pending, wanted_since
            if switch_cost and not cold:
                spend = min(switch_cost, 100.0 - target.seven)
                if target.five_reset is None:
                    target.five_reset = t + FIVE_S
                target.seven += spend
                target.five = min(100.0, target.five + spend * FIVE_PER_SEVEN)
                res.rebuild += spend
            active = target
            pending = None
            wanted_since = None
            switch_times.append(t)
            res.switches += 1

        recent = [s for s in switch_times if t - s < H]
        budget_left = len(recent) < MAX_PER_HOUR
        others = [a for a in accts if a is not active]
        healthy = [a for a in others if a.binding() < threshold]
        if active.binding() >= threshold:
            if budget_left and healthy:
                order = _rank(strategy, healthy, t, burn, margin, similar_h, threshold)
                move(next(a for a in healthy if a.number == order[0]))
            elif budget_left:
                best = max(others, key=lambda a: 100 - a.binding())
                if best.binding() < active.binding() - HYSTERESIS:
                    move(best)
        elif strategy in ("pace", "expiring-first") and healthy and budget_left:
            if switch_times and t - switch_times[-1] < max(COOLDOWN_S, min_dwell_s):
                t += STEP_S
                continue
            views = [a.view(t) for a in healthy]
            if strategy == "pace":
                choice = pace_strategy.choose(
                    active.view(t), views, threshold=threshold, margin=margin,
                    hysteresis_pct=HYSTERESIS, burn_pct_per_h=burn, now=t,
                    cold=cold if idle_aware else True,
                    waited_s=0.0 if wanted_since is None else t - wanted_since,
                )
                if choice.waiting_for is not None:
                    if wanted_since is None:
                        wanted_since = t
                elif choice.action == "stay":
                    wanted_since = None
            else:
                choice = ef.choose(
                    active.view(t), views, threshold=threshold,
                    similar_reset_s=similar_h * H, hysteresis_pct=HYSTERESIS,
                    burn_pct_per_h=burn, now=t,
                )
            if choice.action == "stay":
                pending = None
            elif pending is None or pending[0] != choice.target:
                pending = (choice.target, t)
            elif t - pending[1] >= CONFIRM_S:
                res.log.append(f"{t / H:7.1f}h {choice.reason}")
                move(next(a for a in healthy if a.number == choice.target))
        t += STEP_S
    res.wasted += sum(100.0 - a.seven for a in accts)  # the open windows end here
    return res


def _rank(strategy, healthy, t, burn, margin, similar_h, threshold) -> list[str]:
    views = [a.view(t) for a in healthy]
    if strategy == "pace":
        return pace_strategy.rank(
            views, threshold=threshold, margin=margin, burn_pct_per_h=burn, now=t
        )
    if strategy == "expiring-first":
        return ef.rank(views, similar_reset_s=similar_h * H, burn_pct_per_h=burn, now=t)
    # best: most headroom on the binding window.
    return [v.number for v in sorted(views, key=lambda v: -v.headroom)]


STRATEGIES = ("pace", "expiring-first", "best")
# Each account's first 7d reset, in hours from the start.
RESET_OFFSETS_H: tuple[tuple[float, ...], ...] = (
    (50.0, 20.0),
    (20.0, 80.0),
    (100.0, 104.0),
    (30.0, 90.0, 150.0),
    (10.0, 60.0, 110.0),
    (40.0, 52.0, 64.0),
)
# Weekly demand as a share of the fleet's weekly capacity: where 7d waste
# can be avoided at all (far below 100% every policy wastes the surplus).
DEMAND_SHARES = (0.8, 0.95, 1.1)
WORK_H_PER_WEEK = 72.0  # demand()'s sessions, at wobble 1, weekend halved


def scenario_name(offsets: tuple[float, ...], share: float) -> str:
    resets = "/".join(f"{o:g}h" for o in offsets)
    return f"{len(offsets)} accounts, resets {resets}, demand {share:.0%}"


def table(weeks: int = 8) -> dict[str, dict[str, Result]]:
    out: dict[str, dict[str, Result]] = {}
    for offsets in RESET_OFFSETS_H:
        for share in DEMAND_SHARES:
            rate = share * 100.0 * len(offsets) / WORK_H_PER_WEEK
            out[scenario_name(offsets, share)] = {
                s: simulate(s, list(offsets), rate=rate, weeks=weeks)
                for s in STRATEGIES
            }
    return out


if __name__ == "__main__":  # pragma: no cover - manual report
    rows = table()
    for name, row in rows.items():
        cells = "  ".join(
            f"{s} {r.wasted:6.1f} ({r.blocked_h:4.1f}h, {r.switches} sw)"
            for s, r in row.items()
        )
        print(f"{name:44} {cells}")
    for s in STRATEGIES:
        wasted = sum(row[s].wasted for row in rows.values())
        blocked = sum(row[s].blocked_h for row in rows.values())
        switches = sum(row[s].switches for row in rows.values())
        print(f"total {s:15} wasted {wasted:7.1f}  blocked {blocked:6.1f}h  "
              f"switches {switches}")
