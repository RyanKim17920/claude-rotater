"""Pilot tests for the prompt line (tui/prompt.py), alone and on the main page.

The widget tests drive a bare host: a focusable list and the prompt line.
The page tests drive the real app at 60/80/120/160 columns in both themes:
every question, entry and result must sit whole on the bottom row.
"""

from __future__ import annotations

import time

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import ListItem, ListView, Static

from claude_rotater.tui.prompt import Choice, PromptLine

from .test_tui import (
    FakeSwitcher,
    burn_history,
    make_app,
    prompt_plain,
    settle,
    three_accounts,
    with_history,
)


class Host(App):
    """A list that owns the keyboard, a prompt line under it, and a record
    of every key the list's bindings saw."""

    BINDINGS = [("z", "seen('z')")]

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[object] = []
        self.seen: list[str] = []

    def compose(self) -> ComposeResult:
        yield ListView(ListItem(Static("row")), id="list")
        yield PromptLine(id="prompt")

    def on_mount(self) -> None:
        self.query_one("#list").focus()
        self.line.set_hint("idle hint")

    @property
    def line(self) -> PromptLine:
        return self.query_one(PromptLine)

    def action_seen(self, key: str) -> None:
        self.seen.append(key)


def shown(app: Host) -> str:
    return app.line.render().plain


