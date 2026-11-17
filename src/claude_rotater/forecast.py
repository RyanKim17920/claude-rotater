"""The one forecast every rota surface shows: TUI rows and charts, the poll
planner, and any view-only window beside the engine.

Inputs are on disk only: the machine's shared usage history (keyed by
account), the profile's last switch (``autoswitch_state.json``) and the
other profiles' duty (``UsageHistory.duty``). So two windows on one data
dir compute the same thing, and an account two profiles hold is forecast
the same in each (:func:`claude_rotater.usage_history.shared_duty`).
"""

from __future__ import annotations

import time
from pathlib import Path

from claude_rotater.estimator import (
    AccountEstimate,
    Span,
    estimate_fleet,
    external_burn,
    off_duty,
)
from claude_rotater.usage_history import Point, duty_spans, shared_duty

Identity = tuple[str, str]


def fleet_estimates(
    history,
    data_dir: Path,
    identities: dict[str, Identity],
    *,
    active: str | None,
    models: tuple[str, ...],
    threshold: float,
    now: float | None = None,
) -> tuple[dict[str, AccountEstimate], dict[str, dict[str, list[Point]]]]:
    """``(estimates, series)`` per slot. Raises when history is unreadable
    (callers degrade to no estimates)."""
    from claude_rotater.feed_ingest import read_last_switch_at

    series = history.live_series(identities)
    estimates = estimate_fleet(
        {num: series.get(num, {}) for num in identities},
        active=active, models=models, threshold=threshold,
        now=time.time() if now is None else now,
        since=read_last_switch_at(data_dir),
        shared=shared_duty(history.duty(), identities, data_dir),
    )
    return estimates, series


def elsewhere(
    duty: dict[str, dict],
    identities: dict[str, Identity],
    data_dir: Path,
    *,
    now: float,
) -> dict[str, list[Span]]:
    """Per slot, the recent spans this profile had another account on duty
    (``estimator.off_duty`` over ``usage_history.duty_spans``): when a rise
    of that account was someone else's use."""
    spans = duty_spans(duty, identities, data_dir, now=now)
    return {num: off_duty(spans, num, now=now) for num in identities}


def external_burns(
    series: dict[str, dict[str, list[Point]]],
    off: dict[str, list[Span]],
    *,
    now: float,
) -> dict[str, float]:
    """Per slot, the 7d points per wall hour used elsewhere
    (``estimator.external_burn``): read off the shared history and this
    profile's duty spans, so a shared account off duty in both profiles
    reads the same in each."""
    return {
        num: external_burn(series.get(num, {}).get("7d", []), spans, now=now)
        for num, spans in off.items()
    }
