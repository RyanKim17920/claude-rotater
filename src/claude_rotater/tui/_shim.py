"""INTEGRATION SHIM — delete when the feed-overhaul branch lands.

The TUI codes against three APIs another branch is building:

- ``claude_rotater.statusline_setup``: ``status()``, ``install()``,
  ``uninstall()``;
- ``UsageHistory.long_series(identity, label, since) -> list[Point]``;
- ``UsageHistory.windows(identity, label, since) -> list[WindowSummary]``
  with ``WindowSummary(resets_at, peak_pct)``.

Until then this module answers the same questions from what exists today, and
calls the real API whenever it is importable/present, so the TUI works on
either side of the merge. At integration: point the TUI's four call sites at
the real functions, drop this file and its tests (tests/test_tui_shim.py).

Safety: the fallbacks only *read* rota's own feed file. ``install`` and
``uninstall`` refuse when the real module is missing; nothing here writes
under ~/.claude.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from claude_rotater.exceptions import ClaudeSwitchError
from claude_rotater.usage_history import Point

FeedState = Literal["live", "stale", "off"]
RESET_MATCH_S = 120.0  # same tolerance the estimator uses for resets_at jitter


@dataclass(frozen=True)
class WindowSummary:
    """One finished (or running) usage window: when it reset and its peak."""

    resets_at: float
    peak_pct: float


@dataclass(frozen=True)
class FeedStatus:
    state: FeedState
    installed: bool


def _setup_module():
    try:
        from claude_rotater import statusline_setup  # type: ignore[attr-defined]
    except ImportError:
        return None
    return statusline_setup


def _normalize(raw: object) -> FeedStatus:
    """Accept a plain state string or an object with ``state``/``installed``."""
    state = getattr(raw, "state", raw)
    state = getattr(state, "value", state)  # an Enum
    state = str(state).lower()
    if state not in ("live", "stale", "off"):
        state = "off"
    installed = getattr(raw, "installed", state != "off")
    return FeedStatus(state, bool(installed))  # type: ignore[arg-type]


def feed_status(backup_dir: Path, *, now: float | None = None) -> FeedStatus:
    """Is the statusline feed set up, and is it delivering? Never raises."""
    module = _setup_module()
    if module is not None:
        try:
            return _normalize(module.status())
        except Exception:
            return FeedStatus("off", False)
    from claude_rotater import feed, poll_policy

    try:
        mtime = os.stat(feed.feed_path(backup_dir)).st_mtime
    except OSError:
        return FeedStatus("off", False)
    try:
        stale_s = poll_policy.load_cadence(backup_dir).feed_stale_s
    except Exception:
        stale_s = poll_policy.FEED_STALE_S
    age = (now if now is not None else time.time()) - mtime
    return FeedStatus("live" if age <= stale_s else "stale", True)


def install_statusline() -> object:
    module = _setup_module()
    if module is None:
        raise ClaudeSwitchError("statusline setup is not available in this build yet")
    return module.install()


def uninstall_statusline() -> object:
    module = _setup_module()
    if module is None:
        raise ClaudeSwitchError("statusline setup is not available in this build yet")
    return module.uninstall()


def long_series(history, num: str, identity, label: str, since: float) -> list[Point]:
    """Points of one window label at or after ``since``, oldest first."""
    real = getattr(history, "long_series", None)
    if callable(real):
        return list(real(identity, label, since))
    points = history.series({num: identity}).get(num, {}).get(label, [])
    return [p for p in points if p.t >= since]


def windows(history, num: str, identity, label: str, since: float) -> list[WindowSummary]:
    """Per-window peaks of one label whose reset falls at or after ``since``."""
    real = getattr(history, "windows", None)
    if callable(real):
        return [WindowSummary(float(w.resets_at), float(w.peak_pct)) for w in real(identity, label, since)]
    return summarize_windows(long_series(history, num, identity, label, since))


def summarize_windows(points: list[Point]) -> list[WindowSummary]:
    """Group points by their ``resets_at`` (within jitter) and keep each peak."""
    out: list[WindowSummary] = []
    for p in sorted((p for p in points if p.resets_at is not None), key=lambda p: p.resets_at):
        if out and abs(p.resets_at - out[-1].resets_at) <= RESET_MATCH_S:
            if p.pct > out[-1].peak_pct:
                out[-1] = WindowSummary(out[-1].resets_at, p.pct)
        else:
            out.append(WindowSummary(p.resets_at, p.pct))
    return out
