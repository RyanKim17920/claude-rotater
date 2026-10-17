"""rota against the real ``claude`` and the real ``security`` (opt in: ``ROTA_E2E=1``).

Every check reads the result where Claude Code reads it: the Keychain item
for the profile (through ``/usr/bin/security``), the profile's
``.claude.json``, and ``claude auth status --json`` from the real binary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.sandbox import ACCOUNTS, Sandbox, claude_service, explain

ALICE, BOB, CAROL = ACCOUNTS

# The two ways a profile reaches Claude Code: the default one (no
# CLAUDE_CONFIG_DIR; unsuffixed Keychain item, ~/.claude.json) and one named
# by CLAUDE_CONFIG_DIR (hashed item, <dir>/.claude.json).
PROFILES = ["default", "config-dir"]


def profile_dir(box: Sandbox, profile: str) -> str | None:
    return None if profile == "default" else box.config_dir(".claude-e2e")


def assert_live(box: Sandbox, config_dir: str | None, account) -> None:
    """The profile's live login is ``account``, as Claude Code sees it."""
    assert box.live_token(config_dir) == account.access_token, (
        f"Keychain item {claude_service(config_dir)!r} holds "
        f"{box.live_token(config_dir)!r}, want {account.tag}'s token"
    )
    assert box.live_email(config_dir) == account.email
    status = box.claude_status(config_dir)
    assert status.get("loggedIn") is True, status
    assert status.get("email") == account.email, status


def assert_live_identity(box: Sandbox, config_dir: str | None, account) -> None:
    """Claude Code reports ``account`` for the profile (any token of it)."""
    assert box.live_email(config_dir) == account.email
    status = box.claude_status(config_dir)
    assert status.get("loggedIn") is True and status.get("email") == account.email, status


def seed(box: Sandbox, config_dir: str | None, accounts, **where) -> None:
    """Log each account in (as claude would) and add it the TUI's way
    (``a`` → current login): slots 1..n, the last one live."""
    for account in accounts:
        box.plant_login(account, config_dir)
        result = box.drive("add-current", config_dir=config_dir, **where)
        assert result.returncode == 0, explain(result)


def managed_emails(box: Sandbox, data_dir: str | None = None) -> list[str]:
    seq = Path(data_dir or box.home / ".rota") / "sequence.json"
    accounts = json.loads(seq.read_text()).get("accounts", {})
    return sorted(a.get("email") for a in accounts.values())


def status_json(box: Sandbox, **kw) -> dict:
    result = box.rota("status", "--json", **kw)
    assert result.returncode == 0, explain(result)
    return json.loads(result.stdout)


# -- what the real claude does -------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_claude_reads_the_item_named_for_its_config_dir(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    assert sandbox.claude_status(config_dir).get("loggedIn") is False
    sandbox.plant_login(ALICE, config_dir)
    status = sandbox.claude_status(config_dir)
    assert status.get("loggedIn") is True, status
    assert status.get("email") == ALICE.email
    assert sandbox.keychain_services() == [claude_service(config_dir)]


def test_claude_ignores_the_unsuffixed_item_under_a_config_dir(sandbox: Sandbox):
    sandbox.plant_login(ALICE, None)
    config_dir = sandbox.config_dir(".claude-other")
    Path(config_dir, ".claude.json").write_text(
        json.dumps({"oauthAccount": ALICE.oauth_account()}))
    assert sandbox.claude_status(config_dir).get("loggedIn") is False


def test_claude_auth_login_takes_the_flags_rota_passes(sandbox: Sandbox):
    help_text = sandbox.claude("auth", "login", "--help").stdout
    for flag in ("--claudeai", "--console", "--email"):
        assert flag in help_text, help_text
    from claude_rotater.login import LOGIN_METHODS

    for method in LOGIN_METHODS:
        assert f"--{method}" in help_text


# -- switching -----------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_cli_switch_moves_the_live_login(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, ACCOUNTS)
    assert_live(sandbox, config_dir, CAROL)

    for target, account in (("1", ALICE), (BOB.email, BOB), ("3", CAROL), ("1", ALICE)):
        result = sandbox.rota("switch", target, config_dir=config_dir)
        assert result.returncode == 0, explain(result)
        assert_live(sandbox, config_dir, account)


