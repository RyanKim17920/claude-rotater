"""apiKeyHelper key mode: the helper command, per-profile routing, the
stored key, and ``rota key`` (api_key_mode)."""

from __future__ import annotations

import io
import json
import os
import shlex
from pathlib import Path

import pytest

from claude_rotater import api_key_mode as akm
from claude_rotater import profiles

KEY = "sk-ant-api03-" + "f00dfeed01" * 4
KEY2 = "sk-ant-api03-" + "beadcafe02" * 4


@pytest.fixture
def homes(temp_home: Path):
    personal_cfg = temp_home / ".claude"
    personal_data = temp_home / ".rota"
    work_cfg = temp_home / ".claude-work"
    work_data = temp_home / "job" / ".rota"
    work_cfg.mkdir()
    return personal_cfg, personal_data, work_cfg, work_data


def _settings(cfg: Path) -> dict:
    return json.loads((cfg / "settings.json").read_text(encoding="utf-8"))


def _run_helper(command: str, config_dir: Path | None) -> tuple[int, str]:
    words = shlex.split(command.rsplit("#", 1)[0])
    argv = words[words.index("key") + 1 :]
    env = {} if config_dir is None else {"CLAUDE_CONFIG_DIR": str(config_dir)}
    out = io.StringIO()
    return akm.main(argv, environ=env, stdout=out), out.getvalue()


class TestHelperCommand:
    def test_round_trip_routes(self, homes):
        pc, pd, wc, wd = homes
        cmd = akm.helper_command([(pc, pd), (wc, wd)])
        assert akm.is_ours(cmd)
        assert akm.helper_routes(cmd) == [(str(pc), str(pd)), (str(wc), str(wd))]

    def test_foreign_command_has_no_routes(self):
        assert not akm.is_ours("~/bin/print-key")
        assert akm.helper_routes("~/bin/print-key --route a b") == []
        assert akm.helper_routes(None) == []

    def test_paths_with_spaces_survive(self, tmp_path):
        cfg, data = tmp_path / "my cfg", tmp_path / "my data"
        assert akm.helper_routes(akm.helper_command([(cfg, data)])) == [(str(cfg), str(data))]


class TestActivate:
    def test_activate_and_run_the_helper(self, homes):
        pc, pd, _wc, _wd = homes
        assert akm.activate(KEY, pd, pc, keychain=False, now=100.0) is None
        helper = _settings(pc)["apiKeyHelper"]
        assert KEY not in helper
        assert _run_helper(helper, None) == (0, KEY + "\n")  # unset = ~/.claude
        assert _run_helper(helper, pc) == (0, KEY + "\n")
        assert akm.active_key(pd, pc) == KEY
        assert akm.mode(pd) == akm.KeyMode(100.0, KEY[-4:])

    def test_reactivating_the_same_key_keeps_its_start(self, homes):
        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=False, now=100.0)
        akm.activate(KEY, pd, pc, keychain=False, now=200.0)
        assert akm.mode(pd).since == 100.0
        akm.activate(KEY2, pd, pc, keychain=False, now=300.0)
        assert akm.mode(pd).since == 300.0
        assert akm.helper_routes(_settings(pc)["apiKeyHelper"]) == [(str(pc), str(pd))]

    def test_helper_prints_nothing_for_an_unrouted_profile(self, homes):
        pc, pd, wc, _ = homes
        akm.activate(KEY, pd, pc, keychain=False)
        assert _run_helper(_settings(pc)["apiKeyHelper"], wc) == (1, "")

    def test_helper_prints_nothing_without_a_stored_key(self, homes):
        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=False)
        akm.forget_key(pd)
        assert _run_helper(_settings(pc)["apiKeyHelper"], pc) == (1, "")

    def test_each_profile_gets_its_own_key_from_a_shared_file(self, homes):
        pc, pd, wc, wd = homes
        (pc / "settings.json").write_text("{}\n", encoding="utf-8")
        (wc / "settings.json").symlink_to(pc / "settings.json")
        profiles.add("work", str(wd), str(wc), [str(wd.parent)])
        # The first profile cannot go alone: the other would get its key.
        why = akm.activate(KEY, pd, pc, keychain=False)
        assert why is not None and "shared" in why
        assert "apiKeyHelper" not in _settings(pc)
        assert akm.read_key(pd) is None
        # Once the other profile is keyed too, both are routed.
        settings = {"apiKeyHelper": akm.helper_command([(wc, wd)])}
        (pc / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        akm.store_key(wd, KEY2, keychain=False)
        assert akm.activate(KEY, pd, pc, keychain=False) is None
        helper = _settings(pc)["apiKeyHelper"]
        assert _run_helper(helper, pc) == (0, KEY + "\n")
        assert _run_helper(helper, wc) == (0, KEY2 + "\n")
        assert (wc / "settings.json").is_symlink()  # the link survives the write
        # Leaving drops only that profile's route.
        akm.deactivate(pd, pc)
        helper = _settings(pc)["apiKeyHelper"]
        assert akm.helper_routes(helper) == [(str(wc), str(wd))]
        assert _run_helper(helper, wc) == (0, KEY2 + "\n")

    def test_unparsable_settings_are_never_touched(self, homes):
        pc, pd, _, _ = homes
        (pc / "settings.json").write_text("{ not json", encoding="utf-8")
        assert akm.activate(KEY, pd, pc, keychain=False) is not None
        assert (pc / "settings.json").read_text(encoding="utf-8") == "{ not json"
        assert akm.read_key(pd) is None

    def test_a_backup_is_kept(self, homes):
        pc, pd, _, _ = homes
        (pc / "settings.json").write_text('{"theme": "dark"}\n', encoding="utf-8")
        akm.activate(KEY, pd, pc, keychain=False)
        backups = list((pd / "statusline-backups").glob("settings.*.json"))
        assert [b.read_text(encoding="utf-8") for b in backups] == ['{"theme": "dark"}\n']

    def test_keychain_storage(self, homes, block_real_keychain):
        from claude_rotater import macos_keychain

        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=True)
        acct = macos_keychain.keychain_account_name()
        assert block_real_keychain.get_password(akm.key_service(pd), acct) == KEY
        assert not (pd / akm.KEY_FILENAME).exists()
        akm.deactivate(pd, pc)
        assert block_real_keychain.get_password(akm.key_service(pd), acct) is None


