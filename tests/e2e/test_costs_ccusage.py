"""The costs slicer against the real ccusage (opt in: ``ROTA_E2E=1``).

Made-up transcripts (no real data), priced three ways by the real ccusage:
the original lines, rota's trimmed per-slot copy, and rota's incremental
refresh. All three must agree, which is what lets rota hand ccusage a slice
instead of the transcripts. Needs ``ccusage``, ``bunx`` or ``npx`` on PATH;
bunx/npx reuse the owner's package cache, so only a first run downloads.
"""

from __future__ import annotations

import json
import os
import pwd
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_rotater import costs


@pytest.fixture
def runner(monkeypatch):
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    # The parent conftest swaps HOME; point the package caches back at the
    # owner's so bunx/npx do not fetch ccusage again for every test.
    monkeypatch.setenv("BUN_INSTALL_CACHE_DIR", str(real_home / ".bun" / "install" / "cache"))
    monkeypatch.setenv("npm_config_cache", str(real_home / ".npm"))
    found = costs.find_runner()
    if found is None:
        pytest.skip("no ccusage, bunx or npx on PATH")
    return found


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def line(t: float, n: int, model: str, out: int) -> str:
    return json.dumps({
        "parentUuid": None, "isSidechain": False, "type": "assistant", "timestamp": iso(t),
        "sessionId": "s", "requestId": f"req_e2e_{n}", "cwd": "/tmp/x", "version": "2.1.0",
        "message": {
            "id": f"msg_e2e_{n}", "model": model, "role": "assistant", "type": "message",
            "content": [{"type": "text", "text": "an answer " * 30}],
            "usage": {"input_tokens": 5 + n, "output_tokens": out,
                      "cache_creation_input_tokens": 2000 + n,
                      "cache_read_input_tokens": 40000 + 10 * n,
                      "cache_creation": {"ephemeral_1h_input_tokens": 2000 + n,
                                         "ephemeral_5m_input_tokens": 0},
                      "service_tier": "standard"},
        },
    }, separators=(",", ":")) + "\n"  # compact, as Claude Code writes: ccusage expects it


def transcripts(home: Path, now: float, start: int, count: int) -> None:
    models = ("claude-opus-4-1-20250805", "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001")
    for n in range(start, start + count):
        t = now - (count - (n - start)) * 1700
        path = home / "projects" / f"-proj-{n % 3}" / f"session-{n % 4}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            entry = line(t, n, models[n % 3], 100 + 7 * n)
            fh.write(entry)
            fh.write(entry)  # a streamed repeat, deduplicated by ccusage


def test_trimmed_slots_price_exactly_like_the_transcripts(tmp_path, runner):
    home = tmp_path / ".claude"
    now = time.time()
    transcripts(home, now, 0, 60)
    original = costs.run_ccusage(runner, home)
    sources = costs.transcript_sources(tmp_path / ".rota", [home])
    cache = costs.refresh(None, sources, runner, now)
    want = sum(s["totalCost"] for s in original["sessions"])
    assert want > 0
    assert sum(r.cost for r in cache.rows) == pytest.approx(want, rel=1e-9)
    assert sum(r.output for r in cache.rows) == sum(s["outputTokens"] for s in original["sessions"])
    assert {r.model for r in cache.rows} == {
        "claude-opus-4-1-20250805", "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"}
    assert all(r.slot % costs.SLOT_S == 0 for r in cache.rows)


def test_an_incremental_refresh_matches_a_full_one(tmp_path, runner):
    home = tmp_path / ".claude"
    now = time.time()
    transcripts(home, now - 600, 0, 40)
    sources = costs.transcript_sources(tmp_path / ".rota", [home])
    first = costs.refresh(None, sources, runner, now - 300)
    transcripts(home, now, 40, 10)
    assert not costs.is_current(first, sources, now)
    later = costs.refresh(first, sources, runner, now)
    full = costs.refresh(None, sources, runner, now)
    assert [r.to_json() for r in later.rows] == [r.to_json() for r in full.rows]
    assert costs.is_current(later, sources, now)
