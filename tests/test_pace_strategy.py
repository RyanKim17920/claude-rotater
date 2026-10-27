"""Tests for the pace strategy: the pure policy, a week replay against the
other strategies, and (further down) its engine wiring."""

from __future__ import annotations

import json

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
        (40.0, None, 90.0, 50.0 / 168.0),  # week not started: a week away
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
    leads by far more than the margin, so pace concentrates on B."""

    A = dict(seven=85.0, reset_h=50.0, five=2.0)
    B = dict(seven=63.0, reset_h=19 + 40 / 60, five=37.0)

    def test_on_a_moves_to_b(self):
        assert choose(view("a", **self.A), view("b", **self.B)) == Choice(
            "switch", "b",
            "switch → #b: #b's week resets in 19h40m with 27% left (needs 1.4%/h) "
            "vs #a 0.1%/h",
        )

    def test_on_b_stays(self):
        assert choose(view("b", **self.B), view("a", **self.A)) == Choice(
            "stay", "b",
            "stay: most 7d to spend: #b's week resets in 19h40m with 27% left "
            "(needs 1.4%/h)",
        )

    @pytest.mark.parametrize("burn", [2.0, 6.0])
    def test_a_known_burn_finishes_a_first_when_b_can_still_be_spent(self, burn):
        # A's last 5 points take 2.5h at 2%/h (50m at 6%/h); B still has
        # time for its 27 after that, so A is finished rather than stranded.
        choice = choose(view("a", **self.A), view("b", **self.B), burn=burn)
        assert choice == Choice(
            "stay", "a", "stay: finishing #a's last 5% of 7d before #b (1.4%/h)"
        )

    @pytest.mark.parametrize("burn", [0.5, 1.0])
    def test_a_slower_burn_goes_to_b_at_once(self, burn):
        # Finishing A first would leave B less time than its 27 points need.
        choice = choose(view("a", **self.A), view("b", **self.B), burn=burn)
        assert choice.target == "b"


class TestFiveHourIsNotAGoal:
    """The 5h window never drives a proactive move or a stay: the owner's
    header "stay: #1's 5h resets in 3h56m with 52% left — using it first"
    explained a stay by a 5h window that would never be hit."""

    def test_a_running_5h_window_is_not_a_reason_to_stay(self):
        # 2026-10-17: on #2 (5h 20% resetting in 25m, 7d 28% in 5d8h) beside
        # an idle #3 (7d 1% in 5d21h): the 7d needs are within the margin.
        on_2 = view("2", seven=28, reset_h=5 * 24 + 8, five=20, five_reset_h=25 / 60)
        idle_3 = view("3", seven=1, reset_h=5 * 24 + 21)
        for burn in (None, 1.0):
            choice = choose(on_2, idle_3, burn=burn, threshold=99.9)
            assert choice == Choice(
                "stay", "2",
                "stay: 7d spread evenly: #2 needs 0.6%/h, #3 0.7%/h (within 1.5x)",
            )

    def test_a_tight_5h_window_is_not_a_reason_to_move(self):
        # The engine's pre-empt and threshold paths move when 5h is about to
        # hit; the policy itself only moves for the week.
        active = view("1", seven=10, reset_h=150, five=75)
        others = [view("2", seven=5, reset_h=160, five=30), view("3", seven=0, reset_h=165)]
        choice = choose(active, *others)
        assert choice.action == "stay"
        assert "5h" not in choice.reason

    def test_a_heavy_burn_still_moves_to_the_account_that_needs_it(self):
        # One account fresh, another half used (0.8 vs 0.3%/h): the old
        # policy stayed because 2%/h "covers" both; the week says move.
        choice = choose(
            view("1", seven=50, reset_h=150, five=30),
            view("2", seven=0, reset_h=110),
            burn=2.0,
        )
        assert choice == Choice(
            "switch", "2",
            "switch → #2: #2's week resets in 4d14h with 90% left (needs 0.8%/h) "
            "vs #1 0.3%/h",
        )


class TestNothingToCompareWith:
    """Personal profile, 2026-10-19 23:30: on #1 (5h 1%, 7d 0% resetting in
    6d2h) beside #4 (7d 67%, resets in 4d19h), with a near-zero burn since
    the switch. #4 cannot take meaningful work at that burn, so only #1
    counts."""

    def test_says_no_other_account_needs_using_first(self):
        on_1 = view("1", seven=0, reset_h=6 * 24 + 2, five=1, five_reset_h=50 / 60)
        busy_4 = view("4", seven=67, reset_h=4 * 24 + 19, five=6)
        choice = choose(on_1, busy_4, burn=0.01, threshold=99.9)
        assert choice == Choice(
            "stay", "1",
            "stay: no other account needs using first; #1's week resets in 6d2h "
            "with 100% left (needs 0.7%/h)",
        )


@pytest.mark.parametrize(
    "active, candidates, burn, expected_action, expected_target, reason",
    [
        # -- spread: needs within the margin stay put --------------------------
        pytest.param(
            view("1", seven=10, reset_h=150, five=30),
            [view("2", seven=5, reset_h=160)],
            None, "stay", "1",
            "stay: most 7d to spend: #1's week resets in 6d6h with 80% left (needs 0.5%/h)",
            id="start-of-week-active-leads",
        ),
        pytest.param(
            view("1", seven=50, reset_h=80, five=30),
            [view("2", seven=40, reset_h=100)],
            None, "stay", "1", "most 7d to spend", id="mid-week-tie",
        ),
        pytest.param(
            # 0.7 vs 0.5: a 1.4x lead is under the 1.5x margin.
            view("1", seven=50, reset_h=80, five=30),
            [view("2", seven=55, reset_h=50)],
            None, "stay", "1",
            "stay: 7d spread evenly: #1 needs 0.5%/h, #2 0.7%/h (within 1.5x)",
            id="mid-week-lead-under-margin",
        ),
        pytest.param(
            # 0.8 vs 0.5: a 1.6x lead moves.
            view("1", seven=50, reset_h=80, five=30),
            [view("2", seven=50, reset_h=50)],
            None, "switch", "2",
            "switch → #2: #2's week resets in 2d2h with 40% left (needs 0.8%/h) "
            "vs #1 0.5%/h",
            id="mid-week-lead-over-margin",
        ),
        # -- finish first: a near reset with headroom takes the lead -----------
        pytest.param(
            view("1", seven=40, reset_h=100, five=30),
            [view("2", seven=50, reset_h=9, five=10)],
            2.0, "switch", "2",
            "switch → #2: #2's week resets in 9h with 40% left (needs 4.4%/h) "
            "vs #1 0.5%/h",
            id="near-reset-concentrates",
        ),
        pytest.param(
            view("2", seven=50, reset_h=9, five=10),
            [view("1", seven=40, reset_h=100, five=30)],
            2.0, "stay", "2",
            "stay: most 7d to spend: #2's week resets in 9h with 40% left (needs 4.4%/h)",
            id="near-reset-holds-the-leader",
        ),
        pytest.param(
            # Sooner but implausible: 30 minutes at 2%/h absorbs 1 point.
            view("1", seven=40, reset_h=100),
            [view("2", seven=40, reset_h=0.5)],
            2.0, "stay", "1", "no other account needs using first",
            id="near-reset-implausible",
        ),
        pytest.param(
            # The same account is worth it at a heavier burn (30%/h -> 15).
            view("1", seven=40, reset_h=100),
            [view("2", seven=40, reset_h=0.5)],
            30.0, "switch", "2", "#2's week resets in 30m with 50% left",
            id="near-reset-plausible-at-heavy-burn",
        ),
        # -- 5h as a constraint ----------------------------------------------
        pytest.param(
            view("1", seven=40, reset_h=100, five=30),
            [view("2", seven=50, reset_h=9, five=80)],
            2.0, "stay", "1", "stay: #2 needs 4.4%/h but its 5h is 80% used",
            id="leader-5h-near-limit-is-not-a-target",
        ),
        pytest.param(
            # The leader's 5h is tight: the next account ahead takes it.
            view("1", seven=40, reset_h=100, five=30),
            [view("2", seven=50, reset_h=9, five=80), view("3", seven=50, reset_h=20)],
            2.0, "switch", "3", "#3's week resets in 20h with 40% left",
            id="next-leader-with-5h-room",
        ),
        pytest.param(
            # Two leaders the week cannot tell apart: more 5h room wins.
            view("1", seven=40, reset_h=100),
            [
                view("2", seven=50, reset_h=10, five=50),
                view("3", seven=50, reset_h=10, five=5),
            ],
            None, "switch", "3", "#3's week resets in 10h with 40% left (needs 4.0%/h)",
            id="tie-leaders-by-5h-room",
        ),
        pytest.param(
            # Needs 10% apart are still a tie: 5h room decides.
            view("1", seven=40, reset_h=100),
            [
                view("2", seven=50, reset_h=9.5, five=50),
                view("3", seven=50, reset_h=10, five=5),
            ],
            None, "switch", "3", "#3's week", id="near-tie-leaders-by-5h-room",
        ),
        pytest.param(
            # Active tied with the leader: no move, it is as urgent.
            view("1", seven=50, reset_h=10, five=30),
            [view("2", seven=50, reset_h=10), view("3", seven=10, reset_h=150)],
            None, "stay", "1", "most 7d to spend: #1's week resets in 10h",
            id="tie-active-with-leader",
        ),
        # -- weeks that have not started ---------------------------------------
        pytest.param(
            # A fresh account is planned a whole week out: 0.5%/h.
            view("1", seven=70, reset_h=120, five=30),
            [view("2", seven=0, reset_h=None)],
            None, "switch", "2",
            "switch → #2: #2's week has not started, 90% to spend (needs 0.5%/h) "
            "vs #1 0.2%/h",
            id="candidate-week-not-started",
        ),
        pytest.param(
            view("1", seven=70, reset_h=40, five=30),
            [view("2", seven=0, reset_h=None)],
            None, "stay", "1", "7d spread evenly: #1 needs 0.5%/h, #2 0.5%/h",
            id="candidate-week-not-started-behind-a-sooner-reset",
        ),
        pytest.param(
            view("1", seven=0, reset_h=None),
            [view("2", seven=10, reset_h=5)],
            None, "switch", "2", "#2's week resets in 5h with 80% left (needs 16.0%/h)",
            id="active-week-not-started",
        ),
        # -- finishing an account ----------------------------------------------
        pytest.param(
            view("1", seven=82, reset_h=100, five=10),
            [view("2", seven=40, reset_h=140)],
            2.0, "stay", "1", "stay: finishing #1's last 8% of 7d before #2 (0.4%/h)",
            id="finish-the-last-points",
        ),
        pytest.param(
            # The leader's 5h reset-in-time: finishing first would cost it.
            view("1", seven=82, reset_h=100, five=10),
            [view("2", seven=40, reset_h=5)],
            1.0, "switch", "2", "#2's week resets in 5h",
            id="finish-not-at-the-leader's-cost",
        ),
        pytest.param(
            # Without a burn estimate there is no telling: the leader wins.
            view("1", seven=82, reset_h=100, five=10),
            [view("2", seven=40, reset_h=140)],
            None, "switch", "2", "#2's week resets in 5d20h",
            id="finish-needs-a-burn",
        ),
        # -- gates --------------------------------------------------------------
        pytest.param(
            view("1", seven=40, reset_h=100), [], None, "stay", "1",
            "stay: no other account is under the threshold", id="no-candidates",
        ),
        pytest.param(
            # 3 points to the threshold: under the 5 a move must be worth.
            view("1", seven=50, reset_h=100, five=10),
            [view("2", seven=87, reset_h=5)],
            None, "stay", "1", "no other account needs using first",
            id="candidate-nothing-to-absorb",
        ),
    ],
)
def test_choose_table(active, candidates, burn, expected_action, expected_target, reason):
    choice = choose(active, *candidates, burn=burn)
    assert (choice.action, choice.target) == (expected_action, expected_target)
    assert reason in choice.reason
    assert choice.reason.startswith(expected_action)


def test_spent_active_moves_to_the_most_7d_need():
    # threshold 99 lets a 7d-spent active (98%) still sit below it. #2 and
    # #3 need the same; #3 has more 5h room.
    choice = choose(
        view("1", seven=98, reset_h=100, five=10),
        view("2", seven=40, reset_h=140, five=40),
        view("3", seven=40, reset_h=140, five=10),
        threshold=99.0,
    )
    assert choice == Choice(
        "switch", "3",
        "switch → #3: #1 has under 3% 7d left; #3's week resets in 5d20h with "
        "59% left (needs 0.4%/h)",
    )


def test_spent_active_stays_when_every_target_5h_is_near_its_limit():
    choice = choose(
        view("1", seven=98, reset_h=100, five=10),
        view("2", seven=40, reset_h=140, five=85),
        threshold=99.0,
    )
    assert choice == Choice(
        "stay", "1", "stay: #1 has under 3% 7d left; #2's 5h is 85% used"
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


def test_no_line_mentions_5h_unless_it_binds():
    """Stays and moves are explained in 7d terms; 5h shows up only when it
    is what stops a move."""
    cases = [
        (view("1", seven=10, reset_h=150, five=75), [view("2", seven=5, reset_h=160)]),
        (view("1", seven=50, reset_h=80, five=60), [view("2", seven=50, reset_h=50, five=40)]),
        (view("2", seven=50, reset_h=9, five=60), [view("1", seven=40, reset_h=100, five=30)]),
    ]
    for active, candidates in cases:
        assert "5h" not in choose(active, *candidates).reason


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
    def test_7d_need_first_among_accounts_with_5h_room(self):
        pool = [
            view("far", seven=10, reset_h=150),
            view("lead-busy", seven=50, reset_h=9, five=80),  # 5h near limit
            view("lead", seven=50, reset_h=10, five=20),
            view("mid", seven=40, reset_h=60),
            view("unknown", seven=0, reset_h=None),  # week not started
            view("spent", seven=89, reset_h=2),
        ]
        order = ps.rank(pool, threshold=90.0, margin=1.5, burn_pct_per_h=None, now=NOW)
        assert order == ["lead", "mid", "unknown", "far", "lead-busy", "spent"]

    def test_equal_7d_needs_go_to_5h_room(self):
        pool = [
            view("busy", seven=10, reset_h=150, five=60),
            view("fresh", seven=10, reset_h=150),
            view("running", seven=10, reset_h=150, five=30),
        ]
        order = ps.rank(pool, threshold=90.0, margin=1.5, burn_pct_per_h=2.0, now=NOW)
        assert order == ["fresh", "running", "busy"]

    def test_5h_room_never_beats_a_clearly_higher_7d_need(self):
        pool = [
            view("roomy", seven=10, reset_h=150, five=0),
            view("urgent", seven=40, reset_h=20, five=60),
        ]
        order = ps.rank(pool, threshold=90.0, margin=1.5, burn_pct_per_h=None, now=NOW)
        assert order == ["urgent", "roomy"]


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
    settings_path,
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
            "switch → #2: #2's week resets in 19h38m with 27% left (needs 1.4%/h) "
            "vs #1 0.1%/h"
        )
        assert event.to_json()["reason"] == event.reason
        assert h.engine.last_decision == Decision(
            action="switch", line=event.reason, ts=event.ts, target="2"
        )

    def test_owners_case_on_b_stays_with_reason(self, temp_home):
        h = make_pace(temp_home)  # slot 1 active = B
        assert h.tick_with_usage(owners(h, a="2", b="1")) is TickOutcome.NO_ACTION
        line = "stay: most 7d to spend: #1's week resets in 19h40m with 27% left (needs 1.4%/h)"
        assert [(e.reason, e.detail) for e in no_switch(h)] == [("stay", line)]
        assert h.engine.last_decision.line == line

    def test_a_tight_5h_moves_only_at_the_limit_and_by_7d_need(self, temp_home):
        """A 5h window filling up is no proactive reason to move; when it
        reaches the threshold the forced move goes to the most 7d need
        among accounts with 5h room (#2 and #3 tie on need: #3 has the
        fresh window)."""
        h = make_pace(temp_home, n=3)
        now = h.clock.now
        u = {
            "1": usage_at(now, seven=10, reset_h=150, five=75),
            "2": usage_at(now, seven=5, reset_h=160, five=30),
            "3": usage_at(now, seven=0, reset_h=165, five=0, five_reset_h=None),
        }
        assert h.tick_with_usage(u) is TickOutcome.NO_ACTION
        assert "5h" not in no_switch(h)[-1].detail
        u["1"] = usage_at(now, seven=10, reset_h=150, five=91)
        assert h.tick_with_usage(u) is TickOutcome.SWITCHED
        assert h.active_number() == 3

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

    def test_heavy_burn_from_history_still_follows_the_week(self, temp_home):
        """B needs 1.4%/h, A 0.9%/h: B leads by the margin. The active
        account's own history says the user burns 6 points an hour, which
        once kept pace on A's running 5h window; the week still says B."""
        def fleet(now):
            return {
                "1": usage_at(now, seven=40, reset_h=56, five=20),
                "2": usage_at(now, seven=63, reset_h=19 + 40 / 60, five=37),
            }

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
        # The readings just rose: the caches are warm and B can wait.
        assert h.tick_with_usage(fleet(h.clock.now)) is TickOutcome.NO_ACTION
        assert "switch → #2 when idle (cache warm)" in no_switch(h)[-1].detail
        h.clock.now += ps.CACHE_TTL_S
        assert confirmed(h, fleet(h.clock.now)) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert "#2's week resets in" in switches(h)[0].reason

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
    def test_default_for_new_installs(self, tmp_path):
        assert load_settings(tmp_path).strategy == "pace"

    def test_data_dir_without_the_key_moves_to_pace(self, tmp_path):
        """Both of the owner's profiles never set a strategy: they follow
        the default, so upgrading moves them from expiring-first to pace."""
        settings_path(tmp_path).write_text(json.dumps({"autoswitch": {"threshold": 80}}))
        assert load_settings(tmp_path).strategy == "pace"

    @pytest.mark.parametrize("kept", ["expiring-first", "best", "consume-first"])
    def test_an_explicit_choice_is_kept(self, tmp_path, kept):
        set_setting(tmp_path, "autoswitch.strategy", kept)
        assert load_settings(tmp_path).strategy == kept

    def test_margin_spec(self, tmp_path):
        spec = SETTING_SPECS["autoswitch.paceMargin"]
        assert spec.default == 1.5
        assert (spec.lo, spec.hi, spec.step) == (1.0, 5.0, 0.1)
        assert spec.group in dict(SETTING_GROUPS) and spec.label and spec.unit == "x"
        set_setting(tmp_path, "autoswitch.paceMargin", "2.5")
        assert load_settings(tmp_path).pace_margin == 2.5


