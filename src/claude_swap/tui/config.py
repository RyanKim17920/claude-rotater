"""Config page: every settings.json key, generated from ``SETTING_SPECS``.

One widget, ``SettingsList``, owns the keyboard for the whole page; there are
no per-row Inputs or Switches to tab between (focus hopping between small
editors is where arrow-key editing used to silently stop working). Rows are
grouped under the ``SETTING_GROUPS`` headings, and the keys are:

- ↑/↓ (or k/j): move between settings; home/end jump to the ends;
- ←/→ (or -/+): change the selected value in place. Numbers step by
  ``spec.step_size`` and clamp at the range ends, choices cycle, toggles flip;
- enter, or typing a digit on a number: inline edit of an exact value. Enter
  commits (out-of-range numbers clamp to the nearest bound), Esc cancels,
  ↑/↓ commit and move;
- ctrl+r: back to the default (the key is removed from settings.json);
- esc (or c / q while not editing): back to the main page.

Every change is written at once through ``set_setting`` / ``unset_setting``
(the same path as ``cswap config set``); a value equal to the default is
stored by removing the key, so the file only holds what differs. The screen
then posts ``ConfigScreen.SettingChanged`` so the host app can react
(restart the engine, apply the theme). Those notifications are debounced per
key by ``NOTIFY_DELAY_S``: holding → for a few seconds writes every step but
restarts the engine once. Leaving the page flushes anything pending.

The footer shows the selected setting's full help, range, step and default,
wrapped over as many lines as it needs, so nothing is cut off at 80 columns.
"""

from __future__ import annotations

import math
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_GROUPS,
    SETTING_SPECS,
    SettingSpec,
    effective_settings,
    format_setting_value,
    parse_setting_value,
    set_setting,
    unset_setting,
)
from claude_swap.tui.theme import Palette

_HINT = "↑↓ move · ←→ change · enter or 0-9 exact value · ctrl+r default · esc back"
_LABEL_W = 26
_VALUE_W = 16
_MARKER_W = 8
_MIN_HELP_W = 16  # narrower than this, the in-row help is dropped (footer has it)


def spec_detail(spec: SettingSpec) -> str:
    """Footer text for the selected setting: help, allowed values, default."""
    if spec.kind in ("float", "int"):
        allowed = (
            f"{format_setting_value(spec.lo)}-{format_setting_value(spec.hi)}, "
            f"step {format_setting_value(spec.step_size)}"
        )
    elif spec.kind == "choice":
        allowed = " | ".join(spec.choices)
    elif spec.kind == "bool":
        allowed = "on | off"
    else:
        allowed = "text; empty = unset"
    return (
        f"{spec.dotted}: {spec.help}  [{allowed}, default "
        f"{format_setting_value(spec.default)}]"
    )


def _num_text(spec: SettingSpec, value: float) -> str:
    """A number the way settings.json would hold it for this spec."""
    if spec.kind == "int":
        return str(int(value))
    return format_setting_value(float(round(value, 6)))


def stepped(spec: SettingSpec, value, direction: int):
    """The value one ←/→ press away: clamped numbers, cycled choices,
    flipped toggles. Strings do not step (returns ``value``)."""
    if spec.kind in ("float", "int"):
        base = float(value if value is not None else spec.default)
        nxt = min(max(base + direction * spec.step_size, spec.lo), spec.hi)
        return int(nxt) if spec.kind == "int" else round(nxt, 6)
    if spec.kind == "choice":
        idx = spec.choices.index(value) if value in spec.choices else 0
        return spec.choices[(idx + direction) % len(spec.choices)]
    if spec.kind == "bool":
        return not bool(value)
    return value


def clamp_typed(spec: SettingSpec, raw: str) -> tuple[str, str | None]:
    """(raw value to store, note) for typed text: numbers clamp into range
    instead of erroring; anything else passes through to validation."""
    if spec.kind not in ("float", "int"):
        return raw, None
    try:
        number = float(raw)
    except ValueError:
        return raw, None  # parse_setting_value reports it
    if not math.isfinite(number):
        return raw, None
    if spec.kind == "int":
        number = float(round(number))
    clamped = min(max(number, spec.lo), spec.hi)
    note = None
    if clamped != number:
        note = (
            f"{spec.dotted}: clamped to {_num_text(spec, clamped)} "
            f"(range {format_setting_value(spec.lo)}-{format_setting_value(spec.hi)})"
        )
    return _num_text(spec, clamped), note


