"""In-memory stand-in for ``UsageHistory`` (the read side the TUI uses).

The TUI only calls ``series(identities)``; this fake serves canned series,
honors the identity guard (a slot whose identity differs reads as empty),
and counts reads so tests can assert the history is re-read only when a
fetch stamp advances. Never touches disk.
"""

from __future__ import annotations

from claude_swap.usage_history import Point


class FakeHistory:
    def __init__(
        self,
        series: dict[str, dict[str, list[Point]]] | None = None,
        identities: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._series = series or {}
        self._identities = identities or {}
        self.reads = 0
        self.fail = False

    def series(self, identities: dict[str, tuple[str, str]]) -> dict[str, dict[str, list[Point]]]:
        self.reads += 1
        if self.fail:
            raise OSError("history unreadable")
        out: dict[str, dict[str, list[Point]]] = {}
        for num, ident in identities.items():
            expected = self._identities.get(num)
            if expected is not None and expected != ident:
                continue
            if num in self._series:
                out[num] = self._series[num]
        return out


def linear_points(
    *,
    now: float,
    start_pct: float,
    rate_pct_per_h: float,
    span_s: float,
    step_s: float,
    resets_at: float | None,
) -> list[Point]:
    """Evenly spaced samples of a straight burn ending at ``now``."""
    n = int(span_s // step_s)
    return [
        Point(
            t=now - span_s + i * step_s,
            pct=round(start_pct + rate_pct_per_h * (i * step_s) / 3600, 3),
            resets_at=resets_at,
        )
        for i in range(n + 1)
    ]
