"""One auto engine per data dir, and a warning about stale rota processes.

An editable install never reloads a running process: a rota started before a
fix keeps running the code it imported, and two rotas on one data dir each
run their own engine. That is how a pre-fix process kept writing switches
into the wrong Keychain item for hours next to a fixed one.

So the engine is owned through ``engine.lock`` in the data dir: an exclusive
``flock`` (released by the kernel when the holder exits, so a crash never
leaves it stale) whose file records the holder's pid, code version, commit
and start time. Whoever holds it runs the engine; every other rota on the
same data dir is view-only and says who runs it, flagged "older code" when
the holder started before the installed source last changed.

Processes from before this lock existed hold nothing, so they are found by
their open handle on the data dir's ``rota.log`` (best effort, macOS/Linux
``lsof``) and reported the same way; a new rota stays view-only beside one
rather than running a second engine.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger("rota")

LOCK_FILENAME = "engine.lock"
LOG_FILENAME = "rota.log"
_PKG_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Holder:
    """Another rota process running (or able to run) the engine here."""

    pid: int
    version: str = ""
    commit: str = ""
    started_at: float | None = None
    legacy: bool = False  # predates the engine lock (found by its open log)

    @property
    def older_code(self) -> bool:
        """Whether it started before the installed source last changed (or
        predates the lock altogether), so it runs older code than ours."""
        if self.legacy:
            return True
        if self.commit and self.commit != code_commit():
            return True
        newest = newest_source_mtime()
        return (
            self.started_at is not None
            and newest is not None
            and self.started_at < newest
        )

    def describe(self) -> str:
        """``another rota is running (pid 4242, older code)``."""
        tags = [f"pid {self.pid}"]
        if self.older_code:
            tags.append("older code")
        return f"another rota is running ({', '.join(tags)})"


def code_commit() -> str:
    """The short git commit of the running source, or "" when not in git."""
    global _COMMIT
    if _COMMIT is None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(_PKG_DIR), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3,
            )
            _COMMIT = proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            _COMMIT = ""
    return _COMMIT


_COMMIT: str | None = None


def newest_source_mtime() -> float | None:
    """When the installed package's source last changed (editable installs
    change under a running process)."""
    try:
        return max(p.stat().st_mtime for p in _PKG_DIR.rglob("*.py"))
    except (OSError, ValueError):
        return None


def _version() -> str:
    try:
        from claude_rotater import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 -- metadata missing in odd installs
        return ""


class EngineLock:
    """Exclusive ownership of the auto engine for one data dir."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / LOCK_FILENAME
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> Holder | None:
        """Take the lock. Returns None when this process now owns the engine,
        else who does (or a lock-less older rota that is running here)."""
        if self._fd is not None:
            return None
        legacy = legacy_holder(self.path.parent)
        if legacy is not None:
            return legacy
        if sys.platform == "win32":  # no flock: single-engine is best effort
            return None
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = _read_holder(fd)
            os.close(fd)
            return holder
        record = {
            "pid": os.getpid(),
            "version": _version(),
            "commit": code_commit(),
            "startedAt": _process_start(),
        }
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode(), 0)
        self._fd = fd
        return None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        os.close(fd)  # closing drops the flock


def _process_start() -> float:
    """This process's start time (the imported code is from then)."""
    from claude_rotater.process_detection import process_started_at

    started = process_started_at(os.getpid())
    return float(started) if started is not None else time.time()


def _read_holder(fd: int) -> Holder:
    try:
        raw = os.pread(fd, 4096, 0).decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        data = {}
    pid = data.get("pid") if isinstance(data, dict) else None
    started = data.get("startedAt") if isinstance(data, dict) else None
    return Holder(
        pid=pid if isinstance(pid, int) else 0,
        version=str(data.get("version", "")) if isinstance(data, dict) else "",
        commit=str(data.get("commit", "")) if isinstance(data, dict) else "",
        started_at=float(started) if isinstance(started, (int, float)) else None,
    )


def _rota_engine_pids() -> list[int]:
    """Other processes that look like a rota TUI or ``rota auto`` loop."""
    if sys.platform == "win32":
        return []
    try:
        proc = subprocess.run(
            ["ps", "-A", "-o", "pid=,args="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    me = os.getpid()
    pids = []
    for line in proc.stdout.splitlines():
        pid_text, _, args = line.strip().partition(" ")
        if not pid_text.isdigit() or int(pid_text) == me:
            continue
        if _is_engine_command(args):
            pids.append(int(pid_text))
    return pids


def _is_engine_command(args: str) -> bool:
    """A rota TUI (no subcommand) or a ``rota auto`` loop."""
    parts = args.split()
    for i, part in enumerate(parts):
        if os.path.basename(part) == "rota" or part.endswith("claude_rotater"):
            rest = [p for p in parts[i + 1:] if not p.startswith("-")]
            if not rest:
                return True
            return rest[0] == "auto" and "--once" not in parts
    return False


def _has_open(pid: int, path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    target = "n" + str(path)
    return any(line == target for line in proc.stdout.splitlines())


def legacy_holder(data_dir: Path) -> Holder | None:
    """A running rota on ``data_dir`` that predates the engine lock, found by
    its open ``rota.log``. Lock holders are not legacy, so they are skipped
    here and reported through the lock instead."""
    log = Path(data_dir) / LOG_FILENAME
    lock = Path(data_dir) / LOCK_FILENAME
    for pid in _rota_engine_pids():
        if _has_open(pid, log) and not _has_open(pid, lock):
            return Holder(pid=pid, legacy=True)
    return None


def warn_if_not_owner(holder: Holder | None) -> str:
    """Log the view-only warning; returns the line to show ("" when owner)."""
    if holder is None:
        return ""
    line = f"{holder.describe()}; this one is view-only: auto-switching runs there"
    _logger.warning("%s (version %s, commit %s)", line, holder.version or "?",
                    holder.commit or "?")
    return line
