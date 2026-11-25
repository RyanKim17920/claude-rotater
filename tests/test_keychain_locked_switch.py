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

    def test_a_refresh_of_the_live_login_is_refused_too(
        self, store, temp_home, block_real_keychain, monkeypatch
    ):
        """A refreshed token for the account already live gets no file
        fallback either: once the Keychain unlocks the CLI would read the
        older generation, whose refresh token was spent. The refresh path
        checks first, so it never spends one it cannot store."""
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] = OLD
        for fn in ("set_password", "get_password", "delete_password"):
            monkeypatch.setattr(macos_keychain, fn, _locked)
        assert store._active_write_blocked() is False, "nothing failed yet"

        with pytest.raises(KeychainLockedError):
            store._write_oauth_credentials(NEW)

        assert not _cred_file(temp_home).exists()
        assert block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] == OLD
        assert store._active_write_blocked() is True, "the next refresh is skipped"

    def test_a_verified_clear_does_not_block_a_refresh(self, store):
        store._pin_file_mode(residual_cleared=True)
        assert store._active_write_blocked() is False

    def test_the_block_lifts_when_the_cooldown_lapses(self, store, monkeypatch):
        monkeypatch.setattr(macos_keychain, "get_password", _locked)
        with pytest.raises(macos_keychain.KeychainError):
            store._kc_call(macos_keychain.get_password, "x", "y")
        assert store._active_write_blocked() is True
        store._keychain_disabled_until = 1.0  # a deadline already in the past
        assert store._active_write_blocked() is False

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


# -- refreshing the live login behind a locked Keychain ----------------------
#
# A refresh token rotates: once rota POSTs it, the old one is dead. So a
# refresh whose write-back a locked Keychain would refuse is never started,
# and one the Keychain locks under mid-flight keeps the new login in rota's
# own backup for the account (never the plaintext file the CLI reads while a
# stale Keychain item shadows it).

EMAIL = "a@example.com"
EXPIRED = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-old", "refreshToken": "rt-old", "expiresAt": 1000,
}})
REFRESHED = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-new", "refreshToken": "rt-new", "expiresAt": 9999999999000,
}})


class _Lockable:
    """The in-memory fake Keychain behind a lock the test can flip."""

    def __init__(self, fake, monkeypatch):
        self.fake = fake
        self.locked = False
        for fn in ("set_password", "get_password", "delete_password"):
            monkeypatch.setattr(macos_keychain, fn, self._gate(getattr(fake, fn)))

    def _gate(self, real):
        def call(*a, **kw):
            if self.locked:
                raise macos_keychain.KeychainError("locked")
            return real(*a, **kw)

        return call


@pytest.fixture
def live(temp_home: Path, block_real_keychain, monkeypatch):
    """Account 1 is live on macOS: its login in the CLI's Keychain item and
    in rota's backup, access token expired, refresh token unspent."""
    from claude_rotater import oauth

    monkeypatch.setenv("ROTA_HOME", str(temp_home / ".rota"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("claude_rotater.credentials._ACTIVE_READ_RETRY_DELAY", 0)
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    data = s._get_sequence_data()
    data["accounts"]["1"] = {
        "email": EMAIL, "uuid": "uuid-1", "organizationUuid": "",
        "organizationName": "", "added": "2024-01-01T00:00:00Z",
    }
    data["sequence"] = [1]
    data["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, data)
    (temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": EMAIL, "accountUuid": "uuid-1"},
    }))
    block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] = EXPIRED
    s._write_account_credentials("1", EMAIL, EXPIRED)
    kc = _Lockable(block_real_keychain, monkeypatch)

    posts: list[str] = []

    def refresh(credentials, **_kw):
        posts.append(credentials)
        return oauth.RefreshOutcome(REFRESHED, None)

    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
    monkeypatch.setattr(
        oauth, "try_fetch_usage_for_account",
        lambda *_a, **_kw: oauth.UsageOutcome({"five_hour": {"pct": 1}}),
    )
    return s, kc, posts


def _live_item(kc) -> str | None:
    return kc.fake.data.get((CLAUDE_CODE_KEYCHAIN_SERVICE, _acct()))


def _collect(s) -> object:
    """One collect pass for the live account, as the collector runs it."""
    creds = s._read_credentials()
    return s._fetch_active_usage("1", EMAIL, creds or EXPIRED)


def _cooldown_lapses(s) -> None:
    s._store._keychain_disabled_until = 1.0  # a deadline already in the past


