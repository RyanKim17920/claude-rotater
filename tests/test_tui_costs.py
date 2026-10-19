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
    Estimate,
    ModelLine,
)
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
    accounts = tuple(
        AccountLine(str(i + 1), 80.0 / (i + 1),
                    Estimate(3.1 / (i + 1), 0.2, 6) if i == 0 else Estimate(1.8, None, 1),
                    Estimate(9.4, 0.35, 3) if i == 0 else None)
        for i in range(n_accounts)
    )
    return CostReport(now=now, days=days, today=12.3, week=245.0, month=1020.0, week_change=0.18,
                      models=models, accounts=accounts, unattributed=0.0, built_at=now - 120,
                      took_s=1.2)


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


def test_estimates_are_marked_approximate_with_their_spread():
    assert view.estimate_text(Estimate(3.104, 0.2, 5)) == "~$3.10 ±20%"
    assert view.estimate_text(Estimate(3.1, None, 1)) == "~$3.10 ±?"
    assert view.estimate_text(None) == "–"


def test_trend_words():
    assert view.trend_words(0.18) == "↑ 18%"
    assert view.trend_words(-0.4) == "↓ 40%"
    assert view.trend_words(0.05) == "→"
    assert view.trend_words(None) == ""
    assert view.arrow(None) == "new"


def test_summary_line_is_one_sentence_of_numbers():
    text = view.summary_text(report(), Palette.DARK).plain
    assert text == "today $12.3 · 7d $245 · 30d $1,020   ↑ 18% vs last week · Opus 4.7 60%"


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


def test_the_note_and_gaps_go_before_the_chart_shrinks():
    rep = report(n_models=3, n_accounts=2)
    roomy = view.costs_lines(rep, width=118, height=36, names={}, palette=Palette.DARK)
    assert roomy[-1].plain.startswith("~ estimate")
    assert any(line.plain == "" for line in roomy)
    tight = view.costs_lines(rep, width=78, height=12, names={}, palette=Palette.DARK)
    assert not tight[-1].plain.startswith("~ estimate")
    tiny = view.costs_lines(rep, width=78, height=8, names={}, palette=Palette.DARK)
    assert "daily cost" not in "\n".join(line.plain for line in tiny)  # chart gone first


def test_many_models_fold_into_one_other_row():
    lines = view.model_lines(report(n_models=7), 118, view.MODEL_ROWS, Palette.DARK)
    assert len(lines) == 1 + view.MODEL_ROWS
    assert "3 other" in lines[-1].plain


def test_chart_bars_follow_the_daily_totals_and_model_colors():
    rep = report(n_models=2)
    lines = view.chart_lines(rep, 98, 6, Palette.DARK)
    assert len(lines) == 7 and lines[-1].plain.rstrip().endswith("today")
    assert lines[0].plain.lstrip().startswith("$")  # the peak labels the top row
    colors = {span.style for line in lines[:-1] for span in line.spans}
    assert Palette.DARK.accent in colors and Palette.DARK.sev_ok in colors
    # the tallest day reaches the top row; a day of zero draws nothing
    flat = report(n_models=1)
    flat = CostReport(**{**flat.__dict__, "days": tuple((d, {}) for d, _ in flat.days)})
    empty = view.chart_lines(flat, 98, 4, Palette.DARK)
    assert all(set(line.plain[6:]) <= {" "} for line in empty[:-1])


def test_account_names_come_from_the_table_and_unknown_numbers_show_as_such():
    lines = view.account_lines(report(), 118, 5, {"1": "me@x.com"}, Palette.DARK)
    assert lines[1].plain.startswith("me@x.com") and "~$3.10 ±20%" in lines[1].plain
    assert lines[2].plain.startswith("#2") and "–" in lines[2].plain


def test_plain_page_has_every_part():
    text = view.plain_page(report(), {"1": "me@x.com"})
    for part in ("today $12.3", "daily cost", "model", "me@x.com", "~ estimate"):
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
            assert "me@x.com" in text and "~$3.10 ±20%" in text
            assert "daily cost" in text and "Opus 4.7" in text
            costs_view = app.screen.query_one("#costs")
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
