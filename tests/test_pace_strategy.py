"""Tests for the pace strategy: the pure policy, a week replay against the
other strategies, and (further down) its engine wiring."""

from __future__ import annotations

import pytest

from claude_rotater import pace_strategy as ps
from claude_rotater.expiring_first import AccountView, Choice
from tests import _week_sim

H = 3600.0
NOW = 0.0


def view(
    num: str,
    *,
    seven: float,
    reset_h: float | None,
    five: float | None = None,
    five_reset_h: float | None = 3.0,
) -> AccountView:
    """An account view at NOW. ``five=None`` means no 5h window running."""
    five_reset = None if five is None or five_reset_h is None else NOW + five_reset_h * H
    return AccountView(
        number=num,
        headroom=100.0 - max(seven, five or 0.0),
        seven_day_pct=seven,
        seven_day_reset=None if reset_h is None else NOW + reset_h * H,
        five_hour_pct=five,
        five_hour_reset=five_reset,
    )


def choose(active, *candidates, burn=None, margin=1.5, threshold=90.0):
    return ps.choose(
        active,
        list(candidates),
        threshold=threshold,
        margin=margin,
        hysteresis_pct=10.0,
        burn_pct_per_h=burn,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Required pace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seven, reset_h, threshold, expected",
    [
        (50.0, 9.0, 90.0, 40.0 / 9.0),  # 40 points to the threshold in 9h
        (85.0, 50.0, 90.0, 0.1),
        (0.0, 168.0, 90.0, 90.0 / 168.0),  # a fresh week, evenly paced
        (95.0, 10.0, 90.0, 0.0),  # already past the threshold: nothing left
        (40.0, None, 90.0, 0.0),  # reset unknown: never urgent
        (40.0, 0.0, 90.0, 50.0 * 60.0),  # a reset now counts as a minute away
    ],
)
def test_required_pace(seven, reset_h, threshold, expected):
    v = view("1", seven=seven, reset_h=reset_h)
    assert ps.required_pace(v, threshold=threshold, now=NOW) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


class TestOwnersCase:
    """A: 7d 85%, resets in 2d2h, 5h 2%. B: 7d 63%, resets in 19h40m, 5h 37%.
    B needs 27 points in 19h40m (1.4%/h), A 5 points in 50h (0.1%/h): B
    dominates by far more than the margin, so pace concentrates on B."""

    A = dict(seven=85.0, reset_h=50.0, five=2.0)
    B = dict(seven=63.0, reset_h=19 + 40 / 60, five=37.0)

    def test_on_a_moves_to_b(self):
        assert choose(view("a", **self.A), view("b", **self.B)) == Choice(
            "switch", "b",
            "switch → #b: #b resets in 19h40m with 27% left (needs 1.4%/h) "
            "vs #a needs 0.1%/h",
        )

    def test_on_b_stays(self):
        assert choose(view("b", **self.B), view("a", **self.A)) == Choice(
            "stay", "b",
            "stay: #b resets in 19h40m with 27% left (needs 1.4%/h), next #a 0.1%/h",
        )

    @pytest.mark.parametrize("burn", [None, 1.0, 2.5])
    def test_concentrates_unless_the_burn_dwarfs_the_need(self, burn):
        assert choose(view("a", **self.A), view("b", **self.B), burn=burn).target == "b"

    def test_a_burn_this_heavy_spreads_instead(self):
        # At 6%/h the user empties B in under five hours: nothing to rescue,
        # so A's running 5h window is used instead of opening B's.
        choice = choose(view("a", **self.A), view("b", **self.B), burn=6.0)
        assert choice.action == "stay"
        assert choice.reason.startswith("stay: spreading — all resets far")


