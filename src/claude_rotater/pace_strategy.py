"""The ``pace`` auto-switch policy (the default). Pure decision math, no I/O.

Weekly (7-day) quota is perishable, but spending the soonest-expiring account
first (``expiring-first``) piles every hour onto one account, burning through
its 5-hour windows while the others sit idle. ``pace`` asks a sharper
question per account: how fast would you have to spend it, from now on, to
use what it has left before its window rolls over?

    required pace = 7d headroom (to the threshold) / hours until its 7d reset

Early in a week every account's required pace is low and roughly equal:
whatever you do, all of it gets used. Then the policy **spreads**: it stays
put until the active account's 5-hour window gets tight, then moves to the
account with the most 5-hour room, so several accounts keep a fresh 5-hour
window in reserve. As one account's reset approaches with headroom left, its
required pace climbs above the rest; once it leads by the dominance margin
(``autoswitch.paceMargin``, 1.5x by default) the policy **concentrates** on
it, so that quota is not wasted.

Spreading also wins whenever the highest required pace is well below the
user's actual burn rate (from the active account's history): at that burn
no single account's quota is at risk, so there is nothing to rescue.
Without a burn estimate only the dominance margin can start concentrating.

Anti-flap is structural: a move toward the leader needs it to lead the
active account by the margin, and the move back needs the reverse, a swing
of margin squared that only real usage and time produce. Spreading only
moves when the 5-hour window is tight. The engine adds two agreeing
evaluations, the cooldown, the manual-switch hold and the hourly cap.

The 5-hour rules: never move to an account whose 5-hour window is near its
limit; when spreading, take an account with a window already running and
keep the last fresh one as a backup; and never open a fresh window elsewhere
just to balance while the current one still has room.
"""

from __future__ import annotations

from claude_rotater.expiring_first import (
    FIVE_HOUR_NEAR_MARGIN_PCT,
    MEANINGFUL_ABSORB_PCT,
    SPENT_7D_PCT,
    AccountView,
    Choice,
    fmt_duration,
)

# Spread whenever the highest required pace is under this fraction of the
# user's actual burn: even half of the user's time would empty the most
# urgent account before its reset.
SPREAD_BURN_FRACTION = 0.5
# A 5-hour window at most this full is a fresh backup.
FRESH_FIVE_HOUR_USED_PCT = 20.0
# A reset closer than this counts as this close (no division blow-up).
MIN_HOURS_LEFT = 1.0 / 60.0


def seven_day_left(view: AccountView, threshold: float) -> float:
    """7d points this account has left below the switch threshold."""
    return max(0.0, view.seven_day_headroom - (100.0 - threshold))


def hours_left(view: AccountView, now: float) -> float | None:
    if view.seven_day_reset is None:
        return None
    return max(MIN_HOURS_LEFT, (view.seven_day_reset - now) / 3600.0)


def required_pace(view: AccountView, *, threshold: float, now: float) -> float:
    """7d points per hour needed to use this account's headroom before its
    reset. 0 when the reset is unknown: nothing says it is urgent."""
    hours = hours_left(view, now)
    if hours is None:
        return 0.0
    return seven_day_left(view, threshold) / hours


def _five_used(view: AccountView) -> float:
    return 100.0 - view.five_hour_headroom


def _fresh(view: AccountView) -> bool:
    return _five_used(view) <= FRESH_FIVE_HOUR_USED_PCT


