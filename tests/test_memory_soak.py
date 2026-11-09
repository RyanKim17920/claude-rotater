"""Memory stays bounded while the engine and the TUI run for many hours.

The real ``AutoSwitchEngine`` and ``RotaApp`` run headless against a temp
``ROTA_HOME`` and the in-memory Keychain fake; the usage endpoint is a stub
and every other network call is refused. Wall clock is shifted forward one
simulated hour per round (``time.time`` plus an offset), so the engine,
usage store, usage history and feed see hours pass while Textual's monotonic
timers run normally. Each hour brings fresh usage readings and a burst of
statusline records from new sessions, the way a working day does.

Python heap growth (tracemalloc) is measured after a warm-up, once the
time-bounded stores (8 days of usage history, feed session memory) would
have had to start pruning for a leak to show. ``ROTA_SOAK_HOURS`` makes the
run longer by hand and ``ROTA_SOAK_FRAMES`` (e.g. 12) prints where the
growth was allocated.
"""

from __future__ import annotations

import gc
import os
import subprocess
import time
import tracemalloc
import urllib.request

import pytest

from claude_rotater import feed
from tests.test_tui_engine_pilot import feed_record, until, world  # noqa: F401

H = 3600.0
WARMUP_HOURS = 12
HOURS = int(os.environ.get("ROTA_SOAK_HOURS", "48"))
FRAMES = int(os.environ.get("ROTA_SOAK_FRAMES", "1"))
SAMPLE_EVERY = 6  # simulated hours between heap snapshots (a snapshot is slow)
RECORDS_PER_HOUR = 40
# Bytes of Python heap the run may gain per simulated hour after warm-up.
# Rich's and Textual's bounded render caches (lru_cache(4096) and the like)
# still fill for a while; a leak of render state, of snapshots or of every
# feed record is far above this.
MAX_GROWTH_PER_HOUR = 64 * 1024

# The test's own mocks record every call they get; that is the harness, not rota.
_NOT_ROTA = (
    tracemalloc.Filter(False, "*/unittest/mock.py"),
    tracemalloc.Filter(False, tracemalloc.__file__),
)


def _heap(snapshot: tracemalloc.Snapshot) -> int:
    return sum(s.size for s in snapshot.statistics("filename"))


def _rss_mb() -> int:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, timeout=5
    )
    return int(out.stdout.strip() or 0) // 1024


@pytest.mark.asyncio
async def test_engine_and_tui_memory_bounded_over_simulated_hours(world, monkeypatch):  # noqa: F811
    from claude_rotater.tui.app import RotaApp

    h, usage = world
    home = h.temp_home
    real_time = time.time
    offset = [0.0]
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])

    def no_network(*_a, **_k):
        raise OSError("network disabled in the soak test")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr("claude_rotater.costs.find_runner", lambda *a, **k: None)
    path = feed.feed_path(h.switcher.backup_dir)

    def hour(n: int) -> None:
        now = time.time()
        r5 = now + (5 - n % 5) * H
        r7 = now + (7 * 24 - n % (7 * 24)) * H
        pct5 = float(n % 5) * 18.0
        pct7 = float(n % (7 * 24)) * 0.5
        usage.set(("sk-live", "sk-1"), pct5, pct7, r5, r7)
        usage.set(("sk-2",), pct5 / 2, pct7 / 2, r5, r7)
        for i in range(RECORDS_PER_HOUR):
            record = feed_record(home, pct5, pct7, r5, r7, now + i)
            record["session_id"] = f"s-{n}-{i % 4}"
            feed.append_record(path, record)

    hour(0)
    app = RotaApp(h.switcher, detected="dark")
    samples: list[tuple[int, int, int]] = []
    first_snap = last_snap = None
    tracemalloc.start(FRAMES)
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            await until(pilot, lambda: app.snapshot is not None)
            for n in range(1, HOURS + 1):
                offset[0] += H
                hour(n)
                await pilot.pause(0.25)
                if n >= WARMUP_HOURS and (n - WARMUP_HOURS) % SAMPLE_EVERY == 0:
                    gc.collect()
                    last_snap = tracemalloc.take_snapshot().filter_traces(_NOT_ROTA)
                    first_snap = first_snap or last_snap
                    samples.append((n, _heap(last_snap), _rss_mb()))
    finally:
        tracemalloc.stop()

    (first_h, first_b, first_r), (last_h, last_b, last_r) = samples[0], samples[-1]
    per_hour = (last_b - first_b) / (last_h - first_h)
    key = "traceback" if FRAMES > 1 else "lineno"
    top = [
        "\n".join([str(s), *(s.traceback.format()[-2 * FRAMES:] if FRAMES > 1 else [])])
        for s in last_snap.compare_to(first_snap, key)[:5]
    ]
    print(
        f"\nsoak {last_h - first_h}h after {WARMUP_HOURS}h warm-up: heap {first_b / 1e6:.2f} -> "
        f"{last_b / 1e6:.2f} MB ({per_hour / 1024:+.1f} KB/h), rss {first_r} -> {last_r} MB\n"
        f"series (hour, heap MB, rss MB): {[(n, round(b / 1e6, 2), r) for n, b, r in samples]}\n"
        + "\n".join(top)
    )
    assert per_hour < MAX_GROWTH_PER_HOUR, top
