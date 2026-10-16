"""In-memory stand-in for ``UsageHistory`` (the read side the TUI uses).

The TUI calls ``series(identities)`` for the current windows and
``long_series``/``windows`` for the long-term view. This fake serves canned
series, derives the long-term answers from the same points (hourly folding
aside, the real store keeps the same shape), honors the identity guard (a slot
whose identity differs reads as empty), and counts ``series`` reads so tests
can assert the history is re-read only when a fetch stamp advances. Never
touches disk.
"""

from __future__ import annotations

from claude_rotater.usage_history import Point, WindowSummary

RESET_MATCH_S = 120.0  # resets_at jitter tolerance, as in the estimator


class FakeHistory:
    def __init__(
        self,
        series: dict[str, dict[str, list[Point]]] | None = None,
        identities: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self._series = series or {}
        self._identities = identities or {}
        self.reads = 0
        self._slot_of: dict[tuple[str, str], str] = {}
        self.fail = False

    def series(self, identities: dict[str, tuple[str, str]]) -> dict[str, dict[str, list[Point]]]:
        self.reads += 1
        if self.fail:
            raise OSError("history unreadable")
        out: dict[str, dict[str, list[Point]]] = {}
        for num, ident in identities.items():
            self._slot_of[ident] = num
            expected = self._identities.get(num)
            if expected is not None and expected != ident:
                continue
            if num in self._series:
                out[num] = self._series[num]
        return out

    def _points(self, identity: tuple[str, str], label: str) -> list[Point]:
        if self.fail:
            raise OSError("history unreadable")
        # The canned series are keyed by slot; the long-term calls name an
        # identity. Resolve it through the slots ``series`` was last asked
        # about (the TUI always reads ``series`` first in the same pass).
        num = self._slot_of.get(identity)
        expected = self._identities.get(num) if num is not None else None
        if num is None or (expected is not None and expected != identity):
            return []
        return list(self._series.get(num, {}).get(label, []))

    def long_series(self, identity: tuple[str, str], label: str, since: float) -> list[Point]:
        return [p for p in self._points(identity, label) if p.t >= since]

    def windows(self, identity: tuple[str, str], label: str, since: float) -> list[WindowSummary]:
        """One summary per distinct ``resets_at`` (within jitter), keeping the peak."""
        out: list[WindowSummary] = []
        points = sorted(
            (p for p in self._points(identity, label) if p.resets_at is not None),
            key=lambda p: p.resets_at,
        )
        for p in points:
            if out and abs(p.resets_at - out[-1].resets_at) <= RESET_MATCH_S:
                if p.pct > out[-1].peak_pct:
                    out[-1] = WindowSummary(out[-1].resets_at, p.pct)
            else:
                out.append(WindowSummary(p.resets_at, p.pct))
        return [w for w in out if w.resets_at >= since]


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