@pytest.mark.parametrize(
    "active, candidates, burn, expected_action, expected_target, reason",
    [
        # -- start of week: everyone has low, similar needs -> spread --------
        pytest.param(
            view("1", seven=10, reset_h=150, five=30),
            [view("2", seven=5, reset_h=160)],
            None, "stay", "1",
            "stay: spreading — no account stands out (pace need 0.5%/h); 5h 30% used",
            id="start-of-week-no-burn",
        ),
        pytest.param(
            view("1", seven=10, reset_h=150, five=30),
            [view("2", seven=5, reset_h=160)],
            2.0, "stay", "1",
            "stay: spreading — all resets far (pace need ≤0.5%/h vs burn 2.0%/h); "
            "5h 30% used",
            id="start-of-week-with-burn",
        ),
        pytest.param(
            # One account fresh, another half used: needs differ (0.8 vs 0.3)
            # but the user's 2%/h would empty either in time.
            view("1", seven=50, reset_h=150, five=30),
            [view("2", seven=0, reset_h=110)],
            2.0, "stay", "1", "stay: spreading — all resets far",
            id="start-of-week-burn-covers-a-dominant-need",
        ),
        pytest.param(
            view("1", seven=10, reset_h=150, five=75),
            [view("2", seven=5, reset_h=160, five=30), view("3", seven=0, reset_h=165)],
            None, "switch", "2",
            "switch → #2: 5h 75% used; spreading to #2 (5h 30% used, "
            "#3 kept fresh as backup)",
            id="start-of-week-5h-tight-keeps-a-fresh-backup",
        ),
        # -- mid-week -------------------------------------------------------
        pytest.param(
            view("1", seven=50, reset_h=80, five=30),
            [view("2", seven=40, reset_h=100)],
            None, "stay", "1", "no account stands out (pace need 0.5%/h)",
            id="mid-week-tie",
        ),
        pytest.param(
            # 0.7 vs 0.5: a 1.4x lead is under the 1.5x margin.
            view("1", seven=50, reset_h=80, five=30),
            [view("2", seven=55, reset_h=50)],
            None, "stay", "1", "pace need 0.5–0.7%/h",
            id="mid-week-lead-under-margin",
        ),
        pytest.param(
            # 0.8 vs 0.5: a 1.6x lead concentrates.
            view("1", seven=50, reset_h=80, five=30),
            [view("2", seven=50, reset_h=50)],
            None, "switch", "2",
            "switch → #2: #2 resets in 2d2h with 40% left (needs 0.8%/h) "
            "vs #1 needs 0.5%/h",
            id="mid-week-lead-over-margin",
        ),
        # -- near reset: concentrate -----------------------------------------
        pytest.param(
            view("1", seven=40, reset_h=100, five=30),
            [view("2", seven=50, reset_h=9, five=10)],
            2.0, "switch", "2",
            "switch → #2: #2 resets in 9h with 40% left (needs 4.4%/h) "
            "vs #1 needs 0.5%/h",
            id="near-reset-concentrates",
        ),
        pytest.param(
            view("2", seven=50, reset_h=9, five=10),
            [view("1", seven=40, reset_h=100, five=30)],
            2.0, "stay", "2",
            "stay: #2 resets in 9h with 40% left (needs 4.4%/h), next #1 0.5%/h",
            id="near-reset-holds-the-leader",
        ),
        pytest.param(
            view("1", seven=40, reset_h=100, five=30),
            [view("2", seven=50, reset_h=9, five=80)],
            2.0, "stay", "1", "stay: #2 needs 4.4%/h but its 5h is 80% used",
            id="near-reset-target-5h-near-limit",
        ),
        pytest.param(
            # Sooner but implausible: 30 minutes at 2%/h absorbs 1 point.
            view("1", seven=40, reset_h=100),
            [view("2", seven=40, reset_h=0.5)],
            2.0, "stay", "1", "stay: spreading",
            id="near-reset-implausible",
        ),
        pytest.param(
            # The same account is worth it at a heavier burn (30%/h -> 15).
            view("1", seven=40, reset_h=100),
            [view("2", seven=40, reset_h=0.5)],
            30.0, "switch", "2", "#2 resets in 30m with 50% left",
            id="near-reset-plausible-at-heavy-burn",
        ),
        # -- ties -------------------------------------------------------------
        pytest.param(
            # Two leaders tied on need: the one with more 5h room wins.
            view("1", seven=40, reset_h=100),
            [
                view("2", seven=50, reset_h=10, five=50),
                view("3", seven=50, reset_h=10, five=5),
            ],
            None, "switch", "3", "#3 resets in 10h with 40% left (needs 4.0%/h)",
            id="tie-leaders-by-5h-room",
        ),
        pytest.param(
            # Active tied with the leader: no move, it is as urgent.
            view("1", seven=50, reset_h=10, five=30),
            [view("2", seven=50, reset_h=10), view("3", seven=10, reset_h=150)],
            None, "stay", "1", "as urgent as #2, next #3 0.5%/h",
            id="tie-active-with-leader",
        ),
        # -- 5h rules ----------------------------------------------------------
        pytest.param(
            # Two fresh accounts: taking one still leaves the other fresh.
            view("1", seven=10, reset_h=150, five=75),
            [view("2", seven=5, reset_h=160, five=30), view("3", seven=0, reset_h=165),
             view("4", seven=0, reset_h=166)],
            None, "switch", "3", "spreading to #3 (5h 0% used, #4 kept fresh",
            id="5h-two-fresh-takes-one",
        ),
        pytest.param(
            # The only other account is fresh: it is the backup and the target.
            view("1", seven=10, reset_h=150, five=75),
            [view("2", seven=5, reset_h=160)],
            None, "switch", "2", "spreading to #2 (5h 0% used)",
            id="5h-only-fresh-is-taken",
        ),
        pytest.param(
            view("1", seven=10, reset_h=150, five=75),
            [view("2", seven=5, reset_h=160, five=68)],
            None, "stay", "1", "no account has 10 points more 5h room",
            id="5h-tight-no-roomier-backup",
        ),
        pytest.param(
            # The sole leader's 5h is tight: concentrate until the threshold.
            view("1", seven=50, reset_h=9, five=75),
            [view("2", seven=40, reset_h=100)],
            None, "stay", "1", "5h 75% used; #1 resets in 9h with 40% left "
            "(needs 4.4%/h), staying until the threshold",
            id="5h-tight-sole-leader-stays",
        ),
        pytest.param(
            # Two leaders: the tight one hands over to the roomy one.
            view("1", seven=50, reset_h=9, five=75),
            [view("2", seven=50, reset_h=10), view("3", seven=10, reset_h=150)],
            None, "switch", "2", "5h 75% used; #2 resets in 10h with 40% left "
            "(needs 4.0%/h), as urgent as #1",
            id="5h-tight-leader-hands-over",
        ),
        pytest.param(
            # 5h room is never a reason to open a fresh window while spreading.
            view("1", seven=10, reset_h=150, five=60),
            [view("2", seven=5, reset_h=160)],
            None, "stay", "1", "spreading",
            id="5h-room-left-no-fresh-window",
        ),
        # -- gates --------------------------------------------------------------
        pytest.param(
            view("1", seven=40, reset_h=100),
            [view("2", seven=10, reset_h=None)],
            None, "stay", "1", "spreading", id="candidate-reset-unknown",
        ),
        pytest.param(
            view("1", seven=40, reset_h=None),
            [view("2", seven=10, reset_h=5)],
            None, "stay", "1", "stay: this account's 7d reset time is unknown",
            id="active-reset-unknown",
        ),
        pytest.param(
            view("1", seven=40, reset_h=100), [], None, "stay", "1",
            "stay: no other account is under the threshold", id="no-candidates",
        ),
        pytest.param(
            # 3 points to the threshold: under the 5 a move must be worth.
            view("1", seven=50, reset_h=100, five=10),
            [view("2", seven=87, reset_h=5)],
            None, "stay", "1", "stay: spreading", id="candidate-nothing-to-absorb",
        ),
    ],
)
def test_choose_table(active, candidates, burn, expected_action, expected_target, reason):
    choice = choose(active, *candidates, burn=burn)
    assert (choice.action, choice.target) == (expected_action, expected_target)
    assert reason in choice.reason
    assert choice.reason.startswith(expected_action)