@pytest.mark.parametrize("profile", PROFILES)
def test_cli_rotate_moves_the_live_login(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, ACCOUNTS)
    result = sandbox.rota("switch", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert_live(sandbox, config_dir, ALICE)  # 3 → wraps to 1


@pytest.mark.parametrize("profile", PROFILES)
def test_status_reports_the_live_account(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))
    assert BOB.email in json.dumps(status_json(sandbox, config_dir=config_dir))


@pytest.mark.parametrize("profile", PROFILES)
def test_tui_switch_moves_the_live_login(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, ACCOUNTS)
    result = sandbox.drive("tui", "switch", "1", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert_live(sandbox, config_dir, ALICE)


@pytest.mark.parametrize("profile", PROFILES)
def test_switch_survives_claude_rewriting_its_login(sandbox: Sandbox, profile):
    """Claude Code refreshes tokens in place: a switch away and back must
    carry the refreshed credential, not the one captured at add time."""
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))
    refreshed = BOB.credential()
    refreshed["claudeAiOauth"]["accessToken"] += "-refreshed"
    sandbox.keychain_set(claude_service(config_dir), json.dumps(refreshed))
    for target in ("1", "2"):
        result = sandbox.rota("switch", target, config_dir=config_dir)
        assert result.returncode == 0, explain(result)
    assert sandbox.live_token(config_dir) == BOB.access_token + "-refreshed"


# -- logging in ----------------------------------------------------------------


def _login_env(account, suffix: str = "") -> dict[str, str]:
    return {
        "ROTA_E2E_LOGIN_CREDENTIAL": json.dumps(account.credential(suffix=suffix)),
        "ROTA_E2E_LOGIN_ACCOUNT": json.dumps(account.oauth_account()),
    }


def _shim_calls(box: Sandbox) -> list[dict]:
    log = box.root / "shim.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("entry", ["api", "tui"])
def test_staged_login_adds_the_account(sandbox: Sandbox, profile, entry):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE,))
    before = set(sandbox.keychain_services())

    if entry == "api":
        result = sandbox.drive("login", config_dir=config_dir, shim=True, **_login_env(BOB))
    else:
        result = sandbox.drive("tui", "login", config_dir=config_dir, shim=True, **_login_env(BOB))
    assert result.returncode == 0, explain(result)
    if entry == "tui":
        said = json.loads(result.stdout.splitlines()[-1])["said"]
        assert not any(s.startswith(("error", "warn")) for s in said), said

    calls = _shim_calls(sandbox)
    logins = [c for c in calls if c["argv"][:2] == ["auth", "login"]]
    assert logins and logins[0]["argv"] == ["auth", "login", "--claudeai"], calls
    stage = logins[0]["CLAUDE_CONFIG_DIR"]
    assert stage and stage != config_dir

    # The active login is untouched, the new account is managed, the stage is gone.
    assert_live(sandbox, config_dir, ALICE)
    assert managed_emails(sandbox) == [ALICE.email, BOB.email]
    assert claude_service(stage) not in sandbox.keychain_services()
    assert not Path(stage).exists()
    assert set(sandbox.keychain_services()) - before <= {
        s for s in sandbox.keychain_services() if s.startswith("rota")
    }

    result = sandbox.rota("switch", BOB.email, config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert_live(sandbox, config_dir, BOB)


@pytest.mark.parametrize("profile", PROFILES)
def test_relogin_of_the_active_account(sandbox: Sandbox, profile):
    """A dead live login is fixed by logging the same account in again
    (the TUI's ``l`` on its row pre-fills the address)."""
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))
    result = sandbox.drive("login", "--email", BOB.email, config_dir=config_dir, shim=True,
                           **_login_env(BOB, "-relogin"))
    assert result.returncode == 0, explain(result)
    assert sandbox.live_token(config_dir) == BOB.access_token + "-relogin"
    assert_live_identity(sandbox, config_dir, BOB)
    for target in ("1", "2"):
        result = sandbox.rota("switch", target, config_dir=config_dir)
        assert result.returncode == 0, explain(result)
    assert sandbox.live_token(config_dir) == BOB.access_token + "-relogin"


