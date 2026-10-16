"""TEMPORARY stand-in for the WP1 modules (``estimator``, ``usage_history``).

DELETE AT INTEGRATION: once ``claude_swap.estimator`` and
``claude_swap.usage_history`` exist, every ``from claude_swap.tui._wp1_stub
import X`` becomes a direct import from those modules and this file goes.

The real modules win whenever they are importable, so code and tests written
against this shim already run against WP1 the moment it lands. The fallback
below copies the spec's signatures (tasks/todo.md §2.2/§2.3) and its math, so
the TUI's tests mean the same thing before and after integration.
"""

from __future__ import annotations

try:  # pragma: no cover - exercised only once WP1 is merged
    from claude_swap.estimator import (  # type: ignore[import-not-found]
        WINDOW_LEN_S,
        AccountEstimate,
        WindowEstimate,
        estimate_account,
    )
    from claude_swap.usage_history import Point  # type: ignore[import-not-found]
except ImportError:
    import math
    from dataclasses import dataclass
    from typing import Literal, Sequence

    WINDOW_LEN_S = {"5h": 5 * 3600.0, "7d": 7 * 86400.0}

    @dataclass(frozen=True)
    class Point:  # type: ignore[no-redef]
        t: float
        pct: float
        resets_at: float | None

    @dataclass(frozen=True)
    class _WindowParams:
        lookback_s: float
        half_life_s: float
        high_span_s: float

    _PARAMS_5H = _WindowParams(90 * 60, 30 * 60, 30 * 60)
    _PARAMS_WEEK = _WindowParams(24 * 3600, 6 * 3600, 3 * 3600)
    _MIN_SPAN_S = 240.0
    _RESET_DROP_PCT = 2.0
    _RESET_MATCH_S = 120.0
    _MAX_RATE = 200.0

    Confidence = Literal["none", "low", "medium", "high"]

    @dataclass(frozen=True)
    class WindowEstimate:  # type: ignore[no-redef]
        window: str
        last_pct: float
        last_t: float
        resets_at: float | None
        rate_pct_per_h: float | None
        confidence: str
        points: tuple[Point, ...]

        def projected_pct(self, t: float) -> float | None:
            if self.rate_pct_per_h is None or t < self.last_t:
                return None
            if self.resets_at is not None and t >= self.resets_at:
                return None
            return min(100.0, self.last_pct + self.rate_pct_per_h * (t - self.last_t) / 3600)

        def eta_s(self, target_pct: float, now: float) -> float | None:
            proj = self.projected_pct(now)
            if proj is not None and proj >= target_pct:
                return 0.0
            if self.rate_pct_per_h is None or self.rate_pct_per_h <= 0.05:
                return None
            t_star = self.last_t + (target_pct - self.last_pct) / self.rate_pct_per_h * 3600
            if self.resets_at is not None and t_star >= self.resets_at:
                return None
            return max(0.0, t_star - now)

    def _segment(points: Sequence[Point], now: float, lookback_s: float) -> list[Point]:
        newest = points[-1]
        seg = [newest]
        for i in range(len(points) - 2, -1, -1):
            p, nxt = points[i], points[i + 1]
            if (
                p.resets_at is not None
                and newest.resets_at is not None
                and abs(p.resets_at - newest.resets_at) > _RESET_MATCH_S
            ) or p.pct > nxt.pct + _RESET_DROP_PCT:
                break
            seg.append(p)
        seg.reverse()
        return [p for p in seg if p.t >= now - lookback_s]

    def estimate_window(
        points: Sequence[Point], *, window: str, now: float
    ) -> WindowEstimate | None:
        if not points:
            return None
        params = _PARAMS_5H if window == "5h" else _PARAMS_WEEK
        newest = points[-1]
        if newest.resets_at is not None and newest.resets_at <= now:
            return WindowEstimate(window, 0.0, newest.t, newest.resets_at, None, "none", ())
        seg = _segment(points, now, params.lookback_s) or [newest]
        span = seg[-1].t - seg[0].t
        if len(seg) >= 2 and span >= _MIN_SPAN_S:
            w = [0.5 ** ((now - p.t) / params.half_life_s) for p in seg]
            sw = sum(w)
            tb = sum(wi * p.t for wi, p in zip(w, seg)) / sw
            pb = sum(wi * p.pct for wi, p in zip(w, seg)) / sw
            den = sum(wi * (p.t - tb) ** 2 for wi, p in zip(w, seg))
            slope = sum(wi * (p.t - tb) * (p.pct - pb) for wi, p in zip(w, seg)) / den
            rate = min(_MAX_RATE, max(0.0, slope * 3600))
            rms = math.sqrt(
                sum(wi * (p.pct - (pb + slope * (p.t - tb))) ** 2 for wi, p in zip(w, seg)) / sw
            )
            if len(seg) >= 4 and span >= params.high_span_s and rms <= 2.0:
                conf = "high"
            elif len(seg) >= 3 and span >= 2 * _MIN_SPAN_S:
                conf = "medium"
            else:
                conf = "low"
        else:
            rate, conf = None, "none"
            length = WINDOW_LEN_S.get(window, WINDOW_LEN_S["7d"])
            if newest.resets_at is not None:
                elapsed = newest.t - (newest.resets_at - length)
                if elapsed >= 600:
                    rate, conf = newest.pct / elapsed * 3600, "low"
        return WindowEstimate(
            window, newest.pct, newest.t, newest.resets_at, rate, conf, tuple(seg)
        )

    @dataclass(frozen=True)
    class AccountEstimate:  # type: ignore[no-redef]
        windows: dict[str, WindowEstimate]
        binding: WindowEstimate | None
        eta_threshold_s: float | None
        eta_limit_s: float | None
        confidence: str

    def _min_or_none(values: list[float | None]) -> float | None:
        known = [v for v in values if v is not None]
        return min(known) if known else None

    def estimate_account(
        series: dict[str, list[Point]],
        *,
        models: tuple[str, ...],
        threshold: float,
        now: float,
    ) -> AccountEstimate:
        wanted = {m.lower() for m in models}
        windows: dict[str, WindowEstimate] = {}
        for label, pts in series.items():
            if label not in ("5h", "7d") and not ("all" in wanted or label.lower() in wanted):
                continue
            est = estimate_window(pts, window=label, now=now)
            if est is not None:
                windows[label] = est
        if not windows:
            return AccountEstimate({}, None, None, None, "none")

        def level(e: WindowEstimate) -> float:
            proj = e.projected_pct(now)
            return proj if proj is not None else e.last_pct

        binding = max(windows.values(), key=level)
        return AccountEstimate(
            windows,
            binding,
            _min_or_none([e.eta_s(threshold, now) for e in windows.values()]),
            _min_or_none([e.eta_s(100.0, now) for e in windows.values()]),
            binding.confidence,
        )


__all__ = [
    "WINDOW_LEN_S",
    "AccountEstimate",
    "Point",
    "WindowEstimate",
    "estimate_account",
]
