"""The key ``rota key`` serves is the one last activated, or none: never
another copy left in the other store (api_key_mode's Keychain item and its
0600 file). Every store transition, against the in-memory Keychain."""

from __future__ import annotations

import io
import itertools
import json
import os
import shlex
import subprocess
import threading
from pathlib import Path

import pytest

from claude_rotater import api_key_mode as akm
from claude_rotater import macos_keychain, statusline_setup

A = "sk-ant-api03-" + "aaaa1111" * 5 + "1111"
B = "sk-ant-api03-" + "bbbb2222" * 5 + "2222"
# Real keys end alike often enough ("...gAA"): four characters name no key.
A_TWIN = "sk-ant-api03-" + "cccc3333" * 5 + "1111"

DOWN = frozenset({"get", "set", "delete"})


class Crash(BaseException):
    """The process died here: nothing after it runs, no handler catches it."""


class FlakyKeychain:
    """The fake Keychain, with operations that can be made to fail."""

    def __init__(self, store, monkeypatch):
        self.store = store
        self.failing: frozenset[str] = frozenset()
        self.error: Exception = macos_keychain.KeychainError("locked")
        self.calls: list[str] = []
        self.after_set = lambda: None
        for op in ("get", "set", "delete"):
            monkeypatch.setattr(macos_keychain, f"{op}_password", self._op(op))

    def _op(self, op):
        real = getattr(self.store, f"{op}_password")

        def call(*args):
            self.calls.append(op)
            if op in self.failing:
                raise self.error
            result = real(*args)
            if op == "set":
                self.after_set()
            return result

        return call

    def item(self, data: Path) -> str | None:
        account = macos_keychain.keychain_account_name()
        return self.store.get_password(akm.key_service(data), account)

    def plant(self, data: Path, key: str) -> None:
        account = macos_keychain.keychain_account_name()
        self.store.set_password(akm.key_service(data), account, key)


@pytest.fixture
def kc(block_real_keychain, monkeypatch):
    monkeypatch.setattr(akm, "_use_keychain", lambda: True)
    return FlakyKeychain(block_real_keychain, monkeypatch)


@pytest.fixture
def dirs(temp_home: Path):
    return temp_home / ".claude", temp_home / ".rota"


def _helper(cfg: Path) -> str:
    return json.loads((cfg / "settings.json").read_text(encoding="utf-8"))["apiKeyHelper"]


def _run(helper: str, cfg: Path) -> str | None:
    """What a session of ``cfg`` running ``helper`` gets."""
    words = shlex.split(helper.rsplit("#", 1)[0])
    out = io.StringIO()
    code = akm.main(
        words[words.index("key") + 1 :],
        environ={"CLAUDE_CONFIG_DIR": str(cfg)},
        stdout=out,
    )
    assert (code == 0) == bool(out.getvalue())
    return out.getvalue().strip() or None


def _served(cfg: Path) -> str | None:
    return _run(_helper(cfg), cfg)


