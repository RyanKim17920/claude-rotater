"""Tests for the proper-lockfile-compatible Claude Code lock helpers."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_rotater import claude_locks, fsutil
from claude_rotater.claude_locks import (
    claude_config_lock,
    claude_credentials_lock,
    config_lock_dir,
    credentials_lock_dir,
    proper_lockfile,
)
from claude_rotater.exceptions import ClaudeCodeLockTimeout


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "target.lock"


class TestProperLockfile:
    def test_acquire_creates_and_release_removes(self, lock_dir):
        with proper_lockfile(lock_dir):
            assert lock_dir.is_dir()
        assert not lock_dir.exists()

    def test_reacquire_after_release(self, lock_dir):
        with proper_lockfile(lock_dir):
            pass
        with proper_lockfile(lock_dir):
            assert lock_dir.is_dir()

    def test_contention_times_out(self, lock_dir):
        lock_dir.mkdir()  # fresh mtime = live holder
        start = time.monotonic()
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=0.5):
                pass
        assert time.monotonic() - start < 5.0
        assert lock_dir.is_dir()  # the holder's lock is left alone

    def test_stale_lock_is_taken_over(self, lock_dir):
        lock_dir.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        with proper_lockfile(lock_dir, timeout=2.0):
            assert lock_dir.is_dir()
            # We own it now: mtime is fresh, not the 30s-old corpse.
            assert time.time() - lock_dir.stat().st_mtime < 5.0
        assert not lock_dir.exists()

    def test_release_tolerates_stolen_lock(self, lock_dir):
        with proper_lockfile(lock_dir):
            os.rmdir(lock_dir)  # simulate a stale-takeover by another process
        # No exception; nothing left behind.
        assert not lock_dir.exists()

    def test_toucher_keeps_mtime_fresh(self, lock_dir, monkeypatch):
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.1)
        with proper_lockfile(lock_dir):
            past = time.time() - 30
            os.utime(lock_dir, (past, past))
            # Poll rather than sleep a fixed span: a loaded machine can hold
            # the toucher thread off for longer than a few intervals.
            deadline = time.monotonic() + 10
            while (time.time() - lock_dir.stat().st_mtime >= 10.0
                   and time.monotonic() < deadline):
                time.sleep(0.05)
            assert time.time() - lock_dir.stat().st_mtime < 10.0

    def test_creates_missing_parent(self, tmp_path):
        nested = tmp_path / "a" / "b" / "target.lock"
        with proper_lockfile(nested):
            assert nested.is_dir()


class TestLockPaths:
    def test_default_paths(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert credentials_lock_dir() == temp_home / ".claude.lock"
        assert config_lock_dir() == temp_home / ".claude.json.lock"

    def test_claude_config_dir_is_honored(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom-claude"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert credentials_lock_dir() == tmp_path / "custom-claude.lock"
        # ~/.claude.json resolves relative to CLAUDE_CONFIG_DIR too.
        assert config_lock_dir() == custom / ".claude.json.lock"

    def test_named_helpers_lock_their_dirs(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with claude_credentials_lock():
            assert (temp_home / ".claude.lock").is_dir()
            with claude_config_lock():
                assert (temp_home / ".claude.json.lock").is_dir()
        assert not (temp_home / ".claude.lock").exists()
        assert not (temp_home / ".claude.json.lock").exists()


class TestCcRefreshLockProtocol:
    """Claude Code 2.1.218 guards its OAuth refresh with TWO locks —
    ``<config-home>/.oauth_refresh.lock`` (primary) then the legacy
    ``<config-home>.lock`` — both at a 60s staleness. rota must follow the
    same protocol or mutual exclusion silently fails (extracted from the
    2.1.218 bundle: ``uKi``/``CKi``, ``stale: 60000, update: 5000``)."""

    def test_oauth_refresh_lock_dir_default(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert (
            claude_locks.oauth_refresh_lock_dir()
            == temp_home / ".claude" / ".oauth_refresh.lock"
        )

    def test_oauth_refresh_lock_dir_honors_claude_config_dir(
        self, tmp_path, monkeypatch
    ):
        custom = tmp_path / "custom-claude"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert claude_locks.oauth_refresh_lock_dir() == custom / ".oauth_refresh.lock"

    def test_credentials_lock_takes_both_locks(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        legacy = temp_home / ".claude.lock"
        with claude_credentials_lock():
            assert new.is_dir(), "primary .oauth_refresh.lock not held"
            assert legacy.is_dir(), "legacy .claude.lock not held"
        assert not new.exists()
        assert not legacy.exists()

    def test_primary_contention_never_touches_legacy(self, temp_home, monkeypatch):
        """CC's order: primary first. If the primary is held we must time out
        without ever creating the legacy lock."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        new.mkdir(parents=True)  # fresh mtime = live CC holding its refresh lock
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert not (temp_home / ".claude.lock").exists()
        assert new.is_dir()  # holder's lock untouched

    def test_legacy_contention_releases_primary(self, temp_home, monkeypatch):
        """If the legacy lock is contended after the primary was acquired,
        the primary must not be left behind (CC releases its new lock on
        legacy ELOCKED)."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        legacy = temp_home / ".claude.lock"
        legacy.mkdir()  # fresh = held
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert not (temp_home / ".claude" / ".oauth_refresh.lock").exists()
        assert legacy.is_dir()

    def test_credentials_staleness_is_60s_not_10s(self, temp_home, monkeypatch):
        """A 30s-old credential lock belongs to a live CC (its budget is 60s)
        and must NOT be stolen — the old 10s staleness stole it."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        new.mkdir(parents=True)
        past = time.time() - 30
        os.utime(new, (past, past))
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert new.is_dir()

    def test_credentials_lock_stale_past_60s_is_taken_over(
        self, temp_home, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        legacy = temp_home / ".claude.lock"
        new.mkdir(parents=True)
        legacy.mkdir()
        past = time.time() - 70
        os.utime(new, (past, past))
        os.utime(legacy, (past, past))
        with claude_credentials_lock(timeout=2.0):
            assert new.is_dir()
            assert legacy.is_dir()
        assert not new.exists()
        assert not legacy.exists()

    def test_config_lock_staleness_stays_10s(self, temp_home, monkeypatch):
        """The config lock's CC-side defaults are unchanged — a 30s-old
        config lock is still stale and taken over."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        cfg = temp_home / ".claude.json.lock"
        cfg.mkdir()
        past = time.time() - 30
        os.utime(cfg, (past, past))
        with claude_config_lock(timeout=2.0):
            assert cfg.is_dir()
        assert not cfg.exists()


class TestStorageWriteLock:
    """claude 2.1.302 wraps every credential write in proper-lockfile's
    ``lock(join(dir, ".storage-write"), {stale: 15000, retries: 10})``: the
    directory ``<dir>/.storage-write.lock``, stale past 15s, where ``dir`` is
    the secure-storage config dir (``zx``)."""

    @pytest.fixture(autouse=True)
    def _plain_env(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

    def test_dir_default_profile(self, temp_home):
        assert (
            claude_locks.storage_write_lock_dir()
            == temp_home / ".claude" / ".storage-write.lock"
        )

    def test_dir_follows_claude_config_dir(self, temp_home, monkeypatch):
        custom = temp_home / "work-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert claude_locks.storage_write_lock_dir() == custom / ".storage-write.lock"

    def test_dir_prefers_securestorage_override(self, temp_home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / "a"))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(temp_home / "b"))
        assert (
            claude_locks.storage_write_lock_dir()
            == temp_home / "b" / ".storage-write.lock"
        )

    def test_empty_securestorage_override_means_default_dir(
        self, temp_home, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / "a"))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
        assert (
            claude_locks.storage_write_lock_dir()
            == temp_home / ".claude" / ".storage-write.lock"
        )

    def test_held_while_inside_and_removed_after(self, temp_home):
        lock = claude_locks.storage_write_lock_dir()
        with claude_locks.claude_storage_write_lock():
            assert lock.is_dir()
        assert not lock.exists()

    def test_creates_a_missing_config_dir(self, temp_home, monkeypatch):
        """claude mkdirs the dir before locking; so must we."""
        custom = temp_home / "not-yet"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        with claude_locks.claude_storage_write_lock():
            assert (custom / ".storage-write.lock").is_dir()

    def test_fresh_holder_times_out_and_is_left_alone(self, temp_home):
        lock = claude_locks.storage_write_lock_dir()
        lock.mkdir()  # fresh mtime: a live claude mid-write
        ran = False
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_locks.claude_storage_write_lock(timeout=0.5):
                ran = True
        assert not ran
        assert lock.is_dir()

    def test_ten_seconds_old_is_still_live(self, temp_home):
        """Stale is 15s, not the config lock's 10s: a 12s-old lock may belong
        to a claude whose 7.5s toucher is merely late."""
        lock = claude_locks.storage_write_lock_dir()
        lock.mkdir()
        past = time.time() - 12
        os.utime(lock, (past, past))
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_locks.claude_storage_write_lock(timeout=0.5):
                pass
        assert lock.is_dir()

    def test_past_fifteen_seconds_is_taken_over(self, temp_home):
        lock = claude_locks.storage_write_lock_dir()
        lock.mkdir()
        past = time.time() - 20
        os.utime(lock, (past, past))
        with claude_locks.claude_storage_write_lock(timeout=2.0):
            assert time.time() - lock.stat().st_mtime < 5.0
        assert not lock.exists()

    def test_waits_for_a_brief_holder(self, temp_home):
        import threading

        lock = claude_locks.storage_write_lock_dir()
        lock.mkdir()
        threading.Timer(0.4, lock.rmdir).start()
        start = time.monotonic()
        with claude_locks.claude_storage_write_lock(timeout=3.0):
            assert lock.is_dir()
        assert time.monotonic() - start >= 0.3

    def test_reentrant_in_one_thread(self, temp_home):
        lock = claude_locks.storage_write_lock_dir()
        with claude_locks.claude_storage_write_lock():
            with claude_locks.claude_storage_write_lock(timeout=0.2):
                assert lock.is_dir()
            assert lock.is_dir(), "inner exit must not release the outer hold"
        assert not lock.exists()

    def test_another_thread_still_waits(self, temp_home):
        import threading

        outcome = []

        def other():
            try:
                with claude_locks.claude_storage_write_lock(timeout=0.3):
                    outcome.append("acquired")
            except ClaudeCodeLockTimeout:
                outcome.append("timeout")

        with claude_locks.claude_storage_write_lock():
            t = threading.Thread(target=other)
            t.start()
            t.join()
        assert outcome == ["timeout"]

    def test_per_profile(self, temp_home, monkeypatch):
        """Two profiles never serialize on each other's lock."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / "p1"))
        (temp_home / "p1" / ".storage-write.lock").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / "p2"))
        with claude_locks.claude_storage_write_lock(timeout=0.3):
            assert (temp_home / "p2" / ".storage-write.lock").is_dir()


