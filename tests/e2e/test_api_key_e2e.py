"""An API key handed to the real claude through rota's apiKeyHelper, and
taken back. ``claude auth status`` reads the auth source without calling
the API, so this needs no network."""

from __future__ import annotations

import json
import sys

from tests.e2e.sandbox import ACCOUNTS, DRIVE, Sandbox, explain
from tests.e2e.test_e2e import seed

ALICE = ACCOUNTS[0]
FAKE_KEY = "sk-ant-api03-" + "e2efakekey" * 9 + "ZZ"


def _settings(box: Sandbox) -> dict:
    path = box.home / ".claude" / "settings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _key_items(box: Sandbox) -> list[str]:
    return [s for s in box.keychain_services() if s.endswith("-active-key")]


def test_switching_to_a_key_and_back(sandbox: Sandbox):
    seed(sandbox, None, (ALICE,))
    env = sandbox.env()
    added = sandbox.run([sys.executable, str(DRIVE), "add-key"], env=env, input=FAKE_KEY + "\n")
    assert added.returncode == 0, explain(added)
    number = json.loads(added.stdout.splitlines()[-1])["number"]
    assert FAKE_KEY not in added.stdout + added.stderr

    switched = sandbox.rota("switch", number)
    assert switched.returncode == 0, explain(switched)
    assert FAKE_KEY not in switched.stdout + switched.stderr
    helper = _settings(sandbox).get("apiKeyHelper")
    assert helper and FAKE_KEY not in helper and "rota-key" in helper, helper
    status = sandbox.claude_status(None)
    assert status.get("apiKeySource") == "apiKeyHelper", status
    assert status.get("authMethod") == "api_key_helper", status
    # What claude runs for the key: the helper prints it from rota's own
    # keychain item, in the sandbox keychain.
    printed = sandbox.run(["/bin/sh", "-c", helper], env=env)
    assert printed.returncode == 0 and printed.stdout == FAKE_KEY + "\n", explain(printed)
    assert len(_key_items(sandbox)) == 1
    # The OAuth login stays underneath (rota's identity file names the key
    # account, for its own active-slot check), so going back is instant.
    assert sandbox.live_token(None) == ALICE.access_token
    assert sandbox.live_email(None) == f"api-key-{number}@token.local"

    back = sandbox.rota("switch", "1")
    assert back.returncode == 0, explain(back)
    assert "apiKeyHelper" not in _settings(sandbox)
    status = sandbox.claude_status(None)
    assert status.get("apiKeySource") in (None, "none"), status
    assert status.get("email") == ALICE.email, status
    assert _key_items(sandbox) == []
