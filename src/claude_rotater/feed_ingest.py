"""Ingest the statusline feed (``rota feed``) into the usage store and history.

A feed record is the usage Claude Code saw on its last API response, but the
record does not say which account that response was for. This module decides
(:func:`attribute`), then writes accepted readings through
:meth:`UsageStore.adopt` with a hold, which keeps every collector (engine,
TUI, ``rota status``) from polling that account while the feed is
fresh, and into the usage history so the estimator and plots see them.

**Attribution.** What a running session does after ``rota switch``: it keeps
its old token until it next reads the credential store (on its next request
on Linux/Windows, where credentials are a watched file; up to ~30 s later on
macOS, where the Keychain read is cached), and its statusline keeps showing
the rate limits of its *last response* until it makes a new one, which can be
minutes for an idle session. So "the active account at record time" is right
for most records but wrong for a while after any switch. The rules, in
order:

1. Too old (past ``feedStaleSeconds``) or from the future: dropped.
2. Profile: the record's config dir (from Claude Code's own transcript path)
   must be this profile's config home, else it belongs to a different
   profile and is dropped. A ``rota run`` session dir
   (``<backup>/sessions/<slot>-<email slug>``) is attributed to that slot:
   its account never changes under it.
3. A record for the active account stamped before the last switch, or
   within ``SWITCH_GRACE_S`` after it, is dropped (the session may not have
   picked up the new token yet).
4. Consistency with what the store already knows about that account, per
   window: a ``resets_at`` that disagrees with the stored one (while the
   stored window has not ended) means the record describes another
   account's window, and a pct more than ``BEHIND_PCT`` below the stored one
   within the same window is a stale report (usage never falls inside a
   window). Either drops the record. With nothing stored to compare against,
   a record whose windows match a *different* account's is dropped too.
5. Windows that had already reset by record time are ignored.

Dropping is always safe: the account simply falls back to polling, and the
next good record (every statusline refresh produces one) is taken.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from claude_rotater import feed, oauth, paths, poll_policy
from claude_rotater.estimator import RESET_DROP_PCT, RESET_MATCH_S

if TYPE_CHECKING:
    from claude_rotater.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("rota")

# After a switch, a running session keeps its old token until it re-reads
# the credential store (~30 s Keychain cache on macOS) and keeps showing the
# last response's limits until its next request. The consistency check
# catches longer-lived stale reports; this covers the window where the store
# may not yet know the new account's resets_at to check against.
SWITCH_GRACE_S = 90.0

# A record stamped this far ahead of the ingesting clock is not trusted.
FUTURE_SKEW_S = 60.0

# Within one window usage never decreases; a reading this far below the
# stored one is a stale report, not news.
BEHIND_PCT = RESET_DROP_PCT

# An unchanged reading is added to the history at most this often (the
# statusline refreshes every few seconds while a session is busy).
HISTORY_MIN_GAP_S = 60.0

ACCEPTED = "accepted"
ACCEPTED_SESSION = "accepted-session"


@dataclass(frozen=True)
class Verdict:
    slot: str | None
    reason: str

    @property
    def accepted(self) -> bool:
        return self.slot is not None


@dataclass
class IngestResult:
    """What one :meth:`FeedIngestor.ingest` pass did."""

    adopted: dict[str, float] = field(default_factory=dict)  # slot -> record t
    rejected: Counter = field(default_factory=Counter)  # reason -> count

    @property
    def changed(self) -> bool:
        return bool(self.adopted)


def _norm(path: str) -> str:
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        resolved = os.path.abspath(path)
    return os.path.normcase(resolved)


def _live_windows(record: dict, t: float) -> dict[str, dict]:
    """The record's windows that had not reset by record time."""
    live = {}
    for key in feed.WINDOW_KEYS:
        window = record.get(key)
        if not isinstance(window, dict):
            continue
        reset = window.get("resets_at")
        if reset is not None and reset <= t:
            continue
        live[key] = window
    return live