# ---------------------------------------------------------------------------
# Prompt-cache cost of a move
# ---------------------------------------------------------------------------


def warm_choose(active, *candidates, burn=None, typical=None, waited_s=0.0,
                margin=1.5, threshold=90.0):
    """``choose`` with the local prompt caches warm."""
    return ps.choose(
        active, list(candidates), threshold=threshold, margin=margin,
        hysteresis_pct=10.0, burn_pct_per_h=burn, now=NOW, cold=False,
        waited_s=waited_s, typical_burn_pct_per_h=typical,
    )


class TestCachesCold:
    def test_unknown_activity_counts_as_cold(self):
        assert ps.caches_cold(None, NOW)

    @pytest.mark.parametrize(
        "idle_s, cold", [(0.0, False), (59 * 60.0, False), (3600.0, True), (5 * H, True)]
    )
    def test_cold_after_one_cache_lifetime(self, idle_s, cold):
        assert ps.CACHE_TTL_S == 3600.0  # Claude Code writes 1h cache entries
        assert ps.caches_cold(NOW - idle_s, NOW) is cold


class TestCanWait:
    def test_spare_time_at_the_burn_allows_waiting(self):
        # 40 left (to 90), 30h away: 60 <= 2/h * 29h = 58? no; at 3/h yes.
        target = view("2", seven=50, reset_h=30)
        assert not ps.can_wait(target, threshold=90.0, burn_pct_per_h=2.0, now=NOW)
        assert ps.can_wait(target, threshold=90.0, burn_pct_per_h=3.0, now=NOW)

    def test_unknown_burn_cannot_promise(self):
        target = view("2", seven=0, reset_h=160)
        assert not ps.can_wait(target, threshold=90.0, burn_pct_per_h=None, now=NOW)

    def test_nothing_left_can_always_wait(self):
        target = view("2", seven=90, reset_h=5)
        assert ps.can_wait(target, threshold=90.0, burn_pct_per_h=0.0, now=NOW)