def test_spent_active_spreads_keeping_the_fresh_backup():
    # threshold 99 lets a 7d-spent active (98%) still sit below it.
    choice = choose(
        view("1", seven=98, reset_h=100, five=10),
        view("2", seven=40, reset_h=140, five=40),
        view("3", seven=40, reset_h=140, five=10),
        threshold=99.0,
    )
    assert choice == Choice(
        "switch", "2",
        "switch → #2: #1 has under 3% 7d left; spreading to #2 (5h 40% used)",
    )


def test_nothing_left_anywhere_stays():
    choice = choose(
        view("1", seven=98, reset_h=100, five=10),
        view("2", seven=96, reset_h=5),
        threshold=99.0,
    )
    assert choice == Choice(
        "stay", "1", "stay: no account has meaningful 7d headroom left"
    )


def test_margin_is_a_setting():
    active = view("1", seven=50, reset_h=80, five=30)  # 0.5%/h
    cand = view("2", seven=55, reset_h=50)  # 0.7%/h
    assert choose(active, cand, margin=1.5).action == "stay"
    assert choose(active, cand, margin=1.2).action == "switch"


class TestFlapping:
    def test_the_move_back_needs_margin_squared(self):
        """Once on the leader, the account it led needs to lead it by the
        margin in turn: the same numbers never reverse a move."""
        a = view("a", seven=50, reset_h=80, five=30)
        b = view("b", seven=50, reset_h=50, five=30)
        assert choose(a, b).target == "b"
        assert choose(b, a).action == "stay"

    def test_burning_the_leader_moves_back_only_past_the_margin(self):
        """Burn B (the leader) point by point: A takes over only when B's
        need has fallen to A's over the margin, and then never flips back."""
        a = view("a", seven=50, reset_h=80, five=30)  # 0.5%/h
        on, moves = "b", []
        for b_seven in range(50, 90):
            b = view("b", seven=float(b_seven), reset_h=50, five=30)
            active, other = (b, a) if on == "b" else (a, b)
            choice = choose(active, other)
            if choice.action == "switch":
                moves.append((b_seven, choice.target))
                on = choice.target
        # B's need drops below 0.5 / 1.5 = 0.33%/h at 74% (16 / 50h = 0.32).
        assert moves == [(74, "a")]

    def test_the_burn_estimate_wobbling_never_moves_off_the_leader(self):
        a = view("a", seven=85, reset_h=50, five=2)
        b = view("b", seven=63, reset_h=19 + 40 / 60, five=37)
        for burn in (0.5, 1.0, 2.0, 3.0, 6.0, 12.0, None, 2.0, 0.8):
            assert choose(b, a, burn=burn).action == "stay"

    def test_a_simulated_walk_moves_rarely(self):
        """Two accounts through a week of steady work with the policy
        free to move every ten minutes: moves happen on real events only."""
        result = _week_sim.simulate("pace", [70.0, 100.0], rate=2.0, weeks=1)
        per_day = result.switches / 7.0
        assert per_day <= 4.0


