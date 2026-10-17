"""The prompt line: the one bottom row where rota talks and listens.

There are no popups. Everything that needs an answer or reports an outcome
happens on this single line, like a shell prompt:

- idle: a short dim hint (``⏎ switch · a add · ? keys``);
- :meth:`PromptLine.ask`: a question and its one-key answers
  (``remove work@y.com?  y yes  n no``); a listed key answers at once, esc
  (and ``n`` where it is not an answer of its own) cancels;
- :meth:`PromptLine.read`: a line of text (``token: ••••▌``), typed or
  pasted; enter submits, esc cancels;
- :meth:`PromptLine.say`: a result or an error, shown for a few seconds,
  then the hint comes back. A result that lands while a question is open
  waits until it is answered.

While a question or an entry is open the line holds the keyboard: every key
goes to it, so a stray letter never fires a main-page action. ctrl+c still
quits (it is an app-level priority binding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual import events
from textual.timer import Timer
from textual.widget import Widget

from claude_rotater.tui.theme import Palette

# How long a result stays on the line, by role; errors get longer to read.
SAY_SECONDS = {"info": 4.0, "ok": 4.0, "warn": 8.0, "error": 10.0}
SECRET_DOT = "•"
_SECRET_SHOWN = 16  # dots drawn before the length is given as a number
CURSOR = "▌"


@dataclass(frozen=True)
class Choice:
    """One answer to :meth:`PromptLine.ask`: the key, what it means, and the
    value the callback gets."""

    key: str
    words: str
    value: object


@dataclass
class _Ask:
    question: str
    choices: tuple[Choice, ...]
    done: Callable[[object | None], None]


@dataclass
class _Read:
    label: str
    done: Callable[[str | None], None]
    secret: bool
    note: str = ""
    buffer: str = ""


class PromptLine(Widget):
    """The bottom line (see the module docstring)."""

    DEFAULT_CSS = """
    PromptLine {
        dock: bottom;
        height: 1;
        padding: 0 1;
    }
    """

    can_focus = False  # only while a question or an entry is open

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.hint: Text | str = ""
        self._open: _Ask | _Read | None = None
        self._message: tuple[str, str] | None = None  # (text, role)
        self._pending: tuple[str, str] | None = None  # said while a prompt was open
        self._timer: Timer | None = None
        self._return_focus: Widget | None = None

    # -- state -------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """A question or an entry is waiting for the user."""
        return self._open is not None

    @property
    def message(self) -> str | None:
        """The result currently shown, if any."""
        return self._message[0] if self._message else None

    def set_hint(self, hint: Text | str) -> None:
        self.hint = hint
        self.refresh()

    # -- the three things the line does --------------------------------------------

    def ask(
        self,
        question: str,
        choices: tuple[Choice, ...],
        done: Callable[[object | None], None],
    ) -> None:
        """Ask ``question``; ``done`` gets the picked choice's value, or None
        on esc. A question already open is cancelled first."""
        self._take(_Ask(question, tuple(choices), done))

    def confirm(self, question: str, done: Callable[[bool], None], *, yes: str = "yes") -> None:
        """``question?  y <yes>  n no``: ``done(True)`` only on ``y``."""
        self.ask(
            question,
            (Choice("y", yes, True), Choice("n", "no", False)),
            lambda value: done(value is True),
        )

    def read(
        self,
        label: str,
        done: Callable[[str | None], None],
        *,
        secret: bool = False,
        note: str = "",
    ) -> None:
        """Read one line of text after ``label``; ``done`` gets it (stripped,
        possibly empty) on enter, or None on esc. ``secret`` draws dots."""
        self._take(_Read(label, done, secret, note))

    def say(self, text: str, role: str = "info") -> None:
        """Show a result for a few seconds (``role``: info / ok / warn / error).
        While a prompt is open it waits for the prompt to close."""
        text = " ".join(text.split())  # one line, whatever was captured
        if self._open is not None:
            self._pending = (text, role)
            return
        self._show(text, role)

    def cancel(self) -> None:
        """Close an open prompt as if esc was pressed."""
        if self._open is not None:
            self._finish(None)

    # -- internals ---------------------------------------------------------------

    def _show(self, text: str, role: str) -> None:
        self._message = (text, role)
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(SAY_SECONDS.get(role, 4.0), self._clear_message)
        self.refresh()

    def _clear_message(self) -> None:
        self._message = None
        self._timer = None
        self.refresh()

    def _take(self, prompt: _Ask | _Read) -> None:
        if self._open is not None:
            self._finish(None)
        self._open = prompt
        self._message = None
        focused = self.app.focused
        self._return_focus = focused if focused is not self else self._return_focus
        self.can_focus = True
        self.focus()
        self.refresh()

    def _finish(self, value) -> None:
        prompt, self._open = self._open, None
        if prompt is not None:
            prompt.done(value)
        if self._open is not None:  # the answer asked a follow-up: keep the keyboard
            self.refresh()
            return
        self.can_focus = False
        back, self._return_focus = self._return_focus, None
        if back is not None and back.is_attached and back.display:
            back.focus()
        elif self.has_focus:
            self.screen.set_focus(None)
        pending, self._pending = self._pending, None
        if pending is not None:
            self._show(*pending)
        self.refresh()

    def on_key(self, event: events.Key) -> None:
        prompt = self._open
        if prompt is None:
            return
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self._finish(None)
            return
        if isinstance(prompt, _Ask):
            for choice in prompt.choices:
                if event.key == choice.key or event.character == choice.key:
                    self._finish(choice.value)
                    return
            if event.key == "n":  # "no" without a choice of its own still cancels
                self._finish(None)
            return
        if event.key == "enter":
            self._finish(prompt.buffer.strip())
        elif event.key in ("backspace", "ctrl+h"):
            prompt.buffer = prompt.buffer[:-1]
        elif event.key == "ctrl+u":
            prompt.buffer = ""
        elif event.character is not None and event.is_printable:
            prompt.buffer += event.character
        self.refresh()

    def on_paste(self, event: events.Paste) -> None:
        if isinstance(self._open, _Read):
            event.stop()
            self._open.buffer += "".join(event.text.split())
            self.refresh()

    def on_blur(self) -> None:
        # A click elsewhere took the keyboard: the question is dropped, never
        # left open behind the user's back.
        if self._open is not None and self.app.focused is not self:
            self._return_focus = None
            self._finish(None)

    # -- rendering ---------------------------------------------------------------

    def render(self) -> Text:
        """One line that always fits: the answers of a question and the text
        being typed stay whole; the question, a note and the key reminder
        give way first."""
        palette = Palette.from_theme(self.app.current_theme)
        width = self.content_size.width or 80
        prompt = self._open
        if isinstance(prompt, _Ask):
            answers = Text()
            for choice in prompt.choices:
                answers.append("  ")
                answers.append(choice.key, style=f"bold {palette.accent}")
                answers.append(f" {choice.words}", style=palette.muted)
            answers.append("  esc", style=palette.muted)
            text = Text(prompt.question, style=f"bold {palette.foreground}", no_wrap=True)
            text.truncate(max(0, width - answers.cell_len), overflow="ellipsis")
            return text + answers
        if isinstance(prompt, _Read):
            text = Text(no_wrap=True)
            text.append(f"{prompt.label} ", style=f"bold {palette.foreground}")
            room = max(1, width - text.cell_len - len(CURSOR))
            text.append(_tail(_shown_buffer(prompt), room), style=palette.foreground)
            text.append(CURSOR, style=palette.accent)
            for extra in (prompt.note, "⏎ ok · esc cancel"):
                if extra and text.cell_len + 2 + len(extra) <= width:
                    text.append(f"  {extra}", style=palette.muted)
            return text
        text = Text(no_wrap=True, overflow="ellipsis")
        if self._message is not None:
            words, role = self._message
            style = {
                "ok": palette.sev_ok,
                "warn": palette.sev_warn,
                "error": f"bold {palette.sev_crit}",
            }.get(role, palette.foreground)
            text.append(words, style=style)
        else:
            hint = self.hint
            text.append(hint if isinstance(hint, Text) else Text(hint, style=palette.muted))
        text.truncate(width, overflow="ellipsis")
        return text


def _tail(shown: str, room: int) -> str:
    """The end of what is being typed, where the cursor is."""
    return shown if len(shown) <= room else "…" + shown[len(shown) - room + 1 :]


def _shown_buffer(prompt: _Read) -> str:
    if not prompt.secret:
        return prompt.buffer
    n = len(prompt.buffer)
    if n <= _SECRET_SHOWN:
        return SECRET_DOT * n
    return f"{SECRET_DOT * _SECRET_SHOWN}… {n} chars"


def hint_text(keys: tuple[tuple[str, str], ...], palette: Palette) -> Text:
    """``⏎ switch · a add · ? keys``: all dim, the keys in bold."""
    text = Text(no_wrap=True, overflow="ellipsis")
    for i, (key, words) in enumerate(keys):
        if i:
            text.append(" · ", style=palette.muted)
        text.append(key, style=f"bold {palette.muted}")
        text.append(f" {words}", style=palette.muted)
    return text