class TestDeactivate:
    def test_removes_the_helper_and_the_key(self, homes):
        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=False)
        akm.deactivate(pd, pc)
        assert "apiKeyHelper" not in _settings(pc)
        assert akm.read_key(pd) is None
        assert not akm.mode(pd).on

    def test_a_foreign_helper_stays(self, homes):
        pc, pd, _, _ = homes
        (pc / "settings.json").write_text('{"apiKeyHelper": "my-key"}', encoding="utf-8")
        akm.deactivate(pd, pc)
        assert _settings(pc) == {"apiKeyHelper": "my-key"}

    def test_no_settings_file_is_fine(self, homes):
        pc, pd, _, _ = homes
        akm.deactivate(pd, pc)
        assert not (pc / "settings.json").exists()


class TestOverrides:
    def test_env_and_foreign_helper(self, homes):
        pc, _, _, _ = homes
        assert akm.overrides(pc, environ={}) == []
        env = {"ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": ""}
        assert akm.overrides(pc, environ=env) == ["ANTHROPIC_API_KEY is set"]
        (pc / "settings.json").write_text('{"apiKeyHelper": "my-key"}', encoding="utf-8")
        assert akm.overrides(pc, environ={}) == ["an apiKeyHelper in settings.json"]

    def test_rotas_own_helper_is_no_override(self, homes):
        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=False)
        assert akm.overrides(pc, environ={}) == []


class TestKeyCommand:
    def test_never_writes_stderr_and_survives_garbage(self, capsys):
        assert akm.main(["--route"], environ={}) == 1
        assert akm.main([], environ={"CLAUDE_CONFIG_DIR": "\0bad"}) == 1
        out, err = capsys.readouterr()
        assert out == "" and err == ""

    def test_console_entry_dispatches_before_the_cli(self, homes, monkeypatch, capsys):
        import sys

        from claude_rotater import entry

        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=False)
        monkeypatch.setattr(sys, "argv", ["rota", "key", "--route", str(pc), str(pd)])
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with pytest.raises(SystemExit) as done:
            entry.main()
        assert done.value.code == 0
        assert capsys.readouterr().out == KEY + "\n"

    def test_file_permissions(self, homes):
        pc, pd, _, _ = homes
        akm.activate(KEY, pd, pc, keychain=False)
        assert (os.stat(pd / akm.KEY_FILENAME).st_mode & 0o777) == 0o600


def _share(pc: Path, wc: Path, wd: Path, text: str = '{"theme": "dark"}\n') -> None:
    (pc / "settings.json").write_text(text, encoding="utf-8")
    (wc / "settings.json").symlink_to(pc / "settings.json")
    profiles.add("work", str(wd), str(wc), [str(wd.parent)])