class TestRefreshBehindALockedKeychain:
    def test_a_locked_keychain_skips_the_refresh_and_spends_nothing(
        self, live, temp_home
    ):
        from claude_rotater.json_output import USAGE_KEYCHAIN_UNAVAILABLE

        s, kc, posts = live
        kc.locked = True

        record = _collect(s)

        assert posts == [], "the refresh token must not be spent"
        assert record.sentinel == USAGE_KEYCHAIN_UNAVAILABLE
        assert not _cred_file(temp_home).exists(), "no plaintext the CLI would read"
        kc.locked = False
        assert _live_item(kc) == EXPIRED
        assert s._read_account_credentials("1", EMAIL) == EXPIRED

    def test_skips_when_the_keychain_locks_after_the_collector_read(
        self, live, temp_home
    ):
        """The pass read the login cleanly; the Keychain locks before the
        refresh. The check under the write lock sees it and spends nothing."""
        from claude_rotater.json_output import USAGE_KEYCHAIN_UNAVAILABLE

        s, kc, posts = live
        creds = s._read_credentials()
        assert creds == EXPIRED and not s._active_read_degraded, "premise"
        kc.locked = True

        record = s._fetch_active_usage("1", EMAIL, creds)

        assert posts == []
        assert record.sentinel == USAGE_KEYCHAIN_UNAVAILABLE
        assert _live_item(kc) == EXPIRED
        assert not _cred_file(temp_home).exists()

    def test_known_unusable_keychain_skips_even_when_the_read_came_from_the_file(
        self, live, temp_home
    ):
        """A recent Keychain failure (cooldown pending) and nothing verified
        cleared: the write-back would be refused, so nothing is spent."""
        from claude_rotater.json_output import USAGE_KEYCHAIN_UNAVAILABLE

        s, kc, posts = live
        kc.locked = True
        with pytest.raises(macos_keychain.KeychainError):
            s._store._kc_call(macos_keychain.get_password, "x", "y")
        assert s._store._use_keychain() is False, "premise: known unusable"

        record = s._fetch_active_usage("1", EMAIL, EXPIRED)

        assert posts == []
        assert record.sentinel == USAGE_KEYCHAIN_UNAVAILABLE

    def test_the_skip_is_logged_once(self, live, caplog):
        s, kc, posts = live
        kc.locked = True
        with caplog.at_level("INFO"):
            for _ in range(3):
                _collect(s)
        lines = [r for r in caplog.records if "Keychain locked" in r.getMessage()]
        assert len(lines) == 1, [r.getMessage() for r in caplog.records]

    def test_the_refresh_resumes_after_an_unlock(self, live, temp_home):
        s, kc, posts = live
        kc.locked = True
        _collect(s)
        assert posts == []

        kc.locked = False
        _cooldown_lapses(s)
        record = _collect(s)

        assert posts == [EXPIRED], "one refresh, once the Keychain answers"
        assert record.usage == {"five_hour": {"pct": 1}}
        assert _live_item(kc) == REFRESHED
        assert s._read_account_credentials("1", EMAIL) == REFRESHED
        assert not _cred_file(temp_home).exists()

    def test_a_still_valid_token_keeps_polling_while_locked(self, live, temp_home):
        s, kc, posts = live
        valid = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-old", "refreshToken": "rt-old",
            "expiresAt": 9999999999000,
        }})
        kc.fake.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, _acct())] = valid
        creds = s._read_credentials()
        kc.locked = True

        record = s._fetch_active_usage("1", EMAIL, creds)

        assert record.usage == {"five_hour": {"pct": 1}}
        assert posts == []


class TestKeychainLocksMidRefresh:
    @pytest.fixture
    def locks_during_post(self, live, monkeypatch):
        from claude_rotater import oauth

        s, kc, posts = live

        def refresh(credentials, **_kw):
            posts.append(credentials)
            kc.locked = True  # the screen locks while the POST is in flight
            return oauth.RefreshOutcome(REFRESHED, None)

        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
        return live

    def test_the_new_login_is_kept_in_rotas_backup(
        self, locks_during_post, temp_home, caplog
    ):
        from claude_rotater.json_output import USAGE_KEYCHAIN_UNAVAILABLE

        s, kc, posts = locks_during_post
        with caplog.at_level("INFO"):
            record = _collect(s)

        assert posts == [EXPIRED], "the token was spent before the lock"
        assert record.sentinel == USAGE_KEYCHAIN_UNAVAILABLE
        assert s._read_account_credentials("1", EMAIL) == REFRESHED, (
            "the successor survives in rota's own backup for the account"
        )
        enc = s._store._backup_enc_path("1", EMAIL)
        assert enc.exists()
        if __import__("sys").platform != "win32":
            assert enc.stat().st_mode & 0o777 == 0o600
        assert _live_item(kc) == EXPIRED, "the locked item is untouched"
        assert not _cred_file(temp_home).exists(), (
            "never plaintext where a stale Keychain item shadows it"
        )
        assert any(
            "Keychain locked" in r.getMessage() and "backup" in r.getMessage()
            for r in caplog.records
        )

    def test_unlock_restores_the_kept_login_without_another_post(
        self, locks_during_post, temp_home
    ):
        s, kc, posts = locks_during_post
        _collect(s)
        kc.locked = False
        _cooldown_lapses(s)

        record = _collect(s)

        assert posts == [EXPIRED], "the kept successor is restored, not re-spent"
        assert record.usage == {"five_hour": {"pct": 1}}
        assert _live_item(kc) == REFRESHED
        assert not _cred_file(temp_home).exists()

    def test_stays_quiet_and_spends_nothing_while_still_locked(
        self, locks_during_post
    ):
        s, kc, posts = locks_during_post
        _collect(s)
        _collect(s)
        _collect(s)
        assert posts == [EXPIRED]
        assert s._read_account_credentials("1", EMAIL) == REFRESHED

    def test_a_failed_backup_write_stashes_the_successor(
        self, locks_during_post, monkeypatch
    ):
        """Neither the Keychain nor the backup file took it: the successor
        goes to rota's 0600 stash, where the next pass for the slot adopts it."""
        s, kc, posts = locks_during_post

        def broken(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(s._store, "_write_backup_enc", broken)
        _collect(s)

        manifest, _verdict = s._store._read_stash_manifest_ex()
        rows = list(manifest.values())
        assert len(rows) == 1
        assert rows[0]["configSlot"] == "1"
        assert rows[0]["consumedFp"]
        entry_id = next(iter(manifest))
        assert s._store._read_unclaimed_credential(entry_id)[0] == REFRESHED

