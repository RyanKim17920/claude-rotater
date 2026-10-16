"""Attribution and ingestion of statusline feed records."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_rotater import feed, oauth, poll_policy
from claude_rotater.feed_ingest import (
    ACCEPTED,
    ACCEPTED_SESSION,
    HISTORY_MIN_GAP_S,
    SWITCH_GRACE_S,
    FeedIngestor,
    attribute,
    merged_usage,
    read_last_switch_at,
    reading_time,
)
from tests.test_autoswitch import EngineHarness, _iso_at, _usage

NOW = 1_000_000.0
HOME = "/home/u/.claude"
SESSIONS = Path("/backup/sessions")
IDS = {"1": ("a@example.com", ""), "2": ("b@example.com", "")}
R5 = NOW + 3600.0  # account 1's 5h reset
R7 = NOW + 5 * 86400.0


def rec(t=NOW, pct5=40.0, reset5=R5, pct7=10.0, reset7=R7, config_dir=HOME):
    record = {"t": t, "config_dir": config_dir}
    if pct5 is not None:
        record["five_hour"] = {"pct": pct5, **({"resets_at": reset5} if reset5 else {})}
    if pct7 is not None:
        record["seven_day"] = {"pct": pct7, **({"resets_at": reset7} if reset7 else {})}
    return record


def stored_usage(pct5=30.0, reset5=R5, pct7=10.0, reset7=R7):
    return {
        "five_hour": {"pct": pct5, "resets_at": _iso_at(reset5)},
        "seven_day": {"pct": pct7, "resets_at": _iso_at(reset7)},
    }


def verdict(record, **overrides):
    kwargs = dict(
        now=NOW,
        stale_s=600.0,
        active_slot="1",
        config_home=HOME,
        sessions_dir=SESSIONS,
        identities=IDS,
        last_switch_at=None,
        stored={"1": stored_usage(), "2": None},
    )
    kwargs.update(overrides)
    return attribute(record, **kwargs)


class TestAttribute:
    def test_consistent_record_goes_to_the_active_account(self):
        v = verdict(rec())
        assert (v.slot, v.reason) == ("1", ACCEPTED)

    def test_nothing_stored_still_accepted(self):
        v = verdict(rec(), stored={"1": None, "2": None})
        assert v.slot == "1"

    @pytest.mark.parametrize("t, reason", [
        (NOW - 601, "stale"),
        (NOW + 61, "future"),
    ])
    def test_time_bounds(self, t, reason):
        assert verdict(rec(t=t)).reason == reason

    def test_stale_bound_follows_the_setting(self):
        assert verdict(rec(t=NOW - 200), stale_s=120.0).reason == "stale"

    def test_other_profile_is_rejected(self):
        v = verdict(rec(config_dir="/home/u/.claude-other"))
        assert (v.slot, v.reason) == (None, "foreign-profile")

    def test_trailing_slash_and_dot_segments_still_match(self):
        assert verdict(rec(config_dir=HOME + "/")).slot == "1"
        assert verdict(rec(config_dir="/home/u/x/../.claude")).slot == "1"

    def test_missing_config_dir_is_the_active_profile(self):
        record = rec()
        del record["config_dir"]
        assert verdict(record).slot == "1"

    def test_no_active_account(self):
        assert verdict(rec(), active_slot=None).reason == "no-active"

    def test_unmanaged_active_slot(self):
        assert verdict(rec(), active_slot="9").reason == "no-active"

    def test_switch_grace(self):
        assert verdict(rec(t=NOW), last_switch_at=NOW - 10).reason == "switch-grace"
        # Stamped before the switch: the previous account's reading.
        assert verdict(rec(t=NOW - 100), last_switch_at=NOW - 50).reason == "switch-grace"
        ok = verdict(rec(t=NOW), last_switch_at=NOW - SWITCH_GRACE_S - 1)
        assert ok.slot == "1"

    def test_window_mismatch_means_another_account(self):
        # An idle session still showing the previous account's limits long
        # after the switch: its 5h reset disagrees with account 1's.
        v = verdict(rec(reset5=R5 + 1800), last_switch_at=NOW - 3600)
        assert v.reason == "window-mismatch"

    def test_reset_jitter_is_tolerated(self):
        assert verdict(rec(reset5=R5 + 90)).slot == "1"

    def test_stored_window_already_ended_allows_a_new_reset(self):
        stored = {"1": stored_usage(reset5=NOW - 10), "2": None}
        assert verdict(rec(reset5=NOW + 5 * 3600), stored=stored).slot == "1"

    def test_behind_stored_is_a_stale_report(self):
        stored = {"1": stored_usage(pct5=60.0), "2": None}
        assert verdict(rec(pct5=50.0), stored=stored).reason == "behind-stored"
        # Within the tolerance: accepted (the merge keeps the higher pct).
        assert verdict(rec(pct5=59.0), stored=stored).slot == "1"

    def test_matches_other_account_when_active_unknown(self):
        stored = {"1": None, "2": stored_usage()}
        v = verdict(rec(), stored=stored)
        assert v.reason == "matches-other-account"

    def test_own_match_beats_a_coincident_other(self):
        stored = {"1": stored_usage(), "2": stored_usage()}
        assert verdict(rec(), stored=stored).slot == "1"

    def test_records_without_resets_cannot_conflict(self):
        record = rec(reset5=None, reset7=None)
        stored = {"1": None, "2": stored_usage()}
        assert verdict(record, stored=stored).slot == "1"

    def test_windows_that_already_reset_are_ignored(self):
        v = verdict(rec(reset5=NOW - 5, pct7=None))
        assert v.reason == "window-ended"
        # One live window is enough.
        assert verdict(rec(reset5=NOW - 5)).slot == "1"

    def test_rota_run_session_goes_to_its_slot(self):
        record = rec(config_dir=str(SESSIONS / "2-b_example.com"), reset5=R5 + 7200)
        v = verdict(record, last_switch_at=NOW - 1)  # no grace: never switches
        assert (v.slot, v.reason) == ("2", ACCEPTED_SESSION)

    @pytest.mark.parametrize("name", ["2-other_example.com", "9-a_example.com", "x"])
    def test_unknown_session_dir(self, name):
        v = verdict(rec(config_dir=str(SESSIONS / name)))
        assert v.reason == "unknown-session"

    def test_session_record_still_checked_for_consistency(self):
        record = rec(config_dir=str(SESSIONS / "2-b_example.com"), reset5=R5 + 1800)
        stored = {"1": None, "2": stored_usage()}
        assert verdict(record, stored=stored).reason == "window-mismatch"


class TestMergedUsage:
    def test_overlays_windows_and_keeps_the_rest(self):
        stored = stored_usage()
        stored["seven_day_opus"] = {"pct": 77.0}
        stored["extra_usage"] = {"pct": 5.0}
        usage = merged_usage(stored, rec(pct5=45.0, pct7=12.0))
        assert usage["five_hour"]["pct"] == 45.0
        assert usage["seven_day"]["pct"] == 12.0
        assert usage["seven_day_opus"] == {"pct": 77.0}
        assert usage["extra_usage"] == {"pct": 5.0}
        assert poll_policy.parse_reset_ts(usage["five_hour"]["resets_at"]) == R5
        assert "countdown" in usage["five_hour"] and "clock" in usage["five_hour"]
        # The shape the decision code reads.
        assert oauth.account_headroom(usage, ()) == 55.0

    def test_same_window_keeps_the_higher_pct(self):
        usage = merged_usage(stored_usage(pct5=41.0), rec(pct5=40.0))
        assert usage["five_hour"]["pct"] == 41.0

    def test_new_window_takes_the_record(self):
        usage = merged_usage(stored_usage(pct5=90.0, reset5=NOW - 10), rec(pct5=3.0))
        assert usage["five_hour"]["pct"] == 3.0

    def test_ended_window_leaves_stored(self):
        stored = stored_usage()
        usage = merged_usage(stored, rec(reset5=NOW - 1, pct5=99.0))
        assert usage["five_hour"] == stored["five_hour"]

    def test_nothing_stored(self):
        usage = merged_usage(None, rec(pct7=None, reset5=None))
        assert usage == {"five_hour": {"pct": 40.0}}


def test_read_last_switch_at(tmp_path):
    assert read_last_switch_at(tmp_path) is None
    (tmp_path / "autoswitch_state.json").write_text("{bad")
    assert read_last_switch_at(tmp_path) is None
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({"lastSwitchAt": True}))
    assert read_last_switch_at(tmp_path) is None
    (tmp_path / "autoswitch_state.json").write_text(json.dumps({"lastSwitchAt": 12.5}))
    assert read_last_switch_at(tmp_path) == 12.5


# -- through the real store ------------------------------------------------------

ID1 = ("a@example.com", "")


@pytest.fixture
def h(temp_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("claude_rotater.switcher._FETCH_STAGGER_S", 0)
    harness = EngineHarness(temp_home)
    harness.seed(1, "a@example.com")
    harness.seed(2, "b@example.com")
    harness.make_live("a@example.com", 1)
    monkeypatch.setattr(harness.switcher, "_live_session_pids", lambda *a: [])
    with patch("claude_rotater.oauth.fetch_oauth_profile", return_value=None):
        yield harness


def write(h, **kw):
    kw.setdefault("t", h.clock.now)
    kw.setdefault("config_dir", str(h.temp_home / ".claude"))
    # Fixed windows (FakeClock starts at NOW): one account's resets do not
    # move between refreshes.
    kw.setdefault("reset5", NOW + 4 * 3600)
    kw.setdefault("reset7", NOW + 5 * 86400)
    feed.append_record(feed.feed_path(h.switcher.backup_dir), rec(**kw))


def entry(h, num="1"):
    return h.switcher._usage_store.entries(h.switcher.usage_identities())[num]


def polled(h, *nums):
    """Stamp a real poll of ``nums`` (what holds are capped against)."""
    collect(h, {n: _usage(10) for n in nums}, set(nums))


def collect(h, usage_by_num, fetch):
    fetched: list[str] = []

    def fake(num, email, creds, is_active=False, persist_credentials=None, **kw):
        fetched.append(num)
        return oauth.UsageOutcome(dict(usage_by_num[num]))

    with patch("claude_rotater.oauth.try_fetch_usage_for_account", side_effect=fake):
        h.switcher.usage_entries_by_account(fetch=set(fetch), scheduled=True)
    return fetched


class TestIngest:
    def test_adopts_holds_and_records_history(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        h.clock.advance(1)
        write(h, pct5=42.0)
        result = ing.ingest()
        assert result.adopted == {"1": h.clock.now}
        e = entry(h)
        assert e.last_good["five_hour"]["pct"] == 42.0
        assert e.fetched_at == h.clock.now
        # Capped at the spare cadence after the poll a second ago.
        assert e.held_until == pytest.approx(h.clock.now - 1 + poll_policy.SPARE_INTERVAL_S)
        assert e.decision_value() is not None
        series = h.switcher.usage_history.series({"1": ID1})["1"]
        assert [p.pct for p in series["5h"]][-1] == 42.0
        # Nothing new: nothing done.
        assert not ing.ingest().changed

    def test_fresh_feed_still_gets_a_ground_truth_poll_on_the_spare_cadence(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        usage = {"1": _usage(10), "2": _usage(10)}
        assert sorted(collect(h, usage, {"1", "2"})) == ["1", "2"]
        start = h.clock.now
        polls = []
        for _ in range(30):  # a busy session refreshes the feed every minute
            h.clock.advance(60)
            write(h, pct5=20.0 + len(polls))
            ing.ingest()
            if "1" in collect(h, usage, {"1"}):
                polls.append(h.clock.now - start)
        spare = poll_policy.SPARE_INTERVAL_S
        # Not more often than the spare cadence (jitter aside), but never
        # starved by the feed either.
        assert len(polls) >= 2
        gaps = [b - a for a, b in zip([0.0] + polls, polls)]
        assert all(gap >= spare * (1 - poll_policy.JITTER_FRAC) - 60 for gap in gaps)
        assert all(gap <= spare * (1 + poll_policy.JITTER_FRAC) + 60 for gap in gaps)

    def test_never_polled_account_gets_its_first_poll_despite_the_feed(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        write(h, pct5=20.0)
        ing.ingest()
        e = entry(h)
        assert e.held_until is None
        assert e.next_poll_at == h.clock.now
        assert collect(h, {"1": _usage(10)}, {"1"}) == ["1"]

    def test_hold_is_capped_at_the_spare_cadence_after_the_last_poll(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        h.clock.advance(400)
        write(h)
        ing.ingest()
        assert entry(h).held_until == pytest.approx(
            h.clock.now - 400 + poll_policy.SPARE_INTERVAL_S
        )

    def test_stale_feed_falls_back_to_polling(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        usage = {"1": _usage(10), "2": _usage(10)}
        collect(h, usage, {"1"})
        write(h, pct5=20.0)
        ing.ingest()
        h.clock.advance(poll_policy.FEED_STALE_S - 1)
        assert collect(h, usage, {"1"}) == []
        h.clock.advance(2)  # feed silent past feedStaleSeconds
        assert collect(h, usage, {"1"}) == ["1"]

    def test_feed_stale_setting_shortens_the_hold(self, h):
        (h.switcher.backup_dir / "settings.json").write_text(
            json.dumps({"autoswitch": {"feedStaleSeconds": 120}})
        )
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        write(h)
        ing.ingest()
        assert entry(h).held_until == pytest.approx(h.clock.now + 120)

    def test_late_ingest_holds_only_the_remainder(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        write(h)
        h.clock.advance(100)
        ing.ingest()
        e = entry(h)
        assert e.fetched_at == h.clock.now - 100
        assert e.held_until == pytest.approx(h.clock.now - 100 + poll_policy.FEED_STALE_S)

    def test_newest_accepted_record_wins(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        write(h, t=h.clock.now - 20, pct5=30.0)
        write(h, t=h.clock.now - 10, pct5=35.0)
        write(h, t=h.clock.now - 5, config_dir="/elsewhere", pct5=99.0)
        result = ing.ingest()
        assert result.adopted == {"1": h.clock.now - 10}
        assert result.rejected == {"foreign-profile": 1}
        assert entry(h).last_good["five_hour"]["pct"] == 35.0

    def test_does_not_downgrade_a_newer_poll(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        write(h, t=h.clock.now - 30, pct5=10.0)
        collect(h, {"1": _usage(50, _iso_at(h.clock.now + 3600))}, {"1"})
        ing.ingest()
        assert entry(h).last_good["five_hour"]["pct"] == 50.0

    def test_after_a_switch_old_session_reports_are_dropped(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        (h.switcher.backup_dir / "autoswitch_state.json").write_text(
            json.dumps({"lastSwitchAt": h.clock.now - 5})
        )
        write(h, pct5=88.0)
        result = ing.ingest()
        assert result.rejected == {"switch-grace": 1}
        assert entry(h).last_good is None

    def test_rota_run_session_feeds_its_own_slot(self, h):
        from claude_rotater.session import session_dir_for

        ing = FeedIngestor(h.switcher, clock=h.clock)
        session = session_dir_for(h.switcher.backup_dir, "2", "b@example.com")
        write(h, config_dir=str(session), pct5=12.0)
        assert ing.ingest().adopted == {"2": h.clock.now}
        assert entry(h, "2").last_good["five_hour"]["pct"] == 12.0
        assert entry(h, "1").last_good is None

    def test_history_is_throttled_for_unchanged_readings(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        for _ in range(5):
            write(h, pct5=40.0)
            ing.ingest()
            h.clock.advance(10)
        series = h.switcher.usage_history.series({"1": ID1})["1"]["5h"]
        assert len(series) == 1
        h.clock.advance(HISTORY_MIN_GAP_S)
        write(h, pct5=40.0)
        ing.ingest()
        write(h, pct5=41.0)
        h.clock.advance(1)
        write(h, pct5=42.0)
        ing.ingest()
        series = h.switcher.usage_history.series({"1": ID1})["1"]["5h"]
        assert [p.pct for p in series] == [40.0, 40.0, 42.0]

    def test_garbage_and_missing_file_are_harmless(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        assert not ing.ingest().changed  # no file yet
        path = feed.feed_path(h.switcher.backup_dir)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"garbage\n\x00\xff\n")
        assert not ing.ingest().changed

    def test_store_failure_is_swallowed(self, h, monkeypatch):
        ing = FeedIngestor(h.switcher, clock=h.clock)

        def boom(*a, **k):
            raise RuntimeError("store broken")

        monkeypatch.setattr(h.switcher._usage_store, "adopt", boom)
        write(h)
        assert not ing.ingest().changed


# -- staleness: re-renders, divergent sessions, reading time ------------------------


def swrite(h, session, at, **kw):
    """A record from chat ``session`` whose last response was at ``at``."""
    kw.setdefault("t", h.clock.now)
    kw.setdefault("config_dir", str(h.temp_home / ".claude"))
    kw.setdefault("reset5", NOW + 4 * 3600)
    kw.setdefault("reset7", NOW + 5 * 86400)
    record = rec(**kw)
    record["session_id"] = session
    if at is not None:
        record["at"] = at
    feed.append_record(feed.feed_path(h.switcher.backup_dir), record)


class TestStaleness:
    def test_reading_time_prefers_the_last_response_but_never_the_future(self):
        assert reading_time({"t": 100.0, "at": 40.0}) == 40.0
        assert reading_time({"t": 100.0, "at": 140.0}) == 100.0
        assert reading_time({"t": 100.0}) == 100.0
        assert reading_time({"t": 100.0, "at": True}) == 100.0

    def test_idle_chat_rerendering_old_numbers_is_stale(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        # Rendered just now, but its last response was 20 minutes ago.
        swrite(h, "idle", at=h.clock.now - 1200, pct5=30.0)
        result = ing.ingest()
        assert result.rejected == {"stale": 1}
        assert entry(h).last_good is None

    def test_two_chats_diverge_the_higher_reading_wins(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        h.clock.advance(10)
        # The busy chat answered 5 s ago at 50%; the quieter one, rendered
        # later, still shows its response from 2 minutes ago at 49%.
        swrite(h, "busy", at=h.clock.now - 5, pct5=50.0)
        swrite(h, "quiet", at=h.clock.now - 120, pct5=49.0)
        result = ing.ingest()
        assert result.adopted == {"1": h.clock.now - 5}
        assert entry(h).last_good["five_hour"]["pct"] == 50.0
        # The quiet chat renders again, still behind: it never pulls the
        # account's number down.
        h.clock.advance(30)
        swrite(h, "quiet", at=h.clock.now - 60, pct5=49.5)
        ing.ingest()
        assert entry(h).last_good["five_hour"]["pct"] == 50.0
        # Older than what the store holds: it does not make it look fresher.
        assert entry(h).fetched_at == pytest.approx(h.clock.now - 35)

    def test_far_behind_chat_is_dropped(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        h.clock.advance(10)
        swrite(h, "busy", at=h.clock.now - 5, pct5=50.0)
        ing.ingest()
        swrite(h, "quiet", at=h.clock.now - 2, pct5=30.0)
        assert ing.ingest().rejected == {"behind-stored": 1}
        assert entry(h).last_good["five_hour"]["pct"] == 50.0

    def test_rerenders_are_dropped_until_the_values_change(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        polled(h, "1")
        at = h.clock.now + 1
        h.clock.advance(5)
        for _ in range(3):
            swrite(h, "s", at=at, pct5=40.0)
            h.clock.advance(5)
        result = ing.ingest()
        assert result.adopted == {"1": at}
        assert result.rejected == {"rerender": 2}
        # Same numbers from a new response: news (it renews freshness).
        swrite(h, "s", at=h.clock.now, pct5=40.0)
        assert ing.ingest().adopted == {"1": h.clock.now}
        # Re-render across passes is still recognized.
        h.clock.advance(5)
        swrite(h, "s", at=h.clock.now - 5, pct5=40.0)
        assert ing.ingest().rejected == {"rerender": 1}

    def test_rerender_without_a_transcript_time_is_dropped_too(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        swrite(h, "s", at=None, pct5=40.0)
        h.clock.advance(5)
        swrite(h, "s", at=None, pct5=40.0)
        assert ing.ingest().rejected == {"rerender": 1}

    def test_session_memory_is_bounded(self, h):
        from claude_rotater.feed_ingest import SESSION_MEMORY

        ing = FeedIngestor(h.switcher, clock=h.clock)
        for i in range(SESSION_MEMORY + 10):
            ing._rerender({"session_id": f"s{i}", "t": 1.0})
        assert len(ing._sessions) == SESSION_MEMORY
        assert "s0" not in ing._sessions

    def test_window_reset_takes_the_new_low_reading(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        reset = h.clock.now + 600
        swrite(h, "s", at=h.clock.now, pct5=95.0, reset5=reset)
        ing.ingest()
        assert entry(h).last_good["five_hour"]["pct"] == 95.0
        h.clock.advance(700)  # the 5h window rolled over
        swrite(h, "s", at=h.clock.now - 10, pct5=2.0, reset5=reset + 5 * 3600)
        result = ing.ingest()
        assert result.adopted == {"1": h.clock.now - 10}
        usage = entry(h).last_good["five_hour"]
        assert usage["pct"] == 2.0
        assert poll_policy.parse_reset_ts(usage["resets_at"]) == reset + 5 * 3600

    def test_rerenders_add_no_history(self, h):
        ing = FeedIngestor(h.switcher, clock=h.clock)
        at = h.clock.now
        for _ in range(5):
            swrite(h, "s", at=at, pct5=40.0)
            h.clock.advance(HISTORY_MIN_GAP_S + 1)
            ing.ingest()
        series = h.switcher.usage_history.series({"1": ID1})["1"]["5h"]
        assert [p.t for p in series] == [at]
