"""Costs data: the ccusage runner, slicing, cache, attribution, rollups, and
$ per 1% across profiles (window by window: tests/test_dollar_per_pct.py).

No network and no real ccusage: ``fake_ccusage`` prices a slice the way
ccusage lays it out (one session per ``projects/<source>/<slot>.jsonl``,
streamed repeats deduplicated by message id), at $1 per 1000 output tokens.
The real binary's contract is checked by ``tests/e2e/test_costs_ccusage.py``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_rotater import cost_report as cr
from claude_rotater import costs
from claude_rotater import dollar_per_pct as dpp
from claude_rotater.usage_history import Point, WindowSummary

FIXTURE = Path(__file__).parent / "fixtures" / "ccusage_session.json"
HOUR = 3600.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def entry(t: float, msg: str, *, model: str = "claude-opus-4-7", out: int = 1000, **extra) -> dict:
    return {
        "type": "assistant",
        "timestamp": iso(t),
        "requestId": f"req_{msg}",
        "sessionId": "abc",
        "cwd": "/somewhere",
        "message": {
            "id": msg,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "a long answer " * 20}],
            "usage": {"input_tokens": 3, "output_tokens": out,
                      "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100},
        },
        **extra,
    }


def write(path: Path, *records: dict, raw: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        fh.write(raw)
    return path


def fake_ccusage(calls: list | None = None):
    """A stand-in for :func:`costs.run_ccusage` over the slice it is given."""

    def run(runner, config_dir: Path) -> dict:
        if calls is not None:
            calls.append(config_dir)
        seen: set[str] = set()
        sessions = []
        for project in sorted((config_dir / "projects").iterdir()):
            for f in sorted(project.glob("*.jsonl")):
                per: dict[str, dict] = {}
                for line in f.read_text().splitlines():
                    rec = json.loads(line)
                    key = rec["message"]["id"] + rec.get("requestId", "")
                    if key in seen:
                        continue
                    seen.add(key)
                    usage = rec["message"]["usage"]
                    b = per.setdefault(rec["message"]["model"], {
                        "modelName": rec["message"]["model"], "cost": 0.0, "inputTokens": 0,
                        "outputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0})
                    b["cost"] += usage["output_tokens"] / 1000
                    b["inputTokens"] += usage["input_tokens"]
                    b["outputTokens"] += usage["output_tokens"]
                    b["cacheCreationTokens"] += usage["cache_creation_input_tokens"]
                    b["cacheReadTokens"] += usage["cache_read_input_tokens"]
                if per:
                    sessions.append({"projectPath": project.name, "sessionId": f.stem,
                                     "modelBreakdowns": list(per.values())})
        return {"sessions": sessions, "totals": {}}

    return run


@pytest.fixture
def now() -> float:
    """Mid-afternoon local time, so "today" has room on both sides."""
    d = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    return d.timestamp()


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / ".claude"


def source(home: Path) -> list[costs.Source]:
    (home / "projects").mkdir(parents=True, exist_ok=True)
    return [costs.Source("s0", os.path.realpath(home / "projects"))]


def touch(path: Path, t: float) -> None:
    os.utime(path, (t, t))


# -- runner ---------------------------------------------------------------------


class TestFindRunner:
    def test_installed_ccusage_first(self):
        found = {"ccusage": "/bin/ccusage", "bunx": "/bin/bunx", "npx": "/bin/npx"}
        assert costs.find_runner(found.get) == ["/bin/ccusage"]

    def test_bunx_then_npx_run_the_pinned_release(self):
        pinned = f"ccusage@{costs.CCUSAGE_VERSION}"
        assert costs.find_runner({"bunx": "/b", "npx": "/n"}.get) == ["/b", pinned]
        assert costs.find_runner({"npx": "/n"}.get) == ["/n", "-y", pinned]

    def test_none_without_node_or_bun(self):
        assert costs.find_runner(lambda _name: None) is None


def fake_binary(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake-ccusage"
    script.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return [str(script)]


class TestRunCcusage:
    def test_runs_the_claude_session_report_over_the_slice(self, tmp_path):
        runner = fake_binary(tmp_path, (
            "print(json.dumps({'argv': sys.argv[1:], 'dir': os.environ['CLAUDE_CONFIG_DIR'],"
            " 'cwd': os.getcwd(), 'sessions': []}))"
        ))
        out = costs.run_ccusage(runner, tmp_path)
        assert out["argv"] == ["claude", "session", "--json", "--breakdown", "--offline"]
        assert out["dir"] == str(tmp_path)
        assert os.path.realpath(out["cwd"]) == os.path.realpath(tmp_path)

    def test_an_older_ccusage_falls_back_to_plain_session(self, tmp_path):
        runner = fake_binary(tmp_path, (
            "if sys.argv[1] == 'claude': sys.exit('unknown command')\n"
            "print(json.dumps({'argv': sys.argv[1:]}))"
        ))
        assert costs.run_ccusage(runner, tmp_path)["argv"][0] == "session"

    def test_failure_names_the_error(self, tmp_path):
        runner = fake_binary(tmp_path, "sys.exit('boom: no pricing')")
        with pytest.raises(costs.CostError, match="boom: no pricing"):
            costs.run_ccusage(runner, tmp_path)

    def test_garbage_output_is_an_error(self, tmp_path):
        runner = fake_binary(tmp_path, "print('not json')")
        with pytest.raises(costs.CostError, match="no JSON"):
            costs.run_ccusage(runner, tmp_path)

    def test_a_missing_binary_is_an_error(self, tmp_path):
        with pytest.raises(costs.CostError, match="did not run"):
            costs.run_ccusage([str(tmp_path / "nope")], tmp_path)

    def test_timeout_is_an_error(self, tmp_path):
        runner = fake_binary(tmp_path, "import time; time.sleep(5)")
        with pytest.raises(costs.CostError, match="did not run"):
            costs.run_ccusage(runner, tmp_path, timeout=0.5)


class TestParseSessions:
    def test_reads_claude_models_of_our_slots_only(self):
        rows = costs.parse_sessions(json.loads(FIXTURE.read_text()), ["s0", "s1"])
        assert sorted((r.source, r.slot, r.model) for r in rows) == [
            ("s0", 1792336500, "claude-haiku-4-5-20251001"),
            ("s0", 1792336500, "claude-opus-4-7"),
            ("s1", 1792340400, "claude-sonnet-4-5-20250929"),
        ]
        opus = next(r for r in rows if r.model == "claude-opus-4-7")
        assert (opus.cost, opus.input, opus.output, opus.cache_write, opus.cache_read) == (
            1.25, 20, 900, 1000, 40000)

    def test_token_counts_sum_to_the_sessions_totals_as_ccusage_reports_them(self):
        payload = json.loads(FIXTURE.read_text())
        rows = costs.parse_sessions(payload, ["s0", "s1"])
        for session in payload["sessions"][:2]:
            mine = [r for r in rows if str(r.slot) == session["sessionId"]]
            assert [sum(r.tokens[i] for r in mine) for i in range(4)] == [
                session["inputTokens"], session["cacheCreationTokens"],
                session["cacheReadTokens"], session["outputTokens"]]
        opus = next(r for r in rows if r.model == "claude-opus-4-7")
        assert dict(zip(costs.TOKEN_KINDS, opus.tokens)) == {
            "input": 20, "cache write": 1000, "cache read": 40000, "output": 900}

    def test_unknown_sources_and_garbage_are_skipped(self):
        assert costs.parse_sessions(json.loads(FIXTURE.read_text()), ["s9"]) == []
        assert costs.parse_sessions({"sessions": "x"}, ["s0"]) == []
        assert costs.parse_sessions({"sessions": [1, {"projectPath": "s0", "sessionId": "1",
                                                      "modelBreakdowns": [None]}]}, ["s0"]) == []

    def test_row_json_round_trip_and_rejects(self):
        row = costs.Row("s0", 900, "claude-opus-4-7", 1.5, 1, 2, 3, 4)
        assert costs.Row.from_json(row.to_json()) == row
        assert costs.Row.from_json(["s0", 900, "m", "x", 1, 2, 3, 4]) is None
        assert costs.Row.from_json([1, 2]) is None


# -- sources ----------------------------------------------------------------------


class TestSources:
    def test_main_dir_then_session_profiles_with_their_account(self, tmp_path, home):
        backup = tmp_path / ".rota"
        (home / "projects").mkdir(parents=True)
        (backup / "sessions" / "2-bob-example-com" / "projects").mkdir(parents=True)
        (backup / "sessions" / "notanumber" / "projects").mkdir(parents=True)
        (backup / "sessions" / "3-carol" ).mkdir(parents=True)  # never ran: no projects
        out = costs.transcript_sources(backup, [home])
        assert [(s.key, s.account) for s in out] == [("s0", None), ("s1", "2")]
        assert out[1].path == os.path.realpath(backup / "sessions" / "2-bob-example-com" / "projects")

    def test_shared_history_is_read_once_as_the_main_dir(self, tmp_path, home):
        backup = tmp_path / ".rota"
        (home / "projects").mkdir(parents=True)
        profile = backup / "sessions" / "2-bob"
        profile.mkdir(parents=True)
        (profile / "projects").symlink_to(home / "projects")
        out = costs.transcript_sources(backup, [home, home])
        assert [(s.key, s.account) for s in out] == [("s0", None)]

    def test_missing_dirs_are_no_sources(self, tmp_path):
        assert costs.transcript_sources(tmp_path / ".rota", [tmp_path / "nope"]) == []

    def test_feed_names_extra_config_dirs_but_not_session_profiles(self, tmp_path):
        backup = tmp_path / ".rota"
        feed = backup / "cache" / "feed"
        feed.mkdir(parents=True)
        session = backup / "sessions" / "2-bob"
        (feed / "statusline.jsonl.1").write_text(json.dumps({"config_dir": "/a/.claude"}) + "\n")
        (feed / "statusline.jsonl").write_text(
            json.dumps({"config_dir": "/b/.claude-work"}) + "\n"
            + json.dumps({"config_dir": str(session)}) + "\n"
            + json.dumps({"config_dir": "/a/.claude"}) + "\n" + "{broken\n")
        assert costs.feed_config_dirs(backup) == ["/a/.claude", "/b/.claude-work"]
        assert costs.feed_config_dirs(tmp_path / "none") == []


# -- slicing -------------------------------------------------------------------------


class TestSlice:
    def test_copies_usage_records_in_range_into_slot_files(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl",
                  entry(now - 2 * 86400, "old"),
                  {"type": "user", "timestamp": iso(now - 60), "message": {"content": "hi"}},
                  entry(now - 1000, "m1"),
                  entry(now - 100, "m2", isSidechain=True))
        write(home / "projects" / "p" / "sub" / "subagents" / "agent.jsonl", entry(now - 50, "m3"))
        (home / "projects" / "p" / "notes.txt").write_text(json.dumps(entry(now, "x")))
        dst = tmp_path / "slice"
        result = costs.slice_transcripts(source(home), now - 86400, now + 86400, dst)
        assert result.records == 3
        files = sorted(p.name for p in (dst / "projects" / "s0").iterdir())
        assert files == sorted({f"{int(t // 900) * 900}.jsonl" for t in (now - 1000, now - 100, now - 50)})
        raw = [line for p in (dst / "projects" / "s0").iterdir() for line in p.read_text().splitlines()]
        assert all('": ' not in line and '", ' not in line for line in raw)  # compact, as ccusage needs
        copied = [json.loads(line) for line in raw]
        assert {c["message"]["id"] for c in copied} == {"m1", "m2", "m3"}
        for c in copied:  # only what ccusage reads
            assert set(c) <= set(costs._TOP_KEYS) | {"message"}
            assert set(c["message"]) == {"id", "model", "usage"}
            assert c["requestId"].startswith("req_")
        assert str(f) in result.offsets

    def test_old_files_are_not_opened(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 3 * 86400, "old"))
        touch(f, now - 3 * 86400)
        result = costs.slice_transcripts(source(home), now - 86400, now, tmp_path / "s")
        assert result.records == 0 and result.offsets == {}

    def test_a_line_still_being_written_waits(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 100, "m1"),
                  raw=json.dumps(entry(now - 50, "m2"))[:40])
        result = costs.slice_transcripts(source(home), now - 86400, now + 86400, tmp_path / "s")
        assert result.records == 1
        complete = len(json.dumps(entry(now - 100, "m1"))) + 1
        assert result.offsets[str(f)][1] == complete

    def test_offset_marks_where_the_next_day_begins(self, tmp_path, home, now):
        boundary = now - 500
        first = json.dumps(entry(now - 1000, "m1")) + "\n"
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 1000, "m1"), entry(now - 100, "m2"))
        result = costs.slice_transcripts(source(home), now - 86400, boundary, tmp_path / "s")
        ino, offset, size = result.offsets[str(f)]
        assert (ino, offset, size) == (f.stat().st_ino, len(first), f.stat().st_size)

    def test_resumes_from_a_valid_offset_and_rereads_a_replaced_file(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 1000, "m1"), entry(now - 100, "m2"))
        touch(f, now)  # written at the fixture's 15:00, not the wall clock's
        first = costs.slice_transcripts(source(home), now - 86400, now - 500, tmp_path / "a")
        resumed = costs.slice_transcripts(source(home), now - 500, now + 86400, tmp_path / "b",
                                          first.offsets)
        assert resumed.records == 1
        f.unlink()  # rewritten: new inode, so the offset no longer applies
        write(f, entry(now - 400, "n1"), entry(now - 300, "n2"))
        touch(f, now)
        reread = costs.slice_transcripts(source(home), now - 500, now + 86400, tmp_path / "c",
                                         {str(f): [first.offsets[str(f)][0] + 10**9, 10**6, 10**6]})
        assert reread.records == 2

    def test_bad_lines_are_skipped(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 100, "m1"),
              raw='{"usage": 1, "timestamp": "nope"}\n{"usage" "timestamp":"2026-10-18T10:00:00Z" broken\n'
                  '["usage", {"timestamp": "2026-10-18T10:00:00Z"}]\n')
        result = costs.slice_transcripts(source(home), now - 86400 * 400, now + 86400, tmp_path / "s")
        assert result.records == 1


# -- refresh and cache -------------------------------------------------------------------


class TestRefresh:
    def test_full_build_prices_every_slot_in_range(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl",
              entry(now - 40 * 86400, "ancient"), entry(now - 3 * 86400, "d3", out=2000),
              entry(now - 600, "t1"), entry(now - 590, "t1"),  # a streamed repeat
              entry(now - 60, "t2", model="claude-sonnet-4-5", out=500))
        cache = costs.refresh(None, source(home), ["ccusage"], now, run=fake_ccusage())
        assert sorted((r.model, r.cost) for r in cache.rows) == [
            ("claude-opus-4-7", 1.0), ("claude-opus-4-7", 2.0), ("claude-sonnet-4-5", 0.5)]
        assert cache.boundary == costs.day_start(now)
        assert all(r.slot >= costs.range_start(now) for r in cache.rows)

    def test_slots_leaving_the_range_fold_into_the_archive_and_survive_a_reload(
            self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 3 * 86400, "d3", out=2000),
              entry(now - 600, "t1"))
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        assert first.archive == {}
        later = now + costs.RANGE_DAYS * 86400  # d3 is out of range then
        second = costs.refresh(first, source(home), ["x"], later, run=fake_ccusage())
        d3 = costs.day_start(now - 3 * 86400)
        assert second.archive[d3] == {"claude-opus-4-7": 2.0}
        assert all(r.slot >= costs.range_start(later) for r in second.rows)
        costs.save_cache(tmp_path, second)
        assert costs.load_cache(tmp_path).archive == second.archive
        third = costs.refresh(second, source(home), ["x"], later + 86400, run=fake_ccusage())
        assert third.archive[d3] == {"claude-opus-4-7": 2.0}  # folded once, not again

    def test_the_archive_keeps_token_counts_beside_the_dollars(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 3 * 86400, "d3", out=2000),
              entry(now - 3 * 86400 + 60, "d3b", out=500), entry(now - 600, "t1"))
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        later = now + costs.RANGE_DAYS * 86400
        second = costs.refresh(first, source(home), ["x"], later, run=fake_ccusage())
        d3 = costs.day_start(now - 3 * 86400)
        assert second.archive_tokens[d3] == {"claude-opus-4-7": (6, 20, 200, 2500)}
        costs.save_cache(tmp_path, second)
        assert costs.load_cache(tmp_path).archive_tokens == second.archive_tokens
        third = costs.refresh(second, source(home), ["x"], later + 86400, run=fake_ccusage())
        assert third.archive_tokens[d3] == {"claude-opus-4-7": (6, 20, 200, 2500)}
        raw = second.to_json()
        del raw["archiveTokens"]  # rewritten by an older rota: the dollars stay
        old = costs.CostCache.from_json(raw)
        assert old.archive == second.archive and old.archive_tokens == {}
        raw["archiveTokens"] = {str(int(d3)): {"m": [1, 2, 3], "n": [1, 2, 3, "x"], "o": [1, 2, 3, 4]}}
        assert costs.CostCache.from_json(raw).archive_tokens == {d3: {"o": (1, 2, 3, 4)}}

    def test_the_archive_keeps_its_newest_days(self, now):
        rows = [costs.Row("s0", int(costs.day_start(now, d)), "m", 1.0, 0, 0, 0, 0)
                for d in range(costs.ARCHIVE_DAYS + 5)]
        archive = costs.fold({}, rows)
        assert len(archive) == costs.ARCHIVE_DAYS
        assert min(archive) == costs.day_start(now, costs.ARCHIVE_DAYS - 1)

    def test_a_garbled_archive_reads_as_empty(self, now):
        raw = make_cache([], now).to_json()
        raw["archive"] = {"x": {"m": 1}, "12": "no", "13": {"m": "1", "n": 2.0}}
        assert costs.CostCache.from_json(raw).archive == {13.0: {"n": 2.0}}
        del raw["archive"]  # a cache written before the archive existed
        assert costs.CostCache.from_json(raw).archive == {}

    def test_reprices_only_from_the_boundary_and_matches_a_full_build(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 3 * 86400, "d3"), entry(now - 600, "t1"))
        calls: list = []
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage(calls))
        assert costs.is_current(first, source(home), now)
        write(f, entry(now - 30, "t2", out=3000))
        assert not costs.is_current(first, source(home), now)
        sliced: list[Path] = []

        def spy(runner, d):
            sliced.extend(p.name for p in (d / "projects" / "s0").iterdir())
            return fake_ccusage()(runner, d)

        second = costs.refresh(first, source(home), ["x"], now, run=spy)
        assert all(int(name.split(".")[0]) >= costs.day_start(now) for name in sliced)
        full = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        assert second.rows == full.rows
        assert costs.is_current(second, source(home), now)

    def test_nothing_new_since_the_boundary_runs_no_ccusage(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 3 * 86400, "d3"))
        touch(f, now - 3 * 86400)
        calls: list = []
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage(calls))
        again = costs.refresh(first, source(home), ["x"], now, run=fake_ccusage(calls))
        assert len(calls) == 1 and again.rows == first.rows

    def test_a_new_day_reprices_from_the_old_boundary(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        tomorrow = now + 86400
        write(f, entry(now + 3 * HOUR, "late"), entry(tomorrow - 60, "next"))
        assert not costs.is_current(first, source(home), tomorrow)
        later = costs.refresh(first, source(home), ["x"], tomorrow, run=fake_ccusage())
        assert later.boundary == costs.day_start(tomorrow)
        assert sorted(r.slot for r in later.rows) == sorted(
            int(t // 900) * 900 for t in (now - 600, now + 3 * HOUR, tomorrow - 60))

    def test_old_slots_fall_out_of_the_range(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 29 * 86400, "edge"))
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        assert len(first.rows) == 1
        later = costs.refresh(first, source(home), ["x"], now + 2 * 86400, run=fake_ccusage())
        assert later.rows == []

    def test_changed_sources_rebuild(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))
        first = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        other = tmp_path / ".claude-work"
        write(other / "projects" / "q" / "b.jsonl", entry(now - 3 * 86400, "w1"))
        both = source(home) + [costs.Source("s1", os.path.realpath(other / "projects"))]
        assert not costs.is_current(first, both, now)
        rebuilt = costs.refresh(first, both, ["x"], now, run=fake_ccusage())
        assert {r.source for r in rebuilt.rows} == {"s0", "s1"}

    def test_cache_round_trip_and_garbage(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))
        backup = tmp_path / ".rota"
        cache = costs.refresh(None, source(home), ["x"], now, run=fake_ccusage())
        costs.save_cache(backup, cache)
        loaded = costs.load_cache(backup)
        assert loaded.rows == cache.rows and loaded.offsets == cache.offsets
        assert loaded.boundary == cache.boundary and loaded.sources == cache.sources
        costs.cache_path(backup).write_text("{not json")
        assert costs.load_cache(backup) is None
        costs.cache_path(backup).write_text(json.dumps({"schemaVersion": 99}))
        assert costs.load_cache(backup) is None
        assert costs.load_cache(tmp_path / "missing") is None

    def test_a_ccusage_failure_propagates_and_keeps_no_partial_cache(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))

        def broken(runner, d):
            raise costs.CostError("ccusage failed: boom")

        with pytest.raises(costs.CostError):
            costs.refresh(None, source(home), ["x"], now, run=broken)


# -- attribution ---------------------------------------------------------------------------


def start_line(t: float) -> str:
    stamp = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp},001 - INFO - rota: engine started\n"


def log_line(t: float, frm: str, to: str) -> str:
    stamp = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp},123 - INFO - Switched from account {frm} to {to}\n"


class TestAttribute:
    def test_a_slot_with_a_switch_in_it_is_split_by_time(self):
        spans = [(0.0, 300.0, "1"), (300.0, 2000.0, "2")]
        got = dict(cr.shares(0, spans, [s[0] for s in spans]))
        assert got == pytest.approx({"1": 1 / 3, "2": 2 / 3})

    def test_attribute_credits_session_profiles_to_their_account(self):
        sources = [costs.Source("s0", "/a"), costs.Source("s1", "/b", "7")]
        rows = [costs.Row("s0", 0, "m", 3.0, 0, 0, 0, 0), costs.Row("s1", 0, "m", 2.0, 0, 0, 0, 0),
                costs.Row("s0", 900, "m", 1.0, 0, 0, 0, 0)]
        spans = [(0.0, 300.0, "1"), (300.0, 1800.0, "2")]
        got = cr.attribute(rows, sources, spans)
        assert got["7"] == [(0, 2.0)]
        assert dict(got["1"]) == pytest.approx({0: 1.0})
        assert dict(got["2"]) == pytest.approx({0: 2.0, 900: 1.0})

    def test_spend_before_any_known_duty_is_unattributed(self):
        got = cr.attribute([costs.Row("s0", 0, "m", 3.0, 0, 0, 0, 0)], [costs.Source("s0", "/a")], [])
        assert got == {cr.UNATTRIBUTED: [(0, 3.0)]}

    def test_a_slot_half_covered_leaves_the_rest_unattributed(self):
        got = cr.attribute([costs.Row("s0", 0, "m", 4.0, 0, 0, 0, 0)], [costs.Source("s0", "/a")],
                           [(450.0, 2000.0, "1")])
        assert dict(got["1"]) == pytest.approx({0: 2.0})
        assert dict(got[cr.UNATTRIBUTED]) == pytest.approx({0: 2.0})


# -- one account across profiles ------------------------------------------------------------


def spend_every_slot(lo: float, hi: float, per_hour: float) -> list[tuple[int, float]]:
    start = int(lo // 900) * 900
    return [(s, per_hour / 4) for s in range(start, int(hi), 900)]


class TestAcrossProfiles:
    def test_majority_is_who_was_on_duty_longest(self):
        spans = [(0.0, 1000.0, "1"), (1000.0, 3600.0, "2")]
        assert cr.majority(spans, 0, 3600) == "2"
        assert cr.majority(spans, 0, 900) == "1"
        assert cr.majority(spans, 5000, 6000) is None

    def test_covers_asks_for_every_duty_stretch_to_be_priced(self, now):
        p = cr.ProfileSpend({}, {}, ((now - 10 * HOUR, now - 5 * HOUR, "1"),
                                     (now - 5 * HOUR, now, "2")), built_at=now - 2 * HOUR,
                            lo=now - 20 * HOUR)
        assert p.covers("1", now - 9 * HOUR, now - HOUR)  # 1's stretch ends before built_at
        assert not p.covers("2", now - 4 * HOUR, now - HOUR)  # 2 on duty past built_at
        assert p.covers("2", now - 4 * HOUR, now - 3 * HOUR)
        early = cr.ProfileSpend({}, {}, ((now - 30 * HOUR, now, "1"),), now, now - 20 * HOUR)
        assert not early.covers("1", now - 25 * HOUR, now - 21 * HOUR)  # before its priced range
        assert early.covers("3", now - 25 * HOUR, now)  # never on duty: nothing to price

    def test_read_numbers_keys_accounts_by_identity(self, tmp_path):
        (tmp_path / "sequence.json").write_text(json.dumps({"accounts": {
            "1": {"email": "a@x", "organizationUuid": "o1"}, "2": {"email": "b@x"}, "3": "junk"}}))
        assert cr.read_numbers(tmp_path) == {("a@x", "o1"): "1", ("b@x", ""): "2"}
        assert cr.read_numbers(tmp_path / "missing") == {}

    def test_sources_from_paths_credit_session_profiles_again(self, tmp_path):
        backup = tmp_path / ".rota"
        (backup / "sessions" / "7-a@x" / "projects").mkdir(parents=True)
        session = os.path.realpath(backup / "sessions" / "7-a@x" / "projects")
        got = costs.sources_from_paths(backup, ["/main/projects", session])
        assert got == [costs.Source("s0", "/main/projects"), costs.Source("s1", session, "7")]


# -- rollups ---------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,name", [
    ("claude-opus-4-7", "Opus 4.7"),
    ("claude-opus-4-20250514", "Opus 4"),
    ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    ("claude-fable-5-1", "Fable 5.1"),
    ("claude-3-5-haiku-20241022", "Haiku 3.5"),
    ("claude-3-opus-20240229", "Opus 3"),
    ("claude-sonnet-4-5[1m]", "Sonnet 4.5"),
    ("claude-weird_name", "weird_name"),
])
def test_model_name(raw, name):
    assert cr.model_name(raw) == name


class FakeHistory:
    """Windows and exact readings per (email, label); no hourly peaks."""

    def __init__(self, windows: dict, points: dict | None = None):
        self._w = windows
        self._p = points or {}

    def windows(self, identity, label, since):
        return [w for w in self._w.get((identity[0], label), []) if w.resets_at >= since]

    def long_series(self, identity, label, since):
        return []

    def series(self, identities):
        return {num: {label: pts for (email, label), pts in self._p.items() if email == ident[0]}
                for num, ident in identities.items()}


def climb(reset: float, duration: float, pcts: list[float]) -> list[Point]:
    """Readings spread evenly through the window ending at ``reset``."""
    every = duration / (len(pcts) + 1)
    return [Point(reset - duration + (i + 1) * every, pct, reset) for i, pct in enumerate(pcts)]


def make_cache(rows: list[costs.Row], now: float) -> costs.CostCache:
    return costs.CostCache(costs.day_start(now), ["/a"], rows, {}, now - 60, 1.5)


class TestBuildReport:
    def test_totals_days_models_trend_and_accounts(self, now):
        today = costs.day_start(now)
        rows = [
            costs.Row("s0", int(now - 2 * HOUR) // 900 * 900, "claude-opus-4-7", 30.0, 1, 2, 3, 4),
            costs.Row("s0", int(now - 3 * 86400) // 900 * 900, "claude-opus-4-7", 20.0, 1, 2, 3, 4),
            costs.Row("s0", int(now - 3 * 86400) // 900 * 900, "claude-sonnet-4-5", 10.0, 1, 1, 1, 1),
            costs.Row("s0", int(now - 10 * 86400) // 900 * 900, "claude-opus-4-7", 25.0, 1, 2, 3, 4),
            costs.Row("s0", int(now - 20 * 86400) // 900 * 900, "claude-opus-4-7", 15.0, 0, 0, 0, 0),
            costs.Row("s0", int(now - 2 * HOUR) // 900 * 900, "claude-haiku-4-5", 0.001, 0, 0, 0, 0),
        ]
        spans = [(now - 40 * 86400, now - 3 * 86400 - HOUR, "2"), (now - 3 * 86400 - HOUR, now, "1")]
        report = cr.build_report(make_cache(rows, now), [costs.Source("s0", "/a")], spans, now)
        assert report.today == pytest.approx(30.0 + 0.001)
        assert report.week == pytest.approx(60.001)
        assert report.month == pytest.approx(100.001)
        assert report.week_change == pytest.approx((60.001 - 25.0) / 25.0)
        assert [m.name for m in report.models] == ["Opus 4.7", "Sonnet 4.5"]  # $0.001 is noise
        opus = report.models[0]
        assert opus.cost == 90.0 and opus.share == pytest.approx(0.9, abs=1e-3)
        assert opus.trend == pytest.approx((50 - 25) / 25)
        assert report.models[1].trend is None  # nothing the week before
        assert len(report.days) == costs.RANGE_DAYS and report.days[-1][0] == today
        assert report.days[-1][1]["Opus 4.7"] == 30.0
        assert [(a.number, round(a.cost, 3)) for a in report.accounts] == [("1", 60.001), ("2", 40.0)]
        assert report.unattributed == pytest.approx(0.0, abs=1e-6)

    def test_hours_hold_the_last_week_by_model_and_who_was_on_duty(self, now):
        now += 1200  # 20 minutes into the hour
        hour = int(now // HOUR * HOUR)
        rows = [costs.Row("s0", hour - 3600, "claude-opus-4-7", 4.0, 0, 0, 0, 0),
                costs.Row("s0", hour - 2700, "claude-sonnet-4-5", 1.0, 0, 0, 0, 0),
                costs.Row("s0", hour - 30 * 3600, "claude-opus-4-7", 9.0, 0, 0, 0, 0),
                costs.Row("s0", hour - 8 * 86400, "claude-opus-4-7", 7.0, 0, 0, 0, 0)]
        spans = [(now - 40 * HOUR, hour - 1800, "1"), (hour - 1800, now, "2")]  # switch at :30
        report = cr.build_report(make_cache(rows, now), [costs.Source("s0", "/a")], spans, now)
        assert len(report.hours) == cr.RECENT_HOURS and report.hours[-1][0] == hour
        assert report.hours[-2][1] == {"Opus 4.7": 4.0, "Sonnet 4.5": 1.0}
        assert sum(sum(h.values()) for _, h in report.hours) == 14.0  # 8 days ago is not in it
        assert report.hours[-31][1] == {"Opus 4.7": 9.0}
        assert report.hour_duty[-3:] == ("1", "1", "2")  # the switched hour: a tie goes first-come
        assert cr.build_report(make_cache(rows, now), [], [], now).hour_duty == (None,) * 168

    def test_account_buckets_split_by_who_was_on_duty_and_sum_like_the_model_ones(self, now):
        now += 1200
        hour = int(now // HOUR * HOUR)
        rows = [costs.Row("s0", hour - 3600, "claude-opus-4-7", 4.0, 0, 0, 0, 0),  # all #1
                costs.Row("s0", hour - 1800, "claude-sonnet-4-5", 2.0, 0, 0, 0, 0),  # all #2
                costs.Row("s0", hour - 60 * HOUR, "claude-opus-4-7", 3.0, 0, 0, 0, 0)]  # nobody
        spans = [(now - 40 * HOUR, hour - 1800, "1"), (hour - 1800, now, "2")]
        report = cr.build_report(make_cache(rows, now), [costs.Source("s0", "/a")], spans, now)
        assert report.hour_cells[-2][1] == {("1", "Opus 4.7"): 4.0, ("2", "Sonnet 4.5"): 2.0}
        assert report.hour_cells[-61][1] == {("", "Opus 4.7"): 3.0}  # no duty span: unattributed
        assert report.unattributed == pytest.approx(3.0)
        assert {a.number for a in report.accounts} == {"1", "2"}  # "" is no account
        for (t, by_model), (t2, cells) in zip(report.hours, report.hour_cells):
            assert t == t2 and by_model == pytest.approx(cr.by_model(cells))
        for (t, by_model), (t2, cells) in zip(report.history, report.history_cells):
            assert t == t2 and by_model == pytest.approx(cr.by_model(cells))
        assert sum(sum(d.values()) for _, d in report.history_cells) == pytest.approx(9.0)

    def test_history_puts_the_folded_days_before_the_range(self, now):
        cache = make_cache([costs.Row("s0", int(now) // 900 * 900, "claude-opus-4-7", 3.0,
                                      0, 0, 0, 0)], now)
        old = costs.day_start(now, 40)
        cache.archive.update({old: {"claude-opus-4-7": 5.0, "claude-sonnet-4-5": 1.0},
                              costs.day_start(now): {"claude-opus-4-7": 99.0}})  # in range: ignored
        report = cr.build_report(cache, [], [], now)
        assert report.history[0] == (old, {"Opus 4.7": 5.0, "Sonnet 4.5": 1.0})
        assert report.history[1:] == report.days
        assert report.history[-1][1] == {"Opus 4.7": 3.0}
        assert report.history_cells[0][1] == {("", "Opus 4.7"): 5.0, ("", "Sonnet 4.5"): 1.0}

    def test_accounts_carry_their_figures_even_without_spend(self, now):
        fig = dpp.PerPct(2.0, True)
        report = cr.build_report(make_cache([], now), [], [], now, {"3": {"5h": fig}})
        assert report.accounts == (cr.AccountLine("3", 0.0, fig, None),)

    def test_empty(self, now):
        report = cr.build_report(make_cache([], now), [], [], now)
        assert report.month == 0 and report.models == () and report.week_change is None
        assert report.accounts == ()


# -- the source the view reads ------------------------------------------------------------------


class TestCostSource:
    def make(self, tmp_path, home, now, *, runner=("x",), run=None):
        return cr.CostSource(
            tmp_path / ".rota", FakeHistory({}), [home],
            find_runner=lambda: list(runner) if runner else None,
            run=run or fake_ccusage(), clock=lambda: now,
            other_profiles=lambda: [], store_path=tmp_path / dpp.STORE_FILENAME)

    def test_first_refresh_builds_and_saves_then_reopening_is_cached(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1", out=4000))
        calls: list = []
        src = self.make(tmp_path, home, now, run=fake_ccusage(calls))
        assert src.cached({}).report is None
        first = src.refresh({})
        assert first.ran and first.report.today == pytest.approx(4.0) and first.note is None
        assert costs.cache_path(tmp_path / ".rota").exists()
        assert src.cached({}).report.today == pytest.approx(4.0)
        again = src.refresh({})
        assert not again.ran and len(calls) == 1
        forced = src.refresh({}, force=True)
        assert forced.ran and len(calls) == 2

    def test_missing_ccusage_says_how_to_get_it(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))
        result = self.make(tmp_path, home, now, runner=None).refresh({})
        assert result.report is None and result.note == costs.MISSING_HINT

    def test_missing_ccusage_still_shows_the_cached_report(self, tmp_path, home, now):
        f = write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))
        self.make(tmp_path, home, now).refresh({})
        write(f, entry(now - 60, "t2"))
        result = self.make(tmp_path, home, now, runner=None).refresh({})
        assert result.report is not None and result.note == costs.MISSING_HINT

    def test_a_ccusage_error_is_a_note(self, tmp_path, home, now):
        write(home / "projects" / "p" / "a.jsonl", entry(now - 600, "t1"))

        def broken(runner, d):
            raise costs.CostError("ccusage failed: boom")

        result = self.make(tmp_path, home, now, run=broken).refresh({})
        assert result.report is None and result.note == "ccusage failed: boom"
        assert not costs.cache_path(tmp_path / ".rota").exists()

    def test_switch_log_attributes_spend(self, tmp_path, home, now):
        backup = tmp_path / ".rota"
        backup.mkdir()
        (backup / "rota.log").write_text(start_line(now - 3 * HOUR) + log_line(now - HOUR, "1", "2"))
        write(home / "projects" / "p" / "a.jsonl", entry(now - 4 * HOUR, "z1"),
              entry(now - 2 * HOUR, "a1"), entry(now - 600, "b1"))
        report = self.make(tmp_path, home, now).refresh({}).report
        assert {a.number: a.cost for a in report.accounts} == pytest.approx({"1": 1.0, "2": 1.0})
        assert report.unattributed == pytest.approx(1.0)  # before rota started: nobody


class TestPerPctAcrossProfiles:
    """One account (a@x) held by two profiles: #1 here, #2 in "work"."""

    RESET_AGO = HOUR

    def setup(self, tmp_path, now, *, other_built_at: float | None = None):
        me, work = tmp_path / ".rota", tmp_path / "work" / ".rota"
        home = tmp_path / ".claude"
        (home / "projects").mkdir(parents=True)
        reset = now - self.RESET_AGO
        start = reset - 5 * HOUR
        for backup, number, src in ((me, "1", os.path.realpath(home / "projects")),
                                    (work, "2", "/work/projects")):
            backup.mkdir(parents=True)
            (backup / "sequence.json").write_text(json.dumps({
                "activeAccountNumber": number,
                "accounts": {number: {"email": "a@x", "organizationUuid": "o"}}}))
            (backup / "rota.log").write_text(start_line(start - HOUR))  # on duty since rota began
            rows = [costs.Row("s0", slot, "claude-opus-4-7", usd, 0, 0, 0, 0)
                    for slot, usd in spend_every_slot(start, reset, 10.0)]  # $10/h each
            built = other_built_at if (backup == work and other_built_at) else now - 60
            costs.save_cache(backup, costs.CostCache(costs.day_start(now), [src], rows, {}, built))
        history = FakeHistory(
            {("a@x", "5h"): [WindowSummary(reset, 40.0)]},
            {("a@x", "5h"): climb(reset, 5 * HOUR, [10, 20, 30, 40])})  # peak 1h before reset
        store = tmp_path / "shared" / dpp.STORE_FILENAME
        store.parent.mkdir()
        source = cr.CostSource(me, history, [home], clock=lambda: now,
                               find_runner=lambda: None, other_profiles=lambda: [work],
                               store_path=store)
        return source, store

    def test_spend_is_summed_over_every_profile_holding_the_account(self, tmp_path, now):
        source, store = self.setup(tmp_path, now)
        acc = source.cached({"1": ("a@x", "o")}).report.accounts[0]
        # 4h to the peak at $10/h in each profile, over 40%
        assert acc.per_5h.dollars == pytest.approx(2 * 40.0 / 40)
        assert acc.cost == pytest.approx(50.0)  # the cost column stays this profile's own
        kept = dpp.load_store(store)["a@x|o"]["5h"]  # 10% climbs of 1h: $20 each over both
        assert sum(s.usd for s in kept) == pytest.approx(80.0) and len(kept) == 4

    def test_a_window_the_other_profile_has_not_priced_yet_waits(self, tmp_path, now):
        source, store = self.setup(tmp_path, now, other_built_at=now - 4 * HOUR)
        acc = source.cached({"1": ("a@x", "o")}).report.accounts[0]
        assert acc.per_5h is None and not store.exists()

    def test_a_stored_window_outlives_the_priced_range(self, tmp_path, now):
        source, store = self.setup(tmp_path, now)
        source.cached({"1": ("a@x", "o")})
        kept = store.read_text()
        source.clock = lambda: now + costs.RANGE_DAYS * 86400  # its start is out of range now
        source.cached({"1": ("a@x", "o")})
        assert store.read_text() == kept
