"""API-key account state: refusals in transcripts, budgets, spend
(api_keys)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_rotater import api_keys as ak

NOW = 1_800_000_000.0


def _stamp(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).isoformat().replace("+00:00", "Z")


def _entry(text: str, error: str = "unknown", when: float = NOW, flag=True) -> dict:
    return {
        "type": "assistant",
        "isApiErrorMessage": flag,
        "error": error,
        "timestamp": _stamp(when),
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
    }


class TestClassify:
    @pytest.mark.parametrize(
        ("text", "error", "reason"),
        [
            ("API Error: 400 Your credit balance is too low to access the Anthropic API.",
             "invalid_request", ak.OUT_OF_CREDIT),
            ("Credit balance is too low", "unknown", ak.OUT_OF_CREDIT),
            ("You have reached your specified API usage limits.", "invalid_request", ak.USAGE_LIMIT),
            ("You have reached your specified workspace API usage limits.", "unknown", ak.USAGE_LIMIT),
            ('API Error: 429 {"type":"enforced_spend_limit_reached"}', "rate_limit", ak.SPEND_LIMIT),
            ("API Error: 400 something billing", "billing_error", ak.OUT_OF_CREDIT),
        ],
    )
    def test_refusals(self, text, error, reason):
        assert ak.classify(_entry(text, error)) == reason

    @pytest.mark.parametrize(
        "entry",
        [
            _entry("API Error: 429 rate limited", "rate_limit"),
            _entry("You've hit your session limit", "rate_limit"),
            _entry("Credit balance is too low", flag=None),  # an ordinary message quoting it
            {"type": "user", "message": "Credit balance is too low"},
            "not a dict",
        ],
    )
    def test_not_refusals(self, entry):
        assert ak.classify(entry) == ""


class TestScanRefusals:
    def _write(self, cfg: Path, name: str, lines: list, mtime: float) -> Path:
        project = cfg / "projects" / "-Users-me-repo"
        (project / "subagents").mkdir(parents=True, exist_ok=True)
        path = project / name
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_finds_the_newest_refusal_since(self, tmp_path):
        self._write(tmp_path, "a.jsonl", [
            {"type": "user", "message": {"content": "hi"}},
            _entry("Credit balance is too low", when=NOW - 100),
        ], NOW)
        self._write(tmp_path, "subagents/b.jsonl", [
            _entry("You have reached your specified API usage limits", when=NOW - 10),
        ], NOW)
        assert ak.scan_refusals(tmp_path, NOW - 500) == (ak.USAGE_LIMIT, NOW - 10)

    def test_ignores_refusals_before_the_key_went_on(self, tmp_path):
        self._write(tmp_path, "a.jsonl", [_entry("Credit balance is too low", when=NOW - 100)], NOW)
        assert ak.scan_refusals(tmp_path, NOW - 50) is None

    def test_skips_old_files_and_garbage(self, tmp_path):
        self._write(tmp_path, "old.jsonl", [_entry("Credit balance is too low")], NOW - 9999)
        path = self._write(tmp_path, "bad.jsonl", [], NOW)
        path.write_bytes(b'{"isApiErrorMessage":true, broken\n\xff\xfe\n')
        assert ak.scan_refusals(tmp_path, NOW - 500) is None

    def test_no_projects_dir(self, tmp_path):
        assert ak.scan_refusals(tmp_path / "missing", 0.0) is None


class TestState:
    def test_exhaustion_round_trip(self, tmp_path):
        ak.mark_exhausted(tmp_path, "3", ak.OUT_OF_CREDIT, NOW)
        state = ak.state_of(tmp_path, "3")
        assert state.exhausted and state.blocked(NOW) == ak.OUT_OF_CREDIT
        ak.clear_exhausted(tmp_path, "3")
        assert ak.state_of(tmp_path, "3") == ak.KeyState()
        assert ak.load(tmp_path) == {}

    def test_budget_and_spend(self, tmp_path):
        budget = ak.Budget("topup", 50.0, NOW - 86400)
        ak.set_budget(tmp_path, "3", budget)
        assert ak.state_of(tmp_path, "3").left(NOW) is None  # no estimate yet
        ak.record_spend(tmp_path, "3", 12.345678, budget.since, NOW)
        state = ak.state_of(tmp_path, "3")
        assert state.left(NOW) == pytest.approx(37.6543)
        assert state.blocked(NOW) == ""
        ak.record_spend(tmp_path, "3", 49.5, budget.since, NOW)
        assert ak.state_of(tmp_path, "3").blocked(NOW).startswith("budget nearly spent ($0.50")

    def test_a_new_top_up_clears_exhaustion_and_old_spend(self, tmp_path):
        ak.mark_exhausted(tmp_path, "3", ak.OUT_OF_CREDIT, NOW)
        ak.record_spend(tmp_path, "3", 10.0, 0.0, NOW)
        ak.set_budget(tmp_path, "3", ak.Budget("topup", 20.0, NOW))
        state = ak.state_of(tmp_path, "3")
        assert not state.exhausted and state.spent_usd is None

    def test_spend_for_another_period_is_ignored(self, tmp_path):
        ak.set_budget(tmp_path, "3", ak.Budget("monthly", 100.0))
        ak.record_spend(tmp_path, "3", 99.9, 0.0, NOW)  # not this month's start
        assert ak.state_of(tmp_path, "3").left(NOW) is None

    def test_monthly_period_starts_on_the_first(self):
        start = ak.Budget("monthly", 10.0).period_start(NOW)
        local = datetime.fromtimestamp(start).astimezone()
        assert (local.day, local.hour, local.minute) == (1, 0, 0)

    def test_forget_and_garbage_file(self, tmp_path):
        ak.mark_exhausted(tmp_path, "3", ak.OUT_OF_CREDIT, NOW)
        ak.forget(tmp_path, "3")
        assert ak.load(tmp_path) == {}
        (tmp_path / ak.STATE_FILENAME).write_text('{"3": [], "4": {"budget": {"kind": "x"}}}')
        assert ak.load(tmp_path) == {"4": ak.KeyState()}
        (tmp_path / ak.STATE_FILENAME).write_text("[1]")
        assert ak.load(tmp_path) == {}


class TestParseBudget:
    def test_forms(self):
        assert ak.parse_budget("50", now=NOW) == ak.Budget("topup", 50.0, NOW)
        assert ak.parse_budget(" $1,250 ", now=NOW) == ak.Budget("topup", 1250.0, NOW)
        assert ak.parse_budget("100/m") == ak.Budget("monthly", 100.0)
        assert ak.parse_budget("$100 /month") == ak.Budget("monthly", 100.0)
        assert ak.parse_budget("") is None

    @pytest.mark.parametrize("text", ["abc", "0", "-5", "nan", "inf", "$"])
    def test_rejects(self, text):
        with pytest.raises(ValueError):
            ak.parse_budget(text)

    def test_labels(self):
        assert ak.Budget("monthly", 100.0).label() == "$100/month"
        assert ak.Budget("topup", 50.0, NOW).label().startswith("$50 since ")
