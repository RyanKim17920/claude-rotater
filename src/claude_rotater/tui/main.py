"""The main page: an account table, the selected account's charts, the actions.

Layout, top to bottom, as tall as the terminal allows:

- header band: the ``rota`` chip, the auto-switch state, the statusline feed
  state and why a switch is held (only while one is);
- one ``AccountRow`` per account, measured now: "›" (and an accent band, bold
  name) on the row under the cursor, "●" and an accent name on the account
  on duty, the account, a
  short tag only when it matters (a dead or lapsing login, "off", a real
  org), then per window (5h, 7d, each per-model window in use) its pct, a
  short bar (measured fill, then a lighter tint out to the projection at
  the reset, coloured by its outcome) and a countdown to its
  reset, then one forecast cell for the binding window
  (``ok``, ``~7d limit in 15h``; blank while there is too little history).
  Columns give way as the width runs
  short (:data:`LEVELS`): bars, then the org, then countdowns past the 5h
  one, then per-model windows; a long email keeps its local part
  (``work@…``). A row is always one line, and the 5h reset always shows;
- a line naming the highlighted account, then its 5h and 7d charts (measured history solid, the
  projection dotted and coloured by outcome, the threshold line, the reset at the right
  edge), filling all the spare height and hidden when fewer than
  :func:`chart_min_rows` rows are spare (the rows then get the room).
  ``h`` swaps in the long-term view (past 5h peaks, the 7d curve across
  weeks);
- a 3-line log of recent non-routine engine events (hidden until the first);
- the prompt line (:mod:`claude_rotater.tui.prompt`): a short dim hint when
  idle; every question, text entry and result happens there, never in a
  popup.

``?`` swaps the charts for the list of keys, and ``c`` swaps the whole body
(table, charts, log) for the settings list; the header and the prompt line
stay put. Every key works straight from the page.

One convention for estimates everywhere: measured values are plain, anything
extrapolated is told apart by its look (``~`` prefix, a lighter tint in a
bar, a dotted line) and coloured by its outcome (green under, yellow close, red over the
threshold), and is said in words where it is shown ("est. ~86% at reset").
An estimate too thin to trust shows nothing rather than a precise-looking
number.
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
from textual.containers import Vertical
from textual.css.scalar import Scalar
from textual.screen import Screen
from textual.widgets import ListItem, ListView, RichLog, Static

from claude_rotater import oauth, profiles
from claude_rotater.autoswitch import (
    AutoSwitchEvent,
    Decision,
    NoSwitchEvent,
    SwitchEvent,
    decision_from_event,
    pct_label,
)
from claude_rotater.estimator import (
    WINDOW_LEN_S,
    AccountEstimate,
    WindowEstimate,
    estimate_window,
    is_relevant_window,
)
from claude_rotater.json_output import USAGE_API_KEY, USAGE_RELOGIN_REQUIRED
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.poll_policy import DEFAULT_CADENCE, PollCadence
from claude_rotater.tui import data
from claude_rotater.tui.plot import (
    GUTTER,
    history_plot,
    line_plot,
    peak_columns,
    windows_per_bar,
)
from claude_rotater.tui.config import HINT_KEYS as CONFIG_HINT_KEYS
from claude_rotater.tui.config import ConfigView
from claude_rotater.tui.prompt import PromptLine, hint_text
from claude_rotater.tui.theme import Palette, blend, brand_chip
from claude_rotater.tui.widgets import BAR_EIGHTHS, BAR_FULL, BAR_TICK
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

# Anything the estimator extrapolated (a projection, an ETA) is told apart by
# its look, never by greying: the "~" prefix on text, a tint in a bar,
# the dotted line in a chart. Its colour says the outcome (outcome_color):
# green stays comfortably under the threshold, yellow gets close, red
# crosses it before the reset. Each chart title says "est." in words.
ESTIMATE_MARK = "~"
ESTIMATE_WORD = "est."
NO_ESTIMATE = "—"  # a chart title with too little history to estimate
_TRUSTED = ("medium", "high")  # confidences good enough for a number


NEAR_PTS = 10.0  # a projection this close under the threshold is "close"


def outcome_color(projected: float | None, threshold: float | None, palette: Palette) -> str:
    """Green / yellow / red for where an estimate lands against the threshold."""
    if projected is None or threshold is None:
        return palette.muted
    if projected >= threshold:
        return palette.sev_crit
    if projected >= threshold - NEAR_PTS:
        return palette.sev_warn
    return palette.sev_ok


def rough_duration(seconds: float) -> str:
    """An estimated duration at the precision it deserves: ``40m``, ``15h``,
    ``2d3h`` (never ``47h``)."""
    s = max(0.0, seconds)
    if s < 90 * 60:
        return f"{max(5, round(s / 300) * 5)}m"
    hours = round(s / 3600)
    if hours < 24:
        return f"{hours}h"
    return data.compact_duration(hours * 3600)


def countdown_text(seconds: float) -> str:
    """A measured countdown, the one duration format of the page:
    ``47m``, ``3h20m``, ``2d3h`` (:func:`data.compact_duration`)."""
    return data.compact_duration(seconds)


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
    decision: Decision | None = None  # the engine's latest verdict, for the header


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
        return f"switch held: cooldown {data.compact_duration(remaining)}" if remaining > 0 else ""
    return status.last_outcome if reason in BLOCKED_REASONS else ""


def header_decision(event: AutoSwitchEvent) -> Decision | None:
    """The verdict the header shows for an event, or None to keep the last.

    Switches and the strategy's own "stay: ..." lines; a machine outcome
    ("no switch: cooldown") is the held-switch note's job, which shows it
    only when it actually holds something."""
    if isinstance(event, SwitchEvent) or (isinstance(event, NoSwitchEvent) and event.self_explained):
        return decision_from_event(event)
    return None


def switch_flash(event: SwitchEvent) -> str:
    """The prompt line's brief note for an engine switch: ``→ b@x.com: why``.

    The why is the strategy's reason without its own "switch → #2:" lead
    (the email already says where), else the trigger."""
    dest = (event.to_ref or {}).get("email") or f"#{(event.to_ref or {}).get('number', '?')}"
    lead, sep, why = event.reason.partition(": ")
    if not (sep and lead.startswith("switch")):
        why = event.reason
    return f"→ {dest}: {why or event.trigger}"


def same_verdict(new: Decision, old: Decision | None) -> bool:
    """Whether a decision repeats the one the header shows; its time alone
    does not count, so a steady verdict is not re-stored every tick."""
    return old is not None and (new.action, new.line, new.target) == (old.action, old.line, old.target)


DECISION_SEP = " · "
MIN_DECISION_W = 12  # narrower than "stay: reset…" says nothing; drop it


def status_text(
    status: AutoStatus,
    *,
    now: float,
    width: int,
    palette: Palette = Palette.DARK,
    feed: str | None = None,
    over_threshold: bool = False,
    profile: str | None = None,
) -> Text:
    """`` rota · work   auto on · stay: resets soonest (19h40m)   ● feed live``.

    The engine state with its latest verdict (dim, cut to fit), the feed
    state and, when a switch is blocked, why in place of the verdict; the
    note goes, then the verdict, when the width runs short."""
    head = Text(no_wrap=True, overflow="ellipsis")
    head.append(brand_chip(palette))
    if profile:  # a named profile, so it is obvious which one is open
        head.append(" · ", style=palette.muted)
        head.append(profile, style=f"bold {palette.accent}")
    head.append("  auto ", style=palette.muted)
    if status.running:
        head.append("on", style=f"bold {palette.accent}")
    else:
        head.append("off", style=f"bold {palette.sev_crit}")
    tail = Text(no_wrap=True)
    if feed in FEED_LABELS:
        glyph, role, words = FEED_LABELS[feed]
        tail.append(f"   {glyph} ", style=getattr(palette, role))
        tail.append(words, style=palette.muted)
    note = switch_note(status, now=now, over_threshold=over_threshold)
    if note:
        if head.cell_len + tail.cell_len + 3 + len(note) <= width:
            tail.append(f"   {note}", style=palette.sev_warn)
    elif status.running and status.decision is not None:
        # A held switch's note already says the latest verdict, louder.
        room = width - head.cell_len - tail.cell_len - len(DECISION_SEP)
        if room >= MIN_DECISION_W:
            line = Text(status.decision.line, style=palette.muted, no_wrap=True)
            line.truncate(room, overflow="ellipsis")
            head.append(DECISION_SEP, style=palette.muted)
            head.append_text(line)
    head.append_text(tail)
    return head


# ---------------------------------------------------------------------------
# Account table: measured now, plus one forecast cell
# ---------------------------------------------------------------------------

ON_DUTY_MARK = "●"
CURSOR_MARK = "›"
PRIMARY_WINDOWS = ("5h", "7d")
_PREFIX_W = 3  # "›● ": cursor, on duty, space
_GAP = 2  # between table columns
_REL_W = 6  # "23h59m" (countdown_text)
_RESET_GLYPH = "↻"
_MAX_TAG_W = 16  # "login ends 5h03m"
_MIN_NAME_W = 8
_MAX_NAME_W = 32
_FORECAST_W = 20  # "~Fable limit in 15h"
_FORECAST_SHORT_W = 10  # "~7d in 15h"


@dataclass(frozen=True)
class Level:
    """How much one table row carries; :data:`LEVELS` goes richest first.

    ``bar_w`` is the projection bar's width (0: no bar). The 5h countdown is
    on every level: it is the reset that matters soonest."""

    bar_w: int
    model_reset: bool  # per-model windows (Fable, ...) keep their countdown
    long_forecast: bool  # "~7d limit in 15h" rather than "~7d in 15h"
    reset: bool  # the 7d window keeps its countdown
    org: bool = True  # the org name sits in the tag column (warnings always do)
    models: bool = True  # per-model windows get a column at all
    forecast: bool = True  # the forecast cell


LEVELS: tuple[Level, ...] = (
    Level(10, True, True, True),
    Level(8, True, True, True),
    Level(8, False, True, True),
    Level(8, False, False, True),
    Level(6, False, False, True),
    Level(4, False, False, True),
    Level(0, False, False, True),
    Level(0, False, False, True, org=False),
    Level(0, False, False, False, org=False),
    Level(0, False, False, False, org=False, models=False),
    Level(0, False, False, False, org=False, models=False, forecast=False),
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
        if not self.level.forecast:
            return 0
        return _FORECAST_W if self.level.long_forecast else _FORECAST_SHORT_W

    def has_reset(self, label: str) -> bool:
        if label == "5h":
            return True
        return self.level.reset if label in PRIMARY_WINDOWS else self.level.model_reset

    def window_w(self, label: str) -> int:
        """``5h  58% ▆▆▆▆ ↻ 1h20m``: label, pct, optional meter, countdown."""
        w = len(label) + 5
        if self.bar_w:
            w += 1 + self.bar_w
        if self.has_reset(label):
            w += 3 + _REL_W  # " ↻ 1h20m"
        return w

    @classmethod
    def for_width(
        cls,
        width: int,
        windows: Sequence[str],
        *,
        name_need: int = _MAX_NAME_W,
        tag_need: int = 0,
        short_name_need: int | None = None,
        warn_tag_need: int | None = None,
    ) -> "RowLayout":
        """The richest level that fits ``width``.

        ``tag_need`` is the widest tag, ``warn_tag_need`` the widest one that
        is not an org name (a warning, "off", "stale 2h"): the org column goes
        first. Names stay whole while anything else can give way; past that
        they shrink to ``short_name_need`` (the local part, ``work@…``)
        before the forecast goes."""
        windows = tuple(windows)
        name_need = max(_MIN_NAME_W, min(name_need, _MAX_NAME_W))
        short_need = max(_MIN_NAME_W, min(short_name_need or name_need, name_need))
        warn_need = tag_need if warn_tag_need is None else min(warn_tag_need, tag_need)

        def build(level: Level, name_w: int) -> "RowLayout":
            shown = windows if level.models else tuple(w for w in windows if w in PRIMARY_WINDOWS)
            tag_w = min(tag_need if level.org else warn_need, _MAX_TAG_W)
            return cls(width, shown, name_w, tag_w, level)

        def room(level: Level) -> int:
            layout = build(level, 0)
            return width - (
                _PREFIX_W
                + (layout.tag_w + _GAP if layout.tag_w else 0)
                + sum(layout.window_w(label) + _GAP for label in layout.windows)
                + (layout.forecast_w + _GAP if layout.forecast_w else 0)
            )

        # A whole name beats bars, countdowns past the 5h one, the org and
        # the model columns; the forecast beats a whole name, which first
        # shrinks to its local part.
        for level in LEVELS:
            if level.forecast and room(level) >= name_need:
                return build(level, name_need)
        for level in LEVELS:
            if room(level) >= short_need:
                return build(level, min(name_need, room(level)))
        leanest = LEVELS[-1]
        return build(leanest, max(_MIN_NAME_W, min(name_need, room(leanest))))


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    return text[: max(0, width - 1)] + "…"


def fit_name(name: str, width: int) -> str:
    """``name`` in ``width`` cells; an email that does not fit keeps its
    local part (``work@…``), the part that tells accounts apart."""
    if len(name) > width and "@" in name:
        local = name.split("@", 1)[0].split("(")[-1].strip()
        if len(local) + 2 <= width:
            return f"{local}@…".ljust(width)
    return _fit(name, width)


def short_name_len(name: str) -> int:
    """Cells ``fit_name`` needs for the ``work@…`` form of ``name``."""
    if "@" not in name:
        return len(name)
    return min(len(name), len(name.split("@", 1)[0].split("(")[-1].strip()) + 2)


def name_color(acc: AccountSnapshot, palette: Palette) -> str:
    """The accent for the account on duty, muted when it is off."""
    if acc.disabled:
        return palette.muted
    return palette.accent if acc.is_active else palette.foreground


def account_name(acc: AccountSnapshot) -> str:
    return f"{acc.alias} ({acc.email})" if acc.alias else acc.email


_SENTINEL_TAGS = {USAGE_RELOGIN_REQUIRED: "re-login"}


# A reading is only flagged stale once it is overdue for its own cadence:
# idle accounts are polled every pollSpareSeconds and the active one leans on
# the feed, so a fixed limit would flag every idle account between polls.
# Display only: the auto-switch trust limit (STALE_OK_S) is unchanged.
OVERDUE_FACTOR = 1.5


def overdue_after(acc: AccountSnapshot, cadence: PollCadence = DEFAULT_CADENCE) -> float:
    """Seconds after which this account's reading is overdue: 1.5× the feed
    staleness for the active account, 1.5× the spare poll interval otherwise."""
    return OVERDUE_FACTOR * (cadence.feed_stale_s if acc.is_active else cadence.spare_s)


def stale_age(acc: AccountSnapshot, now: float, cadence: PollCadence = DEFAULT_CADENCE) -> float | None:
    """The reading's age when it is overdue (or aging after a failed poll), else None."""
    fetched = acc.usage.fetched_at
    if fetched is None:
        return None
    age = now - fetched
    if age > overdue_after(acc, cadence) or (acc.usage.last_error and age > STALE_OK_S):
        return age
    return None


