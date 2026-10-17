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
exists. Run with the sandbox environment, so ``security`` sees only the
sandbox keychain.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SECURITY = "/usr/bin/security"
STALE_S = 15.0  # claude's ``stale: 15000``
MARK = "rota-e2e-writer"
SUFFIX = re.compile(r"-w\d+$")


def _lock(lock_dir: Path) -> None:
    while True:
        try:
            os.mkdir(lock_dir)
            return
        except FileExistsError:
            pass
        try:
            if time.time() - lock_dir.stat().st_mtime > STALE_S:
                os.rmdir(lock_dir)  # a dead holder, per the protocol
                continue
        except FileNotFoundError:
            continue
        time.sleep(0.02)


def _read(service: str, user: str) -> str | None:
    r = subprocess.run([SECURITY, "find-generic-password", "-a", user, "-s", service, "-w"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 44:
        return None
    r.check_returncode()
    return r.stdout.removesuffix("\n")


def _write(service: str, user: str, value: str) -> None:
    subprocess.run([SECURITY, "add-generic-password", "-U", "-a", user, "-s", service,
                    "-X", value.encode().hex()], capture_output=True, timeout=30, check=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True, help="the profile's secure-storage dir")
    p.add_argument("--service", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--stop", required=True)
    p.add_argument("--hold", type=float, default=0.15, help="seconds inside the lock")
    p.add_argument("--gap", type=float, default=0.05, help="seconds between rounds")
    args = p.parse_args(argv)
    user = getpass.getuser()
    lock_dir = Path(args.dir) / ".storage-write.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    stop = Path(args.stop)
    last: int | None = None  # None until the first round adopts what is there
    with open(args.log, "a", buffering=1) as log:
        while not stop.exists():
            _lock(lock_dir)
            try:
                raw = _read(args.service, user)
                if raw is not None:
                    last = _round(raw, last, args, user, log)
                else:
                    log.write(json.dumps({"event": "absent"}) + "\n")
            finally:
                os.rmdir(lock_dir)
            time.sleep(args.gap)
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