def _compare(window: dict, stored_usage: dict | None, key: str, t: float) -> str | None:
    """``"match"``, a conflict reason, or None (nothing to compare)."""
    if not isinstance(stored_usage, dict):
        return None
    stored = stored_usage.get(key)
    if not isinstance(stored, dict):
        return None
    stored_reset = poll_policy.parse_reset_ts(stored.get("resets_at"))
    reset = window.get("resets_at")
    if stored_reset is None or reset is None:
        return None
    if stored_reset <= t:
        return None  # the stored window has ended: a new one is expected
    if abs(reset - stored_reset) > RESET_MATCH_S:
        return "window-mismatch"
    stored_pct = stored.get("pct")
    if isinstance(stored_pct, (int, float)) and window["pct"] < stored_pct - BEHIND_PCT:
        return "behind-stored"
    return "match"


def _consistency(windows: dict[str, dict], stored_usage: dict | None, t: float) -> str | None:
    """A conflict reason, ``"match"`` if at least one window matched, else None."""
    matched = False
    for key, window in windows.items():
        result = _compare(window, stored_usage, key, t)
        if result is None:
            continue
        if result != "match":
            return result
        matched = True
    return "match" if matched else None


def _session_slot(
    config_dir: str, sessions_dir: Path, identities: dict[str, tuple[str, str]]
) -> str | None:
    """The slot a ``rota run`` session dir belongs to, or None."""
    from claude_rotater.session import session_dir_for

    name = os.path.basename(config_dir.rstrip("/\\"))
    num = name.split("-", 1)[0]
    identity = identities.get(num)
    if identity is None:
        return None
    expected = session_dir_for(sessions_dir.parent, num, identity[0])
    return num if expected.name == name else None


def attribute(
    record: dict,
    *,
    now: float,
    stale_s: float,
    active_slot: str | None,
    config_home: str,
    sessions_dir: Path,
    identities: dict[str, tuple[str, str]],
    last_switch_at: float | None,
    stored: dict[str, dict | None],
) -> Verdict:
    """Which stored account ``record`` describes, or why it can't be told
    safely (see the module docstring for the rules). Pure."""
    t = float(record["t"])
    if t > now + FUTURE_SKEW_S:
        return Verdict(None, "future")
    if now - t > stale_s:
        return Verdict(None, "stale")
    windows = _live_windows(record, t)
    if not windows:
        return Verdict(None, "window-ended")

    config_dir = record.get("config_dir") or ""
    norm_dir = _norm(config_dir) if config_dir else ""
    session = False
    if norm_dir and os.path.dirname(norm_dir) == _norm(str(sessions_dir)):
        slot = _session_slot(norm_dir, sessions_dir, identities)
        if slot is None:
            return Verdict(None, "unknown-session")
        session = True
    elif norm_dir and norm_dir != _norm(config_home):
        return Verdict(None, "foreign-profile")
    else:
        slot = active_slot
        if slot is None or slot not in identities:
            return Verdict(None, "no-active")
        if last_switch_at is not None and t < last_switch_at + SWITCH_GRACE_S:
            return Verdict(None, "switch-grace")

    result = _consistency(windows, stored.get(slot), t)
    if result is not None and result != "match":
        return Verdict(None, result)
    if result is None:
        for other, usage in stored.items():
            if other != slot and _consistency(windows, usage, t) == "match":
                return Verdict(None, "matches-other-account")
    return Verdict(slot, ACCEPTED_SESSION if session else ACCEPTED)


def merged_usage(stored: dict | None, record: dict) -> dict:
    """The stored usage dict with the record's live windows laid over it, in
    the shape ``oauth.build_usage_result`` produces. Scoped (per-model)
    windows and extra usage, which the statusline does not report, are kept
    from the stored reading. Within one window the higher pct wins."""
    usage = dict(stored) if isinstance(stored, dict) else {}
    t = float(record["t"])
    for key, window in _live_windows(record, t).items():
        entry: dict = {"pct": float(window["pct"])}
        reset = window.get("resets_at")
        if reset is not None:
            iso = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
            entry["resets_at"] = iso
            entry["countdown"], entry["clock"] = oauth.format_reset(iso)
        if _compare(window, stored, key, t) == "match":
            entry["pct"] = max(entry["pct"], float(stored[key]["pct"]))
        usage[key] = entry
    return usage