class TestRank:
    def test_concentrating_puts_the_leaders_first(self):
        pool = [
            view("far", seven=10, reset_h=150),
            view("lead-busy", seven=50, reset_h=9, five=80),  # 5h near limit
            view("lead", seven=50, reset_h=10, five=20),
            view("mid", seven=40, reset_h=60),
            view("unknown", seven=0, reset_h=None),
            view("spent", seven=89, reset_h=2),
        ]
        order = ps.rank(pool, threshold=90.0, margin=1.5, burn_pct_per_h=None, now=NOW)
        assert order == ["lead", "mid", "far", "lead-busy", "unknown", "spent"]

    def test_spreading_orders_by_5h_room_keeping_a_fresh_backup(self):
        pool = [
            view("busy", seven=10, reset_h=150, five=60),
            view("fresh", seven=10, reset_h=150),
            view("running", seven=10, reset_h=150, five=30),
        ]
        order = ps.rank(pool, threshold=90.0, margin=1.5, burn_pct_per_h=2.0, now=NOW)
        assert order == ["running", "fresh", "busy"]


# ---------------------------------------------------------------------------
# Week replay
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def table():
    return _week_sim.table(weeks=8)


class TestWeekReplay:
    """Eight simulated weeks per scenario (``tests/_week_sim.py``): pace wastes
    less weekly quota than expiring-first and best in total, blocks the user
    less, and is never meaningfully worse in any one scenario."""

    def test_less_waste_and_blocking_in_total(self, table):
        total = {
            s: sum(row[s].wasted for row in table.values()) for s in _week_sim.STRATEGIES
        }
        blocked = {
            s: sum(row[s].blocked_h for row in table.values())
            for s in _week_sim.STRATEGIES
        }
        assert total["pace"] < total["expiring-first"] < total["best"], total
        assert blocked["pace"] < blocked["expiring-first"] < blocked["best"], blocked

    def test_never_much_worse_in_any_scenario(self, table):
        for name, row in table.items():
            others = min(row[s].wasted for s in ("expiring-first", "best"))
            assert row["pace"].wasted <= others * 1.02, (name, row)

    def test_owner_like_staggered_pair(self, table):
        row = table["2 accounts, resets 50h/20h, demand 95%"]
        assert row["pace"].wasted < row["expiring-first"].wasted
        assert row["pace"].wasted < row["best"].wasted


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------

