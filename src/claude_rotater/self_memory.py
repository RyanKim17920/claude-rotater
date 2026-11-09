"""rota's own memory: an hourly line in rota.log and a header hint when high.

The number is the process's peak resident set size from ``getrusage``: one
cheap syscall, no subprocess, and once rota has been that large the hint is
worth seeing until a restart. ``None`` where the platform has no
``resource`` module (Windows).
"""

from __future__ import annotations

import logging
import sys

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

LOG_EVERY_S = 3600.0
WARN_MB = 300.0


def peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB, or None if unknown."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def warning_text(mb: float | None) -> str:
    """The header hint, or "" while memory is normal."""
    if mb is None or mb < WARN_MB:
        return ""
    return f"mem {mb:.0f} MB, restart rota"


class HourlyLog:
    """Logs the peak RSS at most once per ``LOG_EVERY_S`` of ``now``."""

    def __init__(self) -> None:
        self._last: float | None = None

    def maybe_log(self, now: float, mb: float | None) -> bool:
        if mb is None or (self._last is not None and now - self._last < LOG_EVERY_S):
            return False
        self._last = now
        _logger.info("rota memory: peak RSS %.0f MB", mb)
        return True
