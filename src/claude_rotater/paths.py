"""Path resolution for Claude Code config and credential files.

Mirrors claude-code's own resolution so rota reads and writes the same files
claude-code does. Key rules (from claude-code source):

- Config home: ``CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``.
- Global config: ``<config_home>/.config.json`` (legacy) if it is a regular
  file that is non-empty, or empty and writable with no ``.claude.json`` (the
  rule of claude-code 2.1.299), otherwise
  ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``. Note the asymmetry:
  ``.claude.json`` sits at homedir by default, not inside ``.claude/``.
- Credentials: ``<config_home>/.credentials.json``.

Also resolves rota's own data directory (``~/.rota``, or ``$ROTA_HOME``), the
Keychain service its per-account backups live under, and where the upstream
claude-swap tool kept its data (read only, by the one-time import).

References:
- claude-code utils/env.ts getGlobalClaudeFile
- claude-code utils/secureStorage/plainTextStorage.ts getStoragePath
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from claude_rotater.models import Platform

DATA_DIRNAME = ".rota"
DATA_HOME_ENV = "ROTA_HOME"
KEYCHAIN_SERVICE_BASE = "rota"

# Upstream claude-swap's data locations, read only by the one-time import.
LEGACY_BACKUP_DIRNAME = ".claude-swap-backup"
LEGACY_XDG_DIRNAME = "claude-swap"


def get_claude_config_home() -> Path:
    """Return the Claude config home directory (CLAUDE_CONFIG_DIR or ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def _pick_global_config(legacy: Path, global_cfg: Path) -> Path:
    """Claude Code 2.1.299's choice between ``.config.json`` and ``.claude.json``."""
    try:
        st = legacy.stat()  # follows symlinks; an error makes the global file win
    except OSError:
        return global_cfg
    if stat.S_ISREG(st.st_mode) and (
        st.st_size > 0 or (st.st_mode & 0o222 and not os.path.exists(global_cfg))
    ):
        return legacy
    return global_cfg


def get_global_config_path() -> Path:
    """Return the path to the global Claude config file.

    Returns the legacy ``<config_home>/.config.json`` if Claude Code would use
    it (see :func:`_pick_global_config`), else
    ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(env) if env else Path.home()
    return _pick_global_config(
        get_claude_config_home() / ".config.json", base / ".claude.json"
    )


def get_default_claude_config_home() -> Path:
    """Return the *default* profile's config home, ignoring ``CLAUDE_CONFIG_DIR``.

    ``_read_capture_credentials`` has to tell an env var that names the default
    profile from one that names another, since only the former's credential is
    the active store's.
    """
    return Path.home() / ".claude"


def get_default_global_config_path() -> Path:
    """Return the global config path of the *default* profile.

    Same legacy fallback as :func:`get_global_config_path`, but deliberately
    ignores ``CLAUDE_CONFIG_DIR``: callers that mirror the user's real profile
    (session sharing) must not source from another session when invoked from
    inside one.
    """
    return _pick_global_config(
        get_default_claude_config_home() / ".config.json", Path.home() / ".claude.json"
    )


def get_credentials_path() -> Path:
    """Return the path to the Claude credentials file."""
    return get_claude_config_home() / ".credentials.json"


def get_default_backup_root() -> Path:
    """Return rota's default data directory, ``~/.rota``, ignoring ``ROTA_HOME``."""
    return Path.home() / DATA_DIRNAME


def get_backup_root() -> Path:
    """Return rota's data directory: ``$ROTA_HOME`` if set, else ``~/.rota``.

    ``ROTA_HOME`` is the one way to run a separate profile (its own accounts,
    settings, cache and Keychain items). A leading ``~`` is expanded, since
    launchd plists, systemd units and Dockerfiles do not expand it, and a
    relative value is made absolute against the current directory.
    """
    value = os.environ.get(DATA_HOME_ENV, "")
    if value:
        return Path(os.path.abspath(os.path.expanduser(value)))
    return get_default_backup_root()


def _normalized(path: Path) -> str:
    return os.path.normpath(os.path.abspath(path))


def backup_keychain_service(backup_root: Path) -> str:
    """Keychain service holding the per-account backups of ``backup_root``.

    The default data directory uses the fixed name ``rota``. Any other
    directory gets ``rota-<8 hex>``, the first 8 hex digits of the SHA-256 of
    its absolute, normalized path, so two data directories never read or
    overwrite each other's items and the name stays the same across runs.
    Symlinks are not resolved, matching how Claude Code derives its own
    per-config-dir service names. Upstream claude-swap used one fixed
    ``claude-swap`` service; rota only ever reads it (see legacy_import).
    """
    normalized = _normalized(backup_root)
    if os.path.normcase(normalized) == os.path.normcase(
        _normalized(get_default_backup_root())
    ):
        return KEYCHAIN_SERVICE_BASE
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{KEYCHAIN_SERVICE_BASE}-{digest}"


def get_legacy_backup_roots() -> list[Path]:
    """Where upstream claude-swap kept its data, most specific first.

    Linux/WSL: ``$XDG_DATA_HOME/claude-swap`` (default
    ``~/.local/share/claude-swap``), then ``~/.claude-swap-backup`` for an
    install that never ran upstream's XDG move. macOS/Windows/unknown:
    ``~/.claude-swap-backup``. ``$XDG_DATA_HOME`` is ignored when unset,
    empty, or non-absolute, as upstream did.
    """
    roots: list[Path] = []
    if Platform.detect() in (Platform.LINUX, Platform.WSL):
        xdg = os.environ.get("XDG_DATA_HOME", "")
        xdg_path = Path(os.path.expanduser(xdg)) if xdg else None
        if xdg_path is not None and xdg_path.is_absolute():
            roots.append(xdg_path / LEGACY_XDG_DIRNAME)
        else:
            roots.append(Path.home() / ".local" / "share" / LEGACY_XDG_DIRNAME)
    roots.append(Path.home() / LEGACY_BACKUP_DIRNAME)
    return roots
