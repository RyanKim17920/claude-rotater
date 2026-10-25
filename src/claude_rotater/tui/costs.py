"""The costs view: Claude Code spend in place of the account body (``$``).

Header and prompt line stay; esc (or ``$``) goes back. What it shows, top
to bottom, dropping the lower parts first when rows run out:

- one summary line: today / 7d / 30d and the week's trend;
- daily cost for 30 days beside hourly cost for the last 24 hours (who was
  on duty under it), bars stacked by model, dashed lines at round dollars;
- per account: cost, and $ per 1% of the 5h and 7d windows with its trend
  (a falling $ per 1% means a smaller limit);
- per model: cost, share and trend (tokens too when wide);
- a note on what is estimated.

Data comes from :class:`claude_rotater.cost_report.CostSource`, which runs
ccusage only when asked: the cached report shows at once, then a refresh
reprices what changed since (``r`` forces today's slots again).
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget

from claude_rotater.cost_report import CostReport, CostResult, ModelLine
from claude_rotater.dollar_per_pct import PerPct
from claude_rotater.tui.theme import Palette

HINT_KEYS: tuple[tuple[str, str], ...] = (
    ("r", "refresh"),
    ("y / Y", "copy"),
    ("esc", "back"),
)

LOADING = "reading transcripts… (the first time takes a while)"
NOTE = "~ estimate, ? unsure · $/1% = spend per 1% of a limit, ↓ = limit shrinking · priced by ccusage"
MODEL_ROWS = 5  # the costliest four, then one "other" row
COLORED_MODELS = 3
CHART_MAX_H = 10
CHART_MIN_H = 3
CHART_ROOMY_H = 6
MAX_GRID_LINES = 4
SIDE_BY_SIDE_W = 70  # narrower: the charts stack, the hourly one over 12 hours
NARROW_HOURS = 12
_BLOCKS = "▁▂▃▄▅▆▇"
_SPARK = "▁▂▃▄▅▆▇█"
_GRID = "╌"


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


def nice_step(peak: float, max_lines: int = MAX_GRID_LINES) -> float:
    """The smallest 1-2-5 step that puts between one and ``max_lines``
    gridlines under ``peak`` (7 -> 2, 140 -> 50, 3400 -> 1000)."""
    exp = math.floor(math.log10(peak)) - 1
    while True:
        for m in (1, 2, 5):
            step = m * 10.0 ** exp
            if peak // step <= max_lines:
                return step
        exp += 1


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


def per_pct_trend(p: PerPct | None) -> str:
    """``↓12%/4w``; ``steady`` within the noise; empty with too little data."""
    if p is None or p.trend is None:
        return ""
    if p.steady:
        return "steady"
    return f"{'↑' if p.trend > 0 else '↓'}{abs(p.trend) * 100:.0f}%/4w"


def spark(values: Sequence[float]) -> str:
    """A sparkline, lowest value at ▁ and highest at █ (mid when all equal)."""
    lo, hi = min(values, default=0.0), max(values, default=0.0)
    if hi <= lo:
        return _SPARK[3] * len(values)
    return "".join(_SPARK[round((v - lo) / (hi - lo) * (len(_SPARK) - 1))] for v in values)


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


def grid_rows(peak: float, height: int) -> dict[int, str]:
    """``{row: label}`` of the gridlines, row 0 at the bottom: each at the
    row whose top its value reaches, a shared row keeping the lower one."""
    if peak <= 0 or height <= 0:
        return {}
    step = nice_step(peak, min(MAX_GRID_LINES, max(1, height // 2)))
    rows: dict[int, str] = {}
    k = 1
    while k * step <= peak * (1 + 1e-9):
        row = min(height - 1, max(0, math.ceil(k * step / peak * height - 1e-9) - 1))
        rows.setdefault(row, short_money(k * step))
        k += 1
    return rows


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
    model holding each cell; dashed rows mark round dollar values."""
    totals = [sum(c.values()) for c in columns]
    peak = max(totals, default=0.0)
    grid = grid_rows(peak, height)
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
            eighths = round(total / peak * height * 8) if peak > 0 else 0
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
    span = col * n
    if under is not None:
        lines.append(_duty_row(under, col, label_w, palette))
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
    lines.append(text)
    return lines


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


