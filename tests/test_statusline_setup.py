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

    def test_live_idle_and_stale_follow_the_feed_file(self, env):
        path, data = env
        ss.install(data)
        assert ss.status(data).state == "stale"  # nothing fed yet
        feed.append_record(feed.feed_path(data), {"t": 1.0, "five_hour": {"pct": 1.0}})
        mtime = os.stat(feed.feed_path(data)).st_mtime
        assert ss.status(data, now=mtime + 10).state == "live"
        # Fed before, quiet since: no chat answered lately, which is normal.
        assert ss.status(data, now=mtime + 10_000).state == "idle"

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

    def test_feed_command_routes_this_profile_to_its_data_dir(self, env):
        path, data = env
        command = ss.feed_command()
        assert "ROTA_HOME" not in command
        assert command.endswith(f" feed --route {path.parent} {data}")

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

    def test_missing_target_dir_is_unset(self, env):
        path, data = env
        write_settings(path, {"statusLine": {"type": "command",
                                             "command": f"ROTA_HOME={data} rota feed --line # rota-statusline"}})
        assert ss.status(data).state == "stale"
        data.rmdir()
        assert ss.status(data).state == "unset"

    def test_feeding_another_data_dir_is_not_set_up_here(self, env, tmp_path):
        """An older install's ROTA_HOME for some other data dir feeds that
        profile, whether or not the dir exists: not set up for this one."""
        path, data = env
        other = tmp_path / "other-profile"
        write_settings(path, {"statusLine": {"type": "command",
                                             "command": f"ROTA_HOME={other} rota feed --line # rota-statusline"}})
        assert (ss.status(data).install, ss.status(data).state) == (ss.FOREIGN, "unset")
        other.mkdir()
        assert (ss.status(data).install, ss.status(data).state) == (ss.FOREIGN, "unset")

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
        assert ss.feed_target(st.command, path_parent := _path.parent) == data
        assert ss.feed_target(st.command) != data  # other profiles fall through
        assert ss.feed_target(st.command, path_parent / "x") != data
        assert st.state == "stale"

    def test_install_feeds_the_given_data_dir_without_rota_home(self, env, tmp_path, monkeypatch):
        """The TUI installs for its own data dir, whatever the environment says."""
        monkeypatch.delenv("ROTA_HOME")
        other = tmp_path / "other"
        other.mkdir()
        st = ss.install(other)
        assert "ROTA_HOME" not in st.command
        assert ss.feed_target(st.command, st.settings_path.parent) == other

    def test_default_data_dir_needs_no_rota_home(self, env, monkeypatch):
        from claude_rotater import paths

        monkeypatch.delenv("ROTA_HOME")
        command = ss.feed_command(paths.get_default_backup_root())
        assert "ROTA_HOME" not in command and "--route" not in command


