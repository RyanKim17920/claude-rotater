"""Cadence policy for the ``/api/oauth/usage`` endpoint — every number in one place.

The endpoint enforces a budget on non-first-party clients: a **~60-minute
window of ~28-30 requests per identity × UA-class** (measured 2026-07-11,
probe3, two runs: a rested identity admitted 30 requests before the first
429; the post-drain 429 oscillation ended exactly when the drain burst aged
60 minutes; steady 1/180 s polling then ran 96 minutes from a rested window
with zero 429s). It is NOT a bucket with a refill rate: capacity returns
only as old requests age out of the trailing hour, so a burst saturates the
identity for up to a full hour — pausing does not restore headroom early,
and earlier "refill rate" estimates were artifacts of measuring while
saturated.

What that identity is depends on which 429 regime the org is on — the two
regimes coexist across orgs (see the Retry-After discussion in
``usage_store``). Under the fixed-deadline regime it is the **account/org**
(measured 2026-07-28: a freshly minted token was blocked 135 s after issue,
which a per-token counter cannot produce). Under the saturated-edge
(``Retry-After: 0``) regime it is the **access token** (measured 2026-07-29,
probe4: at saturation, a freshly minted token of the same lineage was
admitted while the old token stayed blocked, requests interleaved). Plan for
the account-scoped case — it is the conservative one: re-authenticating
cannot be relied on to clear a block, and two machines holding different
tokens for one account may share one budget, which is what
``POST_429_BACKOFF_MULT`` below exists to converge.

Error bars: the horizon is bracketed to ~55-64 minutes from a single
transition event, the exact edge algorithm (likely a Cloudflare
sliding-window approximation) is undocumented, and Anthropic can retune it
any day — so the constants below lean only on the robust parts: a sustained
rate safely under the cap, and an ~hour recovery horizon. The budget target
is an **average of at most ~1 request / 3 minutes** (20/hour vs the ~28-30/
hour cap), leaving ~8-10 requests/hour of headroom for manual commands,
and wake-from-sleep catch-up.

Health invariant to watch in the logs: steady state shows zero http-429.
A post-burst 429 does NOT reliably clear at its stated horizon — measured
over one machine's full log (re-measured 2026-08-03; re-derive rather than
trust this verbatim, it ages as the log grows — method recorded next to
``usage_store.RETRY_AFTER_MARGIN_S``), 20 of 35 lapsed blocks re-blocked
within 900s of their own deadline (+2s..+887s), each for a fresh full hour
("of 35", not 38 raw gaps: 3 are negative — NOT a uniform mechanism (one has
no within-block revision at all, one is revised BACKWARD mid-block, one is
unchanged — see usage_store.RETRY_AFTER_MARGIN_S's comment for the per-gap
detail) — excluded from both numerator and denominator; round 8 switched
from the prior "21 of 36"/"2 of 38" figures here to these, on the OTHER of
two equally-reproducing readings — see usage_store.RETRY_AFTER_MARGIN_S's
comment for which reading and why).
The prior "10 of 23" figure here did not reproduce under any of 40 method
variants swept and is superseded. That is why the wait is Retry-After plus
``usage_store.RETRY_AFTER_MARGIN_S`` and not Retry-After alone. What would
mean this model needs revisiting is a 429 episode at modest rates that
outlasts an hour *past* that margin.

Plans computed here are persisted per account in the usage store
(``nextPollAt``/``pollIntervalS``) by whichever collector fetched, so every
surface — ``cswap status``, the TUI, the auto engine — inherits
the same cadence no matter how often it repaints.

If a future probe revises the measured shape, adjust the constants in this
module only.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import oauth

if TYPE_CHECKING:
    from claude_swap.estimator import AccountEstimate

# Freshness floor shared by every collector: an entry younger than this is
# served from the store without any fetch, so the maximum sustained rate on
# one token is 1/SERVE_TTL_S regardless of how many surfaces are open.
SERVE_TTL_S = 180.0

# Approach floor: an account the estimator projects to cross the threshold
# soon is sampled down to this cadence, never below.
MIN_INTERVAL_S = 180.0

# The one baseline, for every account: ten minutes, 6 requests/hour. The
# active account's live usage comes from Claude Code's statusline
# (``cswap feed``) at no request cost, and while that feed is fresh the
# account is held off polling entirely (see FEED_STALE_S); it is polled like
# a spare only once the feed goes quiet. A DEFAULT; the effective value comes
# from settings (see PollCadence).
SPARE_INTERVAL_S = 600.0

# How long a statusline feed record keeps the active account off polling. No
# Claude Code session reporting for this long usually means nothing is
# burning, so spare cadence is enough. A DEFAULT, see PollCadence.
FEED_STALE_S = 600.0

# Aim to sample about this many times before the projected threshold
# crossing, so the switch decision sees the crossing at most one poll late.
POLLS_BEFORE_CROSSING = 3

# Planned requests per token per trailing hour (the measured cap is ~28-30),
# enforced last from the on-disk attempt ledger (usage_history), so nothing
# schedules above it. A default; the effective value comes from settings.
HOURLY_BUDGET = 20

# Kept for readers outside this module: the candidate names for the baseline.
CANDIDATE_DEFAULT_INTERVAL_S = SPARE_INTERVAL_S
CANDIDATE_MAX_INTERVAL_S = SPARE_INTERVAL_S

# Exhaustion is stable enough to poll slowly, but not to stop polling until a
# reported reset. Quota grants and provider-side corrections can make an
# account usable before that timestamp, and decision-grade status must not age
# into "unavailable" while the scheduler is deliberately waiting. Ten-minute
# polling (six requests/hour) stays below the measured budget and
# detects recovery promptly; a nearer reported reset still pulls the next poll
# forward.
EXHAUSTED_INTERVAL_S = 600.0

# ±fraction applied to each scheduled interval so independent processes
# (TUI + auto) drift apart instead of fetching in lockstep.
JITTER_FRAC = 0.1

# Reaction to a 429 with ``Retry-After: 0`` (the saturated-window edge):
# probe at most every 5 minutes (≤12/hour) so aging-out — up to ~30/hour —
# outpaces the probing (used by the usage store's failure backoff)...
EDGE_BACKOFF_S = 300.0
# ...and while any 429 was seen on the token within this window, floor the
# planned cadence here so freed capacity accumulates instead of being
# re-spent. The window matches the saturation horizon: a full trailing hour
# takes up to 60 minutes to age out.
POST_429_MIN_INTERVAL_S = 360.0
RECENT_429_WINDOW_S = 3600.0

# AIMD on a contended budget. The budget is shared across every machine
# polling the same account (under the account-scoped regime a machine with its
# own token is no less a competitor — see the module docstring on scope),
# none of them can see the others, and the endpoint
# exposes no remaining-request count — only a Retry-After once already
# blocked. So while 429s recur, each successful poll multiplicatively grows the
# interval (×POST_429_BACKOFF_MULT) toward POST_429_MAX_INTERVAL_S — wider than
# the normal candidate ceiling so several machines can each back off far enough
# that their combined rate fits under the budget. Once the 429s age out of
# RECENT_429_WINDOW_S the planner returns to the account's baseline.
# This is TCP-style congestion control: the budget gets fair-shared by
# reaction alone, with no machine count or shared state to configure.
POST_429_BACKOFF_MULT = 1.5
POST_429_MAX_INTERVAL_S = 1800.0

# The engine escalates to a full candidate refresh when the active account is
# within this margin of the threshold (decision policy, but the approach
# cadence keys on the same band, so it lives with the cadence numbers).
ESCALATION_MARGIN_PCT = 15.0

# Never schedule a poll later than a known window reset (+ slack): stored
# usage is obsolete the moment the window rolls over.
RESET_SLACK_S = 60.0

TRAILING_HOUR_S = 3600.0


@dataclass(frozen=True)
class PollCadence:
    """The user-tunable cadence knobs; defaults are the constants above.

    Read from ``settings.json`` (``autoswitch.pollSpareSeconds``,
    ``pollHourlyBudget``, ``feedStaleSeconds``) by :func:`load_cadence`.
    Other keys in the section (such as the retired ``pollActiveSeconds``,
    ``pollUrgentSeconds`` and ``intervalSeconds``) are ignored. The post-429
    AIMD is not tunable: it is the safety net that converges whatever these
    are set to."""

    spare_s: float = SPARE_INTERVAL_S
    hourly_budget: int = HOURLY_BUDGET
    feed_stale_s: float = FEED_STALE_S


DEFAULT_CADENCE = PollCadence()

# settings.json key (autoswitch section) -> (field, kind, lo, hi)
CADENCE_SETTING_KEYS: dict[str, tuple[str, str, float, float]] = {
    "pollSpareSeconds": ("spare_s", "float", 10.0, 3600.0),
    "pollHourlyBudget": ("hourly_budget", "int", 1.0, 3600.0),
    "feedStaleSeconds": ("feed_stale_s", "float", 60.0, 3600.0),
}


def cadence_from_section(section: object) -> PollCadence:
    """Build a cadence from the raw ``autoswitch`` settings section. Missing
    or non-numeric values take the default; numbers are clamped into range."""
    if not isinstance(section, dict):
        return DEFAULT_CADENCE
    kwargs: dict[str, float | int] = {}
    for key, (field_name, kind, lo, hi) in CADENCE_SETTING_KEYS.items():
        value = section.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            continue
        clamped = min(max(float(value), lo), hi)
        kwargs[field_name] = int(clamped) if kind == "int" else clamped
    return PollCadence(**kwargs)


def load_cadence(backup_root: Path) -> PollCadence:
    """The effective cadence from ``settings.json``; never raises."""
    from claude_swap.settings import _read_raw, settings_path

    try:
        return cadence_from_section(_read_raw(settings_path(backup_root)).get("autoswitch"))
    except Exception:
        return DEFAULT_CADENCE


def binding_pct(usage: dict | None, models: tuple[str, ...] = ()) -> float | None:
    """Utilization of the binding (worst) relevant window, or None."""
    headroom = oauth.account_headroom(usage, models)
    return None if headroom is None else 100.0 - headroom


def limiting_reset_ts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch when the last of the ≥100% relevant windows resets (account
    usable again)."""
    latest: float | None = None
    for _, pct, resets_at in oauth.relevant_windows(usage, models):
        if pct < 100.0:
            continue
        ts = parse_reset_ts(resets_at)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def earliest_future_reset_ts(
    usage: dict | None, now: float, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch of the next relevant-window reset ahead of ``now``, any
    utilization."""
    earliest: float | None = None
    for _, _, resets_at in oauth.relevant_windows(usage, models):
        ts = parse_reset_ts(resets_at)
        if ts is not None and ts > now and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def parse_reset_ts(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def trailing_attempts(attempts: Sequence[float], now: float) -> list[float]:
    """Attempts inside the trailing hour ending at ``now``, oldest first."""
    return sorted(a for a in attempts if now - TRAILING_HOUR_S < a <= now)


def budget_release_at(
    attempts: Sequence[float], now: float, budget: int = HOURLY_BUDGET
) -> float | None:
    """Earliest time another request fits the hourly budget, or None if one
    fits now.

    With ``budget`` attempts already in the trailing hour, the next request
    waits until the oldest of the newest ``budget`` ages out."""
    trailing = trailing_attempts(attempts, now)
    if budget <= 0:
        return (trailing[-1] + TRAILING_HOUR_S) if trailing else None
    if len(trailing) < budget:
        return None
    return trailing[-budget] + TRAILING_HOUR_S


def plan_after_fetch(
    *,
    prev_interval_s: float | None,
    prev_usage: dict | None,
    new_usage: dict | None,
    is_active: bool,
    threshold: float,
    models: tuple[str, ...],
    recent_429: bool,
    now: float,
    rng: Callable[[], float] = random.random,
    estimate: AccountEstimate | None = None,
    attempts: Sequence[float] = (),
    cadence: PollCadence = DEFAULT_CADENCE,
) -> tuple[float, float]:
    """``(next_poll_at, interval_s)`` for an account just fetched successfully.

    In order:

    1. Start from the baseline, ``cadence.spare_s``, for every account: the
       active account's live usage comes from the statusline feed, which
       holds it off polling while fresh (``UsageStore.adopt``), so a plan
       here only runs once the feed has gone quiet.
    2. An exhausted account polls no faster than ``EXHAUSTED_INTERVAL_S``
       (bounded, so an early quota grant is still observed).
    3. Otherwise, when the estimator projects a threshold crossing, shrink
       the interval to ``eta / POLLS_BEFORE_CROSSING``, floored at
       ``MIN_INTERVAL_S``. Never above the baseline.
    4. A recent 429 widens the interval multiplicatively (AIMD) toward
       ``POST_429_MAX_INTERVAL_S``.
    5. Jitter by ``JITTER_FRAC``.
    6. Never later than the next known window reset (+ ``RESET_SLACK_S``).
    7. Never earlier than the hourly budget allows (``attempts`` is this
       token's ledger, including the fetch just made). Last, so nothing —
       not even the reset pull-in — schedules above budget.

    ``prev_usage`` and ``is_active`` are accepted for signature
    compatibility: burn is read from ``estimate``, and the active account no
    longer has a cadence of its own.
    """
    del prev_usage, is_active
    baseline = cadence.spare_s
    interval = baseline
    headroom = oauth.account_headroom(new_usage, models)
    exhausted = headroom is not None and headroom <= 0
    if exhausted:
        # Keep probing exhausted accounts: Anthropic can grant/reset quota
        # before the previously advertised timestamp. The approach step is
        # skipped: its ETA is 0 here, which would undo this bound.
        interval = max(interval, EXHAUSTED_INTERVAL_S)
    elif estimate is not None and estimate.confidence != "none":
        eta = estimate.eta_threshold_s
        if eta is not None:
            # A floor is never above the baseline: a user who asked for a
            # faster baseline must not be slowed down near the threshold.
            floor = min(MIN_INTERVAL_S, baseline)
            interval = min(max(eta / POLLS_BEFORE_CROSSING, floor), baseline)
        else:
            binding = estimate.binding
            if (
                binding is not None
                and binding.last_pct >= threshold - ESCALATION_MARGIN_PCT
                and (binding.rate_pct_per_h or 0.0) > 0
            ):
                # Burning inside the band, but the crossing lands after the
                # reset: the baseline is enough (the reset pull-in re-reads
                # the new window).
                interval = min(interval, baseline)
    if recent_429:
        # AIMD additive-increase: grow the interval multiplicatively from the
        # last one toward the wider 429 ceiling, so machines sharing a
        # contended token each retreat until their combined rate fits the
        # budget. Floored at POST_429_MIN_INTERVAL_S for the first 429.
        base = prev_interval_s or baseline
        increased = max(base * POST_429_BACKOFF_MULT, POST_429_MIN_INTERVAL_S)
        interval = min(POST_429_MAX_INTERVAL_S, max(interval, increased))

    next_poll = now + interval * (1.0 + JITTER_FRAC * (2.0 * rng() - 1.0))
    if exhausted:
        reset_ts = limiting_reset_ts(new_usage, models)
        if reset_ts is not None and reset_ts > now:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    else:
        reset_ts = earliest_future_reset_ts(new_usage, now, models)
        if reset_ts is not None:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)

    release = budget_release_at(attempts, now, cadence.hourly_budget)
    if release is not None:
        next_poll = max(next_poll, release)
    return next_poll, interval
