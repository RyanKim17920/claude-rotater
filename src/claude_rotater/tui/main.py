"""The main page: an account table, the selected account's charts, the actions.

Layout, top to bottom, as tall as the terminal allows:

- header band: the ``rota`` chip, the auto-switch state, the statusline feed
  state, why a switch is held (only while one is), and the estimate legend;
- one ``AccountRow`` per account, measured now: on-duty bar, the account, a
  short tag only when it matters (a dead or lapsing login, "off", a real
  org), then per window (5h, 7d, each per-model window in use) its pct and a
  countdown to its reset, then one forecast cell for the binding window
  (``ok``, ``~7d limit in 15h``, ``~?``). Columns shrink as the width runs
  short (:data:`LEVELS`);
- the highlighted account's 5h and 7d charts (measured history solid, the
  projection dotted and dim, the threshold line, the reset at the right
  edge), filling the spare height and hidden when fewer than
  :data:`CHART_MIN_ROWS` rows are spare (the rows then get small meters).
  ``h`` swaps in the long-term view (past 5h peaks, the 7d curve across
  weeks);
- a 3-line log of recent non-routine engine events (hidden until the first);
- the action bar: the everyday keys and ``?`` for the rest.

One convention for estimates everywhere: measured values are plain, anything
extrapolated is dim and prefixed with ``~`` (:data:`ESTIMATE_MARK`), and an
estimate too thin to trust reads ``~?`` rather than a precise-looking number.
Each fact appears once: countdowns in the table, reset clocks on the charts.

The renderers below are plain functions of (snapshot row, estimate, now) so
they are tested without an app; the screen only wires them to app state.
Nothing here does I/O, and nothing animates: the screen repaints when the
app's data changes, plus one coarse clock tick for countdown text.
"""

from __future__ import annotations

import dataclasses
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
from claude_rotater.estimator import (
    WINDOW_LEN_S,
    AccountEstimate,
    WindowEstimate,
    estimate_window,
    is_relevant_window,
)
from claude_rotater.json_output import USAGE_API_KEY, USAGE_RELOGIN_REQUIRED
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.tui import data
from claude_rotater.tui.plot import (
    GUTTER,
    history_plot,
    line_plot,
    peak_columns,
    windows_per_bar,
)
from claude_rotater.tui.modals import KeysModal
from claude_rotater.tui.theme import Palette, brand_chip
from claude_rotater.tui.widgets import bar_cells
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
# Estimate styling: one convention for every extrapolated value
# ---------------------------------------------------------------------------

# Anything the estimator extrapolated (a projection, an ETA) is prefixed with
# this mark and drawn dim; measured values are plain. The header carries the
# legend once.
ESTIMATE_MARK = "~"
LEGEND = f"{ESTIMATE_MARK} = estimate"
UNSURE = f"{ESTIMATE_MARK}?"  # an estimate too thin to put a number on
_TRUSTED = ("medium", "high")  # confidences good enough for a number


def estimate_style(color: str) -> str:
    return f"{color} dim"