class TestAvailability:
    def test_own_file_is_available(self, homes):
        pc, _, _, _ = homes
        assert akm.unavailable(pc) is None
        assert akm.shared_with(pc) == []

    def test_a_shared_file_is_not(self, homes):
        pc, _, wc, wd = homes
        _share(pc, wc, wd)
        assert akm.shared_with(wc) == [str(pc)]
        assert "shared with" in akm.unavailable(wc)
        assert "shared with" in akm.unavailable(pc)

    def test_a_sharer_already_on_the_helper_does_not_block(self, homes):
        pc, pd, wc, wd = homes
        _share(pc, wc, wd)
        settings = {"apiKeyHelper": akm.helper_command([(pc, pd)])}
        (pc / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        assert akm.unavailable(wc) is None

    def test_a_foreign_helper_blocks(self, homes):
        pc, _, _, _ = homes
        (pc / "settings.json").write_text('{"apiKeyHelper": "~/bin/k"}', encoding="utf-8")
        assert "not rota's" in akm.unavailable(pc)


class TestSplit:
    def test_replaces_the_link_with_a_copy(self, homes):
        pc, _, wc, wd = homes
        _share(pc, wc, wd)
        akm.split_settings(wc, wd)
        mine = wc / "settings.json"
        assert not mine.is_symlink()
        assert mine.read_text(encoding="utf-8") == '{"theme": "dark"}\n'
        assert (pc / "settings.json").read_text(encoding="utf-8") == '{"theme": "dark"}\n'
        assert akm.shared_with(wc) == [] and akm.unavailable(wc) is None
        backups = list((wd / "statusline-backups").glob("settings.*.json"))
        assert [b.read_text(encoding="utf-8") for b in backups] == ['{"theme": "dark"}\n']

    def test_a_hard_link_splits_too(self, homes):
        pc, _, wc, wd = homes
        (pc / "settings.json").write_text("{}\n", encoding="utf-8")
        os.link(pc / "settings.json", wc / "settings.json")
        profiles.add("work", str(wd), str(wc), [str(wd.parent)])
        assert akm.shared_with(wc) == [str(pc)]
        akm.split_settings(wc, wd)
        assert os.stat(wc / "settings.json").st_ino != os.stat(pc / "settings.json").st_ino

    def test_the_copy_feeds_this_profile(self, homes):
        from claude_rotater import feed, statusline_setup as ss

        pc, pd, wc, wd = homes
        command = ss.wrap("~/bin/line.sh", ss.feed_command(pd))
        _share(pc, wc, wd, json.dumps({"statusLine": {"type": "command", "command": command}}))
        akm.split_settings(wc, wd)
        copied = _settings(wc)["statusLine"]["command"]
        assert ss.feed_target(copied, wc) == wd
        assert feed.route_for(str(wc), ss._routes(copied)) == str(wd)
        assert _settings(pc)["statusLine"]["command"] == command  # the original is untouched

    def test_refuses_what_is_not_shared(self, homes):
        from claude_rotater.statusline_setup import StatuslineSetupError

        pc, pd, _, _ = homes
        (pc / "settings.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(StatuslineSetupError, match="not shared"):
            akm.split_settings(pc, pd)

    def test_refuses_one_folder_under_two_names(self, homes, temp_home):
        from claude_rotater.statusline_setup import StatuslineSetupError

        pc, _, _, _ = homes
        (pc / "settings.json").write_text("{}\n", encoding="utf-8")
        alias = temp_home / ".claude-alias"
        alias.symlink_to(pc)
        data = temp_home / "alias" / ".rota"
        profiles.add("alias", str(data), str(alias), [str(data.parent)])
        with pytest.raises(StatuslineSetupError, match="same folder"):
            akm.split_settings(alias, data)
        assert (pc / "settings.json").read_text(encoding="utf-8") == "{}\n"


def test_a_shared_file_only_ever_routes_every_sharer(homes, temp_home):
    """Whatever order three profiles sharing one settings.json try key mode
    in, the file never carries a helper that leaves one of them out."""
    pc, pd, wc, wd = homes
    tc, td = temp_home / ".claude-third", temp_home / "third" / ".rota"
    tc.mkdir()
    _share(pc, wc, wd, "{}\n")
    (tc / "settings.json").symlink_to(pc / "settings.json")
    profiles.add("third", str(td), str(tc), [str(td.parent)])
    trio = [(pc, pd), (wc, wd), (tc, td)]
    for cfg, data in trio + trio[::-1]:
        akm.activate(KEY, data, cfg, keychain=False)
        helper = _settings(pc).get("apiKeyHelper")
        if helper is not None:
            routed = {c for c, _ in akm.helper_routes(helper)}
            assert routed == {str(c) for c, _ in trio}, routed