from claude_rotater.autoswitch import (  # noqa: E402
    PROACTIVE_CONFIRM_S,
    Decision,
    TickOutcome,
    note_manual_switch,
)
from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED  # noqa: E402
from claude_rotater.settings import (  # noqa: E402
    SETTING_GROUPS,
    SETTING_SPECS,
    load_settings,
    set_setting,
)
from tests.test_expiring_first import (  # noqa: E402
    confirmed,
    make,
    no_switch,
    switches,
    usage_at,
)


def make_pace(temp_home, n: int = 2, **settings):
    settings.setdefault("strategy", "pace")
    return make(temp_home, n=n, **settings)


def owners(h, *, a: str, b: str) -> dict:
    now = h.clock.now
    return {
        a: usage_at(now, seven=85, reset_h=50, five=2),
        b: usage_at(now, seven=63, reset_h=19 + 40 / 60, five=37),
    }


class TestEngine:
    def test_owners_case_on_a_moves_to_b_with_reason(self, temp_home):
        h = make_pace(temp_home)  # slot 1 active = A
        assert confirmed(h, owners(h, a="1", b="2")) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        (event,) = switches(h)
        assert event.trigger == "pace"
        assert event.reason == (
            "switch → #2: #2 resets in 19h38m with 27% left (needs 1.4%/h) "
            "vs #1 needs 0.1%/h"
        )
        assert event.to_json()["reason"] == event.reason
        assert h.engine.last_decision == Decision(
            action="switch", line=event.reason, ts=event.ts, target="2"
        )

    def test_owners_case_on_b_stays_with_reason(self, temp_home):
        h = make_pace(temp_home)  # slot 1 active = B
        assert h.tick_with_usage(owners(h, a="2", b="1")) is TickOutcome.NO_ACTION
        line = "stay: #1 resets in 19h40m with 27% left (needs 1.4%/h), next #2 0.1%/h"
        assert [(e.reason, e.detail) for e in no_switch(h)] == [("stay", line)]
        assert h.engine.last_decision.line == line

    def test_start_of_week_spreads_then_hands_over_on_a_tight_5h(self, temp_home):
        h = make_pace(temp_home, n=3)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=10, reset_h=150, five=40),
            "2": usage_at(now, seven=5, reset_h=160, five=30),
            "3": usage_at(now, seven=0, reset_h=165, five=0, five_reset_h=None),
        }
        assert h.tick_with_usage(u) is TickOutcome.NO_ACTION
        assert no_switch(h)[-1].detail.startswith("stay: spreading — ")
        u["1"] = usage_at(now, seven=10, reset_h=150, five=75)
        assert confirmed(h, u) is TickOutcome.SWITCHED
        assert h.active_number() == 2  # #3's fresh window stays in reserve
        assert "#3 kept fresh as backup" in switches(h)[0].reason

    def test_dry_run_reports_the_reason_and_moves_nothing(self, temp_home):
        h = make_pace(temp_home)
        h.engine = h._make_engine(dry_run=True)
        assert confirmed(h, owners(h, a="1", b="2")) is TickOutcome.SWITCHED
        (event,) = switches(h)
        assert event.dry_run and event.reason.startswith("switch → #2")
        assert h.active_number() == 1

    def test_disabled_account_is_never_a_target(self, temp_home):
        h = make_pace(temp_home)
        h.switcher.set_account_disabled("2", True)
        h.tick_with_usage(owners(h, a="1", b="2"))
        assert h.active_number() == 1 and not switches(h)

    def test_manual_hold_blocks_proactive_moves_only(self, temp_home):
        h = make_pace(temp_home, cooldown_seconds=0, manual_hold_minutes=60)
        note_manual_switch(h.switcher.backup_dir, "1", clock=h.clock)
        u = owners(h, a="1", b="2")
        assert h.tick_with_usage(u) is TickOutcome.NO_ACTION
        assert no_switch(h)[-1].reason == "manual-hold"
        u["1"] = usage_at(h.clock.now, seven=95, reset_h=50)
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert switches(h)[-1].trigger == "proactive"

    def test_cooldown_holds_a_second_proactive_move(self, temp_home):
        h = make_pace(temp_home, n=3, cooldown_seconds=600)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=40, reset_h=100),
            "2": usage_at(now, seven=50, reset_h=30),  # 1.3%/h vs 0.5%/h
            "3": usage_at(now, seven=90.5, reset_h=5),  # at threshold: not yet
        }
        assert confirmed(h, u) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(60)
        u["3"] = usage_at(h.clock.now, seven=40, reset_h=5)  # rolled into view
        assert h.tick_with_usage(u) is TickOutcome.NO_ACTION
        assert no_switch(h)[-1].reason == "cooldown"
        h.clock.advance(600)
        assert confirmed(h, u) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_at_threshold_targets_the_leader_not_the_most_headroom(self, temp_home):
        h = make_pace(temp_home, n=3)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=92, reset_h=100),
            "2": usage_at(now, seven=10, reset_h=150),  # most headroom, 0.5%/h
            "3": usage_at(now, seven=50, reset_h=20),  # 2%/h
        }
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert switches(h)[0].trigger == "proactive"

    def test_failover_still_moves(self, temp_home):
        h = make_pace(temp_home, unhealthy_ticks=1)
        u = {"1": USAGE_RELOGIN_REQUIRED, "2": usage_at(h.clock.now, seven=40, reset_h=20)}
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert switches(h)[0].trigger == "failover"

    @pytest.mark.parametrize("margin, outcome", [
        (1.2, TickOutcome.SWITCHED), (1.5, TickOutcome.NO_ACTION),
    ])
    def test_margin_setting_reaches_the_policy(self, temp_home, margin, outcome):
        h = make_pace(temp_home, pace_margin=margin)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=50, reset_h=80, five=30),  # 0.5%/h
            "2": usage_at(now, seven=55, reset_h=50),  # 0.7%/h: a 1.4x lead
        }
        h.tick_with_usage(u)
        h.clock.advance(PROACTIVE_CONFIRM_S)
        assert h.tick_with_usage(u) is outcome

    def test_heavy_burn_from_history_spreads(self, temp_home):
        """B needs 1.4%/h, A 0.9%/h: B leads by the margin, but the active
        account's own history says the user burns 6 points an hour, so B
        needs no rescuing and A's running 5h window is used."""
        def fleet(now):
            return {
                "1": usage_at(now, seven=40, reset_h=56, five=20),
                "2": usage_at(now, seven=63, reset_h=19 + 40 / 60, five=37),
            }

        without_history = choose(
            view("1", seven=40, reset_h=56, five=20),
            view("2", seven=63, reset_h=19 + 40 / 60, five=37),
        )
        assert without_history.target == "2"

        h = make_pace(temp_home)
        history = h.switcher.usage_history
        history.clock = h.clock
        ident = {"1": ("a@example.com", "")}
        start = h.clock.now
        for k in range(7):  # 7d window: 4% -> 40% over 6h = 6 pts/h
            history.record(
                {"1": usage_at(start, seven=4 + 6 * k, reset_h=62)}, ident, start + k * H
            )
        h.clock.now = start + 6 * H
        assert h.engine._burn_rate("1", h.clock.now) == pytest.approx(6.0, rel=0.05)
        assert h.tick_with_usage(fleet(h.clock.now)) is TickOutcome.NO_ACTION
        assert no_switch(h)[-1].detail.startswith("stay: spreading — all resets far")

    def test_walk_does_not_flap(self, temp_home):
        """Sixty ticks with jittering 5h readings: one move, then it holds."""
        h = make_pace(temp_home)
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
        assert actives[0] == 1  # the first evaluation only confirms
        assert set(actives[1:]) == {2}
        assert len(switches(h)) == 1


class TestSettings:
    def test_pace_is_a_strategy_choice(self, tmp_path):
        set_setting(tmp_path, "autoswitch.strategy", "pace")
        assert load_settings(tmp_path).strategy == "pace"

    def test_margin_spec(self, tmp_path):
        spec = SETTING_SPECS["autoswitch.paceMargin"]
        assert spec.default == 1.5
        assert (spec.lo, spec.hi, spec.step) == (1.0, 5.0, 0.1)
        assert spec.group in dict(SETTING_GROUPS) and spec.label and spec.unit == "x"
        set_setting(tmp_path, "autoswitch.paceMargin", "2.5")
        assert load_settings(tmp_path).pace_margin == 2.5