class _Fleet:
    """One evaluation's numbers: who counts, their required paces, and the
    group worth being on (everyone when spreading, the leaders otherwise)."""

    def __init__(
        self,
        views: list[AccountView],
        *,
        active: str | None,
        threshold: float,
        margin: float,
        burn_pct_per_h: float | None,
        now: float,
    ) -> None:
        self.threshold = threshold
        self.now = now
        self.burn = burn_pct_per_h
        self.need = {
            v.number: required_pace(v, threshold=threshold, now=now) for v in views
        }

        def counts(v: AccountView) -> bool:
            # The active account is judged on raw headroom, a would-be target
            # on what it can absorb at the user's burn before its reset. The
            # gap keeps a wobbling burn estimate from toggling either.
            if v.number == active:
                return v.seven_day_headroom >= SPENT_7D_PCT
            if v.seven_day_reset is None:
                return False  # a target needs a known reset to plan on
            left = seven_day_left(v, threshold)
            if burn_pct_per_h is not None:
                left = min(left, burn_pct_per_h * hours_left(v, now))
            return left >= MEANINGFUL_ABSORB_PCT

        self.pool = [v for v in views if counts(v)]
        self.top_need = max((self.need[v.number] for v in self.pool), default=0.0)
        # Without a burn estimate only dominance can say "concentrate".
        self.burn_covers_all = bool(burn_pct_per_h) and (
            self.top_need < SPREAD_BURN_FRACTION * burn_pct_per_h
        )
        if self.burn_covers_all:
            self.group = list(self.pool)
        else:
            self.group = [
                v for v in self.pool if self.need[v.number] * margin >= self.top_need
            ]
        self.spreading = len(self.group) == len(self.pool)

    def roomy(self, view: AccountView) -> bool:
        """Its 5h window is not near its limit."""
        return _five_used(view) < self.threshold - FIVE_HOUR_NEAR_MARGIN_PCT

    def by_need(self, views: list[AccountView]) -> list[AccountView]:
        """Most urgent first; equal needs go to the one with more 5h room."""
        return sorted(
            views, key=lambda v: (-self.need[v.number], -v.five_hour_headroom)
        )

    def spread_order(
        self, views: list[AccountView], others: list[AccountView]
    ) -> list[AccountView]:
        """Most 5h room first (then most need), except that the last fresh
        5h window among ``others`` (everyone who would stay behind) is kept
        as a backup while a running window can take the work instead."""
        ordered = sorted(
            views, key=lambda v: (-v.five_hour_headroom, -self.need[v.number])
        )
        if ordered and _fresh(ordered[0]):
            first = ordered[0]
            spare_fresh = any(
                _fresh(v) for v in others if v.number != first.number
            )
            running = next((v for v in ordered if not _fresh(v)), None)
            if not spare_fresh and running is not None:
                ordered.remove(running)
                ordered.insert(0, running)
        return ordered

    def pace(self, view: AccountView) -> str:
        return f"{self.need[view.number]:.1f}%/h"

    def deadline(self, view: AccountView) -> str:
        """``#2 resets in 9h with 40% left (needs 4.4%/h)``."""
        hours = hours_left(view, self.now)
        when = fmt_duration(hours * 3600.0) if hours is not None else "unknown"
        return (
            f"#{view.number} resets in {when} with "
            f"{seven_day_left(view, self.threshold):.0f}% left "
            f"(needs {self.pace(view)})"
        )

    def using_first(self, view: AccountView) -> str:
        """``#2's 5h resets in 25m with 80% left — using it first``: why a
        spreading fleet stays on a 5h window that still has room."""
        left = f"{view.five_hour_headroom:.0f}% left"
        if view.five_hour_reset is None:
            return f"#{view.number}'s 5h has {left} — using it first"
        when = fmt_duration(view.five_hour_reset - self.now)
        return f"#{view.number}'s 5h resets in {when} with {left} — using it first"

    def why_spread(self) -> str:
        """Why no account's weekly quota calls for concentrating on it."""
        if self.burn_covers_all:
            vs = f" vs burn {self.burn:.1f}%/h" if self.burn else ""
            return f"no 7d reset is close (needs ≤{self.top_need:.1f}%/h{vs})"
        low = f"{min(self.need[v.number] for v in self.pool):.1f}"
        high = f"{self.top_need:.1f}"
        span = high if low == high else f"{low}–{high}"
        return f"7d needs similar ({span}%/h)"


