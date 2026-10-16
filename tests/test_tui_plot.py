"""Unit tests for the pure plot renderers in ``claude_swap.tui.plot``."""

from __future__ import annotations

import os
import time

import pytest

from claude_swap.estimator import WindowEstimate, estimate_account
from claude_swap.tui.plot import (
    GUTTER,
    RESET_GLYPH,
    SPARK_GLYPHS,
    THRESHOLD_GLYPH,
    line_plot,
    sparkline,
)
from claude_swap.tui.theme import CSWAP_LIGHT, SEV_CRIT, Palette
from claude_swap.usage_history import Point

NOW = 1_700_000_000.0
BRAILLE = range(0x2800, 0x2900)


@pytest.fixture
def utc(monkeypatch):
    """Pin local time to UTC so axis clocks are deterministic."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def burn_5h(n: int = 13, step: float = 600.0, start: float = 20.0, per_step: float = 3.5):
    reset = NOW + 3 * 3600
    pts = [
        Point(NOW - (n - 1) * step + i * step, start + i * per_step, reset)
        for i in range(n)
    ]
    est = estimate_account({"5h": pts}, models=(), threshold=90, now=NOW).windows["5h"]
    return pts, est


def flat_est(pct: float = 40.0, *, resets_at: float | None = None, rate: float | None = 0.0):
    pts = tuple(Point(NOW - 3600 * 6 + i * 3600, pct, resets_at) for i in range(7))
    return WindowEstimate("7d", pct, NOW, resets_at, rate, "medium", pts)


def dots(cell: str) -> int:
    return ord(cell) - 0x2800 if ord(cell) in BRAILLE else 0


def top_dot_per_column(lines) -> dict[int, int]:
    """Highest lit dot row (0 = top of the plot) per braille dot column."""
    bits = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
    tops: dict[int, int] = {}
    for r, line in enumerate(lines[:-1]):
        body = line.plain[GUTTER:]
        for c, ch in enumerate(body):
            b = dots(ch)
            for sub_r in range(4):
                for sub_c in range(2):
                    if b & bits[sub_r][sub_c]:
                        x = c * 2 + sub_c
                        y = r * 4 + sub_r
                        tops[x] = min(tops.get(x, y), y)
    return tops


class TestSparkline:
    def test_empty_series_is_blank_cells(self):
        assert sparkline([], 10).plain == " " * 10

    def test_zero_width(self):
        assert sparkline([Point(NOW, 50.0, None)], 0).plain == ""

    def test_single_point_lands_in_last_cell(self):
        text = sparkline([Point(NOW, 100.0, None)], 5).plain
        assert text == "    " + SPARK_GLYPHS[-1]

    def test_extremes_map_to_lowest_and_highest_glyph(self):
        pts = [Point(NOW, 0.0, None), Point(NOW + 60, 100.0, None)]
        text = sparkline(pts, 2).plain
        assert text == SPARK_GLYPHS[0] + SPARK_GLYPHS[-1]

    def test_gap_in_time_is_a_space(self):
        pts = [Point(NOW, 10.0, None), Point(NOW + 900, 20.0, None)]
        text = sparkline(pts, 10).plain
        assert text[0] != " " and text[-1] != " "
        assert text[1:-1].strip() == ""

    def test_monotone_input_gives_monotone_output(self):
        pts = [Point(NOW + i * 60, i * 4.0, None) for i in range(26)]
        text = sparkline(pts, 13).plain
        levels = [SPARK_GLYPHS.index(c) for c in text if c != " "]
        assert levels == sorted(levels) and len(levels) == 13

    def test_span_clips_points_outside(self):
        pts = [Point(NOW - 999, 90.0, None), Point(NOW, 10.0, None)]
        text = sparkline(pts, 4, span=(NOW - 60, NOW)).plain
        assert SPARK_GLYPHS[-1] not in text

    def test_severity_colors_follow_palette(self):
        text = sparkline([Point(NOW, 95.0, None)], 1)
        assert any(SEV_CRIT in str(span.style) for span in text.spans)
        light = sparkline([Point(NOW, 95.0, None)], 1, palette=Palette.from_theme(CSWAP_LIGHT))
        assert any(CSWAP_LIGHT.error in str(span.style) for span in light.spans)


class TestLinePlot:
    def test_shape_is_height_lines_of_width(self, utc):
        _pts, est = burn_5h()
        lines = line_plot(est, now=NOW, threshold=90, width=40, height=8)
        assert len(lines) == 8
        assert all(line.cell_len == 40 for line in lines)

    def test_golden_5h_burn_with_projection(self, utc):
        _pts, est = burn_5h()
        lines = line_plot(est, now=NOW, threshold=90, width=40, height=8)
        assert [line.plain for line in lines] == [
            "100┤┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄⡀⠄⠄⠂⠁┄┄┄┄┄┄┄┄",
            "   │                  ⡀⠄⠂⠂⠁            ┊",
            "   │            ⢀⡠⠄⠂⠁⠁                 ┊",
            "   │        ⣀⠤⠒⠊⠁                      ┊",
            "   │   ⢀⠤⠔⠊⠉                           ┊",
            "   │                                   ┊",
            "  0┤                                   ┊",
            "    20:13        now               01:13",
        ]

    def test_golden_flat_week_without_reset(self, utc):
        est = flat_est()
        lines = line_plot(est, now=NOW, threshold=80, width=30, height=5, lookback_s=6 * 3600)
        assert [line.plain for line in lines] == [
            "100┤┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
            "   │                          ",
            "   │⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠂⠂⠂⠂",
            "  0┤                          ",
            "    Tue 16:13        Tue 23:13",
        ]

    def test_empty_series_draws_axes_only(self, utc):
        est = WindowEstimate("5h", 0.0, NOW, NOW + 3600, None, "none", ())
        lines = line_plot(est, now=NOW, threshold=90, width=30, height=6)
        assert len(lines) == 6
        assert not any(dots(c) for line in lines for c in line.plain)

    def test_single_point_lights_one_dot(self, utc):
        p = Point(NOW, 50.0, NOW + 3600)
        est = WindowEstimate("5h", 50.0, NOW, NOW + 3600, None, "none", (p,))
        lines = line_plot(est, now=NOW, threshold=None, width=30, height=6)
        lit = [dots(c) for line in lines for c in line.plain if dots(c)]
        assert len(lit) == 1 and bin(lit[0]).count("1") == 1

    def test_projection_stops_at_reset(self, utc):
        # 1%/h never reaches 100 before the reset 3h out: the dotted line
        # must end on the reset column, not run past it.
        _pts, est = burn_5h(per_step=0.2)
        lines = line_plot(est, now=NOW, threshold=None, width=40, height=8)
        cols = {c for line in lines[:-1] for c, ch in enumerate(line.plain[GUTTER:]) if dots(ch)}
        assert max(cols) == 40 - GUTTER - 1

    def test_projection_stops_at_100(self, utc):
        # 18%/h from 86% hits 100% ~47 min out, long before the 3 h reset:
        # nothing is drawn between that crossing and the reset column.
        _pts, est = burn_5h(start=50.0, per_step=3.0)
        lines = line_plot(est, now=NOW, threshold=None, width=40, height=8)
        reset_col = 40 - GUTTER - 1
        cols = {c for line in lines[:-1] for c, ch in enumerate(line.plain[GUTTER:]) if dots(ch)}
        assert max(cols) < reset_col - 5

    def test_no_projection_without_rate(self, utc):
        pts = [Point(NOW - 600, 30.0, NOW + 3600), Point(NOW, 31.0, NOW + 3600)]
        est = WindowEstimate("5h", 31.0, NOW, NOW + 3600, None, "none", tuple(pts))
        lines = line_plot(est, now=NOW, threshold=None, width=40, height=8)
        dim = [s for line in lines for s in line.spans if "dim" in str(s.style)]
        assert dim == []

    def test_threshold_row_is_dashed(self, utc):
        est = flat_est(10.0, resets_at=NOW + 3600)
        lines = line_plot(est, now=NOW, threshold=50, width=30, height=9)
        dashed = [i for i, line in enumerate(lines) if THRESHOLD_GLYPH in line.plain]
        assert len(dashed) == 1
        assert lines[dashed[0]].plain.startswith(" 50┤")

    def test_no_threshold_no_dashes(self, utc):
        est = flat_est(10.0, resets_at=NOW + 3600)
        lines = line_plot(est, now=NOW, threshold=None, width=30, height=9)
        assert not any(THRESHOLD_GLYPH in line.plain for line in lines)

    def test_reset_marker_column(self, utc):
        est = flat_est(10.0, resets_at=NOW + 3600)
        lines = line_plot(est, now=NOW, threshold=None, width=30, height=6)
        assert any(line.plain.endswith(RESET_GLYPH) for line in lines[:-1])

    def test_no_reset_marker_without_reset(self, utc):
        lines = line_plot(flat_est(), now=NOW, threshold=None, width=30, height=6)
        assert not any(RESET_GLYPH in line.plain for line in lines)

    @pytest.mark.parametrize("width,height", [(19, 8), (40, 2), (5, 1)])
    def test_degrades_to_sparkline_when_too_small(self, utc, width, height):
        _pts, est = burn_5h()
        lines = line_plot(est, now=NOW, threshold=90, width=width, height=height)
        assert len(lines) == 1
        assert lines[0].cell_len == width
        assert set(lines[0].plain) <= set(SPARK_GLYPHS + " ")

    def test_monotone_input_draws_monotone_curve(self, utc):
        pts, est = burn_5h()
        lines = line_plot(est, now=NOW, threshold=None, width=60, height=12, points=pts)
        tops = top_dot_per_column(lines)
        xs = sorted(tops)
        ys = [tops[x] for x in xs]
        # screen rows grow downward: a rising curve never moves down
        assert all(a >= b for a, b in zip(ys, ys[1:]))

    def test_points_override_extends_history(self, utc):
        pts, est = burn_5h()
        short = line_plot(est, now=NOW, threshold=None, width=60, height=10)
        longer = line_plot(est, now=NOW, threshold=None, width=60, height=10, points=pts)
        assert min(top_dot_per_column(longer)) <= min(top_dot_per_column(short))

    def test_future_points_are_not_drawn_as_measured(self, utc):
        p_now = Point(NOW, 20.0, NOW + 3600)
        p_future = Point(NOW + 1800, 90.0, NOW + 3600)
        est = WindowEstimate("5h", 20.0, NOW, NOW + 3600, None, "none", (p_now,))
        lines = line_plot(est, now=NOW, threshold=None, width=30, height=6, points=[p_now, p_future])
        lit = [dots(c) for line in lines for c in line.plain if dots(c)]
        assert len(lit) == 1

    def test_does_not_read_the_environment_clock(self, utc, monkeypatch):
        # Purity: a frozen-but-wrong wall clock must not change the drawing.
        _pts, est = burn_5h()
        before = [line.plain for line in line_plot(est, now=NOW, threshold=90, width=40, height=8)]
        monkeypatch.setattr(time, "time", lambda: NOW + 10_000)
        after = [line.plain for line in line_plot(est, now=NOW, threshold=90, width=40, height=8)]
        assert before == after
        assert os.environ["TZ"] == "UTC"