class TestCacheDeferral:
    def test_cold_caches_move_as_before(self):
        a, b = view("1", seven=85, reset_h=50), view("2", seven=63, reset_h=19 + 40 / 60)
        assert ps.choose(
            a, [b], threshold=90.0, margin=1.5, hysteresis_pct=10.0,
            burn_pct_per_h=6.0, now=NOW, cold=True,
        ) == choose(a, b, burn=6.0)

    def test_warm_spread_move_waits_for_idle(self):
        # #2 needs 0.9%/h, #1 0.5%/h: past the margin, under urgent, and #2
        # has days to spend its 30 at 3%/h.
        a = view("1", seven=10, reset_h=160)
        b = view("2", seven=30, reset_h=64)
        assert choose(a, b, burn=3.0).action == "switch"
        got = warm_choose(a, b, burn=3.0)
        assert got.action == "stay" and got.target == "1"
        assert got.waiting_for == "2"
        assert got.reason.startswith("stay: switch → #2 when idle (cache warm); ")
        assert "#2's week resets in" in got.reason
        # Not urgent: it keeps waiting, however long the user works.
        assert warm_choose(a, b, burn=3.0, waited_s=10 * H).waiting_for == "2"

    def test_users_case_moves_to_the_earlier_reset_now(self):
        """The current account has 20% left and resets in 6 days; the other
        has all 100% left and resets in 2. At the user's typical spend it
        cannot be used up if rota waits, so it moves now, caches or not."""
        current = view("1", seven=80, reset_h=6 * 24)
        early = view("2", seven=0, reset_h=2 * 24)
        got = warm_choose(current, early, burn=6.0, typical=2.0, threshold=100.0)
        assert got.action == "switch" and got.target == "2"
        assert got.reason.startswith(
            "switch → #2: #2 has 100% to spend in 2d — moving now; "
        )

    def test_users_case_with_room_to_spare_waits_at_most_the_urgent_wait(self):
        """At a typical spend that could finish it either way, the 15x need
        ratio is urgent: wait for idle caches half an hour, no longer."""
        current = view("1", seven=80, reset_h=6 * 24)
        early = view("2", seven=0, reset_h=2 * 24)
        kw = dict(burn=6.0, typical=6.0, threshold=100.0)
        assert ps.required_pace(early, threshold=100.0, now=NOW) >= 14 * ps.required_pace(
            current, threshold=100.0, now=NOW
        )
        first = warm_choose(current, early, **kw)
        assert first.action == "stay" and first.waiting_for == "2"
        assert warm_choose(current, early, waited_s=ps.URGENT_WAIT_S - 60, **kw).action == "stay"
        late = warm_choose(current, early, waited_s=ps.URGENT_WAIT_S, **kw)
        assert late.action == "switch" and late.target == "2"
        assert late.reason.startswith("switch → #2: waited 30m for idle; ")

    def test_three_accounts_go_to_the_earliest_reset(self):
        current = view("1", seven=80, reset_h=6 * 24)
        mid = view("2", seven=20, reset_h=5 * 24)
        early = view("3", seven=0, reset_h=2 * 24)
        got = warm_choose(current, mid, early, burn=6.0, typical=2.0, threshold=100.0)
        assert (got.action, got.target) == ("switch", "3")
        assert "moving now" in got.reason

    def test_similar_resets_spread_and_stay(self):
        a = view("1", seven=40, reset_h=100)
        b = view("2", seven=45, reset_h=96)
        got = warm_choose(a, b, burn=3.0, typical=3.0)
        assert got.action == "stay" and got.waiting_for is None
        assert "when idle" not in got.reason

    def test_a_small_lead_waits_for_the_cache(self):
        """Ahead by 1.6x (past the 1.5 margin, far from urgent): the move is
        not worth a rebuild while the user works."""
        a = view("1", seven=10, reset_h=100)
        need_a = 80 / 100
        b_left = need_a * 1.6 * 60  # #2 needs 1.6x as much, 60h out
        b = view("2", seven=90 - b_left, reset_h=60)
        got = warm_choose(a, b, burn=5.0, typical=5.0, waited_s=4 * H)
        assert got.action == "stay" and got.waiting_for == "2"

    def test_unknown_burn_does_not_wait(self):
        a = view("1", seven=10, reset_h=160)
        b = view("2", seven=30, reset_h=64)
        got = warm_choose(a, b, burn=None, typical=None)
        assert got.action == "switch"
        assert "moving now" in got.reason

    def test_typical_burn_rules_over_the_duty_burn(self):
        a = view("1", seven=10, reset_h=160)
        b = view("2", seven=30, reset_h=64)
        assert warm_choose(a, b, burn=3.0, typical=0.5).action == "switch"
        assert warm_choose(a, b, burn=0.5, typical=3.0).action == "stay"

    def test_spent_active_move_waits_too_when_the_target_can(self):
        a = view("1", seven=98, reset_h=100)  # under 3% left
        b = view("2", seven=20, reset_h=120)
        assert warm_choose(a, b, burn=None, typical=None, threshold=100.0).action == "switch"
        got = warm_choose(a, b, burn=3.0, typical=3.0, threshold=100.0)
        assert got.action == "stay" and got.waiting_for == "2"


