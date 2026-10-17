"""Tests for the expiring-first strategy: the pure policy and its engine wiring."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from claude_rotater import expiring_first as ef
from claude_rotater.autoswitch import (
    Decision,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    decision_from_event,
    note_manual_switch,
)
from claude_rotater.settings import (
    SETTING_GROUPS,
    SETTING_SPECS,
    AutoSwitchSettings,
    load_settings,
    set_setting,
    settings_path,
)
from tests.test_autoswitch import EngineHarness, _iso_at

H = 3600.0
NOW = 0.0


def view(
    num: str,
    *,
    seven: float,
    reset_h: float | None,
    five: float | None = None,
    five_reset_h: float | None = 3.0,
) -> ef.AccountView:
    """An account view at NOW. ``five=None`` means no 5h window running."""
    five_pct = five
    five_reset = None if five is None or five_reset_h is None else NOW + five_reset_h * H
    return ef.AccountView(
        number=num,
        headroom=100.0 - max(seven, five or 0.0),
        seven_day_pct=seven,
        seven_day_reset=None if reset_h is None else NOW + reset_h * H,
        five_hour_pct=five_pct,
        five_hour_reset=five_reset,
    )


def choose(active, *candidates, burn=None, similar_h=12.0, threshold=90.0):
    return ef.choose(
        active,
        list(candidates),
        threshold=threshold,
        similar_reset_s=similar_h * H,
        hysteresis_pct=10.0,
        burn_pct_per_h=burn,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


class TestOwnersCase:
    """A: 7d 85%, resets in 2d2h, 5h 2%. B: 7d 63%, resets in 19h40m, 5h 37%."""

    A = dict(seven=85.0, reset_h=50.0, five=2.0)
    B = dict(seven=63.0, reset_h=19 + 40 / 60, five=37.0)

    def test_on_b_stays_with_the_reason(self):
        choice = choose(view("b", **self.B), view("a", **self.A))
        assert choice == ef.Choice(
            "stay", "b", "stay: resets soonest (19h40m), 37% headroom"
        )

    def test_on_a_moves_to_b_despite_the_running_5h_window(self):
        # 30h20m sooner is past the large-gain bar (2 x 12h), so a 5h window
        # in progress does not hold the move.
        choice = choose(view("a", **self.A), view("b", **self.B))
        assert choice == ef.Choice(
            "switch", "b", "switch → #b: #a's 7d resets in 2d2h, #b's in 19h40m"
        )

    def test_no_flap_back_once_on_b(self):
        on_a = choose(view("a", **self.A), view("b", **self.B))
        assert on_a.target == "b"
        assert choose(view("b", **self.B), view("a", **self.A)).action == "stay"


@pytest.mark.parametrize(
    "active, candidates, burn, expected_action, expected_target, reason_has",
    [
        # Reset-axis gain under the similar margin is a tie, not a move.
        (
            view("1", seven=40, reset_h=30, five=30),
            [view("2", seven=40, reset_h=25, five=0)],
            None, "stay", "1", "keeping #2 as the 5h backup",
        ),
        # Gain past the margin but under 2x, mid 5h window: hold.
        (
            view("1", seven=40, reset_h=40, five=30),
            [view("2", seven=40, reset_h=25)],
            None, "stay", "1", "mid 5h window; #2 resets only 15h sooner (needs 1d)",
        ),
        # Same gain with no 5h window running: move.
        (
            view("1", seven=40, reset_h=40, five=None),
            [view("2", seven=40, reset_h=25)],
            None, "switch", "2", "#1's 7d resets in 1d16h, #2's in 1d1h",
        ),
        # Same gain while the active's 5h is near its limit: move (no need
        # to protect a window that is nearly spent).
        (
            view("1", seven=40, reset_h=40, five=75),
            [view("2", seven=40, reset_h=25)],
            None, "switch", "2", "switch → #2",
        ),
        # Tie, active's 5h near its limit, backup has room: balance move.
        (
            view("1", seven=40, reset_h=30, five=75),
            [view("2", seven=40, reset_h=25, five=10)],
            None, "switch", "2", "5h 75% used (near limit)",
        ),
        # Tie, active near its 5h limit, but the backup is barely better.
        (
            view("1", seven=40, reset_h=30, five=75),
            [view("2", seven=40, reset_h=25, five=70)],
            None, "stay", "1", "no tied account has 10 points more 5h headroom",
        ),
        # Tie among candidates, active later: the most 5h headroom wins.
        (
            view("1", seven=40, reset_h=100),
            [
                view("2", seven=40, reset_h=20, five=60),
                view("3", seven=40, reset_h=25, five=5),
            ],
            None, "switch", "3", "switch → #3",
        ),
        # Sooner but implausible: 30 minutes at 2 pts/h absorbs 1 point.
        (
            view("1", seven=40, reset_h=100),
            [view("2", seven=40, reset_h=0.5)],
            2.0, "stay", "1", "resets soonest",
        ),
        # The same account is worth it at a heavier burn (30 pts/h -> 15).
        (
            view("1", seven=40, reset_h=100),
            [view("2", seven=40, reset_h=0.5)],
            30.0, "switch", "2", "switch → #2",
        ),
        # A candidate with nothing left to absorb never wins the slot.
        (
            view("1", seven=40, reset_h=100, five=10),
            [view("2", seven=97, reset_h=5)],
            None, "stay", "1", "resets soonest",
        ),
        # Candidate reset unknown: not a target.
        (
            view("1", seven=40, reset_h=100),
            [view("2", seven=10, reset_h=None)],
            None, "stay", "1", "resets soonest (4d4h), 60% headroom",
        ),
        # Active reset unknown: no basis for comparison.
        (
            view("1", seven=40, reset_h=None),
            [view("2", seven=10, reset_h=5)],
            None, "stay", "1", "7d reset time is unknown",
        ),
        # Nobody else healthy.
        (
            view("1", seven=40, reset_h=100), [], None, "stay", "1",
            "no other account is under the threshold",
        ),
    ],
)
def test_choose_table(active, candidates, burn, expected_action, expected_target, reason_has):
    choice = choose(active, *candidates, burn=burn)
    assert (choice.action, choice.target) == (expected_action, expected_target)
    assert reason_has in choice.reason
    assert choice.reason.startswith(expected_action)


def test_spent_active_leaves_even_mid_5h_window():
    # threshold 99 lets a 7d-spent active (98%) still sit below it.
    choice = choose(
        view("1", seven=98, reset_h=100, five=10),
        view("2", seven=40, reset_h=90),
        threshold=99.0,
    )
    assert (choice.action, choice.target) == ("switch", "2")


def test_similar_margin_is_a_setting():
    active = view("1", seven=40, reset_h=30, five=30)
    cand = view("2", seven=40, reset_h=25, five=0)
    assert choose(active, cand, similar_h=12).action == "stay"  # 5h apart: tied
    # With a 2h margin, 5h sooner is no tie, and it clears the 2 x 2h bar
    # for leaving mid 5h window.
    assert choose(active, cand, similar_h=2).action == "switch"


def test_burn_down_walk_moves_at_most_once_per_real_event():
    """A drains B, then A takes over; B never comes back until it resets."""
    a = dict(seven=50.0, reset_h=50.0, five=20.0)
    on = "b"
    moves = []
    for b_seven in range(60, 100):
        b = view("b", seven=float(b_seven), reset_h=19.0, five=40.0)
        av = view("a", **a)
        active, other = (b, av) if on == "b" else (av, b)
        healthy = [other] if 100 - other.headroom < 99.0 else []
        choice = choose(active, *healthy, threshold=99.0)
        if choice.action == "switch":
            moves.append((b_seven, choice.target))
            on = choice.target
    assert moves == [(98, "a")]  # once B is spent (< 3 points), never back


@pytest.mark.parametrize(
    "seconds, text",
    [
        (0, "0m"),
        (45 * 60, "45m"),
        (18 * H, "18h"),
        (19 * H + 40 * 60, "19h40m"),
        (50 * H, "2d2h"),
        (48 * H, "2d"),
        (math.inf, "unknown"),
        (-5, "0m"),
    ],
)
def test_fmt_duration(seconds, text):
    assert ef.fmt_duration(seconds) == text


class TestRank:
    def test_tie_group_by_5h_then_reset_then_headroom(self):
        pool = [
            view("far", seven=10, reset_h=150),
            view("spent", seven=98, reset_h=2),
            view("tied-busy", seven=40, reset_h=20, five=80),
            view("tied-fresh", seven=40, reset_h=28),
            view("mid", seven=40, reset_h=60),
            view("unknown", seven=0, reset_h=None),
        ]
        order = ef.rank(pool, similar_reset_s=12 * H, burn_pct_per_h=None, now=NOW)
        assert order == ["tied-fresh", "tied-busy", "mid", "far", "unknown", "spent"]

    def test_burn_rate_demotes_an_implausible_soonest(self):
        pool = [view("soon", seven=40, reset_h=1), view("later", seven=40, reset_h=40)]
        assert ef.rank(pool, similar_reset_s=12 * H, burn_pct_per_h=None, now=NOW)[0] == "soon"
        assert ef.rank(pool, similar_reset_s=12 * H, burn_pct_per_h=1.0, now=NOW)[0] == "later"


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------


def usage_at(now: float, *, seven: float, reset_h: float | None, five: float = 0.0,
             five_reset_h: float | None = 3.0) -> dict:
    seven_w: dict = {"pct": seven}
    if reset_h is not None:
        seven_w["resets_at"] = _iso_at(now + reset_h * H)
    five_w: dict = {"pct": five}
    if five_reset_h is not None:
        five_w["resets_at"] = _iso_at(now + five_reset_h * H)
    return {"five_hour": five_w, "seven_day": seven_w}


def make(temp_home: Path, n: int = 2, **settings) -> EngineHarness:
    settings.setdefault("strategy", "expiring-first")
    h = EngineHarness(temp_home, **settings)
    for i in range(1, n + 1):
        h.seed(i, f"{chr(96 + i)}@example.com")
    h.make_live("a@example.com", 1)
    return h


def owners_usage(h: EngineHarness, *, a: str, b: str) -> dict:
    """The owner's fleet with A and B on the given slots."""
    now = h.clock.now
    return {
        a: usage_at(now, seven=85, reset_h=50, five=2),
        b: usage_at(now, seven=63, reset_h=19 + 40 / 60, five=37),
    }