def status_tag(
    acc: AccountSnapshot, now: float, warn_days: int, cadence: PollCadence = DEFAULT_CADENCE
) -> tuple[str, bool] | None:
    """(tag, is_warning) for a state worth a word: "off" for a disabled
    account, a dead login, a login about to lapse, "stale 2h" for an old
    measurement. None when the account is fine."""
    sentinel = acc.usage.sentinel
    if acc.disabled:
        return "off", False
    if sentinel is not None:
        return _SENTINEL_TAGS.get(sentinel, sentinel), sentinel != USAGE_API_KEY
    warning = login_warning(acc, now, warn_days)
    if warning is not None:
        return warning, True
    age = stale_age(acc, now, cadence)
    if age is not None:
        return f"stale {countdown_text(age)}", False
    return None


def row_tag(
    acc: AccountSnapshot, now: float, warn_days: int, cadence: PollCadence = DEFAULT_CADENCE
) -> tuple[str, bool]:
    """(tag, is_warning): the :func:`status_tag`, else a real org name. A
    personal account has no tag: its org is named after the email (see
    ``models.org_tag``)."""
    status = status_tag(acc, now, warn_days, cadence)
    if status is not None:
        return status
    tag = acc.display_tag
    return ("" if tag == "personal" else tag), False


def login_warning(acc: AccountSnapshot, now: float, warn_days: int) -> str | None:
    """Short tag when the stored login lapses within ``warn_days`` (or has).

    ``login ends 2d`` / ``login ends 5h03m`` / ``login ended``; None when the end is
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
        return f"login ends {int(remaining // 86400)}d"
    return f"login ends {data.compact_duration(remaining)}"


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
    projected: float | None = None,
) -> Text:
    """One window column, exactly ``layout.window_w(label)`` cells:
    ``5h  58% ███▌░░░▕ ↻ 1h20m``. The pct, the solid fill and the countdown
    are measured; the hatch is the projection to the reset, coloured by its
    outcome. An unused window drops its countdown; an absent one is blank. A stale reading keeps its
    colours: the row's tag says how old it is."""
    width = layout.window_w(label)
    cell = Text(no_wrap=True)
    if measured is not None:  # an absent window is blank, label and all
        cell.append(label, style=palette.muted)
        pct, resets_at = measured
        cell.append(f" {pct:3.0f}%", style=palette.severity(pct))
        if layout.bar_w:
            cell.append(" ")
            cell.append(projection_bar(
                pct, projected, layout.bar_w, threshold=threshold, palette=palette,
            ))
        if layout.has_reset(label) and resets_at is not None and pct > 0:
            rel = countdown_text(resets_at - now) if resets_at > now else "now"
            cell.append(f" {_RESET_GLYPH} {rel}", style=palette.muted)
    cell.truncate(width)
    cell.pad_right(width - cell.cell_len)
    return cell


