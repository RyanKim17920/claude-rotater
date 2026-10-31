"""Plans without a 5-hour limit: their readings carry 7d alone.

Detected from the readings themselves (never configured), reversible when 5h
comes back, and read as unlimited everywhere: no stale 5h number, projection,
pre-empt, pace constraint or $ per 1% trend past the drop.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from claude_rotater import oauth, preempt
from claude_rotater import pace_strategy as ps
from claude_rotater.forecast import fleet_estimates
from claude_rotater.usage_history import UsageHistory

from tests import _week_sim
from tests.test_pace_strategy import NOW, H, view
from tests.test_tui import make_account, make_entry

IDENT = {"1": ("a@x.com", "org-a"), "2": ("b@x.com", "org-b")}
T0 = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def reading(t: float, *, five: float | None, seven: float) -> dict:
    out = {"seven_day": {"pct": seven, "resets_at": iso(t + 3 * 86400)}}
    if five is not None:
        out["five_hour"] = {"pct": five, "resets_at": iso(T0 + 4 * H)}
    return out


@pytest.fixture
def history(tmp_path):
    clock = {"now": T0}
    h = UsageHistory(tmp_path, clock=lambda: clock["now"])
    h.clock_ref = clock
    return h


def record(history, t: float, slot: str, **kw) -> None:
    history.clock_ref["now"] = t
    history.record({slot: reading(t, **kw)}, IDENT, t)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    @pytest.mark.parametrize("raw", [
        {"five_hour": None, "seven_day": {"utilization": 40.0, "resets_at": "2026-11-02T07:00:00Z"}},
        {"seven_day": {"utilization": 40.0, "resets_at": "2026-11-02T07:00:00Z"}},
    ])
    def test_endpoint_null_or_missing_five_hour(self, raw):
        usage = oauth.build_usage_result(raw)
        assert "five_hour" not in usage
        assert oauth.no_five_hour(usage)

    def test_a_window_not_started_still_has_5h(self):
        usage = oauth.build_usage_result({
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 40.0, "resets_at": "2026-11-02T07:00:00Z"},
        })
        assert not oauth.no_five_hour(usage)

    @pytest.mark.parametrize("usage", [None, {}, {"five_hour": {"pct": 3.0}}])
    def test_no_7d_reading_says_nothing(self, usage):
        assert not oauth.no_five_hour(usage)

    def test_history_marks_when_5h_stopped(self, history):
        record(history, T0, "1", five=10.0, seven=20.0)
        record(history, T0 + 600, "1", five=12.0, seven=21.0)
        record(history, T0 + 1200, "1", five=None, seven=22.0)
        record(history, T0 + 1800, "1", five=None, seven=23.0)
        assert history.ended(IDENT) == {"1": {"5h": T0 + 1200}}

    def test_5h_coming_back_clears_it(self, history):
        record(history, T0, "1", five=None, seven=20.0)
        record(history, T0 + 600, "1", five=5.0, seven=21.0)
        assert history.ended(IDENT) == {}

    def test_never_had_5h_ends_at_its_first_reading(self, history):
        record(history, T0, "2", five=None, seven=20.0)
        record(history, T0 + 600, "2", five=None, seven=21.0)
        assert history.ended(IDENT) == {"2": {"5h": T0}}

    def test_a_reading_without_7d_decides_nothing(self, history):
        record(history, T0, "1", five=10.0, seven=20.0)
        history.clock_ref["now"] = T0 + 600
        history.record({"1": {"scoped": [{"name": "Fable", "pct": 3.0}]}}, IDENT, T0 + 600)
        assert history.ended(IDENT) == {}

    def test_live_series_drops_the_ended_window_series_keeps_it(self, history):
        record(history, T0, "1", five=10.0, seven=20.0)
        record(history, T0 + 600, "1", five=None, seven=21.0)
        assert "5h" in history.series(IDENT)["1"]
        assert set(history.live_series(IDENT)["1"]) == {"7d"}


# ---------------------------------------------------------------------------
# Forecasts: old 5h history projects nothing
# ---------------------------------------------------------------------------


def steep_then_dropped(history) -> float:
    """5h climbing ~60%/h with its reset hours away, then the plan drops 5h."""
    for i in range(6):
        record(history, T0 + i * 300, "1", five=10.0 + 5 * i, seven=20.0 + 0.2 * i)
    for i in range(6, 9):
        record(history, T0 + i * 300, "1", five=None, seven=20.0 + 0.2 * i)
    return T0 + 9 * 300


class TestForecastsReadItUnlimited:
    def test_estimates_have_no_5h_window(self, history, tmp_path):
        now = steep_then_dropped(history)
        est, series = fleet_estimates(
            history, tmp_path, IDENT, active="1", models=(), threshold=90.0, now=now,
        )
        assert "5h" not in est["1"].windows and "5h" not in series["1"]
        assert est["1"].binding is None or est["1"].binding.window != "5h"

    def test_preempt_never_projects_5h(self, history):
        now = steep_then_dropped(history)
        live = {"7d": (21.8, now + 3 * 86400)}
        stale = preempt.project(
            history.series(IDENT)["1"], live, models=(), now=now, since=None, horizon_s=600,
        )
        assert stale is not None and stale.window == "5h"  # the bug this guards
        fresh = preempt.project(
            history.live_series(IDENT)["1"], live, models=(), now=now, since=None, horizon_s=600,
        )
        assert fresh is None or fresh.window != "5h"


# ---------------------------------------------------------------------------
# Pace: no 5h is unlimited room, never a constraint
# ---------------------------------------------------------------------------


class TestPace:
    def test_view_without_5h_has_full_room(self):
        v = view("1", seven=50.0, reset_h=48.0, five=None)
        assert v.five_hour_headroom == 100.0

    def test_tie_goes_to_the_account_without_5h(self):
        tight = view("2", seven=50.0, reset_h=48.0, five=70.0)
        unlimited = view("3", seven=50.0, reset_h=48.0, five=None)
        order = ps.rank([tight, unlimited], threshold=90.0, margin=1.5,
                        burn_pct_per_h=None, now=NOW)
        assert order[0] == "3"

    def test_no_5h_text_for_an_account_without_5h(self):
        active = view("1", seven=10.0, reset_h=150.0, five=None)
        target = view("2", seven=20.0, reset_h=20.0, five=None)
        choice = ps.choose(active, [target], threshold=90.0, margin=1.5, hysteresis_pct=10.0,
                           burn_pct_per_h=None, now=NOW)
        assert choice.action == "switch" and "5h" not in choice.reason

    def test_sim_account_without_5h_blocks_no_more(self):
        offsets = [20.0, 70.0, 120.0]
        base = _week_sim.simulate("pace", offsets, rate=4.0, weeks=2)
        lifted = _week_sim.simulate("pace", offsets, rate=4.0, weeks=2, no_five_hour={1})
        assert lifted.blocked_h <= base.blocked_h
        assert lifted.wasted <= base.wasted + 1e-6


# ---------------------------------------------------------------------------
# Rows, detail view, clipboard
# ---------------------------------------------------------------------------


def _layout(width=120):
    from claude_rotater.tui.main import RowLayout

    return RowLayout.for_width(width, ("5h", "7d"))


class TestRows:
    def _row(self, acc, width=120):
        from claude_rotater.tui.main import account_row_text

        return account_row_text(acc, None, _layout(width), now=time.time(), threshold=90.0).plain

    def test_row_says_no_5h_and_keeps_the_columns(self):
        with_5h = self._row(make_account(1, entry=make_entry(40.0, 30.0), email="a@x.com"))
        without = self._row(make_account(2, entry=make_entry(None, 30.0), email="b@x.com"))
        assert "no 5h" in without and "%" not in without.split("7d")[0]
        assert with_5h.index("7d") == without.index("7d")

    @pytest.mark.parametrize("width", [60, 80, 100, 160])
    def test_alignment_at_every_width(self, width):
        a = self._row(make_account(1, entry=make_entry(40.0, 30.0), email="a@x.com"), width)
        b = self._row(make_account(2, entry=make_entry(None, 30.0), email="b@x.com"), width)
        assert a.index("7d") == b.index("7d")

    def test_cell_is_dim(self):
        from claude_rotater.tui.main import window_cell
        from claude_rotater.tui.theme import Palette

        cell = window_cell("5h", None, _layout(), now=0.0, threshold=90.0,
                           palette=Palette.DARK, unlimited=True)
        assert cell.plain.startswith("no 5h")
        assert cell.cell_len == _layout().window_w("5h")
        assert all("dim" in str(sp.style) for sp in cell.spans)

    def test_detail_shows_7d_alone(self):
        from claude_rotater.tui.main import chart_blocks
        from claude_rotater.tui.theme import Palette

        acc = make_account(1, entry=make_entry(None, 30.0))
        blocks = chart_blocks(acc, None, {}, now=time.time(), threshold=90.0,
                              palette=Palette.DARK)
        assert [t.plain.split(" ")[0] for t, _ in blocks] == ["7d"]

    def test_clipboard_line_says_no_5h_limit(self):
        from claude_rotater.tui.main import summary_line

        line = summary_line(make_account(1, entry=make_entry(None, 30.0)), None,
                            threshold=90.0, now=time.time())
        assert "no 5h limit" in line and "5h " not in line.replace("no 5h limit", "")


# ---------------------------------------------------------------------------
# Costs: the 5h $ per 1% stops at the drop
# ---------------------------------------------------------------------------


class TestCosts:
    def _report(self, no_limit):
        from tests.test_tui_costs import report_with_trend

        return report_with_trend(no_limit)

    def test_table_says_no_5h(self):
        from claude_rotater.tui.costs import account_lines
        from claude_rotater.tui.theme import Palette

        rep = self._report({"5h": time.time() - 86400})
        text = "\n".join(t.plain for t in account_lines(rep, 100, 10, {}, Palette.DARK))
        assert "no 5h" in text

    def test_trend_keeps_old_points_and_says_since(self):
        from claude_rotater.tui.costs import trend_chart
        from claude_rotater.tui.theme import Palette

        dropped = time.time() - 86400
        rep = self._report({"5h": dropped})
        lines = [t.plain for t in trend_chart(rep, "5h", 80, 8, {}, Palette.DARK)]
        since = time.strftime("%m-%d", time.localtime(dropped))
        assert any(f"no 5h limit since {since}" in ln for ln in lines)
        assert any("●" in ln or "○" in ln for ln in lines[:8])  # old points still drawn

    def test_trendline_stops_at_the_drop(self):
        from claude_rotater.tui.costs import trend_chart
        from claude_rotater.tui.theme import Palette

        plot = lambda rep: [t.plain for t in trend_chart(rep, "5h", 80, 8, {}, Palette.DARK)][:8]  # noqa: E731
        full = plot(self._report({}))
        cut = plot(self._report({"5h": time.time() - 3 * 86400}))
        last_col = lambda lines: max(  # noqa: E731
            (max(i for i, ch in enumerate(ln) if ch in "┄·") for ln in lines
             if any(ch in "┄·" for ch in ln)), default=-1)
        assert last_col(cut) < last_col(full)
