"""The one-time, copy-only import of upstream claude-swap data."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from claude_rotater import legacy_import
from claude_rotater.exceptions import MigrationError
from claude_rotater.legacy_import import (
    IMPORT_RECORD,
    LEGACY_KEYCHAIN_SERVICE,
    import_legacy_data,
)
from claude_rotater.models import Platform
from claude_rotater.paths import backup_keychain_service


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.MACOS))
    return Platform.MACOS


def _seed_legacy(root: Path) -> None:
    """A realistic upstream store: data, plus logs, locks and sessions."""
    (root / "configs").mkdir(parents=True)
    (root / "credentials").mkdir()
    (root / "cache").mkdir()
    (root / "sessions" / "1-a").mkdir(parents=True)
    sequence = {
        "activeAccountNumber": 1,
        "sequence": [1, 2],
        "accounts": {
            "1": {"email": "a@example.com"},
            "2": {"email": "b@example.com"},
        },
    }
    (root / "sequence.json").write_text(json.dumps(sequence))
    (root / "settings.json").write_text('{"version": 1}')
    (root / "autoswitch_state.json").write_text("{}")
    (root / ".migrations.json").write_text('{"version": 1, "applied": {}}')
    (root / "configs" / ".claude-config-1-a@example.com.json").write_text("{}")
    (root / "credentials" / ".creds-2-b@example.com.enc").write_text("ZmlsZQ==")
    (root / "cache" / "usage.json").write_text('{"1": 42}')
    (root / "cache" / ".usage.lock").write_text("")
    (root / ".lock").write_text("")
    (root / "claude-swap.log").write_text("old log")
    (root / "sessions" / "1-a" / ".credentials.json").write_text("{}")


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Every entry under ``root`` with its mode and content hash."""
    snap: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        snap[str(path.relative_to(root))] = (st.st_mode, digest)
    return snap


def _seed_keychain(store) -> None:
    store.data[(LEGACY_KEYCHAIN_SERVICE, "account-1-a@example.com")] = "tok-a"
    store.data[(LEGACY_KEYCHAIN_SERVICE, "account-1-a@example.com.prev")] = "tok-a0"
    store.data[(LEGACY_KEYCHAIN_SERVICE, "account-2-b@example.com")] = "tok-b"


class TestDefaultDataDir:
    def test_copies_data_and_keychain_and_leaves_the_source_untouched(
        self, temp_home, macos, block_real_keychain
    ):
        legacy = temp_home / ".claude-swap-backup"
        _seed_legacy(legacy)
        _seed_keychain(block_real_keychain)
        before = _snapshot(legacy)
        keychain_before = dict(block_real_keychain.data)
        target = temp_home / ".rota"

        result = import_legacy_data(target, macos)

        assert result is not None
        assert result.source == legacy and result.target == target
        assert result.keychain_items == 3
        # Data that is copied.
        for rel in (
            "sequence.json",
            "settings.json",
            "autoswitch_state.json",
            ".migrations.json",
            "configs/.claude-config-1-a@example.com.json",
            "credentials/.creds-2-b@example.com.enc",
            "cache/usage.json",
        ):
            assert (target / rel).read_bytes() == (legacy / rel).read_bytes(), rel
        # Not copied: locks, logs, path-keyed session profiles.
        for rel in (".lock", "cache/.usage.lock", "claude-swap.log", "sessions"):
            assert not (target / rel).exists(), rel
        # Re-keyed, never moved: the originals are all still there.
        for (service, user), secret in keychain_before.items():
            assert block_real_keychain.data[(service, user)] == secret
            assert block_real_keychain.data[("rota", user)] == secret
        assert _snapshot(legacy) == before
        # Logged in the data dir, and no staging dir left behind.
        record = json.loads((target / IMPORT_RECORD).read_text())
        assert record["source"] == str(legacy) and record["keychainItems"] == 3
        assert [p.name for p in temp_home.iterdir() if ".import-" in p.name] == []

    def test_second_run_is_a_no_op(self, temp_home, macos, block_real_keychain):
        legacy = temp_home / ".claude-swap-backup"
        _seed_legacy(legacy)
        target = temp_home / ".rota"
        assert import_legacy_data(target, macos) is not None
        (target / "sequence.json").write_text('{"changed": "by rota"}')

        assert import_legacy_data(target, macos) is None
        assert (target / "sequence.json").read_text() == '{"changed": "by rota"}'

    def test_an_existing_data_dir_is_never_imported_into(
        self, temp_home, macos, block_real_keychain
    ):
        _seed_legacy(temp_home / ".claude-swap-backup")
        target = temp_home / ".rota"
        target.mkdir()

        assert import_legacy_data(target, macos) is None
        assert list(target.iterdir()) == []

    def test_no_legacy_dir_creates_nothing(self, temp_home, macos):
        target = temp_home / ".rota"
        assert import_legacy_data(target, macos) is None
        assert not target.exists()

    def test_linux_prefers_upstreams_xdg_dir_and_skips_the_keychain(
        self, temp_home, monkeypatch, block_real_keychain
    ):
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
        xdg_legacy = temp_home / ".local" / "share" / "claude-swap"
        _seed_legacy(xdg_legacy)
        _seed_legacy(temp_home / ".claude-swap-backup")
        (temp_home / ".claude-swap-backup" / "sequence.json").write_text("{}")
        _seed_keychain(block_real_keychain)

        result = import_legacy_data(temp_home / ".rota", Platform.LINUX)

        assert result is not None and result.source == xdg_legacy
        assert result.keychain_items == 0
        assert not any(service == "rota" for service, _ in block_real_keychain.data)


