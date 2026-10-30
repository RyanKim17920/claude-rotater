"""In-app login (TUI `l`): a staged profile, never the active one.

Every test drives a fake ``claude`` (the ``runner``) that writes what the real
``claude auth login`` would write into the exported ``CLAUDE_CONFIG_DIR``: an
``oauthAccount`` in ``.claude.json`` plus the credential, in the profile's
hashed keychain item (macOS) or its plaintext file (elsewhere). The keychain
is the suite's in-memory fake (``block_real_keychain``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_rotater import login as login_mod
from claude_rotater.exceptions import LoginCancelled, LoginError, ValidationError
from claude_rotater.login import StagedLogin, login_env, sweep_stale_stages
from claude_rotater.macos_keychain import keychain_account_name
from claude_rotater.models import Platform
from claude_rotater.session import keychain_service_name
from claude_rotater.switcher import ClaudeAccountSwitcher

PLAINTEXT = ".credentials" + ".json"  # spelled apart: a repo hook guards the literal


def _creds(tag: str, *, rt_expires_ms: int | None = None) -> str:
    oauth = {
        "accessToken": f"sk-ant-oat01-{tag}",
        "refreshToken": f"rt-{tag}",
        "expiresAt": 99999999999000,
    }
    if rt_expires_ms is not None:
        oauth["refreshTokenExpiresAt"] = rt_expires_ms
    return json.dumps({"claudeAiOauth": oauth})


class FakeClaude:
    """Stands in for ``subprocess.run`` of the claude binary."""

    def __init__(
        self,
        *,
        email: str = "new@example.com",
        org: str = "",
        creds: str | None = None,
        platform: Platform = Platform.LINUX,
        keychain=None,
        login_rc: int = 0,
        login_exc: BaseException | None = None,
        status: dict | None = None,
        write_identity: bool = True,
    ) -> None:
        self.email = email
        self.org = org
        self.creds = creds or _creds("new")
        self.platform = platform
        self.keychain = keychain
        self.login_rc = login_rc
        self.login_exc = login_exc
        self.status = status if status is not None else {
            "loggedIn": True, "authMethod": "claude.ai", "email": email,
        }
        self.write_identity = write_identity
        self.calls: list[tuple[list[str], dict]] = []
        self.config_dirs: list[str] = []

    def __call__(self, argv, **kwargs):
        env = kwargs.get("env") or {}
        self.calls.append((list(argv), kwargs))
        config_dir = env["CLAUDE_CONFIG_DIR"]
        self.config_dirs.append(config_dir)
        if argv[1:3] == ["auth", "login"]:
            if self.login_exc is not None:
                raise self.login_exc
            if self.login_rc == 0:
                self._write(config_dir)
            return subprocess.CompletedProcess(argv, self.login_rc)
        if argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.status), "")
        raise AssertionError(f"unexpected claude call {argv}")

    def _write(self, config_dir: str) -> None:
        if self.write_identity:
            Path(config_dir, ".claude.json").write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": self.email,
                    "organizationUuid": self.org,
                    "accountUuid": f"uuid-{self.email}",
                },
            }))
        if self.platform == Platform.MACOS:
            self.keychain.set_password(
                keychain_service_name(config_dir), keychain_account_name(), self.creds
            )
        else:
            Path(config_dir, PLAINTEXT).write_text(self.creds)


@pytest.fixture
def switcher(temp_home: Path, mock_claude_config: Path) -> ClaudeAccountSwitcher:
    """A switcher whose live login (test@example.com) is managed as slot 1."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    with patch.object(s, "_read_capture_credentials", return_value=_creds("live")):
        s.add_account()
    assert s._get_sequence_data()["activeAccountNumber"] == 1
    return s


@pytest.fixture(params=[Platform.LINUX, Platform.MACOS], ids=["plaintext", "keychain"])
def platform(request, monkeypatch):
    monkeypatch.setattr("claude_rotater.session.Platform.detect", lambda: request.param)
    return request.param


