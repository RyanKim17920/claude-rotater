"""The costs view: Claude Code spend in place of the account body (``$``).

Header and prompt line stay; esc (or ``$``) goes back. What it shows, top
to bottom, dropping the lower parts first when rows run out:

- one summary line: today / 7d / 30d, and the week's trend with the top model;
- daily cost for 30 days, each bar stacked by model (top three colored);
- per model: cost, share, tokens (cache writes and reads included), trend;
- per account: cost and the estimated $ per 1% of the 5h and 7d windows;
- a note on what is estimated.

Data comes from :class:`claude_rotater.cost_report.CostSource`, which runs
ccusage only when asked: the cached report shows at once, then a refresh
reprices what changed since (``r`` forces today's slots again).
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget

from claude_rotater.cost_report import CostReport, CostResult, Estimate, ModelLine
from claude_rotater.tui.theme import Palette

HINT_KEYS: tuple[tuple[str, str], ...] = (
    ("r", "refresh"),
    ("y / Y", "copy"),
    ("esc", "back"),
)

LOADING = "reading transcripts… (the first time takes a while)"
NOTE = "~ estimate · accounts by switch time (approximate) · priced by ccusage"
MODEL_ROWS = 5  # the costliest four, then one "other" row
COLORED_MODELS = 3
CHART_MAX_H = 12
CHART_MIN_H = 3
_BLOCKS = "▁▂▃▄▅▆▇"


# -- formatting ------------------------------------------------------------------


def money(usd: float) -> str:
    if usd < 10:
        return f"${usd:.2f}"
    if usd < 100:
        return f"${usd:.1f}"
    if usd < 100_000:
        return f"${usd:,.0f}"
    return f"${usd / 1000:,.0f}k"


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
    return a if a == "→" else f"{a} {abs(change) * 100:.0f}%"


def estimate_text(est: Estimate | None) -> str:
    """``~$3.10 ±20%``; ``±?`` from a single window; ``–`` with none."""
    if est is None:
        return "–"
    err = "±?" if est.rel_err is None else f"±{min(est.rel_err, 9.99) * 100:.0f}%"
    return f"~{money(est.dollars)} {err}"


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
    """``today $35 · 7d $1,392 · 30d $5,757   ↑ 18% vs last week · Opus 5.5 72%``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, (label, usd) in enumerate((("today", report.today), ("7d", report.week),
                                      ("30d", report.month))):
        if i:
            text.append(" · ", style=palette.muted)
        text.append(f"{label} ", style=palette.muted)
        text.append(money(usd), style=f"bold {palette.foreground}")
    words = trend_words(report.week_change)
    top = report.top_model
    tail: list[str] = []
    if words:
        tail.append(f"{words} vs last week")
    if top is not None:
        tail.append(f"{top.name} {top.share * 100:.0f}%")
    if tail:
        text.append("   ")
        text.append(" · ".join(tail), style=palette.foreground)
    if status:
        text.append(f"   {status}", style=palette.muted)
    return text


def _stack(day: dict[str, float], order: Sequence[str]) -> list[tuple[str, float]]:
    """A day's spend bottom-up: the costliest models first, the rest as one."""
    shown = [(name, day.get(name, 0.0)) for name in order]
    rest = sum(v for k, v in day.items() if k not in order)
    return shown + ([("", rest)] if rest > 0 else [])