class TestProfiles:
    def test_a_rota_home_profile_never_imports_implicitly(
        self, temp_home, tmp_path, macos, monkeypatch
    ):
        _seed_legacy(temp_home / ".claude-swap-backup")
        profile = tmp_path / "work"
        monkeypatch.setenv("ROTA_HOME", str(profile))

        assert import_legacy_data(profile, macos) is None
        assert not profile.exists()

    def test_rota_import_from_feeds_a_profile_its_own_keychain_service(
        self, temp_home, tmp_path, macos, monkeypatch, block_real_keychain
    ):
        source = tmp_path / "old-work-store"
        _seed_legacy(source)
        _seed_keychain(block_real_keychain)
        profile = tmp_path / "work"
        monkeypatch.setenv("ROTA_HOME", str(profile))
        monkeypatch.setenv("ROTA_IMPORT_FROM", str(source))

        result = import_legacy_data(profile, macos)

        assert result is not None and result.source == source
        service = backup_keychain_service(profile)
        assert service.startswith("rota-")
        assert block_real_keychain.data[(service, "account-2-b@example.com")] == "tok-b"
        assert not any(s == "rota" for s, _ in block_real_keychain.data)
        assert (profile / "sequence.json").exists()

    def test_rota_import_from_expands_tilde(
        self, temp_home, macos, monkeypatch
    ):
        _seed_legacy(temp_home / "elsewhere")
        monkeypatch.setenv("ROTA_IMPORT_FROM", "~/elsewhere")
        result = import_legacy_data(temp_home / ".rota", macos)
        assert result is not None and result.source == temp_home / "elsewhere"

    def test_a_missing_rota_import_from_is_an_error_not_an_empty_start(
        self, temp_home, tmp_path, macos, monkeypatch
    ):
        monkeypatch.setenv("ROTA_IMPORT_FROM", str(tmp_path / "typo"))
        target = temp_home / ".rota"
        with pytest.raises(MigrationError, match="ROTA_IMPORT_FROM"):
            import_legacy_data(target, macos)
        assert not target.exists()


class TestKeychainServiceOverride:
    def _import(self, temp_home, macos):
        legacy = temp_home / ".claude-swap-backup"
        _seed_legacy(legacy)
        return import_legacy_data(temp_home / ".rota", macos)

    def test_copies_from_the_named_service(
        self, temp_home, macos, monkeypatch, block_real_keychain
    ):
        block_real_keychain.data[("swap-work", "account-2-b@example.com")] = "tok-work"
        _seed_keychain(block_real_keychain)  # the default service: must be ignored
        monkeypatch.setenv("ROTA_IMPORT_KEYCHAIN_SERVICE", "swap-work")

        result = self._import(temp_home, macos)

        assert result is not None and result.keychain_items == 1
        assert block_real_keychain.data[("rota", "account-2-b@example.com")] == "tok-work"
        assert ("rota", "account-1-a@example.com") not in block_real_keychain.data
        assert block_real_keychain.data[("swap-work", "account-2-b@example.com")] == "tok-work"
        record = json.loads((temp_home / ".rota" / IMPORT_RECORD).read_text())
        assert record["keychainService"] == "swap-work"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_unset_or_blank_means_claude_swap(
        self, temp_home, macos, monkeypatch, block_real_keychain, value
    ):
        if value is not None:
            monkeypatch.setenv("ROTA_IMPORT_KEYCHAIN_SERVICE", value)
        _seed_keychain(block_real_keychain)

        result = self._import(temp_home, macos)

        assert result is not None and result.keychain_items == 3
        record = json.loads((temp_home / ".rota" / IMPORT_RECORD).read_text())
        assert record["keychainService"] == LEGACY_KEYCHAIN_SERVICE

    def test_a_locked_keychain_names_the_overridden_service(
        self, temp_home, macos, monkeypatch, block_real_keychain
    ):
        monkeypatch.setenv("ROTA_IMPORT_KEYCHAIN_SERVICE", "swap-work")

        def locked(service, account):
            raise subprocess.TimeoutExpired("security", 5)

        monkeypatch.setattr("claude_rotater.macos_keychain.get_password", locked)
        with pytest.raises(MigrationError, match="'swap-work'"):
            self._import(temp_home, macos)
        assert not (temp_home / ".rota").exists()


