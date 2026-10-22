"""Two profiles holding one account share its history, its readings, its
request budget and its forecast (``usage_history`` + the collector)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_rotater import oauth
from claude_rotater.estimator import estimate_fleet
from claude_rotater.forecast import fleet_estimates
from claude_rotater.usage_history import UsageHistory, shared_duty
from tests.test_autoswitch import EngineHarness, FakeClock, _usage

SHARED = ("s@example.com", "org-s")
OTHER = ("o@example.com", "org-o")
NOW = 1_800_000_000.0


def _u(five: float, seven: float = 10.0) -> dict:
    return {"five_hour": {"pct": five}, "seven_day": {"pct": seven}}


class TestLegacyMerge:
    def test_a_profiles_slot_keyed_file_is_merged_by_identity(self, tmp_path):
        legacy = tmp_path / "work" / "cache"
        legacy.mkdir(parents=True)
        (legacy / "usage_history.json").write_text(json.dumps({
            "schemaVersion": 1,
            "accounts": {"4": {"email": SHARED[0], "organizationUuid": SHARED[1],
                               "samples": [[NOW - 60, {"5h": [20.0, None]}]],
                               "attempts": [NOW - 60]}},
        }))
        (legacy / "usage_longterm.json").write_text(json.dumps({
            "schemaVersion": 1,
            "accounts": {f"{SHARED[0]}|{SHARED[1]}": {"w": {"5h": {"h0": int(NOW // 3600) - 5, "h": [150]}}}},
        }))
        shared = tmp_path / "shared"
        mine = UsageHistory(shared, clock=lambda: NOW)
        mine.record({"1": _u(21)}, {"1": SHARED}, NOW)

        merged = UsageHistory(shared, clock=lambda: NOW, legacy_dirs=(legacy,))

        assert [p.pct for p in merged.series({"1": SHARED})["1"]["5h"]] == [20.0, 21.0]
        assert merged.attempts("9", SHARED, 0) == [NOW - 60]
        assert merged.long_series(SHARED, "5h", 0)[0].pct == 15.0
        assert not (legacy / "usage_history.json").exists()
        assert (legacy / "usage_history.json.merged").exists()
        assert (legacy / "usage_longterm.json.merged").exists()
        # Done once: a second start finds nothing left to merge.
        UsageHistory(shared, clock=lambda: NOW, legacy_dirs=(legacy,))
        assert len(merged.series({"1": SHARED})["1"]["5h"]) == 2

    def test_a_version_1_file_in_place_is_rekeyed(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "usage_history.json").write_text(json.dumps({
            "schemaVersion": 1,
            "accounts": {"1": {"email": SHARED[0], "organizationUuid": SHARED[1],
                               "samples": [[NOW, {"5h": [30.0, None]}]], "attempts": []}},
        }))
        hist = UsageHistory(cache, clock=lambda: NOW, legacy_dirs=(cache,))
        assert hist.series({"7": SHARED})["7"]["5h"][0].pct == 30.0
        hist.record({"7": _u(31)}, {"7": SHARED}, NOW + 60)
        raw = json.loads((cache / "usage_history.json").read_text())
        assert raw["schemaVersion"] == 2
        assert list(raw["accounts"]) == [f"{SHARED[0]}|{SHARED[1]}"]
        assert (cache / "usage_history.json").exists()  # never renamed away


class TestLatest:
    def test_newest_reading_and_its_taker(self, tmp_path):
        hist = UsageHistory(tmp_path, clock=lambda: NOW)
        hist.record({"1": _u(40)}, {"1": SHARED}, NOW - 60, by="/a")
        hist.record({"4": _u(41)}, {"4": SHARED}, NOW, by="/b")
        hist.record({"1": _u(39)}, {"1": SHARED}, NOW - 120, by="/a")  # older: not latest
        latest = hist.latest({"1": SHARED, "2": OTHER})
        assert set(latest) == {"1"}
        assert latest["1"].t == NOW and latest["1"].by == "/b"
        assert latest["1"].usage["five_hour"]["pct"] == 41


class TestDuty:
    def test_shared_account_reads_the_same_from_both_profiles(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        hist = UsageHistory(tmp_path / "shared", clock=lambda: NOW)
        hist.set_duty(a, active=SHARED, since=NOW - 600, members=[SHARED, OTHER])
        hist.set_duty(b, active=OTHER, since=NOW - 300, members=[SHARED, OTHER, ("x@y", "")])
        duty = hist.duty()
        from_a = shared_duty(duty, {"1": SHARED, "2": OTHER, "3": ("only@a", "")}, a)
        from_b = shared_duty(duty, {"4": SHARED, "5": OTHER}, b)
        assert from_a == {"1": (True, NOW - 300), "2": (True, NOW - 300)}
        assert from_b == {"4": (True, NOW - 300), "5": (True, NOW - 300)}

    def test_unchanged_duty_is_not_rewritten(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        hist = UsageHistory(tmp_path / "shared", clock=lambda: NOW)
        hist.set_duty(a, active=SHARED, since=None, members=[SHARED])
        before = hist.path.stat().st_mtime_ns
        with patch("claude_rotater.usage_history.atomic_write_json") as write:
            hist.set_duty(a, active=SHARED, since=None, members=[SHARED])
        write.assert_not_called()
        assert hist.path.stat().st_mtime_ns == before

    def test_a_removed_profile_drops_out(self, tmp_path):
        a, gone = tmp_path / "a", tmp_path / "gone"
        a.mkdir()
        hist = UsageHistory(tmp_path / "shared", clock=lambda: NOW)
        gone.mkdir()
        hist.set_duty(gone, active=SHARED, since=None, members=[SHARED])
        gone.rmdir()
        hist.set_duty(a, active=SHARED, since=None, members=[SHARED])
        assert list(hist.duty()) == [str(a)]


class TestOneForecast:
    def test_both_profiles_forecast_the_shared_account_alike(self, tmp_path):
        """Personal holds it as #1 (on duty since long ago), work as #4 (on
        duty since a recent switch): one history, one forecast."""
        a, b = tmp_path / "personal", tmp_path / "work"
        a.mkdir(), b.mkdir()
        (a / "autoswitch_state.json").write_text(json.dumps({"lastSwitchAt": NOW - 86400}))
        (b / "autoswitch_state.json").write_text(json.dumps({"lastSwitchAt": NOW - 1800}))
        hist = UsageHistory(tmp_path / "shared", clock=lambda: NOW)
        for i in range(24):
            t = NOW - 7200 + i * 300
            hist.record({"1": _u(10 + i, 40 + i * 0.2)}, {"1": SHARED}, t, by=a)
        hist.set_duty(a, active=SHARED, since=NOW - 86400, members=[SHARED, OTHER])
        hist.set_duty(b, active=SHARED, since=NOW - 1800, members=[SHARED])

        est_a, _ = fleet_estimates(hist, a, {"1": SHARED, "2": OTHER},
                                   active="1", models=(), threshold=95.0, now=NOW)
        est_b, _ = fleet_estimates(hist, b, {"4": SHARED},
                                   active="4", models=(), threshold=95.0, now=NOW)
        assert est_a["1"] == est_b["4"]
        assert est_a["1"].binding is not None

    def test_without_sharing_the_profile_reads_its_own_switch(self):
        series = {"1": {}, "2": {}}
        assert estimate_fleet(series, active="1", models=(), threshold=95.0, now=NOW,
                              shared=None) == estimate_fleet(
            series, active="1", models=(), threshold=95.0, now=NOW)


# -- the collector: one poller per account across profiles -------------------


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """Two profiles (separate homes and data dirs) holding the same account,
    on one shared cache dir and one clock."""
    monkeypatch.setenv("ROTA_SHARED_HOME", str(tmp_path / "shared"))
    monkeypatch.setattr("claude_rotater.switcher._FETCH_STAGGER_S", 0)
    clock = FakeClock(NOW)
    harnesses = []
    for name in ("personal", "work"):
        home = tmp_path / name
        home.mkdir()
        h = EngineHarness(home)
        h.clock = clock
        h.switcher._usage_store.clock = clock
        h.seed(1, "s@example.com")  # idle in both: no live login to fake
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        harnesses.append(h)
    with patch("claude_rotater.oauth.fetch_oauth_profile", return_value=None):
        yield harnesses


def collect(h, pct: float) -> list[str]:
    """One on-demand collector pass with faked HTTP; returns slots fetched."""
    fetched: list[str] = []

    def fake(num, email, creds, is_active=False, persist_credentials=None, **kw):
        fetched.append(num)
        return oauth.UsageOutcome(_usage(pct))

    with patch("claude_rotater.oauth.try_fetch_usage_for_account", side_effect=fake):
        h.switcher.usage_entries_by_account(fetch={"1"}, scheduled=True)
    return fetched


def pct_of(h) -> float:
    ident = h.switcher.usage_identities()
    return h.switcher._usage_store.entries(ident)["1"].last_good["five_hour"]["pct"]


class TestOnePoller:
    def test_the_second_profile_adopts_instead_of_fetching(self, profiles):
        personal, work = profiles
        assert collect(personal, 40) == ["1"]
        assert collect(work, 99) == []  # the reading personal just took
        assert pct_of(work) == 40

    def test_only_the_profile_already_polling_keeps_polling(self, profiles):
        personal, work = profiles
        fetches = {"personal": 0, "work": 0}
        for step in range(30):  # 30 minutes, a pass each minute from both
            fetches["personal"] += len(collect(personal, 40 + step))
            fetches["work"] += len(collect(work, 40 + step))
            personal.clock.advance(60)
        assert fetches["work"] == 0
        assert 3 <= fetches["personal"] <= 7  # its own cadence (rising: ~5 min)
        assert pct_of(work) == pct_of(personal)

    def test_the_other_takes_over_when_the_poller_stops(self, profiles):
        personal, work = profiles
        collect(personal, 40)
        assert collect(work, 41) == []
        personal.clock.advance(15 * 60)  # personal closed: nobody renews
        assert collect(work, 42) == ["1"]
        assert pct_of(work) == 42

    def test_both_profiles_draw_on_one_attempt_ledger(self, profiles):
        personal, work = profiles
        collect(personal, 40)
        personal.clock.advance(15 * 60)
        collect(work, 41)
        ident = {"1": ("s@example.com", "")}
        for h in profiles:
            assert len(h.switcher.usage_history.attempts("1", ident["1"], 0)) == 2

    def test_each_profile_publishes_its_duty(self, profiles):
        personal, work = profiles
        collect(personal, 40)
        collect(work, 40)
        duty = personal.switcher.usage_history.duty()
        assert set(duty) == {str(personal.switcher.backup_dir), str(work.switcher.backup_dir)}
        assert all(r["members"] == ["s@example.com|"] for r in duty.values())


def test_shared_dir_ignores_rota_home(monkeypatch, tmp_path):
    from claude_rotater import paths

    monkeypatch.setenv("ROTA_HOME", str(tmp_path / "work"))
    monkeypatch.delenv("ROTA_SHARED_HOME", raising=False)
    assert paths.get_shared_cache_dir() == Path.home() / ".rota" / "cache"
    monkeypatch.setenv("ROTA_SHARED_HOME", str(tmp_path / "s"))
    assert paths.get_shared_cache_dir() == tmp_path / "s" / "cache"
