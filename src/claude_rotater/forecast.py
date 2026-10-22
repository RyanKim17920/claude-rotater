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

from claude_rotater.estimator import AccountEstimate, estimate_fleet
from claude_rotater.usage_history import Point, shared_duty

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

    series = history.series(identities)
    estimates = estimate_fleet(
        {num: series.get(num, {}) for num in identities},
        active=active, models=models, threshold=threshold,
        now=time.time() if now is None else now,
        since=read_last_switch_at(data_dir),
        shared=shared_duty(history.duty(), identities, data_dir),
    )
    return estimates, series
