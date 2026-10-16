"""Proactive refresh of idle accounts (upstream #415)."""

from __future__ import annotations

import json
import threading

import pytest

from claude_swap import oauth
from claude_swap.oauth import RefreshOutcome
from claude_swap.token_refresh import (
    BUSY_RETRY_S,
    HELD_RECHECK_S,
    LEAD_S,
    MAX_SLEEP_S,
    RETRY_S,
    TokenRefresher,
    due_at,
)
from claude_swap.usage_store import UsageStore

NOW = 1_800_000_000.0


def creds(tag: str, *, expires_in: float, rt: bool = True, rt_expires_in: float | None = None) -> str:
    data = {"accessToken": f"at-{tag}", "expiresAt": int((NOW + expires_in) * 1000)}
    if rt:
        data["refreshToken"] = f"rt-{tag}"
    if rt_expires_in is not None:
        data["refreshTokenExpiresAt"] = int((NOW + rt_expires_in) * 1000)
    return json.dumps({"claudeAiOauth": data})


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSwitcher:
    """The slice of ClaudeAccountSwitcher the refresher reads."""

    def __init__(self, tmp_path, clock, accounts: dict[str, str], *, active: str = "1"):
        self.accounts = dict(accounts)  # num -> creds
        self.active = active
        self.kinds: dict[str, str] = {}
        self.held: set[str] = set()
        self.outcomes: dict[str, RefreshOutcome] = {}
        self.consumed: list[str] = []
        self.on_consume = None
        self._usage_store = UsageStore(tmp_path, clock=clock)

    def email(self, num: str) -> str:
        return f"u{num}@x.com"

    def current_account_number(self) -> str | None:
        return self.active

    def _build_accounts_info(self):
        return [
            (int(n), self.email(n), "", "org", n == self.active, c, "")
            for n, c in self.accounts.items()
        ]

    def _account_kind(self, num: str) -> str:
        return self.kinds.get(num, "oauth")

    def _live_session_pids(self, num: str, email: str) -> list[int]:
        return [123] if num in self.held else []

    def consume_backup_grant(self, num: str, email: str, snapshot: str) -> RefreshOutcome:
        assert email == self.email(num)
        self.consumed.append(num)
        if self.on_consume is not None:
            self.on_consume(num)
        outcome = self.outcomes.get(num)
        if outcome is None:
            fresh = creds(f"{num}-new", expires_in=8 * 3600)
            self.accounts[num] = fresh
            return RefreshOutcome(fresh, None, consumed_fp=oauth.credential_fingerprint(snapshot))
        return outcome


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, clock, accounts, **kw):
    sw = FakeSwitcher(tmp_path, clock, accounts, **kw)
    events = []
    ref = TokenRefresher(sw, on_event=events.append, clock=clock)
    return sw, ref, events


class TestDueAt:
    def test_lead_before_expiry(self):
        assert due_at(creds("a", expires_in=3600), now=NOW) == pytest.approx(NOW + 3600 - LEAD_S)

    @pytest.mark.parametrize("blob", [
        creds("a", expires_in=3600, rt=False),
        json.dumps({"claudeAiOauth": {"refreshToken": "r"}}),
        "sk-ant-api03-key",
        "not json",
    ])
    def test_cannot_refresh(self, blob):
        assert due_at(blob, now=NOW) is None

    def test_lapsed_login_is_not_refreshed(self):
        assert due_at(creds("a", expires_in=60, rt_expires_in=-1), now=NOW) is None
        assert due_at(creds("a", expires_in=60, rt_expires_in=86400), now=NOW) is not None