@pytest.mark.asyncio
class TestPromptLineWidget:
    async def test_idle_shows_the_hint_and_takes_no_keys(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            assert shown(app) == "idle hint"
            assert not app.line.can_focus and not app.line.is_open
            await pilot.press("z")
            assert app.seen == ["z"]  # keys reach the page

    async def test_ask_answers_by_key_and_swallows_the_rest(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.ask("pick:", (Choice("b", "blue", "B"), Choice("r", "red", "R")), app.answers.append)
            await pilot.pause()
            assert shown(app) == "pick:  b blue  r red  esc"
            assert app.focused is app.line
            await pilot.press("z", "x")  # not answers: swallowed, nothing fires
            assert app.seen == [] and app.line.is_open
            await pilot.press("r")
            assert app.answers == ["R"] and not app.line.is_open
            assert app.focused is app.query_one("#list")  # the keyboard goes back
            assert shown(app) == "idle hint"

    @pytest.mark.parametrize("key", ["escape", "n"])
    async def test_esc_or_n_cancels_a_question(self, key):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.ask("pick:", (Choice("b", "blue", "B"),), app.answers.append)
            await pilot.pause()
            await pilot.press(key)
            assert app.answers == [None] and not app.line.is_open

    async def test_confirm_is_true_only_on_y(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            for key, expected in (("y", True), ("n", False), ("escape", False)):
                app.line.confirm("go?", app.answers.append, yes="go")
                await pilot.pause()
                assert shown(app) == "go?  y go  n no  esc"
                await pilot.press(key)
                assert app.answers[-1] is expected

    async def test_read_types_edits_and_submits(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.read("name:", app.answers.append, note="optional")
            await pilot.pause()
            assert shown(app) == "name: ▌  optional  ⏎ ok · esc cancel"
            await pilot.press(*"abx", "backspace", "c")
            assert shown(app).startswith("name: abc▌")
            await pilot.press("enter")
            assert app.answers == ["abc"]

    async def test_read_esc_gives_none_and_enter_on_nothing_gives_empty(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.read("name:", app.answers.append)
            await pilot.pause()
            await pilot.press("a", "escape")
            app.line.read("name:", app.answers.append)
            await pilot.pause()
            await pilot.press("enter")
            assert app.answers == [None, ""]

    async def test_secret_is_dots_and_paste_lands_whole(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.read("token:", app.answers.append, secret=True)
            await pilot.pause()
            app.line.post_message(events.Paste("sk-ant-oat01-abc\n"))
            await pilot.pause()
            assert shown(app).startswith("token: " + "•" * 16 + "▌")
            await pilot.press("d")
            assert "17 chars" in shown(app) and "sk-ant" not in shown(app)
            await pilot.press("enter")
            assert app.answers == ["sk-ant-oat01-abcd"]

    async def test_a_follow_up_keeps_the_keyboard(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            def first(value):
                app.answers.append(value)
                app.line.read("second:", app.answers.append)

            app.line.read("first:", first)
            await pilot.pause()
            await pilot.press("a", "enter")
            await pilot.pause()
            assert app.line.is_open and app.focused is app.line
            await pilot.press("b", "enter")
            await pilot.pause()
            assert app.answers == ["a", "b"] and app.focused is app.query_one("#list")

    async def test_say_shows_briefly_then_the_hint_returns(self, monkeypatch):
        from claude_rotater.tui import prompt

        monkeypatch.setitem(prompt.SAY_SECONDS, "error", 1.0)
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.say("it broke\n  badly", "error")
            await pilot.pause()
            assert shown(app) == "it broke badly"  # always one line
            assert app.line.message == "it broke badly"
            await pilot.pause(1.3)
            assert shown(app) == "idle hint" and app.line.message is None

    async def test_a_result_waits_for_an_open_question(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.confirm("go?", app.answers.append)
            await pilot.pause()
            app.line.say("done elsewhere")
            await pilot.pause()
            assert shown(app).startswith("go?")  # the question is not overwritten
            await pilot.press("n")
            await pilot.pause()
            assert shown(app) == "done elsewhere"

    async def test_a_click_elsewhere_drops_the_question(self):
        app = Host()
        async with app.run_test(size=(80, 10)) as pilot:
            app.line.confirm("go?", app.answers.append)
            await pilot.pause()
            app.query_one("#list").focus()
            await pilot.pause()
            assert not app.line.is_open and app.answers == [False]

    @pytest.mark.parametrize("width", [30, 60])
    async def test_narrow_line_keeps_the_answers_and_the_typing(self, width):
        app = Host()
        async with app.run_test(size=(width, 10)) as pilot:
            app.line.confirm("remove a.very.long.address@example-company.com and its login?",
                             app.answers.append, yes="remove")
            await pilot.pause()
            text = shown(app)
            assert len(text) <= width - 2 and text.endswith("y remove  n no  esc")
            assert "…" in text
            await pilot.press("escape")
            app.line.read("email label:", app.answers.append, note="optional, ⏎ skips")
            await pilot.pause()
            await pilot.press(*"someone.with.a.long.name@example.com")
            text = shown(app)
            assert len(text) <= width - 2 and text.rstrip().endswith("example.com▌")


THEMES = ["dark", "light"]
SIZES = [(60, 20), (80, 24), (120, 40), (160, 50)]


def page(app) -> list[str]:
    strips = app.screen._compositor.render_strips()
    return ["".join(seg.text for seg in strip).rstrip() for strip in strips]


def themed_app(tmp_path, theme_name: str, fake: FakeSwitcher | None = None):
    from claude_rotater.settings import set_setting

    set_setting(tmp_path, "ui.theme", theme_name)
    fake = fake or three_accounts(tmp_path)
    with_history(fake, burn_history(time.time()))
    return make_app(fake)


def bottom(app, size) -> str:
    """The bottom row of the terminal, which is the prompt line."""
    lines = page(app)
    assert len(lines) == size[1] and all(len(line) <= size[0] for line in lines)
    return lines[-1].strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", THEMES)
@pytest.mark.parametrize("size", SIZES)
class TestPromptLineOnThePage:
    async def test_idle_hint_on_the_bottom_row(self, tmp_path, size, theme_name):
        app = themed_app(tmp_path, theme_name)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            assert bottom(app, size) == "⏎ switch · a add · ? keys"

    async def test_confirm_yes_no_and_esc(self, tmp_path, size, theme_name):
        app = themed_app(tmp_path, theme_name)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("down", "x")
            await pilot.pause()
            assert bottom(app, size).endswith("y remove  n no  esc")
            await pilot.press("escape")
            await pilot.pause()
            assert bottom(app, size) == "⏎ switch · a add · ? keys"
            await pilot.press("x", "n", "x", "y")
            await settle(pilot)
            assert [c for c in app.switcher.calls if c[0] == "remove"] == [("remove", "2", True)]

    async def test_add_chooser_and_token_entry(self, tmp_path, size, theme_name):
        app = themed_app(tmp_path, theme_name)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            assert bottom(app, size) == "add:  b browser login  c current login  t paste token  esc"
            await pilot.press("t", *"sk-ant-oat-y")
            await pilot.pause()
            line = bottom(app, size)
            assert line.startswith("token: ") and "▌" in line and "sk-ant" not in line
            await pilot.press("enter", "enter", "enter")  # no label, next free slot
            await settle(pilot)
            assert ("add_token", "sk-ant-oat-y", None, None, True) in app.switcher.calls

    async def test_error_is_shown_inline(self, tmp_path, size, theme_name):
        from claude_rotater.exceptions import ClaudeSwitchError

        fake = three_accounts(tmp_path)

        def refuse(number, **_kw):
            raise ClaudeSwitchError("account 2 is busy")

        fake.switch_to = refuse
        app = themed_app(tmp_path, theme_name, fake)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("down", "enter")
            await settle(pilot)
            line = bottom(app, size)
            full = "switch to account 2 failed: account 2 is busy"
            assert line == full or (line.endswith("…") and full.startswith(line[:-1]))
            assert len(app.screen_stack) == 2  # the default screen and the main page: no popup

    async def test_keys_panel_and_config_in_place(self, tmp_path, size, theme_name):
        app = themed_app(tmp_path, theme_name)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            screen = app.screen
            await pilot.press("question_mark")
            await pilot.pause()
            assert screen.keys_shown and "this list" in "\n".join(page(app))
            await pilot.press("question_mark", "c")
            await pilot.pause()
            assert app.screen is screen and screen.config_shown
            assert bottom(app, size).endswith("esc back")
            assert page(app)[0].strip().startswith("rota")  # the same header
            await pilot.press("escape")
            await pilot.pause()
            assert not screen.config_shown
            assert bottom(app, size) == "⏎ switch · a add · ? keys"
