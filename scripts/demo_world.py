"""A throwaway rota world that replays an anonymised usage recording.

The recording (a JSON file that stays out of the repo) holds only numbers:

    history       {account: [[t, {window: [pct, resets_at]}], ...]}
    long          {account: {window: {h0, h, p}}}, as usage_longterm.json
    per_pct       {account: {window: [stored $ per 1% samples]}}
    profiles      {name: {slots, switches, log_start, costs}}

``account`` is a pseudonym such as A0. Everything rota shows is computed by
rota itself from the files written here: the engine decides, the forecasts
and costs come from the real modules. Only the edges are faked: the clock,
the usage fetch (the last recorded reading) and the login switch.
"""
from __future__ import annotations

import bisect
import socket
import string
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from os import utime
from pathlib import Path

from claude_rotater import autoswitch, cost_report, instance, paths
from claude_rotater.feed import feed_path
from claude_rotater.models import AccountSnapshot, AccountsSnapshot
from claude_rotater.poll_policy import load_cadence
from claude_rotater.settings import atomic_write_json, load_settings
from claude_rotater.usage_history import DUTY_PAST_S, UsageHistory
from claude_rotater.usage_store import UsageEntry

WINDOW_KEYS = {"5h": "five_hour", "7d": "seven_day"}


def assert_throwaway() -> None:
    """Refuse to run unless every path rota resolves is in a temp folder."""
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/private/tmp"), Path("/tmp")}
    for path in (
        Path.home(),
        paths.get_claude_config_home(),
        paths.get_backup_root(),
        paths.get_shared_cache_dir(),
    ):
        real = path.resolve()
        if not any(real.is_relative_to(root) for root in temp_roots):
            raise SystemExit(f"not a throwaway path, set it when launching: {real}")


def forbid_side_effects() -> None:
    """No network and no child process (so no Keychain, CLI or ccusage)."""

    def refuse(*_args, **_kwargs):
        raise OSError("the replay has no network and starts no process")

    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    subprocess.Popen = refuse
    subprocess.run = refuse
    instance._rota_engine_pids = list  # no look at the rotas really running


def epoch(value: str | float) -> float:
    if isinstance(value, str):
        return datetime.fromisoformat(value).timestamp()
    return float(value)


def iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class World:
    """One profile of the recording as of the clock's time, on disk.

    ``members`` is ``{slot: account}`` as it was then; ``disabled`` the slots
    held out of rotation. The account on duty follows the recorded switches
    up to ``start`` and the engine's own from there on.
    """

    def __init__(self, data: dict, profile: str, start: float,
                 members: dict[str, str] | None = None, disabled: tuple[str, ...] = ()) -> None:
        self.data, self.profile = data, profile
        self.clock = Clock(start)
        time.time = self.clock
        self.members = members or data["profiles"][profile]["slots"]
        self.disabled = set(disabled if members else data["profiles"][profile]["disabled"])
        accounts = sorted(data["history"])
        self.identities = {
            a: (f"{string.ascii_lowercase[i]}•••@example.com", f"org-{string.ascii_lowercase[i]}")
            for i, a in enumerate(accounts)
        }
        self.keys = {a: "|".join(identity) for a, identity in self.identities.items()}
        default = paths.get_default_backup_root()
        self.dirs = {profile: default}
        for other in data["profiles"]:
            if other != profile:
                self.dirs[other] = default.parent / f"profile-{other.lower()}"
        for directory in self.dirs.values():
            (directory / "cache").mkdir(parents=True, exist_ok=True)
        self.backup_dir = default
        self.cache_dir = paths.get_shared_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage_history = UsageHistory(self.cache_dir, clock=self.clock)
        self.duty = {p: self._recorded_duty(p, start) for p in data["profiles"]}
        self.active = self.slot_of(self.duty[profile][-1][1])
        self._write_static()
        state = {"schemaVersion": 1, "lastSwitchAt": self.duty[profile][-1][0]}
        atomic_write_json(self.backup_dir / autoswitch.STATE_FILENAME, state)
        self.advance(start)

    def slot_of(self, account: str) -> str:
        return next(n for n, a in self.members.items() if a == account)

    def _recorded_duty(self, profile: str, until: float) -> list[tuple[float, str]]:
        return [(t, to) for t, to, *_ in self.data["profiles"][profile]["switches"] if t <= until]

    # -- files -----------------------------------------------------------------

    def _write_static(self) -> None:
        for profile, directory in self.dirs.items():
            record = self.data["profiles"][profile]
            slots = self.members if profile == self.profile else record["slots"]
            atomic_write_json(directory / "sequence.json", {
                "sequence": sorted(int(n) for n in slots),
                "activeAccountNumber": int(self.active) if profile == self.profile else None,
                "accounts": {
                    n: {"email": self.identities[a][0], "organizationUuid": self.identities[a][1]}
                    for n, a in slots.items()
                },
            })
        atomic_write_json(self.cache_dir / "dollar_per_pct.json", {
            "schemaVersion": cost_report.dpp.STORE_SCHEMA,
            "accounts": {self.keys[a]: v for a, v in self.data["per_pct"].items()},
        })

    def advance(self, now: float) -> None:
        """Move the clock and rewrite what rota would have on disk by then."""
        self.clock.now = now
        hour = int(now // 3600)
        rows, longterm = {}, {}
        for account, samples in self.data["history"].items():
            email, org = self.identities[account]
            upto = samples[: bisect.bisect_right([s[0] for s in samples], now)]
            rows[self.keys[account]] = {
                "email": email,
                "organizationUuid": org,
                "samples": [s for s in upto if s[0] >= now - 8 * 86400],
                "foldedHour": hour,
            }
            labels = {}
            for label, entry in self.data["long"].get(account, {}).items():
                kept = {"p": [p for p in entry["p"] if p[0] <= now]}
                if entry["h0"] is not None and entry["h0"] < hour:
                    kept["h0"], kept["h"] = entry["h0"], entry["h"][: hour - entry["h0"]]
                labels[label] = kept
            longterm[self.keys[account]] = {"w": labels}
        table = {}
        for profile, switches in self.duty.items():
            done = [s for s in switches if s[0] <= now]
            if profile != self.profile:
                done = self._recorded_duty(profile, now)
            if not done:
                continue
            slots = self.members if profile == self.profile else self.data["profiles"][profile]["slots"]
            table[str(self.dirs[profile])] = {
                "active": self.keys.get(done[-1][1]),
                "since": done[-1][0],
                "members": sorted(self.keys[a] for a in slots.values()),
                "past": [
                    [a, b, self.keys[who]]
                    for (a, who), (b, _) in zip(done, done[1:])
                    if who in self.keys and b >= now - DUTY_PAST_S
                ],
            }
        atomic_write_json(self.usage_history.path,
                          {"schemaVersion": 2, "accounts": rows, "duty": table}, compact=True)
        atomic_write_json(self.usage_history.long_path,
                          {"schemaVersion": 1, "accounts": longterm}, compact=True)
        feed = Path(feed_path(self.backup_dir))
        feed.parent.mkdir(parents=True, exist_ok=True)
        feed.write_text("{}\n")
        utime(feed, (now, now))

    def write_costs(self) -> None:
        """The recorded cost caches and the switch lines they are credited by."""
        for profile, directory in self.dirs.items():
            record = self.data["profiles"][profile]
            projects = directory / "replay-projects"
            projects.mkdir(exist_ok=True)
            costs = record["costs"]
            atomic_write_json(directory / "cache" / "costs.json", {
                "schemaVersion": 1,
                "boundary": costs["boundary"],
                "builtAt": costs["builtAt"],
                "tookS": costs["tookS"],
                "sources": [str(projects.resolve())],
                "offsets": {},
                "rows": costs["rows"],
                "archive": costs["archive"],
                "archiveTokens": costs["archiveTokens"],
            }, compact=True)
            lines = [(record["log_start"], "replay started")]
            for t, number, account in record["added"]:
                lines.append((t, f"Added account {number} (org: {self.identities[account][1]})"))
            for t, _to, _frm, to_slot, from_slot in record["switches"]:
                lines.append((t, f"Switched from account {from_slot or '-'} to {to_slot}"))
            (directory / "rota.log").write_text("".join(
                f"{datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S},000 - INFO - {text}\n"
                for t, text in sorted(lines)
            ))

    # -- the switcher surface the TUI and the engine use ---------------------------

    def entry(self, account: str) -> UsageEntry:
        now = self.clock.now
        samples = self.data["history"][account]
        i = bisect.bisect_right([s[0] for s in samples], now)
        if not i:
            return UsageEntry()
        usage: dict = {}
        for label, (pct, resets_at) in samples[i - 1][1].items():
            if resets_at is not None and resets_at <= now:
                pct, resets_at = 0.0, None  # what a poll reads once the window has reset
            if label in WINDOW_KEYS:
                usage[WINDOW_KEYS[label]] = {"pct": pct, "resets_at": iso(resets_at)}
            else:
                usage.setdefault("scoped", []).append(
                    {"name": label, "pct": pct, "resets_at": iso(resets_at)})
        # A poll now returns the last reading: usage only moves at a recorded one.
        return UsageEntry(last_good=usage, fetched_at=now, age_s=0.0)

    def accounts_snapshot(self, fetch=None) -> AccountsSnapshot:
        accounts = tuple(
            AccountSnapshot(
                number=n, email=self.identities[a][0], org_name="", org_uuid=self.identities[a][1],
                is_active=n == self.active, kind="oauth", switchable=True, usage=self.entry(a),
                disabled=n in self.disabled,
            )
            for n, a in sorted(self.members.items(), key=lambda item: int(item[0]))
        )
        return AccountsSnapshot(active_number=self.active, accounts=accounts, taken_at=self.clock.now)

    def usage_entries_by_account(self, fetch=None, **_kwargs) -> dict[str, UsageEntry]:
        return {n: self.entry(a) for n, a in self.members.items()}

    def usage_identities(self) -> dict[str, tuple[str, str]]:
        return {n: self.identities[a] for n, a in self.members.items()}

    def switch_to(self, number, json_output=False, force=False) -> dict:
        before, self.active = self.active, str(number)
        self.duty[self.profile].append((self.clock.now, self.members[self.active]))
        self.advance(self.clock.now)
        return {
            "switched": True,
            "from": {"number": int(before), "email": self.account_email(before)},
            "to": {"number": int(self.active), "email": self.account_email(self.active)},
        }

    def current_account_number(self) -> str:
        return self.active

    def account_email(self, number) -> str:
        return self.identities[self.members[str(number)]][0]

    def account_identity(self, number) -> tuple[str, str]:
        return self.identities[self.members[str(number)]]

    def account_kind_for(self, _number) -> str:
        return "oauth"

    def is_account_disabled(self, number) -> bool:
        return str(number) in self.disabled

    def key_unserved_over_login(self, _number) -> bool:
        return False  # the replay holds no API key

    def provider_block_of(self, _number) -> None:
        return None

    def switchable_account_numbers(self) -> list[str]:
        return [n for n in self.members if n not in self.disabled]

    def live_session_pids_for(self, *_args) -> list[int]:
        return []

    def models_in_use(self) -> tuple[str, ...]:
        return ()

    def has_live_login(self) -> bool:
        return True

    def _poll_cadence(self):
        return load_cadence(self.backup_dir)

    def set_poll_policy_inputs(self, *_args) -> None:
        pass

    def clear_poll_policy_inputs(self) -> None:
        pass


class ReplayEngine(autoswitch.AutoSwitchEngine):
    """The real engine, without its login and provider side effects."""

    def _freshen_target(self, number, email) -> str:
        return "ok"

    def _check_provider_refusal(self, current, state) -> None:
        pass

    def _watch_logins(self, entries) -> None:
        pass


def engine_for(world: World, on_event, *, dry_run: bool = False) -> ReplayEngine:
    return ReplayEngine(world, load_settings(world.backup_dir), on_event,
                        clock=world.clock, dry_run=dry_run)


def ticks(world: World, engine: ReplayEngine, until: float, every: float = 60.0) -> None:
    """Tick at every recorded reading of the account on duty, and every minute."""
    start = world.clock.now
    times = {until}
    t = start + every
    while t < until:
        times.add(t)
        t += every
    for account in world.members.values():
        times.update(s[0] for s in world.data["history"][account] if start < s[0] <= until)
    for t in sorted(times):
        world.advance(t)
        engine.tick()
