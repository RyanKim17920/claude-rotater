"""The ``pace`` auto-switch policy (the default). Pure decision math, no I/O.

What rotation is for is the weekly (7-day) quota: whatever an account has
left when its week rolls over is gone. The 5-hour window is a speed limit on
the way, not something to spend for its own sake: a 5-hour window that rolls
over unused wastes nothing. So every proactive choice here is made on the
7-day windows, and the 5-hour window only ever constrains it.

Per account the policy asks: how fast would you have to spend it, from now
on, to use what it has left before its week rolls over?

    required pace (need) = 7d headroom (to the threshold) / hours to its 7d reset

A week that has not started yet (no reset reported) is planned as a whole
week away: that is the soonest it can roll over once it starts.

**Spread.** Being on the account with the highest need lowers that need and
leaves the others' to climb as their resets near, so following the leader
keeps every account on track to finish its week: an even spread. To keep
that from flapping, the policy stays while the active account's need is
within the margin (``autoswitch.paceMargin``, 1.5x by default) of the
leader's, and moves only when the leader is ahead by more. The move back
needs the reverse, a swing of margin squared that only real use and time
produce.

**A move must save some week.** The need ratio says which account is
behind, not whether moving to it now changes anything. Before a proactive
move the policy plans the rest of the weeks at the user's typical spend per
wall hour (and at ``DEFER_SAFETY`` times slower): stay on the active account
until it is done, then the others earliest reset first, against moving
first. Unless moving leaves at least ``MOVE_SAVES_PCT`` less 7d unused by
the resets, it stays: the move would only throw the caches away. When it
does move, it goes to the account whose move saves the most. A stay that
would spend the active account days before its reset, with no second
account left beside the next one, is not planned (``ALONE_H``): one account
alone cannot absorb a 5h burst. Without a spend estimate the need ratio
decides alone.

**Finish first.** As an account's reset nears with headroom left, its need
climbs (hours shrink, headroom does not), so it takes the lead and the
policy concentrates on it until that quota is spent. No separate rule.

**5 hours is a constraint only.** The policy never moves to an account whose
5-hour window is near its limit, and between accounts the 7-day picture
cannot tell apart it prefers 5-hour room. It never moves, or stays, to use
up a 5-hour window. When the active account's 5-hour window is about to hit,
the engine's pre-empt and threshold paths make the move, ordered by
``rank``: highest 7-day need first among accounts with 5-hour room.

**A move costs a cache rebuild.** The prompt cache belongs to the account,
so a switch while sessions are warm makes each one re-write its whole
context on its next turn, at cache-write prices, on the new account's
windows (about 0.3-0.7% of a week per switch, measured). A move the policy
wants while the caches are warm therefore waits for them to go cold (no
local turn for ``CACHE_TTL_S``), for as long as waiting costs no week: the
target must still be able to spend what it has after one more cache
lifetime at the user's burn, with ``DEFER_SAFETY`` to spare. Limit moves
(pre-empt, at the threshold, a dead login) never wait: a blocked turn costs
more than a rebuild.

The engine adds two agreeing evaluations, the cooldown, the manual-switch
hold and the hourly cap.
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

# A reset closer than this counts as this close (no division blow-up).
MIN_HOURS_LEFT = 1.0 / 60.0
# A week with no reported reset has not started: once it does, it rolls over
# a week later, so that is the plan.
WEEK_HOURS = 7 * 24.0
# Two needs within this ratio are the same to the 7-day picture; 5-hour room
# breaks the tie.
SIMILAR_NEED_RATIO = 1.1
# An active account with at most this many 7d points left is finished before
# moving on: leave it now and the rest is under what a later move would come
# back for (``MEANINGFUL_ABSORB_PCT``), so it would expire unused.
FINISH_PCT = 10.0
# Claude Code writes its prompt cache with a one-hour lifetime (every
# transcript turn reports ``cache_creation.ephemeral_1h_input_tokens``): an
# hour after a session's last turn its cache is gone whichever account is on.
CACHE_TTL_S = 3600.0
# A move waits for cold caches only while the target could spend this many
# times what it has left in the hours after one more cache lifetime.
DEFER_SAFETY = 1.5
# A target this many times as needy as the active account is urgent: its
# move waits for idle caches at most ``URGENT_WAIT_S``, not as long as the
# week allows.
URGENT_RATIO = 3.0
URGENT_WAIT_S = 30 * 60.0
# A proactive move must leave at least this many fewer 7d points unused by
# the resets than staying would (``plan_loss``): about two cache rebuilds
# (0.3-0.7% each), so planning noise never pays for a switch.
MOVE_SAVES_PCT = 1.0
# Finishing the active account first is planned only if its week rolls over
# within this long of it being done, or two other accounts can take work:
# spent days early, it leaves one account to absorb every 5h burst (the week
# replay loses more than the switches save without this).
ALONE_H = 48.0


def seven_day_left(view: AccountView, threshold: float) -> float:
    """7d points this account has left below the switch threshold."""
    return max(0.0, view.seven_day_headroom - (100.0 - threshold))


def hours_left(view: AccountView, now: float) -> float:
    """Hours until this account's week rolls over; a whole week when no
    reset is reported (the week has not started)."""
    if view.seven_day_reset is None:
        return WEEK_HOURS
    return max(MIN_HOURS_LEFT, (view.seven_day_reset - now) / 3600.0)


def required_pace(view: AccountView, *, threshold: float, now: float) -> float:
    """7d points per hour needed to use this account's headroom before its
    reset."""
    return seven_day_left(view, threshold) / hours_left(view, now)


def caches_cold(last_activity_at: float | None, now: float) -> bool:
    """Whether a switch now finds no warm prompt cache to throw away: no
    local turn for a cache lifetime. Unknown activity counts as cold, so a
    rota that cannot see the sessions moves as it always did."""
    return last_activity_at is None or now - last_activity_at >= CACHE_TTL_S


def can_wait(
    target: AccountView,
    *,
    threshold: float,
    burn_pct_per_h: float | None,
    now: float,
) -> bool:
    """Whether putting a move to ``target`` off by one more cache lifetime
    costs no 7d: at ``burn_pct_per_h`` (the user's typical spend per wall
    hour, nights and breaks included) it can still spend what it has left
    ``DEFER_SAFETY`` times over in the hours after the wait. An unknown burn
    cannot promise that."""
    if burn_pct_per_h is None:
        return False
    spare_h = hours_left(target, now) - CACHE_TTL_S / 3600.0
    return seven_day_left(target, threshold) * DEFER_SAFETY <= burn_pct_per_h * spare_h


def plan_loss(
    order: list[AccountView], *, threshold: float, rate: float, now: float
) -> float:
    """7d points left unused at the resets if the user spends ``rate`` points
    per hour on ``order[0]`` until it is done (or its week ends), then on the
    rest earliest reset first. ``rate`` is a spend per wall hour, nights
    included, so a plan that fits at it fits the user's real days."""
    first, rest = order[0], order[1:]
    rest = sorted(rest, key=lambda v: hours_left(v, now))
    cursor_h = 0.0
    lost = 0.0
    for v in (first, *rest):
        left = seven_day_left(v, threshold)
        avail_h = max(0.0, hours_left(v, now) - cursor_h)
        if left <= rate * avail_h:
            cursor_h += left / rate
        else:
            lost += left - rate * avail_h
            cursor_h += avail_h
    return lost


