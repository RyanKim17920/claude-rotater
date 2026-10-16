"""``cswap feed``: statusline payload parsing, the bounded JSONL writer, the
incremental reader, and the never-fail command contract."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_rotater import feed


NOW = 1_760_000_000.0


def payload(**overrides) -> dict:
    base = {
        "session_id": "sess-1",
        "transcript_path": "/home/u/.claude/projects/-repo/sess-1.jsonl",
        "model": {"id": "claude-opus-x", "display_name": "Opus"},
        "cost": {"total_cost_usd": 1.25},
        "rate_limits": {
            "five_hour": {"used_percentage": 42.5, "resets_at": 1_760_003_600},
            "seven_day": {"used_percentage": 12, "resets_at": 1_760_400_000},
        },
    }
    base.update(overrides)
    return base


def raw(**overrides) -> bytes:
    return json.dumps(payload(**overrides)).encode()


class TestParsePayload:
    def test_full_payload(self):
        rec = feed.parse_payload(raw(), NOW, environ={})
        assert rec == {
            "t": NOW,
            "five_hour": {"pct": 42.5, "resets_at": 1_760_003_600.0},
            "seven_day": {"pct": 12.0, "resets_at": 1_760_400_000.0},
            "session_id": "sess-1",
            "config_dir": "/home/u/.claude",
            "model": "Opus",
            "cost": 1.25,
        }

    @pytest.mark.parametrize(
        "data",
        [b"", b"not json", b"[]", b"42", b'"x"', b"{}", b'{"rate_limits": null}',
         b'{"rate_limits": {}}', b"\xff\xfe", b"[" * 100000],
    )
    def test_garbage_and_missing_limits_give_none(self, data):
        assert feed.parse_payload(data, NOW, environ={}) is None

    def test_one_window_is_enough(self):
        rec = feed.parse_payload(
            raw(rate_limits={"seven_day": {"used_percentage": 3}}), NOW, environ={}
        )
        assert rec["seven_day"] == {"pct": 3.0}
        assert "five_hour" not in rec

    @pytest.mark.parametrize(
        "window",
        [None, "x", {}, {"used_percentage": "50"}, {"used_percentage": True},
         {"used_percentage": -1}, {"used_percentage": float("nan")}],
    )
    def test_bad_windows_are_dropped(self, window):
        limits = {"five_hour": window, "seven_day": {"used_percentage": 7}}
        rec = feed.parse_payload(raw(rate_limits=limits), NOW, environ={})
        assert "five_hour" not in rec
        assert rec["seven_day"]["pct"] == 7.0

    def test_pct_clamped_to_100(self):
        limits = {"five_hour": {"used_percentage": 140}}
        rec = feed.parse_payload(raw(rate_limits=limits), NOW, environ={})
        assert rec["five_hour"]["pct"] == 100.0

    @pytest.mark.parametrize(
        "value, expected",
        [
            (1_760_003_600, 1_760_003_600.0),
            (1_760_003_600_000, 1_760_003_600.0),  # milliseconds
            ("2025-10-09T10:00:00Z", 1_760_004_000.0),
            ("2025-10-09T10:00:00+00:00", 1_760_004_000.0),
            ("2025-10-09T10:00:00", 1_760_004_000.0),  # naive -> UTC
            ("garbage", None),
            (0, None),
            (None, None),
        ],
    )
    def test_resets_at_forms(self, value, expected):
        limits = {"five_hour": {"used_percentage": 1, "resets_at": value}}
        rec = feed.parse_payload(raw(rate_limits=limits), NOW, environ={})
        assert rec["five_hour"].get("resets_at") == expected

    def test_config_dir_from_transcript_beats_env(self):
        rec = feed.parse_payload(
            raw(transcript_path="/w/.claude-other/projects/p/s.jsonl"),
            NOW,
            environ={"CLAUDE_CONFIG_DIR": "/elsewhere"},
        )
        assert rec["config_dir"] == "/w/.claude-other"

    def test_config_dir_falls_back_to_env_then_default(self):
        no_transcript = payload()
        del no_transcript["transcript_path"]
        data = json.dumps(no_transcript)
        assert (
            feed.parse_payload(data, NOW, environ={"CLAUDE_CONFIG_DIR": "/c"})["config_dir"]
            == "/c"
        )
        assert feed.parse_payload(data, NOW, environ={})["config_dir"] == os.path.join(
            os.path.expanduser("~"), ".claude"
        )

    def test_transcript_outside_projects_is_ignored(self):
        rec = feed.parse_payload(
            raw(transcript_path="/tmp/x.jsonl"), NOW, environ={"CLAUDE_CONFIG_DIR": "/c"}
        )
        assert rec["config_dir"] == "/c"

    def test_optional_fields_absent_or_malformed(self):
        rec = feed.parse_payload(
            raw(session_id=5, model="Opus", cost={"total_cost_usd": "1"}),
            NOW,
            environ={},
        )
        assert "session_id" not in rec and "model" not in rec and "cost" not in rec

    def test_model_falls_back_to_id_and_text_is_capped(self):
        rec = feed.parse_payload(
            raw(model={"id": "m" * 1000}, session_id="s" * 1000), NOW, environ={}
        )
        assert rec["model"] == "m" * 128
        assert rec["session_id"] == "s" * 128

    def test_parsed_record_is_valid(self):
        assert feed.valid_record(feed.parse_payload(raw(), NOW, environ={}))


class TestValidRecord:
    @pytest.mark.parametrize(
        "record",
        [None, [], {}, {"t": "1"}, {"t": 1.0}, {"t": 1.0, "five_hour": "x"},
         {"t": 1.0, "five_hour": {"pct": "1"}},
         {"t": 1.0, "five_hour": {"pct": 1, "resets_at": "soon"}},
         {"t": 1.0, "five_hour": {"pct": 1}, "config_dir": 3}],
    )
    def test_rejects(self, record):
        assert not feed.valid_record(record)

    def test_accepts_minimal(self):
        assert feed.valid_record({"t": 1.0, "seven_day": {"pct": 1}})


class TestAppendAndRead:
    def test_round_trip_and_modes(self, tmp_path):
        path = feed.feed_path(tmp_path)
        rec = {"t": 1.0, "five_hour": {"pct": 1.0}}
        feed.append_record(path, rec)
        records, cursor = feed.read_new(path, None)
        assert records == [rec]
        if sys.platform != "win32":
            assert os.stat(path).st_mode & 0o777 == 0o600
            assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
        # Nothing new.
        again, cursor2 = feed.read_new(path, cursor)
        assert again == [] and cursor2 == cursor

    def test_incremental_reads(self, tmp_path):
        path = feed.feed_path(tmp_path)
        feed.append_record(path, {"t": 1.0, "five_hour": {"pct": 1.0}})
        _, cursor = feed.read_new(path, None)
        feed.append_record(path, {"t": 2.0, "five_hour": {"pct": 2.0}})
        feed.append_record(path, {"t": 3.0, "five_hour": {"pct": 3.0}})
        records, _ = feed.read_new(path, cursor)
        assert [r["t"] for r in records] == [2.0, 3.0]

    def test_partial_line_waits_for_newline(self, tmp_path):
        path = feed.feed_path(tmp_path)
        feed.append_record(path, {"t": 1.0, "five_hour": {"pct": 1.0}})
        _, cursor = feed.read_new(path, None)
        with open(path, "ab") as fh:
            fh.write(b'{"t": 2.0, "five_hour": {"pc')
        records, cursor = feed.read_new(path, cursor)
        assert records == []
        with open(path, "ab") as fh:
            fh.write(b't": 2.0}}\n')
        records, _ = feed.read_new(path, cursor)
        assert records == [{"t": 2.0, "five_hour": {"pct": 2.0}}]

    def test_garbage_lines_skipped(self, tmp_path):
        path = feed.feed_path(tmp_path)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as fh:
            fh.write(b'nope\n{"t": "x"}\n\xff\n{"t": 5, "seven_day": {"pct": 9}}\n')
        records, _ = feed.read_new(path, None)
        assert records == [{"t": 5, "seven_day": {"pct": 9}}]

    def test_rotation_keeps_one_generation_and_reader_follows(self, tmp_path):
        path = feed.feed_path(tmp_path)
        rec = {"t": 1.0, "five_hour": {"pct": 1.0}}
        feed.append_record(path, rec, max_bytes=100)
        _, cursor = feed.read_new(path, None)
        for i in range(5):
            feed.append_record(path, {"t": 2.0 + i, "five_hour": {"pct": 1.0}}, max_bytes=100)
        assert os.path.exists(path + feed.ROTATED_SUFFIX)
        assert os.path.getsize(path) < 200
        records, _ = feed.read_new(path, cursor)
        # New identity: read from the top of the fresh file; the newest
        # record is always there.
        assert records and records[-1]["t"] == 6.0

    def test_truncated_file_restarts(self, tmp_path):
        path = feed.feed_path(tmp_path)
        for i in range(3):
            feed.append_record(path, {"t": float(i), "five_hour": {"pct": 1.0}})
        _, cursor = feed.read_new(path, None)
        with open(path, "wb") as fh:
            fh.write(b'{"t": 9, "five_hour": {"pct": 1}}\n')
        records, _ = feed.read_new(path, cursor)
        assert records == [{"t": 9, "five_hour": {"pct": 1}}]

    def test_first_read_takes_only_the_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(feed, "INITIAL_TAIL_BYTES", 120)
        path = feed.feed_path(tmp_path)
        for i in range(50):
            feed.append_record(path, {"t": float(i), "five_hour": {"pct": 1.0}})
        records, _ = feed.read_new(path, None)
        assert 0 < len(records) < 5
        assert records[-1]["t"] == 49.0
        assert all(feed.valid_record(r) for r in records)

    def test_missing_file_reads_nothing(self, tmp_path):
        records, cursor = feed.read_new(str(tmp_path / "none.jsonl"), None)
        assert records == [] and cursor == feed.FeedCursor()


class TestMain:
    @pytest.fixture
    def backup(self, tmp_path, monkeypatch):
        root = tmp_path / "backup"
        root.mkdir()
        from claude_rotater import paths

        monkeypatch.setattr(paths, "get_backup_root", lambda: root)
        return root

    def run(self, data: bytes, argv=(), backup=None):
        out = io.BytesIO()
        rc = feed.main(list(argv), io.BytesIO(data), out, now=NOW)
        return rc, out.getvalue()

    def lines(self, backup: Path) -> list[dict]:
        path = feed.feed_path(backup)
        if not os.path.exists(path):
            return []
        return [json.loads(line) for line in open(path)]

    def test_appends_to_the_patched_backup_root(self, backup):
        rc, out = self.run(raw())
        assert rc == 0 and out == b""
        assert [r["five_hour"]["pct"] for r in self.lines(backup)] == [42.5]

    def test_tee_echoes_stdin_verbatim(self, backup):
        data = raw()
        rc, out = self.run(data, ["--tee"])
        assert rc == 0 and out == data
        assert len(self.lines(backup)) == 1

    def test_tee_echoes_garbage_and_records_nothing(self, backup):
        rc, out = self.run(b"garbage", ["--tee"])
        assert rc == 0 and out == b"garbage"
        assert self.lines(backup) == []

    def test_huge_input_is_passed_through_but_not_recorded(self, backup):
        data = b'{"rate_limits": {"five_hour": {"used_percentage": 1}}, "pad": "' + (
            b"x" * (feed.MAX_INPUT_BYTES + 10)
        ) + b'"}'
        rc, out = self.run(data, ["--tee"])
        assert rc == 0 and out == data
        assert self.lines(backup) == []

    def test_missing_backup_root_writes_nothing(self, tmp_path, monkeypatch):
        from claude_rotater import paths

        root = tmp_path / "absent"
        monkeypatch.setattr(paths, "get_backup_root", lambda: root)
        assert self.run(raw())[0] == 0
        assert not root.exists()

    def test_write_failure_is_swallowed(self, backup, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(feed, "append_record", boom)
        rc, out = self.run(raw(), ["--tee"])
        assert rc == 0 and out == raw()

    def test_backup_root_failure_is_swallowed(self, monkeypatch):
        from claude_rotater import paths

        def boom():
            raise RuntimeError("no home")

        monkeypatch.setattr(paths, "get_backup_root", boom)
        assert self.run(raw())[0] == 0

    def test_broken_stdin_is_swallowed(self, backup):
        class Broken:
            def read(self, n=-1):
                raise OSError("EIO")

        assert feed.main([], Broken(), io.BytesIO(), now=NOW) == 0

    def test_help(self, capsys):
        assert feed.main(["--help"], io.BytesIO(), io.BytesIO()) == 0
        assert "cswap feed" in capsys.readouterr().out


class TestEntryPoint:
    def test_feed_dispatches_without_loading_the_cli(self, tmp_path):
        """The console script must reach the feed without importing the CLI
        or the switcher: that is the whole latency budget."""
        backup = tmp_path / "backup"
        backup.mkdir()
        code = (
            "import sys, io\n"
            "from claude_rotater import paths\n"
            f"paths.get_backup_root = lambda: {str(backup)!r}\n"
            "sys.argv = ['cswap', 'feed']\n"
            f"sys.stdin = io.TextIOWrapper(io.BytesIO({raw()!r}))\n"
            "from claude_rotater import entry\n"
            "try:\n"
            "    entry.main()\n"
            "except SystemExit as e:\n"
            "    assert e.code == 0, e.code\n"
            "heavy = [m for m in ('claude_rotater.cli', 'claude_rotater.switcher',"
            " 'claude_rotater.oauth', 'urllib.request') if m in sys.modules]\n"
            "assert not heavy, heavy\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert len(open(feed.feed_path(backup)).readlines()) == 1

    def test_cli_main_dispatches_feed_for_wrappers(self, monkeypatch, tmp_path):
        """Wrappers (a second-profile launcher) call cli.main directly after
        patching paths.get_backup_root; the feed must land in that root."""
        from claude_rotater import cli, paths

        backup = tmp_path / "work"
        backup.mkdir()
        monkeypatch.setattr(paths, "get_backup_root", lambda: backup)
        monkeypatch.setattr(sys, "argv", ["cswap", "feed"])
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw())))
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert len(open(feed.feed_path(backup)).readlines()) == 1

    def test_feed_is_hidden_from_help(self, monkeypatch, capsys):
        from claude_rotater import cli

        monkeypatch.setattr(sys, "argv", ["cswap", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        assert "feed" not in capsys.readouterr().out