# The projected part of a bar: a solid block in a tint of its outcome colour
# (half way to the track), readable at 8-10 cells where a ░ hatch was a blur.
PROJECTION_TINT = 0.5


def projection_bar(
    pct: float,
    projected: float | None,
    width: int,
    *,
    threshold: float | None,
    palette: Palette,
) -> Text:
    """``███▌██▕``: the measured pct as a solid fill, then a lighter tint out
    to ``projected`` (the estimate at the reset), green / yellow / red by
    where it lands (:func:`outcome_color`); the threshold tick (``▕``,
    warning colour) where nothing covers it."""
    text = Text(no_wrap=True)
    if width <= 0:
        return text
    track = f"on {palette.track}"
    eighths = round(min(max(pct, 0.0), 100.0) / 100.0 * width * 8)
    full, part = divmod(eighths, 8)
    measured_end = full + (1 if part else 0)
    proj_end = measured_end
    if projected is not None and projected > pct:
        proj_end = max(measured_end, round(min(projected, 100.0) / 100.0 * width))
    tick_at = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width) - 1))
    tint = blend(outcome_color(projected, threshold, palette), palette.track, PROJECTION_TINT)
    fill = f"{palette.severity(pct)} {track}"
    for i in range(width):
        if i < full:
            text.append(BAR_FULL, style=fill)
        elif i == full and part:
            text.append(BAR_EIGHTHS[part], style=fill)
        elif i < proj_end:
            text.append(BAR_FULL, style=f"{tint} {track}")
        elif i == tick_at:
            text.append(BAR_TICK, style=f"{palette.sev_warn} {track}")
        else:
            text.append(" ", style=track)
    return text


