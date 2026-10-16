"""The main page: every account on one line, a detail plot, the auto-switch line.

Layout, top to bottom:

- status line: the always-on engine's threshold, cooldown, last/next check,
  and the last switch (or why it did not switch);
- column header + one ``AccountRow`` per account (active marker, 5h/7d bars
  with the projected pct beside the measured one, burn rate, ETA to the
  threshold with a confidence glyph, binding window's reset);
- detail pane for the highlighted account: a braille plot of the binding
  window (measured solid, projection dotted, threshold dashed, reset marker)
  plus a sparkline per other window;
- a 3-line log of recent non-routine engine events;
- the action bar, mirroring the key bindings.

The renderers below are plain functions of (snapshot row, estimate, now) so
they are tested without an app; the screen only wires them to app state.
Nothing here does I/O: estimates and history are computed in the app's
refresh worker and re-projected from the cached estimate once a second.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, ListItem, ListView, RichLog, Static

from claude_swap import oauth
from claude_swap.autoswitch import AutoSwitchEvent, pct_label
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.tui import data
from claude_swap.tui._wp1_stub import AccountEstimate, Point, WindowEstimate
from claude_swap.tui.plot import line_plot, sparkline
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import account_card_text, bar_cells
from claude_swap.usage_store import STALE_OK_S

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

# ---------------------------------------------------------------------------
# Engine event log lines
# ---------------------------------------------------------------------------

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
# Routine engine chatter: summarized by the status line, kept out of the log.
QUIET_KINDS = frozenset({"poll", "no-switch", "sleep", "account-unquarantined"})


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


# ---------------------------------------------------------------------------
# Auto-switch status line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoStatus:
    """What the status line knows about the engine, folded from its events.

    ``last_switch_at`` may also come from the shared state file, so a switch
    made by another process (``cswap auto``, the menu bar) still shows its
    cooldown here.
    """

    threshold: float | None = None
    cooldown_s: float = 0.0
    running: bool = False
    last_check_at: float | None = None
    next_check_at: float | None = None
    last_switch_at: float | None = None
    last_switch: str = ""  # "2 → 1" when this session saw the switch
    last_outcome: str = ""  # most recent no-switch reason / error summary
    events: tuple[str, ...] = field(default=())


def status_text(
    status: AutoStatus, *, now: float, width: int, palette: Palette = Palette.DARK
) -> Text:
    """``claude-swap  AUTO ● thr 90% · cooldown 4m · next 1m 20s   14:02 2 → 1``."""
    left = Text(no_wrap=True, overflow="ellipsis")
    left.append("claude-swap  ", style=f"bold {palette.foreground}")
    if status.running:
        left.append("AUTO ●", style=f"bold {palette.accent}")
    else:
        left.append("AUTO ○", style=palette.muted)
    parts: list[str] = []
    if status.threshold is not None:
        parts.append(f"thr {pct_label(status.threshold)}%")
    if status.last_switch_at is not None and status.cooldown_s > 0:
        remaining = status.last_switch_at + status.cooldown_s - now
        if remaining > 0:
            parts.append(f"cooldown {data.format_duration(remaining)}")
    if status.last_check_at is not None:
        parts.append(f"checked {data.format_duration(max(0.0, now - status.last_check_at))} ago")
    if status.next_check_at is not None:
        parts.append(f"next {data.format_duration(max(0.0, status.next_check_at - now))}")
    if parts:
        left.append("  " + " · ".join(parts), style=palette.muted)

    right = Text(no_wrap=True)
    if status.last_switch_at is not None:
        clock = time.strftime("%H:%M", time.localtime(status.last_switch_at))
        label = f"switched {status.last_switch}" if status.last_switch else "last switch"
        right.append(f"{clock} {label}", style=palette.accent)
    elif status.last_outcome:
        right.append(status.last_outcome, style=palette.muted)

    gap = width - left.cell_len - right.cell_len
    if right.cell_len and gap >= 2:
        left.append(" " * gap)
        left.append(right)
    return left


# ---------------------------------------------------------------------------
# Account rows
# ---------------------------------------------------------------------------

_CONFIDENCE_GLYPH = {"high": " ", "medium": "~", "low": "?"}
_PREFIX_W = 4  # "▸ 1 "
_TAG_W = 12  # " [personal] "
_BURN_W = 9  # "+12.5%/h "
_ETA_W = 10  # "3h 30m ~ "
_RESET_W = 7  # "2h 13m"
_MIN_NAME_W = 12
MAX_PLOT_LINES = 11  # a compact plot reads better than a full-height one


@dataclass(frozen=True)
class RowLayout:
    """Column widths for one terminal width; header and rows share it.

    Wide terminals get bars; narrower ones keep the pcts and drop the bars,
    then the reset and burn columns, so the account name never vanishes.
    """

    width: int
    windows: tuple[str, ...]
    name_w: int
    bar_w: int
    show_burn: bool
    show_reset: bool

    @property
    def window_w(self) -> int:
        # bar + " " + "100%" + " →100%"(projection) + gap
        return (self.bar_w + 1 if self.bar_w else 0) + 4 + 6 + 1

    @classmethod
    def for_width(cls, width: int, windows: Sequence[str]) -> "RowLayout":
        windows = tuple(windows)
        for bar_w, burn, reset in (
            (10, True, True),
            (6, True, True),
            (0, True, True),
            (0, True, False),
            (0, False, False),
        ):
            layout = cls(width, windows, 0, bar_w, burn, reset)
            fixed = (
                _PREFIX_W
                + _TAG_W
                + len(windows) * layout.window_w
                + (_BURN_W if burn else 0)
                + _ETA_W
                + (_RESET_W if reset else 0)
            )
            if width - fixed >= _MIN_NAME_W:
                # Names rarely need more than ~28 cells; the rest is slack.
                name_w = min(width - fixed, 28)
                return cls(width, windows, name_w, bar_w, burn, reset)
        return cls(width, windows, _MIN_NAME_W, 0, False, False)


def _fit(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "…"


def header_text(layout: RowLayout, *, threshold: float | None, palette: Palette = Palette.DARK) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis", style=palette.muted)
    text.append(" " * _PREFIX_W)
    text.append(_fit("account", layout.name_w + _TAG_W))
    for label in layout.windows:
        text.append(_fit(label, layout.window_w))
    if layout.show_burn:
        text.append(_fit("burn", _BURN_W))
    eta = f"ETA→{pct_label(threshold)}%" if threshold is not None else "ETA"
    text.append(_fit(eta, _ETA_W))
    if layout.show_reset:
        text.append("reset")
    return text


def window_pcts(acc: AccountSnapshot, models: Sequence[str]) -> dict[str, tuple[float, float | None]]:
    """label → (measured pct, resets_at epoch) for every window that gates the account."""
    return {
        label: (pct, data.iso_epoch(resets_at))
        for label, pct, resets_at in oauth.relevant_windows(acc.usage.last_good, models)
    }


def live_eta(est: AccountEstimate | None, threshold: float | None, now: float) -> float | None:
    """ETA to the threshold re-evaluated at ``now`` from the cached estimate."""
    if est is None or threshold is None:
        return None
    etas = [w.eta_s(threshold, now) for w in est.windows.values()]
    known = [e for e in etas if e is not None]
    return min(known) if known else None


def eta_cell(est: AccountEstimate | None, threshold: float | None, now: float) -> tuple[str, str]:
    """(text, role) for the ETA column. Roles: "muted", "warn", "fg"."""
    if est is None or not est.windows or est.confidence == "none":
        return "—", "muted"
    eta = live_eta(est, threshold, now)
    if eta is None:
        # The rate is known but the crossing lands after the window resets.
        return "≥reset", "muted"
    if eta <= 0:
        return "now", "warn"
    glyph = _CONFIDENCE_GLYPH.get(est.confidence, "?")
    return f"{data.format_duration(eta)} {glyph}".rstrip(), "fg"


def burn_cell(est: AccountEstimate | None) -> str:
    binding = est.binding if est is not None else None
    if binding is None or binding.rate_pct_per_h is None:
        return "—"
    if binding.rate_pct_per_h < 0.05:
        return "0"
    return f"+{binding.rate_pct_per_h:.1f}%/h"


def account_row_text(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    layout: RowLayout,
    *,
    now: float,
    threshold: float | None,
    models: Sequence[str] = (),
    palette: Palette = Palette.DARK,
) -> Text:
    """One account on one line, laid out by ``layout``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    marker = "▸" if acc.is_active else " "
    text.append(marker, style=f"bold {palette.accent}")
    text.append(f"{acc.number:>2} ", style=f"bold {palette.foreground if acc.is_active else palette.muted}")
    name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
    name_style = palette.muted if acc.disabled else palette.foreground
    text.append(_fit(name, layout.name_w), style=f"bold {name_style}" if acc.is_active else name_style)
    tag = "disabled" if acc.disabled else f"[{acc.display_tag}]"
    text.append(" " + _fit(tag, _TAG_W - 1), style=palette.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        text.append(data.sentinel_label(sentinel), style=style)
        return text
    pcts = window_pcts(acc, models)
    if not pcts:
        text.append("usage unknown", style=palette.muted)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    for label in layout.windows:
        cell = Text(no_wrap=True)
        if label not in pcts:
            cell.append("·", style=palette.track)
        else:
            pct, _reset = pcts[label]
            if layout.bar_w:
                cell.append(bar_cells(pct, layout.bar_w, stale=stale, threshold=threshold, palette=palette))
                cell.append(" ")
            color = palette.severity(pct)
            cell.append(f"{pct:3.0f}%", style=f"{color} dim" if stale else color)
            west = est.windows.get(label) if est is not None else None
            projected = west.projected_pct(now) if west is not None else None
            if projected is not None and projected - pct >= 1.0:
                cell.append(f" →{projected:.0f}%", style=f"{palette.severity(projected)} dim")
        pad = layout.window_w - cell.cell_len
        text.append(cell)
        text.append(" " * max(1, pad))

    if layout.show_burn:
        text.append(_fit(burn_cell(est), _BURN_W), style=palette.muted)
    eta, role = eta_cell(est, threshold, now)
    eta_style = {"muted": palette.muted, "warn": palette.sev_warn}.get(role, palette.foreground)
    text.append(_fit(eta, _ETA_W), style=eta_style)
    if layout.show_reset:
        binding_label = max(pcts, key=lambda k: pcts[k][0])
        reset_at = pcts[binding_label][1]
        if reset_at is not None:
            remaining = reset_at - now
            reset = data.format_duration(remaining) if remaining > 0 else "now"
            text.append(reset, style=palette.muted)
    return text


# ---------------------------------------------------------------------------
# Detail pane
# ---------------------------------------------------------------------------


def _window_summary(
    label: str,
    est: WindowEstimate | None,
    points: Sequence[Point],
    measured: tuple[float, float | None] | None,
    *,
    now: float,
    width: int,
    palette: Palette,
) -> Text:
    """`` 7d ▁▁▂▂▃▃▄▅  31%  +0.4%/h  resets Thu 09:00`` for a non-plotted window."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{label:>3} ", style=palette.muted)
    spark_w = max(8, min(40, width - 40))
    reset_at = (est.resets_at if est is not None else None) or (measured[1] if measured else None)
    span = None
    if points:
        start = points[0].t
        span = (start, max(now, points[-1].t))
    text.append(sparkline(points, spark_w, palette=palette, span=span))
    pct = est.last_pct if est is not None else (measured[0] if measured else None)
    if pct is not None:
        text.append(f"  {pct:3.0f}%", style=palette.severity(pct))
    if est is not None and est.rate_pct_per_h is not None:
        text.append(f"  +{est.rate_pct_per_h:.1f}%/h", style=palette.muted)
    if reset_at is not None and reset_at > now:
        text.append(
            f"  resets {time.strftime('%a %H:%M', time.localtime(reset_at))}",
            style=palette.muted,
        )
    return text


def detail_lines(
    acc: AccountSnapshot | None,
    est: AccountEstimate | None,
    series: dict[str, list[Point]],
    *,
    now: float,
    threshold: float | None,
    width: int,
    height: int,
    models: Sequence[str] = (),
    palette: Palette = Palette.DARK,
) -> list[Text]:
    """The detail pane for one account, at most ``height`` lines."""
    if acc is None or height <= 0:
        return []
    name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
    binding = est.binding if est is not None else None
    title = f"─ {acc.number} · {name}" + (f" · {binding.window} " if binding else " ")
    rule = title + "─" * max(0, width - len(title))
    lines = [Text(rule, style=palette.muted, no_wrap=True, overflow="ellipsis")]
    if binding is None:
        # No history yet: the full card (bars, resets, sentinel notes) carries
        # the same facts the plot would, so the pane is never empty.
        card = account_card_text(acc, width, threshold=threshold, now=now, palette=palette)
        lines.extend(card.split())
        lines.append(Text("no usage history yet: the plot fills in as polls land", style=palette.muted))
        return lines[:height]

    pcts = window_pcts(acc, models)
    others = [label for label in pcts if label != binding.window]
    others += [label for label in est.windows if label != binding.window and label not in others]
    plot_h = max(0, min(MAX_PLOT_LINES, height - 1 - len(others)))
    window_points = [p for p in series.get(binding.window, ()) if p.t <= now]
    lines.extend(
        line_plot(
            binding,
            now=now,
            threshold=threshold,
            width=width,
            height=plot_h,
            palette=palette,
            points=window_points or None,
        )
        if plot_h
        else []
    )
    for label in others:
        lines.append(
            _window_summary(
                label,
                est.windows.get(label),
                series.get(label, []),
                pcts.get(label),
                now=now,
                width=width,
                palette=palette,
            )
        )
    return lines[:height]


# ---------------------------------------------------------------------------
# Widgets and screen
# ---------------------------------------------------------------------------


class _RowBody(Static):
    def render(self) -> Text:
        row = self.parent
        screen = self.screen
        if not isinstance(row, AccountRow) or not isinstance(screen, MainScreen):
            return Text()
        return screen.row_text(row.number, self.size.width or 80)


class AccountRow(ListItem):
    """One account line in the list; remembers its slot."""

    def __init__(self, number: str) -> None:
        super().__init__(_RowBody())
        self.number = number

    def repaint(self) -> None:
        self.query_one(_RowBody).refresh()


class _Painted(Static):
    """A Static whose content is drawn by a screen method at its own size."""

    def __init__(self, painter: str, *, id: str) -> None:
        super().__init__(id=id)
        self._painter = painter

    def render(self) -> Text:
        screen = self.screen
        if not isinstance(screen, MainScreen):
            return Text()
        return getattr(screen, self._painter)(self.size.width or 80, self.size.height or 1)


class ActionButton(Button):
    """Mouse-clickable action chip that never takes focus: the account list
    keeps the keyboard, so arrows/enter always mean the list."""

    can_focus = False


ACTIONS: tuple[tuple[str, str, str], ...] = (
    # (button id, label, screen action)
    ("act-switch", "⏎ switch", "switch_selected"),
    ("act-add", "a add", "app.add_current"),
    ("act-token", "t token", "app.add_token"),
    ("act-disable", "d disable", "toggle_selected"),
    ("act-remove", "x remove", "remove_selected"),
    ("act-config", "c config", "app.open_config"),
    ("act-quit", "q quit", "app.quit"),
)


NARROW_ACTIONS_W = 76
OPTIONAL_ACTIONS = ("act-switch", "act-quit")


class MainScreen(Screen):
    """The only page besides Config: accounts, detail, auto status, actions."""

    BINDINGS = [
        Binding("a", "app.add_current", "Add login", show=False),
        Binding("t", "app.add_token", "Add token", show=False),
        Binding("d", "toggle_selected", "Disable/enable", show=False),
        Binding("x,delete", "remove_selected", "Remove", show=False),
        Binding("c", "app.open_config", "Config", show=False),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("q", "app.quit", "Quit", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def compose(self) -> ComposeResult:
        yield _Painted("status_line", id="status")
        yield _Painted("header_line", id="columns")
        yield ListView(id="accounts")
        yield _Painted("detail_text", id="detail")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=False, max_lines=50)
        with Horizontal(id="actions"):
            for button_id, label, _action in ACTIONS:
                yield ActionButton(label, id=button_id)

    def on_mount(self) -> None:
        self.query_one("#accounts", ListView).focus()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "estimates", lambda _e: self.repaint())
        self.watch(self.app, "auto_status", lambda _s: self._repaint_status())
        self.watch(self.app, "theme", lambda _t: self.repaint())
        self.set_interval(1.0, self.repaint)

    # -- state accessors ------------------------------------------------------

    @property
    def palette(self) -> Palette:
        return Palette.from_theme(self.app.current_theme)

    def account(self, number: str | None) -> AccountSnapshot | None:
        snap = self.app.snapshot
        if snap is None or number is None:
            return None
        return next((a for a in snap.accounts if a.number == number), None)

    @property
    def selected_number(self) -> str | None:
        child = self.query_one("#accounts", ListView).highlighted_child
        return child.number if isinstance(child, AccountRow) else None

    def window_labels(self) -> tuple[str, ...]:
        labels = ["5h", "7d"]
        snap = self.app.snapshot
        for acc in snap.accounts if snap else ():
            for label in window_pcts(acc, self.app.models):
                if label not in labels:
                    labels.append(label)
        return tuple(labels)

    def layout_for(self, width: int) -> RowLayout:
        return RowLayout.for_width(width, self.window_labels())

    # -- painters (called from widget render) ---------------------------------

    def status_line(self, width: int, _height: int) -> Text:
        return status_text(self.app.auto_status, now=time.time(), width=width, palette=self.palette)

    def header_line(self, width: int, _height: int) -> Text:
        snap = self.app.snapshot
        if snap is None:
            return Text("loading…", style=self.palette.muted)
        if not snap.accounts:
            return Text(
                "No managed accounts yet. Press a to add your current Claude Code "
                "login, or t to add a setup-token / API key.",
                style=self.palette.muted,
            )
        # Rows sit inside the ListView's 1-cell padding; match it.
        return header_text(self.layout_for(width - 1), threshold=self.app.threshold_pct, palette=self.palette)

    def row_text(self, number: str, width: int) -> Text:
        acc = self.account(number)
        if acc is None:
            return Text()
        return account_row_text(
            acc,
            self.app.estimates.get(number),
            self.layout_for(width),
            now=time.time(),
            threshold=self.app.threshold_pct,
            models=self.app.models,
            palette=self.palette,
        )

    def detail_text(self, width: int, height: int) -> Text:
        number = self.selected_number
        acc = self.account(number)
        lines = detail_lines(
            acc,
            self.app.estimates.get(number) if number else None,
            self.app.series.get(number, {}) if number else {},
            now=time.time(),
            threshold=self.app.threshold_pct,
            width=width,
            height=height,
            models=self.app.models,
            palette=self.palette,
        )
        return Text("\n").join(lines)

    # -- refresh ---------------------------------------------------------------

    def repaint(self) -> None:
        for widget_id in ("#status", "#columns", "#detail"):
            self.query_one(widget_id).refresh()
        for row in self.query(AccountRow):
            row.repaint()

    def _repaint_status(self) -> None:
        self.query_one("#status").refresh()

    async def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        listview = self.query_one("#accounts", ListView)
        numbers = [acc.number for acc in snap.accounts]
        current = [row.number for row in listview.query(AccountRow)]
        if numbers != current:
            keep = self.selected_number or snap.active_number
            await listview.clear()
            await listview.extend(AccountRow(n) for n in numbers)
            if numbers:
                listview.index = numbers.index(keep) if keep in numbers else 0
        self.repaint()

    def on_resize(self, event) -> None:
        # The full bar needs ~74 cells. Below that, drop the two chips whose
        # keys need no reminder (enter, q) so the rest stay on screen.
        wide = event.size.width >= NARROW_ACTIONS_W
        for button_id in OPTIONAL_ACTIONS:
            self.query_one(f"#{button_id}").display = wide

    def on_list_view_highlighted(self, _event: ListView.Highlighted) -> None:
        self.query_one("#detail").refresh()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, AccountRow):
            self.app.do_switch(event.item.number)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        action = next((a for bid, _l, a in ACTIONS if bid == event.button.id), None)
        if action is not None:
            await self.run_action(action)

    def write_event(self, event: AutoSwitchEvent) -> None:
        if event.kind in QUIET_KINDS:
            return
        self.query_one("#event-log", RichLog).write(event_text(event, palette=self.palette))

    # -- actions ---------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()

    def action_switch_selected(self) -> None:
        number = self.selected_number
        if number is not None:
            self.app.do_switch(number)

    def action_toggle_selected(self) -> None:
        number = self.selected_number
        if number is not None:
            self.app.do_toggle_disabled(number)

    def action_remove_selected(self) -> None:
        acc = self.account(self.selected_number)
        if acc is not None:
            self.app.confirm_remove(acc.number, acc.email)


__all__ = [
    "AccountRow",
    "AutoStatus",
    "MainScreen",
    "QUIET_KINDS",
    "RowLayout",
    "account_row_text",
    "detail_lines",
    "event_text",
    "header_text",
    "status_text",
]
