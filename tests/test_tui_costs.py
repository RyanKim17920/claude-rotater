"""The costs view (``$``): page layout at every size, and the keys around it.

The data source is a fake (:class:`FakeCosts`) handing back a fixed report,
so no transcript is read and no ccusage runs; tests/test_costs.py covers the
data. Every page check runs at 80x24, 120x40 and 160x50, in both themes.
"""

from __future__ import annotations

import math
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
from claude_rotater.dollar_per_pct import Fit, PerPct
from claude_rotater.settings import set_setting
from claude_rotater.tui import costs as view
from claude_rotater.tui.main import MainScreen
from claude_rotater.tui.theme import Palette
from tests.test_tui import (  # noqa: F401  (autouse fixtures)
    fake_engine,
    fake_refresher,
    make_app,
    prompt_plain,
    settle,
    three_accounts,
)

SIZES = [(80, 24), (120, 40), (160, 50)]


def trend_fig(dollars: float, *, sure: bool = True, change: float = 0.0, steady: bool = True,
              provisional: bool = False, days: int = 20, n: int = 6) -> PerPct:
    """A $ per 1% figure with ``n`` windows over ``days`` on a known line."""
    now = time.time()
    slope = math.log1p(change) / 28 if not steady else 0.0
    t0 = now - days * 86400 / 2
    fit = Fit(t0, math.log(dollars), slope, steady, provisional, n, days * 86400.0)
    points = tuple((now - days * 86400 + i * days * 86400 / max(1, n - 1),
                    fit.at(now - days * 86400 + i * days * 86400 / max(1, n - 1)), sure or i % 2 == 0)
                   for i in range(n))
    return PerPct(dollars, sure, fit, points)