@pytest.mark.parametrize("profile", PROFILES)
def test_relogin_of_an_idle_account(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))
    result = sandbox.drive("login", "--email", ALICE.email, config_dir=config_dir, shim=True,
                           **_login_env(ALICE, "-relogin"))
    assert result.returncode == 0, explain(result)
    assert_live(sandbox, config_dir, BOB)
    assert managed_emails(sandbox) == [ALICE.email, BOB.email]
    result = sandbox.rota("switch", "1", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert sandbox.live_token(config_dir) == ALICE.access_token + "-relogin"
    assert_live_identity(sandbox, config_dir, ALICE)


# -- expired tokens ------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_switch_to_an_account_whose_stored_token_expired(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    sandbox.plant_login(ALICE, config_dir, ALICE.credential(expired=True))
    result = sandbox.drive("add-current", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    seed(sandbox, config_dir, (BOB,))
    result = sandbox.rota("switch", "1", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert (sandbox.live_token(config_dir) or "").startswith(ALICE.access_token)
    assert_live_identity(sandbox, config_dir, ALICE)


@pytest.mark.parametrize("profile", PROFILES)
def test_switch_away_from_an_expired_live_login(sandbox: Sandbox, profile):
    config_dir = profile_dir(sandbox, profile)
    seed(sandbox, config_dir, (ALICE, BOB))
    sandbox.keychain_set(claude_service(config_dir), json.dumps(BOB.credential(expired=True)))
    result = sandbox.rota("switch", "1", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert_live(sandbox, config_dir, ALICE)
    result = sandbox.rota("switch", "2", config_dir=config_dir)
    assert result.returncode == 0, explain(result)
    assert (sandbox.live_token(config_dir) or "").startswith(BOB.access_token)
    assert_live_identity(sandbox, config_dir, BOB)


# -- profile selection ---------------------------------------------------------


def test_profile_is_picked_by_the_current_directory(sandbox: Sandbox):
    home = sandbox.home
    work_root = home / "work"
    (work_root / "repo" / "sub").mkdir(parents=True)
    work_data = str(work_root / ".rota")
    work_config = sandbox.config_dir(".claude-work")

    result = sandbox.rota("profile", "add", "work", "--root", str(work_root),
                          "--data-dir", work_data, "--config-dir", work_config)
    assert result.returncode == 0, explain(result)

    # Default profile, from outside the root, with nothing exported.
    seed(sandbox, None, (ALICE,))
    # Work profile, from inside the root, with nothing exported either:
    # rota (and the claude it is pointed at) must pick the work dirs itself.
    sandbox.plant_login(BOB, work_config)
    result = sandbox.drive("add-current", cwd=str(work_root / "repo"))
    assert result.returncode == 0, explain(result)
    assert json.loads(result.stdout.splitlines()[-1])["backup_dir"] == work_data

    inside = json.dumps(status_json(sandbox, cwd=str(work_root / "repo" / "sub")))
    outside = json.dumps(status_json(sandbox, cwd=str(home)))
    assert BOB.email in inside and ALICE.email not in inside, inside
    assert ALICE.email in outside and BOB.email not in outside, outside
    # Switching inside the root moves the work profile's login only.
    sandbox.plant_login(CAROL, work_config)
    result = sandbox.drive("add-current", cwd=str(work_root))
    assert result.returncode == 0, explain(result)
    assert managed_emails(sandbox, work_data) == [BOB.email, CAROL.email]
    assert managed_emails(sandbox) == [ALICE.email]
    result = sandbox.rota("switch", "1", cwd=str(work_root / "repo"))
    assert result.returncode == 0, explain(result)
    assert_live(sandbox, work_config, BOB)
    assert_live(sandbox, None, ALICE)


def test_explicit_config_dir_inside_a_root_keeps_the_default_data_dir(sandbox: Sandbox):
    work_root = sandbox.home / "work"
    work_root.mkdir()
    work_config = sandbox.config_dir(".claude-work")
    other = sandbox.config_dir(".claude-other")
    result = sandbox.rota("profile", "add", "work", "--root", str(work_root),
                          "--data-dir", str(work_root / ".rota"), "--config-dir", work_config)
    assert result.returncode == 0, explain(result)
    sandbox.plant_login(CAROL, other)
    result = sandbox.drive("add-current", config_dir=other, cwd=str(work_root))
    assert result.returncode == 0, explain(result)
    assert json.loads(result.stdout.splitlines()[-1])["backup_dir"] == str(sandbox.home / ".rota")
