"""Named profiles picked by the current directory.

A second profile is a rota data dir plus the Claude Code config dir its
accounts log in to (``ROTA_HOME`` and ``CLAUDE_CONFIG_DIR``). Setting both by
hand, or in a shell wrapper, works but is easy to miss: a terminal opened
before the wrapper was added, or any non-interactive shell, silently opens
the default profile. So rota also picks the profile itself: the registry in
the default data dir (``~/.rota/profiles.json``) lists each profile with the
directories it owns, and a rota started inside one of them (or any
subdirectory) uses that profile::

    {"schemaVersion": 1,
     "profiles": [{"name": "work",
                   "dataDir": "/Users/me/work/.rota",
                   "claudeConfigDir": "/Users/me/.claude-work",
                   "roots": ["/Users/me/work"]}]}

``claudeConfigDir`` is kept as the exact string given: Claude Code hashes
the raw ``CLAUDE_CONFIG_DIR`` string into its Keychain service name, so a
normalized spelling would name another Keychain item.

An explicit environment always wins: with ``ROTA_HOME`` set nothing is
picked, and a ``CLAUDE_CONFIG_DIR`` naming another config dir than the
matching profile's keeps the default behavior rather than mixing the two.
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import paths

REGISTRY_FILENAME = "profiles.json"
SCHEMA_VERSION = 1


class ProfileError(Exception):
    """The registry could not be read or changed."""


@dataclass(frozen=True)
class Profile:
    name: str
    data_dir: str
    claude_config_dir: str
    roots: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "dataDir": self.data_dir,
            "claudeConfigDir": self.claude_config_dir,
            "roots": list(self.roots),
        }


def registry_path() -> Path:
    return paths.get_default_backup_root() / REGISTRY_FILENAME


def _real(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def _parse(raw: object) -> list[Profile]:
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
        return []
    out: list[Profile] = []
    for item in raw["profiles"]:
        if not isinstance(item, dict):
            continue
        name, data, config = item.get("name"), item.get("dataDir"), item.get("claudeConfigDir")
        roots = item.get("roots")
        if not all(isinstance(v, str) and v for v in (name, data, config)):
            continue
        if not isinstance(roots, list):
            continue
        roots = tuple(r for r in roots if isinstance(r, str) and r)
        out.append(Profile(name, data, config, roots))
    return out


def load() -> list[Profile]:
    """The registered profiles; none when the registry is missing or
    unreadable (a broken registry never stops rota from starting)."""
    try:
        raw = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _parse(raw)


def _save(profiles: list[Profile]) -> None:
    path = registry_path()
    text = json.dumps(
        {"schemaVersion": SCHEMA_VERSION, "profiles": [p.to_json() for p in profiles]},
        indent=2,
    ) + "\n"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".profiles.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise ProfileError(f"cannot write {path}: {e}") from e


def match(cwd: str, profiles: list[Profile]) -> Profile | None:
    """The profile owning ``cwd``: the one with the deepest root that is
    ``cwd`` or one of its parents, compared with symlinks resolved."""
    here = _real(cwd)
    best: tuple[int, Profile] | None = None
    for profile in profiles:
        for root in profile.roots:
            top = _real(root)
            if here == top or here.startswith(top.rstrip(os.sep) + os.sep):
                if best is None or len(top) > best[0]:
                    best = (len(top), profile)
    return best[1] if best else None


def resolve(environ=os.environ, cwd: str | None = None) -> Profile | None:
    """The profile to use for this process, or None for the environment as
    it is (see the module docstring for when the environment wins)."""
    if environ.get(paths.DATA_HOME_ENV):
        return None
    try:
        here = cwd if cwd is not None else os.getcwd()
    except OSError:
        return None
    profile = match(here, load())
    if profile is None:
        return None
    config = environ.get("CLAUDE_CONFIG_DIR")
    if config and _real(config) != _real(profile.claude_config_dir):
        return None
    return profile


def apply(environ=os.environ, cwd: str | None = None) -> Profile | None:
    """Point this process at the profile owning the current directory, by
    setting ``ROTA_HOME`` and ``CLAUDE_CONFIG_DIR`` before anything reads
    them. Children (``rota run``'s claude) inherit both."""
    profile = resolve(environ, cwd)
    if profile is not None:
        environ[paths.DATA_HOME_ENV] = profile.data_dir
        environ.setdefault("CLAUDE_CONFIG_DIR", profile.claude_config_dir)
    return profile


def current_name(environ=os.environ) -> str | None:
    """The registered name of the data dir this process uses, if any."""
    value = environ.get(paths.DATA_HOME_ENV)
    if not value:
        return None
    here = _real(value)
    for profile in load():
        if _real(profile.data_dir) == here:
            return profile.name
    return None


@functools.cache
def active_name() -> str | None:
    """:func:`current_name` for this process, read once: the data dir is
    fixed at startup, so the TUI header can ask on every repaint."""
    return current_name()


def add(name: str, data_dir: str, claude_config_dir: str, roots: list[str]) -> Profile:
    """Register (or replace) the profile called ``name``."""
    if not name or not data_dir or not claude_config_dir or not roots:
        raise ProfileError("a profile needs a name, a data dir, a config dir and a root")
    if _real(data_dir) == _real(str(paths.get_default_backup_root())):
        raise ProfileError("the default data dir is not a separate profile")
    profile = Profile(
        name,
        os.path.abspath(os.path.expanduser(data_dir)),
        claude_config_dir,
        tuple(os.path.abspath(os.path.expanduser(r)) for r in roots),
    )
    _save([p for p in load() if p.name != name] + [profile])
    return profile


def remove(name: str) -> bool:
    profiles = load()
    kept = [p for p in profiles if p.name != name]
    if len(kept) == len(profiles):
        return False
    _save(kept)
    return True