class TestTick:
    def test_refreshes_due_inactive_and_skips_active(self, tmp_path, clock):
        sw, ref, events = make(tmp_path, clock, {
            "1": creds("1", expires_in=60),          # active, due: never touched
            "2": creds("2", expires_in=LEAD_S - 1),  # due
            "3": creds("3", expires_in=4 * 3600),    # not due
        })
        next_at = ref.tick()
        assert sw.consumed == ["2"]
        assert [(e.kind, e.number) for e in events] == [("refreshed", "2")]
        # next wake: account 3's due time (earlier than 2's new one)
        assert next_at == pytest.approx(NOW + 4 * 3600 - LEAD_S)

    def test_nothing_due_returns_earliest_due(self, tmp_path, clock):
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=LEAD_S + 120)})
        assert ref.tick() == pytest.approx(NOW + 120)
        assert sw.consumed == []

    def test_nothing_refreshable_relists_after_max_sleep(self, tmp_path, clock):
        _sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=60, rt=False)})
        assert ref.tick() == pytest.approx(NOW + MAX_SLEEP_S)

    def test_loop_sleep_is_capped(self, tmp_path, clock, monkeypatch):
        _sw, ref, _ = make(tmp_path, clock, {"1": "x"})
        waits = []

        def fake_wait(timeout):
            waits.append(timeout)
            ref._stopping = True
            return False

        ref.tick = lambda: clock() + 10 * 3600
        monkeypatch.setattr(ref._wake, "wait", fake_wait)
        ref.run_loop()
        assert waits == [MAX_SLEEP_S]

    def test_becomes_due_as_time_passes(self, tmp_path, clock):
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=LEAD_S + 120)})
        ref.tick()
        clock.t += 121
        ref.tick()
        assert sw.consumed == ["2"]

    def test_held_by_live_session_is_skipped_and_rechecked(self, tmp_path, clock):
        sw, ref, events = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=0)})
        sw.held.add("2")
        assert ref.tick() == pytest.approx(NOW + HELD_RECHECK_S)
        assert sw.consumed == [] and events[0].kind == "held"
        clock.t += 60
        ref.tick()
        assert sw.consumed == []  # still inside the recheck window
        sw.held.clear()
        clock.t += HELD_RECHECK_S
        ref.tick()
        assert sw.consumed == ["2"]

    def test_account_that_became_active_since_listing_is_skipped(self, tmp_path, clock):
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=0)})
        listed = list(ref.candidates())
        sw.active = "2"  # a switch landed between listing and refreshing
        ref.candidates = lambda: iter(listed)
        ref.tick()
        assert sw.consumed == []

    def test_api_key_and_empty_accounts_skipped(self, tmp_path, clock):
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=0), "3": ""})
        sw.kinds["2"] = "api_key"
        ref.tick()
        assert sw.consumed == []

    def test_invalid_grant_strikes_once_and_never_retries_that_login(self, tmp_path, clock):
        dead = creds("2", expires_in=0)
        sw, ref, events = make(tmp_path, clock, {"1": "x", "2": dead})
        sw.outcomes["2"] = RefreshOutcome(None, "invalid_grant", consumed_fp=oauth.credential_fingerprint(dead))
        ref.tick()
        clock.t += 3 * MAX_SLEEP_S
        ref.tick()
        assert sw.consumed == ["2"]
        assert events[0].kind == "dead" and "re-login" in events[0].human()
        entry = sw._usage_store.entries({"2": ("u2@x.com", "org")})["2"]
        assert entry.token_dead(stored_fp=oauth.credential_fingerprint(dead))

    def test_struck_in_store_is_skipped_by_a_new_refresher(self, tmp_path, clock):
        dead = creds("2", expires_in=0)
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": dead})
        sw.outcomes["2"] = RefreshOutcome(None, "invalid_grant", consumed_fp=oauth.credential_fingerprint(dead))
        ref.tick()
        fresh_process = TokenRefresher(sw, clock=clock)
        fresh_process.tick()
        assert sw.consumed == ["2"]

    def test_relogin_makes_a_dead_account_eligible_again(self, tmp_path, clock):
        dead = creds("2", expires_in=0)
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": dead})
        sw.outcomes["2"] = RefreshOutcome(None, "invalid_grant", consumed_fp=oauth.credential_fingerprint(dead))
        ref.tick()
        del sw.outcomes["2"]
        sw.accounts["2"] = creds("2-relogin", expires_in=0)
        ref.tick()
        assert sw.consumed == ["2", "2"]

    @pytest.mark.parametrize("error, wait", [("transient", RETRY_S), ("consume-busy", BUSY_RETRY_S)])
    def test_transient_failure_backs_off(self, tmp_path, clock, error, wait):
        sw, ref, events = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=0)})
        sw.outcomes["2"] = RefreshOutcome(None, error)
        assert ref.tick() == pytest.approx(NOW + wait)
        ref.tick()
        assert sw.consumed == ["2"]
        assert events[0].kind == "failed"
        clock.t += wait
        ref.tick()
        assert sw.consumed == ["2", "2"]

    def test_disabled_accounts_are_still_kept_warm(self, tmp_path, clock):
        # Disabled means out of rotation, not abandoned: nothing filters it.
        sw, ref, _ = make(tmp_path, clock, {"1": "x", "2": creds("2", expires_in=0)})
        ref.tick()
        assert sw.consumed == ["2"]


