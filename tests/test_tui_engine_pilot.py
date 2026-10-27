"""The TUI and the real auto engine together, the way the owner runs them.

Nothing here is faked between the engine and the screen: the real
``AutoSwitchEngine`` runs on its worker thread with the real
``FeedIngestor``, statusline records arrive in the feed file over time, the
engine polls the (stubbed) usage endpoint through the real switcher and
store, and the pace strategy makes the switch on its own. The test then
reads what the page shows: the header, every row's bar and forecast, and the
charts under the table. Only the network and the engine's tick floor are
replaced (the floor so a minute of engine life fits in a few seconds).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_rotater import feed
from claude_rotater.autoswitch import SwitchEvent
from claude_rotater.settings import set_setting
from claude_rotater.tui.main import MainScreen
from tests.test_autoswitch import EngineHarness, _iso_at

H = 3600.0


class Usage:
    """The usage endpoint: one mutable reading per access token."""

    def __init__(self) -> None:
        self.by_token: dict[str, dict] = {}

    def set(self, tokens: tuple[str, ...], pct5: float, pct7: float, r5: float, r7: float) -> None:
        raw = {
            "five_hour": {"utilization": pct5, "resets_at": _iso_at(r5)},
            "seven_day": {"utilization": pct7, "resets_at": _iso_at(r7)},
        }
        for token in tokens:
            self.by_token[token] = raw

    def __call__(self, access_token: str) -> dict:
        return json.loads(json.dumps(self.by_token[access_token]))


def feed_record(home: Path, pct5: float, pct7: float, r5: float, r7: float, t: float) -> dict:
    return {
        "t": t,
        "at": t,
        "five_hour": {"pct": pct5, "resets_at": r5},
        "seven_day": {"pct": pct7, "resets_at": r7},
        "session_id": "s-1",
        "config_dir": str(home / ".claude"),
    }


@pytest.fixture
def world(temp_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ROTA_HOME", str(temp_home / ".rota"))
    h = EngineHarness(temp_home, strategy="pace", threshold=99.9)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    # The real clock everywhere: the engine thread runs on it.
    h.switcher._usage_store.clock = time.time
    h.switcher.usage_history.clock = time.time
    for key, value in (
        ("autoswitch.strategy", "pace"),
        ("autoswitch.threshold", 99.9),
    ):
        set_setting(h.switcher.backup_dir, key, value)
    usage = Usage()
    monkeypatch.setattr("claude_rotater.oauth.request_usage_data", usage)
    monkeypatch.setattr("claude_rotater.autoswitch.MIN_TICK_S", 0.3)
    monkeypatch.setattr("claude_rotater.autoswitch.PROACTIVE_CONFIRM_S", 1.0)
    return h, usage


async def until(pilot, predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await pilot.pause(0.1)


def page(app) -> dict:
    screen: MainScreen = app.main_screen
    width = 140
    return {
        "header": screen.status_line(width, 1).plain,
        "rows": {n: screen.row_text(n, width).plain for n in ("1", "2")},
        "charts": screen.detail_text(width, 30).plain,
        "cursor": screen.selected_number,
        "active": app.snapshot.active_number if app.snapshot else None,
    }


@pytest.mark.asyncio
async def test_pace_switch_moves_cursor_charts_and_forecasts(world):
    from claude_rotater.tui.app import RotaApp

    h, usage = world
    home = h.temp_home
    now = time.time()
    r5_a, r5_b, r7 = now + 3 * H, now + 2 * H, now + 4 * 24 * H
    # #1 on duty has been burning for the last hour (5h 40 -> 70); #2 idle.
    for i in range(13):
        t = now - H + i * 300
        h.switcher.usage_history.record(
            {"1": {"five_hour": {"pct": 40 + i * 2.5, "resets_at": _iso_at(r5_a)},
                   "seven_day": {"pct": 40 + i * 0.25, "resets_at": _iso_at(r7)}},
             "2": {"five_hour": {"pct": 10.0, "resets_at": _iso_at(r5_b)},
                   "seven_day": {"pct": 45.0, "resets_at": _iso_at(r7)}}},
            {"1": ("a@example.com", ""), "2": ("b@example.com", "")}, t,
        )
    usage.set(("sk-live", "sk-1"), 70, 43, r5_a, r7)
    usage.set(("sk-2",), 10, 45, r5_b, r7)
    path = feed.feed_path(h.switcher.backup_dir)

    app = RotaApp(h.switcher, detected="dark")
    seen: list = []
    original = app._on_engine_event

    def on_event(event):
        seen.append(event)
        original(event)

    app._on_engine_event = on_event
    async with app.run_test(size=(140, 45)) as pilot:
        await until(pilot, lambda: app.snapshot is not None and "~" in page(app)["rows"]["1"])
        before = page(app)
        assert before["cursor"] == before["active"] == "1"
        assert before["charts"].startswith("› #1 a@example.com")
        assert "~5h limit in" in before["rows"]["1"]  # the user is burning #1
        # The user keeps working on #1, one statusline record at a time:
        # its week fills until #2 needs using more than it by pace's margin
        # (#1 past about 63%). Only the feed says so (the endpoint still
        # answers 43%), so the switch proves the ingestion.
        five, seven = 70.0, 43.0
        while not any(isinstance(e, SwitchEvent) for e in seen):
            assert seven < 85.0, [e.human() for e in seen]
            five, seven = five + 1.0, seven + 2.0
            feed.append_record(path, feed_record(home, five, seven, r5_a, r7, time.time()))
            await pilot.pause(0.4)
        switch = next(e for e in seen if isinstance(e, SwitchEvent))
        assert switch.trigger == "pace" and switch.to_ref["number"] == 2
        await until(pilot, lambda: page(app)["active"] == "2", timeout=10.0)
        await pilot.pause(0.5)
        after = page(app)
    assert "switch → #2" in after["header"]
    # The cursor rode the account on duty, so it and the charts followed.
    assert after["cursor"] == "2"
    assert after["charts"].startswith("› #2 b@example.com"), after["charts"][:80]
    # The new duty carries the user's burn at once; the old one stops
    # projecting the burn that was the user's, not its own.
    two, one = app.estimates["2"], app.estimates["1"]
    assert two.windows["5h"].rate_pct_per_h > 20.0
    assert two.windows["7d"].rate_pct_per_h > 1.0
    assert "~" in after["rows"]["2"], after["rows"]["2"]
    assert all(w.rate_pct_per_h is None for w in one.windows.values())
    assert "~" not in after["rows"]["1"], after["rows"]["1"]
