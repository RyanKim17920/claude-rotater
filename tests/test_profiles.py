"""profiles: rota picks a registered profile by the current directory, so a
shell without the profile's wrapper still opens the right data dir."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from claude_rotater import paths, profiles


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """A registry in an isolated default data dir, one 'work' profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "get_default_backup_root", lambda: home / ".rota")
    root = tmp_path / "job"
    (root / "sub" / "deeper").mkdir(parents=True)
    data = root / ".rota"
    data.mkdir()
    config = "$HOME-style/.claude-work"  # kept verbatim, never normalized
    profiles.add("work", str(data), config, [str(root)])
    profiles.active_name.cache_clear()
    yield root, data, config
    profiles.active_name.cache_clear()


def env(**values) -> dict:
    return dict(values)


class TestMatch:
    def test_root_and_subdirectories_match(self, reg):
        root, data, config = reg
        for here in (root, root / "sub", root / "sub" / "deeper"):
            e = env()
            assert profiles.apply(e, cwd=str(here)).name == "work"
            assert e == {"ROTA_HOME": str(data), "CLAUDE_CONFIG_DIR": config}

    def test_outside_the_root_keeps_the_default(self, reg, tmp_path):
        e = env()
        assert profiles.apply(e, cwd=str(tmp_path)) is None and e == {}

    def test_a_sibling_with_the_same_prefix_does_not_match(self, reg, tmp_path):
        (tmp_path / "job2").mkdir()
        assert profiles.resolve(env(), cwd=str(tmp_path / "job2")) is None

    def test_symlinked_cwd_matches_its_target(self, reg, tmp_path):
        root, _data, _config = reg
        link = tmp_path / "link"
        link.symlink_to(root / "sub")
        assert profiles.resolve(env(), cwd=str(link)).name == "work"

    def test_symlinked_root_matches_the_real_dir(self, reg, tmp_path):
        root, data, config = reg
        link = tmp_path / "jlink"
        link.symlink_to(root)
        profiles.add("work", str(data), config, [str(link)])
        assert profiles.resolve(env(), cwd=str(root / "sub")).name == "work"

    def test_deepest_root_wins(self, reg, tmp_path):
        root, _data, _config = reg
        inner = root / "sub"
        (tmp_path / "d2").mkdir()
        profiles.add("inner", str(tmp_path / "d2"), "/c2", [str(inner)])
        assert profiles.resolve(env(), cwd=str(inner / "deeper")).name == "inner"
        assert profiles.resolve(env(), cwd=str(root)).name == "work"


class TestEnvironmentWins:
    def test_rota_home_set_picks_nothing(self, reg):
        root, _data, _config = reg
        e = env(ROTA_HOME="/explicit")
        assert profiles.apply(e, cwd=str(root)) is None
        assert e == {"ROTA_HOME": "/explicit"}

    def test_another_config_dir_picks_nothing(self, reg):
        root, _data, _config = reg
        e = env(CLAUDE_CONFIG_DIR="/somewhere/else")
        assert profiles.apply(e, cwd=str(root)) is None
        assert e == {"CLAUDE_CONFIG_DIR": "/somewhere/else"}

    def test_the_same_config_dir_keeps_its_spelling(self, reg):
        root, data, config = reg
        e = env(CLAUDE_CONFIG_DIR=config)
        assert profiles.apply(e, cwd=str(root)).name == "work"
        assert e == {"CLAUDE_CONFIG_DIR": config, "ROTA_HOME": str(data)}


class TestRegistry:
    def test_missing_or_broken_registry_is_empty(self, reg):
        path = profiles.registry_path()
        path.write_text("{nope")
        assert profiles.load() == []
        path.unlink()
        assert profiles.load() == []

    def test_bad_entries_are_skipped(self, reg):
        path = profiles.registry_path()
        good = json.loads(path.read_text())["profiles"][0]
        path.write_text(json.dumps({"profiles": [good, {"name": 3}, "x", {**good, "roots": "no"}]}))
        assert [p.name for p in profiles.load()] == ["work"]

    def test_add_replaces_by_name_and_remove(self, reg, tmp_path):
        root, data, _config = reg
        profiles.add("work", str(data), "/new", [str(root)])
        assert [p.claude_config_dir for p in profiles.load()] == ["/new"]
        assert profiles.remove("work") and profiles.load() == []
        assert not profiles.remove("work")

    def test_default_data_dir_is_not_a_profile(self, reg, tmp_path):
        with pytest.raises(profiles.ProfileError):
            profiles.add("x", str(paths.get_default_backup_root()), "/c", [str(tmp_path)])

    def test_current_name_follows_rota_home(self, reg):
        _root, data, _config = reg
        assert profiles.current_name(env(ROTA_HOME=str(data))) == "work"
        assert profiles.current_name(env(ROTA_HOME="/other")) is None
        assert profiles.current_name(env()) is None


class TestHeader:
    def test_header_names_the_profile(self):
        from claude_rotater.tui.main import AutoStatus, status_text

        plain = status_text(AutoStatus(), now=0.0, width=120, profile="work").plain
        assert plain.startswith(" rota  · work")
        assert "·" not in status_text(AutoStatus(), now=0.0, width=120).plain.split("auto")[0]


def test_rota_status_picks_the_profile_from_a_plain_shell(tmp_path):
    """End to end: `rota profile add`, then `rota` started in a subdirectory
    of the root, with neither variable set, reads the profile's data dir."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "job"
    (root / "sub").mkdir(parents=True)
    child = {k: v for k, v in os.environ.items()
             if k not in ("ROTA_HOME", "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME")}
    child["HOME"] = str(home)
    rota = [sys.executable, "-m", "claude_rotater"]
    add = subprocess.run(
        rota + ["profile", "add", "work", "--root", str(root),
                "--data-dir", str(root / ".rota"), "--config-dir", str(home / ".claude-work")],
        capture_output=True, text=True, env=child, timeout=60,
    )
    assert add.returncode == 0, add.stderr
    probe = ("from claude_rotater import cli, paths\n"
             "import sys, os\n"
             "sys.argv = ['rota', 'profile', 'list']\n"
             "from claude_rotater import profiles\n"
             "profiles.apply()\n"
             "print(paths.get_backup_root()); print(os.environ.get('CLAUDE_CONFIG_DIR'))\n")
    out = subprocess.run([sys.executable, "-c", probe], cwd=root / "sub",
                         capture_output=True, text=True, env=child, timeout=60)
    assert out.stdout.split() == [str(root / ".rota"), str(home / ".claude-work")], out.stderr
    listing = subprocess.run(rota + ["profile", "list"], capture_output=True, text=True,
                             env=child, timeout=60)
    assert listing.stdout.startswith("work: data ")
