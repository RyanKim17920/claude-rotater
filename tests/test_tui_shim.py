"""Tests for the TUI's integration shim (removed with tui/_shim.py at merge)."""

from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_rotater.exceptions import ClaudeSwitchError
from claude_rotater.tui import _shim
from claude_rotater.usage_history import Point

sys.path.insert(0, str(Path(__file__).parent))
from _fake_history import FakeHistory  # noqa: E402

NOW = 1_700_000_000.0


@pytest.fixture
def no_setup_module(monkeypatch):
    monkeypatch.setattr(_shim, "_setup_module", lambda: None)


def _write_feed(backup: Path, mtime: float) -> None:
    from claude_rotater import feed

    path = Path(feed.feed_path(backup))
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))


class TestFeedStatusFallback:
    def test_no_feed_file_is_off(self, tmp_path, no_setup_module):
        assert _shim.feed_status(tmp_path, now=NOW) == _shim.FeedStatus("off", False)

    def test_fresh_feed_is_live(self, tmp_path, no_setup_module):
        _write_feed(tmp_path, NOW - 30)
        assert _shim.feed_status(tmp_path, now=NOW) == _shim.FeedStatus("live", True)

    def test_quiet_feed_is_stale(self, tmp_path, no_setup_module):
        _write_feed(tmp_path, NOW - 3600)
        assert _shim.feed_status(tmp_path, now=NOW).state == "stale"

    def test_install_refuses_without_module(self, no_setup_module):
        with pytest.raises(ClaudeSwitchError):
            _shim.install_statusline()
        with pytest.raises(ClaudeSwitchError):
            _shim.uninstall_statusline()


class TestRealModuleDelegation:
    def _module(self, monkeypatch, status):
        calls: list[str] = []
        module = types.SimpleNamespace(
            status=lambda: status,
            install=lambda: calls.append("install"),
            uninstall=lambda: calls.append("uninstall"),
        )
        monkeypatch.setattr(_shim, "_setup_module", lambda: module)
        return calls

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("live", _shim.FeedStatus("live", True)),
            ("off", _shim.FeedStatus("off", False)),
            ("weird", _shim.FeedStatus("off", False)),
            (types.SimpleNamespace(state="stale", installed=True), _shim.FeedStatus("stale", True)),
            (types.SimpleNamespace(state="off", installed=True), _shim.FeedStatus("off", True)),
        ],
    )
    def test_status_shapes(self, monkeypatch, tmp_path, raw, expected):
        self._module(monkeypatch, raw)
        assert _shim.feed_status(tmp_path) == expected

    def test_status_error_is_off(self, monkeypatch, tmp_path):
        def boom():
            raise RuntimeError("x")

        monkeypatch.setattr(_shim, "_setup_module", lambda: types.SimpleNamespace(status=boom))
        assert _shim.feed_status(tmp_path).state == "off"

    def test_install_and_uninstall_call_through(self, monkeypatch):
        calls = self._module(monkeypatch, "off")
        _shim.install_statusline()
        _shim.uninstall_statusline()
        assert calls == ["install", "uninstall"]


class TestHistoryFallback:
    def _history(self):
        r1, r2 = NOW - 20 * 3600, NOW - 10 * 3600
        pts = [
            Point(NOW - 24 * 3600, 10.0, r1),
            Point(NOW - 22 * 3600, 55.0, r1 + 30),  # jitter: same window
            Point(NOW - 14 * 3600, 70.0, r2),
            Point(NOW - 12 * 3600, 40.0, r2),
            Point(NOW - 11 * 3600, 5.0, None),  # no reset: not a window
        ]
        return FakeHistory({"1": {"5h": pts}}, {"1": ("a@x", "org")})

    def test_long_series_filters_by_since(self):
        out = _shim.long_series(self._history(), "1", ("a@x", "org"), "5h", NOW - 13 * 3600)
        assert [p.pct for p in out] == [40.0, 5.0]

    def test_identity_mismatch_is_empty(self):
        assert _shim.long_series(self._history(), "1", ("b@x", "org"), "5h", 0) == []

    def test_windows_keep_each_peak(self):
        out = _shim.windows(self._history(), "1", ("a@x", "org"), "5h", 0)
        assert [w.peak_pct for w in out] == [55.0, 70.0]
        assert out[0].resets_at == NOW - 20 * 3600

    def test_real_methods_are_preferred(self):
        @dataclass
        class Summary:
            resets_at: float
            peak_pct: float

        class Real:
            def long_series(self, identity, label, since):
                return [Point(since + 1, 1.0, None)]

            def windows(self, identity, label, since):
                return [Summary(since + 5, 42)]

        assert _shim.long_series(Real(), "1", ("a", "b"), "5h", 10.0) == [Point(11.0, 1.0, None)]
        assert _shim.windows(Real(), "1", ("a", "b"), "5h", 10.0) == [_shim.WindowSummary(15.0, 42.0)]
