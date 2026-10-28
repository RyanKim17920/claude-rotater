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
        assert akm.mode(pd) == akm.KeyMode(100.0, KEY[-4:], "helper", "")

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
