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

A profile whose data dir is not ``~/.rota`` is reached with ``--route
<config dir> <data dir>`` on the feed command, never a ``ROTA_HOME=`` for
the whole command: profiles often share one settings.json (symlinked from a
second config dir), so one command serves several profiles, and each
install adds its own route to it. ``rota feed`` sends a session's record by
the session's config dir; an unrouted one goes to ``~/.rota``.

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
import re
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
    # "live" (feed recent) | "stale" (installed, feed quiet) | "off" |
    # "unset" (installed, but the data dir the command feeds does not exist,
    # so ``rota feed`` quietly records nothing)
    state: str
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


def _norm(path: Path | str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _route_args(config_home: Path, root: Path) -> str:
    return f" --route {shlex.quote(_norm(config_home))} {shlex.quote(_norm(root))}"


def feed_command(backup_dir: Path | None = None) -> str:
    """The shell words that run ``rota feed`` for this install and data dir.

    A data dir other than ``~/.rota`` gets a ``--route`` from this profile's
    config dir, since Claude Code runs the statusline without rota's
    environment (see the module docstring)."""
    from claude_rotater import paths

    exe = shutil.which("rota")
    words = [exe] if exe else [sys.executable, "-m", "claude_rotater"]
    command = " ".join(shlex.quote(w) for w in words) + " feed"
    root = _backup_root(backup_dir)
    if _norm(root) != _norm(paths.get_default_backup_root()):
        command += _route_args(settings_path().parent, root)
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


def _split_feed(command: str) -> tuple[str, str, str] | None:
    """``(head, feed words, tail)`` of a command rota wrote, else None."""
    if unwrap(command) is not None:
        mid = command.find(_WRAP_MID, len(_WRAP_HEAD))
        return _WRAP_HEAD, command[len(_WRAP_HEAD) : mid], command[mid:]
    if _is_default(command):
        return "", command[: -len(_DEFAULT_TAIL)], _DEFAULT_TAIL
    return None


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return []


def _routes(command: str) -> list[tuple[str, str]]:
    from claude_rotater import feed

    split = _split_feed(command)
    return feed.parse_routes(_words(split[1] if split else command))


_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _join(words: list[str]) -> str:
    """Shell words back into a command; ``NAME=value`` stays an assignment."""
    out = []
    for word in words:
        match = _ASSIGNMENT_RE.match(word)
        out.append(f"{match[1]}={shlex.quote(match[2])}" if match else shlex.quote(word))
    return " ".join(out)


def _without_routes(feed_words: str, config_home: Path) -> str:
    """``feed_words`` with every ``--route`` for ``config_home`` removed."""
    from claude_rotater import feed

    words, kept, i = _words(feed_words), [], 0
    while i < len(words):
        if words[i] == "--route" and i + 2 < len(words):
            if not feed.route_for(str(config_home), [(words[i + 1], words[i + 2])]):
                kept += words[i : i + 3]
            i += 3
            continue
        kept.append(words[i])
        i += 1
    return _join(kept)


_ROTA_HOME_RE = re.compile(r"""(?:^|[\s;&|({])ROTA_HOME=('[^']*'|"[^"]*"|[^\s;&|)]+)""")


def feed_target(command: str, config_home: Path | str | None = None) -> Path:
    """The data dir ``rota feed`` in ``command`` writes a session of
    ``config_home`` to: the last ``--route`` for that config dir, else the
    ``ROTA_HOME=`` the command sets (older installs wrote one), else
    ``~/.rota``. rota never exports ``ROTA_HOME`` to Claude Code, so nothing
    else sets it."""
    from claude_rotater import feed, paths

    if config_home is not None:
        routed = feed.route_for(os.fspath(config_home), _routes(command))
        if routed is not None:
            return Path(routed)
    match = _ROTA_HOME_RE.search(command)
    if match is None:
        return paths.get_default_backup_root()
    try:
        words = shlex.split(match.group(1))
    except ValueError:
        words = [match.group(1)]
    value = words[0] if words else ""
    return Path(os.path.expanduser(value)) if value else paths.get_default_backup_root()


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
    # Names sort oldest first: a readable stamp, then the full nanosecond
    # clock (macOS only ticks in microseconds, so two backups in one tick
    # take the next free name instead of colliding).
    stamp = time.strftime("%Y%m%dT%H%M%S")
    ns = time.time_ns()
    while True:
        target = directory / f"settings.{stamp}.{ns:020d}.json"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            ns += 1
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

        root = _backup_root(backup_dir)
        try:
            last_feed_at = os.stat(feed.feed_path(root)).st_mtime
        except OSError:
            last_feed_at = None
        current = time.time() if now is None else now
        fresh = last_feed_at is not None and current - last_feed_at <= poll_policy.FEED_STALE_S
        state = "live" if fresh else "stale"
        if not fresh and command:
            target = feed_target(command, path.parent)
            if _split_feed(command) is not None and _norm(target) != _norm(root):
                # A statusline rota set up for another profile that shares
                # this settings.json: it feeds that profile, not this one.
                # install() adds this profile's route.
                install, state = FOREIGN, "unset"
            elif not os.path.isdir(target):
                state = "unset"  # `rota feed` exits 0 and records nothing
    return StatuslineStatus(install, state, path, command, last_feed_at)


def install(backup_dir: Path | None = None) -> StatuslineStatus:
    """Make the statusline feed rota (see the module docstring). A no-op when
    it already does. Raises :class:`StatuslineSetupError` on failure."""
    path = settings_path()
    settings, raw = _read(path)
    command = _command_of(settings)
    root = _backup_root(backup_dir)
    split = _split_feed(command) if command else None
    if split is not None:
        if _norm(feed_target(command, path.parent)) != _norm(root):
            # Shared with another profile: add this one's route to it.
            head, words, tail = split
            words += _route_args(path.parent, root)
            settings["statusLine"] = {**settings["statusLine"], "command": head + words + tail}
            _write(path, settings, raw, backup_dir)
    elif _classify(command) != INSTALLED:
        line = settings.get("statusLine")
        if command:
            settings["statusLine"] = {**line, "command": wrap(command, feed_command(backup_dir))}
        else:
            # A statusLine object without a usable command is replaced whole.
            settings["statusLine"] = {
                "type": "command",
                "command": default_command(feed_command(backup_dir)),
            }
        _write(path, settings, raw, backup_dir)
    return status(backup_dir)


def _feeds_others(command: str, config_home: Path) -> bool:
    """Whether ``command`` has a route for ``config_home`` and still feeds
    some other profile without it: another config dir's route, or the data
    dir an unrouted session goes to, if that exists (rota there is in use)."""
    from claude_rotater import feed

    routes = _routes(command)
    mine = [feed.route_for(str(config_home), [route]) is not None for route in routes]
    if not any(mine):
        return False
    return not all(mine) or os.path.isdir(feed_target(command))


def uninstall(backup_dir: Path | None = None) -> StatuslineStatus:
    """Undo :func:`install`: restore the wrapped command exactly, or remove
    the default statusline. A statusline rota did not install is left alone,
    and one that still feeds another profile only loses this profile's route.
    Raises :class:`StatuslineSetupError` on failure."""
    path = settings_path()
    settings, raw = _read(path)
    command = _command_of(settings)
    split = _split_feed(command) if command is not None else None
    if split is not None and _feeds_others(command, path.parent):
        # Other profiles still feed through it: drop only this one's route.
        head, words, tail = split
        settings["statusLine"] = {
            **settings["statusLine"],
            "command": head + _without_routes(words, path.parent) + tail,
        }
        _write(path, settings, raw, backup_dir)
    elif command is not None:
        original = unwrap(command)
        if original is not None:
            settings["statusLine"] = {**settings["statusLine"], "command": original}
            _write(path, settings, raw, backup_dir)
        elif _is_default(command):
            del settings["statusLine"]
            _write(path, settings, raw, backup_dir)
    return status(backup_dir)