def _win_error(winerror: int) -> PermissionError:
    e = PermissionError(13, "Access is denied")
    e.winerror = winerror
    return e


def _fault(monkeypatch, call: str, path: Path, *errors, forever=None) -> list:
    """Make ``os.<call>`` on ``path`` raise ``errors`` in turn (None = run it),
    then ``forever`` if given, else behave. Returns the calls made."""
    real = getattr(os, call)
    pending = list(errors)
    calls = []

    def faulty(target, *args, **kwargs):
        if target == path:
            calls.append(target)
            error = pending.pop(0) if pending else forever
            if error is not None:
                raise error
        return real(target, *args, **kwargs)

    monkeypatch.setattr(os, call, faulty)
    return calls


@pytest.fixture
def platform(monkeypatch):
    """Set the platform the Windows-error check sees, and only that."""
    def use(name: str) -> None:
        monkeypatch.setattr(fsutil, "sys", SimpleNamespace(platform=name))

    return use


@pytest.fixture
def clock(monkeypatch):
    """A clock for the acquire loop that only moves when it sleeps."""
    fake = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        fake.sleeps.append(seconds)
        fake.now += seconds
        assert len(fake.sleeps) < 1000, "the wait never ended"

    monkeypatch.setattr(
        claude_locks,
        "time",
        SimpleNamespace(monotonic=lambda: fake.now, time=time.time, sleep=sleep),
    )
    return fake