def _keeps_a_spare(
    active: AccountView,
    others: list[AccountView],
    *,
    threshold: float,
    rate: float,
    now: float,
) -> bool:
    """Whether finishing the active account first still leaves a second
    account beside the one in use. Spent early in its week, it is out until
    its reset, and a lone account blocks the user at its 5h limit: so a stay
    is planned only when the active account's week rolls over within
    ``ALONE_H`` of it being done (at the slower planned spend), or two other
    accounts have meaningful 7d left."""
    done_h = seven_day_left(active, threshold) / (rate / DEFER_SAFETY)
    if hours_left(active, now) - done_h <= ALONE_H:
        return True
    spare = [v for v in others if seven_day_left(v, threshold) >= MEANINGFUL_ABSORB_PCT]
    return len(spare) >= 2


def _plan_rates(rate: float) -> tuple[float, ...]:
    """The spends a plan must hold at: the typical one and a slower week."""
    return (rate, rate / DEFER_SAFETY)


def _best_move(
    active: AccountView,
    accounts: list[AccountView],
    targets: list[AccountView],
    *,
    threshold: float,
    rate: float,
    now: float,
) -> tuple[AccountView | None, float]:
    """The target whose move saves the most 7d against staying, and how much
    (the most at any planned spend). None when no move saves
    ``MOVE_SAVES_PCT``; ``targets`` order breaks ties."""
    others = [v for v in accounts if v.number != active.number]
    best: AccountView | None = None
    best_saved = 0.0
    for target in targets:
        rest = [v for v in others if v.number != target.number]
        saved = max(
            plan_loss([active, *others], threshold=threshold, rate=r, now=now)
            - plan_loss([target, active, *rest], threshold=threshold, rate=r, now=now)
            for r in _plan_rates(rate)
        )
        if saved > best_saved:
            best, best_saved = target, saved
    if best_saved < MOVE_SAVES_PCT:
        return None, best_saved
    return best, best_saved


def _five_used(view: AccountView) -> float:
    return 100.0 - view.five_hour_headroom


