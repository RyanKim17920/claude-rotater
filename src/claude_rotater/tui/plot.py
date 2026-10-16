"""Pure plot renderers for the main page: a sparkline and a braille line plot.

Everything here takes data in and returns ``rich.text.Text``; nothing reads
the clock, the store, or the app. That keeps the drawing exactly testable
(golden renders) and lets the app call it from any thread.

The line plot draws one usage window on a braille canvas (2x4 dots per
cell): the measured series as a solid line, the estimator's projection as a
dotted line from the last measurement up to the reset (or 100%), the
auto-switch threshold as a dashed row, and the reset as a marker column.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from rich.text import Text

from claude_rotater.estimator import WINDOW_LEN_S, WindowEstimate
from claude_rotater.tui.theme import Palette
from claude_rotater.usage_history import Point

SPARK_GLYPHS = "▁▂▃▄▅▆▇█"
THRESHOLD_GLYPH = "┄"
RESET_GLYPH = "┊"
GUTTER = 4  # "100┤" — y-axis label plus its tick
MIN_PLOT_WIDTH = 20
MIN_PLOT_HEIGHT = 3  # total lines, including the time-axis row
PROJECTION_STEP = 2  # every Nth dot along the projected line is lit

# Braille dot bit for (row 0..3 top-down, column 0..1) inside one cell.
_DOT_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
_BRAILLE_BASE = 0x2800


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def sparkline(
    points: Sequence[Point],
    width: int,
    *,
    lo: float = 0.0,
    hi: float = 100.0,
    palette: Palette = Palette.DARK,
    span: tuple[float, float] | None = None,
) -> Text:
    """One-line sparkline, ``width`` cells, bucketed by time.

    ``span`` is the ``(t0, t1)`` the cells cover; by default the points'
    own first..last time. Each cell shows the newest value that falls in its
    time bucket, so a monotone series stays monotone; a bucket with no data
    is a space (a gap in polling reads as a gap, not as a flat line).
    """
    text = Text(no_wrap=True)
    if width <= 0:
        return text
    pts = sorted(points, key=lambda p: p.t)
    if not pts:
        text.append(" " * width)
        return text
    t0, t1 = span if span is not None else (pts[0].t, pts[-1].t)
    buckets: list[float | None] = [None] * width
    for p in pts:
        if p.t < t0 or p.t > t1:
            continue
        if t1 <= t0:
            i = width - 1
        else:
            i = min(width - 1, int((p.t - t0) / (t1 - t0) * width))
        buckets[i] = p.pct  # sorted by time: the newest value in the bucket wins
    scale = hi - lo if hi > lo else 1.0
    for value in buckets:
        if value is None:
            text.append(" ")
            continue
        frac = _clamp((value - lo) / scale, 0.0, 1.0)
        glyph = SPARK_GLYPHS[round(frac * (len(SPARK_GLYPHS) - 1))]
        text.append(glyph, style=palette.severity(value))
    return text


class _Canvas:
    """Braille dot grid. Dot (0, 0) is the bottom-left corner."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        self.dot_w = cols * 2
        self.dot_h = rows * 4
        self.solid = [[0] * cols for _ in range(rows)]
        self.dotted = [[0] * cols for _ in range(rows)]

    def set(self, x: int, y: int, *, dotted: bool = False) -> None:
        if not (0 <= x < self.dot_w and 0 <= y < self.dot_h):
            return
        top_down = self.dot_h - 1 - y
        row, sub_row = divmod(top_down, 4)
        col, sub_col = divmod(x, 2)
        layer = self.dotted if dotted else self.solid
        layer[row][col] |= _DOT_BITS[sub_row][sub_col]

    def line(self, x0: int, y0: int, x1: int, y1: int, *, dotted: bool = False) -> None:
        """Bresenham between two dots; dotted lights every PROJECTION_STEP-th."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        step = 0
        while True:
            if not dotted or step % PROJECTION_STEP == 0:
                self.set(x0, y0, dotted=dotted)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            step += 1


def _clock(t: float, window_len_s: float) -> str:
    fmt = "%H:%M" if window_len_s <= 86400 else "%a %H:%M"
    return datetime.fromtimestamp(t).strftime(fmt)


def _place(buf: list[str], start: int, label: str) -> bool:
    """Write ``label`` into ``buf`` at ``start`` if it fits on blank cells
    with one blank cell of breathing room on each side."""
    start = int(_clamp(start, 0, len(buf) - len(label)))
    lo, hi = max(0, start - 1), min(len(buf), start + len(label) + 1)
    if start < 0 or start + len(label) > len(buf):
        return False
    if any(c != " " for c in buf[lo:hi]):
        return False
    buf[start : start + len(label)] = list(label)
    return True


def line_plot(
    est: WindowEstimate,
    *,
    now: float,
    threshold: float | None,
    width: int,
    height: int,
    palette: Palette = Palette.DARK,
    window_len_s: float | None = None,
    lookback_s: float | None = None,
    points: Sequence[Point] | None = None,
) -> list[Text]:
    """Multi-row plot of one window; ``height`` lines including the time axis.

    The x range is ``[max(window_start, now - lookback_s), resets_at or now +
    1h]`` (``lookback_s`` defaults to the window length, i.e. the whole
    current window) and y is 0-100. ``points`` overrides the estimate's own
    segment as the measured series (the app passes the longer stored history
    of the current window). Falls back to a one-line sparkline when the area
    is too small to draw (``height < 3`` or ``width < 20``).
    """
    pts = sorted(points if points is not None else est.points, key=lambda p: p.t)
    if height < MIN_PLOT_HEIGHT or width < MIN_PLOT_WIDTH:
        return [sparkline(pts, max(0, width), palette=palette)]

    window_len = window_len_s or WINDOW_LEN_S.get(est.window, WINDOW_LEN_S["7d"])
    lookback = lookback_s or window_len
    x0 = now - lookback
    if est.resets_at is not None:
        x0 = max(est.resets_at - window_len, x0)
    x1 = est.resets_at if est.resets_at is not None else now + 3600.0
    if x1 <= x0:
        x1 = x0 + 1.0

    cols, rows = width - GUTTER, height - 1
    canvas = _Canvas(cols, rows)

    def dx(t: float) -> int:
        return round(_clamp((t - x0) / (x1 - x0), 0.0, 1.0) * (canvas.dot_w - 1))

    def dy(pct: float) -> int:
        return round(_clamp(pct, 0.0, 100.0) / 100.0 * (canvas.dot_h - 1))

    measured = [p for p in pts if x0 <= p.t <= now]
    for a, b in zip(measured, measured[1:]):
        canvas.line(dx(a.t), dy(a.pct), dx(b.t), dy(b.pct))
    if len(measured) == 1:
        canvas.set(dx(measured[0].t), dy(measured[0].pct))

    rate = est.rate_pct_per_h
    if rate is not None and x0 <= est.last_t < x1:
        t_end = x1
        if rate > 0:
            t_end = min(t_end, est.last_t + (100.0 - est.last_pct) / rate * 3600.0)
        p_end = min(100.0, est.last_pct + rate * (t_end - est.last_t) / 3600.0)
        canvas.line(
            dx(est.last_t), dy(est.last_pct), dx(t_end), dy(p_end), dotted=True
        )

    thr_row = None
    if threshold is not None:
        thr_row = (canvas.dot_h - 1 - dy(threshold)) // 4
    reset_col = None
    if est.resets_at is not None and x0 <= est.resets_at <= x1:
        reset_col = dx(est.resets_at) // 2

    lines: list[Text] = []
    for r in range(rows):
        line = Text(no_wrap=True)
        if r == 0:
            label = "100"
        elif r == rows - 1:
            label = "0"
        elif r == thr_row and threshold is not None:
            label = f"{threshold:.0f}"
        else:
            label = ""
        line.append(f"{label:>{GUTTER - 1}}{'┤' if label else '│'}", style=palette.muted)
        # Color by height: a cell's severity is its row's midpoint pct, so the
        # curve warms as it climbs instead of taking one flat color.
        row_pct = 100.0 * (rows - r - 0.5) / rows
        color = palette.severity(row_pct)
        for c in range(cols):
            solid, dotted = canvas.solid[r][c], canvas.dotted[r][c]
            if solid or dotted:
                glyph = chr(_BRAILLE_BASE | solid | dotted)
                line.append(glyph, style=color if solid else f"{color} dim")
            elif r == thr_row:
                line.append(THRESHOLD_GLYPH, style=f"{palette.sev_warn} dim")
            elif c == reset_col:
                line.append(RESET_GLYPH, style=palette.muted)
            else:
                line.append(" ")
        lines.append(line)

    axis = [" "] * cols
    _place(axis, 0, _clock(x0, window_len))
    right = _clock(x1, window_len)
    _place(axis, cols - len(right), right)
    if x0 <= now <= x1:
        _place(axis, dx(now) // 2 - 1, "now")
    lines.append(Text(" " * GUTTER + "".join(axis), style=palette.muted, no_wrap=True))
    return lines


__all__ = ["line_plot", "sparkline"]