class TestLoop:
    def test_run_loop_stops_promptly_and_survives_a_bad_pass(self, tmp_path, clock):
        sw, ref, _ = make(tmp_path, clock, {"1": "x"})
        passes = []

        def bad_tick():
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("boom")
            ref.stop()
            return clock() + 10_000

        ref.tick = bad_tick
        t = threading.Thread(target=ref.run_loop)
        t.start()
        ref.wake()  # cut the post-failure sleep short
        t.join(5)
        assert not t.is_alive()
        assert len(passes) == 2

    def test_stop_interrupts_sleep(self, tmp_path, clock):
        sw, ref, _ = make(tmp_path, clock, {"1": "x"})
        t = threading.Thread(target=ref.run_loop)
        t.start()
        ref.stop()
        t.join(5)
        assert not t.is_alive()


def real_creds(tag: str, expires_in: float) -> str:
    """Like ``creds`` but relative to the wall clock (the real switcher's)."""
    import time

    return creds(tag, expires_in=time.time() - NOW + expires_in)


class TestRealSwitcher:
    """The refresher over a real ClaudeAccountSwitcher: only the token POST is faked."""

    @pytest.fixture
    def real(self, temp_home, mock_claude_config, tmp_path, monkeypatch):
        from unittest.mock import patch

        from claude_swap.models import Platform
        from claude_swap.switcher import ClaudeAccountSwitcher

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._init_sequence_file()
        live = real_creds("live", 60)
        with patch.object(s, "_read_capture_credentials", return_value=live):
            s.add_account()
        stage = tmp_path / "stage"
        stage.mkdir()
        (stage / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": "idle@x.com", "organizationUuid": "", "accountUuid": "u-idle"}}))
        (stage / (".credentials" + ".json")).write_text(real_creds("idle", -60))
        monkeypatch.setattr("claude_swap.session.Platform.detect", lambda: Platform.LINUX)
        s.add_account(source_dir=str(stage))
        return s, live

    def test_refreshes_idle_slot_and_never_the_active_one(self, real, monkeypatch):
        s, live = real
        posted = []

        def fake_post(c, timeout_s=10.0):
            posted.append(json.loads(c)["claudeAiOauth"]["refreshToken"])
            return RefreshOutcome(real_creds("idle-new", 8 * 3600), None)

        monkeypatch.setattr("claude_swap.oauth.try_refresh_oauth_credentials", fake_post)
        TokenRefresher(s).tick()
        assert posted == ["rt-idle"]
        assert "at-idle-new" in s._read_account_credentials("2", "idle@x.com")
        assert s._read_account_credentials("1", "test@example.com") == live

    def test_invalid_grant_marks_slot_dead(self, real, monkeypatch):
        s, _ = real
        monkeypatch.setattr(
            "claude_swap.oauth.try_refresh_oauth_credentials",
            lambda c, timeout_s=10.0: RefreshOutcome(None, "invalid_grant"),
        )
        TokenRefresher(s).tick()
        org = s._get_sequence_data()["accounts"]["2"].get("organizationUuid", "")
        entry = s._usage_store.entries({"2": ("idle@x.com", org)})["2"]
        assert entry.token_dead()  # struck: every surface shows "re-login needed"


def test_login_expires_at_epoch():
    assert oauth.login_expires_at(creds("a", expires_in=0, rt_expires_in=100)) == pytest.approx(NOW + 100)
    assert oauth.login_expires_at(creds("a", expires_in=0)) is None
    assert oauth.login_expires_at("garbage") is None