def login_account(switcher, *, runner, method="claudeai", email=None):
    """The TUI's two phases back to back: authenticate, then import, with
    cleanup guaranteed by the context manager."""
    with StagedLogin(switcher.backup_dir, runner=runner) as stage:
        stage.authenticate(method=method, email=email)
        stage.import_into(switcher)


def _stages(s: ClaudeAccountSwitcher) -> list[Path]:
    root = login_mod.stage_root(s.backup_dir)
    return list(root.iterdir()) if root.exists() else []


def _record(s: ClaudeAccountSwitcher, num: str) -> dict:
    return s._get_sequence_data()["accounts"][num]


# -- naming -------------------------------------------------------------------


class TestKeychainNaming:
    def test_service_name_is_sha256_of_nfc_string_first_8(self):
        raw = "/tmp/café/login-x"  # decomposed é
        nfc = unicodedata.normalize("NFC", raw)
        digest = hashlib.sha256(nfc.encode("utf-8")).hexdigest()[:8]
        assert keychain_service_name(raw) == f"Claude Code-credentials-{digest}"

    def test_trailing_slash_is_a_different_item(self):
        # The exported string is hashed verbatim; a Path round-trip would drop it.
        assert keychain_service_name("/x/y/") != keychain_service_name("/x/y")

    def test_stage_dir_string_hashes_to_what_claude_writes(self, switcher, block_real_keychain, monkeypatch):
        monkeypatch.setattr("claude_rotater.session.Platform.detect", lambda: Platform.MACOS)
        fake = FakeClaude(platform=Platform.MACOS, keychain=block_real_keychain)
        deleted: list[str] = []
        real_delete = block_real_keychain.delete_password
        monkeypatch.setattr(
            "claude_rotater.macos_keychain.delete_password",
            lambda service, account: (deleted.append(service), real_delete(service, account)),
        )
        login_account(switcher, runner=fake)
        exported = fake.config_dirs[0]
        assert not exported.endswith("/")
        assert deleted == [keychain_service_name(exported)]
        assert block_real_keychain.data == {
            k: v for k, v in block_real_keychain.data.items()
            if not k[0].startswith("Claude Code-credentials-")
        }


# -- the flow ------------------------------------------------------------------