def report(now: float | None = None, *, n_models: int = 3, n_accounts: int = 2,
           archive_days: int = 0) -> CostReport:
    now = now or time.time()
    names = ["Opus 4.7", "Sonnet 4.5", "Haiku 4.5", "Opus 4.1", "Sonnet 4", "Haiku 3.5", "Opus 4"]
    models = tuple(
        ModelLine(names[i], 100.0 / (i + 1), 0.6 / (i + 1), 1200 * (i + 1), 340_000, 12_000_000,
                  2_100_000_000, [0.18, -0.4, None, 0.02, None, None, None][i])
        for i in range(n_models)
    )
    def spread(back: int) -> dict[str, float]:
        return {m.name: (back % 5 + 1) * 3.0 / (j + 1) for j, m in enumerate(models)}

    days = tuple((costs.day_start(now, back), spread(back))
                 for back in range(costs.RANGE_DAYS - 1, -1, -1))
    older = tuple((costs.day_start(now, back), spread(back))
                  for back in range(costs.RANGE_DAYS + archive_days - 1, costs.RANGE_DAYS - 1, -1))
    first_hour = (now // 3600 - 167) * 3600
    hours = tuple((first_hour + h * 3600, {m.name: (h % 6) * 1.5 / (j + 1) for j, m in enumerate(models)})
                  for h in range(168))
    duty = tuple("1" if h < 160 else "2" for h in range(168))
    accounts = tuple(
        AccountLine(str(i + 1), 80.0 / (i + 1),
                    trend_fig(3.1, change=-0.12, steady=False) if i == 0
                    else PerPct(1.8, False),
                    trend_fig(9.4) if i == 0 else None)
        for i in range(n_accounts)
    )
    def cells(buckets, attributed=True):  # two thirds to #1, the rest to #2 (#1 alone with one)
        share = {"1": 2 / 3, "2": 1 / 3} if n_accounts > 1 else {"1": 1.0}
        out = []
        for t, m in buckets:
            if not attributed:  # days older than the priced range never had an account
                out.append((t, {("", name): usd for name, usd in m.items()}))
                continue
            out.append((t, {(who, name): usd * f for name, usd in m.items()
                            for who, f in share.items() if usd * f > 0}))
        return tuple(out)

    history_cells = cells(older, False) + cells(days)
    hour_cells = cells(hours)
    return CostReport(now=now, days=days, history=older + days, hours=hours, hour_duty=duty,
                      history_cells=history_cells, hour_cells=hour_cells,
                      history_tokens=token_cells(history_cells), hour_tokens=token_cells(hour_cells),
                      today=12.3, week=245.0, month=1020.0, week_change=0.18, models=models,
                      accounts=accounts, unattributed=0.0, built_at=now - 120, took_s=1.2)


TOKENS_PER_USD = (100.0, 2_000.0, 50_000.0, 1_000.0)  # input, cache write, cache read, output


def token_cells(buckets):
    """Token counts for dollar cells: each dollar is :data:`TOKENS_PER_USD`."""
    return tuple((t, {cell: tuple(usd * k for k in TOKENS_PER_USD) for cell, usd in c.items()})
                 for t, c in buckets)


def with_days(rep: CostReport, days) -> CostReport:
    """``rep`` with its priced days replaced (all credited to #1)."""
    older = len(rep.history) - len(rep.days)
    cells = tuple((t, {("1", m): usd for m, usd in d.items()}) for t, d in days)
    return CostReport(**{**rep.__dict__, "days": tuple(days),
                         "history": rep.history[:older] + tuple(days),
                         "history_cells": rep.history_cells[:older] + cells,
                         "history_tokens": rep.history_tokens[:older] + token_cells(cells)})


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
    assert view.per_pct_value(PerPct(3.104, True)) == "~$3.10"
    assert view.per_pct_value(PerPct(3.1, False)) == "~$3.10?"
    assert view.per_pct_value(None) == "–"


P = Palette.DARK


@pytest.mark.parametrize("fig,words,style", [
    (trend_fig(3.0), "steady", P.foreground),
    (trend_fig(3.0, change=-0.12, steady=False), "↓12%/4w limit shrinking", P.sev_warn),
    (trend_fig(3.0, change=-0.30, steady=False), "↓30%/4w limit shrinking", P.sev_crit),
    (trend_fig(3.0, change=0.08, steady=False), "↑8%/4w more room", P.sev_ok),
    (trend_fig(3.0, provisional=True, days=8), "~ steady, provisional: 8 days of data",
     f"dim {P.foreground}"),
    # a few days stretched to 4 weeks would overstate it: the change over its span
    (trend_fig(3.0, change=-0.3, steady=False, provisional=True, days=7),
     "~ ↓9% in 7 days limit shrinking, provisional", f"dim {P.sev_crit}"),
    (trend_fig(3.0, change=0.5, steady=False, provisional=True, days=1),
     "~ ↑1% in 1 day more room, provisional", f"dim {P.sev_ok}"),
    (PerPct(3.0, True, None, ((1.0, 3.0, True),)), "one day so far", P.muted),
    (None, "no samples yet", P.muted),
])
def test_verdict_names_the_direction_and_how_sure_it_is(fig, words, style):
    assert view.verdict(fig, P) == (words, style)


@pytest.mark.parametrize("peak,step,top", [(7, 2, 8), (140, 50, 150), (3400, 1000, 4000),
                                           (2600, 1000, 3000), (175, 50, 200), (99, 50, 100),
                                           (0.3, 0.1, 0.3), (25, 10, 30), (4, 1, 4)])
def test_the_top_is_a_round_value_at_or_above_the_peak(peak, step, top):
    assert view.nice_top(peak) == (pytest.approx(step), pytest.approx(top))
    assert top >= peak and 1 <= round(top / step) <= view.MAX_GRID_LINES


@pytest.mark.parametrize("usd,text", [(0.5, "$0.5"), (20, "$20"), (500, "$500"), (1000, "$1k"),
                                      (1500, "$1.5k"), (20000, "$20k")])
def test_short_money(usd, text):
    assert view.short_money(usd) == text


def test_grid_rows_top_out_at_a_labelled_line_and_never_share_a_row():
    assert view.grid_rows(3400, 10) == (4000, {2: "$1k", 4: "$2k", 7: "$3k", 9: "$4k"})
    assert view.grid_rows(2600, 9) == (3000, {2: "$1k", 5: "$2k", 8: "$3k"})
    assert view.grid_rows(140, 7) == (150, {2: "$50", 4: "$100", 6: "$150"})
    assert view.grid_rows(100, 2) == (100, {1: "$100"})  # a short chart gets one line
    assert view.grid_rows(0, 5) == (0.0, {})


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


@pytest.mark.parametrize("v", view.VIEWS)
@pytest.mark.parametrize("width,height", [(78, 20), (118, 36), (158, 46), (60, 16), (40, 16),
                                          (78, 8)])
def test_every_view_fits_and_keeps_the_essentials(v, width, height):
    rep = report(n_models=7, n_accounts=4, archive_days=60)
    lines = view.costs_lines(rep, width=width, height=height, names={}, palette=P, view=v)
    text = "\n".join(line.plain for line in lines)
    assert len(lines) <= height
    assert all(line.cell_len <= width for line in lines[1:])  # the summary may ellipsize
    assert lines[0].plain.startswith("today $12.3")
    if v == view.TREND:
        assert "$/1% 5h" not in text.split("\n")[1:2]  # no table header: the legend names them
    else:
        assert "$/1% 5h" in text and "#1" in text  # the accounts always stay


def test_one_chart_at_a_time_titled_with_its_view_and_the_key():
    rep = report()
    for v in view.VIEWS:
        text = "\n".join(line.plain for line in view.costs_lines(
            rep, width=118, height=36, names={}, palette=P, view=v))
        assert f"‹ {v} ›  ←/→" in text
        assert text.count("‹") == 1


def test_the_freshness_note_goes_first_when_the_summary_is_too_wide():
    rep = report()
    wide = view.costs_lines(rep, width=118, height=36, names={}, palette=P,
                            status="updated 2m ago")
    assert wide[0].plain.endswith("updated 2m ago")
    narrow = view.costs_lines(rep, width=60, height=36, names={}, palette=P,
                              status="updated 2m ago")
    assert "updated" not in narrow[0].plain and narrow[0].cell_len <= 60


def test_the_note_and_gaps_go_before_the_chart_shrinks():
    rep = report(n_models=3, n_accounts=2)
    roomy = view.costs_lines(rep, width=118, height=36, names={}, palette=P, view="hourly")
    assert roomy[-1].plain.startswith("~ estimate")
    assert any(line.plain == "" for line in roomy)
    tight = view.costs_lines(rep, width=78, height=17, names={}, palette=P, view="hourly")
    assert not tight[-1].plain.startswith("~ estimate")
    assert "24 hours" in "\n".join(line.plain for line in tight)
    tiny = view.costs_lines(rep, width=78, height=8, names={}, palette=P, view="hourly")
    assert "24 hours" not in "\n".join(line.plain for line in tiny)  # chart gone first


def test_many_models_fold_into_one_other_row_only_when_rows_run_out():
    rep = report(n_models=7)
    table = view.model_table(rep, "monthly", 118)
    lines = view.model_lines(table, 118, 5, P, view.model_colors(rep, P))
    assert len(lines) == 6 and "4 other" in lines[-2].plain
    assert lines[-1].plain.lstrip().startswith("total")
    every = view.model_lines(table, 118, 8, P, view.model_colors(rep, P))
    assert len(every) == 9 and not any("other" in line.plain for line in every)


def test_the_page_lists_every_model_with_spend_when_there_is_room():
    rep = report(n_models=7)
    page = "\n".join(line.plain for line in view.costs_lines(
        rep, width=118, height=60, names={}, palette=P, view="monthly"))
    assert all(m.name in page for m in rep.models) and "other" not in page


# -- the money views ------------------------------------------------------------------


def test_group_sums_runs_aligned_to_the_end():
    series = [(float(i), {"a": 1.0, "b": float(i)}) for i in range(7)]
    assert view.group(series, 3) == [(1.0, {"a": 3.0, "b": 6.0}), (4.0, {"a": 3.0, "b": 15.0})]
    assert view.group(series, 1) == series
    assert view.group(series[:2], 3) == [(0.0, {"a": 2.0, "b": 1.0})]  # one short run stays
    assert view.group_duty(["1", "1", "2", None, "2", "2", "3"], 3) == ["1", "2"]
    assert view.group_duty([None] * 3, 3) == [None]


def test_fill_days_puts_empty_days_in_the_gaps():
    now = time.time()
    d0, d3 = costs.day_start(now, 3), costs.day_start(now)
    days = view.fill_days([(d0, {"a": 1.0}), (d3, {"a": 2.0})])
    assert [d for d, _ in days] == [costs.day_start(now, b) for b in (3, 2, 1, 0)]
    assert [m for _, m in days] == [{"a": 1.0}, {}, {}, {"a": 2.0}]
    assert view.fill_days([]) == []


@pytest.mark.parametrize("v,width,bars,unit", [
    ("monthly", 118, 30, "30 days"),
    ("weekly", 158, 56, "7 days, 3h bars"),
    ("weekly", 78, 28, "7 days, 6h bars"),
    ("hourly", 78, 24, "24 hours"),
    ("hourly", 40, 12, "12 hours"),
    ("total", 158, 30 + 40, "all, daily"),
    ("total", 78, 10, "all, weekly"),
])
def test_each_money_view_buckets_to_fit_the_width(v, width, bars, unit):
    rep = report(archive_days=40)
    buckets, duty, axis = view.money_series(rep, v, width)
    assert len(buckets) == bars and axis[1] == unit
    assert (duty is not None) == (v in ("weekly", "hourly"))
    assert sum(sum(b.values()) for _, b in buckets) <= sum(
        sum(m.values()) for _, m in (rep.history if v in ("total", "monthly") else rep.hours))


def test_the_total_view_sums_every_known_day_in_its_title():
    rep = report(archive_days=10)
    title = view.chart_title(rep, "total", P).plain
    total = sum(sum(m.values()) for _, m in rep.history)
    since = time.strftime("%m-%d", time.localtime(rep.history[0][0]))
    assert f"{view.money(total)} since {since}" in title


def test_chart_bars_follow_the_totals_and_model_colors():
    rep = report(n_models=2)
    lines = view.money_chart(rep, "monthly", 98, 6, P)
    assert len(lines) == 7 and lines[-1].plain.rstrip().endswith("today")
    colors = {span.style for line in lines[:-1] for span in line.spans}
    assert P.accent in colors and P.sev_ok in colors
    flat = with_days(rep, tuple((d, {}) for d, _ in rep.days))
    empty = view.money_chart(flat, "monthly", 98, 4, P)
    assert all(set(line.plain[3:]) <= {" "} for line in empty[:-1])  # no bars, no gridlines


def test_the_top_row_is_a_labelled_gridline_at_or_above_the_tallest_bar():
    rep = report(n_models=1)  # days peak at $15: lines at $5, $10, $15, the top
    lines = view.money_chart(rep, "monthly", 98, 9, P)
    labelled = [line.plain for line in lines[:-1] if line.plain.strip().startswith("$")]
    assert [p.split()[0] for p in labelled] == ["$15", "$10", "$5"]
    assert lines[0].plain.lstrip().startswith("$15")  # the top row
    assert all("╌" in p for p in labelled)
    dashes = [span.style for line in lines for span in line.spans
              if "╌" in line.plain[span.start:span.end]]
    assert dashes and all(style.startswith("dim") for style in dashes)
    # a peak between round values tops out at the next one, never above the last line
    days = tuple((d, {"Opus 4.7": 26.0 if i == 3 else 4.0}) for i, (d, _) in enumerate(rep.days))
    tall = with_days(rep, days)
    top = view.money_chart(tall, "monthly", 98, 8, P)[0].plain
    assert top.lstrip().startswith("$30")


def test_the_week_and_the_day_show_who_was_on_duty():
    rep = report(n_models=2)
    for v, width in (("hourly", 78), ("weekly", 78)):
        lines = view.money_chart(rep, v, width, 5, P)
        assert lines[-1].plain.rstrip().endswith("now")
        duty = lines[-2].plain
        assert duty.lstrip().startswith("on #1") and duty.count("#2") == 1
    assert "on #" not in "".join(x.plain for x in view.money_chart(rep, "monthly", 78, 5, P))


def test_duty_marks_never_run_into_each_other_or_off_the_end():
    row = view._duty_row(["1", "2", "3", "3", "3", "3", "3", "4"], 1, 3, P, {}).plain
    assert row == "on #1 #3   "  # #2 had no room; #3 waits for some; #4 would run off the end


@pytest.mark.parametrize("v", view.VIEWS)
@pytest.mark.parametrize("width", [118, 78, 58, 40])
def test_chart_rows_counts_what_chart_lines_draws(v, width):
    rep = report(n_models=2, archive_days=20)
    lines = view.chart_lines(rep, v, width, 4, {}, P)
    assert len(lines) == view.chart_rows(rep, v, width, 4)
    assert all(line.cell_len <= width for line in lines)


# -- ↑/↓ and tab: whose spend and which model ------------------------------------------


def test_account_picks_go_all_by_account_then_each_account_with_spend():
    rep = report(n_models=3, n_accounts=3)
    rep = CostReport(**{**rep.__dict__, "accounts": rep.accounts + (
        AccountLine("x:old@x|o", 0.0, None, None),)})  # no spend in the range: no pick
    assert view.account_picks(rep, "monthly", 98) == [view.ALL, view.BY_ACCOUNT, "1", "2"]


def test_model_picks_take_every_model_with_spend_costliest_first():
    rep = report(n_models=3)
    days = list(rep.days)
    days[-1] = (days[-1][0], {**days[-1][1], "Opus 4.8": 0.01})  # $0.01 of hundreds still counts
    rep = with_days(rep, days)
    assert view.model_picks(rep, "monthly", 98) == [view.ALL_MODELS, "Opus 4.7", "Sonnet 4.5",
                                                    "Haiku 4.5", "Opus 4.8"]
    assert view.model_picks(rep, view.TREND, 98) == []  # the trend has no model to pick


def test_trend_picks_go_all_accounts_then_each_account_with_points():
    rep = trend_report(**{"1": trend_fig(5.0), "2": PerPct(1.8, False), "3": trend_fig(2.0)})
    assert view.account_picks(rep, view.TREND, 78) == [view.ALL_ACCOUNTS, "1", "3"]  # #2: no points
    only_7d = CostReport(**{**rep.__dict__, "accounts": (
        *rep.accounts, AccountLine("4", 1.0, None, trend_fig(9.0)))})
    assert view.account_picks(only_7d, view.TREND, 78)[-1] == "4"  # points in either window


def test_a_pick_the_view_lacks_falls_back_to_all():
    rep = report()
    assert view.resolve_model(rep, "monthly", 98, "Haiku 4.5") == "Haiku 4.5"
    assert view.resolve_model(rep, "monthly", 98, "Opus 9") == view.ALL_MODELS
    assert view.resolve_model(rep, "monthly", 98, None) == view.ALL_MODELS
    assert view.resolve_pick(rep, "monthly", 98, "2") == "2"
    assert view.resolve_pick(rep, "monthly", 98, "7") == view.ALL
    assert view.resolve_pick(rep, view.TREND, 98, view.BY_ACCOUNT) == view.ALL_ACCOUNTS


@pytest.mark.parametrize("v", ["total", "monthly", "weekly", "hourly"])
def test_one_model_alone_keeps_only_its_spend(v):
    rep = report(n_models=3, archive_days=20)
    every, _, _ = view.money_series(rep, v, 98)
    alone, _, axis = view.money_series(rep, v, 98, view.ALL, "Sonnet 4.5")
    assert len(alone) == len(every) and axis == view.money_series(rep, v, 98)[2]
    assert all(set(m) <= {"Sonnet 4.5"} for _, m in alone)
    assert [m.get("Sonnet 4.5", 0) for _, m in alone] == pytest.approx(
        [m.get("Sonnet 4.5", 0) for _, m in every])


@pytest.mark.parametrize("v", ["total", "monthly", "weekly", "hourly"])
def test_every_filter_sums_alike_by_account_and_by_model(v):
    """For each model pick, Σ by account = Σ over the accounts alone = Σ by
    model = the filtered total, bucket by bucket."""
    rep = report(n_models=3, archive_days=5)
    for model in view.model_picks(rep, v, 98):
        by_model, duty, _ = view.money_series(rep, v, 98, view.ALL, model)
        by_account, duty2, _ = view.money_series(rep, v, 98, view.BY_ACCOUNT, model)
        alone = [view.money_series(rep, v, 98, k, model)[0] for k in ("1", "2")]
        assert duty == duty2
        for i, ((t, m), (t2, a)) in enumerate(zip(by_model, by_account)):
            assert t == t2 and sum(m.values()) == pytest.approx(sum(a.values()))
            assert sum(sum(one[i][1].values()) for one in alone) == pytest.approx(
                sum(v for k, v in a.items() if k))  # what nobody was credited with is in neither
            assert set(a) <= {"1", "2", ""}


def test_one_account_alone_stacks_by_model_and_by_account_one_model_stacks_by_account():
    rep = report(n_models=3)
    two, _, _ = view.money_series(rep, "monthly", 98, "2")
    assert all(set(m) <= {"Opus 4.7", "Sonnet 4.5", "Haiku 4.5"} for _, m in two)
    total = sum(sum(m.values()) for _, m in view.money_series(rep, "monthly", 98)[0])
    assert sum(sum(m.values()) for _, m in two) == pytest.approx(total / 3)  # #2's third
    split, _, _ = view.money_series(rep, "monthly", 98, view.BY_ACCOUNT, "Haiku 4.5")
    assert all(set(m) <= {"1", "2"} for _, m in split)
    both, _, _ = view.money_series(rep, "monthly", 98, "1", "Haiku 4.5")
    assert all(set(m) <= {"Haiku 4.5"} for _, m in both)


def test_by_account_total_marks_days_before_the_priced_range_unattributed():
    rep = report(n_models=2, archive_days=10)
    split, _, _ = view.money_series(rep, "total", 200, view.BY_ACCOUNT)
    older = [m for t, m in split if t < rep.days[0][0]]
    assert len(older) == 10 and all(set(m) == {""} for m in older)
    assert sum(sum(m.values()) for _, m in split) == pytest.approx(
        sum(sum(m.values()) for _, m in rep.history))


def test_one_model_alone_gets_its_own_scale_and_color():
    rep = report(n_models=3)
    def top(lines):
        return lines[0].plain.split()[0]
    every = view.money_chart(rep, "monthly", 98, 8, P)
    haiku = view.money_chart(rep, "monthly", 98, 8, P, view.ALL, "Haiku 4.5")
    assert top(every) == "$30" and top(haiku) == "$6"  # the gridlines follow the one series
    bars = {span.style for line in haiku[:-1] for span in line.spans
            if set(line.plain[span.start:span.end]) & set("▁▂▃▄▅▆▇█")}
    assert bars == {P.sev_warn}  # Haiku's color in the model table
    one = view.money_chart(rep, "monthly", 98, 8, P, "2", "Haiku 4.5")
    assert top(one) == "$2"  # a third of it: rescaled again


def test_by_account_bars_take_the_account_colors():
    rep = report(n_models=3)
    kept = {"1": P.account(3), "2": P.account(0), "": P.muted}  # kept by identity, not order
    lines = view.money_chart(rep, "monthly", 98, 8, P, view.BY_ACCOUNT, colors=kept)
    bars = {span.style for line in lines[:-1] for span in line.spans
            if set(line.plain[span.start:span.end]) & set("▁▂▃▄▅▆▇█")}
    assert bars == {kept["1"], kept["2"]}
    duty = view.money_chart(rep, "weekly", 98, 5, P, colors=kept)[-2]
    assert {sp.style for sp in duty.spans if "#" in duty.plain[sp.start:sp.end]} == {
        kept["1"], kept["2"]}  # "on #1 … #2" in the same hues


def test_the_title_names_both_picks_with_the_filtered_spend_and_share():
    rep = report(n_models=3)
    def title(v, pick, model=view.ALL_MODELS):
        return view.chart_title(rep, v, P, width=98, pick=pick, model=model,
                                names={"1": "me@x.com"}).plain
    assert title("weekly", view.ALL) == "‹ weekly ›  ←/→ · ↕ all · ⇥ all models"
    assert title("weekly", view.BY_ACCOUNT).startswith(
        "‹ weekly ›  ←/→ · ↕ by account · ⇥ all models · ")
    assert title("weekly", view.BY_ACCOUNT).endswith("■ #1 ■ #2")
    cells = view.range_cells(rep, "monthly", 98)
    whole = sum(cells.values())
    part = sum(usd for (who, name), usd in cells.items() if who == "2" and name == "Opus 4.7")
    assert title("monthly", "2", "Opus 4.7") == (
        f"‹ monthly ›  ←/→ · ↕ #2 · ⇥ Opus 4.7 · {view.money(part)} ({part / whole * 100:.0f}%)")
    assert "since" not in title("total", view.ALL, "Opus 4.7")  # the filtered total stands in
    assert title(view.TREND, "1").startswith("‹ $/1% trend ›  ←/→ · ↕ #1 me@x.com")
    assert "⇥" not in title(view.TREND, view.ALL_ACCOUNTS)  # no model to pick on the trend


def test_accounts_are_tagged_by_number_removed_ones_by_email_and_nobody_by_a_question_mark():
    assert view.account_tag("2") == "#2" and view.account_tag("") == "?"
    assert view.account_tag("x:oldname@x.com|o") == "×oldnam"
    names = {"2": "me@x.com"}
    assert view.account_label("2", names) == "#2 me@x.com"
    assert view.account_label("x:old@x.com|o", names) == "× old@x.com (removed)"
    assert view.account_label("", names) == "? unattributed"


def test_one_account_alone_on_a_trend_drops_the_others():
    rep = trend_report(**{"1": trend_fig(5.0, change=-0.3, steady=False), "2": trend_fig(20.0)})
    both = view.chart_lines(rep, view.TREND, 78, 8, {}, P)
    one = view.chart_lines(rep, view.TREND, 78, 8, {}, P, "2")
    assert len(one) == len(both) - 1 == view.chart_rows(rep, view.TREND, 78, 8, "2")
    assert one[-2].plain.startswith("● #2")
    assert "no 7d climb" in one[-1].plain  # the 7d panel: nothing priced yet
    labels = [line.plain.split()[0] for line in one[1:-2] if line.plain.strip().startswith("$")]
    assert "$5" not in labels  # #1 at $5 no longer stretches the scale


@pytest.mark.parametrize("width,height", [(40, 16), (60, 16), (78, 20), (158, 46)])
def test_every_filter_of_every_view_fits(width, height):
    rep = report(n_models=7, n_accounts=4, archive_days=60)
    for v in view.VIEWS:
        for pick in view.account_picks(rep, v, width):
            for model in view.model_picks(rep, v, width) or [None]:
                lines = view.costs_lines(rep, width=width, height=height, names={}, palette=P,
                                         view=v, pick=pick, model=model)
                assert len(lines) <= height
                assert all(line.cell_len <= width for line in lines[1:]), (v, pick, model)


# -- tokens ------------------------------------------------------------------------------


def test_the_views_cycle_through_the_money_charts_then_the_one_trend():
    assert view.VIEWS == ("total", "monthly", "weekly", "hourly", view.TREND)


@pytest.mark.parametrize("n,text", [(500, "500"), (2_000, "2k"), (1_500_000, "1.5M"),
                                    (3e9, "3B")])
def test_short_tokens(n, text):
    assert view.short_tokens(n) == text


def test_the_model_table_has_every_token_count_and_a_total_matching_the_range():
    rep = report(n_models=3)
    table = view.model_table(rep, "monthly", 118)
    assert [m.name for m in table] == ["Opus 4.7", "Sonnet 4.5", "Haiku 4.5"]
    usd = sum(view.range_cells(rep, "monthly", 118).values())
    assert sum(m.cost for m in table) == pytest.approx(usd)
    for m in table:  # the fixture counts TOKENS_PER_USD tokens of each kind per dollar
        assert (m.input, m.cache_write, m.cache_read, m.output) == tuple(
            round(m.cost * k) for k in TOKENS_PER_USD)
    lines = view.model_lines(table, 118, 10, P, view.model_colors(rep, P))
    assert lines[0].plain.split() == ["model", "cost", "share", "input", "cache", "w", "cache",
                                      "r", "output", "7d"]
    total = lines[-1].plain.split()
    assert total[0] == "total" and total[1] == view.money(usd) and total[2] == "100%"
    assert total[-1] == view.tokens(sum(m.output for m in table))


def test_the_model_table_follows_the_account_and_model_picks():
    rep = report(n_models=3)
    whole = view.model_table(rep, "weekly", 118)
    mine = view.model_table(rep, "weekly", 118, "2")
    assert [m.cost for m in mine] == pytest.approx([m.cost / 3 for m in whole])  # #2: a third
    assert all(m.trend is None for m in mine)  # the week's trend is for everyone's spend
    assert view.model_table(rep, "weekly", 118, view.BY_ACCOUNT) == whole
    one = view.model_table(rep, "weekly", 118, "1", "Sonnet 4.5")
    assert [m.name for m in one] == ["Sonnet 4.5"] and one[0].share == 1.0
    assert one[0].output == round(whole[1].output * 2 / 3)
    lines = view.model_lines(one, 118, 5, P, view.model_colors(rep, P), trend=False)
    assert len(lines) == 2 and "7d" not in lines[0].plain  # one model: no total row


def test_a_model_with_tokens_but_no_dollars_still_gets_a_row():
    rep = report(n_models=1)
    cells = dict(rep.history_tokens[-1][1])
    cells[("1", "Opus 4.8")] = (5.0, 0.0, 0.0, 7.0)
    rep = CostReport(**{**rep.__dict__, "history_tokens": rep.history_tokens[:-1] + (
        (rep.history_tokens[-1][0], cells),)})
    names = [m.name for m in view.model_table(rep, "monthly", 118)]
    assert names == ["Opus 4.7", "Opus 4.8"]


@pytest.mark.parametrize("width,kept", [
    (70, ["cost", "share", "input", "cache w", "cache r", "output", "7d"]),
    (69, ["cost", "share", "input", "cache w", "cache r", "output"]),
    (61, ["cost", "share", "cache w", "cache r", "output"]),
    (53, ["cost", "cache w", "cache r", "output"]),
    (46, ["cost", "cache r", "output"]),
    (37, ["cost", "output"]),
    (20, ["cost"]),
])
def test_narrow_tables_drop_columns_in_order_keeping_cost_output_and_cache_reads(width, kept):
    assert [h for h, _ in view.model_columns(10, width, True)] == kept


@pytest.mark.parametrize("width", [40, 60, 78, 118])
def test_the_model_table_fits_every_width(width):
    rep = report(n_models=7)
    lines = view.model_lines(view.model_table(rep, "monthly", width), width, 9, P,
                             view.model_colors(rep, P))
    assert all(line.cell_len <= width for line in lines)
    assert "total" in lines[-1].plain


def test_tokens_stack_by_kind_with_token_gridlines_and_the_filters_apply():
    rep = report(n_models=3)
    buckets, _, _ = view.money_series(rep, "hourly", 118, unit=view.TOKENS)
    dollars, _, _ = view.money_series(rep, "hourly", 118)
    for (_, kinds), (_, usd) in zip(buckets, dollars):
        assert set(kinds) <= set(view.TOKEN_LABELS)
        assert kinds.get("cache r", 0.0) == pytest.approx(sum(usd.values()) * TOKENS_PER_USD[2])
    mine, _, _ = view.money_series(rep, "hourly", 118, "2", "Opus 4.7", view.TOKENS)
    for (_, kinds), (_, usd) in zip(mine, dollars):
        assert kinds.get("output", 0.0) == pytest.approx(usd.get("Opus 4.7", 0.0) / 3 * 1000)
    by_account, _, _ = view.money_series(rep, "hourly", 118, view.BY_ACCOUNT, unit=view.TOKENS)
    assert set().union(*(set(b) for _, b in by_account)) <= {"1", "2"}
    lines = view.money_chart(rep, "hourly", 78, 8, P, unit=view.TOKENS)
    labels = [line.plain.split()[0] for line in lines[:8] if line.plain.strip()[:1].isdigit()]
    assert labels and all(label[-1] in "kMB" for label in labels)
    assert not any("$" in line.plain for line in lines)


def test_the_title_says_tokens_with_the_kinds_legend_and_the_token_share():
    rep = report()

    def title(*args, **kw) -> str:
        return view.chart_title(rep, *args, palette=P, width=200, names={}, **kw).plain

    assert title("weekly", pick=view.ALL, unit=view.TOKENS) == (
        "‹ weekly ›  ←/→ · tokens · ↕ all · ⇥ all models ■ in ■ write ■ read ■ out")
    assert "tokens (33%)" in title("weekly", pick="2", unit=view.TOKENS)
    assert "tokens" not in title(view.TREND, pick=view.ALL_ACCOUNTS, unit=view.TOKENS)
    assert "tokens since" not in title("total", pick=view.ALL)
    assert title("total", pick=view.ALL, unit=view.TOKENS).endswith(" since " + time.strftime(
        "%m-%d", time.localtime(rep.history[0][0])))


def test_the_hint_names_only_the_keys_that_act_and_what_t_switches_to():
    keys = dict(view.hint_keys("monthly"))
    assert keys["t"] == "tokens" and keys["tab"] == "model"
    assert dict(view.hint_keys("monthly", view.TOKENS))["t"] == "dollars"
    trend = dict(view.hint_keys(view.TREND))
    assert "t" not in trend and "tab" not in trend and trend["↑/↓"] == "account"


def test_the_trend_shows_the_5h_panel_over_the_7d_one():
    rep = trend_report(**{"1": trend_fig(5.0)})
    rep = CostReport(**{**rep.__dict__, "accounts": (AccountLine("1", 10.0, trend_fig(5.0),
                                                                 trend_fig(21.0)),)})
    lines = [line.plain for line in view.chart_lines(rep, view.TREND, 78, 4, {}, P)]
    axes = [i for i, line in enumerate(lines) if "$ per 1% of" in line]
    assert len(axes) == 2 and "of 5h" in lines[axes[0]] and "of 7d" in lines[axes[1]]
    assert len(lines) == view.chart_rows(rep, view.TREND, 78, 4) == 1 + 2 * (4 + 1 + 1)


# -- the $ per 1% views ------------------------------------------------------------------


def trend_report(**figs) -> CostReport:
    base = report()
    accounts = tuple(AccountLine(n, 10.0, f, None) for n, f in figs.items())
    return CostReport(**{**base.__dict__, "accounts": accounts})


def test_trend_view_draws_each_account_with_its_own_line_and_verdict():
    rep = trend_report(**{"1": trend_fig(5.0, change=-0.3, steady=False),
                          "2": trend_fig(2.0)})
    lines = view.trend_chart(rep, "5h", 78, 10, {"1": "me@x.com"}, P)
    legend = [line.plain for line in lines[-2:]]
    assert legend[0].startswith("● #1 me@x.com") and "↓30%/4w limit shrinking" in legend[0]
    assert legend[1].startswith("● #2") and legend[1].endswith("steady")
    plot = lines[:10]
    colors = view.account_colors(rep, P)
    styles = {span.style for line in plot for span in line.spans}
    assert colors["1"] in styles and colors["2"] in styles  # never pooled: one hue each
    assert "$ per 1% of 5h" in lines[10].plain and lines[10].plain.rstrip().endswith("now")


def test_a_steady_account_gets_a_flat_line_and_a_moving_one_a_slope():
    rep = trend_report(**{"1": trend_fig(5.0)})
    plot = [line.plain for line in view.trend_chart(rep, "5h", 78, 10, {}, P)[:10]]
    flat_rows = [i for i, row in enumerate(plot) if view._FLAT in row]
    assert len(flat_rows) == 1 and view._SLOPE not in "".join(plot)
    moving = trend_report(**{"1": trend_fig(5.0, change=-0.5, steady=False, days=30)})
    plot = [line.plain for line in view.trend_chart(moving, "5h", 78, 10, {}, P)[:10]]
    slope_rows = [i for i, row in enumerate(plot) if view._SLOPE in row]
    assert len(slope_rows) > 2 and view._FLAT not in "".join(plot)
    first = [row.index(view._SLOPE) for row in plot if view._SLOPE in row]
    assert first == sorted(first)  # falling: the line starts high on the left


def test_unsure_points_are_hollow_and_dim_and_a_provisional_line_is_dim():
    rep = trend_report(**{"1": trend_fig(5.0, sure=False, provisional=True, days=8)})
    lines = view.trend_chart(rep, "5h", 78, 10, {}, P)
    plot = lines[:10]
    text = "".join(line.plain for line in plot)
    assert "○" in text and "●" in text
    hollow = [s.style for line in plot for s in line.spans if line.plain[s.start:s.end] == "○"]
    assert hollow and all(style.startswith("dim") for style in hollow)
    flat = [s.style for line in plot for s in line.spans if view._FLAT in line.plain[s.start:s.end]]
    assert flat and all(style.startswith("dim") for style in flat)
    assert "provisional: 8 days of data" in lines[-1].plain


def test_few_points_still_show_with_a_provisional_line():
    rep = trend_report(**{"1": trend_fig(5.0, provisional=True, days=2, n=2)})
    lines = view.trend_chart(rep, "5h", 60, 6, {}, P)
    assert "●" in "".join(line.plain for line in lines[:6])
    assert "~ steady, provisional: 2 days of data" in lines[-1].plain


def test_many_samples_squeeze_to_one_median_point_per_column_over_their_spread():
    now = time.time()
    fig = trend_fig(5.0, days=10, n=2)
    # 40 samples a day over the last day: far more than the columns
    dense = tuple((now - 86400 + i * 2160, v, True)
                  for i, v in enumerate([3.0, 5.0, 7.0, 5.0] * 10))
    rep = trend_report(**{"1": PerPct(5.0, True, fig.fit, fig.points + dense)})
    lines = view.trend_chart(rep, "5h", 78, 12, {}, P)
    plot = [line.plain for line in lines[:12]]
    dots = sum(row.count("●") for row in plot)
    assert dots <= 78  # at most one point per column
    last_cols = {i for row in plot for i, ch in enumerate(row) if ch in "●│" and i > 60}
    assert last_cols  # the dense day is drawn at the right
    assert any(view._SPREAD in row for row in plot)  # quartiles of a busy column
    spread = [s.style for line in lines[:12] for s in line.spans
              if view._SPREAD in line.plain[s.start:s.end]]
    assert spread and all(style.startswith("dim") for style in spread)


def test_a_column_mostly_used_elsewhere_is_hollow():
    now = time.time()
    fig = trend_fig(5.0, days=10, n=2)
    pts = fig.points + tuple((now - 60 - i, 5.0, i == 0) for i in range(5))  # 2 of 6 sure
    rep = trend_report(**{"1": PerPct(5.0, True, fig.fit, pts)})
    plot = "".join(line.plain for line in view.trend_chart(rep, "5h", 78, 10, {}, P)[:10])
    assert "○" in plot


def test_columns_take_the_median_and_quartiles():
    cols = view._columns([(0, 1.0, True), (0, 2.0, True), (0, 9.0, False), (5, 4.0, False)],
                         lambda t: int(t))
    assert cols[0] == (pytest.approx(1.5), 2.0, pytest.approx(5.5), True)
    assert cols[5] == (4.0, 4.0, 4.0, False)


def test_a_trend_view_with_nothing_to_plot_says_so_in_one_line():
    rep = trend_report(**{"1": None})
    lines = view.trend_chart(rep, "7d", 78, 10, {}, P)
    assert len(lines) == 1 and "no 7d climb" in lines[0].plain
    assert view.chart_rows(rep, view.TREND, 78, 10) == 3  # the title and a line per window


@pytest.mark.parametrize("width", [40, 60, 78, 158])
def test_trend_view_fits_narrow_widths(width):
    rep = trend_report(**{"1": trend_fig(5.0, change=-0.3, steady=False),
                          "2": trend_fig(20.0, sure=False, provisional=True, days=3)})
    lines = view.chart_lines(rep, view.TREND, width, 6, {"1": "someone@example.com"}, P)
    assert all(line.cell_len <= width for line in lines)


def test_account_table_shows_the_figures_and_leaves_the_trend_to_its_view():
    lines = view.account_lines(report(), 118, 5, {"1": "me@x.com"}, P)
    assert lines[1].plain.startswith("#1 me@x.com") and "~$3.10" in lines[1].plain
    assert "~$9.40" in lines[1].plain and "4w" not in lines[1].plain
    assert lines[2].plain.startswith("#2") and "~$1.80?" in lines[2].plain and "–" in lines[2].plain
    styles = {lines[2].plain[s.start:s.end].strip(): s.style for s in lines[2].spans}
    assert styles["~$1.80?"] == P.sev_warn
    assert all(line.cell_len <= 50 for line in view.account_lines(report(), 50, 5, {}, P))
    kept = {"1": P.account(2), "2": P.account(4)}
    colored = view.account_lines(report(), 118, 5, {"1": "me@x.com"}, P, kept)
    assert any(kept["1"] in st for st in [str(sp.style) for sp in colored[1].spans
                                           if colored[1].plain[sp.start:sp.end] == "#1"])


def test_the_account_table_ends_with_what_nobody_was_credited_with():
    rep = CostReport(**{**report().__dict__, "unattributed": 345.0})
    lines = view.account_lines(rep, 118, 5, {"1": "me@x.com"}, P)
    assert lines[-1].plain.startswith("? unattributed") and "$345" in lines[-1].plain
    assert "~" not in lines[-1].plain  # no $ per 1% for nobody
    assert view.account_lines(report(), 118, 5, {}, P)[-1].plain.startswith("#2")


def test_plain_page_has_every_part():
    text = view.plain_page(report(), {"1": "me@x.com"})
    for part in ("today $12.3", "30 days", "model", "me@x.com", "$/1% trend", "~ estimate",
                 "$ per 1% of 5h", "$ per 1% of 7d", "limit shrinking"):
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
            assert "‹ total ›" in text and "Opus 4.7" in text
            costs_view = app.screen.query_one("#costs")
            assert costs_view.render().no_wrap  # a line too long is cut, never wrapped
            screen = ["".join(seg.text for seg in strip)
                      for strip in app.screen._compositor.render_strips()]
            assert screen[2].strip().startswith("today") and screen[3].strip() == ""
            assert costs_view.region.bottom <= app.screen.query_one("#prompt").region.y
            assert all(len(line) <= size[0] - 2 for line in lines[1:])
            assert app.screen.query_one("#status").region.y == 0  # the header stays
            hint = app.screen.prompt.render().plain
            assert "←/→ chart" in hint and "r refresh" in hint and "esc back" in hint
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

    async def test_arrows_cycle_the_chart_wrap_around_and_the_view_is_kept(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            seen = [view.VIEWS[0]]
            for _ in view.VIEWS:
                await pilot.press("right")
                await pilot.pause()
                seen.append(app.screen.query_one("#costs").view)
            assert seen == [*view.VIEWS, view.VIEWS[0]]  # every view once, then around
            await pilot.press("left")
            await pilot.pause()
            assert app.screen.query_one("#costs").view == view.VIEWS[-1]
            assert f"‹ {view.VIEWS[-1]} ›" in page(app)
            await pilot.press("escape")
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            assert f"‹ {view.VIEWS[-1]} ›" in page(app)  # reopened on the same chart
            assert app.screen.costs_shown  # arrows never left the page

    async def test_up_and_down_cycle_whose_spend_and_keep_it_across_views(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            costs_view = app.screen.query_one("#costs")
            options = view.account_picks(report(), "total", costs_view.size.width)
            assert options == [view.ALL, view.BY_ACCOUNT, "1", "2"]
            seen = [costs_view.pick]
            for _ in options:
                await pilot.press("down")
                await pilot.pause()
                seen.append(costs_view.pick)
            assert seen == [*options, options[0]]  # every pick once, then around
            await pilot.press("up")
            await pilot.pause()
            assert costs_view.pick == "2" and "↕ #2 · ⇥ all models" in page(app)
            await pilot.press("right")  # monthly: #2 is still there
            await pilot.pause()
            assert "↕ #2" in page(app)
            for _ in range(3):  # on to the trend: its own pick, all accounts
                await pilot.press("right")
                await pilot.pause()
            assert costs_view.view == view.TREND and "↕ all accounts" in page(app)
            await pilot.press("down")
            await pilot.pause()
            assert costs_view.trend_pick == "1" and costs_view.money_pick == "2"
            await pilot.press("left", "left", "left")
            await pilot.pause()
            assert "↕ #2" in page(app)  # the money pick came back
            assert app.screen.costs_shown and not app.screen.keys_shown

    async def test_tab_cycles_the_model_combined_with_the_account_and_stays_on_the_page(
            self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            assert app.screen.query_one("#costs").model_pick == view.ALL_MODELS
            await pilot.press("tab")  # on the main page tab is not the costs' to take
            await pilot.pause()
            assert app.screen.query_one("#costs").model_pick == view.ALL_MODELS
            await pilot.press("dollar_sign")
            await settle(pilot)
            costs_view = app.screen.query_one("#costs")
            models = view.model_picks(report(), "total", costs_view.size.width)
            seen = [costs_view.model_pick]
            for _ in models:
                await pilot.press("tab")
                await pilot.pause()
                seen.append(costs_view.model_pick)
                assert app.focused is costs_view  # tab never moves focus off the page
            assert seen == [*models, models[0]]
            await pilot.press("shift+tab")
            await pilot.pause()
            assert costs_view.model_pick == models[-1]
            await pilot.press("down", "down", "down")  # #2 only
            await pilot.pause()
            assert f"↕ #2 · ⇥ {models[-1]} · $" in page(app)
            for _ in range(4):  # a trend: tab does nothing there
                await pilot.press("right")
            await pilot.press("tab")
            await pilot.pause()
            assert costs_view.model_pick == models[-1] and "⇥" not in page(app).splitlines()[2]

    async def test_a_pick_missing_from_the_range_shows_all_and_the_cycle_starts_there(self, tmp_path):
        base = report(n_models=3)  # the last week only ever used Opus, on #1
        rep = CostReport(**{**base.__dict__, "hour_cells": tuple(
            (t, {("1", "Opus 4.7"): 1.0}) for t, _ in base.hour_cells)})
        app = costs_app(tmp_path, FakeCosts(CostResult(rep, ran=True)))
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            costs_view = app.screen.query_one("#costs")
            costs_view.model_pick = "Haiku 4.5"  # chosen on the month
            costs_view.money_pick = "2"
            costs_view.view = "hourly"  # the day never used either
            costs_view.refresh()
            await pilot.pause()
            assert "↕ all · ⇥ all models" in page(app)
            await pilot.press("down")
            await pilot.pause()
            assert costs_view.money_pick == view.BY_ACCOUNT
            await pilot.press("tab")
            await pilot.pause()
            assert costs_view.model_pick == "Opus 4.7"

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

    async def test_t_switches_dollars_and_tokens_on_the_page_only(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("dollar_sign")
            await settle(pilot)
            costs_view = app.screen.query_one("#costs")
            assert "t tokens" in prompt_plain(app)
            await pilot.press("t")
            await pilot.pause()
            assert costs_view.unit == view.TOKENS and "· tokens ·" in page(app)
            assert "t dollars" in prompt_plain(app)  # not the token prompt
            for _ in range(4):  # on to the trend: no tokens there, nor their keys
                await pilot.press("right")
                await pilot.pause()
            assert costs_view.view == view.TREND
            assert "tab" not in prompt_plain(app) and "t dollars" not in prompt_plain(app)
            await pilot.press("t")
            await pilot.pause()
            assert costs_view.unit == view.TOKENS  # kept for the money views
            await pilot.press("right")
            await pilot.pause()
            assert "· tokens ·" in page(app) and "t dollars" in prompt_plain(app)
            await pilot.press("escape")
            await settle(pilot)
            await pilot.press("t")  # the main page's t is still the token paste
            await pilot.pause()
            assert prompt_plain(app).startswith("token: ▌")

    async def test_the_key_list_names_the_costs(self, tmp_path):
        app = costs_app(tmp_path, FakeCosts())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert "$      costs ←→↑↓⇥t (esc)" in app.screen.query_one("#detail").render().plain
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