def daily_chart(report: CostReport, width: int, height: int, palette: Palette) -> list[Text]:
    first = time.strftime("%m-%d", time.localtime(report.days[0][0])) if report.days else ""
    return bar_chart([day for _, day in report.days], width, height,
                     [m.name for m in report.models[:COLORED_MODELS]],
                     model_colors(report, palette), palette, axis=(first, "daily", "today"))


def hourly_chart(
    report: CostReport, width: int, height: int, palette: Palette, hours: int | None = None
) -> list[Text]:
    hours = hours or len(report.hours)
    shown = report.hours[-hours:]
    first = time.strftime("%H:00", time.localtime(shown[0][0])) if shown else ""
    duty = report.hour_duty[-hours:]
    return bar_chart([h for _, h in shown], width, height,
                     [m.name for m in report.models[:COLORED_MODELS]],
                     model_colors(report, palette), palette, axis=(first, "hourly", "now"),
                     under=duty if any(d is not None for d in duty) else None)


def _pad(line: Text, width: int) -> Text:
    out = line.copy()
    out.truncate(width)
    out.append(" " * (width - out.cell_len))
    return out


def chart_lines(
    report: CostReport, width: int, height: int, palette: Palette, *, hourly_only: bool = False
) -> list[Text]:
    """Daily and hourly charts: side by side when wide, else stacked with
    the hourly one over :data:`NARROW_HOURS` (``hourly_only`` drops the daily)."""
    if width >= SIDE_BY_SIDE_W and not hourly_only:
        gap = 3
        left_w = (width - gap) * len(report.days) // (len(report.days) + len(report.hours))
        left = daily_chart(report, left_w, height, palette)
        right = hourly_chart(report, width - gap - left_w, height, palette)
        left[-1:-1] = [Text()] * (len(right) - len(left))  # keep the axes level
        lines = []
        for a, b in zip(left, right):
            line = _pad(a, left_w)
            line.append(" " * gap)
            line.append(b)
            lines.append(line)
        return lines
    hours = len(report.hours) if width >= 2 * len(report.hours) + 6 else NARROW_HOURS
    hourly = hourly_chart(report, width, height, palette, hours)
    if hourly_only:
        return hourly
    return daily_chart(report, width, height, palette) + [Text()] + hourly


def chart_rows(report: CostReport, width: int, height: int, *, hourly_only: bool) -> int:
    """How many rows :func:`chart_lines` takes for bars ``height`` high."""
    under = int(any(d is not None for d in report.hour_duty))
    if width >= SIDE_BY_SIDE_W and not hourly_only:
        return height + 1 + under
    one = height + 1
    return one + under if hourly_only else 2 * one + 1 + under


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


def per_pct_cell(p: PerPct | None, width: int, palette: Palette, *, sparks: bool) -> Text:
    """``~$5.30 ▃▄▅▆ ↓12%/4w`` in ``width`` cells: the figure (saffron and
    ``?`` when unsure), the per-day or per-window sparkline, the trend."""
    cell = Text(no_wrap=True)
    value = per_pct_value(p)
    cell.append(f"{value:<7}", style=palette.foreground if p is None or p.sure
                else palette.sev_warn)
    if p is not None and sparks and len(p.spark) >= 3:
        cell.append(" " + spark(p.spark), style=palette.muted)
    trend = per_pct_trend(p)
    if trend:
        style = (palette.muted if trend == "steady"
                 else palette.sev_warn if trend.startswith("↓") else palette.sev_ok)
        cell.append(" " + trend, style=style)
    return _pad(cell, width)