def projected_at_reset(west: WindowEstimate, resets_at: float | None, now: float) -> float | None:
    """Where a trusted estimate puts the window just before its measured
    reset; None when the estimate is too thin or the reset is unknown/past."""
    west = _at_measured_reset(west, resets_at)
    if west.confidence not in _TRUSTED or west.resets_at is None or west.resets_at <= now:
        return None
    return west.projected_pct(west.resets_at - 1)


@dataclass(frozen=True)
class Forecast:
    """The one forecast cell: the binding window's outcome in words."""

    text: str
    short: str  # the same at the narrow width
    role: str  # "ok" | "warn" | "crit" | "muted": green / yellow / red / grey
    estimate: bool  # extrapolated: the text carries ESTIMATE_MARK


def forecast(
    acc: AccountSnapshot,
    est: AccountEstimate | None,
    *,
    threshold: float | None,
    now: float,
    models: Sequence[str] = (),
) -> Forecast | None:
    """``ok``, ``~7d limit in 15h`` or ``5h at limit`` (measured). None when
    there is nothing to forecast or too little history to say."""
    if threshold is None or acc.disabled or acc.usage.sentinel is not None:
        return None
    pcts = window_pcts(acc, models)
    if not pcts:
        return None
    label, (pct, _r) = max(pcts.items(), key=lambda kv: kv[1][0])
    if pct >= threshold:
        return Forecast(f"{label} at limit", f"{label} limit", "crit", False)
    # Too little history to say: the cell stays blank rather than guess.
    if est is None or not est.windows:
        return None
    hits = []
    for west in est.windows.values():
        west = _at_measured_reset(west, pcts.get(west.window, (0.0, None))[1])
        eta = west.eta_s(threshold, now)
        if eta is not None:
            hits.append((eta, west))
    if hits:
        eta, west = min(hits, key=lambda h: h[0])
        if west.confidence not in _TRUSTED:
            return None
        mark, name = ESTIMATE_MARK, west.window
        if eta <= 0:
            return Forecast(f"{mark}{name} at limit", f"{mark}{name} limit", "crit", True)
        when = rough_duration(eta)
        return Forecast(f"{mark}{name} limit in {when}", f"{mark}{name} in {when}", "crit", True)
    if any(w.rate_pct_per_h is None for w in est.windows.values()):
        return None
    # Stays under: "ok" is always the same green; how close it lands is the
    # bar's and the chart's to say.
    return Forecast("ok", "ok", "ok", False)