def choose(
    active: AccountView,
    candidates: list[AccountView],
    *,
    threshold: float,
    margin: float,
    hysteresis_pct: float,
    burn_pct_per_h: float | None,
    now: float,
) -> Choice:
    """Stay or move, for an active account that is healthy and below the
    threshold. ``candidates`` must already be filtered to healthy accounts
    (enabled, readable, under the threshold on every gating window)."""
    me = active.number

    def stay(why: str) -> Choice:
        return Choice("stay", me, f"stay: {why}")

    def switch(target: AccountView, why: str) -> Choice:
        return Choice("switch", target.number, f"switch → #{target.number}: {why}")

    if not candidates:
        return stay("no other account is under the threshold")
    if active.seven_day_reset is None:
        return stay("this account's 7d reset time is unknown")
    fleet = _Fleet(
        [active, *candidates], active=me, threshold=threshold, margin=margin,
        burn_pct_per_h=burn_pct_per_h, now=now,
    )
    if not fleet.pool:
        return stay("no account has meaningful 7d headroom left")
    five_used = _five_used(active)
    others = [v for v in candidates if v in fleet.pool]

    if active in fleet.group:
        if fleet.roomy(active):
            if fleet.spreading:
                return stay(f"{fleet.using_first(active)}; {fleet.why_spread()}")
            rivals = [v for v in fleet.group if v.number != me]
            rest = fleet.by_need([v for v in others if v not in fleet.group])
            vs = f", next #{rest[0].number} {fleet.pace(rest[0])}" if rest else ""
            if rivals:
                return stay(f"{fleet.deadline(active)}, as urgent as #{rivals[0].number}{vs}")
            return stay(f"{fleet.deadline(active)}{vs}")
        # 5h tight: hand over to a group member with clearly more 5h room.
        backups = [
            v for v in fleet.group
            if v.number != me and fleet.roomy(v)
            and v.five_hour_headroom - active.five_hour_headroom >= hysteresis_pct
        ]
        if not backups:
            if fleet.spreading:
                return stay(
                    f"5h {five_used:.0f}% used but no account has "
                    f"{hysteresis_pct:.0f} points more 5h room"
                )
            return stay(
                f"5h {five_used:.0f}% used; {fleet.deadline(active)}, "
                "staying until the threshold"
            )
        if fleet.spreading:
            target = fleet.spread_order(backups, others)[0]
            kept = [v for v in others if v is not target and _fresh(v)]
            backup = f", #{kept[0].number} kept fresh as backup" if kept else ""
            return switch(
                target,
                f"5h {five_used:.0f}% used; spreading to #{target.number} "
                f"(5h {_five_used(target):.0f}% used{backup})",
            )
        target = fleet.by_need(backups)[0]
        return switch(
            target,
            f"5h {five_used:.0f}% used; {fleet.deadline(target)}, as urgent as #{me}",
        )

    # The active account is out of the group: spent, or out-paced.
    targets = [v for v in fleet.group if fleet.roomy(v)]
    if not targets:
        leader = fleet.by_need(fleet.group)[0]
        return stay(
            f"#{leader.number} needs {fleet.pace(leader)} but its 5h is "
            f"{_five_used(leader):.0f}% used"
        )
    if fleet.spreading:
        target = fleet.spread_order(targets, others)[0]
        return switch(
            target,
            f"#{me} has under {SPENT_7D_PCT:.0f}% 7d left; spreading to "
            f"#{target.number} (5h {_five_used(target):.0f}% used)",
        )
    target = fleet.by_need(targets)[0]
    mine = f"#{me} needs {fleet.pace(active)}"
    return switch(target, f"{fleet.deadline(target)} vs {mine}")


def rank(
    pool: list[AccountView],
    *,
    threshold: float,
    margin: float,
    burn_pct_per_h: float | None,
    now: float,
) -> list[str]:
    """Order would-be targets for a move the engine has to make anyway (the
    active account is at its threshold or unreadable).

    The group worth being on first (by need when concentrating, by 5h room
    with a fresh backup kept when spreading), then other accounts with 5h
    room by need, then everyone else that can absorb work by 5h room, then
    the rest by most headroom. Sorting is stable, so the caller's order
    breaks remaining ties.
    """
    fleet = _Fleet(
        pool, active=None, threshold=threshold, margin=margin,
        burn_pct_per_h=burn_pct_per_h, now=now,
    )
    roomy_group = [v for v in fleet.group if fleet.roomy(v)]
    if fleet.spreading:
        first = fleet.spread_order(roomy_group, pool)
    else:
        first = fleet.by_need(roomy_group)
    seen = {v.number for v in first}
    second = fleet.by_need(
        [v for v in fleet.pool if v.number not in seen and fleet.roomy(v)]
    )
    seen |= {v.number for v in second}
    third = sorted(
        (v for v in fleet.pool if v.number not in seen),
        key=lambda v: -v.five_hour_headroom,
    )
    seen |= {v.number for v in third}
    rest = sorted((v for v in pool if v.number not in seen), key=lambda v: -v.headroom)
    return [v.number for v in (*first, *second, *third, *rest)]
