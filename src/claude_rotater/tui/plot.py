"""Pure plot renderers for the main page: a sparkline and a braille line plot.

Everything here takes data in and returns ``rich.text.Text``; nothing reads
the clock, the store, or the app. That keeps the drawing exactly testable
(golden renders) and lets the app call it from any thread.

The line plot draws one usage window on a braille canvas (2x4 dots per
cell): the measured series as a solid braille line, the estimator's
projection as a row of sparse ``·`` dots from the last measurement up to the
reset (or 100%), the
auto-switch threshold as a dashed row, the reset as a marker column, and a
dim vertical line at "now", where the measured line ends and the projection
begins.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Sequence

from rich.text import Text

from claude_rotater.estimator import WINDOW_LEN_S, WindowEstimate
from claude_rotater.tui.theme import Palette
from claude_rotater.usage_history import Point

SPARK_GLYPHS = "▁▂▃▄▅▆▇█"
THRESHOLD_GLYPH = "┄"
RESET_GLYPH = "┊"
NOW_GLYPH = "│"  # where measurement ends and the projection starts
# The projection is drawn in its own glyph, a row of single centred dots,
# so it reads apart from the dense braille of the measured line even where
# colours are hard to tell apart.
PROJECTION_GLYPH = "·"
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
    projection_color: str | None = None,
) -> list[Text]:
    """Multi-row plot of one window; ``height`` lines including the time axis.

    The projection is told apart from the measured line by its glyph
    (:data:`PROJECTION_GLYPH` dots, not braille), never by fading:
    ``projection_color`` colours it by its outcome (the caller's call), else
    it takes the height colour like the measured line.

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

    reset_col = None
    if est.resets_at is not None and x0 <= est.resets_at <= x1:
        reset_col = dx(est.resets_at) // 2

    now_col = dx(now) // 2 if x0 <= now <= x1 else None
    lines = _grid_rows(
        canvas, threshold=threshold, reset_col=reset_col, palette=palette, now_col=now_col,
        projection_color=projection_color,
    )
    left_fmt, right_fmt = _clock_fmts(window_len, x1 - x0)
    lines.append(_time_axis(cols, x0, x1, now, left_fmt, palette, right_fmt=right_fmt))
    return lines


def _clock_fmts(window_len_s: float, span_s: float) -> tuple[str, str]:
    """(left, right) axis label formats for one usage window: clocks for 5h,
    weekday and clock for a short look into a weekly window; across days,
    dates ("Sat 10" … "Sat 17 19:00"), since a weekly window starts and
    resets on the same weekday and clock. The right edge is the reset."""
    if window_len_s <= 86400:
        return "%H:%M", "%H:%M"
    if span_s <= 86400:
        return "%a %H:%M", "%a %H:%M"
    return "%a %d", "%a %d %H:%M"


def _span_fmt(span_s: float) -> str:
    """Axis label format for a multi-window span: dates once it passes a day."""
    return "%H:%M" if span_s <= 86400 else "%b %d"


def _y_label(r: int, rows: int, thr_row: int | None, threshold: float | None) -> str:
    if r == 0:
        return "100"
    if r == rows - 1:
        return "0"
    if r == thr_row and threshold is not None:
        return f"{threshold:.0f}"
    return ""


def _threshold_row(threshold: float | None, dot_h: int) -> int | None:
    if threshold is None:
        return None
    dot = round(_clamp(threshold, 0.0, 100.0) / 100.0 * (dot_h - 1))
    return (dot_h - 1 - dot) // 4


def _grid_rows(
    canvas: "_Canvas",
    *,
    threshold: float | None,
    reset_col: int | None,
    palette: Palette,
    now_col: int | None = None,
    projection_color: str | None = None,
) -> list[Text]:
    """The plot body: y-axis gutter, braille cells, threshold row, reset column."""
    rows, cols = canvas.rows, canvas.cols
    thr_row = _threshold_row(threshold, canvas.dot_h)
    lines: list[Text] = []
    for r in range(rows):
        line = Text(no_wrap=True)
        label = _y_label(r, rows, thr_row, threshold)
        line.append(f"{label:>{GUTTER - 1}}{'┤' if label else '│'}", style=palette.muted)
        # Color by height: a cell's severity is its row's midpoint pct, so the
        # curve warms as it climbs instead of taking one flat color.
        row_pct = 100.0 * (rows - r - 0.5) / rows
        color = palette.severity(row_pct)
        for c in range(cols):
            solid, dotted = canvas.solid[r][c], canvas.dotted[r][c]
            if solid:
                line.append(chr(_BRAILLE_BASE | solid | dotted), style=color)
            elif dotted:
                line.append(PROJECTION_GLYPH, style=projection_color or color)
            elif r == thr_row:
                line.append(THRESHOLD_GLYPH, style=f"{palette.sev_warn} dim")
            elif c == reset_col:
                line.append(RESET_GLYPH, style=palette.muted)
            elif c == now_col:
                line.append(NOW_GLYPH, style=f"{palette.muted} dim")
            else:
                line.append(" ")
        lines.append(line)
    return lines


def _time_axis(
    cols: int, x0: float, x1: float, now: float | None, fmt: str, palette: Palette,
    *, right_fmt: str | None = None,
) -> Text:
    axis = [" "] * cols
    _place(axis, 0, datetime.fromtimestamp(x0).strftime(fmt))
    right = datetime.fromtimestamp(x1).strftime(right_fmt or fmt)
    _place(axis, cols - len(right), right)
    if now is not None and x0 <= now <= x1:
        dot = round((now - x0) / (x1 - x0) * (cols * 2 - 1))
        _place(axis, dot // 2 - 1, "now")
    return Text(" " * GUTTER + "".join(axis), style=palette.muted, no_wrap=True)


def history_plot(
    points: Sequence[Point],
    *,
    x0: float,
    x1: float,
    threshold: float | None,
    width: int,
    height: int,
    palette: Palette = Palette.DARK,
) -> list[Text]:
    """Measured series over an arbitrary span (days to weeks), no projection.

    Used by the long-term view: the curve runs straight through window resets,
    so each drop reads as the window rolling over. ``height`` lines including
    the time axis; a sparkline when the area is too small.
    """
    pts = sorted((p for p in points if x0 <= p.t <= x1), key=lambda p: p.t)
    if height < MIN_PLOT_HEIGHT or width < MIN_PLOT_WIDTH:
        return [sparkline(pts, max(0, width), palette=palette, span=(x0, x1))]
    if x1 <= x0:
        x1 = x0 + 1.0
    cols, rows = width - GUTTER, height - 1
    canvas = _Canvas(cols, rows)

    def dx(t: float) -> int:
        return round(_clamp((t - x0) / (x1 - x0), 0.0, 1.0) * (canvas.dot_w - 1))

    def dy(pct: float) -> int:
        return round(_clamp(pct, 0.0, 100.0) / 100.0 * (canvas.dot_h - 1))

    for a, b in zip(pts, pts[1:]):
        canvas.line(dx(a.t), dy(a.pct), dx(b.t), dy(b.pct))
    if len(pts) == 1:
        canvas.set(dx(pts[0].t), dy(pts[0].pct))
    lines = _grid_rows(canvas, threshold=threshold, reset_col=None, palette=palette)
    lines.append(_time_axis(cols, x0, x1, None, _span_fmt(x1 - x0), palette))
    return lines


COLUMN_GLYPHS = " ▁▂▃▄▅▆▇█"


# A bar needs one column plus one blank column before its right neighbour.
_MIN_COLS_PER_BAR = 2


def windows_per_bar(*, window_len_s: float, span_s: float, cols: int) -> int:
    """How many consecutive windows one peak bar stands for, so that every
    bar keeps a blank column from the next one in ``cols`` plot columns."""
    if cols <= 0 or span_s <= 0 or window_len_s <= 0:
        return 1
    per_window = cols * window_len_s / span_s
    return max(1, math.ceil(_MIN_COLS_PER_BAR / per_window - 1e-9))


def _peak_bars(
    visible: Sequence[tuple[float, float]], *, window_len_s: float, x0: float, x1: float, cols: int
) -> list[tuple[int, int, float]]:
    """``(first_col, end_col, peak)`` bars, ``end_col`` exclusive, each leaving
    at least one blank column before the next bar.

    When a window gets two or more columns, each window is its own bar placed
    at its real time, so idle stretches show as gaps. When windows are
    narrower, they are grouped into buckets of ``N`` window lengths counted
    back from ``x1`` and each bucket shows its highest peak."""

    def col(t: float) -> int:
        return math.floor(_clamp((t - x0) / (x1 - x0), 0.0, 1.0) * cols)

    def bar(start: float, end: float, peak: float) -> tuple[int, int, float] | None:
        c0 = col(start)
        # The running window reaches the right edge; everything else gives up
        # its last column as the gap before the next bar.
        c1 = cols if end >= x1 else col(end) - 1
        return (c0, c1, peak) if c1 > c0 else None

    n = windows_per_bar(window_len_s=window_len_s, span_s=x1 - x0, cols=cols)
    if n == 1:
        bars = (bar(r - window_len_s, r, p) for r, p in visible)
        return [b for b in bars if b is not None]
    bucket_s = n * window_len_s
    # Bucket ``edge`` holds ``x0``. Cut off too narrow for a bar, it joins
    # its right neighbour, which then stretches to the left edge.
    edge = math.floor((x1 - x0) / bucket_s)
    merge_edge = edge > 0 and col(x1 - edge * bucket_s) < _MIN_COLS_PER_BAR
    buckets: dict[int, float] = {}
    for resets_at, peak in visible:
        k = min(edge, max(0, math.floor((x1 - resets_at) / bucket_s)))
        if merge_edge and k == edge:
            k = edge - 1
        buckets[k] = max(peak, buckets.get(k, 0.0))
    out = []
    for k, peak in sorted(buckets.items(), reverse=True):
        start = x0 if merge_edge and k == edge - 1 else max(x0, x1 - (k + 1) * bucket_s)
        found = bar(start, x1 - k * bucket_s, peak)
        if found is not None:
            out.append(found)
    return out


def peak_columns(
    peaks: Sequence[tuple[float, float]],
    *,
    window_len_s: float,
    x0: float,
    x1: float,
    threshold: float | None,
    width: int,
    height: int,
    palette: Palette = Palette.DARK,
) -> list[Text]:
    """One bar per past window, as tall as that window's peak pct.

    ``peaks`` is ``(resets_at, peak_pct)`` pairs; a window covers
    ``[resets_at - window_len_s, resets_at]`` on the x axis, so gaps between
    bars are real idle time, and adjacent bars always keep a blank column.
    When the plot is too narrow for that, one bar stands for several
    consecutive windows (see :func:`windows_per_bar`) and shows their highest
    peak. Bars use eighth blocks (8 steps per row) and take the severity
    color of their peak. ``height`` lines including the time axis; a
    sparkline of the peaks when the area is too small.
    """
    if x1 <= x0:
        x1 = x0 + 1.0
    visible = sorted((r, p) for r, p in peaks if r > x0 and r - window_len_s < x1)
    if height < MIN_PLOT_HEIGHT or width < MIN_PLOT_WIDTH:
        pts = [Point(r, p, r) for r, p in visible]
        return [sparkline(pts, max(0, width), palette=palette, span=(x0, x1))]
    cols, rows = width - GUTTER, height - 1
    heights = [0] * cols  # eighths, per column
    colors: list[str | None] = [None] * cols

    for c0, c1, peak in _peak_bars(visible, window_len_s=window_len_s, x0=x0, x1=x1, cols=cols):
        eighths = round(_clamp(peak, 0.0, 100.0) / 100.0 * rows * 8)
        # Windows that stayed under the threshold recede; the ones that
        # crossed it (the ones that forced a switch) keep full color.
        color = palette.severity(peak)
        if threshold is None or peak < threshold:
            color = f"{color} dim"
        for c in range(c0, c1):
            if eighths >= heights[c]:
                heights[c] = eighths
                colors[c] = color

    thr_row = None
    if threshold is not None:
        thr_eighths = _clamp(threshold, 0.0, 100.0) / 100.0 * rows * 8
        thr_row = rows - 1 - min(rows - 1, int(thr_eighths // 8))
    lines: list[Text] = []
    for r in range(rows):
        line = Text(no_wrap=True)
        label = _y_label(r, rows, thr_row, threshold)
        line.append(f"{label:>{GUTTER - 1}}{'┤' if label else '│'}", style=palette.muted)
        floor = (rows - 1 - r) * 8  # eighths below this row
        for c in range(cols):
            fill = heights[c] - floor
            if fill > 0:
                line.append(COLUMN_GLYPHS[min(8, fill)], style=colors[c] or palette.muted)
            elif r == thr_row:
                line.append(THRESHOLD_GLYPH, style=f"{palette.sev_warn} dim")
            else:
                line.append(" ")
        lines.append(line)
    lines.append(_time_axis(cols, x0, x1, None, _span_fmt(x1 - x0), palette))
    return lines


__all__ = [
    "PROJECTION_GLYPH", "history_plot", "line_plot", "peak_columns", "sparkline", "windows_per_bar",
]