class _Fleet:
    """One evaluation's numbers: who can take work, and their needs."""

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
        self.margin = margin
        self.now = now
        self.need = {
            v.number: required_pace(v, threshold=threshold, now=now) for v in views
        }

        def counts(v: AccountView) -> bool:
            # The active account is judged on raw headroom, a would-be target
            # on what it can absorb at the user's burn before its reset. The
            # gap keeps a wobbling burn estimate from toggling either.
            if v.number == active:
                return v.seven_day_headroom >= SPENT_7D_PCT
            left = seven_day_left(v, threshold)
            if burn_pct_per_h is not None:
                left = min(left, burn_pct_per_h * hours_left(v, now))
            return left >= MEANINGFUL_ABSORB_PCT

        self.pool = [v for v in views if counts(v)]

    def roomy(self, view: AccountView) -> bool:
        """Its 5h window is not near its limit."""
        return _five_used(view) < self.threshold - FIVE_HOUR_NEAR_MARGIN_PCT

    def by_need(self, views: list[AccountView]) -> list[AccountView]:
        """Most 7d need first. Needs the 7d picture cannot tell apart (within
        ``SIMILAR_NEED_RATIO`` of the one ahead) go to more 5h room."""
        ordered = sorted(views, key=lambda v: -self.need[v.number])
        out: list[AccountView] = []
        while ordered:
            head = self.need[ordered[0].number]
            tied = [v for v in ordered if self.need[v.number] * SIMILAR_NEED_RATIO >= head]
            tied.sort(key=lambda v: -v.five_hour_headroom)
            out.extend(tied)
            ordered = [v for v in ordered if v not in tied]
        return out

    def pace(self, view: AccountView) -> str:
        return f"{self.need[view.number]:.1f}%/h"

    def deadline(self, view: AccountView) -> str:
        """``#2's week resets in 9h with 40% left (needs 4.4%/h)``."""
        if view.seven_day_reset is None:
            return (
                f"#{view.number}'s week has not started, "
                f"{seven_day_left(view, self.threshold):.0f}% to spend "
                f"(needs {self.pace(view)})"
            )
        when = fmt_duration(hours_left(view, self.now) * 3600.0)
        return (
            f"#{view.number}'s week resets in {when} with "
            f"{seven_day_left(view, self.threshold):.0f}% left "
            f"(needs {self.pace(view)})"
        )


def choose(
    active: AccountView,
    candidates: list[AccountView],
    *,
    threshold: float,
    margin: float,
    hysteresis_pct: float,  # noqa: ARG001 - the policies share one signature
    burn_pct_per_h: float | None,
    now: float,
    cold: bool = True,
    waited_s: float = 0.0,
    typical_burn_pct_per_h: float | None = None,
) -> Choice:
    """Stay or move, for an active account that is healthy and below the
    threshold. ``candidates`` must already be filtered to healthy accounts
    (enabled, readable, under the threshold on every gating window).

    ``cold``: no local session has a warm prompt cache (``caches_cold``).
    While they are warm a move waits for them (a stay with ``waiting_for``)
    unless waiting could cost the target's week (``can_wait`` at
    ``typical_burn_pct_per_h``, the user's spend per wall hour over the last
    days; ``burn_pct_per_h`` when unknown) or the target is urgent
    (``URGENT_RATIO``) and has waited ``URGENT_WAIT_S`` already
    (``waited_s``: how long the caller has been holding this move)."""
    # Only a spend per wall hour can plan a week: the duty burn is a rate
    # per hour of use and would make every stay look affordable.
    choice = _choose(
        active, candidates, threshold=threshold, margin=margin,
        burn_pct_per_h=burn_pct_per_h, now=now, plan_rate=typical_burn_pct_per_h,
    )
    if choice.action != "switch" or cold:
        return choice
    target = next(v for v in candidates if v.number == choice.target)
    _, _, why = choice.reason.partition(": ")
    num = target.number
    typical = typical_burn_pct_per_h if typical_burn_pct_per_h is not None else burn_pct_per_h
    if not can_wait(target, threshold=threshold, burn_pct_per_h=typical, now=now):
        when = fmt_duration(hours_left(target, now) * 3600.0)
        return Choice(
            "switch", num,
            f"switch → #{num}: #{num} has {seven_day_left(target, threshold):.0f}% "
            f"to spend in {when} — moving now; {why}",
        )
    mine = required_pace(active, threshold=threshold, now=now)
    theirs = required_pace(target, threshold=threshold, now=now)
    if theirs >= mine * URGENT_RATIO and waited_s >= URGENT_WAIT_S:
        return Choice(
            "switch", num,
            f"switch → #{num}: waited {fmt_duration(waited_s)} for idle; {why}",
        )
    return Choice(
        "stay", active.number,
        f"stay: switch → #{num} when idle (cache warm); {why}",
        waiting_for=num,
    )


