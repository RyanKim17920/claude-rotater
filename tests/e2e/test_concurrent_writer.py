"""rota writing the live login while a running claude rewrites it.

claude 2.1.302 rewrites its Keychain item on its own (a token refresh, an
MCP token save) as a read-modify-write under ``<dir>/.storage-write.lock``.
``writer.py`` does exactly that in a loop, beside rota switching and beside
the auto engine repairing a foreign login. Without the lock rota reads the
item, the writer writes, and rota's write puts back the copy it read: the
writer's round is gone. With it, every round survives, the live login stays
one account's, and the backups carry the newest token.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from tests.e2e.sandbox import ACCOUNTS, HERE, Sandbox, claude_service, explain
from tests.e2e.test_e2e import PROFILES, profile_dir, seed

ALICE, BOB, _ = ACCOUNTS
WRITER = HERE / "writer.py"


# Longer than anything the writer can block on by itself (a ``security`` call
# is cut at 10s, a lock wait ends at the stop file), so a stuck writer is
# reported by the writer, and a wait here running out is a real stall.
WRITER_WAIT_S = 60.0


class ClaudeWriter:
    """``writer.py`` running against one profile's item, for a ``with`` block."""

    def __init__(self, box: Sandbox, config_dir: str | None) -> None:
        self.box = box
        self.config_dir = config_dir
        self.log = box.root / "writer.log"
        self.err = box.root / "writer.err"
        self.stop_file = box.root / "writer.stop"
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "ClaudeWriter":
        secure_dir = self.config_dir or str(self.box.home / ".claude")
        # stderr to a file, not a pipe: readable while the writer runs, for
        # the diagnostics below, and never a full pipe stalling the writer.
        with open(self.err, "wb") as err:
            self.proc = subprocess.Popen(
                [sys.executable, str(WRITER), "--dir", secure_dir,
                 "--service", claude_service(self.config_dir),
                 "--log", str(self.log), "--stop", str(self.stop_file)],
                env=self.box.env(config_dir=self.config_dir), cwd=str(self.box.home),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
            )
        return self

    def __exit__(self, *exc) -> None:
        self.stop_file.touch()
        assert self.proc is not None
        try:
            self.proc.wait(timeout=WRITER_WAIT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()  # our own child
            self.proc.wait()
            raise AssertionError(
                f"writer still running {WRITER_WAIT_S}s after the stop file\n"
                + self.diagnostics()) from None
        if exc[0] is None:
            assert self.proc.returncode == 0, f"writer failed\n{self.diagnostics()}"

    def diagnostics(self, lines: int = 25) -> str:
        """The writer's exit code, stderr and log tail, for a failure message."""
        rc = self.proc.poll() if self.proc is not None else None
        err = self.err.read_text(errors="replace") if self.err.exists() else ""
        log = self.log.read_text().splitlines()[-lines:] if self.log.exists() else []
        return (f"\n--- writer rc={rc}\n--- writer stderr\n{err}"
                f"\n--- writer log (last {len(log)})\n" + "\n".join(log))

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def writes(self) -> list[dict]:
        return [e for e in self.events() if e["event"] == "write"]

    def wait_for_writes(self, n: int, timeout: float = WRITER_WAIT_S) -> None:
        """Until the writer has completed ``n`` more rounds than now."""
        target = len(self.writes()) + n
        deadline = time.monotonic() + timeout
        while len(self.writes()) < target:
            assert self.proc is not None and self.proc.poll() is None, (
                f"writer exited{self.diagnostics()}")
            assert time.monotonic() < deadline, (
                f"writer made no progress in {timeout}s{self.diagnostics()}")
            time.sleep(0.05)


def owner(token: str) -> str:
    """Which fake account an access token (writer-rotated or not) is."""
    for account in ACCOUNTS:
        if token == account.access_token or token.startswith(account.access_token + "-w"):
            return account.tag
    raise AssertionError(f"token of no known account: {token!r}")


def live_item(box: Sandbox, config_dir: str | None) -> dict:
    raw = box.keychain_get(claude_service(config_dir))
    assert raw is not None, "the live item is gone"
    return json.loads(raw)


def assert_coherent(box: Sandbox, config_dir: str | None, writer: ClaudeWriter, account):
    """No round lost, and the live item is wholly ``account``'s login with the
    writer's last round on it, as claude reads it."""
    lost = [e for e in writer.events() if e["event"] == "lost"]
    assert not lost, f"rota overwrote the writer's rounds: {lost}"
    taken = [e for e in writer.events() if e["event"] in ("lock-lost", "takeover")]
    assert not taken, f"the lock changed hands while held: {taken}"
    writes = writer.writes()
    assert writes, "the writer never wrote"
    item = live_item(box, config_dir)
    oauth = item["claudeAiOauth"]
    assert owner(oauth["accessToken"]) == account.tag
    assert oauth["refreshToken"] == account.refresh_token
    assert item["mcpOAuth"]["rota-e2e-writer"]["seq"] == writes[-1]["seq"]
    assert box.live_email(config_dir) == account.email
    status = box.claude_status(config_dir)
    assert status.get("loggedIn") is True and status.get("email") == account.email, status


def latest_tokens(writer: ClaudeWriter) -> dict[str, str]:
    """The newest token the writer gave each account."""
    out: dict[str, str] = {}
    for write in writer.writes():
        out[owner(write["token"])] = write["token"]
    return out


@pytest.mark.parametrize("profile", PROFILES)
def test_switching_while_claude_rewrites_its_login(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))  # bob live

    targets = ["1", "2", "1", "2", "1", "2"]
    with ClaudeWriter(sandbox, config_dir) as writer:
        writer.wait_for_writes(2)
        for target in targets:
            result = sandbox.rota("switch", target, config_dir=config_dir)
            assert result.returncode == 0, explain(result) + writer.diagnostics()
            if target == "1" and "alice" not in latest_tokens(writer):
                # Rounds are ~1s apart and a switch takes less, so alice's
                # short turns can all fall between rounds: wait for one
                # round on her login (the next switch still races the
                # following round), or the backup check below has nothing
                # of hers to check.
                writer.wait_for_writes(1)
        writer.wait_for_writes(2)  # rounds on the final login too

    assert_coherent(sandbox, config_dir, writer, BOB)
    latest = latest_tokens(writer)
    assert set(latest) == {"alice", "bob"}, latest  # it rewrote both logins
    assert live_item(sandbox, config_dir)["claudeAiOauth"]["accessToken"] == latest["bob"]
    # Each switch away backed up the newest token: switching back now (no
    # writer) restores exactly it.
    result = sandbox.rota("switch", "1", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert sandbox.live_token(config_dir) == latest["alice"]
    result = sandbox.rota("switch", "2", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert sandbox.live_token(config_dir) == latest["bob"]


def _auto_once(box: Sandbox, config_dir: str | None,
               writer: ClaudeWriter) -> tuple[int, list[dict]]:
    result = box.rota("auto", "--once", "--json", "--strategy", "best",
                      config_dir=config_dir)
    # switched / no action
    assert result.returncode in (0, 2), explain(result) + writer.diagnostics()
    events = [json.loads(line) for line in result.stdout.splitlines()
              if line.startswith("{")]
    return result.returncode, events


@pytest.mark.parametrize("profile", PROFILES)
def test_auto_repair_while_claude_rewrites_its_login(sandbox: Sandbox, profile):
    if sandbox.api is None:
        pytest.skip("telling a foreign login apart needs the profile endpoint")
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))
    result = sandbox.rota("switch", "1", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    # Bob's login lands in the item while rota marks alice active.
    sandbox.keychain_set(claude_service(config_dir), json.dumps(BOB.credential()))
    # As if the poll interval had passed: the next tick fetches usage (and so
    # sees whose token is live) instead of serving the seeding's cache.
    (sandbox.home / ".rota" / "cache" / "usage.json").unlink()

    codes, switches, ticks = [], [], []
    with ClaudeWriter(sandbox, config_dir) as writer:
        writer.wait_for_writes(2)
        for _ in range(4):
            code, events = _auto_once(sandbox, config_dir, writer)
            codes.append(code)
            ticks.append(events)
            switches += [e for e in events if e["event"] == "switch"]
        writer.wait_for_writes(2)

    # One repair, then quiet: no flapping, and never a failover to bob.
    assert [s["trigger"] for s in switches] == ["repair"], ticks
    assert codes == [0, 2, 2, 2], (codes, ticks)
    assert switches[0]["to"]["email"] == ALICE.email
    assert_coherent(sandbox, config_dir, writer, ALICE)
    assert live_item(sandbox, config_dir)["claudeAiOauth"]["accessToken"] == (
        latest_tokens(writer)["alice"]
    )
