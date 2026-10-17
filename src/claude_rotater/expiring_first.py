"""The ``expiring-first`` auto-switch policy. Pure decision math, no I/O.

Weekly (7-day) quota is perishable: whatever an account has left when its
window rolls over is gone. So the policy spends the account whose 7-day
window resets SOONEST first, the way earliest-deadline-first scheduling
minimises missed work: capacity used on the soonest-expiring account is
capacity the later ones keep, so the fleet as a whole wastes the least.

Two refinements keep that from being naive:

* **Plausibility.** A sooner reset only counts when the account can actually
  absorb meaningful work before it: ``absorbable = min(7d headroom,
  burn rate x time to reset)``. An account resetting in 20 minutes with 40%
  left saves almost nothing at a normal burn, so it does not win the slot.
  Without a burn estimate the whole 7d headroom counts.
* **Similar resets balance instead.** When accounts' 7-day resets sit within
  ``similar_reset_s`` of each other, reset order is noise; what matters then
  is keeping a 5-hour backup. The policy stays on the current account until
  its 5-hour window is near the limit (never opening a fresh 5-hour window
  elsewhere just to balance), then moves to the tied account with the most
  5-hour headroom.

Anti-flap: a proactive reset-axis move needs a LARGE gain, the target
resetting ``LARGE_GAIN_FACTOR`` x the similar margin sooner (24h at the
defaults), unless the active account is spent; smaller edges wait for the
threshold like the plain strategy. Resets only move when a window rolls
over, so burn alone can never reverse such a move. The engine adds its
cooldown, the manual-switch hold, and a second agreeing evaluation before
any proactive move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# Below this many 7d points an account is spent: an edge this small is under
# two poll intervals of work (same reasoning as autoswitch.SPENT_HEADROOM_PCT).
SPENT_7D_PCT = 3.0
# A target must be able to absorb at least this much before its reset to be
# worth a proactive move. Higher than SPENT_7D_PCT on purpose: the active
# account is judged on raw headroom and a target on absorbable headroom, and
# the gap between the two bars keeps a wobbling burn estimate from toggling
# an account in and out of the running.
MEANINGFUL_ABSORB_PCT = 5.0
# A 5h window within this many points of the switch threshold is "near its
# limit": the moment a tied account may take over as the 5h backup.
FIVE_HOUR_NEAR_MARGIN_PCT = 20.0
# Leaving an account in the middle of its 5h window needs the target to
# reset this many similar-margins sooner.
LARGE_GAIN_FACTOR = 2.0


@dataclass(frozen=True)
class AccountView:
    """What the policy needs to know about one account at one instant."""

    number: str
    headroom: float  # binding headroom over every gating window, in points
    seven_day_pct: float | None
    seven_day_reset: float | None  # epoch; None when unknown or already past
    five_hour_pct: float | None
    five_hour_reset: float | None  # epoch; None when unknown or already past

    @property
    def seven_day_headroom(self) -> float:
        # A missing 7d reading falls back to the binding headroom, which is
        # never more generous than the 7d window itself.
        if self.seven_day_pct is None:
            return self.headroom
        return max(0.0, 100.0 - self.seven_day_pct)

    @property
    def five_hour_headroom(self) -> float:
        # An unreported 5h window has not started: the whole window is free.
        if self.five_hour_pct is None or self.five_hour_reset is None:
            return 100.0
        return max(0.0, 100.0 - self.five_hour_pct)

    @property
    def mid_five_hour_window(self) -> bool:
        return (
            self.five_hour_reset is not None
            and self.five_hour_pct is not None
            and self.five_hour_pct > 0
        )


@dataclass(frozen=True)
class Choice:
    action: Literal["stay", "switch"]
    target: str  # the account to be on; the active one for "stay"
    reason: str  # one line for humans: "stay: ..." / "switch → #2: ..."


def fmt_duration(seconds: float) -> str:
    """Compact duration: ``2d2h``, ``19h40m``, ``18h``, ``45m``."""
    if not math.isfinite(seconds):
        return "unknown"
    minutes = max(0, int(seconds // 60))
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def absorbable(view: AccountView, burn_pct_per_h: float | None, now: float) -> float:
    """7d points this account can plausibly use before its window resets."""
    headroom = view.seven_day_headroom
    if burn_pct_per_h is None or view.seven_day_reset is None:
        return headroom
    hours_left = max(0.0, view.seven_day_reset - now) / 3600.0
    return min(headroom, burn_pct_per_h * hours_left)


def _reset(view: AccountView) -> float:
    return view.seven_day_reset if view.seven_day_reset is not None else math.inf


def _tie_group(
    pool: list[AccountView],
    *,
    similar_reset_s: float,
    burn_pct_per_h: float | None,
    now: float,
    active: str | None,
) -> list[AccountView]:
    """Accounts tied for "resets soonest": the earliest worthwhile reset and
    everyone within ``similar_reset_s`` of it. The active account is judged
    on raw headroom, a would-be target on what it can absorb."""

    def worthwhile(v: AccountView) -> bool:
        if v.seven_day_reset is None:
            return False
        if v.number == active:
            return v.seven_day_headroom >= SPENT_7D_PCT
        return absorbable(v, burn_pct_per_h, now) >= MEANINGFUL_ABSORB_PCT

    live = [v for v in pool if worthwhile(v)]
    if not live:
        return []
    earliest = min(_reset(v) for v in live)
    return [v for v in live if _reset(v) - earliest <= similar_reset_s]


def rank(
    pool: list[AccountView],
    *,
    similar_reset_s: float,
    burn_pct_per_h: float | None,
    now: float,
) -> list[str]:
    """Order would-be targets for a move the engine has to make anyway (the
    active account is at its threshold or unreadable).

    The tie group first, most 5h headroom first inside it; then other
    worthwhile accounts by soonest reset; then the rest by most headroom.
    Sorting is stable, so the caller's order breaks remaining ties.
    """
    group = _tie_group(
        pool, similar_reset_s=similar_reset_s, burn_pct_per_h=burn_pct_per_h,
        now=now, active=None,
    )
    in_group = {v.number for v in group}
    first = sorted(group, key=lambda v: -v.five_hour_headroom)
    worthwhile = sorted(
        (
            v for v in pool
            if v.number not in in_group
            and v.seven_day_reset is not None
            and absorbable(v, burn_pct_per_h, now) >= MEANINGFUL_ABSORB_PCT
        ),
        key=_reset,
    )
    seen = in_group | {v.number for v in worthwhile}
    rest = sorted((v for v in pool if v.number not in seen), key=lambda v: -v.headroom)
    return [v.number for v in (*first, *worthwhile, *rest)]


def choose(
    active: AccountView,
    candidates: list[AccountView],
    *,
    threshold: float,
    similar_reset_s: float,
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

    if not candidates:
        return stay("no other account is under the threshold")
    if active.seven_day_reset is None:
        return stay("this account's 7d reset time is unknown")

    pool = [active, *candidates]
    group = _tie_group(
        pool, similar_reset_s=similar_reset_s, burn_pct_per_h=burn_pct_per_h,
        now=now, active=me,
    )
    if not group:
        return stay("no account has meaningful 7d headroom left")
    by_five_hour = sorted(group, key=lambda v: -v.five_hour_headroom)
    my_reset_in = fmt_duration(active.seven_day_reset - now)
    similar_label = fmt_duration(similar_reset_s)

    if any(v.number == me for v in group):
        others = [v for v in by_five_hour if v.number != me]
        if not others:
            return stay(
                f"resets soonest ({my_reset_in}), "
                f"{active.seven_day_headroom:.0f}% headroom"
            )
        five_used = 100.0 - active.five_hour_headroom
        if five_used < threshold - FIVE_HOUR_NEAR_MARGIN_PCT:
            return stay(
                f"7d resets within {similar_label} of #{others[0].number}; "
                f"5h {five_used:.0f}% used, keeping #{others[0].number} as "
                "the 5h backup"
            )
        backup = others[0]
        if backup.five_hour_headroom - active.five_hour_headroom < hysteresis_pct:
            return stay(
                f"5h {five_used:.0f}% used but no tied account has "
                f"{hysteresis_pct:.0f} points more 5h headroom"
            )
        return Choice(
            "switch",
            backup.number,
            f"switch → #{backup.number}: 5h {five_used:.0f}% used (near limit); "
            f"#{backup.number}'s 7d resets within {similar_label} with "
            f"{backup.five_hour_headroom:.0f}% 5h headroom",
        )

    target = by_five_hour[0]
    gain = _reset(active) - _reset(target)
    if active.seven_day_headroom < SPENT_7D_PCT:
        gain = math.inf  # nothing here worth staying for
    if gain < LARGE_GAIN_FACTOR * similar_reset_s:
        return stay(
            f"#{target.number} resets only {fmt_duration(gain)} sooner "
            f"(a move before the threshold needs "
            f"{fmt_duration(LARGE_GAIN_FACTOR * similar_reset_s)})"
        )
    return Choice(
        "switch",
        target.number,
        f"switch → #{target.number}: #{me}'s 7d resets in {my_reset_in}, "
        f"#{target.number}'s in {fmt_duration(_reset(target) - now)}",
    )