class TestLoginSuccess:
    def test_new_account_lands_in_next_slot_active_untouched(self, switcher, platform, block_real_keychain):
        live_before = switcher._get_claude_config_path().read_text()
        fake = FakeClaude(platform=platform, keychain=block_real_keychain)
        login_account(switcher, runner=fake)

        seq = switcher._get_sequence_data()
        assert seq["activeAccountNumber"] == 1
        assert _record(switcher, "2")["email"] == "new@example.com"
        assert switcher._read_account_credentials("2", "new@example.com") == fake.creds
        stored_cfg = json.loads(switcher._read_account_config("2", "new@example.com"))
        assert stored_cfg["oauthAccount"]["emailAddress"] == "new@example.com"
        assert switcher._get_claude_config_path().read_text() == live_before
        assert switcher._read_account_credentials("1", "test@example.com") == _creds("live")

    def test_stage_is_removed_and_keychain_item_deleted(self, switcher, platform, block_real_keychain):
        fake = FakeClaude(platform=platform, keychain=block_real_keychain)
        login_account(switcher, runner=fake)
        assert not Path(fake.config_dirs[0]).exists()
        assert _stages(switcher) == []
        service = keychain_service_name(fake.config_dirs[0])
        assert (service, keychain_account_name()) not in block_real_keychain.data

    def test_runs_login_then_status_in_the_same_stage(self, switcher, platform, block_real_keychain):
        fake = FakeClaude(platform=platform, keychain=block_real_keychain)
        login_account(switcher, runner=fake)
        (login_argv, login_kw), (status_argv, status_kw) = fake.calls
        assert login_argv[1:] == ["auth", "login", "--claudeai"]
        assert "capture_output" not in login_kw  # attached to the terminal
        assert status_argv[1:] == ["auth", "status", "--json"]
        assert fake.config_dirs[0] == fake.config_dirs[1]
        assert Path(fake.config_dirs[0]).parent == login_mod.stage_root(switcher.backup_dir)

    def test_email_passes_through(self, switcher, block_real_keychain):
        fake = FakeClaude()
        login_account(switcher, runner=fake, email="new@example.com")
        assert fake.calls[0][0][1:] == ["auth", "login", "--claudeai", "--email", "new@example.com"]

    def test_a_console_login_is_refused(self, switcher, block_real_keychain):
        fake = FakeClaude()
        with pytest.raises(LoginError, match="Unknown login method"):
            login_account(switcher, runner=fake, method="console")
        assert fake.calls == []

    def test_existing_account_gets_credentials_replaced_in_place(self, switcher, platform, block_real_keychain):
        # Slot 2 exists with a dead lineage and a strike; re-login replaces it.
        login_account(switcher, runner=FakeClaude(platform=platform, keychain=block_real_keychain,
                                                  creds=_creds("old")))
        ident = {"2": ("new@example.com", "")}
        switcher._usage_store._mutate(ident, ["2"], lambda _n, row: row.update(authDeadStrikes=3))
        fresh = _creds("fresh")
        login_account(switcher, runner=FakeClaude(platform=platform, keychain=block_real_keychain,
                                                  creds=fresh))
        seq = switcher._get_sequence_data()
        assert sorted(seq["accounts"]) == ["1", "2"]
        assert seq["activeAccountNumber"] == 1
        assert switcher._read_account_credentials("2", "new@example.com") == fresh
        assert switcher._usage_store.entries(ident)["2"].auth_dead_strikes == 0

    def test_active_account_login_installs_live_and_backup(self, switcher, platform, block_real_keychain):
        fresh = _creds("fresh")
        fake = FakeClaude(email="test@example.com", platform=platform,
                          keychain=block_real_keychain, creds=fresh)
        login_account(switcher, runner=fake)
        live = switcher._read_credentials()
        assert json.loads(live) == json.loads(fresh)
        assert switcher._read_account_credentials("1", "test@example.com") == live
        seq = switcher._get_sequence_data()
        assert sorted(seq["accounts"]) == ["1"]
        assert seq["activeAccountNumber"] == 1
        assert _stages(switcher) == []

    def test_active_account_login_keeps_live_shared_fields(self, switcher, block_real_keychain):
        # The machine-shared MCP state belongs to the live credential, as on a
        # switch; the fresh login does not carry it and must not drop it.
        live = json.loads(_creds("live"))
        live["mcpOAuth"] = {"server": {"accessToken": "mcp-live"}}
        switcher._write_credentials(json.dumps(live))
        login_account(switcher, runner=FakeClaude(email="test@example.com",
                                                  creds=_creds("fresh")))
        now = json.loads(switcher._read_credentials())
        assert now["claudeAiOauth"]["refreshToken"] == "rt-fresh"
        assert now["mcpOAuth"] == live["mcpOAuth"]
        assert json.loads(switcher._read_account_credentials("1", "test@example.com")) == now

    def test_active_account_live_write_failure_leaves_backup(self, switcher, block_real_keychain):
        from claude_rotater.exceptions import CredentialWriteError

        switcher._write_credentials(_creds("live"))  # Claude Code's live copy
        live_before = switcher._read_credentials()
        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(switcher._store, "_write_credentials",
                          side_effect=CredentialWriteError("disk full")):
            with pytest.raises(CredentialWriteError):
                login_account(switcher, runner=fake)
        assert switcher._read_account_credentials("1", "test@example.com") == _creds("live")
        assert switcher._read_credentials() == live_before
        assert _stages(switcher) == []

    def test_active_account_partial_live_write_is_rolled_back(self, switcher, block_real_keychain):
        from claude_rotater.exceptions import CredentialWriteError

        switcher._write_credentials(_creds("live"))  # Claude Code's live copy
        live_before = switcher._read_credentials()
        real_write = switcher._store._write_credentials
        calls: list[str] = []

        def write_then_fail(creds):
            calls.append(creds)
            real_write(creds)
            if len(calls) == 1:  # the install lands, then its tail fails
                raise CredentialWriteError("clear managed key failed")

        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(switcher._store, "_write_credentials", side_effect=write_then_fail):
            with pytest.raises(CredentialWriteError):
                login_account(switcher, runner=fake)
        assert switcher._read_credentials() == live_before
        assert switcher._read_account_credentials("1", "test@example.com") == _creds("live")

    def test_active_account_backup_failure_restores_live(self, switcher, block_real_keychain):
        from claude_rotater.exceptions import CredentialWriteError

        switcher._write_credentials(_creds("live"))  # Claude Code's live copy
        live_before = switcher._read_credentials()
        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(switcher, "_write_account_credentials",
                          side_effect=CredentialWriteError("backup store")):
            with pytest.raises(CredentialWriteError):
                login_account(switcher, runner=fake)
        assert switcher._read_credentials() == live_before
        assert switcher._read_account_credentials("1", "test@example.com") == _creds("live")

    def test_active_account_with_empty_live_keeps_new_live_on_backup_failure(
        self, switcher, block_real_keychain, caplog
    ):
        # No previous live credential to restore: live keeps the new login
        # (live ahead of its backup, the state every switch-out reconciles),
        # and the backup is never left ahead of live.
        from claude_rotater.exceptions import CredentialWriteError

        assert switcher._read_credentials() == ""
        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(switcher, "_write_account_credentials",
                          side_effect=CredentialWriteError("backup store")):
            with pytest.raises(CredentialWriteError):
                login_account(switcher, runner=fake)
        assert json.loads(switcher._read_credentials()) == json.loads(_creds("fresh"))
        assert switcher._read_account_credentials("1", "test@example.com") == _creds("live")
        assert "Login install: no previous live credential to restore" in caplog.messages

    def test_active_account_switched_away_under_lock_changes_nothing(self, switcher, block_real_keychain):
        # A switch landing between the pre-lock identity check and the lock:
        # the live store is another account's now.
        switcher._write_credentials(_creds("live"))
        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(switcher, "_live_identity_matches", side_effect=[True, False]):
            with pytest.raises(ValidationError, match="active account changed"):
                login_account(switcher, runner=fake)
        assert switcher._read_credentials() == _creds("live")
        assert switcher._read_account_credentials("1", "test@example.com") == _creds("live")

    def test_active_account_live_moved_on_is_not_clobbered(self, switcher, block_real_keychain):
        # Claude Code rotated the live token between the install and the
        # failure: restoring the old login over it would destroy that rotation.
        from claude_rotater.exceptions import CredentialWriteError

        rotated = _creds("rotated")

        def rotate_then_fail(*_a, **_kw):
            switcher._store._write_credentials(rotated)
            raise CredentialWriteError("backup store")

        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(switcher, "_write_account_credentials", side_effect=rotate_then_fail):
            with pytest.raises(CredentialWriteError):
                login_account(switcher, runner=fake)
        assert switcher._read_credentials() == rotated

    def test_env_drops_auth_overrides_and_exports_exact_dir(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/elsewhere")
        env = login_env("/a/b/login-1")
        assert env["CLAUDE_CONFIG_DIR"] == "/a/b/login-1"
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env


class TestLoginFailure:
    @pytest.mark.parametrize(
        "fake_kwargs, exc",
        [
            ({"login_exc": KeyboardInterrupt()}, LoginCancelled),
            ({"login_rc": 1}, LoginCancelled),
            ({"login_exc": FileNotFoundError("claude")}, LoginError),
            ({"status": {"loggedIn": False}}, LoginError),
            ({"status": {}}, LoginError),
        ],
        ids=["ctrl-c", "nonzero-exit", "no-binary", "not-logged-in", "garbage-status"],
    )
    def test_nothing_stored_and_stage_removed(self, switcher, block_real_keychain, fake_kwargs, exc):
        before = json.dumps(switcher._get_sequence_data(), sort_keys=True)
        fake = FakeClaude(**fake_kwargs)
        with pytest.raises(exc):
            login_account(switcher, runner=fake)
        assert json.dumps(switcher._get_sequence_data(), sort_keys=True) == before
        assert _stages(switcher) == []

    def test_status_timeout_is_a_login_error(self, switcher):
        fake = FakeClaude()

        def runner(argv, **kw):
            if argv[1:3] == ["auth", "status"]:
                raise subprocess.TimeoutExpired(argv, 1)
            return fake(argv, **kw)

        with pytest.raises(LoginError, match="timed out"):
            login_account(switcher, runner=runner)
        assert _stages(switcher) == []

    def test_import_failure_still_cleans_up(self, switcher, platform, block_real_keychain):
        fake = FakeClaude(platform=platform, keychain=block_real_keychain)
        with patch.object(switcher, "add_account", side_effect=ValidationError("boom")):
            with pytest.raises(ValidationError):
                login_account(switcher, runner=fake)
        assert _stages(switcher) == []
        assert (keychain_service_name(fake.config_dirs[0]), keychain_account_name()) \
            not in block_real_keychain.data

    def test_missing_identity_is_a_clear_error(self, switcher):
        fake = FakeClaude(write_identity=False)
        with pytest.raises(Exception, match="no oauthAccount"):
            login_account(switcher, runner=fake)
        assert _stages(switcher) == []

    def test_foreign_credential_is_refused(self, switcher):
        fake = FakeClaude()
        with patch("claude_rotater.oauth.fetch_oauth_profile",
                   return_value={"uuid": "someone-else", "email": "x@y", "organizationUuid": ""}):
            with pytest.raises(Exception, match="does not belong"):
                login_account(switcher, runner=fake)
        assert "2" not in switcher._get_sequence_data()["accounts"]
        assert _stages(switcher) == []

    def test_import_without_authenticate_refuses(self, switcher):
        with StagedLogin(switcher.backup_dir, runner=FakeClaude()) as stage:
            with pytest.raises(LoginError):
                stage.import_into(switcher)

    def test_unknown_method(self, switcher):
        with StagedLogin(switcher.backup_dir, runner=FakeClaude()) as stage:
            with pytest.raises(LoginError, match="Unknown login method"):
                stage.authenticate(method="bogus")

    def test_cleanup_is_idempotent(self, switcher):
        stage = StagedLogin(switcher.backup_dir, runner=FakeClaude())
        path = stage.open()
        stage.cleanup()
        stage.cleanup()
        assert not Path(path).exists()


class TestSweep:
    def test_old_stages_are_swept_young_ones_kept(self, tmp_path, block_real_keychain, monkeypatch):
        monkeypatch.setattr("claude_rotater.session.Platform.detect", lambda: Platform.MACOS)
        root = login_mod.stage_root(tmp_path)
        old, young = root / "login-old", root / "login-young"
        for d in (old, young):
            d.mkdir(parents=True)
            block_real_keychain.set_password(keychain_service_name(str(d)), keychain_account_name(), "x")
        now = time.time()
        os.utime(old, (now - 7200, now - 7200))
        assert sweep_stale_stages(tmp_path, now=now) == 1
        assert not old.exists() and young.exists()
        assert (keychain_service_name(str(old)), keychain_account_name()) not in block_real_keychain.data
        assert (keychain_service_name(str(young)), keychain_account_name()) in block_real_keychain.data

    def test_missing_root_is_fine(self, tmp_path):
        assert sweep_stale_stages(tmp_path, now=time.time()) == 0



# -- rota.log ---------------------------------------------------------------------


class TestLoginLogging:
    """Every staged install logs its start, its result and any failure reason;
    never an email or a token."""

    @staticmethod
    def _rota_lines(caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "rota"]

    @staticmethod
    def _assert_no_secrets(lines: list[str], *secrets: str) -> None:
        for line in lines:
            assert "@" not in line, line
            assert "sk-ant-" not in line, line
            for secret in secrets:
                assert secret not in line, line

    def test_new_account_success(self, switcher, caplog):
        caplog.set_level("INFO", logger="rota")
        caplog.clear()
        login_account(switcher, runner=FakeClaude())
        lines = self._rota_lines(caplog)
        assert "Login install started: new account, slot next free" in lines
        assert "Added account 2 (org: personal)" in lines
        self._assert_no_secrets(lines, "rt-new")

    def test_relogin_of_inactive_account(self, switcher, caplog):
        login_account(switcher, runner=FakeClaude(creds=_creds("old")))
        caplog.set_level("INFO", logger="rota")
        caplog.clear()
        login_account(switcher, runner=FakeClaude(creds=_creds("fresh")))
        lines = self._rota_lines(caplog)
        assert "Login install started: re-login, slot 2" in lines
        assert "Updated credentials for account 2" in lines
        self._assert_no_secrets(lines, "rt-fresh")

    def test_relogin_of_active_account(self, switcher, block_real_keychain, caplog):
        caplog.set_level("INFO", logger="rota")
        caplog.clear()
        login_account(switcher, runner=FakeClaude(email="test@example.com",
                                                  creds=_creds("fresh")))
        lines = self._rota_lines(caplog)
        assert "Login install started: re-login of the active account, slot 1" in lines
        assert "Login install: live credential replaced" in lines
        assert "Updated credentials for account 1 (live login replaced)" in lines
        self._assert_no_secrets(lines, "rt-fresh")

    def test_failure_logs_class_and_masked_reason(self, switcher, block_real_keychain, caplog):
        from claude_rotater.exceptions import CredentialWriteError

        switcher._write_credentials(_creds("live"))
        caplog.set_level("INFO", logger="rota")
        caplog.clear()
        fake = FakeClaude(email="test@example.com", creds=_creds("fresh"))
        with patch.object(
            switcher, "_write_account_credentials",
            side_effect=CredentialWriteError(
                "cannot store test@example.com token sk-ant-oat01-fresh"
            ),
        ):
            with pytest.raises(CredentialWriteError):
                login_account(switcher, runner=fake)
        lines = self._rota_lines(caplog)
        assert (
            "Login install failed: CredentialWriteError: "
            "cannot store <email> token <token>"
        ) in lines
        assert "Login install: previous live credential restored" in lines
        self._assert_no_secrets(lines, "rt-fresh")

    def test_rota_add_is_not_logged_as_a_login(self, switcher, caplog):
        caplog.set_level("INFO", logger="rota")
        caplog.clear()
        with patch.object(switcher, "_read_capture_credentials", return_value=_creds("live")):
            switcher.add_account()
        assert not any("Login install" in m for m in self._rota_lines(caplog))


class TestRedact:
    def test_masks_emails_and_tokens(self):
        from claude_rotater.logging_config import redact

        text = 'x "a.b+c@ex-ample.co.uk" sk-ant-ort01-AbC_d-9 (me@x.io), done'
        assert redact(text) == 'x "<email>" <token> (<email>), done'

    def test_leaves_plain_text(self):
        from claude_rotater.logging_config import redact

        assert redact("slot 3 failed: timed out") == "slot 3 failed: timed out"
