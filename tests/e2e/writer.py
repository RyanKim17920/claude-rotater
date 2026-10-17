"""A stand-in for a running claude rewriting its own Keychain item.

claude 2.1.302 writes its credential item as a locked read-modify-write:
proper-lockfile's ``lock(<dir>/.storage-write, {stale: 15000})``, i.e. the
directory ``<dir>/.storage-write.lock``, then re-read the item, change it,
write it back, unlock. This process does the same in a loop, written out
here from that protocol rather than imported from rota, so the test checks
rota against claude's behavior and not against itself.

Each round, like claude's own token refresh, it keeps the account and
rotates the access token (``-w<seq>`` suffix), and it bumps a counter in a
machine-shared field (``mcpOAuth``), as an MCP token save would. Before
writing it checks that the item still carries the counter it wrote last:
anything else means another writer replaced the item with a copy read
before our last write, which is a lost write.

One JSON line per round goes to ``--log``; the loop ends when ``--stop``
exists, checked while waiting for the lock too. Run with the sandbox
environment, so ``security`` sees only the sandbox keychain.

The lock is held the way proper-lockfile holds it: its mtime is touched every
``stale / 2`` so a slow round is never taken over as stale, and it is removed
on release only while it is still the directory this process made (a lock
taken from us is logged as ``lock-lost``, never removed from its new holder).
Between rounds the writer leaves the lock free for longer than a waiter's
longest poll (rota sleeps up to 0.5s between tries), so every waiter gets in
within a round: without that gap a waiter can lose every race for the lock
and time out, which is the writer starving rota, not a lost write.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SECURITY = "/usr/bin/security"
STALE_S = 15.0  # claude's ``stale: 15000``
TOUCH_S = STALE_S / 2  # proper-lockfile's default ``update``
# Well inside the test's own waits (60s), so a wedged ``security`` ends the
# writer with its own error on stderr instead of hanging the harness.
SECURITY_TIMEOUT_S = 10.0
POLL_S = 0.02
MARK = "rota-e2e-writer"
SUFFIX = re.compile(r"-w\d+$")


def _acquire(lock_dir: Path, stop: Path, log) -> int | None:
    """Take the lock; its inode, or None once ``stop`` exists."""
    while not stop.exists():
        try:
            os.mkdir(lock_dir)
            return os.stat(lock_dir).st_ino
        except FileExistsError:
            pass
        try:
            age = time.time() - lock_dir.stat().st_mtime
        except FileNotFoundError:
            continue
        if age > STALE_S:  # a dead holder, per the protocol
            log.write(json.dumps({"event": "takeover", "age": round(age, 3)}) + "\n")
            try:
                os.rmdir(lock_dir)
            except FileNotFoundError:
                pass
            continue
        time.sleep(POLL_S)
    return None


def _ours(lock_dir: Path, ino: int) -> bool:
    try:
        return os.stat(lock_dir).st_ino == ino
    except FileNotFoundError:
        return False


class _Held:
    """The lock while held: touched every ``TOUCH_S``, released only if ours."""

    def __init__(self, lock_dir: Path, ino: int, log) -> None:
        self.lock_dir, self.ino, self.log = lock_dir, ino, log
        self.done = threading.Event()
        self.toucher = threading.Thread(target=self._touch, daemon=True)

    def _touch(self) -> None:
        while not self.done.wait(TOUCH_S):
            if not _ours(self.lock_dir, self.ino):
                return
            try:
                os.utime(self.lock_dir)
            except FileNotFoundError:
                return

    def __enter__(self) -> "_Held":
        self.toucher.start()
        return self

    def __exit__(self, *exc) -> None:
        self.done.set()
        self.toucher.join()
        if _ours(self.lock_dir, self.ino):
            os.rmdir(self.lock_dir)
        else:
            self.log.write(json.dumps({"event": "lock-lost"}) + "\n")


def _security(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([SECURITY, *args], capture_output=True, text=True,
                              timeout=SECURITY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"security {args[0]} timed out after {SECURITY_TIMEOUT_S}s") from None


def _read(service: str, user: str) -> str | None:
    r = _security("find-generic-password", "-a", user, "-s", service, "-w")
    if r.returncode == 44:
        return None
    if r.returncode != 0:
        raise RuntimeError(f"security find-generic-password rc={r.returncode}: {r.stderr}")
    return r.stdout.removesuffix("\n")


def _write(service: str, user: str, value: str) -> None:
    r = _security("add-generic-password", "-U", "-a", user, "-s", service,
                  "-X", value.encode().hex())
    if r.returncode != 0:
        raise RuntimeError(f"security add-generic-password rc={r.returncode}: {r.stderr}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True, help="the profile's secure-storage dir")
    p.add_argument("--service", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--stop", required=True)
    p.add_argument("--hold", type=float, default=0.15, help="seconds inside the lock")
    p.add_argument("--gap", type=float, default=0.75,
                   help="seconds between rounds, lock free; above any waiter's poll")
    args = p.parse_args(argv)
    user = getpass.getuser()
    lock_dir = Path(args.dir) / ".storage-write.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    stop = Path(args.stop)
    last: int | None = None  # None until the first round adopts what is there
    with open(args.log, "a", buffering=1) as log:
        while True:
            ino = _acquire(lock_dir, stop, log)
            if ino is None:
                break
            with _Held(lock_dir, ino, log):
                raw = _read(args.service, user)
                if raw is not None:
                    last = _round(raw, last, args, user, log)
                else:
                    log.write(json.dumps({"event": "absent"}) + "\n")
            deadline = time.monotonic() + args.gap
            while time.monotonic() < deadline and not stop.exists():
                time.sleep(POLL_S)
    return 0


def _round(raw: str, last: int | None, args, user: str, log) -> int:
    """One locked read-modify-write; returns the counter it wrote."""
    item = json.loads(raw)
    found = ((item.get("mcpOAuth") or {}).get(MARK) or {}).get("seq", 0)
    if last is not None and found != last:
        log.write(json.dumps({"event": "lost", "expected": last, "found": found}) + "\n")
    seq = max(found, last or 0) + 1
    oauth = item["claudeAiOauth"]
    oauth["accessToken"] = SUFFIX.sub("", oauth["accessToken"]) + f"-w{seq}"
    item.setdefault("mcpOAuth", {})[MARK] = {"seq": seq}
    time.sleep(args.hold)  # a slow read-modify-write widens any race
    _write(args.service, user, json.dumps(item))
    log.write(json.dumps({"event": "write", "seq": seq, "token": oauth["accessToken"]}) + "\n")
    return seq


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
