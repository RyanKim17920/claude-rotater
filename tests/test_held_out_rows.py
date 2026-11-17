"""An account out of rotation (disabled by the user, or suspended by the
provider) shows its state in place of usage bars."""

from __future__ import annotations

import dataclasses
import time

from claude_rotater.tui.main import (
    MODEL_SLOT,
    RowLayout,
    account_row_text,
    summary_line,
)
from claude_rotater.tui.theme import Palette
from tests.test_tui import has_bar, make_account, make_entry

PALETTE = Palette.DARK


def _row(acc, *, width=120, windows=("5h", "7d"), **kw):
    kw.setdefault("num_need", 3)
    layout = RowLayout.for_width(width, windows, **kw)
    return account_row_text(acc, None, layout, now=time.time(), threshold=90.0,
                            palette=PALETTE, color="#ff0000")


def _style_of(text, needle: str) -> str:
    start = text.plain.index(needle)
    return " ".join(
        str(span.style) for span in text.spans if span.start <= start < span.end
    )


class TestDisabledRow:
    def test_enabled_row_draws_bars(self):
        assert has_bar(_row(make_account(1, entry=make_entry(40, 30))).plain)

    def test_disabled_row_has_no_bars_and_says_disabled(self):
        text = _row(make_account(1, entry=make_entry(40, 30), disabled=True))
        assert not has_bar(text.plain)
        assert "5h" not in text.plain and "7d" not in text.plain
        assert "disabled" in text.plain
        assert PALETTE.muted in _style_of(text, "disabled")

    def test_disabled_row_keeps_number_and_its_colour(self):
        text = _row(make_account(7, entry=make_entry(40, 30), disabled=True))
        assert "#7" in text.plain
        assert "#ff0000" in _style_of(text, "#7")

    def test_disabled_row_hides_the_model_bar_too(self):
        acc = make_account(1, entry=make_entry(40, 30, scoped=[("Fable", 50.0)]), disabled=True)
        text = _row(acc, windows=("5h", "7d", MODEL_SLOT))
        assert "Fable" not in text.plain and not has_bar(text.plain)

    def test_disabled_active_row_still_marks_duty(self):
        text = _row(make_account(1, active=True, entry=make_entry(40, 30), disabled=True))
        assert "disabled" in text.plain and not has_bar(text.plain)

    def test_disabled_tag_is_not_said_twice(self):
        text = _row(make_account(1, entry=make_entry(40, 30), disabled=True), tag_need=11)
        assert " off " not in f" {text.plain} "
        assert text.plain.count("disabled") == 1

    def test_disabled_api_key_row_says_disabled(self):
        text = _row(make_account(1, kind="api_key", disabled=True))
        assert "disabled" in text.plain

    def test_summary_line_of_disabled_has_no_usage(self):
        line = summary_line(make_account(1, entry=make_entry(40, 30), disabled=True),
                            None, threshold=90.0, now=time.time())
        assert "5h" not in line and "disabled" in line


class TestProviderBlockedRow:
    def _blocked(self, **kw):
        acc = make_account(2, entry=make_entry(40, 30), **kw)
        return dataclasses.replace(acc, provider_block="payment past due")

    def test_blocked_row_has_no_bars_and_says_suspended_in_red(self):
        text = _row(self._blocked())
        assert not has_bar(text.plain)
        assert "suspended by provider" in text.plain
        assert "payment past due" in text.plain
        assert PALETTE.sev_crit in _style_of(text, "suspended by provider")

    def test_blocked_wins_over_disabled(self):
        text = _row(self._blocked(disabled=True))
        assert "suspended by provider" in text.plain

    def test_summary_line_of_blocked(self):
        line = summary_line(self._blocked(), None, threshold=90.0, now=time.time())
        assert "suspended by provider" in line and "5h" not in line


class TestNoForecastWhenHeldOut:
    def test_disabled_and_blocked_have_no_forecast(self):
        from claude_rotater.tui.main import forecast

        now = time.time()
        live = make_account(1, entry=make_entry(95, 30))
        assert forecast(live, None, threshold=90.0, now=now) is not None
        for acc in (dataclasses.replace(live, disabled=True),
                    dataclasses.replace(live, provider_block="payment past due")):
            assert forecast(acc, None, threshold=90.0, now=now) is None
