"""Unit tests for the pure plot renderers in ``claude_rotater.tui.plot``."""

from __future__ import annotations

import os
import time

import pytest

from claude_rotater.estimator import WindowEstimate, estimate_account
from claude_rotater.tui.plot import (
    NOW_GLYPH,
    PROJECTION_GLYPH,
    GUTTER,
    RESET_GLYPH,
    SPARK_GLYPHS,
    THRESHOLD_GLYPH,
    line_plot,
    sparkline,
)
from claude_rotater.tui.theme import ROTA_LIGHT, SEV_CRIT, Palette
from claude_rotater.usage_history import Point

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
        light = sparkline([Point(NOW, 95.0, None)], 1, palette=Palette.from_theme(ROTA_LIGHT))
        assert any(ROTA_LIGHT.error in str(span.style) for span in light.spans)


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
            "100┤┄ switch 90% ┄┄┄┄┄┄┄┄┄┄·····┄┄┄┄┄┄┄┄",
            "   │              │   ·····            ┊",
            "   │            ⢀⡠⠄···                 ┊",
            "   │        ⣀⠤⠒⠊⠁ │                    ┊",
            "   │   ⢀⠤⠔⠊⠉      │                    ┊",
            "   │              │                    ┊",
            "  0┤              │                    ┊",
            "    20:13        now         reset 01:13",
        ]

    def test_golden_flat_week_without_reset(self, utc):
        est = flat_est()
        lines = line_plot(est, now=NOW, threshold=80, width=30, height=5, lookback_s=6 * 3600)
        assert [line.plain for line in lines] == [
            "100┤┄ switch 80% ┄┄┄┄┄┄┄┄┄┄┄┄┄",
            "   │                      │   ",
            "   │⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠂···",
            "  0┤                      │   ",
            "    16:13                23:13",
        ]

    def test_week_axis_starts_on_a_date_and_ends_on_the_reset(self, utc):
        """A 7d window starts and resets on the same weekday and clock, so
        its start is a date ("Nov 08") and its end says it is the reset,
        once: "reset Wed 01:13", never the weekday and clock twice."""
        x1 = NOW + 27 * 3600
        est = WindowEstimate("7d", 30.0, NOW, x1, None, "none", (Point(NOW, 30.0, x1),))
        axis = line_plot(est, now=NOW, threshold=None, width=60, height=6)[-1].plain.split()
        x0 = x1 - 7 * 86400
        assert axis[:2] == time.strftime("%b %d", time.gmtime(x0)).split()
        assert axis[-3:] == ["reset", *time.strftime("%a %H:%M", time.gmtime(x1)).split()]

    def test_clock_label_formats(self, utc):
        from claude_rotater.tui.plot import clock_label

        assert clock_label(NOW + 5 * 3600, NOW) == time.strftime("%H:%M", time.gmtime(NOW + 5 * 3600))
        assert clock_label(NOW + 3 * 86400, NOW) == time.strftime("%a %H:%M", time.gmtime(NOW + 3 * 86400))
        assert clock_label(NOW - 9 * 86400, NOW) == time.strftime("%b %d", time.gmtime(NOW - 9 * 86400))

    def test_now_line_splits_measured_from_projection(self, utc):
        _pts, est = burn_5h()
        lines = line_plot(est, now=NOW, threshold=90, width=40, height=8, projection_color="#123456")
        body = lines[:-1]
        col = next(c for c, ch in enumerate(body[-2].plain) if ch == NOW_GLYPH and c > GUTTER)
        assert lines[-1].plain[col - 1 : col + 2] == "now"
        solid = [c for line in body for sp in line.spans if "#123456" not in str(sp.style)
                 for c in range(sp.start, sp.end) if dots(line.plain[c])]
        dotted = [c for line in body for sp in line.spans if "#123456" in str(sp.style)
                  for c in range(sp.start, sp.end) if line.plain[c] == PROJECTION_GLYPH]
        assert max(solid) <= col <= min(dotted)
        # the projection is its own glyph: no braille right of now
        assert not any(dots(line.plain[c]) for line in body for c in range(col + 2, len(line.plain)))
        # told apart by the dots, never by fading
        assert not any("dim" in str(sp.style) for line in body for sp in line.spans
                       if dots(line.plain[sp.start]))

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
        cols = {c for line in lines[:-1] for c, ch in enumerate(line.plain[GUTTER:])
                if dots(ch) or ch == PROJECTION_GLYPH}
        assert max(cols) == 40 - GUTTER - 1

    def test_projection_stops_at_100(self, utc):
        # 18%/h from 86% hits 100% ~47 min out, long before the 3 h reset:
        # nothing is drawn between that crossing and the reset column.
        _pts, est = burn_5h(start=50.0, per_step=3.0)
        lines = line_plot(est, now=NOW, threshold=None, width=40, height=8)
        reset_col = 40 - GUTTER - 1
        cols = {c for line in lines[:-1] for c, ch in enumerate(line.plain[GUTTER:])
                if dots(ch) or ch == PROJECTION_GLYPH}
        assert max(cols) < reset_col - 5

    def test_no_projection_without_rate(self, utc):
        pts = [Point(NOW - 600, 30.0, NOW + 3600), Point(NOW, 31.0, NOW + 3600)]
        est = WindowEstimate("5h", 31.0, NOW, NOW + 3600, None, "none", tuple(pts))
        lines = line_plot(est, now=NOW, threshold=None, width=40, height=8)
        dim = {line.plain[s.start:s.end] for line in lines for s in line.spans if "dim" in str(s.style)}
        assert dim == {NOW_GLYPH}  # only the now line: no dotted projection

    def test_threshold_row_is_dashed(self, utc):
        est = flat_est(10.0, resets_at=NOW + 3600)
        lines = line_plot(est, now=NOW, threshold=50, width=30, height=9)
        dashed = [i for i, line in enumerate(lines) if THRESHOLD_GLYPH in line.plain]
        assert len(dashed) == 1
        assert lines[dashed[0]].plain.startswith("   ┤┄ switch 50% ┄")  # named in place

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


