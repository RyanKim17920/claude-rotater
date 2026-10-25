"""The costs view: Claude Code spend in place of the account body (``$``).

Header and prompt line stay; esc (or ``$``) goes back. What it shows, top
to bottom, dropping the lower parts first when rows run out:

- one summary line: today / 7d / 30d and the week's trend;
- one chart, cycled with ←/→ (:data:`VIEWS`): the whole history, the last
  30 days, the last week, the last day (bars stacked by model, dashed
  lines at round dollars, the top one at or above the tallest bar, who was
  on duty under the week and the day), then each account's $ per 1% of the
  5h and of the 7d window over time, one series and trendline per account;
- per account: cost and $ per 1% of the 5h and 7d windows;
- per model: cost, share and trend (tokens too when wide);
- a note on what is estimated.

Data comes from :class:`claude_rotater.cost_report.CostSource`, which runs
ccusage only when asked: the cached report shows at once, then a refresh
reprices what changed since (``r`` forces today's slots again).
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Sequence

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget

from claude_rotater.cost_report import CostReport, CostResult, ModelLine
from claude_rotater.dollar_per_pct import PerPct
from claude_rotater.tui.theme import Palette

HINT_KEYS: tuple[tuple[str, str], ...] = (
    ("←/→", "chart"),
    ("r", "refresh"),
    ("y / Y", "copy"),
    ("esc", "back"),
)

VIEWS: tuple[str, ...] = ("total", "monthly", "weekly", "hourly", "$/1% 5h", "$/1% 7d")
TREND_VIEWS = {"$/1% 5h": "5h", "$/1% 7d": "7d"}

LOADING = "reading transcripts… (the first time takes a while)"
NOTE = "~ estimate, ? unsure · $/1% = spend per 1% of a limit, falling = limit shrinking · priced by ccusage"
MODEL_ROWS = 5  # the costliest four, then one "other" row
COLORED_MODELS = 3
CHART_MAX_H = 10
CHART_MIN_H = 3
CHART_ROOMY_H = 6
MAX_GRID_LINES = 4
SHRINK_ALARM = -0.20  # a fall this steep per 4 weeks is red, a smaller one amber
DAY_S = 86400.0
_BLOCKS = "▁▂▃▄▅▆▇"
_GRID = "╌"
_FLAT = "┄"  # a steady trendline
_SLOPE = "·"  # a moving one


# -- formatting ------------------------------------------------------------------


def money(usd: float) -> str:
    if usd < 10:
        return f"${usd:.2f}"
    if usd < 100:
        return f"${usd:.1f}"
    if usd < 100_000:
        return f"${usd:,.0f}"
    return f"${usd / 1000:,.0f}k"


def short_money(usd: float) -> str:
    """A round gridline value: ``$0.5``, ``$20``, ``$500``, ``$1k``, ``$1.5k``."""
    if usd >= 1000:
        return f"${usd / 1000:g}k"
    return f"${usd:g}"


def nice_step(span: float, max_lines: int = MAX_GRID_LINES) -> float:
    """The smallest 1-2-5 step whose multiples reach ``span`` in at most
    ``max_lines`` lines (7 -> 2, 140 -> 50, 3400 -> 1000)."""
    exp = math.floor(math.log10(span)) - 1
    while True:
        for m in (1, 2, 5):
            step = m * 10.0 ** exp
            if math.ceil(span / step - 1e-9) <= max_lines:
                return step
        exp += 1


def nice_top(peak: float, max_lines: int = MAX_GRID_LINES) -> tuple[float, float]:
    """``(step, top)``: the gridline step and the round value at or above
    ``peak`` the chart tops out at (2600 -> (1000, 3000))."""
    step = nice_step(peak, max_lines)
    return step, math.ceil(peak / step - 1e-9) * step


def tokens(n: int) -> str:
    for scale, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= scale:
            value = n / scale
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    return str(n)


def arrow(change: float | None, *, flat: float = 0.10) -> str:
    if change is None:
        return "new"
    if change > flat:
        return "↑"
    if change < -flat:
        return "↓"
    return "→"


def trend_words(change: float | None) -> str:
    """``↑ 18%``; ``→`` within ±10%; empty without a week to compare."""
    if change is None:
        return ""
    a = arrow(change)
    if a == "→":
        return a
    return f"{a} {change + 1:.0f}x" if change >= 9 else f"{a} {abs(change) * 100:.0f}%"


def per_pct_value(p: PerPct | None) -> str:
    """``~$5.30``; ``~$5.30?`` when unsure; ``–`` without a figure."""
    if p is None:
        return "–"
    return f"~{money(p.dollars)}{'' if p.sure else '?'}"


def verdict(p: PerPct | None, palette: Palette) -> tuple[str, str]:
    """``(words, style)`` for one account's $ per 1% trend: ``steady``,
    ``↓12%/4w limit shrinking`` (amber, red past :data:`SHRINK_ALARM`) or
    ``↑8%/4w more room``; a provisional fit says so and is dimmed."""
    f = p.fit if p is not None else None
    if f is None:
        return ("one window so far" if p is not None and p.points else "no windows yet",
                palette.muted)
    if f.steady:
        words, color = "steady", palette.foreground
    elif f.change < 0:
        words = f"↓{abs(f.change) * 100:.0f}%/4w limit shrinking"
        color = palette.sev_crit if f.change <= SHRINK_ALARM else palette.sev_warn
    else:
        words, color = f"↑{f.change * 100:.0f}%/4w more room", palette.sev_ok
    if not f.provisional:
        return words, color
    days = f.span_s / DAY_S
    span = "under a day" if days < 1 else f"{days:.0f} day{'s' if round(days) != 1 else ''}"
    return f"~ {words}, provisional: {span} of data", f"dim {color}"


def ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def model_colors(report: CostReport, palette: Palette) -> dict[str, str]:
    """The costliest models get the accent, green and saffron; the rest muted."""
    hues = (palette.accent, palette.sev_ok, palette.sev_warn)
    return {m.name: hues[i] if i < COLORED_MODELS else palette.muted
            for i, m in enumerate(report.models)}


def account_colors(report: CostReport, palette: Palette) -> dict[str, str]:
    """One hue per account for the trend series, in table order."""
    hues = (palette.accent, palette.sev_ok, palette.foreground, palette.muted)
    return {a.number: hues[i % len(hues)] for i, a in enumerate(report.accounts)}


# -- bucketing -------------------------------------------------------------------


Bucket = tuple[float, dict[str, float]]  # (start, {model: usd})


def group(series: Sequence[Bucket], size: int) -> list[Bucket]:
    """Consecutive runs of ``size`` buckets summed, aligned so the last run
    ends with the series (a short first run is dropped when there are more)."""
    if size <= 1:
        return list(series)
    skip = len(series) % size if len(series) > size else 0
    out: list[Bucket] = []
    for i in range(skip, len(series), size):
        chunk = series[i:i + size]
        merged: dict[str, float] = {}
        for _, models in chunk:
            for name, usd in models.items():
                merged[name] = merged.get(name, 0.0) + usd
        out.append((chunk[0][0], merged))
    return out


def group_duty(duty: Sequence[str | None], size: int) -> list[str | None]:
    """Who was on duty most of each run :func:`group` makes."""
    if size <= 1:
        return list(duty)
    skip = len(duty) % size if len(duty) > size else 0
    out: list[str | None] = []
    for i in range(skip, len(duty), size):
        seen = Counter(d for d in duty[i:i + size] if d is not None)
        out.append(seen.most_common(1)[0][0] if seen else None)
    return out


def fill_days(history: Sequence[Bucket]) -> list[Bucket]:
    """Every local day from the first known one to the last, empty days in
    (days are local midnights, so a DST day is 23 or 25 hours)."""
    if not history:
        return []
    known = dict(history)
    out: list[Bucket] = []
    midnight, last = _midnight(history[0][0]), history[-1][0]
    while midnight <= last:
        out.append((midnight, known.get(midnight, {})))
        midnight = _midnight(midnight + DAY_S + 3 * 3600)  # past any DST shift, back to midnight
    return out


def _midnight(t: float) -> float:
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


# -- the parts -------------------------------------------------------------------


def summary_text(report: CostReport, palette: Palette, *, status: str = "") -> Text:
    """``today $35 · 7d $1,392 · 30d $5,757   ↑ 18% vs last week``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, (label, usd) in enumerate((("today", report.today), ("7d", report.week),
                                      ("30d", report.month))):
        if i:
            text.append(" · ", style=palette.muted)
        text.append(f"{label} ", style=palette.muted)
        text.append(money(usd), style=f"bold {palette.foreground}")
    if words := trend_words(report.week_change):
        text.append(f"   {words} vs last week", style=palette.foreground)
    if status:
        text.append(f"   {status}", style=palette.muted)
    return text


