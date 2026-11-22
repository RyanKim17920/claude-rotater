"""Run the CLI's own interactive login in a pseudo-terminal, so the TUI never
hands the terminal over (TUI ``l``).

The CLI's ``auth login`` prints a sign-in URL, opens the browser itself and
waits: either its local callback receives the sign-in, or the page shows a
code to paste at ``Paste code here if prompted >``. :class:`PtyLogin` runs it
with a pty of its own (it wants a terminal: raw mode, ctrl+c), reads what it
prints, and reports :class:`LoginProgress` (the URL, whether a code is asked
for, success, the CLI's last problem line). The TUI shows that on its prompt
line and writes a pasted code back with :meth:`PtyLogin.send_code`.

This is the official login, unchanged: rota never talks to the sign-in
servers itself.

Cancel sends ctrl+c (the CLI exits 130 on its own), then SIGTERM and finally
SIGKILL to the child's process group, which is a new session so nothing of
the user's terminal or of rota is in it.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable

from claude_rotater.exceptions import LoginCancelled, LoginError

LOGIN_TIMEOUT_S = 600.0
CANCEL_GRACE_S = 2.0
_POLL_S = 0.1
_MAX_OUTPUT = 64 * 1024  # what the login prints is small; never hold more
# Wide enough that the CLI never wraps the URL itself.
_PTY_ROWS, _PTY_COLS = 50, 1000

PASTE_PROMPT = "Paste code here if prompted"
_URL_HEADERS = ("visit:", "Sign in at:")
_SUCCESS = "Login successful."
_SIGNING_IN = "Signing in"
_MASK = "•••"

# Complete escape sequences: OSC (ended by BEL or ST), CSI, then any other
# two-byte escape. A lone ESC left after these is a sequence still arriving.
_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[^\[\]]"
)
_CONTROLS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL = re.compile(rf"https://[{_URL_CHARS}]+")
_URL_PIECE = re.compile(rf"[{_URL_CHARS}]+")


def pty_supported() -> bool:
    """Pseudo-terminals exist here (every Unix; not Windows)."""
    return sys.platform != "win32" and hasattr(os, "openpty")


def clean_output(raw: str) -> str:
    """Terminal output as plain text: escape sequences and control
    characters removed, line ends normalised to ``\\n``. An escape sequence
    cut off at the end (more is on its way) is dropped with what follows."""
    text = _ESCAPES.sub("", raw)
    cut = text.find("\x1b")
    if cut != -1:
        text = text[:cut]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROLS.sub("", text)


@dataclass(frozen=True)
class LoginProgress:
    """How far the login has got, read off the CLI's output."""

    url: str | None = None
    asks_for_code: bool = False
    signing_in: bool = False
    succeeded: bool = False
    message: str = ""  # the CLI's latest problem/error line, codes masked