def account_lines(
    report: CostReport, width: int, rows: int, names: dict[str, str], palette: Palette
) -> list[Text]:
    shown = report.accounts[:rows]
    labels = [names.get(a.number, f"#{a.number}") for a in shown]
    want = max([len("account"), *(len(s) for s in labels)])
    sparks = width >= 10 + 2 + 7 + 2 * (2 + 23)
    est_w = 23 if sparks else 15
    name_w = max(8, min(want, width - 7 - 2 * (est_w + 2) - 2))
    out = [_row([("account", -name_w, palette.muted), ("30d", 7, palette.muted),
                 ("$/1% 5h", -est_w, palette.muted), ("$/1% 7d", -est_w, palette.muted)])]
    for acc, label in zip(shown, labels):
        line = _row([(label, -name_w, palette.foreground),
                     (money(acc.cost), 7, f"bold {palette.foreground}")])
        for p in (acc.per_5h, acc.per_7d):
            line.append("  ")
            line.append(per_pct_cell(p, est_w, palette, sparks=sparks))
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
    status: str = "",
    empty: str = LOADING,
) -> list[Text]:
    """The page at ``width`` x ``height``, richest layout that fits.

    Lower-priority parts give way first: the note, the gaps, model rows
    (down to three), chart rows (down to three), the daily chart where the
    charts stack, then the charts and the model table. The summary and the
    account table stay.
    """
    if report is None:
        return [Text(status or empty, style=palette.muted)]
    n_models = min(len(report.models), MODEL_ROWS)
    n_accounts = len(report.accounts)
    summary = summary_text(report, palette, status=status)
    if summary.cell_len > width:  # the freshness note is the first to go
        summary = summary_text(report, palette)

    def fits(note: bool, gaps: bool, model_rows: int, chart: str | None,
             min_h: int) -> int | None:
        """Bar height that fits these choices (0: no chart), None if none does."""
        used = 1 + (1 + n_accounts if n_accounts else 0) + (1 + model_rows if model_rows else 0)
        blocks = 1 + bool(n_accounts) + bool(model_rows) + bool(chart) + note
        used += note + (blocks - 1 if gaps else 0)
        if chart is None:
            return 0 if used <= height else None
        for h in range(CHART_MAX_H, min_h - 1, -1):
            if used + chart_rows(report, width, h, hourly_only=chart == "hourly") <= height:
                return h
        return None

    plans = [
        (note, gaps, rows, chart, min_h)
        for chart, min_h in (("both", CHART_ROOMY_H), ("hourly", CHART_ROOMY_H),
                             ("both", CHART_MIN_H), ("hourly", CHART_MIN_H), (None, 0))
        for rows in (n_models, min(n_models, 3), 0)
        for gaps in (True, False)
        for note in (True, False)
    ]
    plan, chart_h = (False, False, 0, None, 0), 0
    for candidate in plans:
        if candidate[2] == 0 and n_models and candidate[3]:
            continue  # a chart without its legend (the model table) is no use
        h = fits(*candidate)
        if h is not None:
            plan, chart_h = candidate, h
            break
    note, gaps, model_rows, chart, _ = plan
    blocks: list[list[Text]] = [[summary]]
    if chart:
        blocks.append(chart_lines(report, width, chart_h, palette, hourly_only=chart == "hourly"))
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
    """The whole page as text, every part, for ``Y``."""
    lines = costs_lines(report, width=width, height=200, names=names, palette=Palette.DARK)
    return "\n".join(line.plain.rstrip() for line in lines)


# -- the widget ----------------------------------------------------------------------


class CostsView(Widget, can_focus=True):
    """The costs page; posts :class:`CostsView.Closed` on esc or ``$``."""

    class Closed(Message):
        """The user left the costs: the host shows its body again."""

    BINDINGS = [
        Binding("escape,dollar_sign", "close", show=False),
        Binding("r", "refresh", show=False),
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
                            palette=palette, status=self.status())
        return Text("\n", no_wrap=True, overflow="ellipsis").join(lines)

    # -- actions ---------------------------------------------------------------------

    def action_close(self) -> None:
        self.post_message(self.Closed())

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