class _StubFeed:
    """A statusline feed that reports one activity time and nothing else."""

    def __init__(self, at):
        self.last_activity_at = at

    def ingest(self):
        from claude_rotater.feed_ingest import IngestResult

        return IngestResult()


def burning(h, *, hours=6):
    """#1's history: 7d 52% -> 70% over ``hours`` (3 points an hour)."""
    history = h.switcher.usage_history
    history.clock = h.clock
    ident = {"1": ("a@example.com", "")}
    start = h.clock.now
    for k in range(hours + 1):
        history.record(
            {"1": usage_at(start, seven=52 + 3 * k, reset_h=100 + hours)},
            ident, start + k * H,
        )
    h.clock.now = start + hours * H


def urgent(h) -> dict:
    """#1 needs 0.2%/h, #2 0.75%/h (3.75x): urgent, and at the burn the
    estimator reads off ``burning`` (well over 1.2 an hour)
    #2's 30 can still be spent in its 40h, so the move may wait."""
    now = h.clock.now
    return {
        "1": usage_at(now, seven=70, reset_h=100, five=10),
        "2": usage_at(now, seven=60, reset_h=40, five=0),
    }


class TestEngineCache:
    def test_warm_with_unknown_burn_moves_at_once(self, temp_home):
        h = make_pace(temp_home)
        h.engine.feed = _StubFeed(h.clock.now)
        assert confirmed(h, owners(h, a="1", b="2")) is TickOutcome.SWITCHED
        assert "moving now" in switches(h)[0].reason

    def test_forced_move_ignores_warm_caches(self, temp_home):
        h = make_pace(temp_home)
        h.engine.feed = _StubFeed(h.clock.now)
        now = h.clock.now
        usage = {
            "1": usage_at(now, seven=100, reset_h=50, five=10),
            "2": usage_at(now, seven=20, reset_h=100, five=0),
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert switches(h)[0].trigger != "pace"

    def test_urgent_hold_ends_after_the_urgent_wait(self, temp_home):
        """The user keeps working: the first evaluations wait for idle
        caches, the one after half an hour moves."""
        h = make_pace(temp_home)
        burning(h)
        feed_ = _StubFeed(h.clock.now)
        h.engine.feed = feed_
        assert h.tick_with_usage(urgent(h)) is TickOutcome.NO_ACTION
        assert "switch → #2 when idle (cache warm)" in no_switch(h)[-1].detail
        h.clock.advance(ps.URGENT_WAIT_S / 2)
        feed_.last_activity_at = h.clock.now
        assert h.tick_with_usage(urgent(h)) is TickOutcome.NO_ACTION
        assert "when idle" in no_switch(h)[-1].detail
        h.clock.advance(ps.URGENT_WAIT_S / 2)
        feed_.last_activity_at = h.clock.now
        assert confirmed(h, urgent(h)) is TickOutcome.SWITCHED
        # Half an hour plus the confirming evaluation.
        assert "waited 32m for idle; " in switches(h)[0].reason

    def test_idle_caches_release_a_held_move(self, temp_home):
        h = make_pace(temp_home)
        burning(h)
        h.engine.feed = _StubFeed(h.clock.now)
        assert h.tick_with_usage(urgent(h)) is TickOutcome.NO_ACTION
        assert "when idle" in no_switch(h)[-1].detail
        h.clock.advance(ps.CACHE_TTL_S)  # no turn since: cold
        assert confirmed(h, urgent(h)) is TickOutcome.SWITCHED
        assert "when idle" not in switches(h)[0].reason

    def test_without_a_feed_the_last_usage_rise_says_warm(self, temp_home):
        h = make_pace(temp_home)
        assert h.engine.feed is None
        burning(h)
        cold, _ = h.engine._cache_inputs("1", h.clock.now)
        assert not cold
        cold, _ = h.engine._cache_inputs("1", h.clock.now + ps.CACHE_TTL_S)
        assert cold


class TestSimulatedCacheCost:
    """``tests/_week_sim.py`` with each warm switch charged the measured
    rebuild (0.35% of a week): waiting for cold caches loses less (expired
    plus rebuilt), blocks no more, and switches less than moving at once;
    with no rebuild charge it expires no more 7d than before."""

    def totals(self, **kw):
        lost = blocked = switches = wasted = 0.0
        for offsets in _week_sim.RESET_OFFSETS_H[:3]:
            for share in _week_sim.DEMAND_SHARES:
                rate = share * 100.0 * len(offsets) / _week_sim.WORK_H_PER_WEEK
                r = _week_sim.simulate("pace", list(offsets), rate=rate, weeks=4, **kw)
                lost += r.lost
                wasted += r.wasted
                blocked += r.blocked_h
                switches += r.switches
        return lost, wasted, blocked, switches

    def test_waiting_for_idle_pays_off(self):
        now = self.totals(switch_cost=0.35)
        idle = self.totals(switch_cost=0.35, idle_aware=True)
        assert idle[0] <= now[0]
        assert idle[2] <= now[2] + 1.0
        assert idle[3] < now[3]

    def test_waiting_costs_no_week_without_a_rebuild(self):
        now = self.totals()
        idle = self.totals(idle_aware=True)
        assert idle[1] <= now[1] * 1.01
