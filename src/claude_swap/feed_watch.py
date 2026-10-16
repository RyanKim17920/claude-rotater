"""Event-driven watch on the statusline feed directory.

A daemon thread blocks in the kernel until the feed changes, then calls
``on_change``; an idle watcher costs no wakeups at all. Backends, best first:

- **kqueue** (macOS, BSD): ``EVFILT_VNODE`` on the directory (a file
  created, rotated or replaced) and on the feed file itself (appended).
- **inotify** (Linux), through libc with ``ctypes``: one watch on the
  directory reports writes to, and renames into, any file in it.
- **stat polling** (Windows, or any platform where the above fail): the one
  backend that wakes periodically, every ``POLL_S``.

The feed has its own directory (``<backup>/cache/feed``) so the kernel
backends see nothing but feed traffic. ``stop()`` wakes the blocked thread
through a self-pipe (an Event for polling), so shutdown is immediate.
"""

from __future__ import annotations

import logging
import os
import select
import sys
import threading
from collections.abc import Callable

_logger = logging.getLogger("claude-swap")

POLL_S = 5.0

# inotify(7)
_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_NONBLOCK = 0o4000
_IN_CLOEXEC = 0o2000000


def default_backend() -> str:
    if hasattr(select, "kqueue"):
        return "kqueue"
    if sys.platform.startswith("linux"):
        return "inotify"
    return "poll"


class FeedWatcher:
    """Calls ``on_change`` (from its own thread) whenever ``filename`` in
    ``directory`` may have changed. Spurious calls are possible and harmless:
    the reader keeps a cursor and finds nothing new."""

    def __init__(
        self,
        directory: str,
        filename: str,
        on_change: Callable[[], None],
        *,
        backend: str | None = None,
        poll_s: float = POLL_S,
    ):
        self.directory = directory
        self.path = os.path.join(directory, filename)
        self.on_change = on_change
        self.backend = backend or default_backend()
        self.poll_s = poll_s
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._pipe: tuple[int, int] | None = None
        self.ready = threading.Event()  # set once the watch is armed

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
        except OSError as e:
            _logger.warning(f"Statusline feed watch disabled: {e!r}")
            return
        if self.backend != "poll":
            self._pipe = os.pipe()
        self._thread = threading.Thread(
            target=self._run, name="cswap-feed-watch", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopped.set()
        if self._pipe is not None:
            try:
                os.write(self._pipe[1], b"x")
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if self._pipe is not None:
            for fd in self._pipe:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._pipe = None

    # -- thread ---------------------------------------------------------------

    def _run(self) -> None:
        runners = {"kqueue": self._run_kqueue, "inotify": self._run_inotify}
        runner = runners.get(self.backend)
        if runner is not None:
            try:
                runner()
                return
            except Exception as e:
                if self._stopped.is_set():
                    return
                _logger.warning(
                    f"Statusline feed {self.backend} watch failed ({e!r}); polling"
                )
                self.backend = "poll"
        self._run_poll()

    def _notify(self) -> None:
        try:
            self.on_change()
        except Exception as e:
            _logger.warning(f"Statusline feed callback failed: {e!r}")

    def _run_kqueue(self) -> None:
        evtonly = getattr(os, "O_EVTONLY", os.O_RDONLY)
        kq = select.kqueue()
        dir_fd = os.open(self.directory, evtonly)
        file_fd = -1
        try:
            def file_event(fd: int):
                return select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
                )

            def open_file() -> int:
                try:
                    fd = os.open(self.path, evtonly)
                except OSError:
                    return -1
                kq.control([file_event(fd)], 0, 0)
                return fd

            kq.control(
                [
                    select.kevent(
                        dir_fd,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_WRITE,
                    ),
                    select.kevent(
                        self._pipe[0],
                        filter=select.KQ_FILTER_READ,
                        flags=select.KQ_EV_ADD,
                    ),
                ],
                0,
                0,
            )
            file_fd = open_file()
            self.ready.set()
            while not self._stopped.is_set():
                events = kq.control(None, 8, None)  # blocks: no idle wakeups
                if self._stopped.is_set():
                    return
                reopen = False
                for ev in events:
                    if ev.ident == self._pipe[0]:
                        return
                    if ev.ident == dir_fd or ev.fflags & (
                        select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
                    ):
                        reopen = True
                if reopen:
                    # Created, rotated or replaced: follow the new file (a
                    # closed fd drops its kevents).
                    if file_fd >= 0:
                        os.close(file_fd)
                    file_fd = open_file()
                self._notify()
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(dir_fd)
            kq.close()

    def _run_inotify(self) -> None:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        try:
            mask = _IN_MODIFY | _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE
            if libc.inotify_add_watch(fd, os.fsencode(self.directory), mask) < 0:
                raise OSError(ctypes.get_errno(), "inotify_add_watch")
            self.ready.set()
            while not self._stopped.is_set():
                readable, _, _ = select.select([fd, self._pipe[0]], [], [])
                if self._stopped.is_set() or self._pipe[0] in readable:
                    return
                try:
                    while os.read(fd, 4096):  # drain; any event means "look"
                        pass
                except BlockingIOError:
                    pass
                self._notify()
        finally:
            os.close(fd)

    def _stat(self) -> tuple | None:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _run_poll(self) -> None:
        last = self._stat()
        self.ready.set()
        while not self._stopped.wait(self.poll_s):
            current = self._stat()
            if current != last:
                last = current
                self._notify()
