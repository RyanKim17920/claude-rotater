"""Pilot tests for the TUI Config page (tui/config.py).

Every test drives the page only with the keys a user presses: ↑/↓ to move,
←/→ (or -/+) to change, digits / enter / esc for exact values, ctrl+r to reset.
No test focuses a widget or sets a value directly. ConfigView runs inside a
minimal host App over a tmp backup dir (with a prompt line showing the
settings hint, as on the main page), so nothing touches the real store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from claude_rotater.tui.prompt import PromptLine, hint_text
from claude_rotater.tui.theme import Palette

from claude_rotater.settings import SETTING_GROUPS, SETTING_SPECS, settings_path
from claude_rotater.tui.config import (
    HINT_KEYS,
    ConfigView,
    SettingsList,
    check_typed,
    spec_detail,
    stepped,
)


class Host(App):
    """ConfigView over a prompt line, recording every SettingChanged that
    reaches the app and whether the view asked to be closed."""

    def __init__(self, backup_dir: Path, *, delay: float = 0.0) -> None:
        super().__init__()
        self.backup_dir = backup_dir
        self.delay = delay
        self.changes: list[str] = []
        self.closed = False

    def compose(self) -> ComposeResult:
        view = ConfigView(self.backup_dir)
        view.NOTIFY_DELAY_S = self.delay
        yield view
        yield PromptLine()

    def on_mount(self) -> None:
        self.query_one(PromptLine).set_hint(hint_text(HINT_KEYS, Palette.DARK))
        self.query_one(ConfigView).open()

    def on_config_view_setting_changed(self, message: ConfigView.SettingChanged) -> None:
        self.changes.append(message.key)

    def on_config_view_closed(self, _message: ConfigView.Closed) -> None:
        self.closed = True


def read_file(backup_dir: Path) -> dict | None:
    path = settings_path(backup_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def section(backup_dir: Path, name: str) -> dict:
    return (read_file(backup_dir) or {}).get(name, {})


def lst(app: Host) -> SettingsList:
    assert not app.closed
    return app.query_one(SettingsList)


def footer(app: Host) -> str:
    return str(app.screen.query_one("#config-detail", Static).render())


def plain(app: Host) -> str:
    return lst(app).render().plain


def screen_lines(app: Host) -> list[str]:
    """What the terminal shows, line by line."""
    strips = app.screen._compositor.render_strips()
    return ["".join(seg.text for seg in strip).rstrip() for strip in strips]


async def go_to(pilot, app: Host, key: str) -> None:
    """Walk down from the top with ↓ until ``key`` is selected."""
    await pilot.press("home")
    for _ in range(len(lst(app).specs)):
        if lst(app).spec.dotted == key:
            return
        await pilot.press("down")
    raise AssertionError(f"{key} not reachable with ↓")


def ordered_specs():
    return [s for g, _t in SETTING_GROUPS for s in SETTING_SPECS.values() if s.group == g]


# -- pure helpers ---------------------------------------------------------------


def test_spec_detail_mentions_range_step_and_default():
    detail = spec_detail(SETTING_SPECS["autoswitch.threshold"])
    assert "autoswitch.threshold" in detail
    assert "50-99.9" in detail and "step 1" in detail and "default 90" in detail


@pytest.mark.parametrize("spec", list(SETTING_SPECS.values()), ids=lambda s: s.dotted)
def test_every_spec_steps_or_is_text(spec):
    """Generated UI guarantee: every kind has a ←/→ behaviour (or is text)."""
    if spec.kind == "string":
        assert stepped(spec, "x", 1) == "x"
        return
    new = stepped(spec, spec.default, 1)
    if spec.kind in ("float", "int"):
        assert spec.lo <= new <= spec.hi
        assert new == min(spec.default + spec.step_size, spec.hi)
    else:
        assert new != spec.default


def test_stepped_clamps_and_cycles():
    thr = SETTING_SPECS["autoswitch.threshold"]
    assert stepped(thr, 99.0, 1) == 99.9
    assert stepped(thr, 99.9, 1) == 99.9
    assert stepped(thr, 50.0, -1) == 50.0
    strat = SETTING_SPECS["autoswitch.strategy"]
    assert stepped(strat, "consume-first", 1) == "expiring-first"
    assert stepped(strat, "expiring-first", 1) == "best"
    assert stepped(strat, "best", -1) == "expiring-first"


def test_numbers_show_their_unit_and_labels_carry_none():
    thr = SETTING_SPECS["autoswitch.threshold"]
    assert "50-99.9%" in spec_detail(thr) and "default 90%" in spec_detail(thr)
    assert "pct" not in spec_detail(thr)
    for spec in SETTING_SPECS.values():
        assert "(s)" not in spec.label and "%" not in spec.label and "pct" not in spec.help
    cool = SETTING_SPECS["autoswitch.cooldownSeconds"]
    poll = SETTING_SPECS["autoswitch.pollSpareSeconds"]
    assert cool.unit == poll.unit == "s"  # every duration in the same style


def test_check_typed_refuses_out_of_range_with_the_range():
    thr = SETTING_SPECS["autoswitch.threshold"]
    assert check_typed(thr, "85") is None
    note = check_typed(thr, "9085")
    assert "9085" in note and "50-99.9" in note
    assert "50-99.9" in check_typed(thr, "10")
    assert check_typed(thr, "abc") is not None
    ticks = SETTING_SPECS["autoswitch.unhealthyTicks"]
    assert check_typed(ticks, "2.6") is not None  # not silently rounded


# -- layout ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestLayout:
    async def test_one_row_per_spec_under_group_headings(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            text = plain(app)
            for spec in SETTING_SPECS.values():
                assert spec.label in text
            headings = [t.upper() for _g, t in SETTING_GROUPS]  # small-caps headings
            positions = [text.index(h) for h in headings]
            assert positions == sorted(positions)
            assert [s.dotted for s in lst(app).specs] == [s.dotted for s in ordered_specs()]

    async def test_list_owns_focus_and_first_row_selected(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.focused is lst(app)
            assert lst(app).spec.dotted == ordered_specs()[0].dotted
            assert footer(app) == spec_detail(ordered_specs()[0])

    async def test_bool_renders_as_text_not_a_widget(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            line = next(l for l in plain(app).splitlines() if "Include API-key" in l)
            assert "○ off" in line

    async def test_defaults_and_custom_markers(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"threshold": 80}}))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            lines = plain(app).splitlines()
            thr = next(l for l in lines if "Threshold" in l)
            cool = next(l for l in lines if "Cooldown" in l)
            assert "80%" in thr and "custom" in thr and "300s" in cool
            assert "custom" not in cool and "default" not in cool  # a default row is quiet
            assert sum("custom" in l for l in lines) == 1

    @pytest.mark.parametrize("size", [(80, 24), (60, 20)])
    async def test_narrow_terminal_footer_wraps_full_help(self, tmp_path, size):
        app = Host(tmp_path)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            detail = app.screen.query_one("#config-detail", Static)
            full = spec_detail(lst(app).spec)
            assert detail.size.height >= 2  # wrapped, not cut
            assert footer(app) == full
            assert all(len(l) <= size[0] for l in plain(app).splitlines())
            hint = screen_lines(app)[-1]
            assert hint.rstrip().endswith("esc back") and "…" not in hint  # whole, not cut
            # no mid-sentence cut help in the rows: the footer carries it
            assert "…" not in plain(app)
            assert lst(app).spec.help not in plain(app)

    @pytest.mark.parametrize("size", [(140, 40), (160, 50)])
    async def test_rows_carry_no_help_the_footer_has_it_whole(self, tmp_path, size):
        app = Host(tmp_path)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            text = plain(app)
            assert all(spec.help not in text for spec in SETTING_SPECS.values())
            assert "…" not in text
            assert lst(app).spec.help in footer(app)

    async def test_section_heading_shows_with_its_first_row_at_60x20(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            first_profile = next(s for s in lst(app).specs if s.group == "profile")
            await go_to(pilot, app, first_profile.dotted)
            await pilot.pause()
            shown = "\n".join(screen_lines(app))
            assert "PROFILE" in shown and first_profile.label in shown

    async def test_selection_scrolls_into_view_at_80x24(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("end")
            await pilot.pause()
            body = app.screen.query_one("#config-body")
            line = lst(app).line_of(lst(app).cursor)
            assert body.scroll_offset.y <= line < body.scroll_offset.y + body.size.height


# -- navigation -------------------------------------------------------------------


@pytest.mark.asyncio
class TestNavigation:
    async def test_up_down_move_and_stop_at_ends(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            specs = ordered_specs()
            await pilot.press("up")
            assert lst(app).cursor == 0
            await pilot.press("down", "down")
            assert lst(app).spec.dotted == specs[2].dotted
            assert footer(app) == spec_detail(specs[2])
            await pilot.press(*["down"] * (len(specs) + 3))
            assert lst(app).cursor == len(specs) - 1
            await pilot.press("k")
            assert lst(app).cursor == len(specs) - 2

    async def test_down_crosses_section_heading(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            specs = ordered_specs()
            last_auto = max(i for i, s in enumerate(specs) if s.group == "autoswitch")
            await pilot.press(*["down"] * (last_auto + 1))
            assert lst(app).spec.group == specs[last_auto + 1].group != "autoswitch"

    @pytest.mark.parametrize("key", ["escape", "c"])
    async def test_close_keys_ask_to_go_back(self, tmp_path, key):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            assert app.closed

    async def test_click_selects_row(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            target = 3
            y = lst(app).line_of(target)
            await pilot.click(SettingsList, offset=(5, y))
            assert lst(app).cursor == target


# -- stepping with ←/→ -----------------------------------------------------------------


@pytest.mark.asyncio
class TestStepping:
    async def test_right_steps_threshold_and_saves_immediately(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("right")
            assert section(tmp_path, "autoswitch") == {"threshold": 91.0}
            await pilot.press("plus")
            assert section(tmp_path, "autoswitch") == {"threshold": 92.0}
            await pilot.press("minus", "left")
            assert read_file(tmp_path).get("autoswitch") is None  # back at default: key removed
            await pilot.pause()
            assert app.changes.count("autoswitch.threshold") == 4

    async def test_holding_right_clamps_at_max(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press(*["right"] * 40)
            assert section(tmp_path, "autoswitch") == {"threshold": 99.9}
            assert "‹ 99.9% ›" in plain(app)
            app.changes.clear()
            await pilot.press("right")
            await pilot.pause()
            assert app.changes == []  # at the bound: nothing written or posted

    async def test_holding_left_clamps_at_min(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press(*["left"] * 60)
            assert section(tmp_path, "autoswitch") == {"threshold": 50.0}

    async def test_seconds_step_by_thirty(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.cooldownSeconds")
            await pilot.press("right", "right")
            assert section(tmp_path, "autoswitch") == {"cooldownSeconds": 360.0}
            await pilot.press(*["left"] * 20)
            assert section(tmp_path, "autoswitch") == {"cooldownSeconds": 0.0}

    async def test_int_steps_stay_ints(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.unhealthyTicks")
            await pilot.press("right")
            value = section(tmp_path, "autoswitch")["unhealthyTicks"]
            assert value == 4 and isinstance(value, int)

    async def test_bool_flips_both_ways_and_enter_flips(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.includeApiKeyAccounts")
            await pilot.press("right")
            assert section(tmp_path, "autoswitch") == {"includeApiKeyAccounts": True}
            assert "● on" in plain(app)
            await pilot.press("left")
            assert read_file(tmp_path).get("autoswitch") is None
            await pilot.press("enter")
            assert section(tmp_path, "autoswitch") == {"includeApiKeyAccounts": True}

    async def test_choice_cycles(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.strategy")
            await pilot.press("right")
            assert section(tmp_path, "autoswitch") == {"strategy": "consume-first"}
            await pilot.press("right")
            assert section(tmp_path, "autoswitch") == {"strategy": "expiring-first"}
            await pilot.press("right")
            assert read_file(tmp_path).get("autoswitch") is None  # wrapped to default
            await go_to(pilot, app, "ui.theme")
            await pilot.press("left")
            assert section(tmp_path, "ui") == {"theme": "light"}

    async def test_unknown_keys_survive_a_step(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps(
            {"autoswitch": {"futureKey": 1}, "other": {"x": 2}}))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("left")
        raw = read_file(tmp_path)
        assert raw["autoswitch"] == {"futureKey": 1, "threshold": 89.0}
        assert raw["other"] == {"x": 2}

    async def test_corrupt_file_shows_error_and_is_not_overwritten(self, tmp_path):
        settings_path(tmp_path).write_text("{not json")
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("right")
            await pilot.pause()
            assert "not valid JSON" in footer(app)
            assert app.screen.query_one("#config-detail").has_class("-error")
        assert settings_path(tmp_path).read_text() == "{not json"
        assert app.changes == []


# -- exact values ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestExactEntry:
    async def test_digits_open_edit_and_enter_commits(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("8")
            assert lst(app).editing == "8"
            assert "8▏" in plain(app)
            await pilot.press("5", "enter")
            assert lst(app).editing is None
            assert section(tmp_path, "autoswitch") == {"threshold": 85.0}

    async def test_enter_opens_edit_with_current_value(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.cooldownSeconds")
            await pilot.press("enter")
            assert lst(app).editing == "300"
            await pilot.press("4", "5", "enter")
            assert section(tmp_path, "autoswitch") == {"cooldownSeconds": 45.0}

    async def test_typing_over_the_shown_value_replaces_it(self, tmp_path):
        # The reported bug: enter opened "90", typing 85 gave "9085", which
        # was clamped to 99.9 and saved. Typing must start a fresh value.
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("enter")
            assert lst(app).editing == "90"
            await pilot.press("8", "5")
            assert lst(app).editing == "85"
            await pilot.press("enter")
            assert lst(app).editing is None
            assert section(tmp_path, "autoswitch") == {"threshold": 85.0}

    async def test_backspace_on_the_shown_value_clears_it(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("enter", "backspace")
            assert lst(app).editing == ""
            await pilot.press("7", "5", "backspace")
            assert lst(app).editing == "7"

    async def test_out_of_range_is_refused_with_the_range_never_clamped(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("9", "0", "8", "5", "enter")
            await pilot.pause()
            assert read_file(tmp_path) is None  # nothing saved
            assert lst(app).editing == "9085"  # still editing, fix it in place
            assert "50-99.9" in footer(app) and "9085" in footer(app)
            assert app.screen.query_one("#config-detail").has_class("-error")
            await pilot.press("down")  # moving away does not sneak it in either
            assert read_file(tmp_path) is None
            await pilot.press("backspace", "backspace", "backspace", "enter")
            assert lst(app).editing == "9"
            await pilot.press("5", "enter")
            assert section(tmp_path, "autoswitch") == {"threshold": 95.0}

    async def test_garbage_keeps_editing_writes_nothing_then_esc_cancels(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("8", "x", "enter")
            await pilot.pause()
            assert lst(app).editing == "8x"
            assert app.screen.query_one("#config-detail").has_class("-error")
            assert read_file(tmp_path) is None
            await pilot.press("escape")
            assert lst(app).editing is None
            assert not app.closed  # esc cancelled, did not close
            assert "‹ 90% ›" in plain(app)
            assert footer(app) == spec_detail(lst(app).spec)

    async def test_down_commits_and_moves(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            start = lst(app).cursor
            await pilot.press("7", "0", "down")
            assert section(tmp_path, "autoswitch") == {"threshold": 70.0}
            assert lst(app).cursor == start + 1

    async def test_arrows_do_not_step_while_editing(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("7", "right", "left")
            assert lst(app).editing == "7"
            assert read_file(tmp_path) is None

    async def test_string_edit_and_empty_unsets(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.model")
            await pilot.press("right")  # strings do not step
            assert read_file(tmp_path) is None
            await pilot.press("enter", "F", "a", "b", "l", "e", "enter")
            assert section(tmp_path, "autoswitch") == {"model": "Fable"}
            await pilot.press("enter", *["backspace"] * 5, "enter")
            assert read_file(tmp_path).get("autoswitch") is None

    async def test_q_and_c_are_typed_while_editing(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.model")
            await pilot.press("enter", "q", "c")
            assert not app.closed
            assert lst(app).editing == "qc"

    async def test_letters_on_a_number_do_not_start_editing(self, tmp_path):
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("x")
            assert lst(app).editing is None


# -- reset and notifications ---------------------------------------------------------------


@pytest.mark.asyncio
class TestResetAndNotify:
    async def test_ctrl_r_resets(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"threshold": 70}}))
        app = Host(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert read_file(tmp_path).get("autoswitch") is None
            assert app.changes == ["autoswitch.threshold"]
            await pilot.press("ctrl+r")  # already default: no write, no message
            await pilot.pause()
            assert app.changes == ["autoswitch.threshold"]

    async def test_notifications_are_debounced_per_key(self, tmp_path):
        # The delay must outlast a stall between key presses on a loaded
        # machine (xdist), or the burst fires early and the test flakes.
        app = Host(tmp_path, delay=1.0)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press(*["right"] * 12)
            assert section(tmp_path, "autoswitch") == {"threshold": 99.9}  # written at once
            assert app.changes == []
            for _ in range(50):
                await pilot.pause(0.1)
                if app.changes:
                    break
            await pilot.pause(0.3)  # no second notification trails the first
            assert app.changes == ["autoswitch.threshold"]

    async def test_closing_flushes_pending_notifications(self, tmp_path):
        app = Host(tmp_path, delay=10.0)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await go_to(pilot, app, "autoswitch.threshold")
            await pilot.press("right", "down", "right", "escape")
            await pilot.pause()
            assert app.closed
            assert sorted(app.changes) == sorted(
                ["autoswitch.threshold", ordered_specs()[1].dotted]
            )