class TestHistoryPlot:
    def test_shape_and_axis_in_days(self, utc):
        from claude_rotater.tui.plot import history_plot

        pts = [Point(NOW - 86400 * (7 - i), 10.0 * i, None) for i in range(8)]
        lines = history_plot(pts, x0=NOW - 7 * 86400, x1=NOW, threshold=90, width=50, height=8)
        assert len(lines) == 8
        assert all(line.cell_len == 50 for line in lines)
        axis = lines[-1].plain
        assert "Nov 07" in axis and "Nov 14" in axis  # day labels, not clocks
        assert "now" not in axis

    def test_monotone_series_climbs(self, utc):
        from claude_rotater.tui.plot import history_plot

        pts = [Point(NOW - 3600 * (24 - i), 4.0 * i, None) for i in range(25)]
        lines = history_plot(pts, x0=NOW - 86400, x1=NOW, threshold=None, width=40, height=9)
        tops = top_dot_per_column(lines)
        ordered = [tops[x] for x in sorted(tops)]
        assert ordered == sorted(ordered, reverse=True)

    def test_points_outside_span_ignored_and_threshold_drawn(self, utc):
        from claude_rotater.tui.plot import history_plot

        pts = [Point(NOW - 10 * 86400, 99.0, None)]
        lines = history_plot(pts, x0=NOW - 86400, x1=NOW, threshold=50, width=30, height=6)
        body = "".join(line.plain[GUTTER:] for line in lines[:-1])
        assert not any(ord(ch) in BRAILLE for ch in body)
        assert THRESHOLD_GLYPH in body

    def test_degrades_to_sparkline(self, utc):
        from claude_rotater.tui.plot import history_plot

        pts = [Point(NOW - 60, 50.0, None)]
        lines = history_plot(pts, x0=NOW - 3600, x1=NOW, threshold=90, width=10, height=8)
        assert len(lines) == 1 and lines[0].cell_len == 10

    def test_empty_draws_axes_only(self, utc):
        from claude_rotater.tui.plot import history_plot

        lines = history_plot([], x0=NOW - 86400, x1=NOW, threshold=None, width=30, height=5)
        assert len(lines) == 5
        assert lines[0].plain.startswith("100┤")


