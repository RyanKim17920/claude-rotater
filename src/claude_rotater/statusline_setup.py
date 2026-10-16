"""Wire ``rota feed`` into Claude Code's statusline, for any user and profile.

Claude Code runs ``statusLine.command`` from ``settings.json`` in its config
home (``$CLAUDE_CONFIG_DIR``, else ``~/.claude``) and pipes it a JSON payload
on every refresh. :func:`install` makes that payload reach ``rota feed``:

- With a statusline already configured, its command is wrapped so the
  payload goes to ``rota feed`` in the background and, byte for byte, to the
  original command, whose output stays the statusline. The original command
  is kept verbatim inside the wrapper, so :func:`uninstall` restores it
  exactly, with no side file to lose::

      { p=$(cat); printf '%s' "$p" | rota feed >/dev/null 2>&1 & printf '%s' "$p"; } | { <original>
      } # rota-statusline

- With none, a minimal one is installed: ``rota feed --line``, which records
  the payload and prints a short usage line.

Writes are careful with a file that belongs to the user: settings.json is
copied into rota's data dir first, rewritten atomically (temp file and
rename beside the resolved target, so a symlinked settings.json keeps its
link), with its other keys, key order, indentation and trailing newline
kept. An unparsable settings.json is never touched. Both operations are
idempotent. The wrapper needs a POSIX shell, which is what Claude Code runs
statusline commands with on macOS and Linux.

Stdlib only and import-light: the TUI calls :func:`status` on refresh.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MARKER = "rota-statusline"
_WRAP_HEAD = "{ p=$(cat); printf '%s' \"$p\" | "
_WRAP_MID = " >/dev/null 2>&1 & printf '%s' \"$p\"; } | { "
_WRAP_TAIL = "\n} # " + MARKER
_DEFAULT_TAIL = " --line # " + MARKER
# A statusline the user wired to the feed by hand counts as installed.
_MANUAL_HINTS = ("rota feed", "claude_rotater feed")

BACKUP_DIRNAME = "statusline-backups"
MAX_BACKUPS = 10

NOT_INSTALLED = "not-installed"
INSTALLED = "installed"
FOREIGN = "foreign"


class StatuslineSetupError(Exception):
    """settings.json could not be read or written; nothing was changed."""


@dataclass(frozen=True)
class StatuslineStatus:
    install: str  # NOT_INSTALLED | INSTALLED | FOREIGN (someone else's statusline)
    state: str  # "live" (feed recent) | "stale" (installed, feed quiet) | "off"
    settings_path: Path
    command: str | None  # the current statusLine command, if any
    last_feed_at: float | None  # mtime of the feed file, if any

    @property
    def installed(self) -> bool:
        return self.install == INSTALLED


# -- paths ----------------------------------------------------------------------


def settings_path() -> Path:
    """Claude Code's user settings file for the current profile."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(env) if env else Path.home() / ".claude"
    return home / "settings.json"


def _backup_root(backup_dir: Path | None) -> Path:
    if backup_dir is not None:
        return Path(backup_dir)
    from claude_rotater import paths

    return paths.get_backup_root()


def feed_command() -> str:
    """The shell words that run ``rota feed`` for this install and data dir."""
    exe = shutil.which("rota")
    words = [exe] if exe else [sys.executable, "-m", "claude_rotater"]
    command = " ".join(shlex.quote(w) for w in words) + " feed"
    rota_home = os.environ.get("ROTA_HOME")
    if rota_home:
        command = f"ROTA_HOME={shlex.quote(rota_home)} {command}"
    return command


# -- command shapes -----------------------------------------------------------------


def wrap(original: str, feed: str | None = None) -> str:
    return _WRAP_HEAD + (feed or feed_command()) + _WRAP_MID + original + _WRAP_TAIL


def default_command(feed: str | None = None) -> str:
    return (feed or feed_command()) + _DEFAULT_TAIL


def unwrap(command: str) -> str | None:
    """The original command inside a wrapper :func:`wrap` made, else None."""
    if not (command.startswith(_WRAP_HEAD) and command.endswith(_WRAP_TAIL)):
        return None
    mid = command.find(_WRAP_MID, len(_WRAP_HEAD))
    if mid < 0:
        return None
    return command[mid + len(_WRAP_MID) : -len(_WRAP_TAIL)]


def _is_default(command: str) -> bool:
    return command.endswith(_DEFAULT_TAIL)


def _classify(command: str | None) -> str:
    if not command:
        return NOT_INSTALLED
    if command.endswith(MARKER) or any(h in command for h in _MANUAL_HINTS):
        return INSTALLED
    return FOREIGN