def forecast_text(fc: Forecast | None, width: int, *, long: bool, palette: Palette) -> Text:
    if fc is None:
        return Text()
    color = {
        "ok": palette.sev_ok, "warn": palette.sev_warn, "crit": palette.sev_crit,
    }.get(fc.role, palette.muted)
    words = fc.text if long else fc.short
    return Text(_fit(words, width), style=color, no_wrap=True)


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
    cadence: PollCadence = DEFAULT_CADENCE,
    selected: bool = False,
) -> Text:
    """One account on one line, laid out by ``layout``:
    ``›● me@x.com  Acme  5h 58% ↻ 1h20m  7d 48% ↻ 23h  ~7d limit in 15h``.

    ``›`` and a bold name mark the row under the cursor (its band is CSS);
    ``●`` and an accent name mark the account on duty, so the two read apart
    at a glance."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(CURSOR_MARK if selected else " ", style=f"bold {palette.accent}")
    text.append(ON_DUTY_MARK if acc.is_active else " ", style=palette.accent)
    text.append(" ")
    name_style = name_color(acc, palette)
    text.append(fit_name(account_name(acc), layout.name_w), style=f"bold {name_style}" if selected else name_style)
    if layout.tag_w:
        if layout.level.org:
            tag, warn = row_tag(acc, now, login_warn_days, cadence)
        else:
            tag, warn = status_tag(acc, now, login_warn_days, cadence) or ("", False)
        text.append(" " * _GAP + _fit(tag, layout.tag_w), style=palette.sev_warn if warn else palette.muted)

    pcts = shown_windows(acc)
    if acc.usage.sentinel is not None:  # the tag says what is wrong
        text.rstrip()
        return text
    if not pcts:
        text.append(" " * _GAP + "usage unknown", style=palette.muted)
        return text

    for label in layout.windows:
        measured = pcts.get(label)
        projected = None
        if measured is not None and est is not None and label in est.windows:
            projected = projected_at_reset(est.windows[label], measured[1], now)
        text.append(" " * _GAP)
        text.append(
            window_cell(
                label, measured, layout, now=now, threshold=threshold, palette=palette,
                projected=projected,
            )
        )
    fc = forecast(acc, est, threshold=threshold, now=now, models=models) if layout.forecast_w else None
    if fc is not None:
        text.append(" " * _GAP)
        text.append(forecast_text(fc, layout.forecast_w, long=layout.level.long_forecast, palette=palette))
    text.rstrip()
    return text


# ---------------------------------------------------------------------------
# Charts: the selected account's 5h and 7d windows
# ---------------------------------------------------------------------------

# Two graphs sit side by side from this pane width; below it they stack.
SIDE_BY_SIDE_MIN_WIDTH = 100
# A chart shorter than this many lines (its time axis included) is too
# cramped to read, so the charts are hidden instead and the rows get the room.
CHART_MIN_PLOT = 8


def chart_min_rows(width: int, n_blocks: int = 2) -> int:
    """Spare rows the charts need at ``width``: the account line, then each
    chart's title and a :data:`CHART_MIN_PLOT` plot, side by side on a wide
    pane (10 rows) or stacked on a narrow one (19)."""
    if n_blocks <= 0:
        return 0
    per_block = 1 + CHART_MIN_PLOT
    if n_blocks == 2 and width >= SIDE_BY_SIDE_MIN_WIDTH:
        return 1 + per_block
    return 1 + n_blocks * per_block


CHART_MIN_ROWS = chart_min_rows(SIDE_BY_SIDE_MIN_WIDTH)  # the least they ever need


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
    """``5h · est. ~81% at reset`` / ``7d · est. ~limit in 15h`` / ``5h · —``.

    Only the forecast, said to be an estimate in words: the pct is in the
    table and the reset clock is the chart's right edge."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(west.window, style=f"bold {palette.accent}")
    sep = " · "
    est = f"{ESTIMATE_WORD} {ESTIMATE_MARK}"
    rate = west.rate_pct_per_h
    if threshold is not None and measured_pct >= threshold:
        text.append(f"{sep}at limit", style=palette.sev_crit)
    elif measured_pct <= 0 and not rate:
        text.append(f"{sep}unused", style=palette.muted)
    elif rate is None or west.confidence not in _TRUSTED:
        text.append(f"{sep}{NO_ESTIMATE}", style=palette.muted)
    else:
        eta = west.eta_s(threshold, now) if threshold is not None else None
        if eta is not None:
            words = f"{est}at limit" if eta <= 0 else f"{est}limit in {rough_duration(eta)}"
            text.append(f"{sep}{words}", style=palette.sev_crit)
        else:
            future = west.resets_at is not None and west.resets_at > now
            at_reset = west.projected_pct(west.resets_at - 1) if future else None
            if at_reset is not None:
                color = outcome_color(at_reset, threshold, palette)
                text.append(f"{sep}{est}{at_reset:.0f}% at reset", style=color)
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

    Measured history is solid; the projection is dotted and coloured by its
    outcome (:func:`outcome_color`), and left out when the estimate is too
    thin to trust."""
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
        color = outcome_color(projected_at_reset(west, resets_at, now), threshold, palette)

        def render(w: int, h: int, drawn=drawn, history=history, color=color) -> list[Text]:
            return line_plot(
                drawn, now=now, threshold=threshold, width=w, height=h,
                palette=palette, points=history or None, projection_color=color,
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
    """The charts in at most ``height`` lines, or none when fewer than
    :func:`chart_min_rows` are spare: a line naming the account they belong
    to, then the 5h and 7d charts."""
    if acc is None or height < CHART_MIN_ROWS:
        return []
    blocks = chart_blocks(acc, est, series, now=now, threshold=threshold, palette=palette)
    if not blocks or height < chart_min_rows(width, len(blocks)):
        return []
    title = Text(f"{CURSOR_MARK} ", style=f"bold {palette.accent}", no_wrap=True)
    title.append(account_title(acc, palette))
    return [title, *_arrange(blocks, width=width, height=height - 1)]


# ---------------------------------------------------------------------------
# History view (h): the long-term view
# ---------------------------------------------------------------------------

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
    """``me@x.com  Acme`` above the graphs: the name in the accent when it
    is the account on duty, as in its row."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(account_name(acc), style=f"bold {name_color(acc, palette)}")
    tag = "off" if acc.disabled else acc.display_tag
    if tag != "personal":
        text.append(f"  {tag}", style=palette.muted)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
    return text