def rough_duration(seconds: float) -> str:
    """An estimated duration at the precision it deserves: ``40m``, ``15h``, ``3d``."""
    s = max(0.0, seconds)
    if s < 90 * 60:
        return f"{max(5, round(s / 300) * 5)}m"
    if s < 48 * 3600:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def countdown_text(seconds: float) -> str:
    """A measured countdown to a reset, at most 5 cells: ``40m``, ``1h20m``, ``23h``, ``2d``."""
    s = max(0, int(seconds))
    if s < 60:
        return "<1m"
    if s < 3600:
        return f"{s // 60}m"
    if s < 10 * 3600:
        h, m = divmod(s // 60, 60)
        return f"{h}h{m:02d}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoStatus:
    """What the header knows about the engine, folded from its events.

    ``last_switch_at`` may also come from the shared state file, so a switch
    made by another process (``rota auto``, ``rota switch``) still holds its
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
    last_reason: str = ""  # its machine reason ("cooldown", "no-candidates", "error", ...)


FEED_LABELS = {
    # state -> (glyph, palette role, words)
    "live": ("●", "sev_ok", "feed live"),
    "stale": ("◐", "sev_warn", "feed stale"),
    "off": ("○", "muted", "feed off"),
    "unset": ("○", "sev_warn", "feed: not set up"),
}

# Engine outcomes that mean a switch was wanted and could not happen.
BLOCKED_REASONS = frozenset(
    {"no-candidates", "no-viable-target", "no-qualifying-candidate", "error", "all-exhausted"}
)


def switch_note(status: AutoStatus, *, now: float, over_threshold: bool) -> str:
    """Why a switch is held, or "" when nothing is blocked.

    A cooldown only matters while the account on duty is at its threshold;
    the engine also reports one for a mere consume-first nudge."""
    reason = status.last_reason
    if reason == "cooldown":
        if not over_threshold or status.last_switch_at is None:
            return ""
        remaining = status.last_switch_at + status.cooldown_s - now
        return f"switch held: cooldown {data.format_duration(remaining)}" if remaining > 0 else ""
    return status.last_outcome if reason in BLOCKED_REASONS else ""


def status_text(
    status: AutoStatus,
    *,
    now: float,
    width: int,
    palette: Palette = Palette.DARK,
    feed: str | None = None,
    over_threshold: bool = False,
) -> Text:
    """`` rota   auto on   ● feed live   switch held: cooldown 4m      ~ = estimate``.

    Only the engine state, the feed state and, when a switch is blocked,
    why; the estimate legend sits at the right edge. The note goes first
    when the width runs short, then the legend."""
    head = Text(no_wrap=True, overflow="ellipsis")
    head.append(brand_chip(palette))
    head.append("  auto ", style=palette.muted)
    if status.running:
        head.append("on", style=f"bold {palette.accent}")
    else:
        head.append("off", style=f"bold {palette.sev_crit}")
    if feed in FEED_LABELS:
        glyph, role, words = FEED_LABELS[feed]
        head.append(f"   {glyph} ", style=getattr(palette, role))
        head.append(words, style=palette.muted)
    note = switch_note(status, now=now, over_threshold=over_threshold)
    legend_w = len(LEGEND) + 2
    if note and head.cell_len + 3 + len(note) + legend_w <= width:
        head.append(f"   {note}", style=palette.sev_warn)
    gap = width - head.cell_len - len(LEGEND)
    if gap >= 2:
        head.append(" " * gap)
        head.append(LEGEND, style=palette.muted)
    return head


# ---------------------------------------------------------------------------
# Account table: measured now, plus one forecast cell
# ---------------------------------------------------------------------------

ON_DUTY_MARK = "▌"
PRIMARY_WINDOWS = ("5h", "7d")
_PREFIX_W = 2  # "▌ "
_GAP = 2  # between table columns
_REL_W = 5  # "1h20m" (countdown_text)
_RESET_GLYPH = "↻"
_MAX_TAG_W = 14
_MIN_NAME_W = 8
_MAX_NAME_W = 32
_FORECAST_W = 20  # "~Fable limit in 15h"
_FORECAST_SHORT_W = 10  # "~7d in 15h"


@dataclass(frozen=True)
class Level:
    """How much one table row carries; :data:`LEVELS` goes richest first.

    ``bar_w`` meters only appear when the charts are hidden for height."""

    bar_w: int
    model_reset: bool  # per-model windows (Fable, ...) keep their countdown
    long_forecast: bool  # "~7d limit in 15h" rather than "~7d in 15h"
    reset: bool  # 5h/7d keep their countdown


LEVELS: tuple[Level, ...] = (
    Level(4, True, True, True),
    Level(0, True, True, True),
    Level(0, False, True, True),
    Level(0, False, False, True),
    Level(0, False, False, False),
)


@dataclass(frozen=True)
class RowLayout:
    """Column widths for one terminal width; every row shares it, so the
    windows line up without a header line."""

    width: int
    windows: tuple[str, ...]
    name_w: int
    tag_w: int
    level: Level

    @property
    def bar_w(self) -> int:
        return self.level.bar_w

    @property
    def forecast_w(self) -> int:
        return _FORECAST_W if self.level.long_forecast else _FORECAST_SHORT_W

    def has_reset(self, label: str) -> bool:
        return self.level.reset if label in PRIMARY_WINDOWS else self.level.model_reset

    def window_w(self, label: str) -> int:
        """``5h  58% ▆▆▆▆ ↻1h20m``: label, pct, optional meter, countdown."""
        w = len(label) + 5
        if self.bar_w:
            w += 1 + self.bar_w
        if self.has_reset(label):
            w += 2 + _REL_W  # " ↻1h20m"
        return w

    @classmethod
    def for_width(
        cls,
        width: int,
        windows: Sequence[str],
        *,
        name_need: int = _MAX_NAME_W,
        tag_need: int = 0,
        bars: bool = False,
    ) -> "RowLayout":
        windows = tuple(windows)
        name_need = max(_MIN_NAME_W, min(name_need, _MAX_NAME_W))
        tag_w = min(tag_need, _MAX_TAG_W)
        levels = [lv for lv in LEVELS if bars or not lv.bar_w]

        def room(level: Level) -> int:
            layout = cls(width, windows, 0, tag_w, level)
            return width - (
                _PREFIX_W
                + (tag_w + _GAP if tag_w else 0)
                + sum(layout.window_w(label) + _GAP for label in windows)
                + layout.forecast_w + _GAP
            )

        # A whole name beats meters, model countdowns and the long forecast;
        # the 5h/7d countdowns beat a whole name (it shrinks to a few cells
        # first); only past that do they go.
        for level in levels:
            if level.reset and room(level) >= name_need:
                return cls(width, windows, name_need, tag_w, level)
        leanest_with_reset = [lv for lv in levels if lv.reset][-1]
        for level in (leanest_with_reset, levels[-1]):
            if room(level) >= _MIN_NAME_W:
                return cls(width, windows, min(name_need, room(level)), tag_w, level)
        return cls(width, windows, _MIN_NAME_W, tag_w, levels[-1])


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "…"


def account_name(acc: AccountSnapshot) -> str:
    return f"{acc.alias} ({acc.email})" if acc.alias else acc.email


_SENTINEL_TAGS = {USAGE_RELOGIN_REQUIRED: "re-login"}


def row_tag(acc: AccountSnapshot, now: float, warn_days: int) -> tuple[str, bool]:
    """(tag, is_warning): a dead login, a login about to lapse, "off" for a
    disabled account, else a real org name. A personal account has no tag:
    its org is named after the email (see ``models.org_tag``)."""
    sentinel = acc.usage.sentinel
    if acc.disabled:
        return "off", False
    if sentinel is not None:
        return _SENTINEL_TAGS.get(sentinel, sentinel), sentinel != USAGE_API_KEY
    warning = login_warning(acc, now, warn_days)
    if warning is not None:
        return warning, True
    tag = acc.display_tag
    return ("" if tag == "personal" else tag), False


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


def window_pcts(acc: AccountSnapshot, models: Sequence[str]) -> dict[str, tuple[float, float | None]]:
    """label → (measured pct, resets_at epoch) for every window that gates the account."""
    return {
        label: (pct, data.iso_epoch(resets_at))
        for label, pct, resets_at in oauth.relevant_windows(acc.usage.last_good, models)
    }


def shown_windows(acc: AccountSnapshot) -> dict[str, tuple[float, float | None]]:
    """Every window the usage endpoint reported, per-model ones (Fable, ...)
    included whether or not the ``models`` setting gates on them."""
    return window_pcts(acc, ("all",))


def table_windows(accounts: Sequence[AccountSnapshot], models: Sequence[str]) -> tuple[str, ...]:
    """5h, 7d, then each per-model window that any account has used or that
    gates switching; an unused, non-gating one is only noise."""
    labels = list(PRIMARY_WINDOWS)
    for acc in accounts:
        if acc.usage.sentinel is not None:
            continue
        for label, (pct, _r) in shown_windows(acc).items():
            if label in labels:
                continue
            if pct > 0 or is_relevant_window(label, tuple(models)):
                labels.append(label)
    return tuple(labels)


def window_cell(
    label: str,
    measured: tuple[float, float | None] | None,
    layout: RowLayout,
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
    stale: bool = False,
) -> Text:
    """One window column, exactly ``layout.window_w(label)`` cells:
    ``5h 58% ↻1h20m``. All measured, so nothing here is dim; a stale
    reading is muted instead. An unused window drops its countdown."""
    width = layout.window_w(label)
    cell = Text(no_wrap=True)
    cell.append(label, style=palette.muted)
    if measured is None:
        cell.append("    ·", style=palette.track)
    else:
        pct, resets_at = measured
        cell.append(f" {pct:3.0f}%", style=palette.muted if stale else palette.severity(pct))
        if layout.bar_w:
            cell.append(" ")
            cell.append(bar_cells(pct, layout.bar_w, stale=stale, threshold=threshold, palette=palette))
        if layout.has_reset(label) and resets_at is not None and pct > 0:
            rel = countdown_text(resets_at - now) if resets_at > now else "now"
            cell.append(f" {_RESET_GLYPH}{rel}", style=palette.muted)
    cell.truncate(width)
    cell.pad_right(width - cell.cell_len)
    return cell


@dataclass(frozen=True)
class Forecast:
    """The one forecast cell: the binding window's outcome in words."""

    text: str
    short: str  # the same at the narrow width
    role: str  # "ok" | "warn" | "fg" | "muted"
    estimate: bool  # extrapolated: dim, and the text carries ESTIMATE_MARK


def forecast(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    *,
    threshold: float | None,
    now: float,
    models: Sequence[str] = (),
) -> Forecast | None:
    """``ok``, ``~7d limit in 15h``, ``5h at limit`` (measured) or ``~?``
    (not enough history to say). None when there is nothing to forecast."""
    if threshold is None or acc.disabled or acc.usage.sentinel is not None:
        return None
    pcts = window_pcts(acc, models)
    if not pcts:
        return None
    label, (pct, _r) = max(pcts.items(), key=lambda kv: kv[1][0])
    if pct >= threshold:
        return Forecast(f"{label} at limit", f"{label} limit", "warn", False)
    unsure = Forecast(UNSURE, UNSURE, "muted", True)
    if est is None or not est.windows:
        return unsure
    hits = []
    for west in est.windows.values():
        west = _at_measured_reset(west, pcts.get(west.window, (0.0, None))[1])
        eta = west.eta_s(threshold, now)
        if eta is not None:
            hits.append((eta, west))
    if hits:
        eta, west = min(hits, key=lambda h: h[0])
        if west.confidence not in _TRUSTED:
            return unsure
        mark, name = ESTIMATE_MARK, west.window
        if eta <= 0:
            return Forecast(f"{mark}{name} at limit", f"{mark}{name} limit", "warn", True)
        when = rough_duration(eta)
        role = "warn" if eta < 3600 else "fg"
        return Forecast(f"{mark}{name} limit in {when}", f"{mark}{name} in {when}", role, True)
    if any(w.rate_pct_per_h is None for w in est.windows.values()):
        return unsure
    return Forecast("ok", "ok", "ok", False)


def forecast_text(fc: Forecast | None, width: int, *, long: bool, palette: Palette) -> Text:
    if fc is None:
        return Text()
    color = {
        "ok": palette.sev_ok, "warn": palette.sev_warn, "muted": palette.muted,
    }.get(fc.role, palette.foreground)
    words = fc.text if long else fc.short
    return Text(_fit(words, width), style=estimate_style(color) if fc.estimate else color, no_wrap=True)


def account_row_text(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    layout: RowLayout,
    *,
    now: float,
    threshold: float | None,
    palette: Palette = Palette.DARK,
    login_warn_days: int = 3,
    models: Sequence[str] = (),
) -> Text:
    """One account on one line, laid out by ``layout``:
    ``▌ me@x.com  Acme  5h 58% ↻1h20m  7d 48% ↻23h  Fable 0%  ~7d limit in 15h``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    # The on-duty account carries a solid accent bar at the left edge.
    text.append(ON_DUTY_MARK + " " if acc.is_active else "  ", style=palette.accent)
    name_style = palette.muted if acc.disabled else palette.foreground
    text.append(_fit(account_name(acc), layout.name_w), style=f"bold {name_style}" if acc.is_active else name_style)
    if layout.tag_w:
        tag, warn = row_tag(acc, now, login_warn_days)
        text.append(" " * _GAP + _fit(tag, layout.tag_w), style=palette.sev_warn if warn else palette.muted)

    pcts = shown_windows(acc)
    if acc.usage.sentinel is not None:  # the tag says what is wrong
        text.rstrip()
        return text
    if not pcts:
        text.append(" " * _GAP + "usage unknown", style=palette.muted)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    for label in layout.windows:
        text.append(" " * _GAP)
        text.append(
            window_cell(
                label, pcts.get(label), layout, now=now, threshold=threshold, palette=palette, stale=stale
            )
        )
    fc = forecast(acc, est, threshold=threshold, now=now, models=models)
    if fc is not None:
        text.append(" " * _GAP)
        text.append(forecast_text(fc, layout.forecast_w, long=layout.level.long_forecast, palette=palette))
    text.rstrip()
    return text


# ---------------------------------------------------------------------------
# Charts: the selected account's 5h and 7d windows
# ---------------------------------------------------------------------------

# Below this many spare rows the charts are hidden (and the table gets its
# small meters back); above it they fill the spare height up to a cap.
CHART_MIN_ROWS = 8
CHART_MAX_PLOT = 14


def _measured_window(
    label: str,
    est: AccountEstimate | None,
    measured: tuple[float, float | None],
    measured_at: float | None,
    now: float,
    points: Sequence[Point] = (),
) -> WindowEstimate:
    """The window's estimate; for a window the ``models`` setting does not
    gate (so the account estimate skipped it), one from its own history; else
    a rate-less stand-in from the measurement (a window with no history still
    gets its chart)."""
    west = est.windows.get(label) if est is not None else None
    if west is None and points:
        west = estimate_window(points, window=label, now=now)
    if west is not None:
        return west
    pct, resets_at = measured
    t = measured_at if measured_at is not None else now
    return WindowEstimate(label, pct, t, resets_at, None, "none", (Point(t, pct, resets_at),))


def _at_measured_reset(west: WindowEstimate, resets_at: float | None) -> WindowEstimate:
    """``west`` bounded by the measured reset, the one the table counts down
    to (stored history may still carry an older one)."""
    return west if resets_at is None else dataclasses.replace(west, resets_at=resets_at)


def chart_title(
    west: WindowEstimate,
    measured_pct: float,
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
) -> Text:
    """``5h  ~81% at reset`` / ``7d  ~limit in 15h`` / ``5h  not enough data yet``.

    Only the forecast: the pct is in the table and the reset clock is the
    chart's right edge."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(west.window, style=f"bold {palette.accent}")
    mark = ESTIMATE_MARK
    rate = west.rate_pct_per_h
    if threshold is not None and measured_pct >= threshold:
        text.append("  at limit", style=palette.sev_warn)
    elif measured_pct <= 0 and not rate:
        text.append("  unused", style=palette.muted)
    elif rate is None or west.confidence not in _TRUSTED:
        text.append(f"  {UNSURE} not enough data yet", style=estimate_style(palette.muted))
    else:
        eta = west.eta_s(threshold, now) if threshold is not None else None
        if eta is not None:
            words = f"{mark}at limit" if eta <= 0 else f"{mark}limit in {rough_duration(eta)}"
            text.append(f"  {words}", style=estimate_style(palette.sev_warn))
        else:
            future = west.resets_at is not None and west.resets_at > now
            at_reset = west.projected_pct(west.resets_at - 1) if future else None
            if at_reset is not None:
                text.append(f"  {mark}{at_reset:.0f}% at reset", style=estimate_style(palette.severity(at_reset)))
    return text


def chart_blocks(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    series: dict[str, list[Point]],
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
) -> list[tuple[Text, object]]:
    """(title, renderer) for the 5h and 7d charts of the current windows.

    Measured history is solid; the projection is dotted and dim (the estimate
    style), and left out when the estimate is too thin to trust."""
    if acc.usage.sentinel is not None:
        return []
    pcts = window_pcts(acc, ())
    blocks = []
    for label in PRIMARY_WINDOWS:
        if label not in pcts:
            continue
        pct, resets_at = pcts[label]
        west = _measured_window(label, est, pcts[label], acc.usage.fetched_at, now)
        west = _at_measured_reset(west, resets_at)
        drawn = west if west.confidence in _TRUSTED else dataclasses.replace(west, rate_pct_per_h=None)
        history = [p for p in series.get(label, ()) if p.t <= now]

        def render(w: int, h: int, drawn=drawn, history=history) -> list[Text]:
            return line_plot(
                drawn, now=now, threshold=threshold, width=w, height=h,
                palette=palette, points=history or None,
            )

        title = chart_title(west, pct, now=now, threshold=threshold, palette=palette)
        blocks.append((title, render))
    return blocks


def chart_lines(
    acc: AccountSnapshot | None,
    est: AccountEstimate | None,
    series: dict[str, list[Point]],
    *,
    now: float,
    threshold: float | None,
    width: int,
    height: int,
    palette: Palette = Palette.DARK,
) -> list[Text]:
    """The charts in at most ``height`` lines, or none below :data:`CHART_MIN_ROWS`."""
    if acc is None or height < CHART_MIN_ROWS:
        return []
    blocks = chart_blocks(acc, est, series, now=now, threshold=threshold, palette=palette)
    return _arrange(blocks, width=width, height=height, max_plot=CHART_MAX_PLOT)


# ---------------------------------------------------------------------------
# History view (h): the long-term view
# ---------------------------------------------------------------------------

# Two graphs sit side by side from this pane width; below it they stack.
SIDE_BY_SIDE_MIN_WIDTH = 100
_SIDE_GAP = 3
# Graph heights are capped, so the history view is sized to what it shows
# and never turns into a wall on a tall terminal.
LONG_PLOT_MAX = 10
LONG_5H_SPAN_S = 7 * 86400.0
LONG_7D_SPAN_S = 30 * 86400.0
_MIN_LONG_7D_SPAN_S = 7 * 86400.0


@dataclass(frozen=True)
class LongTerm:
    """Past-window history for one account, as the long-term view draws it."""

    peaks_5h: tuple[tuple[float, float], ...] = ()  # (resets_at, peak pct)
    series_7d: tuple[Point, ...] = ()


def account_title(acc: AccountSnapshot, palette: Palette, suffix: str = "") -> Text:
    """``me@x.com  Acme  on duty`` above the graphs."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(account_name(acc), style=f"bold {palette.foreground}")
    tag = "disabled" if acc.disabled else acc.display_tag
    if tag != "personal":
        text.append(f"  {tag}", style=palette.muted)
    if acc.is_active:
        text.append("  on duty", style=palette.accent)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
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


def _block_width(width: int, n_blocks: int) -> int:
    """The width each of ``n_blocks`` graphs gets in a pane ``width`` wide."""
    if n_blocks == 2 and width >= SIDE_BY_SIDE_MIN_WIDTH:
        return (width - _SIDE_GAP) // 2
    return width


def _arrange(
    blocks: list[tuple[Text, object]], *, width: int, height: int, max_plot: int
) -> list[Text]:
    """Lay out (title, render(width, plot_h) -> lines) blocks in at most
    ``height`` lines, each plot at most ``max_plot`` rows.

    Two blocks go side by side on a wide pane, else they stack and share the
    height. Each block is its title line plus its plot.
    """
    if not blocks or height <= 0:
        return []
    if len(blocks) == 2 and width >= SIDE_BY_SIDE_MIN_WIDTH:
        col_w = _block_width(width, 2)
        plot_h = min(max_plot, height - 1)
        columns = [[title, *render(col_w, plot_h)] if plot_h > 0 else [title] for title, render in blocks]
        return _side_by_side(columns[0], columns[1], col_w)[:height]
    each = height // len(blocks)
    lines: list[Text] = []
    for i, (title, render) in enumerate(blocks):
        share = height - each * (len(blocks) - 1) if i == len(blocks) - 1 else each
        plot_h = min(max_plot, share - 1)
        lines.append(title)
        if plot_h > 0:
            lines.extend(render(width, plot_h))
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
    title: bool = True,
    max_plot: int = LONG_PLOT_MAX,
) -> list[Text]:
    """The long-term view: one column per past 5h window over the last week,
    and the 7d usage curve over up to 30 days (as far back as history goes)."""
    if acc is None or height <= 0:
        return []
    lines = [account_title(acc, palette, "history · h for the charts")] if title else []
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
    # Too narrow for a bar per window: say how many windows each bar holds.
    per_bar = windows_per_bar(
        window_len_s=WINDOW_LEN_S["5h"], span_s=LONG_5H_SPAN_S, cols=_block_width(width, 2) - GUTTER
    )
    if per_bar > 1:
        title_5h.append(f" · {per_bar}/bar", style=palette.muted)

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
    lines.extend(_arrange(blocks, width=width, height=height - len(lines), max_plot=max_plot))
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
        width = self.size.width or max(1, screen.size.width - 2)
        return getattr(screen, self._painter)(width, self.size.height or 1)


class ActionButton(Button):
    """Mouse-clickable action chip that never takes focus: the account list
    keeps the keyboard, so arrows/enter always mean the list."""

    can_focus = False


ACTIONS: tuple[tuple[str, str, str, str], ...] = (
    # (button id, key glyph, word, screen action): the everyday keys only
    ("act-switch", "⏎", "switch", "switch_selected"),
    ("act-login", "l", "login", "login_selected"),
    ("act-config", "c", "config", "app.open_config"),
    ("act-history", "h", "history", "toggle_long"),
    ("act-help", "?", "more", "help"),
)

# The rest, listed by ``?``; every key works directly from the main page.
MORE_KEYS: tuple[tuple[str, str], ...] = (
    ("a", "add the current Claude Code login"),
    ("t", "add a setup-token or API key"),
    ("d", "disable / enable the highlighted account"),
    ("x", "remove the highlighted account"),
    ("s", "set up the statusline feed"),
    ("f", "refresh usage now"),
    ("ctrl+t", "theme"),
    ("q", "quit"),
)


def action_label(key: str, word: str) -> str:
    """Chip markup: the key in the accent, the word in the muted text color."""
    return f"[b $primary]{key}[/] [$secondary]{word}[/]"


class MainScreen(Screen):
    """The only page besides Config: header, account table, charts, actions."""

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
        Binding("question_mark", "help", "More keys", show=False),
        Binding("q", "app.quit", "Quit", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    # Countdowns ("↻1h20m") are minute-grained, so one coarse tick keeps
    # them honest; everything else repaints on data change.
    CLOCK_TICK_S = 30.0

    app: "RotaApp"

    def compose(self) -> ComposeResult:
        yield _Painted("status_line", id="status")
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
        self.watch(self.app, "long_term", lambda _l: self._repaint_detail())
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

    def layout_for(self, width: int) -> RowLayout:
        snap = self.app.snapshot
        accounts = snap.accounts if snap else ()
        now = time.time()
        names = [len(account_name(acc)) for acc in accounts]
        tags = [len(row_tag(acc, now, self.app.login_warn_days)[0]) for acc in accounts]
        return RowLayout.for_width(
            width,
            table_windows(accounts, self.app.models),
            name_need=max(names, default=_MIN_NAME_W),
            tag_need=max(tags, default=0),
            bars=not self.charts_fit(),
        )

    def detail_budget(self) -> int:
        """Lines the pane under the table may take so the page fits the
        terminal: the screen minus the header, table, event log and key strip."""
        listview = self.query_one("#accounts", ListView)
        rows = listview.outer_size.height or len(listview.children)
        log = self.query_one("#event-log")
        used = (
            1  # status band
            + 1  # table margin
            + rows
            + 1  # detail margin
            + 2  # key strip + its margin
            + (log.outer_size.height or 3 if log.display else 0)
        )
        return max(0, self.size.height - used)

    def charts_fit(self) -> bool:
        return self.detail_budget() >= CHART_MIN_ROWS

    # -- painters (called from widget render) ---------------------------------

    def status_line(self, width: int, _height: int) -> Text:
        snap = self.app.snapshot
        threshold = self.app.threshold_pct
        active = next((a for a in snap.accounts if a.is_active), None) if snap else None
        pcts = [pct for pct, _r in window_pcts(active, self.app.models).values()] if active else []
        return status_text(
            self.app.auto_status,
            now=time.time(),
            width=width,
            palette=self.palette,
            feed=self.app.feed_state or None,
            over_threshold=threshold is not None and any(p >= threshold for p in pcts),
        )

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
            palette=self.palette,
            login_warn_days=self.app.login_warn_days,
            models=self.app.models,
        )

    def detail_text(self, width: int, _height: int) -> Text:
        snap = self.app.snapshot
        if snap is None:
            return Text("loading…", style=self.palette.muted)
        if not snap.accounts:
            return Text(
                "No managed accounts yet. Press a to add your current Claude Code "
                "login, or t to add a setup-token / API key.",
                style=self.palette.muted,
            )
        number = self.selected_number
        acc = self.account(number)
        budget = self.detail_budget()
        if self.app.long_view:
            lines = long_term_lines(
                acc,
                self.app.long_term.get(number) if number else None,
                now=time.time(),
                threshold=self.app.threshold_pct,
                width=width,
                height=budget,
                palette=self.palette,
            )
        else:
            lines = chart_lines(
                acc,
                self.app.estimates.get(number) if number else None,
                self.app.series.get(number, {}) if number else {},
                now=time.time(),
                threshold=self.app.threshold_pct,
                width=width,
                height=budget,
                palette=self.palette,
            )
        return Text("\n").join(lines)

    # -- refresh ---------------------------------------------------------------

    def repaint(self) -> None:
        self.query_one("#status").refresh()
        self._repaint_detail()
        for row in self.query(AccountRow):
            row.repaint()

    def _repaint_detail(self) -> None:
        # The pane is as tall as its content, so a repaint may resize it.
        self.query_one("#detail").refresh(layout=True)

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

    def on_resize(self, _event) -> None:
        # Whether the charts fit decides whether the rows carry meters.
        self.repaint()

    def on_list_view_highlighted(self, _event: ListView.Highlighted) -> None:
        self._repaint_detail()

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
        self.repaint()  # the log took rows from the charts

    # -- actions ---------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()

    def action_toggle_long(self) -> None:
        self.app.toggle_long_view()

    def action_help(self) -> None:
        self.app.push_screen(KeysModal(MORE_KEYS))

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
    "MainScreen",
    "QUIET_KINDS",
    "RowLayout",
    "account_row_text",
    "chart_lines",
    "event_text",
    "forecast",
    "long_term_lines",
    "snapshot_key",
    "status_text",
]
