"""Tests for claude_rotater.paths resolver helpers.

These tests verify that rota resolves Claude Code config/credential paths the
same way claude-code itself does. If these drift from claude-code's behavior,
rota will read the wrong files and misattribute accounts (see issue #16).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_rotater.models import Platform
from claude_rotater.paths import (
    backup_keychain_service,
    get_backup_root,
    get_claude_config_home,
    get_credentials_path,
    get_default_global_config_path,
    get_global_config_path,
    get_legacy_backup_roots,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp HOME with CLAUDE_CONFIG_DIR unset."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    with patch("pathlib.Path.home", return_value=home):
        yield home


class TestGetClaudeConfigHome:
    def test_default_is_dot_claude_in_home(self, isolated_home: Path):
        assert get_claude_config_home() == isolated_home / ".claude"

    def test_respects_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "custom-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert get_claude_config_home() == custom


class TestGetGlobalConfigPath:
    def test_default_returns_homedir_claude_json(self, isolated_home: Path):
        """Without CCD, claude-code writes .claude.json at $HOME, not inside .claude/."""
        assert get_global_config_path() == isolated_home / ".claude.json"

    def test_ccd_set_returns_ccd_claude_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "ccd"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert get_global_config_path() == custom / ".claude.json"

    def test_legacy_config_json_takes_precedence(self, isolated_home: Path):
        """If ~/.claude/.config.json exists, claude-code uses that (legacy)."""
        config_home = isolated_home / ".claude"
        config_home.mkdir(exist_ok=True)
        legacy = config_home / ".config.json"
        legacy.write_text("{}")
        assert get_global_config_path() == legacy

    def test_legacy_config_json_in_ccd_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "ccd"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        legacy = custom / ".config.json"
        legacy.write_text("{}")
        assert get_global_config_path() == legacy


@pytest.mark.parametrize(
    "resolver", [get_global_config_path, get_default_global_config_path]
)
@pytest.mark.parametrize(
    "legacy_kind, global_present, expect_legacy",
    [
        ("absent", False, False),
        ("empty", True, False),
        ("empty", False, True),
        ("empty_readonly", False, False),
        ("nonempty", True, True),
        ("directory", False, False),
    ],
)
def test_legacy_config_rule_matches_claude_code_2_1_299(
    isolated_home: Path, resolver, legacy_kind, global_present, expect_legacy
):
    """An empty .config.json only wins while it is writable and .claude.json is absent."""
    legacy = isolated_home / ".claude" / ".config.json"
    legacy.parent.mkdir()
    if legacy_kind == "directory":
        legacy.mkdir()
    elif legacy_kind != "absent":
        legacy.write_text("{}" if legacy_kind == "nonempty" else "")
        if legacy_kind == "empty_readonly":
            legacy.chmod(0o444)
    global_cfg = isolated_home / ".claude.json"
    if global_present:
        global_cfg.write_text("{}")
    assert resolver() == (legacy if expect_legacy else global_cfg)


class TestGetCredentialsPath:
    def test_default_inside_dot_claude(self, isolated_home: Path):
        assert get_credentials_path() == isolated_home / ".claude" / ".credentials.json"

    def test_respects_ccd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        custom = tmp_path / "ccd"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert get_credentials_path() == custom / ".credentials.json"


class TestGetBackupRoot:
    """``~/.rota`` on every platform, overridable with ``ROTA_HOME``."""

    @pytest.mark.parametrize(
        "platform", [Platform.LINUX, Platform.WSL, Platform.MACOS, Platform.WINDOWS]
    )
    def test_default_is_dot_rota_on_every_platform(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, platform
    ):
        monkeypatch.delenv("ROTA_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(isolated_home / "xdg"))
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: platform))
        assert get_backup_root() == isolated_home / ".rota"

    def test_rota_home_overrides(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ROTA_HOME", str(tmp_path / "work"))
        assert get_backup_root() == tmp_path / "work"

    def test_empty_rota_home_is_ignored(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ROTA_HOME", "")
        assert get_backup_root() == isolated_home / ".rota"

    def test_rota_home_expands_tilde(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ROTA_HOME", "~/profiles/work")
        assert get_backup_root() == isolated_home / "profiles" / "work"

    def test_relative_rota_home_is_made_absolute(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ROTA_HOME", "rel/profile")
        root = get_backup_root()
        assert root.is_absolute()
        assert root == Path(os.path.abspath(tmp_path / "rel" / "profile"))


class TestBackupKeychainService:
    """Two data dirs never share a Keychain service."""

    def test_default_dir_uses_the_fixed_base_name(self, isolated_home: Path):
        assert backup_keychain_service(isolated_home / ".rota") == "rota"

    def test_default_dir_spelled_differently_is_still_the_base_name(
        self, isolated_home: Path
    ):
        assert backup_keychain_service(isolated_home / "x" / ".." / ".rota") == "rota"

    def test_other_dir_gets_a_short_stable_suffix(
        self, isolated_home: Path, tmp_path: Path
    ):
        work = tmp_path / "work"
        service = backup_keychain_service(work)
        assert service.startswith("rota-")
        suffix = service.removeprefix("rota-")
        assert len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix)
        assert backup_keychain_service(work) == service  # stable across calls
        assert backup_keychain_service(Path(str(work) + "/")) == service

    def test_distinct_dirs_get_distinct_services(
        self, isolated_home: Path, tmp_path: Path
    ):
        services = {
            backup_keychain_service(tmp_path / name)
            for name in ("a", "b", "c", "work", "personal")
        }
        assert len(services) == 5
        assert "rota" not in services

    def test_never_the_upstream_service(self, isolated_home: Path, tmp_path: Path):
        for root in (isolated_home / ".rota", tmp_path / "p"):
            assert backup_keychain_service(root) != "claude-swap"


class TestGetLegacyBackupRoots:
    """Where upstream claude-swap kept its data (read only by the import)."""

    def test_linux_lists_xdg_then_dot_dir(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
        assert get_legacy_backup_roots() == [
            isolated_home / ".local" / "share" / "claude-swap",
            isolated_home / ".claude-swap-backup",
        ]

    def test_linux_respects_absolute_xdg_data_home(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.WSL))
        assert get_legacy_backup_roots()[0] == tmp_path / "xdg" / "claude-swap"

    def test_linux_ignores_relative_xdg_data_home(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("XDG_DATA_HOME", "relative/data")
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
        assert get_legacy_backup_roots()[0] == (
            isolated_home / ".local" / "share" / "claude-swap"
        )

    @pytest.mark.parametrize("platform", [Platform.MACOS, Platform.WINDOWS])
    def test_other_platforms_list_only_the_dot_dir(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, platform
    ):
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: platform))
        assert get_legacy_backup_roots() == [isolated_home / ".claude-swap-backup"]
