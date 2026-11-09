"""rota's own memory: the hourly rota.log line and the header hint."""

from __future__ import annotations

import logging
import sys

import pytest

from claude_rotater import self_memory
from claude_rotater.tui.main import AutoStatus, status_text


@pytest.mark.skipif(sys.platform == "win32", reason="no resource module")
def test_peak_rss_is_a_plausible_number_of_megabytes():
    mb = self_memory.peak_rss_mb()
    assert mb is not None and 5 < mb < 8192


def test_warning_only_past_the_threshold():
    assert self_memory.warning_text(None) == ""
    assert self_memory.warning_text(self_memory.WARN_MB - 1) == ""
    assert self_memory.warning_text(312.4) == "mem 312 MB, restart rota"


def test_hourly_log_logs_once_per_hour(caplog):
    log = self_memory.HourlyLog()
    with caplog.at_level(logging.INFO, logger="claude_rotater.self_memory"):
        assert log.maybe_log(1000.0, 55.0)
        assert not log.maybe_log(1000.0 + self_memory.LOG_EVERY_S - 1, 56.0)
        assert log.maybe_log(1000.0 + self_memory.LOG_EVERY_S, 57.0)
        assert not log.maybe_log(99999.0, None)
    assert [r.getMessage() for r in caplog.records] == [
        "rota memory: peak RSS 55 MB",
        "rota memory: peak RSS 57 MB",
    ]


def test_header_shows_the_memory_note_only_when_given():
    status = AutoStatus()
    plain = status_text(status, now=0.0, width=160).plain
    noted = status_text(status, now=0.0, width=160, memory_note="mem 312 MB, restart rota").plain
    assert "mem" not in plain
    assert noted.endswith("mem 312 MB, restart rota")
