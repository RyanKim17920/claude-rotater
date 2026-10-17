"""Cooperate with Claude Code's own advisory locks while mutating its files.

Claude Code guards its OAuth token refresh with the npm ``proper-lockfile``
package, and its ``~/.claude.json`` writes with the same mechanism on the
config file. The protocol (verified against the 2.1.218 bundle):

- The lock artifact is a **directory**; ``mkdir`` atomicity is the mutex.
- The refresh path takes **two** locks, in order: the primary
  ``<config-home>/.oauth_refresh.lock``, then the legacy
  ``<config-home>.lock`` (``~/.claude.lock``) kept for compatibility with
  external tools. Both run ``stale: 60000, update: 5000`` — a credential
  lock is stale only past **60s**, and live holders touch every 5s. On a
  contended legacy lock Claude Code releases the primary and retries.
- The config lock (``~/.claude.json.lock``) keeps the older defaults:
  stale after 10s, touched every 5s.
- Claude Code retries a held credentials lock 5 times with 1-2s jittered
  sleeps before giving up, so briefly holding it is fully cooperative.

Holding these locks while swapping credentials closes the one real race with a
running Claude Code: its refresh reads credentials, refreshes over the network,
and saves — all under both credential locks — so a swap landing inside that
window would be overwritten by the refreshed old-account token (and the
just-taken backup would keep a pre-rotation refresh token). Under the lock,
Claude Code's own double-checked re-read sees the swapped (non-expired)
credential and aborts the refresh instead.

References (claude-code 2.1.218 bundle): the ``uKi`` lock-options helper
(``lockfilePath: join(dir, ".oauth_refresh.lock"), stale: 60000, update:
5000``) and ``CKi`` (dual acquisition, legacy released-on-contention with
``tengu_oauth_refresh_legacy_lock_contended`` telemetry). The 2.1.302 binary
keeps both (``m4n``: ``.oauth_refresh.lock``, ``stale: 60000, update: 5000``).

Secure-storage write lock (verified against the 2.1.302 native binary, whose
embedded bundle was read with a byte search for ``.storage-write``):

- Every credential write goes through one helper (the ``secureStorage``
  ``mutate`` path) that runs ``proper-lockfile``'s ``lock(join(dir,
  ".storage-write"), {realpath: false, retries: {retries: 10, minTimeout:
  100, maxTimeout: 1000}, stale: 15000})`` with no ``lockfilePath``, so the
  artifact is the **directory** ``<dir>/.storage-write.lock`` (the library
  appends ``.lock``). ``update`` is unset, so the library touches every
  ``stale / 2`` = 7.5s; a lock is stale past **15s** and a waiter then
  removes and retakes it. A held lock is retried ~7.5s in total
  (100, 200, 400, 800 then 1000ms x6) before claude's write gives up.
- ``dir`` is the secure-storage config dir: ``CLAUDE_SECURESTORAGE_CONFIG_DIR``
  when defined (empty selects ``~/.claude``), else the config home
  (``CLAUDE_CONFIG_DIR`` or ``~/.claude``), NFC-normalized: the same
  resolution that names the profile's Keychain item.
- Under the lock claude invalidates its cache, re-reads the store, applies its
  change and writes it back: a read-modify-write. It is a leaf lock: nothing
  else is acquired inside it, and the token refresh takes it *after* its
  ``.oauth_refresh.lock``/legacy pair. rota therefore takes it innermost,
  after every other lock it holds, and acquires nothing while holding it.

References (claude-code 2.1.302 bundle): ``oFo`` (the lock call, with an
``AsyncLocalStorage`` making it re-entrant in-process), ``u`` (the locked
read-modify-write), ``zx`` (the dir), and the bundled ``proper-lockfile``
(``ne``: ``lockfilePath || `${file}.lock```; ``Nt``: the stale/update
defaults and the 2s/1s floors).
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from claude_rotater.exceptions import ClaudeCodeLockTimeout
from claude_rotater.paths import get_claude_config_home, get_global_config_path

# Claude Code's credential-refresh locks run ``stale: 60000, update: 5000``
# (2.1.218 ``uKi``): a lock younger than 60s belongs to a live holder and
# must never be stolen — the holder's toucher may stall well past 10s
# (suspend, blocked event loop) while it still legitimately owns the lock.
CREDENTIALS_STALENESS_S = 60.0
# The config lock (~/.claude.json.lock) keeps the older proper-lockfile
# defaults: stale after 10s, touched every 5s.
CONFIG_STALENESS_S = 10.0
# claude 2.1.302's secure-storage write lock runs ``stale: 15000`` (touched
# every 7.5s, the library default ``stale / 2``).
STORAGE_WRITE_STALENESS_S = 15.0
# We touch a little faster than CC's 5s for margin.
TOUCH_INTERVAL_S = 3.0
# Claude Code holds the credentials lock for one token-endpoint round trip
# (sub-second to a few seconds); its config lock for a local RMW. 9s of
# bounded waiting comfortably outlasts both without stalling the CLI forever.
# Note this is a PER-LOCK budget: claude_credentials_lock acquires two locks
# sequentially, so its worst case is ~2x this value.
DEFAULT_TIMEOUT_S = 9.0

_logger = logging.getLogger("rota")


def credentials_lock_dir() -> Path:
    """Legacy credential lock (``~/.claude.lock``) — CC still takes it for
    compatibility; external exclusion today rests on this one."""
    home = get_claude_config_home()
    return home.parent / (home.name + ".lock")


def oauth_refresh_lock_dir() -> Path:
    """Claude Code's primary OAuth refresh lock
    (``<config-home>/.oauth_refresh.lock``, 2.1.218+)."""
    return get_claude_config_home() / ".oauth_refresh.lock"


def storage_write_lock_dir() -> Path:
    """claude's secure-storage write lock (``<dir>/.storage-write.lock``).

    ``dir`` resolves like claude 2.1.302's ``zx``: a defined
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` (empty means ``~/.claude``), else the
    config home. Per profile, so two profiles never serialize on each other.
    """
    secure = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if secure is not None:
        base = secure or str(Path.home() / ".claude")
    else:
        base = str(get_claude_config_home())
    return Path(unicodedata.normalize("NFC", base)) / ".storage-write.lock"


def config_lock_dir() -> Path:
    """Lock directory guarding the global config file (``~/.claude.json.lock``)."""
    path = get_global_config_path()
    return path.parent / (path.name + ".lock")


@contextmanager
def proper_lockfile(
    lock_dir: Path,
    *,
    timeout: float | None = None,
    staleness: float = CONFIG_STALENESS_S,
):
    """Acquire a proper-lockfile-compatible directory lock.

    Blocks up to ``timeout`` seconds (default ``DEFAULT_TIMEOUT_S``, resolved
    at call time so tests can shorten it), taking over locks whose mtime is
    older than ``staleness``, touches the directory mtime while held so other
    holders don't deem us stale, and removes it on exit.

    Raises:
        ClaudeCodeLockTimeout: The lock stayed held past ``timeout``.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_S
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        if time.monotonic() - start > timeout:
            raise ClaudeCodeLockTimeout(
                f"Could not acquire {lock_dir.name} — Claude Code appears "
                "to be refreshing credentials. Retry in a few seconds."
            )
        try:
            held_mtime = os.stat(lock_dir).st_mtime
        except FileNotFoundError:
            continue  # holder released between mkdir and stat; retry now
        if time.time() - held_mtime > staleness:
            # Dead holder per the protocol: remove and retake. Losing the
            # rmdir/mkdir race to another waiter just means looping again.
            try:
                os.rmdir(lock_dir)
            except OSError:
                time.sleep(0.05)  # can't remove it either; don't spin hot
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stop_touching = threading.Event()

    def _touch() -> None:
        while not stop_touching.wait(TOUCH_INTERVAL_S):
            try:
                os.utime(lock_dir)
            except OSError:
                return  # lock stolen/removed; nothing left to keep alive

    toucher = threading.Thread(target=_touch, daemon=True)
    toucher.start()
    try:
        yield
    finally:
        stop_touching.set()
        toucher.join(timeout=1.0)
        try:
            os.rmdir(lock_dir)
        except FileNotFoundError:
            _logger.warning(
                "Lock %s vanished while held (taken over as stale?)", lock_dir
            )
        except OSError as e:
            _logger.warning("Failed to release lock %s: %s", lock_dir, e)


@contextmanager
def claude_credentials_lock(*, timeout: float | None = None):
    """Hold Claude Code's credential-refresh locks, in CC's own order.

    2.1.218 takes ``<config-home>/.oauth_refresh.lock`` first, then the
    legacy ``~/.claude.lock``; on legacy contention it releases the primary
    before retrying. Mirroring both the pair and the order means a waiting
    rota and a waiting Claude Code can never deadlock against each other,
    and exclusion holds even after CC drops the legacy lock. Both use CC's
    60s staleness — never steal a lock a live CC may still hold.
    """
    with (
        proper_lockfile(
            oauth_refresh_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ),
        proper_lockfile(
            credentials_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ),
    ):
        yield


@contextmanager
def claude_config_lock(*, timeout: float | None = None):
    """Hold Claude Code's global-config write lock (``~/.claude.json.lock``)."""
    with proper_lockfile(config_lock_dir(), timeout=timeout):
        yield


# Lock dirs this thread holds through claude_storage_write_lock, with depth.
_storage_write_held = threading.local()


@contextmanager
def claude_storage_write_lock(*, timeout: float | None = None):
    """Hold claude's secure-storage write lock for the active profile.

    Take it innermost (after FileLock, the credential pair and the config
    lock) and span the live read through the live write, as claude does, so a
    concurrent claude write can neither land between rota's read and its
    write (and be overwritten) nor interleave with it.

    Re-entrant per thread, like claude's own: a switch that already holds it
    calls into the credential store, which takes it again for callers that do
    not. Another thread of this process still waits, as another process would.

    Raises:
        ClaudeCodeLockTimeout: claude held it past ``timeout``; nothing ran.
    """
    lock_dir = storage_write_lock_dir()
    key = str(lock_dir)
    held = getattr(_storage_write_held, "dirs", None)
    if held is None:
        held = _storage_write_held.dirs = {}
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    with proper_lockfile(
        lock_dir, timeout=timeout, staleness=STORAGE_WRITE_STALENESS_S
    ):
        held[key] = 1
        try:
            yield
        finally:
            del held[key]