class TestSharedSettings:
    """Two profiles, one settings.json: the second config dir's settings.json
    is a symlink to the first's, as dotfile setups often do. Each profile's
    install must feed its own data dir, in either order."""

    STATUSLINE = "cat >/dev/null; echo line"

    @pytest.fixture
    def profiles(self, tmp_path, monkeypatch):
        from claude_rotater import paths

        personal = tmp_path / "personal-config"
        work = tmp_path / "work config"  # a space, as real paths can have
        personal.mkdir()
        work.mkdir()
        write_settings(personal / "settings.json",
                       {"statusLine": {"type": "command", "command": self.STATUSLINE}})
        (work / "settings.json").symlink_to(personal / "settings.json")
        default_data = paths.get_default_backup_root()
        default_data.mkdir(parents=True, exist_ok=True)
        work_data = tmp_path / "work data"
        work_data.mkdir()

        def use(profile: str) -> Path:
            if profile == "personal":
                monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
                monkeypatch.delenv("ROTA_HOME", raising=False)
                return default_data
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
            monkeypatch.setenv("ROTA_HOME", str(work_data))
            return work_data

        return use, personal, work

    @staticmethod
    def command(personal: Path) -> str:
        return json.loads((personal / "settings.json").read_text())["statusLine"]["command"]

    @pytest.mark.parametrize("order", [("personal", "work"), ("work", "personal")])
    def test_each_install_feeds_its_own_profile(self, profiles, order):
        use, personal, work = profiles
        for profile in order:
            ss.install(use(profile))
        for profile in order:
            st = ss.status(use(profile))
            assert (st.install, st.state) == (ss.INSTALLED, "stale"), profile
        command = self.command(personal)
        assert ss.feed_target(command, work) == use("work")
        assert ss.feed_target(command, personal) == use("personal")
        assert command.count("--route") == 1
        assert ss.unwrap(command) == self.STATUSLINE
        assert (work / "settings.json").is_symlink()

    def test_set_up_in_one_profile_reads_as_not_set_up_in_the_other(self, profiles):
        """The reported bug: set up in one profile, the other profile's TUI
        said "installed" while every one of its records went elsewhere, and
        `s` there changed nothing."""
        use, personal, work = profiles
        ss.install(use("personal"))
        st = ss.status(use("work"))
        assert (st.install, st.state) == (ss.FOREIGN, "unset")
        assert ss.install(use("work")).installed
        assert ss.status(use("personal")).installed

    def test_install_again_is_a_no_op(self, profiles):
        use, personal, _work = profiles
        ss.install(use("personal"))
        ss.install(use("work"))
        before = self.command(personal)
        ss.install(use("work"))
        ss.install(use("personal"))
        assert self.command(personal) == before

    def test_records_reach_each_profiles_feed_through_the_installed_command(self, profiles):
        """Run the installed wrapper through /bin/sh as Claude Code does,
        without ROTA_HOME or CLAUDE_CONFIG_DIR, as a plain `claude` session
        started by a shell wrapper has neither of rota's variables."""
        use, personal, work = profiles
        ss.install(use("personal"))
        ss.install(use("work"))
        command = self.command(personal)
        child = {k: v for k, v in os.environ.items() if k not in ("ROTA_HOME", "CLAUDE_CONFIG_DIR")}
        child["HOME"] = str(Path.home())
        for config in (work, personal):
            payload = json.dumps({
                "transcript_path": f"{config}/projects/-repo/s.jsonl",
                "rate_limits": {"five_hour": {"used_percentage": 5}},
            })
            out = subprocess.run(["/bin/sh", "-c", command], input=payload.encode(),
                                 capture_output=True, timeout=30, env=child)
            assert out.stdout == b"line\n"
        for config, data in ((work, use("work")), (personal, use("personal"))):
            path = feed.feed_path(data)
            for _ in range(200):  # the feed side runs in the background
                if os.path.exists(path):
                    break
                subprocess.run(["sleep", "0.05"])
            assert [json.loads(line)["config_dir"] for line in open(path)] == [str(config)]

    def test_uninstall_in_one_profile_keeps_the_other_fed(self, profiles):
        use, personal, _work = profiles
        ss.install(use("personal"))
        ss.install(use("work"))
        assert ss.uninstall(use("work")).install == ss.FOREIGN  # feeds personal only
        assert ss.status(use("personal")).installed
        assert "--route" not in self.command(personal)

    def test_uninstall_restores_the_original_when_nothing_else_is_fed(self, profiles):
        """Only the work profile uses rota (no default data dir): its route
        was the wrapper's only use, so the original statusline comes back."""
        use, personal, _work = profiles
        use("personal").rmdir()
        ss.install(use("work"))
        ss.uninstall(use("work"))
        assert self.command(personal) == self.STATUSLINE

    def test_uninstall_of_the_unrouted_profile_restores_the_original(self, profiles):
        use, personal, _work = profiles
        ss.install(use("personal"))
        ss.uninstall(use("personal"))
        assert self.command(personal) == self.STATUSLINE
