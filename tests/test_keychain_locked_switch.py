"""A locked macOS Keychain that still holds the old login refuses the switch.

Claude Code reads the Keychain before the plaintext file. When rota cannot
write the incoming login to the Keychain AND cannot clear the item there, a
file fallback "succeeds" while sessions go back to the old account as soon
as the Keychain unlocks. So that switch is refused with nothing changed. The
fallback stays where nothing can shadow the file: the old item was cleared
(or never existed), or a clear was already verified this process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_rotater import macos_keychain
from claude_rotater.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
)
from claude_rotater.exceptions import CredentialWriteError, KeychainLockedError
from claude_rotater.models import Platform
from claude_rotater.switcher import ClaudeAccountSwitcher
from tests.test_autoswitch import EngineHarness

OLD = json.dumps({"claudeAiOauth": {"accessToken": "sk-old", "refreshToken": "rt-old"}})
NEW = json.dumps({"claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new"}})
KEY = "sk-ant-api03-" + "m" * 40


def _locked(*_a, **_kw):
    raise macos_keychain.KeychainError("locked")


@pytest.fixture
def store(temp_home: Path, monkeypatch):
    monkeypatch.setattr("claude_rotater.credentials._ACTIVE_READ_RETRY_DELAY", 0)
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    return s._store


def _acct() -> str:
    return macos_keychain.keychain_account_name()


def _cred_file(temp_home: Path) -> Path:
    return temp_home / ".claude" / ".credentials.json"


class TestActiveLoginBehindALockedKeychain:
    def test_refuses_when_the_old_item_cannot_be_cleared(
        self, store, temp_home, block_real_keychain, monkeypatch
    ):
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] = OLD
        _cred_file(temp_home).write_text(OLD)
        for fn in ("set_password", "get_password", "delete_password"):
            monkeypatch.setattr(macos_keychain, fn, _locked)

        with pytest.raises(KeychainLockedError, match="Keychain locked: unlock it to switch"):
            store._write_oauth_credentials(NEW)

        assert _cred_file(temp_home).read_text() == OLD, "nothing may change"
        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] == OLD
        assert store._residual_verdict is None, "no pin: nothing was written"
        assert store._file_mode_is_ours is False
        assert store._keychain_disabled_until > 0, "the Keychain is re-probed later"

    def test_refusal_creates_no_file_where_there_was_none(
        self, store, temp_home, monkeypatch
    ):
        for fn in ("set_password", "get_password", "delete_password"):
            monkeypatch.setattr(macos_keychain, fn, _locked)
        with pytest.raises(KeychainLockedError):
            store._write_oauth_credentials(NEW)
        assert not _cred_file(temp_home).exists()

    def test_is_a_credential_write_error(self):
        assert issubclass(KeychainLockedError, CredentialWriteError)

    def test_falls_back_when_the_old_item_was_cleared(
        self, store, temp_home, block_real_keychain, monkeypatch
    ):
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] = OLD
        monkeypatch.setattr(macos_keychain, "set_password", _locked)

        store._write_oauth_credentials(NEW)

        assert _cred_file(temp_home).read_text() == NEW
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()) not in block_real_keychain.data
        assert store._last_active_credentials_backend == "file"
        assert store._residual_verdict is True

    def test_falls_back_when_no_old_item_existed(self, store, temp_home, monkeypatch):
        monkeypatch.setattr(macos_keychain, "set_password", _locked)
        store._write_oauth_credentials(NEW)
        assert _cred_file(temp_home).read_text() == NEW

    def test_falls_back_when_a_clear_was_verified_earlier_this_process(
        self, store, temp_home, monkeypatch
    ):
        store._pin_file_mode(residual_cleared=True)
        monkeypatch.setattr(macos_keychain, "delete_password", _locked)

        store._write_oauth_credentials(NEW)

        assert _cred_file(temp_home).read_text() == NEW
        assert store._residual_verdict is True

    def test_an_unverified_earlier_clear_does_not_license_it(
        self, store, temp_home, monkeypatch
    ):
        store._pin_file_mode(residual_cleared=False)
        monkeypatch.setattr(macos_keychain, "delete_password", _locked)
        with pytest.raises(KeychainLockedError):
            store._write_oauth_credentials(NEW)
        assert not _cred_file(temp_home).exists()

    def test_the_same_logins_refresh_keeps_the_file_fallback(
        self, store, temp_home, monkeypatch
    ):
        """A refreshed token for the account already live: the residual is its
        own older generation, and the file is the only place the successor
        can reach while the Keychain is locked."""
        for fn in ("set_password", "get_password", "delete_password"):
            monkeypatch.setattr(macos_keychain, fn, _locked)
        store._write_credentials(NEW, same_login=True)
        assert _cred_file(temp_home).read_text() == NEW
        assert store._residual_verdict is False

    def test_off_macos_nothing_changes(self, temp_home):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._store._write_oauth_credentials(NEW)
        assert _cred_file(temp_home).read_text() == NEW


class TestManagedKeyBehindALockedKeychain:
    def test_refuses_when_the_old_key_cannot_be_cleared(
        self, store, temp_home, block_real_keychain, monkeypatch
    ):
        block_real_keychain.data[(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct())] = KEY
        (temp_home / ".claude.json").write_text(json.dumps({"primaryApiKey": KEY}))
        real_delete = block_real_keychain.delete_password

        def delete(service, account):
            if service == CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE:
                raise macos_keychain.KeychainError("locked")
            real_delete(service, account)

        monkeypatch.setattr(macos_keychain, "delete_password", delete)

        with pytest.raises(KeychainLockedError, match="unlock it to switch"):
            store._write_credentials(NEW)

        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()) not in block_real_keychain.data, (
            "the login must not be written while the old key still outranks it"
        )
        assert not _cred_file(temp_home).exists()
        assert json.loads((temp_home / ".claude.json").read_text()) == {"primaryApiKey": KEY}
        assert block_real_keychain.data[(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct())] == KEY

    def test_clears_the_old_key_when_it_can(
        self, store, temp_home, block_real_keychain
    ):
        block_real_keychain.data[(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct())] = KEY
        (temp_home / ".claude.json").write_text(json.dumps({"primaryApiKey": KEY}))

        store._write_credentials(NEW)

        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] == NEW
        assert (CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct()) not in block_real_keychain.data
        assert "primaryApiKey" not in json.loads((temp_home / ".claude.json").read_text())

    def test_a_clear_verified_earlier_this_process_licenses_it(
        self, store, block_real_keychain, monkeypatch
    ):
        store._write_credentials(OLD)  # verifies the managed item is gone
        monkeypatch.setattr(macos_keychain, "delete_password", _locked)
        monkeypatch.setattr(macos_keychain, "set_password", block_real_keychain.set_password)
        store._write_credentials(NEW)
        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] == NEW

    def test_a_custom_profile_never_touches_the_default_profiles_key(
        self, store, temp_home, block_real_keychain, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / "work"))
        block_real_keychain.data[(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct())] = KEY
        store._write_credentials(NEW)
        assert block_real_keychain.data[(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, _acct())] == KEY


class TestTheSwitchIsRefused:
    def test_switch_to_keeps_the_old_account(self, temp_home, block_real_keychain, monkeypatch):
        monkeypatch.setattr("claude_rotater.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        live = _cred_file(temp_home).read_text()
        config = (temp_home / ".claude.json").read_text()
        h.switcher.platform = Platform.MACOS
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] = live
        for fn in ("set_password", "get_password", "delete_password"):
            monkeypatch.setattr(macos_keychain, fn, _locked)

        with pytest.raises(KeychainLockedError, match="Keychain locked"):
            h.switcher.switch_to("2", json_output=True)

        assert h.active_number() == 1
        assert _cred_file(temp_home).read_text() == live
        assert (temp_home / ".claude.json").read_text() == config
        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] == live
