"""Config page: every settings.json key, generated from ``SETTING_SPECS``.

The page has no per-setting UI code. Each spec becomes one compact row in
the section named by its ``group`` (``SETTING_GROUPS`` fixes the order), and
the editor follows ``spec.kind``:

- ``float`` / ``int`` / ``string``: an ``Input``. Enter or leaving the field
  validates with ``parse_setting_value``; an error shows inline and nothing
  is written. An empty field means "unset" (back to the default).
- ``bool``: a ``Switch``.
- ``choice``: a ``Select``.

Every write goes through ``set_setting`` / ``unset_setting`` (the same path
as ``cswap config set``), then the screen posts ``ConfigScreen.SettingChanged``
so the host app can react (restart the engine, apply the theme). A value
equal to the current effective value is a no-op, so focus changes and
programmatic refreshes never write.

Keys: tab / shift+tab or up / down move between editors, enter applies,
ctrl+r resets the focused setting, escape (or c / q outside a text field)
goes back. CSS lives in ``DEFAULT_CSS`` so the app stylesheet stays separate.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Input, Select, Static, Switch

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

_HINT = (
    "tab/↑↓ move · enter apply · ctrl+r reset to default · esc back"
)


def row_id(spec: SettingSpec) -> str:
    """DOM id for a spec's row (dots are not valid in Textual ids)."""
    return "setting-" + spec.dotted.replace(".", "-")


def spec_detail(spec: SettingSpec) -> str:
    """One-line description for the focused setting: help, range, default."""
    if spec.kind in ("float", "int"):
        allowed = f"{format_setting_value(spec.lo)}-{format_setting_value(spec.hi)}"
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


class _RowSelect(Select, inherit_bindings=False):
    """A Select that opens on enter/space only, so up/down keep moving
    between rows instead of popping the menu open."""

    BINDINGS = [Binding("enter,space", "show_overlay", "Show menu", show=False)]


class SettingRow(Horizontal):
    """One setting: label, editor, help (or inline error), marker, reset."""

    def __init__(self, spec: SettingSpec, value: object, is_set: bool) -> None:
        super().__init__(id=row_id(spec), classes="setting-row")
        self.spec = spec
        self.value = value
        self.is_set = is_set
        self.error: str | None = None
        # The Input text last written or loaded; equal text means "not dirty".
        self.committed_text = self._text(value)

    @staticmethod
    def _text(value: object) -> str:
        return "" if value is None else format_setting_value(value)

    def compose(self) -> ComposeResult:
        spec = self.spec
        yield Static(spec.label, classes="setting-label")
        if spec.kind == "bool":
            yield Switch(value=bool(self.value), classes="setting-editor")
        elif spec.kind == "choice":
            yield _RowSelect(
                [(choice, choice) for choice in spec.choices],
                value=self.value,
                allow_blank=False,
                compact=True,
                classes="setting-editor",
            )
        else:
            yield Input(
                value=self.committed_text,
                placeholder=self._text(spec.default) or "(none)",
                compact=True,
                classes="setting-editor",
            )
        yield Static(spec.help, classes="setting-help", markup=False)
        yield Static(classes="setting-marker")
        yield Button("reset", compact=True, classes="setting-reset")

    def on_mount(self) -> None:
        self._show_marker()
        # Reset buttons stay clickable but out of the tab order (ctrl+r is
        # the keyboard path), so tab walks editors only.
        self.query_one(".setting-reset", Button).can_focus = False

    @property
    def editor(self) -> Widget:
        return self.query_one(".setting-editor")

    def _show_marker(self) -> None:
        marker = self.query_one(".setting-marker", Static)
        marker.update(Text("custom", style="bold") if self.is_set else "default")
        self.set_class(self.is_set, "-custom")
        self.query_one(".setting-reset", Button).disabled = not self.is_set

    def show_error(self, message: str | None) -> None:
        self.error = message
        help_ = self.query_one(".setting-help", Static)
        help_.update(message if message else self.spec.help)
        self.set_class(message is not None, "-error")
        screen = self.screen
        if isinstance(screen, ConfigScreen):
            screen._show_detail(self, message)

    def load(self, value: object, is_set: bool) -> None:
        """Show a freshly read effective value without firing change events."""
        self.value = value
        self.is_set = is_set
        self.committed_text = self._text(value)
        editor = self.editor
        if isinstance(editor, Switch):
            with editor.prevent(Switch.Changed):
                editor.value = bool(value)
        elif isinstance(editor, Select):
            with editor.prevent(Select.Changed):
                editor.value = value
        elif isinstance(editor, Input):
            editor.value = self.committed_text
        self.show_error(None)
        self._show_marker()


