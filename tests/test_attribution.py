"""attribution: who was on duty when, from switch logs (rota's and the tool
it imported from) and logins recorded in transcripts; nobody before the
earliest evidence."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_rotater import attribution as at
from claude_rotater import cost_report as cr
from claude_rotater import costs

HOUR = 3600.0
NOW = 1_793_000_000.0


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") + ",123"


def line(t: float, msg: str) -> str:
    return f"{stamp(t)} - INFO - {msg}\n"


def seq(path: Path, accounts: dict[str, tuple[str, str]], active: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": active,
        "accounts": {n: {"email": e, "organizationUuid": o} for n, (e, o) in accounts.items()}}))


def login(t: float, org: str) -> str:
    iso = datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "attachment", "timestamp": iso,
                       "attachment": {"type": "credential_org", "organizationUuid": org}}) + "\n"


A, B, C = ("a@x", "oa"), ("b@x", "ob"), ("c@x", "oc")


class TestReadLog:
    def test_parses_every_kind_and_the_first_line(self, tmp_path):
        log = tmp_path / "rota.log"
        log.write_text(
            line(NOW - 9 * HOUR, "rota: imported accounts and settings from /old/dir into /new; x")
            + line(NOW - 8 * HOUR, "Added account 4 (org: oc)")
            + line(NOW - 7 * HOUR, "Switched from account 1 to 4")
            + line(NOW - 6 * HOUR, "Removed account 4: c@x")
            + line(NOW - 5 * HOUR, "Added account 2: b@x (org: ob)")
            + line(NOW - 4 * HOUR, "Added account 3 from browser login: a@x")
            + line(NOW - 3 * HOUR, "Switched from account unknown to 2")
            + "garbage\n" + line(NOW - 2 * HOUR, "Auto-switch (pace): account 4 -> 1"))
        got = at.read_log(log)
        assert [(g.kind, g.number, g.frm, g.email, g.org) for g in got] == [
            ("start", "", "", "", None), ("import", "", "", "", None),
            ("added", "4", "", "", "oc"), ("switch", "4", "1", "", None),
            ("removed", "4", "", "c@x", None), ("added", "2", "", "b@x", "ob"),
            ("added", "3", "", "a@x", None), ("switch", "2", "", "", None)]
        assert got[1].path == "/old/dir"
        assert got[0].at == pytest.approx(NOW - 9 * HOUR, abs=1)

    def test_rotated_logs_come_oldest_first(self, tmp_path):
        for name in ("rota.log", "rota.log.1", "rota.log.2", "notes.txt"):
            (tmp_path / name).write_text("")
        assert [p.name for p in at.log_paths(tmp_path)] == ["rota.log.2", "rota.log.1", "rota.log"]


class TestSwitches:
    def test_a_reused_number_is_the_account_it_named_then(self, tmp_path):
        seq(tmp_path, {"1": A, "4": C})
        (tmp_path / "rota.log").write_text(
            line(NOW - 9 * HOUR, "started")
            + line(NOW - 8 * HOUR, "Added account 4 (org: ob)")
            + line(NOW - 7 * HOUR, "Switched from account 1 to 4")
            + line(NOW - 6 * HOUR, "Removed account 4: b@x")
            + line(NOW - 5 * HOUR, "Added account 4 (org: oc)")
            + line(NOW - 4 * HOUR, "Switched from account 1 to 4"))
        directory = at.Directory()
        directory.add(B)  # known from another profile
        switches, marks = at.profile_switches(tmp_path, directory)
        assert [(s[1], s[2]) for s in switches] == [(B, A), (C, A)]
        assert marks == [(pytest.approx(NOW - 9 * HOUR, abs=1), A)]  # the first switch left A

    def test_the_imported_tools_log_is_read_by_identity(self, tmp_path):
        old, new = tmp_path / "old", tmp_path / "new"
        seq(old, {"1": A, "2": B})
        seq(new, {"1": B, "2": A})  # renumbered on import: identities decide
        (old / "claude-swap.log").write_text(
            line(NOW - 20 * HOUR, "Added account 1: a@x (org: oa)")
            + line(NOW - 19 * HOUR, "Added account 2: b@x (org: ob)")
            + line(NOW - 18 * HOUR, "Switched from account 1 to 2"))
        (new / "rota.log").write_text(
            line(NOW - 10 * HOUR, f"rota: imported accounts and settings from {old} into {new}")
            + line(NOW - 9 * HOUR, "Switched from account 1 to 2"))
        spans = at.timeline(new, [], NOW - 30 * HOUR, NOW, cache_path=tmp_path / "scan.json")
        assert spans == [(pytest.approx(NOW - 20 * HOUR, abs=1), pytest.approx(NOW - 18 * HOUR, abs=1), "2"),
                         (pytest.approx(NOW - 18 * HOUR, abs=1), pytest.approx(NOW - 9 * HOUR, abs=1), "1"),
                         (pytest.approx(NOW - 9 * HOUR, abs=1), NOW, "2")]

    def test_a_number_no_list_names_is_kept_in_the_own_log(self, tmp_path):
        (tmp_path / "rota.log").write_text(line(NOW - 3 * HOUR, "started")
                                           + line(NOW - HOUR, "Switched from account 1 to 2"))
        spans = at.timeline(tmp_path, [], NOW - 10 * HOUR, NOW, cache_path=tmp_path / "s.json")
        assert [s[2] for s in spans] == ["1", "2"]
        assert spans[0][0] == pytest.approx(NOW - 3 * HOUR, abs=1)  # not before rota started

    def test_no_switch_ever_credits_the_active_account_from_when_rota_started(self, tmp_path):
        seq(tmp_path, {"1": A}, active="1")
        (tmp_path / "rota.log").write_text(line(NOW - 2 * HOUR, "started"))
        spans = at.timeline(tmp_path, [], NOW - 10 * HOUR, NOW, cache_path=tmp_path / "s.json")
        assert spans == [(pytest.approx(NOW - 2 * HOUR, abs=1), NOW, "1")]

    def test_no_log_no_evidence_nobody(self, tmp_path):
        seq(tmp_path, {"1": A}, active="1")
        assert at.timeline(tmp_path, [], NOW - 10 * HOUR, NOW, cache_path=tmp_path / "s") == []


class TestBuildSpans:
    def test_logins_hold_until_the_next_mark_and_switches_take_over(self):
        logins = [(NOW - 9 * HOUR, A), (NOW - 6 * HOUR, B), (NOW - HOUR, C)]  # C: after a switch
        switches = [(NOW - 4 * HOUR, A, B), (NOW - 2 * HOUR, B, A)]
        spans = at.build_spans(switches, logins, {A: "1", B: "2"}, NOW - 10 * HOUR, NOW)
        assert spans == [(NOW - 9 * HOUR, NOW - 6 * HOUR, "1"), (NOW - 6 * HOUR, NOW - 4 * HOUR, "2"),
                         (NOW - 4 * HOUR, NOW - 2 * HOUR, "1"), (NOW - 2 * HOUR, NOW, "2")]

    def test_nothing_before_the_earliest_evidence_and_unknowns_are_gaps(self):
        marks = [(NOW - 5 * HOUR, None), (NOW - 3 * HOUR, A)]
        spans = at.build_spans([], marks, {A: "1"}, NOW - 10 * HOUR, NOW)
        assert spans == [(NOW - 3 * HOUR, NOW, "1")]

    def test_a_removed_account_keeps_its_identity(self):
        spans = at.build_spans([], [(NOW - HOUR, C)], {A: "1"}, NOW - 2 * HOUR, NOW)
        assert spans == [(NOW - HOUR, NOW, "x:c@x|oc")]
        assert at.key_identity("x:c@x|oc") == C and at.key_identity("3") is None

    def test_evidence_before_the_range_is_clipped_to_it(self):
        spans = at.build_spans([], [(NOW - 50 * HOUR, A)], {A: "1"}, NOW - 10 * HOUR, NOW)
        assert spans == [(NOW - 10 * HOUR, NOW, "1")]


class TestTranscriptLogins:
    def test_reads_logins_then_only_what_was_appended(self, tmp_path):
        projects = tmp_path / "projects"
        f = projects / "p" / "s.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text('{"type":"user"}\n' + login(NOW - 5 * HOUR, "oa"))
        cache = tmp_path / "scan.json"
        assert at.transcript_logins([str(projects)], NOW - 30 * HOUR, cache) == [
            (pytest.approx(NOW - 5 * HOUR), "oa")]
        offset = json.loads(cache.read_text())["files"][str(f)][0]
        assert offset == f.stat().st_size
        with f.open("a") as fh:
            fh.write(login(NOW - 2 * HOUR, "ob") + '{"partial":')  # a line still being written
        got = at.transcript_logins([str(projects)], NOW - 30 * HOUR, cache)
        assert [o for _, o in got] == ["oa", "ob"]
        assert json.loads(cache.read_text())["files"][str(f)][0] < f.stat().st_size

    def test_subagent_files_count_and_a_rewritten_file_starts_over(self, tmp_path):
        nested = tmp_path / "projects" / "p" / "s" / "subagents" / "a.jsonl"
        nested.parent.mkdir(parents=True)
        nested.write_text(login(NOW - 3 * HOUR, "oa") + login(NOW - 2 * HOUR, "ob"))
        cache = tmp_path / "scan.json"
        assert len(at.transcript_logins([str(tmp_path / "projects")], 0, cache)) == 2
        nested.write_text(login(NOW - HOUR, "oc"))  # shorter: replaced
        assert [o for _, o in at.transcript_logins([str(tmp_path / "projects")], 0, cache)] == ["oc"]

    def test_a_line_across_read_chunks_is_still_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(at, "_CHUNK", 7)
        f = tmp_path / "projects" / "s.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text('{"type":"user","pad":"' + "x" * 50 + '"}\n' + login(NOW - HOUR, "oa"))
        assert [o for _, o in at.transcript_logins([str(tmp_path / "projects")], 0,
                                                   tmp_path / "c.json")] == ["oa"]

    def test_files_untouched_since_the_range_are_skipped(self, tmp_path):
        f = tmp_path / "projects" / "old.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(login(NOW - 100 * HOUR, "oa"))
        os.utime(f, (NOW - 100 * HOUR, NOW - 100 * HOUR))
        assert at.transcript_logins([str(tmp_path / "projects")], NOW - 50 * HOUR,
                                    tmp_path / "c.json") == []

    def test_logins_fill_the_time_before_the_first_switch(self, tmp_path):
        data = tmp_path / ".rota"
        seq(data, {"1": A, "2": B, "3": C})
        (data / "rota.log").write_text(line(NOW - 4 * HOUR, "started")
                                       + line(NOW - 3 * HOUR, "Switched from account 2 to 3"))
        projects = tmp_path / "projects"
        (projects / "p").mkdir(parents=True)
        (projects / "p" / "s.jsonl").write_text(
            login(NOW - 9 * HOUR, "oa") + login(NOW - 6 * HOUR, "ob") + login(NOW - HOUR, "oa"))
        spans = at.timeline(data, [str(projects)], NOW - 10 * HOUR, NOW,
                            cache_path=tmp_path / "scan.json")
        assert [(round((NOW - a) / HOUR), round((NOW - b) / HOUR), who) for a, b, who in spans] == [
            (9, 6, "1"), (6, 3, "2"), (3, 0, "3")]  # the login after the switch is not used


class TestUnattributedInPerPct:
    def test_a_window_with_spend_nobody_was_credited_with_is_not_covered(self):
        spend = {"1": [(int(NOW - 4 * HOUR), 5.0)], cr.UNATTRIBUTED: [(int(NOW - 2 * HOUR) // 900 * 900, 3.0)]}
        p = cr.ProfileSpend({A: "1"}, spend, ((NOW - 10 * HOUR, NOW, "1"),), NOW, NOW - 20 * HOUR)
        assert not p.covers("1", NOW - 3 * HOUR, NOW - HOUR)
        assert p.covers("1", NOW - 9 * HOUR, NOW - 5 * HOUR)

    def test_a_removed_account_is_still_found_by_identity(self):
        p = cr.ProfileSpend({A: "1"}, {"x:c@x|oc": [(0, 1.0)]}, (), NOW, NOW - HOUR)
        assert p.key(C) == "x:c@x|oc" and p.holds(C) and p.key(A) == "1"
        assert not p.holds(B)


def test_build_report_lists_removed_accounts_but_never_the_unattributed_share():
    now = NOW
    slot = int(now - HOUR) // 900 * 900
    rows = [costs.Row("s0", slot, "claude-opus-4-7", 4.0, 0, 0, 0, 0),
            costs.Row("s0", slot - 20 * 3600, "claude-opus-4-7", 2.0, 0, 0, 0, 0)]
    cache = costs.CostCache(costs.day_start(now), ["/a"], rows, {}, now)
    spans = [(now - 2 * HOUR, now, "x:c@x|oc")]
    report = cr.build_report(cache, [costs.Source("s0", "/a")], spans, now)
    assert [(a.number, a.cost) for a in report.accounts] == [("x:c@x|oc", 4.0)]
    assert report.unattributed == pytest.approx(2.0)