def chart_lines(
    report: CostReport, width: int, height: int, palette: Palette
) -> list[Text]:
    """Daily bars, ``height`` rows plus a date axis; a bar is cut in eighths
    at its top and colored by the model holding each cell."""
    colors = model_colors(report, palette)
    order = [m.name for m in report.models[:COLORED_MODELS]]
    totals = [sum(day.values()) for _, day in report.days]
    peak = max(totals, default=0.0)
    label = money(peak) if peak > 0 else "$0"
    label_w = len(label) + 1
    n = max(1, len(report.days))
    col = max(1, min(4, (width - label_w) // n))
    bar = col - 1 if col >= 3 else col
    lines: list[Text] = []
    for r in range(height - 1, -1, -1):
        line = Text(no_wrap=True)
        line.append(f"{label if r == height - 1 else '':>{label_w - 1}} ", style=palette.muted)
        for (_, day), total in zip(report.days, totals):
            eighths = round(total / peak * height * 8) if peak > 0 else 0
            lo = r * 8
            if eighths <= lo:
                line.append(" " * col)
                continue
            fill = min(8, eighths - lo)
            mid = lo + fill / 2
            scale = eighths / total if total else 0.0
            acc, who = 0.0, ""
            for name, usd in _stack(day, order):
                acc += usd * scale
                who = name
                if acc >= mid:
                    break
            glyph = "█" if fill == 8 else _BLOCKS[fill - 1]
            line.append(glyph * bar, style=colors.get(who, palette.muted))
            line.append(" " * (col - bar))
        lines.append(line)
    axis = Text(no_wrap=True, style=palette.muted)
    span = col * n
    first = time.strftime("%m-%d", time.localtime(report.days[0][0])) if report.days else ""
    right = "today"
    middle = "daily cost"
    gap = span - len(first) - len(right)
    axis.append(" " * label_w + first)
    if gap > len(middle) + 2:
        left = (gap - len(middle)) // 2
        axis.append(" " * left + middle + " " * (gap - left - len(middle)))
    else:
        axis.append(" " * max(1, gap))
    axis.append(right)
    lines.append(axis)
    return lines


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
    wide = width >= name_w + 70
    narrow = width < name_w + 52
    head: list[tuple[str, int, str]] = [("model", -(name_w + 2), palette.muted),
                                        ("cost", 7, palette.muted), ("share", 5, palette.muted)]
    if not narrow:
        head.append(("input", 6, palette.muted))
    head.append(("output", 6, palette.muted))
    if not narrow:
        head.append(("cache w", 7, palette.muted))
    head += [("cache r", 7, palette.muted), ("7d", -(7 if wide else 3), palette.muted)]
    out = [_row(head)]
    for m in models:
        swatch = colors.get(m.name, palette.muted)
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("■ ", style=swatch)
        line.append(f"{m.name:<{name_w}}", style=palette.foreground)
        trend = (trend_words(m.trend) or arrow(m.trend)) if wide else arrow(m.trend)
        if m.name.endswith(" other"):
            trend = ""
        cells: list[tuple[str, int, str]] = [
            (money(m.cost), 7, f"bold {palette.foreground}"),
            (f"{m.share * 100:.0f}%", 5, palette.foreground),
        ]
        if not narrow:
            cells.append((tokens(m.input), 6, palette.muted))
        cells.append((tokens(m.output), 6, palette.muted))
        if not narrow:
            cells.append((tokens(m.cache_write), 7, palette.muted))
        cells += [(tokens(m.cache_read), 7, palette.muted),
                  (trend, -(7 if wide else 3), palette.foreground)]
        line.append("  ")
        line.append(_row(cells))
        out.append(line)
    return out


def account_lines(
    report: CostReport, width: int, rows: int, names: dict[str, str], palette: Palette
) -> list[Text]:
    shown = report.accounts[:rows]
    labels = [names.get(a.number, f"#{a.number}") for a in shown]
    est_w = 12
    name_w = max(len("account"), *(len(s) for s in labels)) if labels else len("account")
    name_w = min(name_w, max(8, width - 2 * (est_w + 2) - 9))
    out = [_row([("account", -name_w, palette.muted), ("cost", 7, palette.muted),
                 ("$/1% 5h", -est_w, palette.muted), ("$/1% 7d", -est_w, palette.muted)])]
    for acc, label in zip(shown, labels):
        out.append(_row([
            (label, -name_w, palette.foreground),
            (money(acc.cost), 7, f"bold {palette.foreground}"),
            (estimate_text(acc.per_5h), -est_w, palette.foreground),
            (estimate_text(acc.per_7d), -est_w, palette.foreground),
        ]))
    return out


def note_text(report: CostReport, palette: Palette) -> Text:
    note = NOTE
    if report.unattributed >= 1:
        note += f" · {money(report.unattributed)} unattributed"
    return Text(note, style=palette.muted, no_wrap=True, overflow="ellipsis")


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

    Lower-priority parts give way first: the note, the gaps, chart rows
    (down to three), model rows (down to three), then the chart, then the
    model table. The summary and the account table stay.
    """
    if report is None:
        return [Text(status or empty, style=palette.muted)]
    n_models = min(len(report.models), MODEL_ROWS)
    n_accounts = len(report.accounts)
    summary = summary_text(report, palette, status=status)

    def layout(note: bool, gaps: bool, model_rows: int, chart: bool) -> int | None:
        """Chart height that fits these choices (0: no chart), None if none does."""
        parts = 1 + (1 + model_rows if model_rows else 0) + (1 + n_accounts if n_accounts else 0)
        blocks = 1 + bool(model_rows) + bool(n_accounts) + chart + note
        used = parts + note + (blocks - 1 if gaps else 0)
        if not chart:
            return 0 if used <= height else None
        room = height - used - 1  # the axis
        return min(room, CHART_MAX_H) if room >= CHART_MIN_H else None

    plans = [
        (note, gaps, rows, chart)
        for chart in (True, False)
        for rows in (n_models, min(n_models, 3), 0)
        for gaps in (True, False)
        for note in (True, False)
    ]
    plan, chart_h = (False, False, 0, False), 0
    for candidate in plans:
        if candidate[2] == 0 and n_models and candidate[3]:
            continue  # a chart without its legend (the model table) is no use
        h = layout(*candidate)
        if h is not None:
            plan, chart_h = candidate, h
            break
    note, gaps, model_rows, chart = plan
    blocks: list[list[Text]] = [[summary]]
    if chart and report.days:
        blocks.append(chart_lines(report, width, chart_h, palette))
    if model_rows:
        blocks.append(model_lines(report, width, model_rows, palette))
    if n_accounts:
        blocks.append(account_lines(report, width, n_accounts, names, palette))
    if note:
        blocks.append([note_text(report, palette)])
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
        return Text("\n").join(lines)

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