def no_switch(h: EngineHarness) -> list[NoSwitchEvent]:
    return [e for e in h.events if isinstance(e, NoSwitchEvent)]


def switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


class TestEngine:
    def test_owners_case_stays_on_b_with_reason(self, temp_home):
        h = make(temp_home)  # slot 1 active = B
        assert h.tick_with_usage(owners_usage(h, a="2", b="1")) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        line = "stay: resets soonest (19h40m), 37% headroom"
        assert [(e.reason, e.detail) for e in no_switch(h)] == [("stay", line)]
        assert h.engine.last_decision.action == "stay"
        assert h.engine.last_decision.line == line

    def test_owners_case_on_a_switches_to_b(self, temp_home):
        h = make(temp_home)  # slot 1 active = A
        assert h.tick_with_usage(owners_usage(h, a="1", b="2")) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        (event,) = switches(h)
        assert event.trigger == "expiring-first"
        assert event.reason == "switch → #2: #1's 7d resets in 2d2h, #2's in 19h40m"
        assert event.to_json()["reason"] == event.reason
        assert event.reason in event.human()
        assert h.engine.last_decision == Decision(
            action="switch", line=event.reason, ts=event.ts, target="2"
        )

    def test_dry_run_reports_the_reason_and_moves_nothing(self, temp_home):
        h = make(temp_home)
        h.engine = h._make_engine(dry_run=True)
        assert h.tick_with_usage(owners_usage(h, a="1", b="2")) is TickOutcome.SWITCHED
        (event,) = switches(h)
        assert event.dry_run and event.reason.startswith("switch → #2")
        assert h.active_number() == 1

    def test_disabled_account_is_never_a_target(self, temp_home):
        h = make(temp_home)
        h.switcher.set_account_disabled("2", True)
        h.tick_with_usage(owners_usage(h, a="1", b="2"))
        assert h.active_number() == 1
        assert not switches(h)

    def test_quarantined_account_is_never_a_target(self, temp_home):
        h = make(temp_home)
        h.engine._mutate_state(
            lambda s: s.setdefault("quarantine", {}).update(
                {"2": {"email": "b@example.com", "refreshTokenFingerprint": None}}
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(h.engine, "_release_recovered_quarantines", lambda s: s)
            h.tick_with_usage(owners_usage(h, a="1", b="2"))
        assert h.active_number() == 1

    def test_candidate_at_threshold_on_a_model_window_is_skipped(self, temp_home):
        h = make(temp_home, model="Fable")
        u = owners_usage(h, a="1", b="2")
        u["2"]["scoped"] = [{"name": "Fable", "pct": 95.0}]
        h.tick_with_usage(u)
        assert h.active_number() == 1

    def test_tie_balances_by_5h_only_near_the_limit(self, temp_home):
        h = make(temp_home, n=3)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=40, reset_h=30, five=40),
            "2": usage_at(now, seven=40, reset_h=26, five=50),
            "3": usage_at(now, seven=40, reset_h=24, five=0, five_reset_h=None),
        }
        h.tick_with_usage(u)
        assert h.active_number() == 1  # 5h has room: no fresh window opened
        assert "keeping #3 as the 5h backup" in no_switch(h)[-1].detail
        u["1"] = usage_at(now, seven=40, reset_h=30, five=80)
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert h.active_number() == 3  # the one with the most 5h headroom

    def test_reset_passed_is_not_a_reason_to_move(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=40, reset_h=50, five=10),
            # Stale snapshot: its weekly reset is already behind us.
            "2": usage_at(now, seven=40, reset_h=-1),
        }
        h.tick_with_usage(u)
        assert h.active_number() == 1
        assert no_switch(h)[-1].detail.startswith("stay: resets soonest")

    def test_active_reset_passed_holds(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=40, reset_h=-1),
            "2": usage_at(now, seven=40, reset_h=5),
        }
        h.tick_with_usage(u)
        assert h.active_number() == 1
        assert no_switch(h)[-1].detail == "stay: this account's 7d reset time is unknown"

    @pytest.mark.parametrize("strategy", ["expiring-first", "best"])
    def test_all_near_limits_keeps_the_recovery_ranking(self, temp_home, strategy):
        """Everyone over the threshold: the existing soonest-recovery escape
        decides, identically for both strategies."""
        h = make(temp_home, n=3, strategy=strategy)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=50, reset_h=100, five=96, five_reset_h=2),
            "2": usage_at(now, seven=50, reset_h=20, five=93, five_reset_h=1),
            "3": usage_at(now, seven=50, reset_h=60, five=94, five_reset_h=4),
        }
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert h.active_number() == 2  # its 5h comes back first
        assert switches(h)[0].trigger == "proactive"

    def test_at_threshold_targets_the_soonest_expiring(self, temp_home):
        h = make(temp_home, n=3)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=92, reset_h=100),
            "2": usage_at(now, seven=10, reset_h=150),  # most headroom
            "3": usage_at(now, seven=50, reset_h=20),  # expires soonest
        }
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert switches(h)[0].trigger == "proactive"

    def test_at_threshold_best_strategy_still_takes_most_headroom(self, temp_home):
        h = make(temp_home, n=3, strategy="best")
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=92, reset_h=100),
            "2": usage_at(now, seven=10, reset_h=150),
            "3": usage_at(now, seven=50, reset_h=20),
        }
        h.tick_with_usage(u)
        assert h.active_number() == 2

    def test_walk_does_not_flap(self, temp_home):
        """Sixty ticks with jittering 5h readings: one move, then it holds."""
        h = make(temp_home)
        actives = []
        for i in range(60):
            h.clock.advance(120)
            now = h.clock.now
            jitter = (i * 7) % 5
            u = {
                "1": usage_at(now, seven=60, reset_h=50 - i / 30, five=10 + jitter),
                "2": usage_at(now, seven=55 + i / 10, reset_h=20 - i / 30, five=20 + jitter),
            }
            h.tick_with_usage(u)
            actives.append(h.active_number())
        assert actives[0] == 2
        assert set(actives) == {2}
        assert len(switches(h)) == 1

    def test_cooldown_holds_a_second_proactive_move(self, temp_home):
        h = make(temp_home, n=3, cooldown_seconds=600)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=40, reset_h=100),
            "2": usage_at(now, seven=40, reset_h=60),
            "3": usage_at(now, seven=90.5, reset_h=10),  # at threshold: not yet
        }
        h.tick_with_usage(u)
        assert h.active_number() == 2
        h.clock.advance(60)
        u["3"] = usage_at(h.clock.now, seven=40, reset_h=10)  # rolled into view
        assert h.tick_with_usage(u) is TickOutcome.NO_ACTION
        assert no_switch(h)[-1].reason == "cooldown"
        h.clock.advance(600)
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_manual_hold_blocks_proactive_moves_only(self, temp_home):
        h = make(temp_home, cooldown_seconds=0, manual_hold_minutes=60)
        note_manual_switch(h.switcher.backup_dir, "1", clock=h.clock)
        u = owners_usage(h, a="1", b="2")
        assert h.tick_with_usage(u) is TickOutcome.NO_ACTION
        held = no_switch(h)[-1]
        assert held.reason == "manual-hold"
        assert held.detail == "stay: switched by hand; proactive moves resume in 1h"
        assert h.engine.last_decision.line == held.detail
        # A threshold move is still allowed inside the hold.
        u["1"] = usage_at(h.clock.now, seven=95, reset_h=50)
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == "proactive"

    def test_manual_hold_expires(self, temp_home):
        h = make(temp_home, cooldown_seconds=0, manual_hold_minutes=30)
        note_manual_switch(h.switcher.backup_dir, "1", clock=h.clock)
        h.clock.advance(29 * 60)
        h.tick_with_usage(owners_usage(h, a="1", b="2"))
        assert h.active_number() == 1
        h.clock.advance(61)
        assert h.tick_with_usage(owners_usage(h, a="1", b="2")) is TickOutcome.SWITCHED

    def test_engine_switch_does_not_start_a_manual_hold(self, temp_home):
        h = make(temp_home, cooldown_seconds=0)
        h.tick_with_usage(owners_usage(h, a="1", b="2"))
        assert "lastManualSwitchAt" not in h.state()

    def test_failover_still_moves(self, temp_home):
        h = make(temp_home, unhealthy_ticks=1)
        now = h.clock.now
        u = {"1": None, "2": usage_at(now, seven=40, reset_h=20)}
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert switches(h)[0].trigger == "failover"

    def test_burn_rate_comes_from_the_active_history(self, temp_home):
        h = make(temp_home)
        history = h.switcher.usage_history
        history.clock = h.clock
        ident = {"1": ("a@example.com", "")}
        start = h.clock.now
        for k in range(7):  # 7d window: 40% -> 46% over 6h = 1 pt/h
            t = start + k * H
            history.record({"1": usage_at(start, seven=40 + k, reset_h=100)}, ident, t)
        h.clock.now = start + 6 * H
        rate = h.engine._burn_rate("1", h.clock.now)
        assert rate == pytest.approx(1.0, rel=0.05)
        now = h.clock.now
        # 30% headroom expiring in 2h: at 1 pt/h only 2 points are usable.
        u = {
            "1": usage_at(now, seven=46, reset_h=94),
            "2": usage_at(now, seven=70, reset_h=2),
        }
        h.tick_with_usage(u)
        assert h.active_number() == 1

    def test_no_history_counts_the_whole_headroom(self, temp_home):
        h = make(temp_home)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=46, reset_h=94),
            "2": usage_at(now, seven=70, reset_h=2),
        }
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED

    def test_below_threshold_without_peers_is_no_action(self, temp_home):
        h = make(temp_home, n=1)
        now = h.clock.now
        assert h.tick_with_usage(
            {"1": usage_at(now, seven=40, reset_h=20)}
        ) is TickOutcome.NO_ACTION