def _choose(
    active: AccountView,
    candidates: list[AccountView],
    *,
    threshold: float,
    margin: float,
    burn_pct_per_h: float | None,
    now: float,
    plan_rate: float | None = None,
) -> Choice:
    me = active.number

    def stay(why: str) -> Choice:
        return Choice("stay", me, f"stay: {why}")

    def switch(target: AccountView, why: str) -> Choice:
        return Choice("switch", target.number, f"switch → #{target.number}: {why}")

    if not candidates:
        return stay("no other account is under the threshold")
    fleet = _Fleet(
        [active, *candidates], active=me, threshold=threshold, margin=margin,
        burn_pct_per_h=burn_pct_per_h, now=now,
    )
    others = fleet.by_need([v for v in candidates if v in fleet.pool])
    if active not in fleet.pool:
        targets = [v for v in others if fleet.roomy(v)]
        if not targets:
            if others:
                lead = others[0]
                return stay(
                    f"#{me} has under {SPENT_7D_PCT:.0f}% 7d left; "
                    f"#{lead.number}'s 5h is {_five_used(lead):.0f}% used"
                )
            return stay("no account has meaningful 7d headroom left")
        target = targets[0]
        return switch(
            target,
            f"#{me} has under {SPENT_7D_PCT:.0f}% 7d left; {fleet.deadline(target)}",
        )
    if not others:
        return stay(f"no other account needs using first; {fleet.deadline(active)}")

    mine = fleet.need[me]
    ahead = [v for v in others if fleet.need[v.number] > mine * margin]
    left = seven_day_left(active, threshold)
    if ahead and left <= FINISH_PCT and burn_pct_per_h:
        # Only when finishing costs the leader nothing: at the user's burn
        # it can still spend all it has after this account is done.
        lead = ahead[0]
        finish_h = left / burn_pct_per_h
        after_h = hours_left(lead, now) - finish_h
        if after_h * burn_pct_per_h >= seven_day_left(lead, threshold):
            return stay(
                f"finishing #{me}'s last {left:.0f}% of 7d before "
                f"#{lead.number} ({fleet.pace(lead)})"
            )
    if not ahead:
        lead = others[0]
        if mine >= fleet.need[lead.number]:
            return stay(f"most 7d to spend: {fleet.deadline(active)}")
        return stay(
            f"7d spread evenly: #{me} needs {fleet.pace(active)}, "
            f"#{lead.number} {fleet.pace(lead)} (within {margin:g}x)"
        )
    targets = [v for v in ahead if fleet.roomy(v)]
    if not targets:
        lead = ahead[0]
        return stay(
            f"#{lead.number} needs {fleet.pace(lead)} but its 5h is "
            f"{_five_used(lead):.0f}% used"
        )
    if not plan_rate or not _keeps_a_spare(active, others, threshold=threshold, rate=plan_rate, now=now):
        target = targets[0]
        return switch(target, f"{fleet.deadline(target)} vs #{me} {fleet.pace(active)}")
    target, saved = _best_move(
        active, [active, *others], targets, threshold=threshold, rate=plan_rate, now=now,
    )
    if target is None:
        return stay(
            f"#{me} can finish its {left:.0f}% of 7d first and every other "
            f"week still fits at {plan_rate:.1f}%/h; #{ahead[0].number} "
            f"needs {fleet.pace(ahead[0])}"
        )
    return switch(
        target,
        f"{fleet.deadline(target)} vs #{me} {fleet.pace(active)}; staying would "
        f"leave {saved:.0f}% of 7d unused",
    )


def rank(
    pool: list[AccountView],
    *,
    threshold: float,
    margin: float,
    burn_pct_per_h: float | None,
    now: float,
) -> list[str]:
    """Order would-be targets for a move the engine has to make anyway (the
    active account is at its threshold, about to hit a limit, or unreadable).

    Accounts that can take meaningful 7d work and have 5h room, by 7d need;
    then those that can take 7d work but whose 5h is near its limit, by 5h
    room; then the rest by most headroom. Sorting is stable, so the caller's
    order breaks remaining ties.
    """
    fleet = _Fleet(
        pool, active=None, threshold=threshold, margin=margin,
        burn_pct_per_h=burn_pct_per_h, now=now,
    )
    first = fleet.by_need([v for v in fleet.pool if fleet.roomy(v)])
    seen = {v.number for v in first}
    second = sorted(
        (v for v in fleet.pool if v.number not in seen),
        key=lambda v: -v.five_hour_headroom,
    )
    seen |= {v.number for v in second}
    rest = sorted((v for v in pool if v.number not in seen), key=lambda v: -v.headroom)
    return [v.number for v in (*first, *second, *rest)]