class TestPeakColumns:
    def _render(self, peaks, **kw):
        from claude_rotater.tui.plot import peak_columns

        args = dict(window_len_s=5 * 3600, x0=NOW - 7 * 86400, x1=NOW,
                    threshold=90, width=60, height=10)
        args.update(kw)
        return peak_columns(peaks, **args)

    def test_shape(self, utc):
        lines = self._render([(NOW - 86400, 50.0)])
        assert len(lines) == 10
        assert all(line.cell_len == 60 for line in lines)

    def test_column_height_tracks_peak(self, utc):
        from claude_rotater.tui.plot import COLUMN_GLYPHS

        lines = self._render([(NOW - 5 * 86400, 100.0), (NOW - 86400, 25.0)], threshold=None)
        body = [line.plain[GUTTER:] for line in lines[:-1]]
        tall = [c for c in range(len(body[0])) if body[0][c] == "█"]
        assert tall, "a 100% window fills to the top row"
        short_rows = [r for r, row in enumerate(body) if any(ch in COLUMN_GLYPHS[1:] for ch in row[max(tall) + 2:])]
        # 25% of 9 rows ≈ 2.25 rows: only the bottom three rows hold the short column
        assert short_rows and min(short_rows) >= len(body) - 3

    def test_peak_color_is_severity(self, utc):
        lines = self._render([(NOW - 86400, 95.0)], threshold=None)
        styles = {str(span.style) for span in lines[-2].spans if lines[-2].plain[span.start:span.end].strip()}
        assert any(SEV_CRIT in s for s in styles)

    def test_threshold_row_dashed_where_empty(self, utc):
        lines = self._render([])
        assert any(THRESHOLD_GLYPH in line.plain for line in lines[:-1])

    def test_windows_outside_span_dropped(self, utc):
        lines = self._render([(NOW - 30 * 86400, 80.0)], threshold=None)
        body = "".join(line.plain[GUTTER:] for line in lines[:-1])
        assert body.strip() == ""

    def test_neighbours_keep_a_gap(self, utc):
        reset = NOW - 86400
        lines = self._render(
            [(reset, 60.0), (reset + 5 * 3600, 60.0)], threshold=None, x0=reset - 10 * 3600, x1=reset + 5 * 3600
        )
        bottom = lines[-2].plain[GUTTER:].rstrip()
        assert " " in bottom.strip()

    def test_degrades_to_sparkline(self, utc):
        lines = self._render([(NOW - 86400, 50.0)], width=12, height=2)
        assert len(lines) == 1

    @staticmethod
    def _back_to_back(n: int, *, last_reset: float = NOW + 2 * 3600) -> list[tuple[float, float]]:
        """``n`` consecutive 5h windows, the newest still running at NOW."""
        return [(last_reset - k * 5 * 3600, 40.0 + k % 5 * 10) for k in range(n)]

    @staticmethod
    def _runs(line) -> list[str]:
        return line.plain[GUTTER:].split()

    def test_windows_per_bar(self):
        from claude_rotater.tui.plot import windows_per_bar

        week, five = 7 * 86400, 5 * 3600
        assert windows_per_bar(window_len_s=five, span_s=week, cols=74) == 1  # 80-col pane
        assert windows_per_bar(window_len_s=five, span_s=week, cols=53) == 2  # 120-col, side by side
        assert windows_per_bar(window_len_s=five, span_s=week, cols=20) == 4
        assert windows_per_bar(window_len_s=five, span_s=week, cols=0) == 1
        assert windows_per_bar(window_len_s=five, span_s=0, cols=50) == 1

    @pytest.mark.parametrize("width", [40, 57, 60, 78, 100])
    def test_back_to_back_week_never_touches(self, utc, width):
        from claude_rotater.tui.plot import windows_per_bar

        peaks = self._back_to_back(34)
        lines = self._render(peaks, threshold=None, width=width)
        bottom = lines[-2].plain[GUTTER:]
        assert all(cell_len == width for cell_len in (line.cell_len for line in lines))
        per_bar = windows_per_bar(window_len_s=5 * 3600, span_s=7 * 86400, cols=width - GUTTER)
        runs = self._runs(lines[-2])
        # Every bar is separated by blank space, and together they account
        # for every window in the week, grouped ``per_bar`` at a time.
        assert len(runs) == pytest.approx(34 / per_bar, abs=1.5)
        assert bottom.rstrip() == bottom, "the running window reaches the right edge"

    def test_one_bar_per_window_when_wide(self, utc):
        lines = self._render(self._back_to_back(34), threshold=None, width=78)
        assert len(self._runs(lines[-2])) == 34

    def test_grouped_bar_shows_highest_peak(self, utc):
        # Two windows in the same bucket: the bar is as tall as the higher.
        peaks = [(NOW - 3 * 86400, 20.0), (NOW - 3 * 86400 + 5 * 3600, 100.0)]
        lines = self._render(peaks, threshold=None, width=30)
        assert len(self._runs(lines[-2])) == 1
        assert "█" in lines[0].plain[GUTTER:]

    def test_clipped_left_bucket_is_kept(self, utc):
        # The oldest visible window sits right at the left edge.
        lines = self._render([(NOW - 7 * 86400 + 60, 70.0)], threshold=None, width=30)
        assert self._runs(lines[-2])
