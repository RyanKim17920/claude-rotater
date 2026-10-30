"""Point Claude Code at an API key through ``apiKeyHelper``, per profile.

Claude Code asks ``apiKeyHelper`` (a shell command in the profile's
``settings.json``) for a key on every request it cannot serve from its
cache, reloads the setting when the file changes, and prefers the key over
the claude.ai login stored beside it. So switching a profile to an API key
is one settings edit that running sessions pick up, and switching back is
removing it: the OAuth login is never touched, so returning is instant.

- The key lives only in a rota-owned secret: the Keychain item
  ``<backup service>-active-key`` on macOS, else ``<data dir>/active-key``
  (0600). Never in settings.json, logs or argv.
- The helper is ``<rota> key --route CONFIG_DIR DATA_DIR … # rota-key``.
  ``rota key`` picks the data dir by the session's ``CLAUDE_CONFIG_DIR``
  (unset reads as ``~/.claude``) and prints that profile's key.
- A settings.json shared with another profile (one file, two config dirs,
  as a symlink makes it) would hand that profile the key too: Claude Code
  has no per-config-dir override and no fall-through from a failing
  helper (a helper that prints nothing is an error, not the OAuth login).
  :func:`unavailable` says so, :func:`activate` refuses, and nothing else
  is tried: rota never writes Claude Code's own key login, which would
  sign the profile out of its subscription. :func:`split_settings` gives
  the profile a file of its own when the user asks for it.

Import-light: ``rota key`` runs on Claude Code's request path.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import feed, macos_keychain, paths

MARKER = "rota-key"
SETTINGS_KEY = "apiKeyHelper"
KEY_FILENAME = "active-key"
STATE_FILENAME = "api-key-mode.json"
OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@dataclass(frozen=True)
class KeyMode:
    """This profile's key mode: on since ``since`` with the key ending in
    ``tail``, or off (``since`` None)."""

    since: float | None
    tail: str = ""

    @property
    def on(self) -> bool:
        return self.since is not None


# -- where things live ------------------------------------------------------------


def _norm(path: Path | str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(path))))


def key_service(data_dir: Path) -> str:
    """The Keychain service holding ``data_dir``'s active API key."""
    return f"{paths.backup_keychain_service(Path(data_dir))}-active-key"


def _use_keychain() -> bool:
    return sys.platform == "darwin"


def settings_file(config_dir: Path) -> Path:
    return Path(config_dir) / "settings.json"


def default_config_dir() -> Path:
    return Path.home() / ".claude"


def session_config_dir(environ=os.environ) -> str:
    """The config dir a Claude Code session runs in."""
    return environ.get("CLAUDE_CONFIG_DIR") or str(default_config_dir())


# -- the stored key ---------------------------------------------------------------


def store_key(data_dir: Path, key: str, keychain: bool | None = None) -> None:
    """Keep ``key`` as ``data_dir``'s active key (the Keychain when
    ``keychain``, default on macOS, else a 0600 file). Raises OSError or
    ``KeychainError`` when it cannot be stored."""
    if _use_keychain() if keychain is None else keychain:
        macos_keychain.set_password(
            key_service(data_dir), macos_keychain.keychain_account_name(), key
        )
        return
    path = Path(data_dir) / KEY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{KEY_FILENAME}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key)
    os.replace(tmp, path)


def read_key(data_dir: Path) -> str | None:
    """``data_dir``'s active key, or None: the Keychain item on macOS, then
    the file (where a Keychain that failed at activation left it)."""
    value = None
    if _use_keychain():
        try:
            value = macos_keychain.get_password(
                key_service(data_dir), macos_keychain.keychain_account_name()
            )
        except macos_keychain.KEYCHAIN_ERRORS:
            value = None
    if not value:
        try:
            value = (Path(data_dir) / KEY_FILENAME).read_text(encoding="utf-8")
        except OSError:
            value = None
    value = (value or "").strip()
    return value or None