def _stack(column: dict[str, float], order: Sequence[str]) -> list[tuple[str, float]]:
    """A bar's spend bottom-up: the costliest models first, the rest as one."""
    shown = [(name, column.get(name, 0.0)) for name in order]
    rest = sum(v for k, v in column.items() if k not in order)
    return shown + ([("", rest)] if rest > 0 else [])


def grid_rows(peak: float, height: int) -> tuple[float, dict[int, str]]:
    """``(top, {row: label})``: the round value the chart tops out at (at or
    above ``peak``, labelled on the top row) and the gridlines, row 0 at the
    bottom, each at the row its value reaches, a shared row keeping the lower."""
    if peak <= 0 or height <= 0:
        return 0.0, {}
    step, top = nice_top(peak, min(MAX_GRID_LINES, max(1, height // 2)))
    rows: dict[int, str] = {}
    k = 1
    while k * step <= top * (1 + 1e-9):
        row = min(height - 1, max(0, math.ceil(k * step / top * height - 1e-9) - 1))
        rows.setdefault(row, short_money(k * step))
        k += 1
    rows[height - 1] = short_money(top)  # the top is always labelled
    return top, rows


def bar_chart(
    columns: Sequence[dict[str, float]],
    width: int,
    height: int,
    order: Sequence[str],
    colors: dict[str, str],
    palette: Palette,
    *,
    axis: tuple[str, str, str],
    under: Sequence[str | None] | None = None,
) -> list[Text]:
    """Stacked bars, ``height`` rows, then the optional ``under`` row (who
    was on duty, shown where it changes) and an axis of (left, middle,
    right) labels. A bar is cut in eighths at its top and colored by the
    model holding each cell; dashed rows mark round dollar values, the top
    row the round value at or above the tallest bar."""
    totals = [sum(c.values()) for c in columns]
    top, grid = grid_rows(max(totals, default=0.0), height)
    label_w = max([2, *(len(v) for v in grid.values())]) + 1
    n = max(1, len(columns))
    col = max(1, min(4, (width - label_w) // n))
    bar = col - 1 if col >= 3 else col
    grid_style = f"dim {palette.muted}"
    lines: list[Text] = []
    for r in range(height - 1, -1, -1):
        filler = _GRID if r in grid else " "
        line = Text(no_wrap=True)
        line.append(f"{grid.get(r, ''):>{label_w - 1}} ", style=palette.muted)
        for column, total in zip(columns, totals):
            eighths = round(total / top * height * 8) if top > 0 else 0
            lo = r * 8
            if eighths <= lo:
                line.append(filler * col, style=grid_style)
                continue
            fill = min(8, eighths - lo)
            mid = lo + fill / 2
            scale = eighths / total if total else 0.0
            acc, who = 0.0, ""
            for name, usd in _stack(column, order):
                acc += usd * scale
                who = name
                if acc >= mid:
                    break
            glyph = "█" if fill == 8 else _BLOCKS[fill - 1]
            line.append(glyph * bar, style=colors.get(who, palette.muted))
            line.append(filler * (col - bar), style=grid_style)
        lines.append(line)
    if under is not None:
        lines.append(_duty_row(under, col, label_w, palette))
    lines.append(_axis(axis, label_w, col * n, palette))
    return lines


def _axis(axis: tuple[str, str, str], label_w: int, span: int, palette: Palette) -> Text:
    left, middle, right = axis
    text = Text(no_wrap=True, style=palette.muted)
    gap = span - len(left) - len(right)
    text.append(" " * label_w + left)
    if gap > len(middle) + 2:
        pad = (gap - len(middle)) // 2
        text.append(" " * pad + middle + " " * (gap - pad - len(middle)))
    else:
        text.append(" " * max(1, gap))
    text.append(right)
    return text


def _duty_row(under: Sequence[str | None], col: int, label_w: int, palette: Palette) -> Text:
    """``#3`` under the bar where account 3 came on duty, when it fits
    clear of the mark before it."""
    cells = [" "] * (col * len(under))
    free, prev, pending = 0, None, None
    for i, who in enumerate(under):
        if who != prev:
            pending = who  # marked at the first column with room, if still on duty
        mark = f"#{who}"
        if who is not None and who == pending and free <= i * col <= len(cells) - len(mark):
            cells[i * col:i * col + len(mark)] = mark
            free, pending = i * col + len(mark) + 1, None
        prev = who
    row = Text(no_wrap=True, style=palette.muted)
    row.append(f"{'on':>{label_w - 1}} " + "".join(cells))
    return row


def _fits(n: int, width: int, label_w: int = 6) -> bool:
    """Whether ``n`` bars get two columns each (a bar and a gap)."""
    return 2 * n <= width - label_w


def money_series(report: CostReport, view: str, width: int):
    """``(buckets, duty or None, axis)`` of a money view at ``width``."""
    def stamp(fmt: str, t: float) -> str:
        return time.strftime(fmt, time.localtime(t))

    if view == "total":
        days = fill_days(report.history)
        for size, unit in ((1, "daily"), (7, "weekly"), (30, "monthly")):
            buckets = group(days, size)
            if _fits(len(buckets), width) or size == 30:
                break
        first = stamp("%m-%d", buckets[0][0]) if buckets else ""
        return buckets, None, (first, f"all, {unit}", "today")
    if view == "monthly":
        days = list(report.days)
        return days, None, (stamp("%m-%d", days[0][0]) if days else "", "30 days", "today")
    hours, duty = list(report.hours), list(report.hour_duty)
    if view == "weekly":
        size = 3 if _fits(len(hours) // 3, width) else 6
        buckets = group(hours, size)
        first = stamp("%a %H:00", buckets[0][0]) if buckets else ""
        return buckets, group_duty(duty, size), (first, f"7 days, {size}h bars", "now")
    n = 24 if _fits(24, width) else 12
    buckets = hours[-n:]
    return buckets, duty[-n:], (stamp("%H:00", buckets[0][0]) if buckets else "",
                                f"{n} hours", "now")


def money_chart(report: CostReport, view: str, width: int, height: int,
                palette: Palette) -> list[Text]:
    buckets, duty, axis = money_series(report, view, width)
    under = duty if duty is not None and any(d is not None for d in duty) else None
    return bar_chart([b for _, b in buckets], width, height,
                     [m.name for m in report.models[:COLORED_MODELS]],
                     model_colors(report, palette), palette, axis=axis, under=under)


def trend_accounts(report: CostReport, label: str):
    """``[(account line, its figure)]`` with something to plot for ``label``."""
    out = []
    for acc in report.accounts:
        p = acc.per_5h if label == "5h" else acc.per_7d
        if p is not None and p.points:
            out.append((acc, p))
    return out


def trend_chart(report: CostReport, label: str, width: int, height: int,
                names: dict[str, str], palette: Palette) -> list[Text]:
    """Each account's $ per 1% of ``label`` per finished window (● sure,
    ○ partly used elsewhere) with its trendline, flat when steady, then one
    legend line per account with its figure and verdict."""
    shown = trend_accounts(report, label)
    if not shown:
        return [Text(f"no finished {label} window with ≥10% used and priced locally yet",
                     style=palette.muted, no_wrap=True, overflow="ellipsis")]
    colors = account_colors(report, palette)
    t_hi = report.now
    t_lo = min(t for _, p in shown for t, _, _ in p.points)
    if t_hi - t_lo < DAY_S:
        t_lo = t_hi - DAY_S
    values = [v for _, p in shown for _, v, _ in p.points]
    values += [p.fit.at(t) for _, p in shown if p.fit for t in (t_lo, t_hi)]
    lo_v, hi_v = min(values), max(values)
    step = nice_step(max(hi_v - lo_v, hi_v * 0.2), min(MAX_GRID_LINES, max(1, height // 2)))
    y_lo = math.floor(lo_v / step + 1e-9) * step
    y_hi = max(y_lo + step, math.ceil(hi_v / step - 1e-9) * step)
    labels = {}
    k = 0
    while y_lo + k * step <= y_hi + 1e-9:
        v = y_lo + k * step
        labels[round((v - y_lo) / (y_hi - y_lo) * (height - 1))] = short_money(round(v, 6))
        k += 1
    label_w = max(len(v) for v in labels.values()) + 1
    plot_w = max(4, width - label_w)
    grid_style = f"dim {palette.muted}"
    cells = [[(_GRID if r in labels else " ", grid_style) for _ in range(plot_w)]
             for r in range(height)]

    def row_of(v: float) -> int:
        return max(0, min(height - 1, round((v - y_lo) / (y_hi - y_lo) * (height - 1))))

    def col_of(t: float) -> int:
        return max(0, min(plot_w - 1, round((t - t_lo) / (t_hi - t_lo) * (plot_w - 1))))

    for acc, p in shown:
        if p.fit is None:
            continue
        color = colors[acc.number]
        style = f"dim {color}" if p.fit.provisional else color
        start = col_of(min(t for t, _, _ in p.points))
        for c in range(start, plot_w):
            t = t_lo + c / (plot_w - 1) * (t_hi - t_lo)
            cells[row_of(p.fit.at(t))][c] = (_FLAT if p.fit.steady else _SLOPE, style)
    for acc, p in shown:
        color = colors[acc.number]
        for t, v, sure in p.points:
            cells[row_of(v)][col_of(t)] = ("●", color) if sure else ("○", f"dim {color}")
    lines: list[Text] = []
    for r in range(height - 1, -1, -1):
        line = Text(no_wrap=True)
        line.append(f"{labels.get(r, ''):>{label_w - 1}} ", style=palette.muted)
        for glyph, style in cells[r]:
            line.append(glyph, style=style)
        lines.append(line)
    first = time.strftime("%m-%d", time.localtime(t_lo))
    lines.append(_axis((first, f"$ per 1% of {label}, per window", "now"), label_w, plot_w,
                       palette))
    name_w = max(len(names.get(a.number, f"#{a.number}")) for a, _ in shown)
    for acc, p in shown:
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("● ", style=colors[acc.number])
        line.append(f"{names.get(acc.number, f'#{acc.number}'):<{name_w}}  ",
                    style=palette.foreground)
        value = per_pct_value(p)
        line.append(f"{value:<8} ", style=palette.foreground if p.sure else palette.sev_warn)
        words, style = verdict(p, palette)
        line.append(words, style=style)
        line.truncate(width, overflow="ellipsis")
        lines.append(line)
    return lines


def chart_title(report: CostReport, view: str, palette: Palette) -> Text:
    """``‹ weekly ›  ←/→``, plus the whole-history total on the total view."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("‹ ", style=palette.muted)
    text.append(view, style=f"bold {palette.accent}")
    text.append(" ›", style=palette.muted)
    text.append("  ←/→", style=palette.muted)
    if view == "total" and report.history:
        total = sum(sum(m.values()) for _, m in report.history)
        since = time.strftime("%m-%d", time.localtime(report.history[0][0]))
        text.append("   ")
        text.append(money(total), style=f"bold {palette.foreground}")
        text.append(f" since {since}", style=palette.muted)
    elif view in TREND_VIEWS:
        text.append("   ● sure  ○ partly used elsewhere  ", style=palette.muted)
        text.append(f"{_FLAT} steady  {_SLOPE} moving", style=palette.muted)
    return text


def chart_lines(report: CostReport, view: str, width: int, height: int,
                names: dict[str, str], palette: Palette) -> list[Text]:
    """The chosen view's title and chart, bars ``height`` rows high."""
    title = chart_title(report, view, palette)
    title.truncate(width, overflow="ellipsis")
    if view in TREND_VIEWS:
        return [title, *trend_chart(report, TREND_VIEWS[view], width, height, names, palette)]
    return [title, *money_chart(report, view, width, height, palette)]


def chart_rows(report: CostReport, view: str, width: int, height: int) -> int:
    """How many rows :func:`chart_lines` takes for a chart ``height`` high."""
    if view in TREND_VIEWS:
        shown = trend_accounts(report, TREND_VIEWS[view])
        return 1 + (height + 1 + len(shown) if shown else 1)
    _, duty, _ = money_series(report, view, width)
    under = int(duty is not None and any(d is not None for d in duty))
    return 1 + height + under + 1


def _row(cells: Sequence[tuple[str, int, str]], *, gap: int = 2) -> Text:
    """Cells of (text, width, style); a negative width left-aligns."""
    line = Text(no_wrap=True, overflow="ellipsis")
    for i, (value, w, style) in enumerate(cells):
        if i:
            line.append(" " * gap)
        cell = value[:abs(w)]
        line.append(f"{cell:<{-w}}" if w < 0 else f"{cell:>{w}}", style=style)
    return line


def _fold(models: Sequence[ModelLine], rows: int) -> list[ModelLine]:
    if len(models) <= rows:
        return list(models)
    keep, rest = list(models[: rows - 1]), models[rows - 1:]
    other = ModelLine(
        f"{len(rest)} other", sum(m.cost for m in rest), sum(m.share for m in rest),
        sum(m.input for m in rest), sum(m.output for m in rest),
        sum(m.cache_write for m in rest), sum(m.cache_read for m in rest), None,
    )
    return keep + [other]


def model_lines(report: CostReport, width: int, rows: int, palette: Palette) -> list[Text]:
    colors = model_colors(report, palette)
    models = _fold(report.models, rows)
    name_w = max([len("model")] + [len(m.name) for m in models])
    wide = width >= name_w + 50
    head: list[tuple[str, int, str]] = [("model", -(name_w + 2), palette.muted),
                                        ("cost", 7, palette.muted), ("share", 5, palette.muted)]
    if wide:
        head += [("output", 6, palette.muted), ("cache r", 7, palette.muted)]
    head.append(("7d", -6, palette.muted))
    out = [_row(head)]
    for m in models:
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("■ ", style=colors.get(m.name, palette.muted))
        line.append(f"{m.name:<{name_w}}", style=palette.foreground)
        cells: list[tuple[str, int, str]] = [
            (money(m.cost), 7, f"bold {palette.foreground}"),
            (f"{m.share * 100:.0f}%", 5, palette.foreground),
        ]
        if wide:
            cells += [(tokens(m.output), 6, palette.muted), (tokens(m.cache_read), 7, palette.muted)]
        trend = "" if m.name.endswith(" other") else (trend_words(m.trend) or arrow(m.trend))
        cells.append((trend, -6, palette.foreground))
        line.append("  ")
        line.append(_row(cells))
        out.append(line)
    return out


def account_lines(
    report: CostReport, width: int, rows: int, names: dict[str, str], palette: Palette
) -> list[Text]:
    """Cost and $ per 1% per account; how the $ per 1% moves is the trend
    views' job, so only the figure shows here (saffron and ``?`` when unsure)."""
    shown = report.accounts[:rows]
    labels = [names.get(a.number, f"#{a.number}") for a in shown]
    want = max([len("account"), *(len(s) for s in labels)])
    name_w = max(8, min(want, width - 7 - 2 * (9 + 2) - 2))
    out = [_row([("account", -name_w, palette.muted), ("30d", 7, palette.muted),
                 ("$/1% 5h", -9, palette.muted), ("$/1% 7d", -9, palette.muted)])]
    for acc, label in zip(shown, labels):
        cells = [(label, -name_w, palette.foreground),
                 (money(acc.cost), 7, f"bold {palette.foreground}")]
        for p in (acc.per_5h, acc.per_7d):
            style = palette.foreground if p is None or p.sure else palette.sev_warn
            cells.append((per_pct_value(p), -9, style))
        line = _row(cells)
        line.rstrip()
        out.append(line)
    return out


def note_text(report: CostReport, palette: Palette, width: int) -> Text:
    note = NOTE
    if report.unattributed >= 1:
        note += f" · {money(report.unattributed)} unattributed"
    text = Text(note, style=palette.muted, no_wrap=True, overflow="ellipsis")
    text.truncate(width, overflow="ellipsis")
    return text


# -- the page ----------------------------------------------------------------------


def costs_lines(
    report: CostReport | None,
    *,
    width: int,
    height: int,
    names: dict[str, str],
    palette: Palette,
    view: str = VIEWS[0],
    status: str = "",
    empty: str = LOADING,
) -> list[Text]:
    """The page at ``width`` x ``height`` with the ``view`` chart, the
    richest layout that fits.

    Lower-priority parts give way first: the note, the gaps, model rows
    (down to three), chart rows (down to three), then the chart and the
    model table. The summary and the account table stay. A trend view
    names every account in its legend, so it leaves the tables out.
    """
    if report is None:
        return [Text(status or empty, style=palette.muted)]
    trend = view in TREND_VIEWS
    n_models = 0 if trend else min(len(report.models), MODEL_ROWS)
    n_accounts = 0 if trend else len(report.accounts)
    summary = summary_text(report, palette, status=status)
    if summary.cell_len > width:  # the freshness note is the first to go
        summary = summary_text(report, palette)

    def fits(note: bool, gaps: bool, model_rows: int, chart: bool, min_h: int) -> int | None:
        """Bar height that fits these choices (0: no chart), None if none does."""
        used = 1 + (1 + n_accounts if n_accounts else 0) + (1 + model_rows if model_rows else 0)
        blocks = 1 + bool(n_accounts) + bool(model_rows) + chart + note
        used += note + (blocks - 1 if gaps else 0)
        if not chart:
            return 0 if used <= height else None
        for h in range(CHART_MAX_H, min_h - 1, -1):
            if used + chart_rows(report, view, width, h) <= height:
                return h
        return None

    plans = [
        (note, gaps, rows, chart, min_h)
        for chart, min_h in ((True, CHART_ROOMY_H), (True, CHART_MIN_H), (False, 0))
        for rows in (n_models, min(n_models, 3), 0)
        for gaps in (True, False)
        for note in (True, False)
    ]
    plan, chart_h = (False, False, 0, False, 0), 0
    for candidate in plans:
        if candidate[2] == 0 and n_models and candidate[3]:
            continue  # bars without their legend (the model table) are no use
        h = fits(*candidate)
        if h is not None:
            plan, chart_h = candidate, h
            break
    note, gaps, model_rows, chart, _ = plan
    blocks: list[list[Text]] = [[summary]]
    if chart:
        blocks.append(chart_lines(report, view, width, chart_h, names, palette))
    if n_accounts:
        blocks.append(account_lines(report, width, n_accounts, names, palette))
    if model_rows:
        blocks.append(model_lines(report, width, model_rows, palette))
    if note:
        blocks.append([note_text(report, palette, width)])
    out: list[Text] = []
    for block in blocks:
        if out and gaps:
            out.append(Text())
        out.extend(block)
    return out[:max(1, height)]


def plain_page(report: CostReport, names: dict[str, str], *, width: int = 100) -> str:
    """The whole page as text for ``Y``: the money views' parts once, with
    the 30-day chart, then both trend charts."""
    parts = [costs_lines(report, width=width, height=200, names=names, palette=Palette.DARK,
                         view="monthly")]
    for view in TREND_VIEWS:
        parts.append(chart_lines(report, view, width, 8, names, Palette.DARK))
    return "\n\n".join("\n".join(line.plain.rstrip() for line in lines) for lines in parts)


# -- the widget ----------------------------------------------------------------------


class CostsView(Widget, can_focus=True):
    """The costs page; posts :class:`CostsView.Closed` on esc or ``$``."""

    class Closed(Message):
        """The user left the costs: the host shows its body again."""

    BINDINGS = [
        Binding("escape,dollar_sign", "close", show=False),
        Binding("r", "refresh", show=False),
        Binding("right", "cycle(1)", show=False),
        Binding("left", "cycle(-1)", show=False),
        Binding("y", "copy_summary", show=False),
        Binding("Y", "copy_page", show=False),
    ]

    DEFAULT_CSS = """
    CostsView {
        height: 1fr;
        padding: 0 1;
        margin-top: 1;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.result: CostResult | None = None
        self.busy = False
        self.view = VIEWS[0]  # kept while the app runs, so a reopen shows the same chart

    # -- data ------------------------------------------------------------------------

    def _accounts(self):
        snap = getattr(self.app, "snapshot", None)
        return snap.accounts if snap is not None else ()

    def identities(self) -> dict[str, tuple[str, str]]:
        return {a.number: (a.email, a.org_uuid) for a in self._accounts()}

    def names(self) -> dict[str, str]:
        from claude_rotater.tui.main import account_name

        return {a.number: account_name(a) for a in self._accounts()}

    def open(self) -> None:
        """Take the keyboard; show the cached report, then refresh it."""
        self.focus()
        self._load(force=False)

    def _load(self, *, force: bool) -> None:
        if self.busy:
            return
        self.busy = True
        self.refresh()
        self.run_worker(lambda: self._work(force), thread=True, group="costs",
                        exit_on_error=False, name="costs")

    def _work(self, force: bool) -> None:
        source = self.app.cost_source
        ids = self.identities()
        try:
            if self.result is None:
                cached = source.cached(ids)
                if cached.report is not None:
                    self.app.call_from_thread(self._show, cached, True)
            result = source.refresh(ids, force=force)
        except Exception as e:  # never take the TUI down over a report
            result = CostResult(self.result.report if self.result else None, f"costs failed: {e}")
        self.app.call_from_thread(self._show, result, False)

    def _show(self, result: CostResult, busy: bool) -> None:
        self.result = result
        self.busy = busy
        if result.note and not busy and result.report is not None:
            self.app.say(result.note, "warn")
        self.refresh()

    # -- painting ------------------------------------------------------------------

    def status(self) -> str:
        if self.busy:
            return "updating…"
        if self.result is not None and self.result.report is not None:
            return f"updated {ago(time.time() - self.result.report.built_at)}"
        return ""

    def render(self) -> Text:
        palette = Palette.from_theme(self.app.current_theme)
        width = max(20, self.size.width or 80)
        height = max(1, self.size.height or 20)
        report = self.result.report if self.result else None
        if report is None:
            note = self.result.note if self.result else None
            return Text(note or LOADING, style=palette.muted)
        lines = costs_lines(report, width=width, height=height, names=self.names(),
                            palette=palette, view=self.view, status=self.status())
        return Text("\n", no_wrap=True, overflow="ellipsis").join(lines)

    # -- actions ---------------------------------------------------------------------

    def action_close(self) -> None:
        self.post_message(self.Closed())

    def action_cycle(self, step: int) -> None:
        """The next (or previous) chart, wrapping around."""
        self.view = VIEWS[(VIEWS.index(self.view) + step) % len(VIEWS)]
        self.refresh()

    def action_refresh(self) -> None:
        if self.busy:
            self.app.say("costs are already updating", "info")
            return
        self._load(force=True)

    def action_copy_summary(self) -> None:
        report = self.result.report if self.result else None
        if report is not None:
            self.app.copy_text(summary_text(report, Palette.DARK).plain, "the cost summary")

    def action_copy_page(self) -> None:
        report = self.result.report if self.result else None
        if report is not None:
            self.app.copy_text(plain_page(report, self.names()), "the costs")