class TestWindowsAcquireRace:
    """A holder's rmdir leaves the directory delete-pending until the last
    handle on it closes; mkdir and stat get WinError 5 until then."""

    @pytest.fixture(autouse=True)
    def _windows(self, platform):
        platform("win32")

    @pytest.mark.parametrize("winerror", [5, 32, 33])
    def test_mkdir_refusal_is_waited_out(self, lock_dir, monkeypatch, clock, winerror):
        error = _win_error(winerror)
        calls = _fault(monkeypatch, "mkdir", lock_dir, error, error, error)
        with proper_lockfile(lock_dir, timeout=3.0):
            assert lock_dir.is_dir()
        assert len(calls) == 4
        assert len(clock.sleeps) == 3
        assert all(0.25 <= s <= 0.5 for s in clock.sleeps)
        assert not lock_dir.exists()

    @pytest.mark.parametrize("winerror", [5, 32, 33])
    def test_stat_refusal_is_waited_out(self, lock_dir, monkeypatch, clock, winerror):
        _fault(monkeypatch, "mkdir", lock_dir, FileExistsError())
        _fault(monkeypatch, "stat", lock_dir, _win_error(winerror))
        with proper_lockfile(lock_dir, timeout=3.0):
            pass
        assert len(clock.sleeps) == 1
        assert 0.25 <= clock.sleeps[0] <= 0.5

    def test_persistent_mkdir_refusal_surfaces_itself(
        self, lock_dir, monkeypatch, clock
    ):
        first = _win_error(5)
        calls = _fault(monkeypatch, "mkdir", lock_dir, first, forever=_win_error(32))
        with pytest.raises(PermissionError) as caught:
            with proper_lockfile(lock_dir, timeout=3.0):
                pass
        assert caught.value is first
        assert 3.0 < clock.now <= 3.5
        assert len(calls) <= 14, "spun instead of backing off"

    def test_persistent_stat_refusal_surfaces_itself(
        self, lock_dir, monkeypatch, clock
    ):
        lock_dir.mkdir()
        error = _win_error(5)
        calls = _fault(monkeypatch, "stat", lock_dir, forever=error)
        with pytest.raises(PermissionError) as caught:
            with proper_lockfile(lock_dir, timeout=3.0):
                pass
        assert caught.value is error
        assert 3.0 < clock.now <= 3.5
        assert len(calls) <= 14, "spun instead of backing off"

    def test_refusal_after_a_seen_holder_is_a_timeout(
        self, lock_dir, monkeypatch, clock
    ):
        lock_dir.mkdir()
        _fault(monkeypatch, "mkdir", lock_dir, None, forever=_win_error(5))
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=3.0):
                pass

    def test_refusal_before_a_seen_holder_is_a_timeout(
        self, lock_dir, monkeypatch, clock
    ):
        lock_dir.mkdir()
        _fault(monkeypatch, "mkdir", lock_dir, _win_error(5))
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=3.0):
                pass

    def test_refusal_after_a_holder_left_is_a_timeout(
        self, lock_dir, monkeypatch, clock
    ):
        _fault(monkeypatch, "mkdir", lock_dir, FileExistsError(), forever=_win_error(5))
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=3.0):
                pass

    def test_refusal_while_retaking_a_stale_lock(self, lock_dir, monkeypatch, clock):
        lock_dir.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        _fault(monkeypatch, "mkdir", lock_dir, None, _win_error(5))
        with proper_lockfile(lock_dir, timeout=3.0):
            assert lock_dir.is_dir()
        assert len(clock.sleeps) == 1

    @pytest.mark.parametrize("call", ["mkdir", "stat"])
    def test_other_errors_are_not_waited_on(self, lock_dir, monkeypatch, clock, call):
        if call == "stat":
            lock_dir.mkdir()
        error = _win_error(1314)  # ERROR_PRIVILEGE_NOT_HELD
        _fault(monkeypatch, call, lock_dir, forever=error)
        with pytest.raises(PermissionError) as caught:
            with proper_lockfile(lock_dir, timeout=3.0):
                pass
        assert caught.value is error
        assert clock.sleeps == []

    @pytest.mark.parametrize("call", ["mkdir", "stat"])
    def test_posix_refusal_surfaces_at_once(
        self, lock_dir, monkeypatch, clock, platform, call
    ):
        platform("linux")
        if call == "stat":
            lock_dir.mkdir()
        error = _win_error(5)
        calls = _fault(monkeypatch, call, lock_dir, forever=error)
        with pytest.raises(PermissionError) as caught:
            with proper_lockfile(lock_dir, timeout=3.0):
                pass
        assert caught.value is error
        assert len(calls) == 1
        assert clock.sleeps == []

    def test_storage_write_lock_waits_it_out(self, temp_home, monkeypatch, clock):
        lock = claude_locks.storage_write_lock_dir()
        _fault(monkeypatch, "mkdir", lock, _win_error(5))
        with claude_locks.claude_storage_write_lock(timeout=3.0):
            assert lock.is_dir()

    def test_refused_legacy_lock_releases_the_primary(
        self, temp_home, monkeypatch, clock
    ):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        primary = claude_locks.oauth_refresh_lock_dir()
        _fault(monkeypatch, "mkdir", credentials_lock_dir(), forever=_win_error(5))
        with pytest.raises(PermissionError):
            with claude_credentials_lock(timeout=1.0):
                pass
        assert not primary.exists()


