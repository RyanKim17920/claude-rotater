"""The main page: a dense account table, a detail strip, the actions.

Layout, top to bottom, as tall as its content (no plot fills the screen):

- header band: the ``rota`` chip, one roster pip per account (who is on
  duty, how much headroom each has), the auto-switch state, the statusline
  feed state and rota's own last switch;
- column header + one ``AccountRow`` per account: on-duty bar, the account
  (email once; a tag only for a real org, a disabled account or a login about
  to lapse), then per window (5h, 7d, each per-model window such as Fable) the
  pct, a small meter and the reset as a countdown and a clock, then the burn
  rate and ETA to the threshold of the binding window and a 5h sparkline.
  Columns drop by priority as the width runs short (:data:`LEVELS`);
- detail strip for the highlighted account, 2-4 lines: login end, token
  state and measurement age, then one aligned line per window with its burn,
  ETA, projection at the reset and the reset itself. ``h`` swaps in the
  history view: the current 5h/7d graphs and the long-term view (past 5h
  peaks, the 7d curve across weeks), with capped graph heights;
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
from claude_rotater.estimator import WINDOW_LEN_S, AccountEstimate, WindowEstimate, estimate_window
from claude_rotater.json_output import USAGE_API_KEY, USAGE_RELOGIN_REQUIRED
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.tui import data
from claude_rotater.tui.plot import (
    GUTTER,
    history_plot,
    line_plot,
    peak_columns,
    sparkline,
    windows_per_bar,
)
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
    "unset": ("○", "sev_warn", "feed: not set up"),
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
# Account table
# ---------------------------------------------------------------------------

# Shorter than the headless note: in the TUI the fix is one key away.
RELOGIN_ROW_LABEL = "re-login needed — press l to log in again"
_CONFIDENCE_GLYPH = {"high": " ", "medium": "~", "low": "?"}
ON_DUTY_MARK = "▌"
PRIMARY_WINDOWS = ("5h", "7d")
_PREFIX_W = 4  # "▌ 1 "
_GAP = 2  # between table columns
_REL_W = 6  # "23h59m" (data.compact_duration)
_BURN_W = 8  # "+12.0%/h"
_ETA_W = 7  # "13h05m~"
_MAX_TAG_W = 16
_MIN_NAME_W = 16
_MAX_NAME_W = 32
RESET_GLYPH_SCOPED = "↻"


@dataclass(frozen=True)
class Level:
    """How much one table row carries; :data:`LEVELS` goes richest first.

    ``clock`` is the reset clock of the 5h/7d columns: "full" ("20:23",
    "Mon 17:49"), "short" ("20:23", "Mon") or "" (none). Per-model columns
    (Fable, ...) get their own smaller bar and a weekday only.
    """

    bar_w: int
    rel: bool
    clock: str
    scoped_bar_w: int
    scoped_reset: bool
    spark_w: int
    burn: bool
    eta: bool
    tag: bool


# Dropped in this order as the width runs short: the 5h trend, the meters
# (to a 4-cell stub), the weekly clock (to a weekday), the clocks, burn, the
# tag, the reset countdowns, the ETA. The account name
# keeps at least _MIN_NAME_W cells at every level.
LEVELS: tuple[Level, ...] = (
    Level(8, True, "full", 6, True, 10, True, True, True),
    Level(6, True, "full", 4, True, 8, True, True, True),
    Level(5, True, "full", 0, True, 6, True, True, True),
    Level(4, True, "full", 0, True, 0, True, True, True),
    Level(4, True, "short", 0, True, 0, True, True, True),
    Level(0, True, "short", 0, True, 0, True, True, True),
    Level(4, True, "", 0, False, 0, True, True, True),
    Level(0, True, "", 0, False, 0, True, True, True),
    Level(0, True, "", 0, False, 0, False, True, True),
    Level(0, True, "", 0, False, 0, False, True, False),
    Level(0, False, "", 0, False, 0, False, True, False),
    Level(0, False, "", 0, False, 0, False, False, False),
)


def _clock_w(label: str, clock: str) -> int:
    if not clock:
        return 0
    if label == "5h":
        return 5  # "20:23"
    return 9 if clock == "full" else 3  # "Mon 17:49" / "Mon"


@dataclass(frozen=True)
class RowLayout:
    """Column widths for one terminal width; header and rows share it."""

    width: int
    windows: tuple[str, ...]
    name_w: int
    tag_w: int
    level: Level

    @property
    def bar_w(self) -> int:
        return self.level.bar_w

    def window_bar_w(self, label: str) -> int:
        return self.level.bar_w if label in PRIMARY_WINDOWS else self.level.scoped_bar_w

    def window_w(self, label: str) -> int:
        """``62% ▆▆▆▆ 1h59m 20:23`` (5h/7d) or ``34% ▆▆ ↻Mon`` (per model)."""
        bar = self.window_bar_w(label)
        w = 4 + (1 + bar if bar else 0)
        if label in PRIMARY_WINDOWS:
            if self.level.rel:
                w += 1 + _REL_W
            clock = _clock_w(label, self.level.clock)
            if clock:
                w += 1 + clock
        elif self.level.scoped_reset:
            w += 5  # " ↻Mon"
        return max(w, len(label))  # the header names the column in full

    @classmethod
    def for_width(
        cls,
        width: int,
        windows: Sequence[str],
        *,
        name_need: int = _MAX_NAME_W,
        tag_need: int = 0,
    ) -> "RowLayout":
        windows = tuple(windows)
        name_need = max(_MIN_NAME_W, min(name_need, _MAX_NAME_W))
        tag_need = min(tag_need, _MAX_TAG_W)
        for level in LEVELS:
            tag_w = tag_need if level.tag else 0
            layout = cls(width, windows, 0, tag_w, level)
            fixed = (
                _PREFIX_W
                + (tag_w + _GAP if tag_w else 0)
                + sum(layout.window_w(label) + _GAP for label in windows)
                + (_BURN_W + _GAP if level.burn else 0)
                + (_ETA_W + _GAP if level.eta else 0)
                + (level.spark_w + _GAP if level.spark_w else 0)
            )
            if width - fixed >= _MIN_NAME_W:
                return cls(width, windows, min(name_need, width - fixed), tag_w, level)
        return cls(width, windows, _MIN_NAME_W, 0, LEVELS[-1])


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "…"


def account_name(acc: AccountSnapshot) -> str:
    return f"{acc.alias} ({acc.email})" if acc.alias else acc.email


def row_tag(acc: AccountSnapshot, now: float, warn_days: int) -> tuple[str, bool]:
    """(tag, is_warning) for the tag column: a login about to lapse, then
    "disabled", then a real org name. A personal account has no tag: its
    org is named after the email (see ``models.org_tag``)."""
    warning = login_warning(acc, now, warn_days)
    if warning is not None and not acc.disabled:
        return warning, True
    if acc.disabled:
        return "disabled", False
    tag = acc.display_tag
    return ("" if tag == "personal" else tag), False


def header_text(layout: RowLayout, *, threshold: float | None, palette: Palette = Palette.DARK) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis", style=palette.muted)
    text.append(" " * _PREFIX_W)
    text.append(_fit("account", layout.name_w))
    if layout.tag_w:
        text.append(" " * (_GAP + layout.tag_w))
    for label in layout.windows:
        text.append(" " * _GAP + _fit(label, layout.window_w(label)))
    if layout.level.burn:
        text.append(" " * _GAP + _fit("burn", _BURN_W))
    if layout.level.eta:
        eta = "ETA"
        if threshold is not None:
            eta = f"ETA→{pct_label(threshold)}%"
            eta = eta if len(eta) <= _ETA_W else eta[3:]  # "→99.9%"
        text.append(" " * _GAP + _fit(eta, _ETA_W))
    if layout.level.spark_w:
        label = "5h trend" if layout.level.spark_w >= 8 else "trend"
        text.append(" " * _GAP + _fit(label, layout.level.spark_w))
    text.rstrip()
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
    return f"{data.compact_duration(eta)}{glyph}".rstrip(), "fg"


def burn_cell(est: AccountEstimate | None) -> str:
    binding = est.binding if est is not None else None
    if binding is None or binding.rate_pct_per_h is None:
        return "—"
    if binding.rate_pct_per_h < 0.05:
        return "0"
    return f"+{binding.rate_pct_per_h:.1f}%/h"


def window_len_s(label: str) -> float:
    return WINDOW_LEN_S.get(label, WINDOW_LEN_S["7d"])  # per-model windows are weekly


def current_points(
    points: Sequence[Point], label: str, resets_at: float | None, now: float
) -> tuple[list[Point], tuple[float, float]]:
    """The points of the window running now, and the (start, now) span a
    sparkline of it covers."""
    start = (resets_at - window_len_s(label)) if resets_at is not None else now - window_len_s(label)
    return [p for p in points if start <= p.t <= now], (start, now)


def reset_clock_text(label: str, resets_at: float, style: str = "full") -> str:
    """``20:23`` for the 5h window; ``Mon 17:49`` (or ``Mon``) for weekly ones."""
    stamp = time.localtime(resets_at)
    if label == "5h":
        return time.strftime("%H:%M", stamp)
    return time.strftime("%a %H:%M" if style == "full" else "%a", stamp)


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
    """One window column, exactly ``layout.window_w(label)`` cells."""
    width = layout.window_w(label)
    cell = Text(no_wrap=True)
    if measured is None:
        cell.append("  ·", style=palette.track)
    else:
        pct, resets_at = measured
        color = palette.severity(pct)
        cell.append(f"{pct:3.0f}%", style=f"{color} dim" if stale else color)
        bar = layout.window_bar_w(label)
        if bar:
            cell.append(" ")
            cell.append(bar_cells(pct, bar, stale=stale, threshold=threshold, palette=palette))
        future = resets_at is not None and resets_at > now
        if label in PRIMARY_WINDOWS:
            if layout.level.rel:
                rel = "" if resets_at is None else (data.compact_duration(resets_at - now) if future else "now")
                cell.append(" " + _fit(rel, _REL_W), style=palette.muted)
            if layout.level.clock and future:
                cell.append(" " + reset_clock_text(label, resets_at, layout.level.clock), style=palette.muted)
        elif layout.level.scoped_reset and future:
            cell.append(f" {RESET_GLYPH_SCOPED}{reset_clock_text(label, resets_at, 'short')}", style=palette.muted)
    cell.truncate(width)
    cell.pad_right(width - cell.cell_len)
    return cell


def account_row_text(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    layout: RowLayout,
    *,
    now: float,
    threshold: float | None,
    palette: Palette = Palette.DARK,
    login_warn_days: int = 3,
    series: dict[str, list[Point]] | None = None,
) -> Text:
    """One account on one line, laid out by ``layout``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    # The on-duty account carries a solid accent bar at the left edge.
    text.append(ON_DUTY_MARK if acc.is_active else " ", style=palette.accent)
    text.append(f"{acc.number:>2} ", style=f"bold {palette.accent}" if acc.is_active else palette.muted)
    name_style = palette.muted if acc.disabled else palette.foreground
    text.append(_fit(account_name(acc), layout.name_w), style=f"bold {name_style}" if acc.is_active else name_style)
    if layout.tag_w:
        tag, warn = row_tag(acc, now, login_warn_days)
        text.append(" " * _GAP + _fit(tag, layout.tag_w), style=palette.sev_warn if warn else palette.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        label = RELOGIN_ROW_LABEL if sentinel == USAGE_RELOGIN_REQUIRED else data.sentinel_label(sentinel)
        text.append(" " * _GAP + label, style=style)
        return text
    pcts = shown_windows(acc)
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
    if layout.level.burn:
        text.append(" " * _GAP + _fit(burn_cell(est), _BURN_W), style=palette.muted)
    if layout.level.eta:
        eta, role = eta_cell(est, threshold, now)
        eta_style = {"muted": palette.muted, "warn": palette.sev_warn}.get(role, palette.foreground)
        text.append(" " * _GAP + _fit(eta, _ETA_W), style=eta_style)
    if layout.level.spark_w and "5h" in pcts:
        points, span = current_points((series or {}).get("5h", ()), "5h", pcts["5h"][1], now)
        text.append(" " * _GAP)
        text.append(sparkline(points, layout.level.spark_w, palette=palette, span=span))
    text.rstrip()
    return text


# ---------------------------------------------------------------------------
# Detail strip: the selected account's facts, 2-4 lines
# ---------------------------------------------------------------------------

STRIP_MAX_LINES = 4


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
    gets its line and its graph)."""
    west = est.windows.get(label) if est is not None else None
    if west is None and points:
        west = estimate_window(points, window=label, now=now)
    if west is not None:
        return west
    pct, resets_at = measured
    t = measured_at if measured_at is not None else now
    return WindowEstimate(label, pct, t, resets_at, None, "none", (Point(t, pct, resets_at),))


def _facts_line(head: Text, facts: Sequence[tuple[str, str]], width: int) -> Text:
    """``head`` then `` · ``-joined (text, style) facts, dropping trailing
    facts that do not fit (the first facts matter most)."""
    line = head.copy()
    for i, (fact, style) in enumerate(facts):
        sep = "  " if i == 0 else " · "
        if line.cell_len + len(sep) + len(fact) > width:
            break
        line.append(sep, style="")
        line.append(fact, style=style)
    line.truncate(width, overflow="ellipsis")
    return line


def strip_title(
    acc: AccountSnapshot,
    *,
    now: float,
    width: int,
    palette: Palette,
    login_warn_days: int = 3,
    feed: str | None = None,
) -> Text:
    """``me@x.com  Acme  on duty  login ends Sun 01 Nov (15d 9h) · token ok ·
    measured 2m ago · feed live``."""
    head = Text(no_wrap=True)
    head.append(account_name(acc), style=f"bold {palette.foreground}")
    tag, _warn = row_tag(acc, now, 0)
    if tag:
        head.append(f"  {tag}", style=palette.muted)
    if acc.is_active:
        head.append("  on duty", style=palette.accent)
    facts: list[tuple[str, str]] = []
    sentinel = acc.usage.sentinel
    if acc.login_expires_at is not None and sentinel != USAGE_RELOGIN_REQUIRED:
        remaining = acc.login_expires_at - now
        when = time.strftime("%a %d %b", time.localtime(acc.login_expires_at))
        if remaining <= 0:
            facts.append(("login ended: press l", palette.sev_warn))
        elif login_warning(acc, now, login_warn_days) is not None:
            facts.append((f"login ends {when} ({data.compact_duration(remaining)}): press l", palette.sev_warn))
        else:
            facts.append((f"login ends {when} ({data.format_duration(remaining)})", palette.muted))
    if sentinel is None:
        if acc.usage.last_error:
            facts.append((f"last poll failed ({acc.usage.last_error})", palette.sev_warn))
        else:
            facts.append(("api key" if acc.kind == "api_key" else "token ok", palette.muted))
    if acc.usage.fetched_at is not None:
        age = max(0.0, now - acc.usage.fetched_at)
        stale = age > STALE_OK_S
        facts.append((f"measured {data.format_duration(age)} ago", palette.sev_warn if stale else palette.muted))
    if acc.is_active and feed in FEED_LABELS:
        _glyph, role, words = FEED_LABELS[feed]
        facts.append((words, getattr(palette, role) if role != "muted" else palette.muted))
    return _facts_line(head, facts, width)


def _sentinel_line(acc: AccountSnapshot, palette: Palette) -> Text | None:
    sentinel = acc.usage.sentinel
    if sentinel is None:
        return None
    if sentinel == USAGE_RELOGIN_REQUIRED:
        msg = (
            f"re-login needed: press l and log in as {acc.email} "
            "(the login page opens pre-filled; the active account is untouched)"
        )
        return Text(msg, style=palette.sev_warn, no_wrap=True, overflow="ellipsis")
    style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
    return Text(data.sentinel_label(sentinel), style=style, no_wrap=True, overflow="ellipsis")


# Window-line segments: (name, width, drop rank). The highest rank goes first
# when the line is too wide; "reset" shrinks to its countdown before it goes.
_SEG_PROJ_W = 5  # " →64%"
_SEG_SPARK_W = 12
_SEG_BURN_W = 12  # "no trend yet"
_SEG_ETA_W = 17  # "stays under 99.9%"
_SEG_AT_RESET_W = 14  # "→100% at reset"
_SEG_RESET_FULL_W = 25  # "resets 2d23h · Mon 17:49"
_SEG_RESET_SHORT_W = 13  # "resets 2d23h"
_DROP_ORDER = ("spark", "proj", "at_reset", "eta", "reset_clock", "burn")


def _window_segments(width: int, label_w: int) -> set[str]:
    """Which optional segments a window line keeps at ``width``."""
    keep = {"spark", "proj", "at_reset", "eta", "reset_clock", "burn"}

    def used() -> int:
        w = label_w + 2 + 4
        w += _SEG_PROJ_W if "proj" in keep else 0
        w += (2 + _SEG_SPARK_W) if "spark" in keep else 0
        w += (2 + _SEG_BURN_W) if "burn" in keep else 0
        w += (2 + _SEG_ETA_W) if "eta" in keep else 0
        w += (2 + _SEG_AT_RESET_W) if "at_reset" in keep else 0
        w += 2 + (_SEG_RESET_FULL_W if "reset_clock" in keep else _SEG_RESET_SHORT_W)
        return w

    for name in _DROP_ORDER:
        if used() <= width:
            break
        keep.discard(name)
    return keep


def window_line(
    west: WindowEstimate,
    measured_pct: float,
    points: Sequence[Point],
    resets_at: float | None = None,
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
    label_w: int,
    keep: set[str],
) -> Text:
    """``5h     62% →64%  ▂▃▄▅▆▇  +12.0%/h  90% in 2h19m  →98% at reset  resets 1h59m · 20:49``.

    ``resets_at`` is the measured reset (the same one the table shows); the
    estimate's own is the fallback."""
    label = west.window
    if resets_at is None:
        resets_at = west.resets_at
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(label.ljust(label_w), style=f"bold {palette.accent}")
    text.append(f"  {measured_pct:3.0f}%", style=palette.severity(measured_pct))
    rate = west.rate_pct_per_h
    if "proj" in keep:
        projected = west.projected_pct(now)
        if projected is not None and projected - measured_pct >= 1.0:
            text.append(_fit(f" →{projected:.0f}%", _SEG_PROJ_W), style=f"{palette.severity(projected)} dim")
        else:
            text.append(" " * _SEG_PROJ_W)
    if "spark" in keep:
        pts, span = current_points(points, label, resets_at, now)
        text.append("  ")
        text.append(sparkline(pts, _SEG_SPARK_W, palette=palette, span=span))
    if "burn" in keep:
        if rate is None:
            burn = "no trend yet"
        elif rate < 0.05:
            burn = "flat"
        else:
            burn = f"+{rate:.1f}%/h"
        text.append("  " + _fit(burn, _SEG_BURN_W), style=palette.muted)
    if "eta" in keep:
        eta_text, eta_style = "", palette.muted
        if rate is not None and threshold is not None:
            eta = west.eta_s(threshold, now)
            thr = pct_label(threshold)
            if eta is None:
                eta_text = f"stays under {thr}%"
            elif eta <= 0:
                eta_text, eta_style = f"past {thr}%", palette.sev_warn
            else:
                eta_text, eta_style = f"{thr}% in {data.compact_duration(eta)}", palette.foreground
        text.append("  " + _fit(eta_text, _SEG_ETA_W), style=eta_style)
    future = resets_at is not None and resets_at > now
    if "at_reset" in keep:
        at_reset = west.projected_pct(resets_at - 1) if future and rate is not None else None
        if at_reset is not None:
            text.append("  " + _fit(f"→{at_reset:.0f}% at reset", _SEG_AT_RESET_W),
                        style=f"{palette.severity(at_reset)} dim")
        else:
            text.append("  " + " " * _SEG_AT_RESET_W)
    if resets_at is not None:
        if future:
            reset = f"resets {data.compact_duration(resets_at - now)}"
            if "reset_clock" in keep:
                reset += f" · {reset_clock_text(label, resets_at)}"
        else:
            reset = "reset, awaiting poll"
        text.append(f"  {reset}", style=palette.muted)
    text.rstrip()
    return text