def _key_file(data: Path) -> str | None:
    try:
        return (data / akm.KEY_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _fail_replace(monkeypatch, name: str, error: BaseException, *, after: bool = False) -> None:
    """Fail the atomic replace onto files called ``name``."""
    real = os.replace

    def replace(src, dst):
        if Path(dst).name != name:
            return real(src, dst)
        if after:
            real(src, dst)
        raise error

    monkeypatch.setattr(os, "replace", replace)


class TestKeychainTransitions:
    @pytest.mark.parametrize("second", [B, A_TWIN], ids=["other", "same-tail"])
    def test_usable_to_unusable_then_readable_again(self, kc, dirs, second):
        cfg, data = dirs
        assert akm.activate(A, data, cfg, keychain=True) is None
        kc.failing = DOWN
        assert akm.activate(second, data, cfg, keychain=False) is None
        assert _served(cfg) == second
        kc.failing = frozenset()
        assert _served(cfg) == second
        assert akm.active_key(data, cfg) == second

    def test_readable_but_not_writable(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        kc.failing = frozenset({"set", "delete"})
        assert akm.activate(B, data, cfg, keychain=False) is None
        assert kc.item(data) == A  # the stale copy could not be removed
        assert _served(cfg) == B

    @pytest.mark.parametrize("second", [B, A_TWIN], ids=["other", "same-tail"])
    def test_unusable_to_usable_then_locked_again(self, kc, dirs, monkeypatch, second):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=False)
        real = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == akm.KEY_FILENAME:
                raise PermissionError("cannot delete")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        assert akm.activate(second, data, cfg, keychain=True) is None
        assert _key_file(data) == A  # the stale copy could not be removed
        assert _served(cfg) == second
        kc.failing = DOWN
        assert _served(cfg) is None

    @pytest.mark.parametrize(
        ("first", "then"), [(True, False), (False, True)], ids=["to-file", "to-keychain"]
    )
    def test_the_other_copy_is_removed(self, kc, dirs, first, then):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=first)
        assert akm.activate(B, data, cfg, keychain=then) is None
        assert kc.item(data) == (B if then else None)
        assert _key_file(data) == (None if then else B)

    @pytest.mark.parametrize(
        "error",
        [
            macos_keychain.KeychainError("locked"),
            subprocess.TimeoutExpired("security", 5),
            FileNotFoundError("security"),
        ],
        ids=["locked", "timeout", "no-binary"],
    )
    def test_an_unreadable_keychain(self, kc, dirs, error):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        kc.failing, kc.error = frozenset({"get"}), error
        assert _served(cfg) is None
        kc.failing = frozenset()
        assert _served(cfg) == A

    def test_a_key_in_the_file_needs_no_keychain_read(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=False)
        kc.failing = DOWN
        kc.calls.clear()
        assert _served(cfg) == A
        assert kc.calls == []

    def test_a_garbled_file_does_not_hide_the_keychain(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        (data / akm.KEY_FILENAME).write_bytes(b"\xff\xfe\xff")
        assert _served(cfg) == A

    def test_a_key_rota_did_not_activate_is_not_served(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        kc.plant(data, A_TWIN)
        assert _served(cfg) is None

    def test_every_sequence_serves_the_last_activation(self, kc, tmp_path):
        """Three operations in every order, backend and failure: with the
        Keychain readable the last key is served exactly, with it locked
        that key if it is in the file, else none."""
        stores = [(key, backend) for key in (A, A_TWIN) for backend in (True, False)]
        steps = list(itertools.product([*stores, None], [(), ("delete",), tuple(DOWN)]))
        runs = itertools.product([(s, ()) for s in stores], steps, steps)
        for n, run in enumerate(runs):
            data, last, in_file = tmp_path / str(n), None, False
            for op, failing in run:
                kc.failing = frozenset(failing)
                if op is None:
                    akm.forget_key(data)
                    last = None
                else:
                    try:
                        akm.store_key(data, *op)
                    except macos_keychain.KeychainError:
                        pass  # not activated: the previous one stands
                    else:
                        last, in_file = op[0], not op[1]
                kc.failing = DOWN
                assert akm.read_key(data) == (last if in_file else None), run
                kc.failing = frozenset()
                assert akm.read_key(data) == last, run


class TestPartWayThroughActivate:
    @pytest.mark.parametrize("backend", [True, False], ids=["keychain", "file"])
    def test_a_failed_key_write_changes_nothing(self, kc, dirs, monkeypatch, backend):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=backend)
        if backend:
            kc.failing = frozenset({"set"})
        else:
            _fail_replace(monkeypatch, akm.KEY_FILENAME, PermissionError("busy"))
        assert akm.activate(B, data, cfg, keychain=backend) is not None
        kc.failing = frozenset()
        assert akm.read_key(data) == A

    def test_a_write_that_timed_out_after_landing(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)

        def timed_out():
            raise macos_keychain.KeychainError("timed out")

        kc.after_set = timed_out
        assert akm.activate(A_TWIN, data, cfg, keychain=True) is not None
        assert _served(cfg) is None

    @pytest.mark.parametrize("first", [True, False], ids=["from-keychain", "from-file"])
    @pytest.mark.parametrize("then", [True, False], ids=["to-keychain", "to-file"])
    def test_the_record_cannot_be_written(self, kc, dirs, monkeypatch, first, then):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=first)
        _fail_replace(monkeypatch, akm.STATE_FILENAME, PermissionError("busy"))
        assert akm.activate(A_TWIN, data, cfg, keychain=then) is not None
        assert _served(cfg) == (A if first != then else None)

    @pytest.mark.parametrize("first", [True, False], ids=["from-keychain", "from-file"])
    @pytest.mark.parametrize("then", [True, False], ids=["to-keychain", "to-file"])
    @pytest.mark.parametrize("committed", [True, False], ids=["after", "before"])
    def test_a_crash_around_the_commit(self, kc, dirs, monkeypatch, first, then, committed):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=first)
        _fail_replace(monkeypatch, akm.STATE_FILENAME, Crash(), after=committed)
        with pytest.raises(Crash):
            akm.activate(A_TWIN, data, cfg, keychain=then)
        if committed:
            assert _served(cfg) == A_TWIN
        else:
            assert _served(cfg) == (A if first != then else None)

    def test_a_crash_before_the_helper_is_written(self, kc, dirs, monkeypatch):
        cfg, data = dirs

        def die(*args):
            raise Crash

        monkeypatch.setattr(statusline_setup, "write_settings", die)
        with pytest.raises(Crash):
            akm.activate(A, data, cfg, keychain=True)
        assert akm.active_key(data, cfg) is None

    def test_settings_cannot_be_written(self, kc, dirs, monkeypatch):
        cfg, data = dirs
        kc.failing = frozenset({"delete"})

        def refuse(*args):
            raise statusline_setup.StatuslineSetupError("read-only")

        monkeypatch.setattr(statusline_setup, "write_settings", refuse)
        assert akm.activate(A, data, cfg, keychain=True) == "read-only"
        assert akm.read_key(data) is None
        assert not akm.mode(data).on

    def test_a_failed_write_leaves_no_plaintext_behind(self, kc, dirs, monkeypatch):
        cfg, data = dirs
        _fail_replace(monkeypatch, akm.KEY_FILENAME, PermissionError("busy"))
        assert akm.activate(A, data, cfg, keychain=False) is not None
        assert [p for p in data.iterdir() if p.is_file()] == []

    def test_a_keychain_failure_is_no_reason_for_a_file(self, kc, dirs):
        cfg, data = dirs
        kc.failing = DOWN
        assert akm.activate(A, data, cfg, keychain=True) is not None
        assert _key_file(data) is None

    def test_the_key_itself_is_not_in_the_record(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        assert A not in (data / akm.STATE_FILENAME).read_text(encoding="utf-8")
        assert _key_file(data) is None

    def test_a_twin_key_starts_its_own_time_on_duty(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True, now=100.0)
        akm.activate(A, data, cfg, keychain=False, now=200.0)
        assert akm.mode(data).since == 100.0
        akm.activate(A_TWIN, data, cfg, keychain=False, now=300.0)
        assert akm.mode(data).since == 300.0


class TestLeavingKeyMode:
    @pytest.mark.parametrize("leave", ["deactivate", "forget"])
    @pytest.mark.parametrize("backend", [True, False], ids=["keychain", "file"])
    def test_a_stale_key_does_not_come_back(self, kc, dirs, leave, backend):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=backend)
        kc.plant(data, A)
        helper = _helper(cfg)  # a running session may still have it
        kc.failing = DOWN
        if leave == "deactivate":
            akm.deactivate(data, cfg)
        else:
            akm.forget_key(data)
        kc.failing = frozenset()
        assert kc.item(data) == A
        assert akm.read_key(data) is None
        assert _run(helper, cfg) is None

    def test_reactivating_over_a_key_that_would_not_go(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        kc.failing = DOWN
        akm.deactivate(data, cfg)
        assert akm.activate(B, data, cfg, keychain=False) is None
        kc.failing = frozenset()
        assert _served(cfg) == B

    def test_the_helper_cannot_be_removed(self, kc, dirs, monkeypatch):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        kc.failing = frozenset({"delete"})

        def refuse(*args):
            raise statusline_setup.StatuslineSetupError("read-only")

        monkeypatch.setattr(statusline_setup, "write_settings", refuse)
        akm.deactivate(data, cfg)
        assert _served(cfg) is None


class TestFileOnlyPlatforms:
    def test_never_touches_the_keychain(self, block_real_keychain, dirs, monkeypatch):
        kc = FlakyKeychain(block_real_keychain, monkeypatch)
        monkeypatch.setattr(akm, "_use_keychain", lambda: False)
        cfg, data = dirs
        kc.plant(data, B)
        assert akm.activate(A, data, cfg) is None
        assert _served(cfg) == A
        assert akm.activate(A_TWIN, data, cfg) is None
        assert _served(cfg) == A_TWIN
        akm.deactivate(data, cfg)
        assert akm.read_key(data) is None
        assert kc.calls == []


class TestStateFromBeforeFingerprints:
    """0.1.0's record holds only the start and the key's last four."""

    @pytest.fixture
    def old(self, kc, dirs):
        cfg, data = dirs

        def write(active: str, *, item: str | None = None, file: str | None = None):
            data.mkdir(parents=True, exist_ok=True)
            if item:
                kc.plant(data, item)
            if file:
                (data / akm.KEY_FILENAME).write_text(file, encoding="utf-8")
            state = {"since": 100.0, "tail": active[-4:]}
            (data / akm.STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
            settings = {"apiKeyHelper": akm.helper_command([(str(cfg), str(data))])}
            (cfg / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

        return write

    @pytest.mark.parametrize("copies", [{"item": A}, {"file": A}, {"item": A, "file": A}])
    def test_users_keep_working(self, old, dirs, copies):
        cfg, _ = dirs
        old(A, **copies)
        assert _served(cfg) == A

    def test_the_file_works_without_a_keychain(self, old, dirs, monkeypatch):
        cfg, _ = dirs
        old(A, file=A)
        monkeypatch.setattr(akm, "_use_keychain", lambda: False)
        assert _served(cfg) == A

    @pytest.mark.parametrize("copies", [{"item": A, "file": B}, {"item": B, "file": A}])
    @pytest.mark.parametrize("active", [A, B], ids=["a", "b"])
    def test_two_copies_go_by_the_tail(self, old, dirs, copies, active):
        cfg, _ = dirs
        old(active, **copies)
        assert _served(cfg) == active

    def test_two_copies_with_one_tail_are_not_guessed_between(self, old, dirs):
        cfg, _ = dirs
        old(A, item=A_TWIN, file=A)
        assert _served(cfg) is None

    @pytest.mark.parametrize("copies", [{"item": B}, {"file": B}])
    def test_a_lone_stale_copy(self, old, dirs, copies):
        cfg, _ = dirs
        old(A, **copies)
        assert _served(cfg) is None

    def test_a_file_beside_an_unreadable_item_waits(self, kc, old, dirs):
        cfg, _ = dirs
        old(A, item=A_TWIN, file=A)
        kc.failing = frozenset({"get"})
        assert _served(cfg) is None

    def test_no_record_serves_nothing(self, old, dirs):
        cfg, data = dirs
        old(A, item=A, file=A)
        (data / akm.STATE_FILENAME).unlink()
        assert _served(cfg) is None

    @pytest.mark.parametrize("state", ["", "{", "[]", '{"since": 1}', '{"since": 1, "tail": ""}'])
    def test_a_damaged_record_serves_nothing(self, old, dirs, state):
        cfg, data = dirs
        old(A, item=A, file=A)
        (data / akm.STATE_FILENAME).write_text(state, encoding="utf-8")
        assert _served(cfg) is None

    def test_the_next_activation_names_the_key_exactly(self, old, dirs):
        cfg, data = dirs
        old(A, item=A)
        assert akm.activate(A, data, cfg, keychain=False, now=200.0) is None
        assert akm.mode(data).since == 100.0
        assert akm.mode(data).digest

    def test_an_interrupted_switch_to_a_twin(self, old, dirs, monkeypatch):
        cfg, data = dirs
        old(A, item=A)
        real, writes = os.replace, []

        def replace(src, dst):
            if Path(dst).name == akm.STATE_FILENAME:
                writes.append(dst)
                if len(writes) > 1:  # the first only makes the old record exact
                    raise Crash
            return real(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(Crash):
            akm.activate(A_TWIN, data, cfg, keychain=True)
        assert _served(cfg) is None

    def test_an_interrupted_switch_from_an_unreadable_key(self, kc, old, dirs, monkeypatch):
        cfg, data = dirs
        old(A, item=A)
        kc.failing = DOWN
        _fail_replace(monkeypatch, akm.STATE_FILENAME, Crash())
        with pytest.raises(Crash):
            akm.activate(A_TWIN, data, cfg, keychain=False)
        kc.failing = frozenset()
        assert _served(cfg) is None


class TestConcurrentActivate:
    def test_a_read_between_the_two_writes_gets_neither(self, kc, dirs):
        cfg, data = dirs
        akm.activate(A, data, cfg, keychain=True)
        seen = []
        kc.after_set = lambda: seen.append(akm.read_key(data))
        akm.activate(A_TWIN, data, cfg, keychain=True)
        assert seen == [None]

    @pytest.mark.parametrize("outer", [True, False], ids=["outer-keychain", "outer-file"])
    @pytest.mark.parametrize("inner", [True, False], ids=["inner-keychain", "inner-file"])
    def test_an_activation_inside_another(self, kc, dirs, monkeypatch, outer, inner):
        cfg, data = dirs
        real, entered = os.replace, []

        def replace(src, dst):
            if Path(dst).name == akm.STATE_FILENAME and not entered:
                entered.append(dst)
                assert akm.activate(B, data, cfg, keychain=inner) is None
            return real(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        assert akm.activate(A, data, cfg, keychain=outer) is None
        assert _served(cfg) in (A, None)  # A committed last

    def test_racing_switches_end_on_the_last_one(self, kc, dirs, monkeypatch):
        from claude_rotater.models import Platform
        from claude_rotater.switcher import ClaudeAccountSwitcher

        cfg, _ = dirs
        keys = [f"sk-ant-api03-{'x' * 30}{i}AAAA" for i in range(6)]
        done: list[str] = []
        real = akm.activate

        def activate(key, *args, **kwargs):
            assert real(key, *args, **kwargs) is None
            done.append(key)  # still under the caller's lock

        monkeypatch.setattr(akm, "activate", activate)
        switchers = [ClaudeAccountSwitcher() for _ in keys]
        for switcher in switchers:
            switcher.platform = Platform.MACOS
        switchers[0]._setup_directories()
        threads = [
            threading.Thread(target=s._write_credentials, args=(key,))
            for s, key in zip(switchers, keys)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert sorted(done) == sorted(keys)
        assert _served(cfg) == done[-1]


class TestThroughTheSwitcher:
    @pytest.fixture
    def switcher(self, kc, dirs):
        from claude_rotater.models import Platform
        from claude_rotater.paths import get_credentials_path
        from claude_rotater.switcher import ClaudeAccountSwitcher

        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._init_sequence_file()
        s.add_account_from_token(A)
        s.add_account_from_token(B)
        login = {"claudeAiOauth": {"accessToken": "under", "refreshToken": "under-r"}}
        get_credentials_path().write_text(json.dumps(login), encoding="utf-8")
        return s

    def test_switch_while_the_keychain_cannot_be_written(self, switcher, kc, dirs):
        cfg, _ = dirs
        switcher.switch_to("1", json_output=True)
        assert _served(cfg) == A
        kc.failing = frozenset({"set", "delete"})
        with pytest.raises(macos_keychain.KeychainError):
            switcher._store._kc_call(macos_keychain.set_password, "probe", "x", "y")
        assert switcher.switch_to("2", json_output=True)["switched"]
        kc.failing = frozenset()
        assert switcher.current_account_number() == "2"
        assert _served(cfg) == B

    def _lose_key(self, switcher, dirs) -> None:
        switcher.switch_to("1", json_output=True)
        akm.forget_key(switcher.backup_dir)
        assert _served(dirs[0]) is None

    def test_a_key_slot_never_takes_the_login_under_it(self, switcher, dirs):
        self._lose_key(switcher, dirs)
        assert switcher.switch_to("2", json_output=True)["switched"]
        assert switcher._read_account_credentials("1", "api-key-1@token.local") == A
        switcher.switch_to("1", json_output=True)
        assert _served(dirs[0]) == A

    def test_a_key_slot_never_takes_another_key(self, switcher, dirs):
        cfg, data = dirs
        switcher.switch_to("1", json_output=True)
        # A crash between handing B out and recording the switch to it.
        akm.activate(B, switcher.backup_dir, cfg, keychain=True)
        assert switcher.switch_to("2", json_output=True)["switched"]
        assert switcher._read_account_credentials("1", "api-key-1@token.local") == A

    def test_picking_the_key_again_restores_it(self, switcher, dirs):
        self._lose_key(switcher, dirs)
        switcher.switch_to("1", json_output=True)
        assert _served(dirs[0]) == A

    def test_a_key_slot_never_polls_the_login_under_it(self, switcher, dirs, monkeypatch):
        self._lose_key(switcher, dirs)
        from claude_rotater.switcher import FetchRecord

        polled = []

        def fetch(*args, **kwargs):
            polled.append(args)
            return FetchRecord(error="not expected")

        monkeypatch.setattr(switcher, "_fetch_active_usage", fetch)
        switcher.accounts_snapshot()
        assert polled == []

    @pytest.mark.parametrize("pick", ["1", "3"], ids=["another", "the-login-again"])
    def test_a_login_slot_never_takes_the_key(self, switcher, dirs, pick):
        cfg, data = dirs
        switcher.add_account_from_token("sk-ant-oat01-" + "o" * 40)
        email = "setup-token-3@token.local"
        login = switcher._read_account_credentials("3", email)
        switcher.switch_to("3", json_output=True)
        # A crash between handing B out and recording the switch to it.
        akm.activate(B, switcher.backup_dir, cfg, keychain=True)
        switcher.switch_to(pick, json_output=True)
        assert switcher._read_account_credentials("3", email) == login
        assert switcher._read_credentials() == (A if pick == "1" else login)