class ConfigScreen(Screen):
    """Settings editor generated from ``SETTING_SPECS``.

    Push it with ``app.push_screen(ConfigScreen(backup_dir))``. After each
    persisted change it posts ``ConfigScreen.SettingChanged(key)``; the app
    handles it with ``on_config_screen_setting_changed``.
    """

    class SettingChanged(Message):
        """A setting was written to or removed from settings.json."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key  # dotted key, e.g. "autoswitch.threshold"

    BINDINGS = [
        Binding("escape,c,q", "close", "Back"),
        Binding("down", "app.focus_next", "Next", show=False),
        Binding("up", "app.focus_previous", "Previous", show=False),
        Binding("ctrl+r", "reset_focused", "Reset"),
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
    }
    ConfigScreen .setting-group {
        height: auto;
        border: round $primary 50%;
        border-title-color: $primary;
        border-title-style: bold;
        padding: 0 1;
        margin: 0 1;
    }
    ConfigScreen .setting-row {
        height: 1;
    }
    ConfigScreen .setting-row:focus-within {
        background: $boost;
    }
    ConfigScreen .setting-label {
        width: 25;
    }
    ConfigScreen .setting-editor {
        width: 18;
    }
    ConfigScreen Input.setting-editor {
        padding: 0;
        background: $surface;
    }
    ConfigScreen Switch.setting-editor {
        border: none;
        height: 1;
        padding: 0;
        width: 8;
        margin-right: 10;
    }
    ConfigScreen .setting-help {
        width: 1fr;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ConfigScreen .setting-row.-error .setting-help {
        color: $error;
        text-style: bold;
    }
    ConfigScreen .setting-row.-error Input.setting-editor {
        background-tint: $error 25%;
    }
    ConfigScreen .setting-marker {
        width: 8;
        color: $text-muted;
    }
    ConfigScreen .setting-row.-custom .setting-marker {
        color: $accent;
    }
    ConfigScreen .setting-reset {
        width: 7;
        min-width: 7;
        visibility: hidden;
    }
    ConfigScreen .setting-row.-custom .setting-reset {
        visibility: visible;
    }
    ConfigScreen #config-detail.-error {
        color: $error;
        text-style: bold;
    }
    ConfigScreen #config-detail {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ConfigScreen #config-hint {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    def __init__(self, backup_dir: Path) -> None:
        super().__init__()
        self.backup_dir = Path(backup_dir)

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        rows = effective_settings(self.backup_dir)
        yield Static("claude-swap · config", id="config-title")
        with VerticalScroll(id="config-body"):
            for group, title in SETTING_GROUPS:
                members = [r for r in rows if r[0].group == group]
                if not members:
                    continue
                section = Vertical(classes="setting-group", id=f"group-{group}")
                section.border_title = title
                with section:
                    for spec, value, is_set in members:
                        yield SettingRow(spec, value, is_set)
        yield Static(id="config-detail", markup=False)
        yield Static(_HINT, id="config-hint")

    def on_mount(self) -> None:
        first = self.query(".setting-editor").first()
        first.focus()

    def row(self, dotted_key: str) -> SettingRow:
        return self.query_one(f"#{row_id(SETTING_SPECS[dotted_key])}", SettingRow)

    @staticmethod
    def _row_of(widget: Widget | None) -> SettingRow | None:
        node = widget
        while node is not None and not isinstance(node, SettingRow):
            node = node.parent
        return node

    def _show_detail(self, row: SettingRow | None, error: str | None = None) -> None:
        """Bottom line: the full error (the inline one may be truncated on a
        narrow terminal), else the focused setting's help, range and default."""
        detail = self.query_one("#config-detail", Static)
        detail.set_class(error is not None, "-error")
        if error is not None:
            detail.update(error)
        elif row is not None:
            detail.update(spec_detail(row.spec))

    def on_descendant_focus(self, event) -> None:
        row = self._row_of(event.widget)
        if row is not None:
            self._show_detail(row, row.error)

    # -- writes -------------------------------------------------------------

    def _reload(self, row: SettingRow) -> None:
        for spec, value, is_set in effective_settings(self.backup_dir):
            if spec.dotted == row.spec.dotted:
                row.load(value, is_set)
                return

    def _changed(self, row: SettingRow) -> None:
        self._reload(row)
        self.post_message(self.SettingChanged(row.spec.dotted))

    def apply(self, row: SettingRow, raw: str) -> bool:
        """Validate and persist one raw value; False (with an inline error)
        when it was rejected. Empty means unset; an unchanged value is a
        no-op so nothing is written and no message is posted."""
        spec = row.spec
        raw = raw.strip()
        try:
            if raw == "":
                if unset_setting(self.backup_dir, spec.dotted):
                    self._changed(row)
                else:
                    self._reload(row)
                return True
            parsed = parse_setting_value(spec, raw)
            if parsed == row.value and type(parsed) is type(row.value):
                row.show_error(None)
                row.committed_text = raw
                return True
            set_setting(self.backup_dir, spec.dotted, raw)
        except ConfigError as e:
            row.show_error(str(e))
            return False
        self._changed(row)
        return True

    def reset(self, row: SettingRow) -> None:
        try:
            removed = unset_setting(self.backup_dir, row.spec.dotted)
        except ConfigError as e:
            row.show_error(str(e))
            return
        if removed:
            self._changed(row)
        else:
            self._reload(row)

    def _commit_input(self, input_: Input) -> None:
        row = self._row_of(input_)
        if row is None or input_.value == row.committed_text:
            return
        self.apply(row, input_.value)

    # -- events -------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit_input(event.input)

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if self.is_attached:
            self._commit_input(event.input)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        row = self._row_of(event.switch)
        if row is not None:
            self.apply(row, "true" if event.value else "false")

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        row = self._row_of(event.select)
        if row is not None and isinstance(event.value, str):
            self.apply(row, event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        row = self._row_of(event.button)
        if row is not None:
            self.reset(row)

    # -- actions ------------------------------------------------------------

    def action_reset_focused(self) -> None:
        row = self._row_of(self.focused)
        if row is not None:
            self.reset(row)

    def action_close(self) -> None:
        # A typed-but-unsubmitted value sticks, like leaving the field would.
        for input_ in self.query(Input):
            self._commit_input(input_)
        self.app.pop_screen()
