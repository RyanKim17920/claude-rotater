"""Capture the real TUI replaying a recording: one screen per story step.

    env HOME=<tmp> CLAUDE_CONFIG_DIR=<tmp> ROTA_HOME=<tmp>/.rota \\
        ROTA_SHARED_HOME=<tmp>/.rota \\
        uv run python scripts/demo_capture.py RECORDING STORY OUT_DIR [SCENE ...]

RECORDING is described in demo_world.py. STORY is JSON, also kept out of the
repo (its times are the owner's working hours):

    {"settings": {key: value},
     "scenes": [{"id", "profile", "start", "size": [cols, rows],
                 "members", "disabled", "engine",
                 "steps": [{"at", "keys", "shots"}]}]}

Each step moves the clock to ``at`` (the engine ticking on the way), presses
``keys`` and saves the screen as ANSI and as plain text. ``shots`` are for
demo_video.py.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

from rich.console import Console

import demo_world as dw
from claude_rotater import cost_report, statusline_setup
from claude_rotater.cost_report import CostSource
from claude_rotater.settings import set_setting
from claude_rotater.tui import app as appmod


class IdleLoop:
    """What the app starts: the replay ticks the engine itself."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def run_loop(self) -> int:
        return 0

    def stop(self) -> None:
        pass

    def wake(self) -> None:
        pass

    def apply_threshold(self, _threshold: float) -> None:
        pass


class RecordedCosts:
    """The real cost report over the recorded caches; nothing is repriced."""

    def __init__(self, world: dw.World) -> None:
        others = [d for name, d in world.dirs.items() if name != world.profile]
        # no registry is written: name the other profile for attribution too
        cost_report.other_profile_dirs = lambda _backup_dir: others
        self.source = CostSource(world.backup_dir, world.usage_history, [],
                                 clock=world.clock, other_profiles=lambda: others)

    def cached(self, identities):
        return self.source.cached(identities)

    def refresh(self, identities, *, force=False):
        return self.source.cached(identities)


def follow_replay_clock(clock: dw.Clock) -> None:
    """Make the clock helpers that default to "now" read the replay's."""
    localtime = getattr(time.localtime, "real", time.localtime)
    strftime = getattr(time.strftime, "real", time.strftime)

    def local(seconds=None):
        return localtime(clock() if seconds is None else seconds)

    def stamp(fmt, when=None):
        return strftime(fmt, local() if when is None else when)

    local.real, stamp.real = localtime, strftime
    time.localtime, time.strftime = local, stamp


def screen(app) -> tuple[str, str]:
    """The composited screen as (ANSI, plain text)."""
    width, height = app.size
    console = Console(width=width, height=height, file=io.StringIO(), force_terminal=True,
                      color_system="truecolor", record=True, legacy_windows=False,
                      safe_box=False, _environ={})
    console.print(app.screen._compositor.render_update(
        full=True, screen_stack=app._background_screens, simplify=False))
    return console.file.getvalue(), console.export_text()


async def settle(app, pilot) -> None:
    for _ in range(3):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


async def capture(data: dict, story: dict, scene: dict, out: Path) -> None:
    start = dw.epoch(scene["start"])
    world = dw.World(data, scene.get("profile", "P"), start, scene.get("members"),
                     tuple(scene.get("disabled", ())))
    follow_replay_clock(world.clock)
    world.write_costs()
    set_setting(world.backup_dir, "ui.theme", "dark")
    for key, value in story.get("settings", {}).items():
        set_setting(world.backup_dir, key, value)
    if not statusline_setup.settings_path().is_relative_to(Path.home()):
        raise SystemExit("the statusline settings are outside the throwaway home")
    statusline_setup.install(world.backup_dir)
    appmod.default_engine_factory = IdleLoop
    appmod.default_cost_source_factory = lambda _switcher: RecordedCosts(world)
    app = appmod.RotaApp(world)
    async with app.run_test(size=tuple(scene["size"])) as pilot:
        await settle(app, pilot)
        engine = dw.engine_for(world, app._on_engine_event) if scene.get("engine", True) else None
        if engine:
            engine.tick()
        for i, step in enumerate(scene["steps"]):
            at = dw.epoch(step.get("at", world.clock.now))
            if engine and at > world.clock.now:
                dw.ticks(world, engine, at)
            else:
                world.advance(at)
            app._estimate_stamps = None
            app._start_store_refresh()
            await settle(app, pilot)
            for key in step.get("keys", ()):
                await pilot.press(key)
                await settle(app, pilot)
            app.main_screen.repaint()
            await settle(app, pilot)
            ansi, text = screen(app)
            if "error: " in text:
                raise SystemExit(f"{scene['id']} step {i}: the engine failed in the replay")
            name = f"{scene['id']}-{i:03d}"
            (out / f"{name}.ansi").write_text(ansi)
            (out / f"{name}.txt").write_text(text)
    print(f"{scene['id']}: {len(scene['steps'])} screens")


def main() -> None:
    dw.assert_throwaway()
    recording, story_path, out = map(Path, sys.argv[1:4])
    wanted = set(sys.argv[4:])
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads(recording.read_text())
    story = json.loads(story_path.read_text())
    loop = asyncio.new_event_loop()  # its self-pipe is made before the socket block
    dw.forbid_side_effects()
    for scene in story["scenes"]:
        if not wanted or scene["id"] in wanted:
            loop.run_until_complete(capture(data, story, scene, out))
    loop.close()


if __name__ == "__main__":
    main()