def _find_url(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        match = _URL.search(line)
        if match is None:
            continue
        url = match.group(0)
        if match.end() == len(line.rstrip()):
            # A hard-wrapped URL goes on over the next lines: pieces with no
            # spaces, made of URL characters only.
            for nxt in lines[i + 1 :]:
                piece = nxt.strip()
                if not piece or not _URL_PIECE.fullmatch(piece):
                    break
                url += piece
        return url
    return None


def _is_chatter(line: str, url: str | None) -> bool:
    """A line the CLI always prints on the way (not a problem to show)."""
    s = line.strip()
    if not s:
        return True
    if s.startswith(("Opening browser", _SIGNING_IN, _SUCCESS, PASTE_PROMPT)):
        return True
    if s.endswith(_URL_HEADERS) or s.endswith(">"):
        return True
    if url is not None and (s in url or _URL.fullmatch(s)):
        return True
    return False


def parse_progress(text: str, *, secrets: Iterable[str] = ()) -> LoginProgress:
    """Read a :class:`LoginProgress` off the cleaned output so far.
    ``secrets`` (codes the user pasted, echoed back by the terminal) never
    appear in the message."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, _MASK)
    lines = text.split("\n")
    url = _find_url(lines)
    succeeded = _SUCCESS in text
    message = ""
    for line in reversed(lines):
        if not _is_chatter(line, url) and line.strip() != _MASK:
            message = line.strip()
            break
    return LoginProgress(
        url=url,
        asks_for_code=PASTE_PROMPT in text or "Sign in at:" in text,
        signing_in=_SIGNING_IN in text and not succeeded,
        succeeded=succeeded,
        message=message,
    )


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass  # a default-sized terminal still works; the URL parser joins wraps


class PtyLogin:
    """One run of the CLI's login in its own pty. :meth:`run` blocks (call it
    from a worker thread); :meth:`send_code` and :meth:`cancel` are safe from
    any thread."""

    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        *,
        on_progress: Callable[[LoginProgress], None],
        timeout_s: float = LOGIN_TIMEOUT_S,
        grace_s: float = CANCEL_GRACE_S,
    ) -> None:
        self.argv = list(argv)
        self.env = env
        self._on_progress = on_progress
        self._timeout_s = timeout_s
        self._grace_s = grace_s
        self._lock = threading.Lock()
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._secrets: list[str] = []
        self._raw = ""
        self.progress = LoginProgress()
        self.returncode: int | None = None

    @property
    def output(self) -> str:
        """Everything printed so far, cleaned (pasted codes still in it)."""
        return clean_output(self._raw)

    # -- from any thread ------------------------------------------------------

    def send_code(self, code: str) -> bool:
        """Type ``code`` and enter at the CLI's paste prompt. False when the
        login is not running."""
        code = code.strip()
        with self._lock:
            if self._master is None or not code:
                return False
            self._secrets.append(code)
            try:
                os.write(self._master, code.encode() + b"\r")
            except OSError:
                return False
        return True

    def cancel(self) -> None:
        """Ask the login to stop; :meth:`run` then raises LoginCancelled."""
        self._cancel.set()

    # -- the worker -----------------------------------------------------------

    def run(self) -> None:
        """Run the login to its end. Returns on success (exit 0); raises
        LoginCancelled on cancel or ctrl+c, LoginError on a failure, a
        timeout, or when the CLI cannot start."""
        if self._cancel.is_set():
            raise LoginCancelled("Login cancelled; nothing was stored.")
        master, slave = os.openpty()
        _set_winsize(slave, _PTY_ROWS, _PTY_COLS)
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self.env,
                start_new_session=True,  # its own group, no tie to our terminal
                close_fds=True,
            )
        except OSError as e:
            os.close(master)
            raise LoginError(f"Could not run '{self.argv[0]}' ({e}). Is the CLI installed and on PATH?") from e
        finally:
            os.close(slave)
        with self._lock:
            self._master = master
        timed_out = False
        try:
            timed_out = self._pump(master)
        finally:
            self._end_group()
            with self._lock:
                self._master = None
                os.close(master)
        self.returncode = self._proc.returncode
        self._outcome(timed_out)

    def _pump(self, master: int) -> bool:
        """Read until the CLI exits, escalating a cancel. True on timeout."""
        assert self._proc is not None
        deadline = time.monotonic() + self._timeout_s
        cancel_at: float | None = None
        timed_out = False
        sent_term = False
        open_fd = True
        while True:
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                self._cancel.set()
            if self._cancel.is_set():
                if cancel_at is None:
                    cancel_at = now
                    self._write(master, b"\x03")  # the CLI's own ctrl+c: exit 130
                elif now - cancel_at >= 2 * self._grace_s:
                    self._signal_group(signal.SIGKILL)
                elif now - cancel_at >= self._grace_s and not sent_term:
                    sent_term = True
                    self._signal_group(signal.SIGTERM)
            if open_fd:
                ready, _, _ = select.select([master], [], [], _POLL_S)
                if ready:
                    if self._read(master):
                        continue
                    open_fd = False  # every holder of the terminal has closed it
            else:
                time.sleep(_POLL_S)
            if self._proc.poll() is not None:
                while open_fd and select.select([master], [], [], 0)[0] and self._read(master):
                    pass
                return timed_out

    def _read(self, master: int) -> bool:
        try:
            data = os.read(master, 4096)
        except OSError:  # EIO: the other side is gone
            return False
        if not data:
            return False
        self._raw = (self._raw + data.decode("utf-8", "replace"))[-_MAX_OUTPUT:]
        progress = parse_progress(self.output, secrets=self._secrets)
        if progress != self.progress:
            self.progress = progress
            self._on_progress(progress)
        return True

    def _write(self, master: int, data: bytes) -> None:
        try:
            os.write(master, data)
        except OSError:
            pass

    def _signal_group(self, sig: int) -> None:
        # Only while the leader is unreaped: its pid then still names this
        # group and cannot have been reused.
        assert self._proc is not None
        if self._proc.poll() is not None:
            return
        try:
            os.killpg(self._proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _end_group(self) -> None:
        """Never leave the CLI running behind an exception in the pump."""
        if self._proc is None or self._proc.poll() is not None:
            return
        self._signal_group(signal.SIGKILL)
        self._proc.wait()

    def _outcome(self, timed_out: bool) -> None:
        rc = self.returncode
        if timed_out:
            minutes = max(1, round(self._timeout_s / 60))
            raise LoginError(f"Login timed out after {minutes} min; nothing was stored.")
        if self._cancel.is_set() or rc == 130:
            raise LoginCancelled("Login cancelled; nothing was stored.")
        if rc == 0:
            return
        detail = self.progress.message or f"the CLI exited with status {rc}"
        raise LoginError(f"Login did not complete: {detail}; nothing was stored.")
