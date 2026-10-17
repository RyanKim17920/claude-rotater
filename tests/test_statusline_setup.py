"""statusline_setup: wiring ``rota feed`` into any user's statusline. Every
test runs on a throwaway CLAUDE_CONFIG_DIR and data dir."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_rotater import feed, statusline_setup as ss

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrapper")


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "profile"
    config.mkdir()
    data = tmp_path / "rota-data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("ROTA_HOME", str(data))
    return config / "settings.json", data


def write_settings(path: Path, settings: dict, indent=4) -> str:
    text = json.dumps(settings, indent=indent) + "\n"
    path.write_text(text)
    return text


ORIGINAL = {
    "model": "opus",
    "statusLine": {"type": "command", "command": "~/.claude/statusline.sh", "padding": 1},
    "hooks": {"Stop": []},
}


class TestStatus:
    def test_no_settings_file(self, env):
        st = ss.status(env[1])
        assert (st.install, st.state, st.installed) == (ss.NOT_INSTALLED, "off", False)
        assert st.settings_path == env[0]

    def test_someone_elses_statusline_is_foreign(self, env):
        write_settings(env[0], ORIGINAL)
        st = ss.status(env[1])
        assert (st.install, st.state) == (ss.FOREIGN, "off")
        assert st.command == "~/.claude/statusline.sh"

    def test_hand_wired_feed_counts_as_installed(self, env):
        write_settings(env[0], {"statusLine": {"type": "command",
                                               "command": "rota feed --tee | x.sh"}})
        assert ss.status(env[1]).installed

    def test_live_and_stale_follow_the_feed_file(self, env):
        path, data = env
        ss.install(data)
        assert ss.status(data).state == "stale"  # nothing fed yet
        feed.append_record(feed.feed_path(data), {"t": 1.0, "five_hour": {"pct": 1.0}})
        mtime = os.stat(feed.feed_path(data)).st_mtime
        assert ss.status(data, now=mtime + 10).state == "live"
        assert ss.status(data, now=mtime + 10_000).state == "stale"

    def test_unreadable_settings_reads_as_nothing(self, env):
        env[0].write_text("{nope")
        assert ss.status(env[1]).install == ss.NOT_INSTALLED

    def test_respects_claude_config_dir(self, env, tmp_path, monkeypatch):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
        assert ss.settings_path() == other / "settings.json"
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert ss.settings_path() == tmp_path / ".claude" / "settings.json"


class TestInstallWrapsAnExistingStatusline:
    def test_wraps_keeps_everything_else_and_backs_up(self, env):
        path, data = env
        before = write_settings(path, ORIGINAL)
        st = ss.install(data)
        assert st.installed
        after = json.loads(path.read_text())
        assert list(after) == ["model", "statusLine", "hooks"]
        assert after["model"] == "opus" and after["hooks"] == {"Stop": []}
        assert after["statusLine"]["padding"] == 1
        assert ss.unwrap(after["statusLine"]["command"]) == "~/.claude/statusline.sh"
        assert path.read_text().startswith('{\n    "model"')  # indent kept
        backups = list((data / ss.BACKUP_DIRNAME).iterdir())
        assert [b.read_text() for b in backups] == [before]

    def test_idempotent(self, env):
        path, data = env
        write_settings(path, ORIGINAL)
        ss.install(data)
        once = path.read_text()
        ss.install(data)
        assert path.read_text() == once
        assert len(list((data / ss.BACKUP_DIRNAME).iterdir())) == 1

    def test_uninstall_restores_the_exact_original(self, env):
        path, data = env
        before = write_settings(path, ORIGINAL)
        ss.install(data)
        st = ss.uninstall(data)
        assert path.read_text() == before
        assert st.install == ss.FOREIGN
        ss.uninstall(data)  # idempotent
        assert path.read_text() == before

    @pytest.mark.parametrize("original", [
        "~/.claude/statusline.sh",
        "bash -c 'echo \"{x}\"; exit 0' # trailing comment",
        "a && b; c &",
        "printf '%s' \"; } | { \" | cat",
        "line one\nline two",
    ])
    def test_unwrap_round_trips_any_command(self, original):
        assert ss.unwrap(ss.wrap(original)) == original

    def test_unwrap_ignores_foreign_commands(self):
        assert ss.unwrap("~/.claude/statusline.sh") is None
        assert ss.unwrap(ss.default_command()) is None

    def test_wrapped_command_feeds_both_sides(self, env, tmp_path):
        """Run the wrapper through /bin/sh as Claude Code does: the original
        command gets the payload unchanged and its output is the statusline;
        the feed side gets the payload too and its output is discarded."""
        fed = tmp_path / "fed.json"
        seen = tmp_path / "seen.json"
        fake_feed = f"sh -c 'cat > {fed}; echo NOISE'"
        original = f"cat > {seen}; echo 'my line' # user comment"
        payload = json.dumps({"session_id": "s", "rate_limits": {}, "x": "a b\tc"})
        out = subprocess.run(
            ["/bin/sh", "-c", ss.wrap(original, feed=fake_feed)],
            input=payload.encode(), capture_output=True, timeout=10,
        )
        assert out.returncode == 0
        assert out.stdout == b"my line\n"
        assert seen.read_text() == payload
        for _ in range(100):  # the feed side runs in the background
            if fed.exists() and fed.read_text() == payload:
                break
            subprocess.run(["sleep", "0.05"])
        assert fed.read_text() == payload


class TestInstallDefault:
    def test_no_statusline_installs_the_default_line(self, env):
        path, data = env
        write_settings(path, {"model": "opus"}, indent=2)
        ss.install(data)
        settings = json.loads(path.read_text())
        assert settings["statusLine"]["type"] == "command"
        assert settings["statusLine"]["command"].endswith("--line # " + ss.MARKER)
        st = ss.uninstall(data)
        assert json.loads(path.read_text()) == {"model": "opus"}
        assert st.install == ss.NOT_INSTALLED

    def test_missing_settings_file_is_created(self, env):
        path, data = env
        ss.install(data)
        assert ss.status(data).installed
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert not (data / ss.BACKUP_DIRNAME).exists()  # nothing to back up

    def test_feed_command_embeds_rota_home(self, env):
        assert f"ROTA_HOME={env[1]}" in ss.feed_command()
        assert ss.feed_command().endswith(" feed")

    def test_default_line_prints_usage(self, env, tmp_path):
        command = ss.default_command(feed=f"{sys.executable} -m claude_rotater feed")
        payload = json.dumps({
            "model": {"display_name": "Opus"},
            "rate_limits": {"five_hour": {"used_percentage": 42.4},
                            "seven_day": {"used_percentage": 7}},
        })
        out = subprocess.run(["/bin/sh", "-c", command], input=payload.encode(),
                             capture_output=True, timeout=20, env=dict(os.environ))
        assert out.stdout.decode().strip() == "Opus · 5h 42% · 7d 7%"


class TestCareWithUserFiles:
    def test_invalid_json_is_never_touched(self, env):
        path, data = env
        path.write_text("{ broken")
        with pytest.raises(ss.StatuslineSetupError):
            ss.install(data)
        with pytest.raises(ss.StatuslineSetupError):
            ss.uninstall(data)
        assert path.read_text() == "{ broken"

    def test_non_object_is_never_touched(self, env):
        env[0].write_text("[]")
        with pytest.raises(ss.StatuslineSetupError):
            ss.install(env[1])

    def test_symlinked_settings_keeps_its_link(self, env, tmp_path):
        path, data = env
        real = tmp_path / "dotfiles" / "settings.json"
        real.parent.mkdir()
        write_settings(real, ORIGINAL)
        path.symlink_to(real)
        ss.install(data)
        assert path.is_symlink()
        assert ss.unwrap(json.loads(real.read_text())["statusLine"]["command"])

    def test_file_mode_is_kept(self, env):
        path, data = env
        write_settings(path, ORIGINAL)
        path.chmod(0o644)
        ss.install(data)
        assert oct(path.stat().st_mode & 0o777) == "0o644"

    def test_hand_wired_statusline_is_left_alone(self, env):
        path, data = env
        before = write_settings(path, {"statusLine": {"type": "command",
                                                      "command": "rota feed --tee | x.sh"}})
        ss.install(data)
        ss.uninstall(data)
        assert path.read_text() == before

    def test_backups_are_bounded(self, env):
        path, data = env
        write_settings(path, ORIGINAL)
        for _ in range(ss.MAX_BACKUPS + 3):
            ss.install(data)
            ss.uninstall(data)
        assert len(list((data / ss.BACKUP_DIRNAME).iterdir())) == ss.MAX_BACKUPS

    def test_backups_in_one_clock_tick_do_not_collide_and_keep_the_newest(
        self, env, monkeypatch
    ):
        path, data = env
        # A microsecond clock read twice per tick, as on macOS.
        reads = iter(range(10_000))
        monkeypatch.setattr(
            ss.time, "time_ns", lambda: 1_700_000_000_000_000_000 + next(reads) // 2 * 1000
        )
        for i in range(ss.MAX_BACKUPS + 2):
            write_settings(path, {**ORIGINAL, "n": i})
            ss.install(data)
            ss.uninstall(data)
        kept = sorted((data / ss.BACKUP_DIRNAME).iterdir())
        assert len(kept) == ss.MAX_BACKUPS
        assert json.loads(kept[-1].read_text())["n"] == ss.MAX_BACKUPS + 1
        assert all(json.loads(k.read_text())["n"] > 0 for k in kept)

    def test_write_failure_raises_and_leaves_the_file(self, env, monkeypatch):
        path, data = env
        before = write_settings(path, ORIGINAL)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(ss.os, "replace", boom)
        with pytest.raises(ss.StatuslineSetupError):
            ss.install(data)
        assert path.read_text() == before
        assert not list(path.parent.glob(".settings.*.tmp"))


class TestFeedNotSetUp:
    """`rota feed` exits 0 and records nothing when the data dir it feeds is
    missing; status() reports that as "unset" instead of a quiet "stale"."""

    def test_missing_target_dir_is_unset(self, env, tmp_path):
        path, data = env
        gone = tmp_path / "no-such-profile"
        write_settings(path, {"statusLine": {"type": "command",
                                             "command": f"ROTA_HOME={gone} rota feed --line # rota-statusline"}})
        assert ss.status(data).state == "unset"
        gone.mkdir()
        assert ss.status(data).state == "stale"

    def test_live_feed_wins_over_the_dir_check(self, env, tmp_path):
        path, data = env
        write_settings(path, {"statusLine": {"type": "command",
                                             "command": f"ROTA_HOME={tmp_path / 'gone'} rota feed --tee | x.sh"}})
        feed.append_record(feed.feed_path(data), {"t": 1.0, "five_hour": {"pct": 1.0}})
        mtime = os.stat(feed.feed_path(data)).st_mtime
        assert ss.status(data, now=mtime + 10).state == "live"

    @pytest.mark.parametrize(
        "command, expected",
        [
            ("ROTA_HOME=/a/b rota feed", "/a/b"),
            ("ROTA_HOME='/a b/c' rota feed", "/a b/c"),
            ('x | { ROTA_HOME="/q" /bin/rota feed; }', "/q"),
            ("ROTA_HOME=~/work rota feed", "~/work"),
        ],
    )
    def test_feed_target_reads_rota_home(self, command, expected):
        assert ss.feed_target(command) == Path(os.path.expanduser(expected))

    def test_feed_target_defaults_to_home_rota(self):
        from claude_rotater import paths

        assert ss.feed_target("rota feed --tee | x.sh") == paths.get_default_backup_root()
        assert ss.feed_target("MY_ROTA_HOME=/x rota feed") == paths.get_default_backup_root()

    def test_installed_command_round_trips(self, env):
        _path, data = env
        st = ss.install(data)
        assert ss.feed_target(st.command) == data
        assert st.state == "stale"

    def test_install_feeds_the_given_data_dir_without_rota_home(self, env, tmp_path, monkeypatch):
        """The TUI installs for its own data dir, whatever the environment says."""
        monkeypatch.delenv("ROTA_HOME")
        other = tmp_path / "other"
        other.mkdir()
        st = ss.install(other)
        assert f"ROTA_HOME={other}" in st.command and ss.feed_target(st.command) == other

    def test_default_data_dir_needs_no_rota_home(self, env, monkeypatch):
        from claude_rotater import paths

        monkeypatch.delenv("ROTA_HOME")
        assert "ROTA_HOME" not in ss.feed_command(paths.get_default_backup_root())