class TestFailureLeavesNothingBehind:
    def test_a_locked_keychain_aborts_and_the_next_run_retries(
        self, temp_home, macos, monkeypatch, block_real_keychain
    ):
        legacy = temp_home / ".claude-swap-backup"
        _seed_legacy(legacy)
        _seed_keychain(block_real_keychain)
        before = _snapshot(legacy)
        target = temp_home / ".rota"

        def locked(service, account):
            raise subprocess.TimeoutExpired("security", 5)

        monkeypatch.setattr(legacy_import.macos_keychain, "get_password", locked)
        with pytest.raises(MigrationError, match="Unlock the Keychain"):
            import_legacy_data(target, macos)
        assert not target.exists()
        assert [p.name for p in temp_home.iterdir() if ".import-" in p.name] == []
        assert _snapshot(legacy) == before

        monkeypatch.setattr(
            legacy_import.macos_keychain, "get_password", block_real_keychain.get_password
        )
        result = import_legacy_data(target, macos)
        assert result is not None and result.keychain_items == 3

    def test_losing_a_race_to_a_concurrent_import_returns_none(
        self, temp_home, macos, monkeypatch
    ):
        _seed_legacy(temp_home / ".claude-swap-backup")
        target = temp_home / ".rota"
        real_rename = os.rename

        def racing_rename(src, dst):
            Path(dst).mkdir()
            (Path(dst) / "sequence.json").write_text("{}")  # the winner's data
            real_rename(src, dst)

        monkeypatch.setattr(legacy_import.os, "rename", racing_rename)
        assert import_legacy_data(target, macos) is None
        assert (target / "sequence.json").read_text() == "{}"
        assert [p.name for p in temp_home.iterdir() if ".import-" in p.name] == []


class TestSwitcherRunsTheImport:
    def test_first_switcher_imports_and_says_so(
        self, temp_home, macos, capsys, block_real_keychain
    ):
        from claude_rotater.switcher import ClaudeAccountSwitcher

        legacy = temp_home / ".claude-swap-backup"
        _seed_legacy(legacy)
        _seed_keychain(block_real_keychain)

        switcher = ClaudeAccountSwitcher()

        assert switcher.backup_dir == temp_home / ".rota"
        accounts = switcher._get_sequence_data()["accounts"]
        assert accounts["2"]["email"] == "b@example.com"
        err = capsys.readouterr().err
        assert "imported accounts and settings" in err and str(legacy) in err

        ClaudeAccountSwitcher()  # second construction: silent no-op
        assert "imported" not in capsys.readouterr().err


class TestImportedAt:
    def test_reads_the_import_record(self, tmp_path):
        import json as _json

        from claude_rotater import legacy_import

        (tmp_path / legacy_import.IMPORT_RECORD).write_text(
            _json.dumps({"importedAt": "2026-10-16T22:54:06Z"})
        )
        assert legacy_import.imported_at(tmp_path) == 1792191246.0

    def test_missing_or_bad_record_is_none(self, tmp_path):
        from claude_rotater import legacy_import

        assert legacy_import.imported_at(tmp_path) is None
        (tmp_path / legacy_import.IMPORT_RECORD).write_text("{nope")
        assert legacy_import.imported_at(tmp_path) is None
        (tmp_path / legacy_import.IMPORT_RECORD).write_text('{"importedAt": 5}')
        assert legacy_import.imported_at(tmp_path) is None
