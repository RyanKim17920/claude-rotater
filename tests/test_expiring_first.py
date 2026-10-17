"""Tests for the expiring-first policy (pure decision math)."""

from __future__ import annotations

import math

import pytest

from claude_rotater import expiring_first as ef

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
