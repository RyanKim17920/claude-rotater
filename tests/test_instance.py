"""One auto engine per data dir; stale rota processes are named."""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

from claude_rotater import instance
from claude_rotater.instance import EngineLock, Holder


class TestEngineLock:
    def test_first_owns_second_sees_the_owner(self, tmp_path):
        first, second = EngineLock(tmp_path), EngineLock(tmp_path)
        assert first.acquire() is None and first.held
        holder = second.acquire()
        assert isinstance(holder, Holder) and not second.held
        assert holder.pid == os.getpid()
        record = json.loads((tmp_path / "engine.lock").read_text())
        assert record["pid"] == os.getpid() and "commit" in record and "startedAt" in record

    def test_release_lets_the_next_one_take_over(self, tmp_path):
        first, second = EngineLock(tmp_path), EngineLock(tmp_path)
        first.acquire()
        first.release()
        assert second.acquire() is None

    def test_reacquire_by_the_owner_is_a_no_op(self, tmp_path):
        lock = EngineLock(tmp_path)
        assert lock.acquire() is None
        assert lock.acquire() is None

    def test_a_lockless_older_rota_on_this_data_dir_blocks_the_engine(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(instance, "_rota_engine_pids", lambda: [4242])
        monkeypatch.setattr(
            instance, "_has_open",
            lambda pid, path: path.name == "rota.log",  # log open, no lock
        )
        holder = EngineLock(tmp_path).acquire()
        assert holder == Holder(pid=4242, legacy=True)
        assert holder.describe() == "another rota is running (pid 4242, older code)"

    def test_a_rota_on_another_data_dir_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instance, "_rota_engine_pids", lambda: [4242])
        monkeypatch.setattr(instance, "_has_open", lambda pid, path: False)
        assert EngineLock(tmp_path).acquire() is None


class TestHolder:
    def test_started_before_the_source_changed_is_older_code(self, monkeypatch):
        monkeypatch.setattr(instance, "code_commit", lambda: "abc1234")
        monkeypatch.setattr(instance, "newest_source_mtime", lambda: 2000.0)
        assert Holder(pid=1, commit="abc1234", started_at=1000.0).older_code
        assert not Holder(pid=1, commit="abc1234", started_at=3000.0).older_code

    def test_another_commit_is_older_code(self, monkeypatch):
        monkeypatch.setattr(instance, "code_commit", lambda: "abc1234")
        monkeypatch.setattr(instance, "newest_source_mtime", lambda: None)
        holder = Holder(pid=7, commit="0ld0000", started_at=time.time())
        assert holder.describe() == "another rota is running (pid 7, older code)"

    def test_same_code_says_only_the_pid(self, monkeypatch):
        monkeypatch.setattr(instance, "code_commit", lambda: "abc1234")
        monkeypatch.setattr(instance, "newest_source_mtime", lambda: 1.0)
        holder = Holder(pid=7, commit="abc1234", started_at=time.time())
        assert holder.describe() == "another rota is running (pid 7)"

    def test_warning_is_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="rota"):
            line = instance.warn_if_not_owner(Holder(pid=9, legacy=True))
        assert line.startswith("another rota is running (pid 9, older code)")
        assert "view-only" in line
        assert any("pid 9" in r.getMessage() for r in caplog.records)
        assert instance.warn_if_not_owner(None) == ""


@pytest.mark.parametrize("args,expected", [
    ("/u/.local/share/uv/tools/claude-rotater/bin/python /u/.local/bin/rota", True),
    ("/u/bin/python /u/.local/bin/rota --debug", True),
    ("/u/bin/python /u/.local/bin/rota auto", True),
    ("/u/bin/python /u/.local/bin/rota auto --once", False),
    ("/u/bin/python /u/.local/bin/rota feed", False),
    ("/u/bin/python -m claude_rotater", True),
    ("/usr/bin/vim rota.md", False),
])
def test_engine_command_lines(args, expected):
    assert instance._is_engine_command(args) is expected
