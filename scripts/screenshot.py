"""Render the README screenshots from made-up demo data.

    uv run python scripts/screenshot.py   -> docs/screenshot.svg, docs/costs.svg

Runs the real TUI under Textual's test pilot with fake accounts, usage and
costs (the fixtures in tests/), in a throwaway HOME and ROTA_HOME, so it never
reads or touches a real account. Convert the SVGs to PNG with any SVG
renderer, e.g. a headless browser screenshot of each file.
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
tmp_home = tempfile.mkdtemp(prefix="rota-shots-")
os.environ["HOME"] = tmp_home  # nothing may reach the real home
os.environ["ROTA_HOME"] = str(Path(tmp_home) / ".rota")
os.environ.pop("CLAUDE_CONFIG_DIR", None)  # statusline_setup must edit the fake home only

import test_tui as T  # noqa: E402
from _fake_history import FakeHistory  # noqa: E402
from claude_rotater.usage_history import Point  # noqa: E402


def history(now: float) -> dict:
    rnd = random.Random(7)
    r5 = now + 2 * 3600  # the snapshot's resets (make_entry): 2h, 3d, Fable 2d
    r7 = now + 3 * 86400
    # account 1: current 5h burn, 2h at 12%/h; past 5h windows over 8 days
    cur5 = [Point(now - 7200 + i * 300, 38 + 12 * i * 300 / 3600, r5) for i in range(25)]
    past5 = []
    t = now - 8 * 86400
    while True:
        reset = t + 5 * 3600
        if reset > now - 3 * 3600:
            break
        peak = rnd.choice([25, 40, 55, 68, 74, 88, 93, 97, 35, 60])
        for k in range(1, 6):
            past5.append(Point(t + k * 3600 - 1800, peak * k / 5, reset))
        t = reset + rnd.choice([0, 3600, 7200, 5 * 3600, 9 * 3600])
    prev7 = r7 - 7 * 86400
    seven = [Point(prev7 - 7 * 86400 + h * 3600, min(81, h * 0.48), prev7) for h in range(0, 168, 2)]
    seven += [Point(prev7 + h * 3600, 31 * h / 96, r7) for h in range(0, 97)]
    # account 2: light use
    two5 = [Point(now - 5400 + i * 600, 6 + i * 0.7, r5) for i in range(10)]
    two7 = [Point(prev7 + h * 3600, 30 + 14 * h / 96, r7) for h in range(0, 97, 3)]
    fable = [Point(prev7 + h * 3600, 34 * h / 96, now + 2 * 86400) for h in range(0, 97, 2)]
    return {
        "1": {"5h": past5 + cur5, "7d": seven, "Fable": fable},
        "2": {"5h": two5, "7d": two7},
    }


def accounts(backup: Path):
    """Four accounts: on duty with a Fable window, a real org, a personal
    account whose org is "<email>'s Organization" (shown once), a disabled one."""
    import dataclasses

    from claude_rotater.usage_store import UsageEntry

    def with_org(acc, name):
        return dataclasses.replace(acc, org_name=name, org_uuid="org-" + acc.number)

    login_soon = time.time() + 2 * 86400 + 3600
    return T.FakeSwitcher(
        [
            dataclasses.replace(
                T.make_account(1, active=True, entry=T.make_entry(62.0, 31.0, scoped=[("Fable", 34.0)]),
                               email="a•••@example.com"),
                login_expires_at=time.time() + 15 * 86400,
            ),
            with_org(T.make_account(2, entry=T.make_entry(12.0, 44.0, scoped=[("Fable", 3.0)]),
                                    email="j•••@example.com"), "Acme Corp"),
            dataclasses.replace(
                with_org(T.make_account(3, entry=T.make_entry(91.0, 85.0, scoped=[("Fable", 0.0)]),
                                        email="s•••@example.org"), "s•••@example.org's Organization"),
                login_expires_at=login_soon,
            ),
            T.make_account(4, disabled=True, entry=UsageEntry(), email="o•••@example.net"),
        ],
        backup,
    )


class ShotCosts:
    """A cost source over 30 made-up days: three models, three accounts
    taking turns, about $3 per 1% of 5h and $9 per 1% of 7d, all run
    through the real report code."""

    def __init__(self, now: float):
        from claude_rotater import cost_report as cr, costs, dollar_per_pct as dpp

        rnd = random.Random(11)
        lo = costs.range_start(now)
        rows, spans = [], []
        t, who = lo, "1"
        while t < now:  # duty: mostly 1, now and then 2 or 3
            length = rnd.choice([6, 10, 20, 30]) * 3600
            spans.append((t, min(now, t + length), who))
            t += length
            who = rnd.choice(["1", "1", "2", "3"])
        for slot in range(int(lo // 900) * 900, int(now), 900):
            hour = time.localtime(slot).tm_hour
            day = (slot - lo) / 86400
            if not 9 <= hour <= 23 or rnd.random() < 0.35:
                continue
            base = (0.4 + day / 30) * (0.6 if time.localtime(slot).tm_wday >= 5 else 1.0)
            for model, share in (("claude-opus-4-7", 0.7), ("claude-sonnet-4-5", 0.25), ("claude-haiku-4-5", 0.05)):
                usd = base * share * rnd.uniform(0.5, 1.6)
                rows.append(costs.Row("s0", slot, model, usd, int(usd * 40), int(usd * 2600),
                                      int(usd * 90_000), int(usd * 1_500_000)))
        cache = costs.CostCache(costs.day_start(now), ["/fake"], rows, {}, now - 240, 1.4)
        spend = cr.attribute(rows, [costs.Source("s0", "/fake")], spans)
        per_pct = {}
        for number, series in spend.items():
            if number not in ("1", "2", "3"):  # unattributed spend has no windows
                continue
            per_pct[number] = {}
            for duration, label, rate in ((5 * 3600, "5h", 3.0), (7 * 86400, "7d", 9.0)):
                found, step = [], min(duration, 6 * 3600)  # one climb every few hours
                for start in range(int(lo), int(now), step):
                    usd = sum(c for s, c in series if start <= s < start + step)
                    if usd:
                        pct = usd / (rate * rnd.uniform(0.8, 1.25))
                        found.append(dpp.Sample(start + step / 2, start + duration, usd, pct, 0.0))
                per_pct[number][label] = dpp.summarize(found, label, now)
        self.report = cr.build_report(cache, [costs.Source("s0", "/fake")], spans, now, per_pct)

    def cached(self, identities):
        from claude_rotater.cost_report import CostResult

        return CostResult(self.report)

    def refresh(self, identities, *, force=False):
        from claude_rotater.cost_report import CostResult

        return CostResult(self.report)


async def shoot(size, name, keys=(), theme=None):
    backup = Path(tempfile.mkdtemp(prefix="backup-", dir=tmp_home))
    if theme:
        from claude_rotater.settings import set_setting

        set_setting(backup, "ui.theme", theme)
    from claude_rotater import feed as feedmod, statusline_setup

    assert statusline_setup.settings_path().is_relative_to(tmp_home)
    statusline_setup.install(backup)  # the fake home's settings.json
    feed = Path(feedmod.feed_path(backup))
    feed.parent.mkdir(parents=True, exist_ok=True)
    feed.write_text("{}\n")
    fake = accounts(backup)
    fake.usage_history = FakeHistory(history(time.time()))
    T.FakeEngine.instances = []
    import claude_rotater.tui.app as appmod

    appmod.default_engine_factory = T.FakeEngine
    appmod.default_refresher_factory = T.FakeRefresher
    appmod.default_cost_source_factory = lambda _switcher: ShotCosts(time.time())
    app = appmod.RotaApp(fake)
    async with app.run_test(size=size) as pilot:
        await T.settle(pilot)
        for key in keys:
            await pilot.press(key)
            await T.settle(pilot)
        await asyncio.sleep(0.3)
        await T.settle(pilot)
        out = ROOT / "docs" / f"{name}.svg"
        app.save_screenshot(filename=out.name, path=str(out.parent))
        print(out)


async def main():
    await shoot((120, 36), "screenshot")
    await shoot((120, 36), "costs", ("dollar_sign",))


asyncio.run(main())