def _compact_window(label: str, pct: float, resets_at: float | None, now: float, palette: Palette) -> Text:
    """``Opus 12% ↻Mon`` for windows past the strip's line budget."""
    text = Text(no_wrap=True)
    text.append(label, style=f"bold {palette.accent}")
    text.append(f" {pct:.0f}%", style=palette.severity(pct))
    if resets_at is not None and resets_at > now:
        text.append(f" {RESET_GLYPH_SCOPED}{reset_clock_text(label, resets_at, 'short')}", style=palette.muted)
    return text


def strip_lines(
    acc: AccountSnapshot | None,
    est: AccountEstimate | None,
    series: dict[str, list[Point]],
    *,
    now: float,
    threshold: float | None,
    width: int,
    palette: Palette = Palette.DARK,
    login_warn_days: int = 3,
    feed: str | None = None,
    max_lines: int = STRIP_MAX_LINES,
) -> list[Text]:
    """The selected account in at most ``max_lines`` lines: its facts, then
    one aligned line per usage window (5h, 7d, then each per-model window)."""
    if acc is None or max_lines <= 0:
        return []
    lines = [strip_title(acc, now=now, width=width, palette=palette,
                         login_warn_days=login_warn_days, feed=feed)]
    note = _sentinel_line(acc, palette)
    if note is not None:
        lines.append(note)
    pcts = shown_windows(acc)
    if not pcts and note is None:
        lines.append(Text("usage unknown: no measurement yet (f refreshes now)", style=palette.muted))
    room = max_lines - len(lines)
    labels = list(pcts)
    if room <= 0 or not labels:
        return lines[:max_lines]
    label_w = max(len(label) for label in labels)
    keep = _window_segments(width, label_w)
    full = labels if len(labels) <= room else labels[: room - 1]
    for label in full:
        pct, resets_at = pcts[label]
        west = _measured_window(label, est, (pct, resets_at), acc.usage.fetched_at, now, series.get(label, ()))
        lines.append(window_line(
            west, pct, series.get(label, ()), resets_at, now=now, threshold=threshold,
            palette=palette, label_w=label_w, keep=keep,
        ))
    rest = labels[len(full):]
    if rest:
        line = Text(no_wrap=True, overflow="ellipsis")
        for i, label in enumerate(rest):
            if i:
                line.append(" · ", style=palette.muted)
            line.append(_compact_window(label, *pcts[label], now, palette))
        line.truncate(width, overflow="ellipsis")
        lines.append(line)
    return lines[:max_lines]


