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
- Which key that is, ``<data dir>/api-key-mode.json`` says, by SHA-256.
  Writing it is what activates a key and removing it what revokes one. A
  copy it does not name (one left in a Keychain that could not be written,
  a file that could not be deleted) is never served: neither store outranks
  the other, and when the named key cannot be read none is served.
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

import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import feed, macos_keychain, paths
from claude_rotater.fsutil import read_text_with_retry, replace_with_retry

MARKER = "rota-key"
SETTINGS_KEY = "apiKeyHelper"
KEY_FILENAME = "active-key"
STATE_FILENAME = "api-key-mode.json"
OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@dataclass(frozen=True)
class KeyMode:
    """This profile's key mode: on since ``since`` with the key whose
    SHA-256 is ``digest`` and that ends in ``tail``, or off (``since``
    None). State written before the digest was recorded has none."""

    since: float | None
    tail: str = ""
    digest: str = ""

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


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _write_private(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a 0600 file holding ``text``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        replace_with_retry(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def store_key(
    data_dir: Path, key: str, keychain: bool | None = None, now: float | None = None
) -> None:
    """Make ``key`` ``data_dir``'s active key (kept in the Keychain when
    ``keychain``, default on macOS, else in a 0600 file). Raises OSError or
    ``KeychainError`` when it cannot; ``key`` is then not made active.

    The record is written after the key, so stopping anywhere before it
    leaves the previous key or, where that key was just overwritten, none.
    Callers serialize (the credential store's write lock)."""
    data_dir = Path(data_dir)
    in_keychain = _use_keychain() if keychain is None else keychain
    key = key.strip()
    previous = mode(data_dir)
    if previous.on and not previous.digest:
        # Four characters could name the new key as well: make the old
        # record exact before a copy of the new one exists.
        old = read_key(data_dir)
        if old:
            _write_mode(data_dir, old, previous.since)
        else:
            _clear_mode(data_dir)
        previous = mode(data_dir)
    if in_keychain:
        macos_keychain.set_password(
            key_service(data_dir), macos_keychain.keychain_account_name(), key
        )
    else:
        _write_private(data_dir / KEY_FILENAME, key)
    if previous.on and previous.digest == _fingerprint(key):
        since = previous.since
    else:
        since = time.time() if now is None else now
    _write_mode(data_dir, key, since)
    if in_keychain:
        _delete_key_file(data_dir)
    else:
        _delete_key_item(data_dir)


def read_key(data_dir: Path) -> str | None:
    """The key last activated for ``data_dir``, from whichever store holds
    it, or None when there is none or it cannot be read."""
    record = mode(data_dir)
    if not record.on:
        return None
    try:
        filed = read_text_with_retry(Path(data_dir) / KEY_FILENAME).strip()
    except (OSError, ValueError):
        filed = ""
    if filed and record.digest == _fingerprint(filed):
        return filed
    item = ""
    if _use_keychain():
        try:
            item = macos_keychain.get_password(
                key_service(data_dir), macos_keychain.keychain_account_name()
            )
        except macos_keychain.KEYCHAIN_ERRORS:
            return None
        item = (item or "").strip()
    if record.digest:
        return item if item and record.digest == _fingerprint(item) else None
    # A record from before the digest knows four characters, which two keys
    # can share: serve only when a single key fits.
    fitting = {key for key in (filed, item) if key and key[-4:] == record.tail}
    return fitting.pop() if len(fitting) == 1 else None


def forget_key(data_dir: Path) -> None:
    """Revoke ``data_dir``'s active key and drop its copies; best-effort. A
    copy that cannot be deleted is not served once the record is gone."""
    try:
        _clear_mode(data_dir)
    except OSError:
        pass
    _delete_key_item(data_dir)
    _delete_key_file(data_dir)


def _delete_key_item(data_dir: Path) -> None:
    if not _use_keychain():
        return
    try:
        macos_keychain.delete_password(
            key_service(data_dir), macos_keychain.keychain_account_name()
        )
    except macos_keychain.KEYCHAIN_ERRORS:
        pass


def _delete_key_file(data_dir: Path) -> None:
    try:
        (Path(data_dir) / KEY_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


def mode(data_dir: Path) -> KeyMode:
    """Which key ``data_dir`` is on, and since when (from its state file)."""
    try:
        raw = json.loads(read_text_with_retry(Path(data_dir) / STATE_FILENAME))
    except (OSError, ValueError):
        return KeyMode(None)
    since = raw.get("since") if isinstance(raw, dict) else None
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        return KeyMode(None)
    tail, digest = raw.get("tail"), raw.get("sha256")
    return KeyMode(
        float(since),
        tail if isinstance(tail, str) else "",
        digest if isinstance(digest, str) else "",
    )


def _write_mode(data_dir: Path, key: str, since: float) -> None:
    state = {"since": since, "tail": key[-4:], "sha256": _fingerprint(key)}
    _write_private(Path(data_dir) / STATE_FILENAME, json.dumps(state))


def _clear_mode(data_dir: Path) -> None:
    (Path(data_dir) / STATE_FILENAME).unlink(missing_ok=True)


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
    else why it could not be: ``key`` is then not served, and the previous
    key only while its own copy is intact."""
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
        # before the helper is written, so it never runs on an older record
        store_key(data_dir, key, keychain, now)
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
    return None


def deactivate(data_dir: Path, config_dir: Path) -> None:
    """Take ``config_dir`` off its key; the helper goes once no profile
    uses it. A helper that is not rota's is never touched. Best-effort: a
    settings.json that cannot be read or written is left as it is, and
    the key is revoked all the same, so a helper left there prints none
    rather than bill the account that was switched away from."""
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
                pass
    forget_key(data_dir)


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