class TestDecisionFromEvent:
    def test_generic_no_switch_uses_human(self):
        e = NoSwitchEvent(reason="cooldown")
        assert decision_from_event(e).line == "no switch: cooldown"

    def test_stay_uses_detail(self):
        e = NoSwitchEvent(reason="stay", detail="stay: resets soonest (1h), 5% headroom")
        assert decision_from_event(e).line == e.detail
        assert decision_from_event(e).to_json()["action"] == "stay"

    def test_switch_without_reason_uses_human(self):
        e = SwitchEvent(trigger="at-limit", from_ref=None, to_ref={"number": 2, "email": "b"})
        d = decision_from_event(e)
        assert (d.action, d.target) == ("switch", "2")
        assert d.line == e.human()
        assert "reason" not in e.to_json()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_default_for_new_installs(self, tmp_path):
        assert load_settings(tmp_path).strategy == "expiring-first"

    def test_data_dir_without_the_key_follows_the_new_default(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"threshold": 80}}))
        assert load_settings(tmp_path).strategy == "expiring-first"

    def test_explicit_best_is_kept(self, tmp_path):
        set_setting(tmp_path, "autoswitch.strategy", "best")
        assert load_settings(tmp_path).strategy == "best"

    @pytest.mark.parametrize(
        "key, default, lo, hi",
        [
            ("autoswitch.similarResetHours", 12.0, 0.0, 168.0),
            ("autoswitch.manualHoldMinutes", 60.0, 0.0, 1440.0),
        ],
    )
    def test_new_specs(self, tmp_path, key, default, lo, hi):
        spec = SETTING_SPECS[key]
        assert spec.default == default
        assert (spec.lo, spec.hi) == (lo, hi)
        assert spec.group in dict(SETTING_GROUPS) and spec.label and spec.step
        set_setting(tmp_path, key, str(hi))
        assert getattr(load_settings(tmp_path), spec.field) == hi

    def test_out_of_range_is_clamped_on_load(self, tmp_path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"similarResetHours": 900, "manualHoldMinutes": -5}})
        )
        loaded = load_settings(tmp_path)
        assert (loaded.similar_reset_hours, loaded.manual_hold_minutes) == (168.0, 0.0)

    def test_strategy_choices_lead_with_the_default(self):
        spec = SETTING_SPECS["autoswitch.strategy"]
        assert spec.choices[0] == AutoSwitchSettings().strategy == "expiring-first"
        assert set(spec.choices) == {"expiring-first", "best", "consume-first"}
