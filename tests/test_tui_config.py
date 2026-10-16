"""Pilot tests for the TUI Config page (tui/config.py).

ConfigScreen runs inside a minimal host App (not CswapApp) over a tmp
backup dir, so nothing touches the real ~/.claude-swap-backup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Select, Static, Switch

from claude_swap.settings import SETTING_GROUPS, SETTING_SPECS, settings_path
from claude_swap.tui.config import ConfigScreen, SettingRow, row_id, spec_detail

EDITOR_FOR_KIND = {
    "float": Input,
    "int": Input,
    "string": Input,
    "bool": Switch,
    "choice": Select,
}


class Host(App):
    """Minimal host: a base screen with ConfigScreen pushed on top, recording
    every SettingChanged that bubbles up to the app."""

    def __init__(self, backup_dir: Path) -> None:
        super().__init__()
        self.backup_dir = backup_dir
        self.changes: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("base", id="base")

    def on_mount(self) -> None:
        self.push_screen(ConfigScreen(self.backup_dir))

    def on_config_screen_setting_changed(self, message: ConfigScreen.SettingChanged) -> None:
        self.changes.append(message.key)


def read_file(backup_dir: Path) -> dict | None:
    path = settings_path(backup_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def screen_of(app: Host) -> ConfigScreen:
    assert isinstance(app.screen, ConfigScreen)
    return app.screen


async def focus_editor(pilot, app: Host, key: str):
    row = screen_of(app).row(key)
    row.editor.focus()
    await pilot.pause()
    return row


async def type_into(pilot, app: Host, key: str, text: str, *, submit: bool = True):
    row = await focus_editor(pilot, app, key)
    editor = row.editor
    assert isinstance(editor, Input)
    editor.value = ""
    await pilot.press(*text)
    if submit:
        await pilot.press("enter")
    await pilot.pause()
    return row


def test_kind_map_covers_every_spec_kind():
    # A new kind needs an editor before any spec can use it.
    assert {spec.kind for spec in SETTING_SPECS.values()} <= set(EDITOR_FOR_KIND)


def test_spec_detail_mentions_range_and_default():
    detail = spec_detail(SETTING_SPECS["autoswitch.threshold"])
    assert "50-99.9" in detail and "default 90" in detail
    assert "best | consume-first" in spec_detail(SETTING_SPECS["autoswitch.strategy"])


@pytest.mark.asyncio
class TestLayout:
    @pytest.mark.parametrize("key", list(SETTING_SPECS))
    async def test_every_spec_gets_one_row_with_the_right_editor(self, tmp_path, key):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            spec = SETTING_SPECS[key]
            rows = app.screen.query(f"#{row_id(spec)}")
            assert len(rows) == 1
            row = rows.first()
            assert isinstance(row.editor, EDITOR_FOR_KIND[spec.kind])
            assert row.query_one(".setting-label", Static).content == spec.label
            # The row sits in its group's bordered section.
            assert row.parent.id == f"group-{spec.group}"

    async def test_sections_follow_setting_groups_order(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sections = list(app.screen.query(".setting-group"))
            assert [s.border_title for s in sections] == [t for _, t in SETTING_GROUPS]
            assert len(app.screen.query(SettingRow)) == len(SETTING_SPECS)

    async def test_defaults_shown_with_default_marker(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = screen_of(app)
            row = screen.row("autoswitch.threshold")
            assert row.editor.value == "90"
            assert not row.has_class("-custom")
            assert str(row.query_one(".setting-marker", Static).content) == "default"
            assert screen.row("autoswitch.model").editor.value == ""
            assert screen.row("ui.theme").editor.value == "auto"
            assert screen.row("autoswitch.includeApiKeyAccounts").editor.value is False

    async def test_existing_file_values_loaded_as_custom(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps(
            {"autoswitch": {"threshold": 75, "includeApiKeyAccounts": True,
                            "strategy": "consume-first"},
             "ui": {"theme": "light"}}
        ))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = screen_of(app)
            row = screen.row("autoswitch.threshold")
            assert row.editor.value == "75"
            assert row.has_class("-custom")
            assert screen.row("autoswitch.includeApiKeyAccounts").editor.value is True
            assert screen.row("autoswitch.strategy").editor.value == "consume-first"
            assert screen.row("ui.theme").editor.value == "light"
        # Opening the page never writes.
        assert app.changes == []

    async def test_first_editor_focused_and_detail_shown(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = screen_of(app)
            first = next(iter(screen.query(SettingRow)))
            assert app.focused is first.editor
            detail = str(screen.query_one("#config-detail", Static).content)
            assert first.spec.dotted in detail

    async def test_renders_at_80x24(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # Every row is laid out on a single line.
            for row in app.screen.query(SettingRow):
                assert row.size.height == 1


@pytest.mark.asyncio
class TestNavigation:
    async def test_tab_and_arrows_walk_editors_not_reset_buttons(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            editors = [row.editor for row in app.screen.query(SettingRow)]
            assert app.focused is editors[0]
            await pilot.press("tab")
            assert app.focused is editors[1]
            await pilot.press("down")
            assert app.focused is editors[2]
            await pilot.press("up")
            assert app.focused is editors[1]
            await pilot.press("shift+tab")
            assert app.focused is editors[0]

    async def test_down_on_select_moves_instead_of_opening(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await focus_editor(pilot, app, "autoswitch.strategy")
            await pilot.press("down")
            assert app.focused is not row.editor
            assert not row.editor.expanded

    async def test_focus_updates_detail_line(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await focus_editor(pilot, app, "ui.theme")
            detail = str(app.screen.query_one("#config-detail", Static).content)
            assert detail == spec_detail(SETTING_SPECS["ui.theme"])

    @pytest.mark.parametrize("key", ["escape", "q", "c"])
    async def test_close_keys_pop_back(self, tmp_path, key):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Focus a non-text editor so q/c are not typed into an Input.
            await focus_editor(pilot, app, "ui.theme")
            await pilot.press(key)
            await pilot.pause()
            assert not isinstance(app.screen, ConfigScreen)
            assert app.screen.query_one("#base")

    async def test_q_in_text_field_is_typed_not_close(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await focus_editor(pilot, app, "autoswitch.model")
            await pilot.press("q")
            assert isinstance(app.screen, ConfigScreen)
            assert row.editor.value == "q"


@pytest.mark.asyncio
class TestEditing:
    async def test_valid_float_persists_and_posts_change(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await type_into(pilot, app, "autoswitch.threshold", "80")
            assert read_file(tmp_path)["autoswitch"]["threshold"] == 80.0
            assert app.changes == ["autoswitch.threshold"]
            assert row.has_class("-custom")
            assert not row.has_class("-error")
            assert row.query_one(".setting-reset").visible

    async def test_valid_int_persists(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.unhealthyTicks", "5")
            assert read_file(tmp_path)["autoswitch"]["unhealthyTicks"] == 5
            assert app.changes == ["autoswitch.unhealthyTicks"]

    @pytest.mark.parametrize("text, message", [
        ("abc", "expects a number"),
        ("150", "between 50 and 99.9"),
        ("10", "between 50 and 99.9"),
    ])
    async def test_invalid_float_shows_error_and_writes_nothing(self, tmp_path, text, message):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await type_into(pilot, app, "autoswitch.threshold", text)
            assert read_file(tmp_path) is None
            assert app.changes == []
            assert row.has_class("-error")
            assert message in str(row.query_one(".setting-help", Static).content)
            detail = app.screen.query_one("#config-detail", Static)
            assert detail.has_class("-error")
            assert message in str(detail.content)
            # The bad text stays so it can be fixed in place.
            assert row.editor.value == text

    async def test_invalid_value_leaves_existing_file_unchanged(self, tmp_path):
        original = json.dumps({"autoswitch": {"threshold": 70}, "other": {"x": 1}})
        settings_path(tmp_path).write_text(original)
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.threshold", "999")
            assert settings_path(tmp_path).read_text() == original

    async def test_fixing_an_error_clears_it(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await type_into(pilot, app, "autoswitch.threshold", "x")
            assert row.has_class("-error")
            row = await type_into(pilot, app, "autoswitch.threshold", "85")
            assert not row.has_class("-error")
            assert str(row.query_one(".setting-help", Static).content) == row.spec.help
            assert read_file(tmp_path)["autoswitch"]["threshold"] == 85.0

    async def test_int_rejects_fraction(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await type_into(pilot, app, "autoswitch.unhealthyTicks", "2.5")
            assert row.has_class("-error")
            assert read_file(tmp_path) is None

    async def test_blur_commits_typed_value(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.cooldownSeconds", "120", submit=False)
            assert read_file(tmp_path) is None
            await pilot.press("tab")
            await pilot.pause()
            assert read_file(tmp_path)["autoswitch"]["cooldownSeconds"] == 120.0
            assert app.changes == ["autoswitch.cooldownSeconds"]

    async def test_close_commits_pending_valid_value(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.hysteresisPct", "20", submit=False)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigScreen)
            assert read_file(tmp_path)["autoswitch"]["hysteresisPct"] == 20.0

    async def test_close_with_invalid_pending_value_writes_nothing(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.hysteresisPct", "99", submit=False)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ConfigScreen)
            assert read_file(tmp_path) is None
            assert app.changes == []

    async def test_unchanged_value_is_a_noop(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.threshold", "90.0")
            assert read_file(tmp_path) is None
            assert app.changes == []

    async def test_string_persists_and_empty_unsets(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.model", "Fable")
            assert read_file(tmp_path)["autoswitch"]["model"] == "Fable"
            row = await type_into(pilot, app, "autoswitch.model", "")
            assert "autoswitch" not in read_file(tmp_path)
            assert not row.has_class("-custom")
            assert app.changes == ["autoswitch.model", "autoswitch.model"]

    async def test_empty_numeric_field_unsets(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"threshold": 70}}))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await type_into(pilot, app, "autoswitch.threshold", "")
            assert "autoswitch" not in read_file(tmp_path)
            assert row.editor.value == "90"
            assert app.changes == ["autoswitch.threshold"]

    async def test_bool_switch_persists(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await focus_editor(pilot, app, "autoswitch.includeApiKeyAccounts")
            await pilot.press("space")
            await pilot.pause()
            assert read_file(tmp_path)["autoswitch"]["includeApiKeyAccounts"] is True
            await pilot.press("space")
            await pilot.pause()
            assert read_file(tmp_path)["autoswitch"]["includeApiKeyAccounts"] is False
            assert app.changes == ["autoswitch.includeApiKeyAccounts"] * 2

    async def test_choice_select_persists(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await focus_editor(pilot, app, "autoswitch.strategy")
            await pilot.press("enter")  # open the menu
            await pilot.pause()
            assert row.editor.expanded
            await pilot.press("down", "enter")
            await pilot.pause()
            assert read_file(tmp_path)["autoswitch"]["strategy"] == "consume-first"
            assert app.changes == ["autoswitch.strategy"]
            assert row.has_class("-custom")

    async def test_theme_select_persists_to_ui_section(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = screen_of(app).row("ui.theme")
            row.editor.value = "dark"
            await pilot.pause()
            assert read_file(tmp_path)["ui"]["theme"] == "dark"
            assert app.changes == ["ui.theme"]

    async def test_unknown_keys_survive_a_write(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps(
            {"autoswitch": {"futureKey": 1}, "other": {"x": 2}}
        ))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await type_into(pilot, app, "autoswitch.threshold", "80")
        raw = read_file(tmp_path)
        assert raw["autoswitch"] == {"futureKey": 1, "threshold": 80.0}
        assert raw["other"] == {"x": 2}

    async def test_corrupt_file_shows_error_and_is_not_overwritten(self, tmp_path):
        settings_path(tmp_path).write_text("{not json")
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await type_into(pilot, app, "autoswitch.threshold", "80")
            assert row.has_class("-error")
            assert "not valid JSON" in str(row.query_one(".setting-help", Static).content)
        assert settings_path(tmp_path).read_text() == "{not json"
        assert app.changes == []


@pytest.mark.asyncio
class TestReset:
    async def test_ctrl_r_unsets_focused_setting(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"threshold": 70}}))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await focus_editor(pilot, app, "autoswitch.threshold")
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert "autoswitch" not in read_file(tmp_path)
            assert row.editor.value == "90"
            assert not row.has_class("-custom")
            assert app.changes == ["autoswitch.threshold"]

    async def test_reset_button_click_unsets(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = screen_of(app).row("ui.theme")
            await pilot.click(row.query_one(".setting-reset"))
            await pilot.pause()
            assert "ui" not in read_file(tmp_path)
            assert row.editor.value == "auto"
            assert app.changes == ["ui.theme"]

    async def test_reset_of_unset_setting_is_a_noop(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await focus_editor(pilot, app, "autoswitch.threshold")
            assert not row.query_one(".setting-reset").visible
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert read_file(tmp_path) is None
            assert app.changes == []

    async def test_reset_bool_restores_switch_without_rewriting(self, tmp_path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"includeApiKeyAccounts": True}})
        )
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = await focus_editor(pilot, app, "autoswitch.includeApiKeyAccounts")
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert row.editor.value is False
            # The programmatic switch flip must not write the default back.
            assert read_file(tmp_path) == {"schemaVersion": 1}
            assert app.changes == ["autoswitch.includeApiKeyAccounts"]