class SettingsList(Widget, can_focus=True):
    """Every setting as one keyboard-driven list (see the module docstring)."""

    BINDINGS = [
        Binding("up,k", "move(-1)", "Up", show=False),
        Binding("down,j", "move(1)", "Down", show=False),
        Binding("home", "jump(0)", show=False),
        Binding("end", "jump(-1)", show=False),
        Binding("left,minus,h", "adjust(-1)", "Less", show=False),
        Binding("right,plus,equals_sign,l", "adjust(1)", "More", show=False),
        Binding("enter", "enter", "Edit", show=False),
        Binding("ctrl+r", "reset", "Default", show=False),
    ]

    DEFAULT_CSS = """
    SettingsList {
        height: auto;
        padding: 0 1;
    }
    """

    class Changed(Message):
        """A setting was written; ``key`` is its dotted name."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    class Selected(Message):
        """The selection, edit buffer or note changed: repaint the footer."""

    def __init__(self, backup_dir: Path, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.backup_dir = Path(backup_dir)
        self.specs: list[SettingSpec] = [
            spec
            for group, _title in SETTING_GROUPS
            for spec in SETTING_SPECS.values()
            if spec.group == group
        ]
        self.cursor = 0
        self.editing: str | None = None  # inline edit buffer; None = not editing
        self.error: str | None = None  # rejection or clamp note for the footer
        self.values: dict[str, tuple[object, bool]] = {}
        self.reload()

    # -- state -------------------------------------------------------------------

    @property
    def spec(self) -> SettingSpec:
        return self.specs[self.cursor]

    def value_of(self, dotted: str) -> object:
        return self.values[dotted][0]

    def reload(self) -> None:
        self.values = {
            spec.dotted: (value, is_set)
            for spec, value, is_set in effective_settings(self.backup_dir)
        }

    def lines(self) -> list[tuple[str, SettingSpec | None]]:
        """One entry per rendered line: ("", None) gap, (heading, None), ("row", spec)."""
        out: list[tuple[str, SettingSpec | None]] = []
        for group, title in SETTING_GROUPS:
            members = [s for s in self.specs if s.group == group]
            if not members:
                continue
            if out:
                out.append(("", None))
            out.append((title, None))
            out.extend(("row", s) for s in members)
        return out

    def line_of(self, index: int) -> int:
        target = self.specs[index]
        return next(i for i, (_k, s) in enumerate(self.lines()) if s is target)

    # -- rendering ---------------------------------------------------------------

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(self.lines())

    def render(self) -> Text:
        palette = Palette.from_theme(self.app.current_theme)
        width = max(self.size.width, 40)
        out = Text(no_wrap=True, overflow="ellipsis")
        for i, (kind, spec) in enumerate(self.lines()):
            if i:
                out.append("\n")
            if spec is None:
                out.append(kind, style=f"bold {palette.accent}")
            else:
                out.append(self._row(spec, width, palette))
        return out

    def _value_cell(self, spec: SettingSpec, selected: bool, palette: Palette) -> Text:
        value, _is_set = self.values[spec.dotted]
        cell = Text(no_wrap=True)
        if selected and self.editing is not None:
            cell.append(self.editing, style=f"bold {palette.foreground}")
            cell.append("▏", style=palette.accent)
            return cell
        if spec.kind == "bool":
            shown = "● on" if value else "○ off"
        elif value is None:
            shown = "(none)"
        else:
            shown = format_setting_value(value)
        if selected and spec.kind != "string":
            cell.append("‹ ", style=palette.accent)
            cell.append(shown, style=f"bold {palette.foreground}")
            cell.append(" ›", style=palette.accent)
        else:
            cell.append(f"  {shown}", style=palette.muted if value is None else palette.foreground)
        return cell

    def _row(self, spec: SettingSpec, width: int, palette: Palette) -> Text:
        current = spec is self.spec
        selected = current and self.has_focus
        _value, is_set = self.values[spec.dotted]
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append("▸ " if current else "  ", style=f"bold {palette.accent}")
        label = (spec.label or spec.dotted)[: _LABEL_W - 1].ljust(_LABEL_W)
        row.append(label, style=f"bold {palette.foreground}" if selected else palette.foreground)
        cell = self._value_cell(spec, selected, palette)
        row.append(cell)
        row.append(" " * max(1, _VALUE_W - cell.cell_len))
        row.append(
            ("custom" if is_set else "default").ljust(_MARKER_W),
            style=palette.accent if is_set else palette.muted,
        )
        help_w = width - row.cell_len - 2
        if help_w >= _MIN_HELP_W:
            text = spec.help if len(spec.help) <= help_w else spec.help[: help_w - 1] + "…"
            row.append(text, style=palette.muted)
        if selected:
            row.pad_right(max(0, width - 2 - row.cell_len))
            theme = self.app.current_theme
            row.stylize(f"on {theme.surface or theme.panel or 'grey23'}")
        return row

    # -- navigation --------------------------------------------------------------

    def _changed_view(self) -> None:
        self.refresh(layout=True)
        self.post_message(self.Selected())

    def _select(self, index: int) -> None:
        self.cursor = max(0, min(index, len(self.specs) - 1))
        self.error = None
        self._changed_view()
        self.call_after_refresh(self._scroll_into_view)

    def _scroll_into_view(self) -> None:
        parent = self.parent
        if not isinstance(parent, VerticalScroll):
            return
        line = self.line_of(self.cursor)
        # Keep a section's heading in view when its first row is selected.
        top = line - 1 if line > 0 and self.lines()[line - 1][0] not in ("row", "") else line
        parent.scroll_to_region(
            Region(0, top, max(1, self.size.width), line - top + 1), animate=False
        )

    def action_move(self, delta: int) -> None:
        if self.editing is not None and not self._commit():
            return
        nxt = self.cursor + delta
        if 0 <= nxt < len(self.specs):
            self._select(nxt)

    def action_jump(self, index: int) -> None:
        if self.editing is not None and not self._commit():
            return
        self._select(index if index >= 0 else len(self.specs) - 1)

    # -- changes -------------------------------------------------------------------

    def action_adjust(self, direction: int) -> None:
        if self.editing is not None:
            return
        spec = self.spec
        if spec.kind == "string":
            return
        current = self.value_of(spec.dotted)
        new = stepped(spec, current, direction)
        if new == current and type(new) is type(current):
            return  # at the bound already: nothing to write
        if spec.kind in ("float", "int"):
            raw = _num_text(spec, float(new))
        elif spec.kind == "bool":
            raw = "true" if new else "false"
        else:
            raw = str(new)
        self._write(spec, raw)

    def action_enter(self) -> None:
        if self.editing is not None:
            self._commit()
            return
        spec = self.spec
        if spec.kind in ("bool", "choice"):
            self.action_adjust(1)
            return
        value = self.value_of(spec.dotted)
        self.editing = "" if value is None else format_setting_value(value)
        self.error = None
        self._changed_view()

    def action_reset(self) -> None:
        self.editing = None
        spec = self.spec
        try:
            removed = unset_setting(self.backup_dir, spec.dotted)
        except ConfigError as e:
            self._fail(str(e))
            return
        self.reload()
        self.error = None
        self._changed_view()
        if removed:
            self.post_message(self.Changed(spec.dotted))

    def cancel_edit(self) -> bool:
        """Esc while editing drops the buffer. False when not editing."""
        if self.editing is None:
            return False
        self.editing = None
        self.error = None
        self._changed_view()
        return True

    def _commit(self) -> bool:
        """Store the edit buffer; False (still editing, error shown) if invalid."""
        spec = self.spec
        raw = (self.editing or "").strip()
        if raw == "":
            self.action_reset()
            return True
        raw, note = clamp_typed(spec, raw)
        try:
            parse_setting_value(spec, raw)
        except ConfigError as e:
            self._fail(str(e))
            return False
        self.editing = None
        self._write(spec, raw)
        if note is not None and self.error is None:
            self.error = note
            self._changed_view()
        return True

    def _write(self, spec: SettingSpec, raw: str) -> None:
        """Persist one validated raw value; equal to the default removes the key."""
        try:
            parsed = parse_setting_value(spec, raw)
            before = self.values.get(spec.dotted)
            if parsed == spec.default and type(parsed) is type(spec.default):
                removed = unset_setting(self.backup_dir, spec.dotted)
                changed = removed or (before is not None and before[0] != parsed)
            else:
                set_setting(self.backup_dir, spec.dotted, raw)
                changed = before != (parsed, True)
        except ConfigError as e:
            self._fail(str(e))
            return
        self.reload()
        self.error = None
        self._changed_view()
        if changed:
            self.post_message(self.Changed(spec.dotted))

    def _fail(self, message: str) -> None:
        self.error = message
        self._changed_view()

    # -- typing and mouse ------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        char = event.character
        if self.editing is not None:
            if event.key == "backspace":
                self.editing = self.editing[:-1]
            elif char is not None and char.isprintable() and len(char) == 1:
                self.editing += char
            else:
                return  # enter / escape / arrows: handled by bindings
            event.stop()
            event.prevent_default()
            self.refresh()
            return
        if self.spec.kind in ("float", "int") and char is not None and char.isdigit():
            # A digit starts an exact entry that replaces the shown value.
            event.stop()
            event.prevent_default()
            self.editing = char
            self.error = None
            self._changed_view()

    def on_click(self, event: events.Click) -> None:
        lines = self.lines()
        y = event.y
        if 0 <= y < len(lines) and lines[y][1] is not None:
            if self.editing is not None and not self._commit():
                return
            self._select(self.specs.index(lines[y][1]))

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()


class ConfigScreen(Screen):
    """Settings editor generated from ``SETTING_SPECS``.

    Push it with ``app.push_screen(ConfigScreen(backup_dir))``. After each
    persisted change (debounced per key) it posts
    ``ConfigScreen.SettingChanged(key)``; the app handles it with
    ``on_config_screen_setting_changed``.
    """

    NOTIFY_DELAY_S = 0.4

    class SettingChanged(Message):
        """A setting was written to or removed from settings.json."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key  # dotted key, e.g. "autoswitch.threshold"

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("c,q", "close", "Back", show=False),
    ]

    DEFAULT_CSS = """
    ConfigScreen {
        layout: vertical;
    }
    ConfigScreen #config-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
        background: $panel;
    }
    ConfigScreen #config-body {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }
    ConfigScreen #config-detail {
        height: auto;
        max-height: 4;
        padding: 0 1;
        color: $text-muted;
    }
    ConfigScreen #config-detail.-error {
        color: $error;
        text-style: bold;
    }
    ConfigScreen #config-hint {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self, backup_dir: Path) -> None:
        super().__init__()
        self.backup_dir = Path(backup_dir)
        self._pending: dict[str, object] = {}  # dotted key -> debounce timer

    def compose(self) -> ComposeResult:
        yield Static("claude-swap · config", id="config-title")
        body = VerticalScroll(id="config-body")
        body.can_focus = False
        with body:
            yield SettingsList(self.backup_dir, id="settings")
        yield Static(id="config-detail", markup=False)
        yield Static(_HINT, id="config-hint")

    @property
    def settings_list(self) -> SettingsList:
        return self.query_one(SettingsList)

    def on_mount(self) -> None:
        self.settings_list.focus()
        self._show_detail()

    def _show_detail(self) -> None:
        lst = self.settings_list
        detail = self.query_one("#config-detail", Static)
        detail.set_class(lst.error is not None, "-error")
        detail.update(lst.error if lst.error is not None else spec_detail(lst.spec))

    def on_settings_list_selected(self, event: SettingsList.Selected) -> None:
        event.stop()
        self._show_detail()

    def on_settings_list_changed(self, event: SettingsList.Changed) -> None:
        event.stop()
        key = event.key
        timer = self._pending.pop(key, None)
        if timer is not None:
            timer.stop()
        if self.NOTIFY_DELAY_S <= 0:
            self.post_message(self.SettingChanged(key))
            return
        self._pending[key] = self.set_timer(self.NOTIFY_DELAY_S, lambda: self._notify(key))

    def _notify(self, key: str) -> None:
        if self._pending.pop(key, None) is not None:
            self.post_message(self.SettingChanged(key))

    def flush(self) -> None:
        """Deliver every pending change now: the page is closing, and a
        message posted to a screen leaving the stack would never arrive."""
        for key, timer in list(self._pending.items()):
            timer.stop()
            self._pending.pop(key, None)
            self.app.post_message(self.SettingChanged(key))

    def action_back(self) -> None:
        if self.settings_list.cancel_edit():
            return
        self.action_close()

    def action_close(self) -> None:
        if self.settings_list.editing is not None:
            return  # c / q are being typed into the edit
        self.flush()
        self.app.pop_screen()
