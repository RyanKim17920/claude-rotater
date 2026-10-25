"""The costs view (``$``): page layout at every size, and the keys around it.

The data source is a fake (:class:`FakeCosts`) handing back a fixed report,
so no transcript is read and no ccusage runs; tests/test_costs.py covers the
data. Every page check runs at 80x24, 120x40 and 160x50, in both themes.
"""

from __future__ import annotations

import threading
import time

import pytest

from claude_rotater import costs
from claude_rotater.cost_report import (
    AccountLine,
    CostReport,
    CostResult,
    ModelLine,
)
from claude_rotater.dollar_per_pct import PerPct
from claude_rotater.settings import set_setting
from claude_rotater.tui import costs as view
from claude_rotater.tui.main import MainScreen
from claude_rotater.tui.theme import Palette
from tests.test_tui import (  # noqa: F401  (autouse fixtures)
    fake_engine,
    fake_refresher,
    make_app,
    settle,
    three_accounts,
)

SIZES = [(80, 24), (120, 40), (160, 50)]


def report(now: float | None = None, *, n_models: int = 3, n_accounts: int = 2) -> CostReport:
    now = now or time.time()
    names = ["Opus 4.7", "Sonnet 4.5", "Haiku 4.5", "Opus 4.1", "Sonnet 4", "Haiku 3.5", "Opus 4"]
    models = tuple(
        ModelLine(names[i], 100.0 / (i + 1), 0.6 / (i + 1), 1200 * (i + 1), 340_000, 12_000_000,
                  2_100_000_000, [0.18, -0.4, None, 0.02, None, None, None][i])
        for i in range(n_models)
    )
    days = tuple(
        (costs.day_start(now, back), {m.name: (back % 5 + 1) * 3.0 / (j + 1) for j, m in enumerate(models)})
        for back in range(costs.RANGE_DAYS - 1, -1, -1)
    )
    first_hour = (now // 3600 - 23) * 3600
    hours = tuple((first_hour + h * 3600, {m.name: (h % 6) * 1.5 / (j + 1) for j, m in enumerate(models)})
                  for h in range(24))
    duty = tuple("1" if h < 16 else "2" for h in range(24))
    accounts = tuple(
        AccountLine(str(i + 1), 80.0 / (i + 1),
                    PerPct(3.1, True, -0.12, False, (3.4, 3.3, 3.2, 3.1)) if i == 0
                    else PerPct(1.8, False, None, True, ()),
                    PerPct(9.4, True, 0.03, True, (9.3, 9.5)) if i == 0 else None)
        for i in range(n_accounts)
    )
    return CostReport(now=now, days=days, hours=hours, hour_duty=duty, today=12.3, week=245.0,
                      month=1020.0, week_change=0.18, models=models, accounts=accounts,
                      unattributed=0.0, built_at=now - 120, took_s=1.2)


class FakeCosts:
    """A cost source that never touches a transcript."""

    def __init__(self, result: CostResult | None = None, *, cached: CostResult | None = None):
        self.result = result or CostResult(report(), ran=True)
        self.cached_result = cached or CostResult(None)
        self.calls: list[tuple] = []
        self.gate: threading.Event | None = None

    def cached(self, identities):
        self.calls.append(("cached", dict(identities)))
        return self.cached_result

    def refresh(self, identities, *, force=False):
        self.calls.append(("refresh", force))
        if self.gate is not None:
            self.gate.wait(5)
        return self.result


def costs_app(tmp_path, source: FakeCosts, theme: str = "dark"):
    set_setting(tmp_path, "ui.theme", theme)
    app = make_app(three_accounts(tmp_path))
    app._cost_source = source
    return app


def page(app) -> str:
    return app.screen.query_one("#costs").render().plain


# -- formatting -------------------------------------------------------------------


@pytest.mark.parametrize("usd,text", [(0.5, "$0.50"), (12.34, "$12.3"), (245.4, "$245"),
                                      (5757.0, "$5,757"), (123456.0, "$123k")])
def test_money(usd, text):
    assert view.money(usd) == text


@pytest.mark.parametrize("n,text", [(12, "12"), (3400, "3.4k"), (12_000_000, "12M"),
                                    (2_100_000_000, "2.1B")])
def test_tokens(n, text):
    assert view.tokens(n) == text


def test_per_pct_figures_are_approximate_and_say_when_unsure():
    assert view.per_pct_value(PerPct(3.104, True, None, True, ())) == "~$3.10"
    assert view.per_pct_value(PerPct(3.1, False, None, True, ())) == "~$3.10?"
    assert view.per_pct_value(None) == "–"


def test_per_pct_trend_is_per_four_weeks_and_steady_within_the_noise():
    assert view.per_pct_trend(PerPct(3.1, True, -0.123, False, ())) == "↓12%/4w"
    assert view.per_pct_trend(PerPct(3.1, True, 0.08, False, ())) == "↑8%/4w"
    assert view.per_pct_trend(PerPct(3.1, True, 0.3, True, ())) == "steady"
    assert view.per_pct_trend(PerPct(3.1, True, None, True, ())) == ""
    assert view.per_pct_trend(None) == ""


def test_spark_spans_lowest_to_highest():
    assert view.spark([1.0, 2.0, 3.0]) == "▁▅█"
    assert view.spark([2.0, 2.0]) == "▄▄"


@pytest.mark.parametrize("peak,step", [(7, 2), (140, 50), (3400, 1000), (1415, 500),
                                       (99, 20), (0.3, 0.1), (25, 10), (4, 1)])
def test_nice_step_puts_one_to_four_lines_under_the_peak(peak, step):
    assert view.nice_step(peak) == pytest.approx(step)
    assert 1 <= peak // view.nice_step(peak) <= view.MAX_GRID_LINES


@pytest.mark.parametrize("usd,text", [(0.5, "$0.5"), (20, "$20"), (500, "$500"), (1000, "$1k"),
                                      (1500, "$1.5k"), (20000, "$20k")])
def test_short_money(usd, text):
    assert view.short_money(usd) == text


def test_grid_rows_sit_where_their_value_reaches_and_never_share_a_row():
    assert view.grid_rows(3400, 10) == {2: "$1k", 5: "$2k", 8: "$3k"}
    assert view.grid_rows(140, 7) == {2: "$50", 4: "$100"}
    assert view.grid_rows(100, 2) == {1: "$100"}  # a short chart gets one line
    assert view.grid_rows(0, 5) == {}


def test_trend_words():
    assert view.trend_words(0.18) == "↑ 18%"
    assert view.trend_words(-0.4) == "↓ 40%"
    assert view.trend_words(0.05) == "→"
    assert view.trend_words(None) == ""
    assert view.arrow(None) == "new"


def test_summary_line_is_one_sentence_of_numbers():
    text = view.summary_text(report(), Palette.DARK).plain
    assert text == "today $12.3 · 7d $245 · 30d $1,020   ↑ 18% vs last week"


# -- layout -------------------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(78, 20), (118, 36), (158, 46), (60, 16), (78, 8)])
def test_page_fits_and_keeps_the_essentials(width, height):
    rep = report(n_models=7, n_accounts=4)
    lines = view.costs_lines(rep, width=width, height=height, names={}, palette=Palette.DARK)
    text = "\n".join(line.plain for line in lines)
    assert len(lines) <= height
    assert all(line.cell_len <= width for line in lines[1:])  # the summary may ellipsize
    assert lines[0].plain.startswith("today $12.3")
    assert "$/1% 5h" in text and "#1" in text  # the accounts always stay


def test_the_freshness_note_goes_first_when_the_summary_is_too_wide():
    rep = report()
    wide = view.costs_lines(rep, width=118, height=36, names={}, palette=Palette.DARK,
                            status="updated 2m ago")
    assert wide[0].plain.endswith("updated 2m ago")
    narrow = view.costs_lines(rep, width=60, height=36, names={}, palette=Palette.DARK,
                              status="updated 2m ago")
    assert "updated" not in narrow[0].plain and narrow[0].cell_len <= 60


def test_the_note_and_gaps_go_before_the_chart_shrinks():
    rep = report(n_models=3, n_accounts=2)
    roomy = view.costs_lines(rep, width=118, height=36, names={}, palette=Palette.DARK)
    assert roomy[-1].plain.startswith("~ estimate")
    assert any(line.plain == "" for line in roomy)
    tight = view.costs_lines(rep, width=78, height=13, names={}, palette=Palette.DARK)
    assert not tight[-1].plain.startswith("~ estimate")
    assert "hourly" in "\n".join(line.plain for line in tight)
    tiny = view.costs_lines(rep, width=78, height=8, names={}, palette=Palette.DARK)
    assert "daily" not in "\n".join(line.plain for line in tiny)  # chart gone first


def test_many_models_fold_into_one_other_row():
    lines = view.model_lines(report(n_models=7), 118, view.MODEL_ROWS, Palette.DARK)
    assert len(lines) == 1 + view.MODEL_ROWS
    assert "3 other" in lines[-1].plain


def test_chart_bars_follow_the_daily_totals_and_model_colors():
    rep = report(n_models=2)
    lines = view.daily_chart(rep, 98, 6, Palette.DARK)
    assert len(lines) == 7 and lines[-1].plain.rstrip().endswith("today")
    colors = {span.style for line in lines[:-1] for span in line.spans}
    assert Palette.DARK.accent in colors and Palette.DARK.sev_ok in colors
    # the tallest day reaches the top row; a day of zero draws nothing, not even a gridline
    flat = report(n_models=1)
    flat = CostReport(**{**flat.__dict__, "days": tuple((d, {}) for d, _ in flat.days)})
    empty = view.daily_chart(flat, 98, 4, Palette.DARK)
    assert all(set(line.plain[3:]) <= {" "} for line in empty[:-1])


def test_gridlines_are_dashed_dim_and_labelled_in_round_dollars():
    rep = report(n_models=1)  # days peak at $15: lines at $5, $10, $15
    lines = view.daily_chart(rep, 98, 9, Palette.DARK)
    labelled = [line.plain for line in lines[:-1] if line.plain.strip().startswith("$")]
    assert [p.split()[0] for p in labelled] == ["$15", "$10", "$5"]
    assert all("╌" in p for p in labelled)
    dashes = [span.style for line in lines for span in line.spans
              if "╌" in line.plain[span.start:span.end]]
    assert dashes and all(style.startswith("dim") for style in dashes)


def test_hourly_chart_shows_the_last_day_and_who_was_on_duty():
    rep = report(n_models=2)
    lines = view.hourly_chart(rep, 60, 5, Palette.DARK)
    assert lines[-1].plain.rstrip().endswith("now") and "hourly" in lines[-1].plain
    duty = lines[-2].plain
    assert duty.lstrip().startswith("on #1") and duty.count("#2") == 1  # marks where it changes
    twelve = view.hourly_chart(rep, 40, 5, Palette.DARK, hours=12)
    first = time.strftime("%H:00", time.localtime(rep.hours[12][0]))
    assert first in twelve[-1].plain


def test_duty_marks_never_run_into_each_other_or_off_the_end():
    row = view._duty_row(["1", "2", "3", "3", "3", "3", "3", "4"], 1, 3, Palette.DARK).plain
    assert row == "on #1 #3   "  # #2 had no room; #3 waits for some; #4 would run off the end


@pytest.mark.parametrize("width", [118, 78, 58, 40])
def test_charts_sit_side_by_side_when_wide_and_stack_when_narrow(width):
    rep = report(n_models=2)
    lines = view.chart_lines(rep, width, 4, Palette.DARK)
    text = "\n".join(line.plain for line in lines)
    assert all(line.cell_len <= width for line in lines)
    assert len(lines) == view.chart_rows(rep, width, 4, hourly_only=False)
    if width >= view.SIDE_BY_SIDE_W:
        assert "daily" in lines[-1].plain and "hourly" in lines[-1].plain  # axes level
    else:
        assert text.index("daily") < text.index("hourly")


def test_account_names_come_from_the_table_and_unknown_numbers_show_as_such():
    lines = view.account_lines(report(), 118, 5, {"1": "me@x.com"}, Palette.DARK)
    assert lines[1].plain.startswith("me@x.com")
    assert "~$3.10  █▆▃▁ ↓12%/4w" in lines[1].plain
    assert "~$9.40  steady" in lines[1].plain  # two windows make no sparkline
    assert lines[2].plain.startswith("#2") and "~$1.80?" in lines[2].plain and "–" in lines[2].plain


def test_an_unsure_figure_is_saffron_and_a_falling_one_warns():
    lines = view.account_lines(report(), 118, 5, {}, Palette.DARK)
    styles = {lines[2].plain[s.start:s.end].strip(): s.style for s in lines[2].spans}
    assert styles["~$1.80?"] == Palette.DARK.sev_warn
    falling = {lines[1].plain[s.start:s.end].strip(): s.style for s in lines[1].spans}
    assert falling["↓12%/4w"] == Palette.DARK.sev_warn


def test_narrow_account_table_drops_the_sparklines_first():
    lines = view.account_lines(report(), 64, 5, {"1": "me@x.com"}, Palette.DARK)
    assert "█" not in lines[1].plain and "↓12%/4w" in lines[1].plain
    assert all(line.cell_len <= 64 for line in lines)


def test_plain_page_has_every_part():
    text = view.plain_page(report(), {"1": "me@x.com"})
    for part in ("today $12.3", "daily", "hourly", "model", "me@x.com", "$/1% 5h", "~ estimate"):
        assert part in text


# -- the page in the app ---------------------------------------------------------------


@pytest.mark.asyncio
class TestCostsPage:
    @pytest.mark.parametrize("theme", ["dark", "light"])
    @pytest.mark.parametrize("size", SIZES)
    async def test_dollar_shows_the_costs_in_place_of_the_body(self, tmp_path, size, theme):
        source = FakeCosts()
        app = costs_app(tmp_path, source, theme)
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            assert isinstance(app.screen, MainScreen)  # same page, no popup
            assert app.screen.costs_shown and not app.screen.query_one("#body").display
            text = page(app)
            lines = text.splitlines()
            assert lines[0].startswith("today $12.3 · 7d $245 · 30d $1,020")
            assert "me@x.com" in text and "~$3.10" in text
            assert "daily" in text and "hourly" in text and "Opus 4.7" in text
            costs_view = app.screen.query_one("#costs")
            assert costs_view.render().no_wrap  # a line too long is cut, never wrapped
            screen = ["".join(seg.text for seg in strip)
                      for strip in app.screen._compositor.render_strips()]
            assert screen[2].strip().startswith("today") and screen[3].strip() == ""
            assert costs_view.region.bottom <= app.screen.query_one("#prompt").region.y
            assert all(len(line) <= size[0] - 2 for line in lines[1:])
            assert app.screen.query_one("#status").region.y == 0  # the header stays
            hint = app.screen.prompt.render().plain
            assert "r refresh" in hint and "esc back" in hint
            assert [c[0] for c in source.calls] == ["cached", "refresh"]
            assert source.calls[0][1]["1"] == ("me@x.com", app.snapshot.accounts[0].org_uuid)
            await pilot.press("escape")
            await settle(pilot)
            assert not app.screen.costs_shown and app.screen.query_one("#body").display
            assert "$ costs" in app.screen.prompt.render().plain

    async def test_dollar_again_goes_back_and_reopening_shows_the_last_report_at_once(self, tmp_path):
        source = FakeCosts()
        app = costs_app(tmp_path, source)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            assert not app.screen.costs_shown
            source.gate = threading.Event()  # the next refresh hangs
            await pilot.press("dollar_sign")
            await pilot.pause()
            assert page(app).startswith("today $12.3")  # no wait for the refresh
            assert "updating…" in page(app)
            source.gate.set()
            await settle(pilot)
            assert "updated 2m ago" in page(app)

    async def test_r_reprices_today(self, tmp_path):
        source = FakeCosts()
        app = costs_app(tmp_path, source)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            await pilot.press("r")
            await settle(pilot)
            assert source.calls[-1] == ("refresh", True)

    async def test_first_open_says_it_is_reading_then_shows_the_report(self, tmp_path):
        source = FakeCosts()
        source.gate = threading.Event()
        app = costs_app(tmp_path, source)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await pilot.pause()
            assert page(app) == view.LOADING
            source.gate.set()
            await settle(pilot)
            assert page(app).startswith("today")

    async def test_missing_ccusage_is_one_line(self, tmp_path):
        source = FakeCosts(CostResult(None, costs.MISSING_HINT))
        app = costs_app(tmp_path, source)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            assert page(app) == "install ccusage or Node to see costs"

    async def test_a_note_with_a_report_goes_to_the_prompt_line(self, tmp_path):
        source = FakeCosts(CostResult(report(), "ccusage failed: boom"))
        app = costs_app(tmp_path, source)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            assert page(app).startswith("today")
            assert "ccusage failed: boom" in app.screen.prompt.render().plain

    async def test_a_crashing_source_never_takes_the_page_down(self, tmp_path):
        class Broken(FakeCosts):
            def refresh(self, identities, *, force=False):
                raise RuntimeError("disk on fire")

        app = costs_app(tmp_path, Broken())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            assert "costs failed: disk on fire" in page(app)

    async def test_y_and_Y_copy_the_summary_and_the_page(self, tmp_path, monkeypatch):
        app = costs_app(tmp_path, FakeCosts())
        copied: list[tuple[str, str]] = []
        async with app.run_test(size=(120, 40)) as pilot:
            monkeypatch.setattr(app, "copy_text", lambda text, what: copied.append((what, text)))
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            await pilot.press("y")
            await pilot.press("Y")
            await pilot.pause()
        assert copied[0] == ("the cost summary", view.summary_text(report(), Palette.DARK).plain)
        assert copied[1][0] == "the costs" and "~ estimate" in copied[1][1]

    async def test_account_keys_stand_down_while_the_costs_show(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            await pilot.press("x", "c", "question_mark")
            await pilot.pause()
            assert app.screen.costs_shown
            assert not app.screen.config_shown and not app.screen.keys_shown
            assert "remove" not in app.screen.prompt.render().plain

    async def test_the_key_list_names_the_costs(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert "$      costs (esc back)" in app.screen.query_one("#detail").render().plain
            await pilot.press("dollar_sign")  # straight from the key list
            await settle(pilot)
            assert app.screen.costs_shown and not app.screen.keys_shown

    async def test_theme_change_repaints_the_page(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            await pilot.press("ctrl+t")
            await settle(pilot)
            assert page(app).startswith("today")