# ---------------------------------------------------------------------------
# History view (h): the current windows' graphs and the long-term view
# ---------------------------------------------------------------------------

# Two graphs sit side by side from this pane width; below it they stack.
SIDE_BY_SIDE_MIN_WIDTH = 100
_SIDE_GAP = 3
# Graph heights are capped, so the history view is sized to what it shows
# and never turns into a wall on a tall terminal.
CURRENT_PLOT_MAX = 8
LONG_PLOT_MAX = 10
_MIN_PLOT = 4
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


def plot_title(
    west: WindowEstimate,
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
    measured_pct: float | None = None,
) -> Text:
    """``5h  62% →64%  +12.0%/h  90% in 2h19m  resets 16:40``.

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
                    f"  {pct_label(threshold)}% in {data.compact_duration(eta)}",
                    style=palette.foreground,
                )
    if west.resets_at is not None and west.resets_at > now:
        text.append(f"  resets {reset_clock_text(west.window, west.resets_at)}", style=palette.muted)
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


def _arrange_height(n_blocks: int, width: int, plot_h: int) -> int:
    """Lines ``n_blocks`` (title + plot) blocks take at ``plot_h``."""
    if n_blocks == 2 and width >= SIDE_BY_SIDE_MIN_WIDTH:
        return 1 + plot_h
    return n_blocks * (1 + plot_h)


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


def current_blocks(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    series: dict[str, list[Point]],
    *,
    now: float,
    threshold: float | None,
    palette: Palette,
) -> list[tuple[Text, object]]:
    """(title, renderer) for the 5h and 7d graphs of the current windows."""
    pcts = window_pcts(acc, ())
    blocks = []
    for label in PRIMARY_WINDOWS:
        if label not in pcts:
            continue
        west = _measured_window(label, est, pcts[label], acc.usage.fetched_at, now)
        history = [p for p in series.get(label, ()) if p.t <= now]

        def render(w: int, h: int, west=west, history=history) -> list[Text]:
            return line_plot(
                west, now=now, threshold=threshold, width=w, height=h,
                palette=palette, points=history or None,
            )

        title = plot_title(west, now=now, threshold=threshold, palette=palette, measured_pct=pcts[label][0])
        blocks.append((title, render))
    return blocks


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
    lines = [account_title(acc, palette, "history · h for the table")] if title else []
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


def history_lines(
    acc: AccountSnapshot | None,
    est: AccountEstimate | None,
    series: dict[str, list[Point]],
    long_term: LongTerm | None,
    *,
    now: float,
    threshold: float | None,
    width: int,
    height: int,
    palette: Palette = Palette.DARK,
) -> list[Text]:
    """The ``h`` view, at most ``height`` lines: the current 5h/7d graphs
    when there is room for them beside the long-term view, then the
    long-term view (past 5h peaks, the 7d curve across weeks)."""
    if acc is None or height <= 0:
        return []
    lines = [account_title(acc, palette, "history · h for the table")]
    room = height - 1
    blocks = current_blocks(acc, est, series, now=now, threshold=threshold, palette=palette)
    long_need = _arrange_height(2, width, _MIN_PLOT)
    if blocks and room - long_need >= _arrange_height(len(blocks), width, _MIN_PLOT):
        # Room for both: the current graphs take up to CURRENT_PLOT_MAX rows
        # and leave the long-term view at least its minimum.
        plot_h = CURRENT_PLOT_MAX
        while plot_h > _MIN_PLOT and room - _arrange_height(len(blocks), width, plot_h) < long_need:
            plot_h -= 1
        cur_h = _arrange_height(len(blocks), width, plot_h)
        lines.extend(_arrange(blocks, width=width, height=cur_h, max_plot=plot_h))
        room -= cur_h
    lines.extend(long_term_lines(
        acc, long_term, now=now, threshold=threshold, width=width, height=room,
        palette=palette, title=False,
    ))
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

    def window_labels(self) -> tuple[str, ...]:
        """5h, 7d, then every per-model window any account reports."""
        labels = list(PRIMARY_WINDOWS)
        snap = self.app.snapshot
        for acc in snap.accounts if snap else ():
            for label in shown_windows(acc):
                if label not in labels:
                    labels.append(label)
        return tuple(labels)

    def layout_for(self, width: int) -> RowLayout:
        snap = self.app.snapshot
        accounts = snap.accounts if snap else ()
        now = time.time()
        names = [len(account_name(acc)) for acc in accounts]
        tags = [len(row_tag(acc, now, self.app.login_warn_days)[0]) for acc in accounts]
        return RowLayout.for_width(
            width,
            self.window_labels(),
            name_need=max(names, default=_MIN_NAME_W),
            tag_need=max(tags, default=0),
        )

    def detail_budget(self) -> int:
        """Lines the detail pane may take so the page fits the terminal:
        the screen minus the header, table, event log and key strip."""
        listview = self.query_one("#accounts", ListView)
        rows = listview.outer_size.height or len(listview.children)
        log = self.query_one("#event-log")
        used = (
            1  # status band
            + 2  # column header + its margin
            + rows
            + 1  # detail margin
            + 2  # key strip + its margin
            + (log.outer_size.height or 3 if log.display else 0)
        )
        return max(0, self.size.height - used)

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
            palette=self.palette,
            login_warn_days=self.app.login_warn_days,
            series=self.app.series.get(number, {}),
        )

    def detail_text(self, width: int, _height: int) -> Text:
        number = self.selected_number
        acc = self.account(number)
        budget = self.detail_budget()
        est = self.app.estimates.get(number) if number else None
        series = self.app.series.get(number, {}) if number else {}
        if self.app.long_view:
            lines = history_lines(
                acc,
                est,
                series,
                self.app.long_term.get(number) if number else None,
                now=time.time(),
                threshold=self.app.threshold_pct,
                width=width,
                height=budget,
                palette=self.palette,
            )
        else:
            lines = strip_lines(
                acc,
                est,
                series,
                now=time.time(),
                threshold=self.app.threshold_pct,
                width=width,
                palette=self.palette,
                login_warn_days=self.app.login_warn_days,
                feed=self.app.feed_state or None,
                max_lines=min(STRIP_MAX_LINES, budget),
            )
        return Text("\n").join(lines)

    # -- refresh ---------------------------------------------------------------

    def repaint(self) -> None:
        for widget_id in ("#status", "#columns"):
            self.query_one(widget_id).refresh()
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

    def on_resize(self, event) -> None:
        hidden = hidden_actions(event.size.width)
        for button_id in OPTIONAL_ACTIONS:
            self.query_one(f"#{button_id}").display = button_id not in hidden

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
    "history_lines",
    "strip_lines",
    "event_text",
    "header_text",
    "status_text",
]
