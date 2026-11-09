"""Next-bill estimates from a subscription start date."""

from __future__ import annotations

import json
import os
from datetime import date, datetime

import pytest

from claude_rotater.billing import (
    ANNUAL,
    MONTHLY,
    bill_note,
    next_bill_date,
    parse_started_at,
    started_at_from_config,
    started_at_from_file,
)


class TestNextBillDate:
    def test_next_anniversary_this_month(self):
        assert next_bill_date(date(2026, 8, 14), date(2026, 11, 9)) == date(2026, 11, 14)

    def test_passed_this_month_rolls_to_next(self):
        assert next_bill_date(date(2026, 8, 6), date(2026, 11, 9)) == date(2026, 12, 6)

    def test_anniversary_today_is_today(self):
        assert next_bill_date(date(2026, 8, 9), date(2026, 11, 9)) == date(2026, 11, 9)

    def test_started_today_bills_a_period_later(self):
        assert next_bill_date(date(2026, 11, 9), date(2026, 11, 9)) == date(2026, 12, 9)

    def test_start_in_the_future_bills_a_period_after_it(self):
        # clock skew or a timezone edge: never a date before the start
        assert next_bill_date(date(2026, 11, 10), date(2026, 11, 9)) == date(2026, 12, 10)

    def test_year_wrap(self):
        assert next_bill_date(date(2026, 10, 20), date(2026, 12, 25)) == date(2027, 1, 20)

    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 2, 1), date(2026, 2, 28)),  # short month: its last day
            (date(2026, 3, 1), date(2026, 3, 31)),  # back to the 31st, not the 28th
            (date(2026, 4, 1), date(2026, 4, 30)),
            (date(2028, 2, 1), date(2028, 2, 29)),  # leap February
        ],
    )
    def test_month_end_start_clamps_from_the_start_day(self, today, expected):
        assert next_bill_date(date(2026, 1, 31), today) == expected

    def test_annual(self):
        assert next_bill_date(date(2025, 3, 5), date(2026, 11, 9), ANNUAL) == date(2027, 3, 5)
        assert next_bill_date(date(2025, 12, 5), date(2026, 11, 9), ANNUAL) == date(2026, 12, 5)

    def test_annual_leap_day_start(self):
        assert next_bill_date(date(2024, 2, 29), date(2026, 1, 1), ANNUAL) == date(2026, 2, 28)
        assert next_bill_date(date(2024, 2, 29), date(2027, 6, 1), ANNUAL) == date(2028, 2, 29)

    def test_bad_period(self):
        with pytest.raises(ValueError):
            next_bill_date(date(2026, 1, 1), date(2026, 2, 1), 0)

    def test_monthly_is_the_default(self):
        assert next_bill_date(date(2026, 1, 5), date(2026, 3, 9)) == next_bill_date(
            date(2026, 1, 5), date(2026, 3, 9), MONTHLY
        )


class TestParsing:
    def test_iso_with_z_and_fraction(self):
        assert parse_started_at("2026-10-26T03:38:18.336162Z") == pytest.approx(
            datetime.fromisoformat("2026-10-26T03:38:18.336162+00:00").timestamp()
        )

    @pytest.mark.parametrize("value", [None, "", "  ", "not a date", 17, {}])
    def test_unusable_values(self, value):
        assert parse_started_at(value) is None

    def test_from_config(self):
        cfg = {"oauthAccount": {"subscriptionCreatedAt": "2026-06-09T23:14:49Z"}}
        assert started_at_from_config(cfg) == parse_started_at("2026-06-09T23:14:49Z")

    @pytest.mark.parametrize(
        "cfg", [None, [], {}, {"oauthAccount": None}, {"oauthAccount": {"subscriptionCreatedAt": None}}]
    )
    def test_from_config_missing(self, cfg):
        assert started_at_from_config(cfg) is None


class TestFromFile:
    def test_reads_and_follows_rewrites(self, tmp_path):
        path = tmp_path / ".claude-config-1-a@b.c.json"
        path.write_text(json.dumps({"oauthAccount": {"subscriptionCreatedAt": "2026-06-09T00:00:00Z"}}))
        assert started_at_from_file(path) == parse_started_at("2026-06-09T00:00:00Z")
        path.write_text(json.dumps({"oauthAccount": {"subscriptionCreatedAt": "2026-07-19T00:00:00Z"}}))
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a later save
        assert started_at_from_file(path) == parse_started_at("2026-07-19T00:00:00Z")

    def test_missing_or_corrupt(self, tmp_path):
        assert started_at_from_file(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_bytes(b"\xff{not json")
        assert started_at_from_file(bad) is None


class TestBillNote:
    def test_unknown_start_says_nothing(self):
        assert bill_note(None, 0.0) == ""

    def test_estimate_is_marked(self):
        start = datetime(2026, 8, 14, 12).timestamp()
        now = datetime(2026, 11, 9, 12).timestamp()
        assert bill_note(start, now) == "bills ~Nov 14"

    def test_no_zero_padded_day(self):
        start = datetime(2026, 8, 6, 12).timestamp()
        now = datetime(2026, 11, 9, 12).timestamp()
        assert bill_note(start, now) == "bills ~Dec 6"
