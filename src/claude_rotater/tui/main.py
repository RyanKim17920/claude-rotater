"""The main page: the roster, two usage graphs per account, the actions.

Layout, top to bottom:

- header band: the ``rota`` chip, one roster pip per account (who is on
  duty, how much headroom each has), the auto-switch state, the statusline
  feed state and the last switch;
- column header + one ``AccountRow`` per account (on-duty bar, 5h/7d meters
  with the projected pct beside the measured one, burn rate, ETA to the
  threshold with a confidence glyph, binding window's reset);
- detail pane for the highlighted account: separate 5h and 7d graphs (side
  by side when wide, stacked otherwise), each with the threshold, the history
  of the current window and a dotted projection to the reset; ``h`` swaps in
  the long-term view (past 5h peaks, the 7d curve across weeks);
- a 3-line log of recent non-routine engine events (hidden until the first);
- the action bar, mirroring the key bindings.

The renderers below are plain functions of (snapshot row, estimate, now) so
they are tested without an app; the screen only wires them to app state.
Nothing here does I/O, and nothing animates: the screen repaints when the
app's data changes, plus one coarse clock tick for countdown text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, ListItem, ListView, RichLog, Static

from claude_rotater import oauth
from claude_rotater.autoswitch import AutoSwitchEvent, pct_label
from claude_rotater.estimator import WINDOW_LEN_S, AccountEstimate, WindowEstimate
from claude_rotater.json_output import USAGE_API_KEY, USAGE_RELOGIN_REQUIRED
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.tui import data
from claude_rotater.tui.plot import history_plot, line_plot, peak_columns, sparkline
from claude_rotater.tui.theme import Palette, brand_chip
from claude_rotater.tui.widgets import account_card_text, bar_cells
from claude_rotater.usage_history import Point
from claude_rotater.usage_store import STALE_OK_S

if TYPE_CHECKING:
    from claude_rotater.tui.app import RotaApp

# ---------------------------------------------------------------------------
# Engine event log lines
# ---------------------------------------------------------------------------

_EVENT_ROLES = {
    "switch": "accent",
    "dead": "sev_warn",  # token refresher: a login came back dead
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
# Routine engine chatter: summarized by the status line, kept out of the log.
QUIET_KINDS = frozenset({"poll", "no-switch", "sleep", "account-unquarantined", "held"})


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
    made by another process (``rota auto``, ``rota switch``) still shows its
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


@dataclass(frozen=True)
class RosterPip:
    """One account in the header roster."""

    active: bool
    disabled: bool
    pct: float | None  # binding (highest) window pct, None when unknown


def roster_pips(accounts: Sequence[AccountSnapshot], models: Sequence[str] = ()) -> list[RosterPip]:
    pips = []
    for acc in accounts:
        pcts = [pct for pct, _r in window_pcts(acc, models).values()]
        pips.append(RosterPip(acc.is_active, acc.disabled, max(pcts) if pcts else None))
    return pips


FEED_LABELS = {
    # state -> (glyph, palette role, words)
    "live": ("●", "sev_ok", "feed live"),
    "stale": ("◐", "sev_warn", "feed stale"),
    "off": ("○", "muted", "feed off"),
}


def status_text(
    status: AutoStatus,
    *,
    now: float,
    width: int,
    palette: Palette = Palette.DARK,
    roster: Sequence[RosterPip] = (),
    feed: str | None = None,
) -> Text:
    """`` rota  ◉●◌  auto on · switch at 90% · next 1m      ● feed live  14:02 2 → 1``.

    Lower-priority facts drop out first when the width runs short: the
    "checked" age, then the next check, then the last outcome on the right.
    """
    head = Text(no_wrap=True, overflow="ellipsis")
    head.append(brand_chip(palette))
    if roster:
        head.append("  ")
        for pip in roster:
            glyph = "◉" if pip.active else ("◌" if pip.disabled else "●")
            color = palette.muted if pip.disabled else palette.severity(pip.pct)
            head.append(glyph, style=color)
    head.append("  auto ", style=palette.muted)
    if status.running:
        head.append("on", style=f"bold {palette.accent}")
    else:
        head.append("off", style=f"bold {palette.sev_crit}")

    parts: list[str] = []  # in priority order
    if status.threshold is not None:
        parts.append(f"switch at {pct_label(status.threshold)}%")
    if status.last_switch_at is not None and status.cooldown_s > 0:
        remaining = status.last_switch_at + status.cooldown_s - now
        if remaining > 0:
            parts.append(f"cooldown {data.format_duration(remaining)}")
    if status.next_check_at is not None:
        parts.append(f"next {data.format_duration(max(0.0, status.next_check_at - now))}")
    if status.last_check_at is not None:
        parts.append(f"checked {data.format_duration(max(0.0, now - status.last_check_at))} ago")

    feed_part = Text(no_wrap=True)
    if feed in FEED_LABELS:
        glyph, role, words = FEED_LABELS[feed]
        feed_part.append(f"{glyph} ", style=getattr(palette, role))
        feed_part.append(words, style=palette.muted)
    event = Text(no_wrap=True)
    if status.last_switch_at is not None:
        clock = time.strftime("%H:%M", time.localtime(status.last_switch_at))
        label = f"switched {status.last_switch}" if status.last_switch else "last switch"
        event.append(f"{clock} {label}", style=palette.accent)
    elif status.last_outcome:
        event.append(status.last_outcome, style=palette.muted)

    def right_side(with_event: bool) -> Text:
        right = feed_part.copy()
        if with_event and event.cell_len:
            if right.cell_len:
                right.append("   ")
            right.append(event)
        return right

    right = right_side(True)
    essential = head.cell_len + (2 + len(parts[0]) if parts else 0)
    if essential + 2 + right.cell_len > width and status.last_switch_at is None:
        right = right_side(False)  # a no-switch reason goes before the threshold
    budget = width - right.cell_len - (2 if right.cell_len else 0)
    for i, part in enumerate(parts):
        piece = ("  " if i == 0 else " · ") + part
        if head.cell_len + len(piece) > budget:
            break
        head.append(piece, style=palette.muted)

    gap = width - head.cell_len - right.cell_len
    if right.cell_len and gap >= 2:
        head.append(" " * gap)
        head.append(right)
    return head


# ---------------------------------------------------------------------------
# Account rows
# ---------------------------------------------------------------------------

# Shorter than the headless note: in the TUI the fix is one key away.
RELOGIN_ROW_LABEL = "re-login needed — press l to log in again"
_CONFIDENCE_GLYPH = {"high": " ", "medium": "~", "low": "?"}
ON_DUTY_MARK = "▌"
_PREFIX_W = 4  # "▌ 1 "
_TAG_W = 12  # " personal   "
_BURN_W = 9  # "+12.5%/h "
_ETA_W = 10  # "3h 30m ~ "
_RESET_W = 7  # "2h 13m"
_MIN_NAME_W = 12
# The plot takes whatever height the detail pane has, so the page has no
# dead band above the action bar; this only stops it turning into a wall on
# a very tall terminal.
MAX_PLOT_LINES = 32


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


def login_warning(acc: AccountSnapshot, now: float, warn_days: int) -> str | None:
    """Short tag when the stored login lapses within ``warn_days`` (or has).

    ``login 2d`` / ``login 5h 3m`` / ``login ended``; None when the end is
    unknown, far off, or warnings are off (0 days).
    """
    if acc.login_expires_at is None or warn_days <= 0:
        return None
    remaining = acc.login_expires_at - now
    if remaining <= 0:
        return "login ended"
    if remaining > warn_days * 86400:
        return None
    if remaining >= 86400:
        return f"login {int(remaining // 86400)}d"
    return f"login {data.format_duration(remaining)}"


def login_line(
    acc: AccountSnapshot, now: float, warn_days: int, palette: Palette
) -> Text | None:
    """Detail-pane line about the login itself: dead, ending soon, or its end."""
    if acc.usage.sentinel == USAGE_RELOGIN_REQUIRED:
        return Text(
            f"re-login needed: press l and log in as {acc.email} "
            "(the login page opens pre-filled; the active account is untouched)",
            style=palette.sev_warn, no_wrap=True, overflow="ellipsis",
        )
    if acc.login_expires_at is None:
        return None
    remaining = acc.login_expires_at - now
    clock = time.strftime("%a %d %b %H:%M", time.localtime(acc.login_expires_at))
    if remaining <= 0:
        msg, style = f"login ended {clock}: press l to log in again", palette.sev_warn
    else:
        msg = f"login ends {clock} (in {data.format_duration(remaining)})"
        warn = login_warning(acc, now, warn_days) is not None
        if warn:
            msg += ": press l to log in again ahead of time"
        style = palette.sev_warn if warn else palette.muted
    return Text(msg, style=style, no_wrap=True, overflow="ellipsis")


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
    login_warn_days: int = 3,
) -> Text:
    """One account on one line, laid out by ``layout``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    # The on-duty account carries a solid accent bar at the left edge.
    text.append(ON_DUTY_MARK if acc.is_active else " ", style=palette.accent)
    text.append(f"{acc.number:>2} ", style=f"bold {palette.accent}" if acc.is_active else palette.muted)
    name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
    name_style = palette.muted if acc.disabled else palette.foreground
    text.append(_fit(name, layout.name_w), style=f"bold {name_style}" if acc.is_active else name_style)
    warning = login_warning(acc, now, login_warn_days)
    if warning is not None and not acc.disabled:
        text.append(" " + _fit(warning, _TAG_W - 1), style=palette.sev_warn)
    else:
        tag = "disabled" if acc.disabled else acc.display_tag
        text.append(" " + _fit(tag, _TAG_W - 1), style=palette.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        label = RELOGIN_ROW_LABEL if sentinel == USAGE_RELOGIN_REQUIRED else data.sentinel_label(sentinel)
        text.append(label, style=style)
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


PLOTTED_WINDOWS = ("5h", "7d")
TITLE_MIN_HEIGHT = 16
# Two graphs sit side by side from this pane width; below it they stack.
SIDE_BY_SIDE_MIN_WIDTH = 100
_SIDE_GAP = 3
LONG_5H_SPAN_S = 7 * 86400.0
LONG_7D_SPAN_S = 30 * 86400.0
_MIN_LONG_7D_SPAN_S = 7 * 86400.0


@dataclass(frozen=True)
class LongTerm:
    """Past-window history for one account, as the long-term view draws it."""

    peaks_5h: tuple[tuple[float, float], ...] = ()  # (resets_at, peak pct)
    series_7d: tuple[Point, ...] = ()


def account_title(acc: AccountSnapshot, palette: Palette, suffix: str = "") -> Text:
    """``me@x.com  personal  on duty`` above the graphs."""
    name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(name, style=f"bold {palette.foreground}")
    text.append(f"  {'disabled' if acc.disabled else acc.display_tag}", style=palette.muted)
    if acc.is_active:
        text.append("  on duty", style=palette.accent)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
    return text


def _window_estimate(
    label: str,
    est: AccountEstimate | None,
    measured: tuple[float, float | None] | None,
    measured_at: float | None,
) -> WindowEstimate | None:
    """The estimate to plot for one window, or a rate-less stand-in built from
    the measured pct (so a window with no history still gets its graph)."""
    west = est.windows.get(label) if est is not None else None
    if west is not None:
        return west
    if measured is None:
        return None
    pct, resets_at = measured
    t = measured_at if measured_at is not None else time.time()
    return WindowEstimate(label, pct, t, resets_at, None, "none", (Point(t, pct, resets_at),))


def plot_title(
    west: WindowEstimate,
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
    measured_pct: float | None = None,
) -> Text:
    """``5h  62% →64%  +12.0%/h  90% in 2h 19m  resets 16:40``.

    The pct is the snapshot's measurement (the same number as the row), with
    the projection beside it once they differ by a point or more."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(west.window, style=f"bold {palette.accent}")
    pct = west.last_pct if measured_pct is None else measured_pct
    text.append(f"  {pct:.0f}%", style=palette.severity(pct))
    projected = west.projected_pct(now)
    if projected is not None and projected - pct >= 1.0:
        text.append(f" →{projected:.0f}%", style=f"{palette.severity(projected)} dim")
    if west.rate_pct_per_h is None:
        text.append("  no trend yet", style=palette.muted)
    else:
        text.append(f"  +{west.rate_pct_per_h:.1f}%/h", style=palette.muted)
        if threshold is not None:
            eta = west.eta_s(threshold, now)
            if eta is None:
                text.append(f"  stays under {pct_label(threshold)}%", style=palette.muted)
            elif eta <= 0:
                text.append(f"  past {pct_label(threshold)}%", style=palette.sev_warn)
            else:
                text.append(
                    f"  {pct_label(threshold)}% in {data.format_duration(eta)}",
                    style=palette.foreground,
                )
    if west.resets_at is not None and west.resets_at > now:
        fmt = "%H:%M" if west.window == "5h" else "%a %H:%M"
        text.append(
            f"  resets {time.strftime(fmt, time.localtime(west.resets_at))}",
            style=palette.muted,
        )
    return text


def _side_by_side(left: list[Text], right: list[Text], col_w: int) -> list[Text]:
    out = []
    for i in range(max(len(left), len(right))):
        line = Text(no_wrap=True)
        if i < len(left):
            line.append(left[i])
        line.pad_right(col_w - line.cell_len + _SIDE_GAP)
        line.truncate(col_w + _SIDE_GAP)
        if i < len(right):
            line.append(right[i])
        out.append(line)
    return out


def _arrange(
    blocks: list[tuple[Text, object]], *, width: int, height: int
) -> list[Text]:
    """Lay out (title, render(width, plot_h) -> lines) blocks in ``height``.

    Two blocks go side by side on a wide pane, else they stack and share the
    height. Each block is its title line plus its plot.
    """
    if not blocks or height <= 0:
        return []
    if len(blocks) == 2 and width >= SIDE_BY_SIDE_MIN_WIDTH:
        col_w = (width - _SIDE_GAP) // 2
        plot_h = min(MAX_PLOT_LINES, height - 1)
        columns = [[title, *render(col_w, plot_h)] for title, render in blocks]
        return _side_by_side(columns[0], columns[1], col_w)[:height]
    each = height // len(blocks)
    lines: list[Text] = []
    for i, (title, render) in enumerate(blocks):
        share = height - each * (len(blocks) - 1) if i == len(blocks) - 1 else each
        plot_h = min(MAX_PLOT_LINES, share - 1)
        lines.append(title)
        if plot_h > 0:
            lines.extend(render(width, plot_h))
    return lines[:height]


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
    login_warn_days: int = 3,
) -> list[Text]:
    """The detail pane for one account, at most ``height`` lines: its 5h and
    7d graphs, then a sparkline per extra (per-model) window."""
    if acc is None or height <= 0:
        return []
    # On a short pane the highlighted row already names the account; the
    # graphs need the line more than a repeated title does.
    lines = [account_title(acc, palette)] if height >= TITLE_MIN_HEIGHT else []
    login = login_line(acc, now, login_warn_days, palette)
    if login is not None:
        lines.append(login)

    pcts = window_pcts(acc, models)
    plotted = {
        label: west
        for label in PLOTTED_WINDOWS
        if (west := _window_estimate(label, est, pcts.get(label), acc.usage.fetched_at))
        is not None
    }
    if not plotted:
        # No measurement at all (sentinel, api key, never fetched): the full
        # card carries the facts, so the pane is never empty.
        card = account_card_text(acc, width, threshold=threshold, now=now, palette=palette)
        lines.extend(card.split()[1:])
        lines.append(Text("no usage history yet: the graphs fill in as polls land", style=palette.muted))
        return lines[:height]

    extras = [label for label in pcts if label not in PLOTTED_WINDOWS]
    extras += [
        label for label in (est.windows if est else {}) if label not in PLOTTED_WINDOWS and label not in extras
    ]

    def renderer(west: WindowEstimate):
        history = [p for p in series.get(west.window, ()) if p.t <= now]

        def render(w: int, h: int) -> list[Text]:
            return line_plot(
                west, now=now, threshold=threshold, width=w, height=h,
                palette=palette, points=history or None,
            )

        return render

    blocks = [
        (
            plot_title(
                west, now=now, threshold=threshold, palette=palette,
                measured_pct=pcts[label][0] if label in pcts else None,
            ),
            renderer(west),
        )
        for label, west in plotted.items()
    ]
    body_h = height - len(lines) - len(extras)
    lines.extend(_arrange(blocks, width=width, height=body_h))
    for label in extras:
        lines.append(
            _window_summary(
                label, est.windows.get(label) if est else None, series.get(label, []),
                pcts.get(label), now=now, width=width, palette=palette,
            )
        )
    return lines[:height]


def long_term_lines(
    acc: AccountSnapshot | None,
    long_term: LongTerm | None,
    *,
    now: float,
    threshold: float | None,
    width: int,
    height: int,
    palette: Palette = Palette.DARK,
) -> list[Text]:
    """The long-term view: one column per past 5h window over the last week,
    and the 7d usage curve over up to 30 days (as far back as history goes)."""
    if acc is None or height <= 0:
        return []
    lines = [account_title(acc, palette, "history · h for current windows")]
    if long_term is None:
        lines.append(Text("loading history…", style=palette.muted))
        return lines[:height]
    if not long_term.peaks_5h and not long_term.series_7d:
        lines.append(Text("no past windows recorded yet", style=palette.muted))
        return lines[:height]

    x0_5h = now - LONG_5H_SPAN_S
    first_7d = long_term.series_7d[0].t if long_term.series_7d else now
    x0_7d = max(now - LONG_7D_SPAN_S, min(first_7d, now - _MIN_LONG_7D_SPAN_S))
    peaks = [p for r, p in long_term.peaks_5h if r > x0_5h]
    over = sum(1 for p in peaks if threshold is not None and p >= threshold)

    title_5h = Text(no_wrap=True, overflow="ellipsis")
    title_5h.append("5h peaks", style=f"bold {palette.accent}")
    title_5h.append(f"  last 7 days · {len(peaks)} windows", style=palette.muted)
    if threshold is not None and peaks:
        title_5h.append(f" · {over} past {pct_label(threshold)}%", style=palette.sev_warn if over else palette.muted)

    title_7d = Text(no_wrap=True, overflow="ellipsis")
    title_7d.append("7d usage", style=f"bold {palette.accent}")
    since = time.strftime("%b %d", time.localtime(x0_7d))
    title_7d.append(f"  since {since}", style=palette.muted)
    recent = [p.pct for p in long_term.series_7d if p.t >= x0_7d]
    if recent:
        top = max(recent)
        title_7d.append(f" · peak {top:.0f}%", style=palette.severity(top))

    blocks = [
        (
            title_5h,
            lambda w, h: peak_columns(
                long_term.peaks_5h, window_len_s=WINDOW_LEN_S["5h"], x0=x0_5h, x1=now,
                threshold=threshold, width=w, height=h, palette=palette,
            ),
        ),
        (
            title_7d,
            lambda w, h: history_plot(
                long_term.series_7d, x0=x0_7d, x1=now, threshold=threshold,
                width=w, height=h, palette=palette,
            ),
        ),
    ]
    lines.extend(_arrange(blocks, width=width, height=height - len(lines)))
    return lines[:height]


# ---------------------------------------------------------------------------
# Widgets and screen
# ---------------------------------------------------------------------------


def snapshot_key(snap: AccountsSnapshot) -> tuple:
    """What the page shows from a snapshot, minus the parts that change on
    every store read (``taken_at``, measurement ages)."""
    return tuple(
        (
            acc.number, acc.email, acc.alias, acc.disabled, acc.is_active,
            acc.display_tag, acc.login_expires_at, acc.usage.fetched_at,
            acc.usage.sentinel, repr(acc.usage.last_good), acc.usage.last_error,
        )
        for acc in snap.accounts
    )


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


ACTIONS: tuple[tuple[str, str, str, str], ...] = (
    # (button id, key glyph, word, screen action)
    ("act-switch", "⏎", "switch", "switch_selected"),
    ("act-login", "l", "login", "login_selected"),
    ("act-add", "a", "add", "app.add_current"),
    ("act-token", "t", "token", "app.add_token"),
    ("act-disable", "d", "disable", "toggle_selected"),
    ("act-remove", "x", "remove", "remove_selected"),
    ("act-history", "h", "history", "toggle_long"),
    ("act-statusline", "s", "statusline", "app.statusline"),
    ("act-config", "c", "config", "app.open_config"),
    ("act-quit", "q", "quit", "app.quit"),
)


def action_label(key: str, word: str) -> str:
    """Chip markup: the key in the accent, the word in the muted text color."""
    return f"[b $primary]{key}[/] [$secondary]{word}[/]"


# Chips hidden first when the bar does not fit: their keys need no reminder
# (enter, q) or are rare (t, s). Every key still works with its chip hidden.
OPTIONAL_ACTIONS = ("act-switch", "act-quit", "act-token", "act-statusline", "act-login")
_ACTIONS_PAD = 2  # #actions horizontal padding


def chip_width(key: str, word: str) -> int:
    """Cells one chip takes: "k word" + Button's 1-cell pad each side + margin."""
    return len(key) + 1 + len(word) + 3


def hidden_actions(width: int) -> set[str]:
    """Button ids to hide so the action bar fits in ``width`` cells."""
    widths = {bid: chip_width(key, word) for bid, key, word, _a in ACTIONS}
    used = _ACTIONS_PAD + sum(widths.values())
    hidden: set[str] = set()
    for button_id in OPTIONAL_ACTIONS:
        if used <= width:
            break
        hidden.add(button_id)
        used -= widths[button_id]
    return hidden


class MainScreen(Screen):
    """The only page besides Config: accounts, detail, auto status, actions."""

    BINDINGS = [
        Binding("l", "login_selected", "Log in", show=False),
        Binding("a", "app.add_current", "Add login", show=False),
        Binding("t", "app.add_token", "Add token", show=False),
        Binding("d", "toggle_selected", "Disable/enable", show=False),
        Binding("x,delete", "remove_selected", "Remove", show=False),
        Binding("c", "app.open_config", "Config", show=False),
        Binding("h", "toggle_long", "History", show=False),
        Binding("s", "app.statusline", "Statusline", show=False),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("q", "app.quit", "Quit", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    # Countdowns ("next 1m", "resets 2h 13m") are minute-grained, so one
    # coarse tick keeps them honest; everything else repaints on data change.
    CLOCK_TICK_S = 30.0

    app: "RotaApp"

    def compose(self) -> ComposeResult:
        yield _Painted("status_line", id="status")
        yield _Painted("header_line", id="columns")
        yield ListView(id="accounts")
        yield _Painted("detail_text", id="detail")
        # Hidden until the first event: an empty log would only be a blank band.
        log = RichLog(id="event-log", highlight=False, markup=False, wrap=False, max_lines=50)
        log.display = False
        yield log
        with Horizontal(id="actions"):
            for button_id, key, word, _action in ACTIONS:
                yield ActionButton(action_label(key, word), id=button_id)

    def on_mount(self) -> None:
        self.query_one("#accounts", ListView).focus()
        self._content_key: tuple | None = None
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "estimates", lambda _e: self.repaint())
        self.watch(self.app, "long_view", lambda _v: self.repaint())
        self.watch(self.app, "long_term", lambda _l: self.query_one("#detail").refresh())
        self.watch(self.app, "auto_status", lambda _s: self._repaint_status())
        self.watch(self.app, "feed_state", lambda _f: self._repaint_status())
        self.watch(self.app, "theme", lambda _t: self.repaint())
        self.set_interval(self.CLOCK_TICK_S, self.repaint)

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
        snap = self.app.snapshot
        return status_text(
            self.app.auto_status,
            now=time.time(),
            width=width,
            palette=self.palette,
            roster=roster_pips(snap.accounts, self.app.models) if snap else (),
            feed=self.app.feed_state or None,
        )

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
            login_warn_days=self.app.login_warn_days,
        )

    def detail_text(self, width: int, height: int) -> Text:
        number = self.selected_number
        acc = self.account(number)
        if self.app.long_view:
            lines = long_term_lines(
                acc,
                self.app.long_term.get(number) if number else None,
                now=time.time(),
                threshold=self.app.threshold_pct,
                width=width,
                height=height,
                palette=self.palette,
            )
            return Text("\n").join(lines)
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
            login_warn_days=self.app.login_warn_days,
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
        # The store poller hands over a new snapshot every few seconds even
        # when nothing moved; repaint only when something visible changed.
        key = snapshot_key(snap)
        if key == self._content_key:
            return
        self._content_key = key
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
        hidden = hidden_actions(event.size.width)
        for button_id in OPTIONAL_ACTIONS:
            self.query_one(f"#{button_id}").display = button_id not in hidden

    def on_list_view_highlighted(self, _event: ListView.Highlighted) -> None:
        self.query_one("#detail").refresh()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, AccountRow):
            self.app.do_switch(event.item.number)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        action = next((a for bid, _k, _w, a in ACTIONS if bid == event.button.id), None)
        if action is not None:
            await self.run_action(action)

    def write_event(self, event: AutoSwitchEvent) -> None:
        if event.kind in QUIET_KINDS:
            return
        log = self.query_one("#event-log", RichLog)
        log.display = True
        log.write(event_text(event, palette=self.palette))

    # -- actions ---------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()

    def action_toggle_long(self) -> None:
        self.app.toggle_long_view()

    def action_switch_selected(self) -> None:
        number = self.selected_number
        if number is not None:
            self.app.do_switch(number)

    def action_login_selected(self) -> None:
        """Log in: on a row whose login is dead, re-login that account (its
        address pre-filled); anywhere else, log in any account."""
        acc = self.account(self.selected_number)
        dead = acc is not None and acc.usage.sentinel == USAGE_RELOGIN_REQUIRED
        self.app.start_login(acc.email if dead else None)

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
    "LongTerm",
    "RosterPip",
    "long_term_lines",
    "roster_pips",
    "snapshot_key",
    "MainScreen",
    "QUIET_KINDS",
    "RowLayout",
    "account_row_text",
    "detail_lines",
    "event_text",
    "header_text",
    "status_text",
]