# -- settings.json I/O ------------------------------------------------------------


def _read(path: Path) -> tuple[dict, str | None]:
    """``(settings, raw_text)``; ``({}, None)`` when the file does not exist."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as e:
        raise StatuslineSetupError(f"cannot read {path}: {e}") from e
    if not raw.strip():
        return {}, raw
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise StatuslineSetupError(f"{path} is not valid JSON; not touching it: {e}") from e
    if not isinstance(data, dict):
        raise StatuslineSetupError(f"{path} is not a JSON object; not touching it")
    return data, raw


def _command_of(settings: dict) -> str | None:
    line = settings.get("statusLine")
    if isinstance(line, dict) and isinstance(line.get("command"), str):
        return line["command"]
    return None


def _indent_of(raw: str | None) -> int | str:
    """The indentation unit the file already uses (default 2)."""
    if raw:
        for line in raw.splitlines()[1:]:
            stripped = line.lstrip(" \t")
            if stripped and len(stripped) < len(line):
                lead = line[: len(line) - len(stripped)]
                return "\t" if lead.startswith("\t") else len(lead)
    return 2


def _render(settings: dict, raw: str | None) -> str:
    text = json.dumps(settings, indent=_indent_of(raw), ensure_ascii=False)
    if raw is None or raw.endswith("\n"):
        text += "\n"
    return text


def _backup(path: Path, raw: str | None, backup_dir: Path | None) -> Path | None:
    """Copy the current settings.json into rota's data dir, keeping the
    newest ``MAX_BACKUPS``."""
    if raw is None:
        return None
    directory = _backup_root(backup_dir) / BACKUP_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = directory / f"settings.{stamp}.{time.time_ns() % 1_000_000:06d}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw)
    for old in sorted(directory.glob("settings.*.json"))[:-MAX_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def _atomic_write(path: Path, text: str) -> None:
    """Replace ``path``'s content atomically, through a symlink, keeping its
    permissions (0600 for a new file)."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write(path: Path, settings: dict, raw: str | None, backup_dir: Path | None) -> None:
    try:
        _backup(path, raw, backup_dir)
        _atomic_write(path, _render(settings, raw))
    except OSError as e:
        raise StatuslineSetupError(f"cannot write {path}: {e}") from e


# -- API ------------------------------------------------------------------------------


def status(backup_dir: Path | None = None, *, now: float | None = None) -> StatuslineStatus:
    """Whether the statusline feeds rota, and whether the feed is recent.
    Never raises: an unreadable settings.json reads as no statusline."""
    path = settings_path()
    try:
        settings, _ = _read(path)
    except StatuslineSetupError:
        settings = {}
    command = _command_of(settings)
    install = _classify(command)
    last_feed_at = None
    state = "off"
    if install == INSTALLED:
        from claude_rotater import feed, poll_policy

        try:
            root = _backup_root(backup_dir)
            last_feed_at = os.stat(feed.feed_path(root)).st_mtime
        except OSError:
            last_feed_at = None
        current = time.time() if now is None else now
        fresh = last_feed_at is not None and current - last_feed_at <= poll_policy.FEED_STALE_S
        state = "live" if fresh else "stale"
    return StatuslineStatus(install, state, path, command, last_feed_at)


def install(backup_dir: Path | None = None) -> StatuslineStatus:
    """Make the statusline feed rota (see the module docstring). A no-op when
    it already does. Raises :class:`StatuslineSetupError` on failure."""
    path = settings_path()
    settings, raw = _read(path)
    command = _command_of(settings)
    if _classify(command) != INSTALLED:
        line = settings.get("statusLine")
        if command:
            settings["statusLine"] = {**line, "command": wrap(command)}
        else:
            # A statusLine object without a usable command is replaced whole.
            settings["statusLine"] = {"type": "command", "command": default_command()}
        _write(path, settings, raw, backup_dir)
    return status(backup_dir)


def uninstall(backup_dir: Path | None = None) -> StatuslineStatus:
    """Undo :func:`install`: restore the wrapped command exactly, or remove
    the default statusline. A statusline rota did not install is left alone.
    Raises :class:`StatuslineSetupError` on failure."""
    path = settings_path()
    settings, raw = _read(path)
    command = _command_of(settings)
    if command is not None:
        original = unwrap(command)
        if original is not None:
            settings["statusLine"] = {**settings["statusLine"], "command": original}
            _write(path, settings, raw, backup_dir)
        elif _is_default(command):
            del settings["statusLine"]
            _write(path, settings, raw, backup_dir)
    return status(backup_dir)