class TestWindowsReleaseRace:
    @pytest.fixture
    def warnings(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            claude_locks._logger, "warning", lambda *args: seen.append(args)
        )
        return seen

    @pytest.mark.parametrize("winerror", [5, 32, 33])
    def test_refused_rmdir_is_retried(
        self, lock_dir, monkeypatch, platform, warnings, winerror
    ):
        platform("win32")
        error = _win_error(winerror)
        with proper_lockfile(lock_dir):
            calls = _fault(monkeypatch, "rmdir", lock_dir, error, error, error)
        assert len(calls) == 4
        assert not lock_dir.exists()
        assert warnings == []

    def test_persistent_refusal_warns_after_bounded_retries(
        self, lock_dir, monkeypatch, platform, warnings
    ):
        platform("win32")
        with proper_lockfile(lock_dir):
            calls = _fault(monkeypatch, "rmdir", lock_dir, forever=_win_error(5))
        assert len(calls) == 10
        assert len(warnings) == 1

    def test_posix_release_tries_once(
        self, lock_dir, monkeypatch, platform, warnings
    ):
        platform("linux")
        with proper_lockfile(lock_dir):
            calls = _fault(monkeypatch, "rmdir", lock_dir, forever=_win_error(5))
        assert len(calls) == 1
        assert len(warnings) == 1


class TestWindowsTouchRace:
    @pytest.fixture(autouse=True)
    def _fast_touch(self, monkeypatch):
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.01)

    @staticmethod
    def _wait_for(condition) -> bool:
        deadline = time.monotonic() + 10
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
        return condition()

    def test_refused_touch_keeps_touching(self, lock_dir, monkeypatch, platform):
        """Giving up would let a live hold go stale and be stolen."""
        platform("win32")
        error = _win_error(5)
        calls = _fault(monkeypatch, "utime", lock_dir, error, error)
        with proper_lockfile(lock_dir):
            assert self._wait_for(lambda: len(calls) >= 4)

    @pytest.mark.parametrize(
        ("name", "error"),
        [("linux", _win_error(5)), ("win32", FileNotFoundError())],
    )
    def test_real_failure_stops_touching(
        self, lock_dir, monkeypatch, platform, name, error
    ):
        platform(name)
        calls = _fault(monkeypatch, "utime", lock_dir, forever=error)
        with proper_lockfile(lock_dir):
            assert self._wait_for(lambda: len(calls) >= 1)
            time.sleep(0.2)
            assert len(calls) == 1