def history_title(acc: AccountSnapshot, palette: Palette) -> Text:
    """``history  me@x.com      h/esc back``: which view, whose, the way out."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("history", style=f"bold {palette.accent}")
    text.append("  ")
    text.append(account_title(acc, palette))
    text.append("   h/esc back", style=palette.muted)
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
    blocks: list[tuple[Text, object]], *, width: int, height: int
) -> list[Text]:
    """Lay out (title, render(width, plot_h) -> lines) blocks in at most
    ``height`` lines: the plots grow to fill it, so a tall terminal has no
    dead space under them.

    Two blocks go side by side on a wide pane, else they stack and share the
    height. Each block is its title line plus its plot.
    """
    if not blocks or height <= 0:
        return []
    if len(blocks) == 2 and width >= SIDE_BY_SIDE_MIN_WIDTH:
        col_w = _block_width(width, 2)
        plot_h = height - 1
        columns = [[title, *render(col_w, plot_h)] if plot_h > 0 else [title] for title, render in blocks]
        return _side_by_side(columns[0], columns[1], col_w)[:height]
    each = height // len(blocks)
    lines: list[Text] = []
    for i, (title, render) in enumerate(blocks):
        share = height - each * (len(blocks) - 1) if i == len(blocks) - 1 else each
        plot_h = share - 1
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
) -> list[Text]:
    """The long-term view: one column per past 5h window over the last week,
    and the 7d usage curve over up to 30 days (as far back as history goes)."""
    if acc is None or height <= 0:
        return []
    lines = [history_title(acc, palette)] if title else []
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
        width = self.size.width or 80
        text = screen.row_text(row.number, width)
        text.truncate(width, overflow="ellipsis")  # a row never wraps
        return text


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


# The prompt line's idle hint: the everyday key and the way to the rest.
MAIN_HINT_KEYS: tuple[tuple[str, str], ...] = (
    ("⏎", "switch"),
    ("a", "add"),
    ("?", "keys"),
)

KeyGroup = tuple[str, tuple[tuple[str, str], ...]]  # (heading, ((key, words), ...))

# Every key, listed by ``?`` in two columns in place of the charts; each
# works from the main page. Two columns of at most 12 lines fit a 60x20
# terminal beside three accounts.
KEY_COLUMNS: tuple[tuple[KeyGroup, ...], ...] = (
    (
        ("accounts", (
            ("↑↓ jk", "move"),
            ("enter", "switch to it"),
            ("a", "add an account"),
            ("l", "log in (browser)"),
            ("t", "paste a token"),
            ("d", "turn off / on"),
            ("x", "remove"),
        )),
        ("app", (
            ("?", "this list (esc back)"),
            ("q", "quit"),
        )),
    ),
    (
        ("view", (
            ("h", "history (esc back)"),
            ("f", "refresh now"),
            ("ctrl+t", "theme"),
        )),
        ("setup", (
            ("c", "config"),
            ("s", "statusline feed"),
        )),
    ),
)
_KEY_W = 7  # "ctrl+t "
_COL_GAP = 3


def keys_text(
    columns: tuple[tuple[KeyGroup, ...], ...] = KEY_COLUMNS, *, palette: Palette = Palette.DARK
) -> Text:
    """Key groups laid out in side-by-side columns: a dim heading per group,
    then one ``key  words`` line per key."""
    rendered: list[list[Text]] = []
    for groups in columns:
        lines: list[Text] = []
        for heading, keys in groups:
            if lines:
                lines.append(Text())
            lines.append(Text(heading, style=palette.muted))
            for key, words in keys:
                line = Text()
                line.append(f"{key:<{_KEY_W}}", style=f"bold {palette.accent}")
                line.append(words, style=palette.foreground)
                lines.append(line)
        rendered.append(lines)
    widths = [max((line.cell_len for line in col), default=0) for col in rendered]
    body = Text(no_wrap=True)
    for i in range(max((len(col) for col in rendered), default=0)):
        if i:
            body.append("\n")
        for c, col in enumerate(rendered):
            cell = col[i].copy() if i < len(col) else Text()
            if c < len(rendered) - 1:
                cell.pad_right(widths[c] - cell.cell_len + _COL_GAP)
            body.append(cell)
    body.rstrip()
    return body


KEYS_ROWS = len(keys_text().plain.splitlines())

# Screen actions that only make sense with the account table in view; the
# settings list gets the keyboard to itself while it is shown.
MAIN_ONLY_ACTIONS = frozenset({
    "login_selected", "add", "add_token", "toggle_selected", "remove_selected",
    "toggle_long", "statusline", "refresh_full", "help", "leave_panel",
    "cursor_down", "cursor_up", "open_config",
})


class MainScreen(Screen):
    """The one screen: header, then the account body or the settings in its
    place, then the prompt line."""

    BINDINGS = [
        Binding("l", "login_selected", show=False),
        Binding("a", "add", show=False),
        Binding("t", "add_token", show=False),
        Binding("d", "toggle_selected", show=False),
        Binding("x,delete", "remove_selected", show=False),
        Binding("c", "open_config", show=False),
        Binding("h", "toggle_long", show=False),
        Binding("s", "statusline", show=False),
        Binding("f", "refresh_full", show=False),
        Binding("question_mark", "help", show=False),
        Binding("q", "app.quit", show=False),
        Binding("escape", "leave_panel", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    # Countdowns ("↻ 1h20m") are minute-grained, so one coarse tick keeps
    # them honest; everything else repaints on data change.
    CLOCK_TICK_S = 30.0

    app: "RotaApp"

    def compose(self) -> ComposeResult:
        yield _Painted("status_line", id="status")
        with Vertical(id="body"):
            yield ListView(id="accounts")
            yield _Painted("detail_text", id="detail")
            # Hidden until the first event: an empty log would only be a blank band.
            log = RichLog(id="event-log", highlight=False, markup=False, wrap=False, max_lines=50)
            log.display = False
            yield log
        config = ConfigView(self.app.switcher.backup_dir, id="config")
        config.display = False
        yield config
        yield PromptLine(id="prompt")

    def on_mount(self) -> None:
        self.query_one("#accounts", ListView).focus()
        self._content_key: tuple | None = None
        self.keys_shown = False  # ``?``: the key list in place of the charts
        self._set_hint()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "estimates", lambda _e: self.repaint())
        self.watch(self.app, "long_view", lambda _v: self.repaint())
        self.watch(self.app, "long_term", lambda _l: self._repaint_detail())
        self.watch(self.app, "auto_status", lambda _s: self._repaint_status())
        self.watch(self.app, "feed_state", lambda _f: self._repaint_status())
        self.watch(self.app, "theme", lambda _t: self._on_theme())
        self.set_interval(self.CLOCK_TICK_S, self.repaint)

    # -- state accessors ------------------------------------------------------

    @property
    def palette(self) -> Palette:
        return Palette.from_theme(self.app.current_theme)

    @property
    def prompt(self) -> PromptLine:
        return self.query_one(PromptLine)

    @property
    def config(self) -> ConfigView:
        return self.query_one(ConfigView)

    @property
    def config_shown(self) -> bool:
        return self.config.display

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """With the settings shown, the account keys stand down."""
        if action in MAIN_ONLY_ACTIONS and self.config_shown:
            return False
        return True

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
        names = [account_name(acc) for acc in accounts]
        warn_days, cadence = self.app.login_warn_days, self.app.cadence
        tags = [len(row_tag(acc, now, warn_days, cadence)[0]) for acc in accounts]
        warn_tags = [len((status_tag(acc, now, warn_days, cadence) or ("",))[0]) for acc in accounts]
        return RowLayout.for_width(
            width,
            table_windows(accounts, self.app.models),
            name_need=max(map(len, names), default=_MIN_NAME_W),
            short_name_need=max(map(short_name_len, names), default=_MIN_NAME_W),
            tag_need=max(tags, default=0),
            warn_tag_need=max(warn_tags, default=0),
        )

    def _chrome_rows(self) -> int:
        """Rows everything but the table and the pane under it takes."""
        log = self.query_one("#event-log")
        return (
            1  # status band
            + 1  # table margin
            + 1  # detail margin
            + 2  # prompt line + its margin
            + (log.outer_size.height or 3 if log.display else 0)
        )

    def _fit_table(self) -> None:
        """Let the table take every row the page can spare: with the charts
        hidden the rows get the room, and a long list scrolls instead of
        pushing the prompt line off screen."""
        listview = self.query_one("#accounts", ListView)
        rows = max(1, self.size.height - self._chrome_rows())
        if self.keys_shown:  # the key list needs its rows whole; the table scrolls
            rows = max(1, rows - KEYS_ROWS)
        if listview.styles.max_height != Scalar.parse(str(rows)):
            listview.styles.max_height = rows

    def detail_budget(self) -> int:
        """Lines the pane under the table may take so the page fits the
        terminal: the screen minus the header, table, event log and prompt line."""
        listview = self.query_one("#accounts", ListView)
        rows = listview.outer_size.height or len(listview.children)
        return max(0, self.size.height - self._chrome_rows() - rows)

    def charts_fit(self) -> bool:
        detail = self.query_one("#detail")
        width = detail.size.width or max(1, self.size.width - 2)
        return self.detail_budget() >= chart_min_rows(width)

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
            profile=profiles.active_name(),
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
            cadence=self.app.cadence,
            selected=number == self.selected_number,
        )

    def detail_text(self, width: int, _height: int) -> Text:
        snap = self.app.snapshot
        if snap is None:
            return Text("loading…", style=self.palette.muted)
        if not snap.accounts:
            return Text("No managed accounts yet. Press a to add one.", style=self.palette.muted)
        if self.keys_shown:
            return keys_text(palette=self.palette)
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
        self._fit_table()
        self.query_one("#status").refresh()
        self._repaint_detail()
        for row in self.query(AccountRow):
            row.repaint()

    def _repaint_detail(self) -> None:
        # The pane is as tall as its content, so a repaint may resize it.
        self.query_one("#detail").refresh(layout=True)

    def _repaint_status(self) -> None:
        self.query_one("#status").refresh()

    def _set_hint(self) -> None:
        keys = CONFIG_HINT_KEYS if self.config_shown else MAIN_HINT_KEYS
        self.prompt.set_hint(hint_text(keys, self.palette))

    def _on_theme(self) -> None:
        self._set_hint()
        self.repaint()
        if self.config_shown:
            self.config.settings_list.refresh()

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
        # A height change alone does not resize the chart pane's width, so
        # make it re-measure the spare rows.
        self.repaint()

    def on_list_view_highlighted(self, _event: ListView.Highlighted) -> None:
        # The cursor glyph and bold name move with the highlight.
        for row in self.query(AccountRow):
            row.repaint()
        self._repaint_detail()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, AccountRow):
            self.app.do_switch(event.item.number)

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
        self._show_keys(False)
        self.app.toggle_long_view()

    def action_leave_panel(self) -> None:
        """esc: close the key list, else leave the history view."""
        if self.keys_shown:
            self._show_keys(False)
        elif self.app.long_view:
            self.app.toggle_long_view()

    def action_help(self) -> None:
        """``?``: the key list in place of the charts, or back."""
        self._show_keys(not self.keys_shown)

    def _show_keys(self, shown: bool) -> None:
        if shown != self.keys_shown:
            self.keys_shown = shown
            self.repaint()

    # -- the settings, in place of the body -------------------------------------

    def action_open_config(self) -> None:
        self.prompt.cancel()
        self.query_one("#body").display = False
        self.config.display = True
        self.config.open()
        self._set_hint()

    def on_config_view_closed(self, event: ConfigView.Closed) -> None:
        event.stop()
        self.config.display = False
        self.query_one("#body").display = True
        self.query_one("#accounts", ListView).focus()
        self._set_hint()
        self.repaint()

    # -- app actions, gated by check_action -------------------------------------

    def action_add(self) -> None:
        self.app.action_add()

    def action_add_token(self) -> None:
        self.app.action_add_token()

    def action_statusline(self) -> None:
        self.app.action_statusline()

    def action_refresh_full(self) -> None:
        self.app.action_refresh_full()

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
