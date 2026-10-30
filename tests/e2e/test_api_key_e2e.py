"""An API key handed to the real claude through rota's apiKeyHelper, and
taken back. ``claude auth status`` reads the auth source without calling
the API, so this needs no network."""

from __future__ import annotations

import json
import sys

from tests.e2e.sandbox import ACCOUNTS, DRIVE, Sandbox, explain
from tests.e2e.test_e2e import seed

ALICE, BOB = ACCOUNTS[0], ACCOUNTS[1]
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


def test_a_shared_settings_file_keeps_keys_off_until_split(sandbox: Sandbox):
    """Two profiles reading one settings.json (a symlink): a key in one
    would reach the other, so the switch is refused and both stay on their
    subscription logins; after the user's split it goes through, and the
    other profile still runs on its own login."""
    work_root = sandbox.home / "work"
    work_root.mkdir()
    work_config = sandbox.config_dir(".claude-work")
    added = sandbox.rota("profile", "add", "work", "--root", str(work_root),
                         "--data-dir", str(work_root / ".rota"), "--config-dir", work_config)
    assert added.returncode == 0, explain(added)
    seed(sandbox, None, (ALICE,))
    seed(sandbox, work_config, (BOB,), cwd=str(work_root))
    shared = sandbox.home / ".claude" / "settings.json"
    shared.write_text(json.dumps({"theme": "dark"}))
    (sandbox.home / ".claude-work" / "settings.json").symlink_to(shared)

    key = sandbox.run([sys.executable, str(DRIVE), "add-key"], env=sandbox.env(),
                      input=FAKE_KEY + "\n", cwd=str(work_root))
    assert key.returncode == 0, explain(key)
    number = json.loads(key.stdout.splitlines()[-1])["number"]

    refused = sandbox.rota("switch", number, cwd=str(work_root))
    assert refused.returncode != 0, explain(refused)
    assert "shared with" in refused.stdout + refused.stderr, explain(refused)
    assert json.loads(shared.read_text()) == {"theme": "dark"}
    for config, account in ((None, ALICE), (work_config, BOB)):
        status = sandbox.claude_status(config)
        assert status.get("email") == account.email, status
        assert status.get("apiKeySource") in (None, "none"), status
        assert "primaryApiKey" not in sandbox.read_global_config(config)
    assert sandbox.live_token(work_config) == BOB.access_token

    split = sandbox.drive("split-settings", cwd=str(work_root))
    assert split.returncode == 0, explain(split)
    mine = sandbox.home / ".claude-work" / "settings.json"
    assert not mine.is_symlink() and json.loads(mine.read_text()) == {"theme": "dark"}

    switched = sandbox.rota("switch", number, cwd=str(work_root))
    assert switched.returncode == 0, explain(switched)
    assert sandbox.claude_status(work_config).get("apiKeySource") == "apiKeyHelper"
    personal = sandbox.claude_status(None)
    assert personal.get("email") == ALICE.email, personal
    assert personal.get("apiKeySource") in (None, "none"), personal
    assert json.loads(shared.read_text()) == {"theme": "dark"}
