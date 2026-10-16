"""The feed watcher wakes on appends, creation and rotation, and stops at once."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from claude_swap import feed
from claude_swap.feed_watch import FeedWatcher, default_backend

BACKENDS = [default_backend(), "poll"]
if default_backend() == "poll":
    BACKENDS = ["poll"]


class Counter:
    def __init__(self):
        self.n = 0
        self.event = threading.Event()

    def __call__(self):
        self.n += 1
        self.event.set()

    def wait(self, timeout=3.0) -> bool:
        ok = self.event.wait(timeout)
        self.event.clear()
        return ok


@pytest.fixture(params=BACKENDS)
def watch(request, tmp_path):
    directory = str(tmp_path / "cache" / "feed")
    hits = Counter()
    watcher = FeedWatcher(
        directory, feed.FEED_FILENAME, hits, backend=request.param, poll_s=0.05
    )
    watcher.start()
    assert watcher.ready.wait(3.0)
    yield watcher, hits, os.path.join(directory, feed.FEED_FILENAME)
    watcher.stop()


def append(path, i=0):
    feed.append_record(path, {"t": float(i), "five_hour": {"pct": 1.0}})


def test_creates_the_directory_private(watch):
    watcher, _, _ = watch
    assert os.path.isdir(watcher.directory)
    if sys.platform != "win32":
        assert os.stat(watcher.directory).st_mode & 0o777 == 0o700


def test_file_creation_then_appends(watch):
    _, hits, path = watch
    append(path)
    assert hits.wait()
    for i in range(3):
        time.sleep(0.1)
        append(path, i)
        assert hits.wait(), f"append {i} not seen"


def test_follows_rotation(watch):
    _, hits, path = watch
    append(path)
    assert hits.wait()
    os.replace(path, path + ".1")
    hits.wait(0.5)
    time.sleep(0.1)
    append(path, 1)  # the new file
    assert hits.wait()
    time.sleep(0.1)
    append(path, 2)
    assert hits.wait()


def test_idle_watcher_does_not_call_back(watch):
    watcher, hits, _ = watch
    if watcher.backend == "poll":
        pytest.skip("polling wakes by design; it just finds nothing")
    assert not hits.wait(0.3)


def test_stop_is_prompt_and_idempotent(tmp_path):
    watcher = FeedWatcher(str(tmp_path / "f"), feed.FEED_FILENAME, lambda: None)
    watcher.start()
    assert watcher.ready.wait(3.0)
    t0 = time.monotonic()
    watcher.stop()
    assert time.monotonic() - t0 < 1.0
    watcher.stop()


def test_callback_errors_do_not_kill_the_thread(tmp_path):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    directory = str(tmp_path / "f")
    watcher = FeedWatcher(directory, feed.FEED_FILENAME, flaky, backend="poll", poll_s=0.05)
    watcher.start()
    path = os.path.join(directory, feed.FEED_FILENAME)
    append(path)
    deadline = time.monotonic() + 3
    while len(calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    append(path, 1)
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    watcher.stop()
    assert len(calls) >= 2


def test_broken_backend_falls_back_to_polling(tmp_path, monkeypatch):
    hits = Counter()
    directory = str(tmp_path / "f")
    watcher = FeedWatcher(directory, feed.FEED_FILENAME, hits, backend="kqueue", poll_s=0.05)

    def broken():
        raise OSError("no kqueue here")

    monkeypatch.setattr(watcher, "_run_kqueue", broken)
    watcher.start()
    assert watcher.ready.wait(3.0)
    assert watcher.backend == "poll"
    append(os.path.join(directory, feed.FEED_FILENAME))
    assert hits.wait()
    watcher.stop()


def test_unwritable_parent_disables_the_watch(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    watcher = FeedWatcher(str(blocker / "feed"), feed.FEED_FILENAME, lambda: None)
    watcher.start()  # logs and returns
    assert watcher._thread is None
    watcher.stop()