def forget_key(data_dir: Path) -> None:
    """Drop ``data_dir``'s active key wherever it is; best-effort."""
    if _use_keychain():
        try:
            macos_keychain.delete_password(
                key_service(data_dir), macos_keychain.keychain_account_name()
            )
        except macos_keychain.KEYCHAIN_ERRORS:
            pass
    try:
        (Path(data_dir) / KEY_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


def mode(data_dir: Path) -> KeyMode:
    """When key mode began for ``data_dir`` (from its state file)."""
    try:
        raw = json.loads((Path(data_dir) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return KeyMode(None)
    since = raw.get("since") if isinstance(raw, dict) else None
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        return KeyMode(None)
    tail = raw.get("tail")
    return KeyMode(float(since), tail if isinstance(tail, str) else "")


def _write_mode(data_dir: Path, key: str, now: float) -> None:
    previous = mode(data_dir)
    since = previous.since if previous.on and previous.tail == key[-4:] else now
    paths_dir = Path(data_dir)
    paths_dir.mkdir(parents=True, exist_ok=True)
    target = paths_dir / STATE_FILENAME
    tmp = target.with_name(f".{STATE_FILENAME}.{os.getpid()}.tmp")
    state = {"since": since, "tail": key[-4:]}
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, target)


def _clear_mode(data_dir: Path) -> None:
    try:
        (Path(data_dir) / STATE_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


# -- the helper command -------------------------------------------------------------


def _exe_words() -> list[str]:
    exe = shutil.which("rota")
    return [exe] if exe else [sys.executable, "-m", "claude_rotater"]


def helper_command(routes: list[tuple[str, str]]) -> str:
    words = [*_exe_words(), "key"]
    for config_dir, data_dir in routes:
        words += ["--route", _norm(config_dir), _norm(data_dir)]
    return " ".join(shlex.quote(w) for w in words) + " # " + MARKER


def is_ours(command: object) -> bool:
    return isinstance(command, str) and command.rstrip().endswith("# " + MARKER)


def helper_routes(command: object) -> list[tuple[str, str]]:
    """The routes of a helper rota wrote; [] for anything else."""
    if not is_ours(command):
        return []
    body = command.rstrip()[: -len("# " + MARKER)]
    try:
        words = shlex.split(body)
    except ValueError:
        return []
    return feed.parse_routes(words)


def _routes_without(routes, config_dir) -> list[tuple[str, str]]:
    return [r for r in routes if not feed.route_for(_norm(config_dir), [r])]


# -- settings.json ----------------------------------------------------------------


def _settings():
    from claude_rotater import statusline_setup

    return statusline_setup


def _sharers(config_dir: Path) -> list[tuple[str, str]]:
    """``(config dir, data dir)`` of every other known profile whose
    settings.json is this one's file."""
    from claude_rotater import profiles

    mine = settings_file(config_dir)
    known = [(str(default_config_dir()), str(paths.get_default_backup_root()))]
    known += [(p.claude_config_dir, p.data_dir) for p in profiles.load()]
    out: list[tuple[str, str]] = []
    for cfg, data in known:
        if _norm(cfg) == _norm(config_dir):
            continue
        if _same_file(settings_file(Path(os.path.expanduser(cfg))), mine):
            out.append((_norm(cfg), _norm(data)))
    return out


def _same_file(a: Path, b: Path) -> bool:
    """One file under two names: a symlink, a hard link, or one folder
    under two names. Paths that don't exist yet compare by where they
    would resolve."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def shared_with(config_dir: Path) -> list[str]:
    """Config dirs of the other profiles reading ``config_dir``'s
    settings.json that are not on rota's key helper themselves: a helper
    written there would reach their sessions too."""
    ss = _settings()
    try:
        settings, _ = ss.read_settings(settings_file(config_dir))
    except ss.StatuslineSetupError:
        settings = {}
    keyed = {_norm(c) for c, _ in helper_routes(settings.get(SETTINGS_KEY))}
    return [cfg for cfg, _data in _sharers(config_dir) if cfg not in keyed]


def unavailable(config_dir: Path) -> str | None:
    """Why ``config_dir``'s sessions cannot be handed an API key through
    rota's helper, or None when they can. Changes nothing."""
    ss = _settings()
    path = settings_file(config_dir)
    try:
        settings, _ = ss.read_settings(path)
    except ss.StatuslineSetupError as e:
        return str(e)
    current = settings.get(SETTINGS_KEY)
    if current and not is_ours(current):
        return f"{path} already has an apiKeyHelper that is not rota's"
    others = shared_with(config_dir)
    if others:
        return f"settings.json is shared with {others[0]}, which would get the key too"
    return None


def activate(
    key: str,
    data_dir: Path,
    config_dir: Path,
    *,
    keychain: bool | None = None,
    now: float | None = None,
) -> str | None:
    """Switch ``config_dir``'s sessions to ``key``. Returns None when done,
    else why it could not be (nothing was changed then)."""
    why = unavailable(config_dir)
    if why is not None:
        return why
    ss = _settings()
    path = settings_file(config_dir)
    try:
        settings, raw = ss.read_settings(path)
    except ss.StatuslineSetupError as e:
        return str(e)
    current = settings.get(SETTINGS_KEY)
    routes = _routes_without(helper_routes(current), config_dir)
    try:
        store_key(data_dir, key, keychain)
    except (OSError, *macos_keychain.KEYCHAIN_ERRORS) as e:
        return f"could not store the key: {type(e).__name__}"
    routes.append((_norm(config_dir), _norm(data_dir)))
    command = helper_command(routes)
    if command != current:
        settings[SETTINGS_KEY] = command
        try:
            ss.write_settings(path, settings, raw, Path(data_dir))
        except ss.StatuslineSetupError as e:
            forget_key(data_dir)
            return str(e)
    try:
        _write_mode(data_dir, key, time.time() if now is None else now)
    except OSError:
        pass  # only the start time for the costs and error scans is lost
    return None


def deactivate(data_dir: Path, config_dir: Path) -> None:
    """Take ``config_dir`` off its key; the helper goes once no profile
    uses it. A helper that is not rota's is never touched. Best-effort:
    an unreadable settings.json is left as it is."""
    ss = _settings()
    path = settings_file(config_dir)
    try:
        settings, raw = ss.read_settings(path)
    except ss.StatuslineSetupError:
        settings, raw = {}, None
    current = settings.get(SETTINGS_KEY)
    if is_ours(current):
        routes = _routes_without(helper_routes(current), config_dir)
        if routes:
            settings[SETTINGS_KEY] = helper_command(routes)
        else:
            del settings[SETTINGS_KEY]
        if settings.get(SETTINGS_KEY) != current:
            try:
                ss.write_settings(path, settings, raw, Path(data_dir))
            except ss.StatuslineSetupError:
                return
    forget_key(data_dir)
    _clear_mode(data_dir)


def split_settings(config_dir: Path, data_dir: Path) -> None:
    """Give ``config_dir`` a settings.json of its own, on the user's word:
    the shared file (a symlink, or a hard link) is backed up into
    ``data_dir`` and replaced by a copy of its content, so a key helper
    written there later reaches this profile only. The other profiles keep
    the original file. A rota statusline in the copy gets this profile's
    feed route if it lacks one. Raises ``StatuslineSetupError`` (nothing
    changed) when the file is not shared or cannot be split."""
    ss = _settings()
    path = settings_file(config_dir)
    sharers = _sharers(config_dir)
    if not sharers:
        raise ss.StatuslineSetupError(f"{path} is not shared with another profile")
    mine = os.path.realpath(config_dir)
    for cfg, _data in sharers:
        if os.path.realpath(cfg) == mine:
            raise ss.StatuslineSetupError(
                f"{config_dir} and {cfg} are the same folder; a copy would be shared too"
            )
    settings, raw = ss.read_settings(path)
    if raw is None:
        raise ss.StatuslineSetupError(f"{path} does not exist")
    try:
        ss._backup(path, raw, Path(data_dir))
        mode_bits = os.stat(path).st_mode & 0o777
        tmp = Path(config_dir) / f".settings.json.{os.getpid()}.split.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode_bits or 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(tmp, path)  # replaces the link itself, not its target
    except OSError as e:
        raise ss.StatuslineSetupError(f"cannot split {path}: {e}") from e
    command = ss._command_of(settings)
    split = ss._split_feed(command) if command else None
    if split is not None and ss._norm(ss.feed_target(command, Path(config_dir))) != ss._norm(data_dir):
        head, words, tail = split
        words += ss._route_args(Path(config_dir), Path(data_dir))
        settings["statusLine"] = {**settings["statusLine"], "command": head + words + tail}
        ss.write_settings(path, settings, raw, Path(data_dir))


def routed_here(data_dir: Path, config_dir: Path) -> bool:
    """Whether config_dir's settings.json sends its sessions to rota's
    helper for ``data_dir``."""
    ss = _settings()
    try:
        settings, _ = ss.read_settings(settings_file(config_dir))
    except ss.StatuslineSetupError:
        return False
    target = feed.route_for(_norm(config_dir), helper_routes(settings.get(SETTINGS_KEY)))
    return target is not None and _norm(target) == _norm(data_dir)


def active_key(data_dir: Path, config_dir: Path) -> str | None:
    """The key ``config_dir``'s sessions use through rota's helper, or None.

    The settings file is read first so profiles off key mode (nearly
    always) never touch the Keychain here."""
    if not routed_here(data_dir, config_dir):
        return None
    return read_key(data_dir)


def overrides(config_dir: Path, environ=os.environ) -> list[str]:
    """What would make Claude Code ignore rota's switching here: an API key
    in the environment, or an apiKeyHelper rota did not write."""
    out = [f"{name} is set" for name in OVERRIDE_ENV if environ.get(name)]
    ss = _settings()
    try:
        settings, _ = ss.read_settings(settings_file(config_dir))
    except ss.StatuslineSetupError:
        return out
    helper = settings.get(SETTINGS_KEY)
    if helper and not is_ours(helper):
        out.append("an apiKeyHelper in settings.json")
    return out


# -- ``rota key`` -------------------------------------------------------------------


def main(argv: list[str], environ=os.environ, stdout=None) -> int:
    """``rota key --route CONFIG_DIR DATA_DIR …``: print the key of the
    profile the calling session belongs to. Exit 1, printing nothing, when
    none is set; never writes to stderr."""
    out = stdout if stdout is not None else sys.stdout
    try:
        target = feed.route_for(_norm(session_config_dir(environ)), feed.parse_routes(argv))
        key = read_key(Path(target)) if target else None
    except Exception:
        key = None
    if not key:
        return 1
    out.write(key + "\n")
    out.flush()
    return 0
