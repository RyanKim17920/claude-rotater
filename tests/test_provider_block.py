"""Telling a provider-refused account from every other failure, and the
shared store that holds the verdict."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_rotater import provider_block as pb

NOW = 1_800_000_000.0


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def api_error(text: str, *, error: str | None = None, status: int | None = None,
              type_: str | None = None, code: str | None = None, at: float = NOW) -> dict:
    entry = {
        "type": "assistant", "isApiErrorMessage": True, "timestamp": _iso(at),
        "message": {"model": "<synthetic>", "role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    }
    for key, value in (("error", error), ("apiErrorStatus", status),
                       ("apiErrorType", type_), ("apiErrorCode", code)):
        if value is not None:
            entry[key] = value
    return entry


def turn(at: float = NOW, model: str = "some-model") -> dict:
    return {"type": "assistant", "timestamp": _iso(at),
            "message": {"model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "hello"}]}}


# The shapes seen in the owner's transcripts (texts as written, product
# names generalised).
ORG_DISABLED = api_error(
    "Your organization has disabled subscription access for the CLI · Use an API key "
    "instead, or ask your admin to enable access",
    error="oauth_org_not_allowed", status=403, type_="permission_error",
    code="oauth_not_allowed_for_organization",
)
PAST_DUE = api_error(
    "The CLI can't use your subscription, usually because a payment is past due · Pay "
    "or renew at the billing page (if you just paid, try again shortly)",
    error="oauth_org_not_allowed", status=403, code="oauth_not_allowed_for_organization",
)


class TestClassify:
    def test_org_disabled(self):
        assert pb.classify(ORG_DISABLED) == pb.ORG_DISABLED

    def test_payment_past_due(self):
        assert pb.classify(PAST_DUE) == pb.PAST_DUE

    def test_refusal_code_with_new_wording_is_still_a_block(self):
        entry = api_error("Something new", error="oauth_org_not_allowed", status=403)
        assert pb.classify(entry) == pb.REFUSED

    def test_suspension_wording_on_a_permission_error(self):
        entry = api_error("API Error: 403 Your account has been suspended.", status=403,
                          type_="permission_error")
        assert pb.classify(entry) == pb.SUSPENDED

    def test_a_working_turn(self):
        assert pb.classify(turn()) == pb.WORKING

    @pytest.mark.parametrize("entry", [
        api_error("Login expired · Please run /login", error="authentication_failed"),
        api_error("Not logged in · Please run /login", error="authentication_failed"),
        api_error("API Error: 401 OAuth token has expired", status=401,
                  type_="authentication_error"),
        api_error("You've hit your session limit · resets 3am", error="rate_limit", status=429),
        api_error("You've hit your weekly limit · resets Oct 14", error="rate_limit", status=429),
        api_error("API Error: 529 Overloaded.", error="server_error", status=529),
        api_error("API Error: 500 Internal server error.", error="server_error", status=500),
        api_error("API Error: Can't reach the API server (ENOTFOUND)", error="server_error"),
        api_error("API Error: Connection lost mid-response.", error="server_error"),
        api_error("API Error: the safeguards flagged this session", error="invalid_request"),
        api_error("Your credit balance is too low", error="billing_error", status=400),
        # A token without the right scope gets a bare 403 permission error.
        api_error("API Error: 403 OAuth token does not meet scope requirement user:profile",
                  status=403, type_="permission_error"),
        turn(model="<synthetic>"),
        {"type": "user", "message": {"content": "my account was suspended?"}},
        "not a dict",
    ])
    def test_everything_else_is_not_a_block(self, entry):
        assert pb.classify(entry) == ""


def write_transcript(config_dir: Path, entries: list[dict], name: str = "s.jsonl",
                     mtime: float | None = NOW + 60) -> Path:
    path = config_dir / "projects" / "-tmp-proj" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestScan:
    def test_newest_verdict_wins(self, tmp_path):
        write_transcript(tmp_path, [turn(NOW - 60), ORG_DISABLED])
        assert pb.scan(tmp_path, NOW - 3600) == (pb.ORG_DISABLED, NOW)

    def test_a_later_working_turn_means_it_works(self, tmp_path):
        write_transcript(tmp_path, [ORG_DISABLED, turn(NOW + 30)])
        assert pb.scan(tmp_path, NOW - 3600) == (pb.WORKING, NOW + 30)

    def test_entries_before_since_are_not_this_account(self, tmp_path):
        write_transcript(tmp_path, [ORG_DISABLED])
        assert pb.scan(tmp_path, NOW + 1) is None

    def test_newest_across_files(self, tmp_path):
        write_transcript(tmp_path, [turn(NOW - 100)], name="a.jsonl")
        write_transcript(tmp_path, [PAST_DUE], name="b.jsonl")
        assert pb.scan(tmp_path, NOW - 3600)[0] == pb.PAST_DUE

    def test_other_errors_are_ignored(self, tmp_path):
        write_transcript(tmp_path, [
            api_error("Login expired · Please run /login", error="authentication_failed"),
            api_error("You've hit your session limit", error="rate_limit", status=429),
        ])
        assert pb.scan(tmp_path, NOW - 3600) is None

    def test_no_transcripts(self, tmp_path):
        assert pb.scan(tmp_path / "nowhere", 0.0) is None


class TestUsageRefusal:
    def test_needs_repeats_over_time(self):
        assert not pb.usage_refusal(2, NOW - 7200, NOW)
        assert not pb.usage_refusal(3, NOW - 60, NOW)
        assert pb.usage_refusal(3, NOW - pb.USAGE_SPAN_S, NOW)
        assert not pb.usage_refusal(5, None, NOW)


class TestStore:
    ID = ("r@example.com", "org-1")

    def test_mark_load_clear(self, tmp_path):
        path = tmp_path / "blocks.json"
        assert pb.block_of(self.ID, path=path) is None
        assert pb.mark(self.ID, pb.ORG_DISABLED, pb.SOURCE_TRANSCRIPT, NOW, path=path)
        assert pb.block_of(self.ID, path=path) == pb.Block(pb.ORG_DISABLED, "transcript", NOW)
        assert not pb.mark(self.ID, pb.ORG_DISABLED, pb.SOURCE_TRANSCRIPT, NOW + 5, path=path)
        assert pb.clear(self.ID, path=path)
        assert not pb.clear(self.ID, path=path)
        assert pb.load(path) == {}

    def test_usage_signal_never_rewrites_a_known_block(self, tmp_path):
        path = tmp_path / "blocks.json"
        pb.mark(self.ID, pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, NOW, path=path)
        assert not pb.mark(self.ID, pb.USAGE_REFUSED, pb.SOURCE_USAGE, NOW + 9, path=path)
        assert pb.block_of(self.ID, path=path).reason == pb.PAST_DUE

    def test_kept_by_identity_not_slot(self, tmp_path):
        path = tmp_path / "blocks.json"
        pb.mark(self.ID, pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, NOW, path=path)
        assert pb.block_of(("r@example.com", "org-2"), path=path) is None

    def test_default_path_is_the_shared_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROTA_SHARED_HOME", str(tmp_path / "shared"))
        pb.mark(self.ID, pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, NOW)
        assert (tmp_path / "shared" / "cache" / pb.STORE_FILENAME).exists()

    def test_corrupt_store_reads_empty(self, tmp_path):
        path = tmp_path / "blocks.json"
        path.write_text("{not json")
        assert pb.load(path) == {}
        assert pb.mark(self.ID, pb.PAST_DUE, pb.SOURCE_TRANSCRIPT, NOW, path=path)