def read_last_switch_at(backup_dir: Path) -> float | None:
    """``lastSwitchAt`` from the auto engine's shared state file (stamped by
    engine and manual switches alike), lock-free; None if unknown."""
    from claude_rotater.autoswitch import STATE_FILENAME

    try:
        raw = json.loads((backup_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = raw.get("lastSwitchAt") if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class FeedIngestor:
    """Reads new feed records and records the attributable ones.

    Holds the file cursor and the history throttle, so one instance lives as
    long as its engine. Not thread-safe: the engine calls it from its own
    loop thread only."""

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.switcher = switcher
        self.backup_dir = Path(switcher.backup_dir)
        self.path = feed.feed_path(self.backup_dir)
        self.clock = clock
        self._cursor: feed.FeedCursor | None = None
        self._last_sample: dict[str, tuple[float, tuple]] = {}

    @property
    def watch_dir(self) -> str:
        return feed.feed_dir(self.backup_dir)

    def ingest(self) -> IngestResult:
        """One pass: read what was appended since the last pass, attribute
        it, adopt the newest accepted reading per account. Never raises."""
        result = IngestResult()
        try:
            records, self._cursor = feed.read_new(self.path, self._cursor)
            if records:
                self._apply(records, result)
        except Exception as e:
            _logger.warning(f"Statusline feed ingest failed: {e!r}")
        if result.rejected:
            _logger.debug("Statusline feed records dropped: %s", dict(result.rejected))
        return result

    def _apply(self, records: list[dict], result: IngestResult) -> None:
        now = self.clock()
        stale_s = poll_policy.load_cadence(self.backup_dir).feed_stale_s
        identities = self.switcher.usage_identities()
        store = self.switcher._usage_store
        stored = {num: entry.last_good for num, entry in store.entries(identities).items()}
        active = self.switcher.current_account_number()
        last_switch_at = read_last_switch_at(self.backup_dir)
        config_home = str(paths.get_claude_config_home())
        sessions_dir = self.backup_dir / "sessions"

        newest: dict[str, dict] = {}
        for record in records:
            verdict = attribute(
                record,
                now=now,
                stale_s=stale_s,
                active_slot=active,
                config_home=config_home,
                sessions_dir=sessions_dir,
                identities=identities,
                last_switch_at=last_switch_at,
                stored=stored,
            )
            if not verdict.accepted:
                result.rejected[verdict.reason] += 1
                continue
            prev = newest.get(verdict.slot)
            if prev is None or record["t"] >= prev["t"]:
                newest[verdict.slot] = record

        for slot, record in newest.items():
            usage = merged_usage(stored.get(slot), record)
            age = max(0.0, now - float(record["t"]))
            ident = {slot: identities[slot]}
            store.adopt({slot: (usage, age)}, ident, hold_s=max(0.0, stale_s - age))
            result.adopted[slot] = float(record["t"])
            self._sample(slot, usage, ident, float(record["t"]))

    def _sample(self, slot: str, usage: dict, ident: dict, t: float) -> None:
        key = tuple(
            (key, usage[key].get("pct"), usage[key].get("resets_at"))
            for key in feed.WINDOW_KEYS
            if isinstance(usage.get(key), dict)
        )
        last = self._last_sample.get(slot)
        if last is not None and last[1] == key and t - last[0] < HISTORY_MIN_GAP_S:
            return
        if last is not None and t <= last[0]:
            return
        try:
            self.switcher.usage_history.record({slot: usage}, ident, t)
        except Exception as e:
            _logger.warning(f"Usage history record (feed) failed: {e!r}")
            return
        self._last_sample[slot] = (t, key)
