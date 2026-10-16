"""One-time import of upstream claude-swap data into a fresh rota data dir.

Runs at switcher construction, before anything else touches the data dir,
and only while that dir does not exist yet. It COPIES and never moves,
deletes or modifies the source, so an installed upstream claude-swap keeps
working beside rota:

- Files: ``sequence.json``, ``settings.json``, ``autoswitch_state.json``,
  ``.migrations.json`` and the ``configs/``, ``credentials/`` and ``cache/``
  trees (accounts, backups, settings, usage cache and history). Lock files
  and logs are skipped, and so is ``sessions/``: Claude Code keys a session
  profile's Keychain entry by the profile's path, so a copy at a new path
  would not find its login and is simply recreated by ``rota run``.
- macOS Keychain: every per-account backup item (and its ``.prev``
  generation) under upstream's ``claude-swap`` service is copied to this
  data dir's own service (:func:`paths.backup_keychain_service`). The
  originals stay where they are.

Source: ``$ROTA_IMPORT_FROM`` when set; otherwise, for the default data dir
only, the first of upstream's dirs (:func:`paths.get_legacy_backup_roots`)
that exists. A ``ROTA_HOME`` profile never imports implicitly, so a second
profile does not silently start with the main profile's accounts.

Idempotent and atomic: everything is assembled in a hidden sibling staging
dir and renamed into place as the last step, so the data dir exists only
once the import is complete. A failure (e.g. a locked Keychain) removes the
staging dir and raises :class:`MigrationError`; nothing is created, and the
next run retries. To start empty instead, create the data dir by hand.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import macos_keychain
from claude_rotater.exceptions import MigrationError
from claude_rotater.models import Platform, get_timestamp
from claude_rotater.paths import (
    backup_keychain_service,
    get_default_backup_root,
    get_legacy_backup_roots,
)

IMPORT_FROM_ENV = "ROTA_IMPORT_FROM"
LEGACY_KEYCHAIN_SERVICE = "claude-swap"
IMPORT_RECORD = ".imported-from.json"

_COPY_FILES = (
    "sequence.json",
    "settings.json",
    "autoswitch_state.json",
    ".migrations.json",
)
_COPY_DIRS = ("configs", "credentials", "cache")
_SKIP_PATTERNS = ("*.lock",)


@dataclass(frozen=True)
class ImportResult:
    source: Path
    target: Path
    keychain_items: int

    def describe(self) -> str:
        items = (
            f", {self.keychain_items} Keychain item(s) copied"
            if self.keychain_items
            else ""
        )
        return (
            f"rota: imported accounts and settings from {self.source} into "
            f"{self.target}{items}; {self.source} was left untouched"
        )


def _absolute(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def _same_dir(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.abspath(left) == os.path.abspath(right)


def find_import_source(target: Path) -> Path | None:
    """The dir to import into ``target`` from, or ``None`` for no import.

    Raises :class:`MigrationError` when ``ROTA_IMPORT_FROM`` names something
    that is not a directory: the import was asked for explicitly, so
    silently starting empty would hide the typo.
    """
    explicit = os.environ.get(IMPORT_FROM_ENV, "")
    if explicit:
        source = _absolute(explicit)
        if not source.is_dir():
            raise MigrationError(
                f"{IMPORT_FROM_ENV}={explicit} is not a directory; unset it to "
                f"start {target} empty"
            )
        return source
    if not _same_dir(target, get_default_backup_root()):
        return None
    for root in get_legacy_backup_roots():
        if root.is_dir():
            return root
    return None


def _legacy_backup_usernames(source: Path) -> list[str]:
    """Keychain account names of every backup the legacy store tracks."""
    try:
        data = json.loads((source / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return []
    names: list[str] = []
    for num, info in accounts.items():
        email = info.get("email") if isinstance(info, dict) else None
        if not email:
            continue
        base = f"account-{num}-{email}"
        names.extend((base, f"{base}.prev"))
    return names


def _copy_keychain_items(source: Path, service: str) -> int:
    """Copy legacy backup items to ``service``. Never deletes the originals."""
    copied = 0
    for username in _legacy_backup_usernames(source):
        try:
            secret = macos_keychain.get_password(LEGACY_KEYCHAIN_SERVICE, username)
            if secret:
                macos_keychain.set_password(service, username, secret)
                copied += 1
        except macos_keychain.KEYCHAIN_ERRORS as exc:
            raise MigrationError(
                f"could not copy the Keychain backup {username!r} from the "
                f"{LEGACY_KEYCHAIN_SERVICE!r} service: {exc}. Unlock the "
                "Keychain and run rota again (nothing was imported yet)"
            ) from exc
    return copied


def _copy_tree(source: Path, staging: Path) -> None:
    for name in _COPY_FILES:
        src = source / name
        if src.is_file():
            shutil.copy2(src, staging / name)
    ignore = shutil.ignore_patterns(*_SKIP_PATTERNS)
    for name in _COPY_DIRS:
        src = source / name
        if src.is_dir():
            shutil.copytree(src, staging / name, symlinks=True, ignore=ignore)


def import_legacy_data(target: Path, platform: Platform) -> ImportResult | None:
    """Import upstream data into ``target`` if it does not exist yet.

    Returns what was imported, or ``None`` when there was nothing to do
    (``target`` exists, or there is no source).
    """
    if os.path.lexists(target):
        return None
    source = find_import_source(target)
    if source is None:
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.import-", dir=target.parent)
        )
    except OSError as exc:
        raise MigrationError(f"could not prepare {target} for the import: {exc}") from exc
    try:
        try:
            _copy_tree(source, staging)
        except (OSError, shutil.Error) as exc:
            raise MigrationError(
                f"copying {source} into {target} failed: {exc}"
            ) from exc
        keychain_items = 0
        if platform == Platform.MACOS:
            keychain_items = _copy_keychain_items(
                source, backup_keychain_service(target)
            )
        record = {
            "source": str(source),
            "importedAt": get_timestamp(),
            "keychainItems": keychain_items,
        }
        (staging / IMPORT_RECORD).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.rename(staging, target)
        except OSError:
            if os.path.lexists(target):
                return None  # a concurrent rota finished the import first
            raise
        return ImportResult(source, target, keychain_items)
    except OSError as exc:
        raise MigrationError(f"importing {source} into {target} failed: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
