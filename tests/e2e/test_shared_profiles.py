"""Several rotas at once, as separate processes: one engine per data dir,
and one poller for an account two profiles hold."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from tests.e2e.sandbox import ACCOUNTS, Sandbox, explain
from tests.e2e.test_e2e import seed, status_json

ALICE, _BOB, _CAROL = ACCOUNTS


def _usage_calls(box: Sandbox, tag: str) -> int:
    return sum(1 for path, who in box.api.requests if path.startswith("/api/oauth/usage") and who == tag)


def _five_hour_pct(status: dict) -> float:
    active = status["active"]
    assert active["email"] == ALICE.email, active
    return active["usage"]["fiveHour"]["pct"]


def test_two_profiles_holding_one_account_poll_it_once(sandbox: Sandbox):
    if sandbox.api is None:
        pytest.skip("needs the fake API")
    work_root = sandbox.home / "work"
    work_root.mkdir()
    work_config = sandbox.config_dir(".claude-work")
    result = sandbox.rota("profile", "add", "work", "--root", str(work_root),
                          "--data-dir", str(work_root / ".rota"), "--config-dir", work_config)
    assert result.returncode == 0, explain(result)
    seed(sandbox, None, (ALICE,))
    # The same account logged in separately in the work profile: its own
    # token, the same identity.
    sandbox.plant_login(ALICE, work_config, ALICE.credential(suffix="-work"))
    result = sandbox.drive("add-current", cwd=str(work_root))
    assert result.returncode == 0, explain(result)

    sandbox.api.requests.clear()
    sandbox.api.usage["alice"] = (37.0, 12.0)
    personal = status_json(sandbox, cwd=str(sandbox.home))
    assert _usage_calls(sandbox, "alice") == 1
    work = status_json(sandbox, cwd=str(work_root))
    assert _usage_calls(sandbox, "alice") == 1, "the work profile fetched again"
    assert _five_hour_pct(personal) == _five_hour_pct(work) == 37.0


def test_one_engine_per_data_dir_and_takeover_after_a_crash(sandbox: Sandbox):
    seed(sandbox, None, (ALICE,))
    env = sandbox.env()
    engine = subprocess.Popen(
        [*sandbox.rota_argv(), "auto", "--json"], env=env, cwd=str(sandbox.home),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        lock = sandbox.home / ".rota" / "engine.lock"
        deadline = time.time() + 30
        while time.time() < deadline and str(engine.pid) not in (lock.read_text() if lock.exists() else ""):
            time.sleep(0.1)
        assert str(engine.pid) in lock.read_text(), "the first engine never took the lock"

        second = sandbox.rota("auto", "--json", "--once")
        assert second.returncode != 0, explain(second)
        assert "engine-busy" in second.stdout, explain(second)
        assert f"pid {engine.pid}" in second.stdout, explain(second)

        os.kill(engine.pid, signal.SIGKILL)  # a crash: no clean release
        engine.wait(timeout=10)
        third = sandbox.rota("auto", "--json", "--once")
        assert "engine-busy" not in third.stdout, explain(third)
    finally:
        if engine.poll() is None:
            engine.kill()
            engine.wait(timeout=10)
