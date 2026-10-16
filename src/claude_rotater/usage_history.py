"""Per-account usage history: a sample ring plus a fetch-attempt ledger.

``UsageStore`` keeps exactly one row per slot (the last good measurement),
which is enough to serve and gate fetches but not to say how fast an account
is burning. This module keeps the recent past next to it, in its own file
(``cache/usage_history.json``) under its own lock, so the store's lock stays
short and the large store module stays untouched.

Two things live here per slot:

- ``samples``: ``[t, {window_label: [pct, resets_at_epoch_or_null]}]`` for
  every successful measurement. The window label is the
  ``oauth.relevant_windows`` label (``5h``, ``7d``, or a scoped model name),
  the one key the estimator and the TUI share.
- ``attempts``: epochs of every fetch attempt this machine made (successes
  and failures alike), trimmed to the trailing hour. The poll planner reads
  it to enforce the hourly request budget from shared on-disk state, so
  every process on this machine draws on one budget.

History is best effort by contract. A missing, corrupt or foreign-schema
file reads as empty, and a failed write is logged and swallowed: losing
history only degrades scheduling to the plain baseline, while raising here
would fail a usage fetch.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import oauth
from claude_rotater.locking import FileLock
from claude_rotater.settings import atomic_write_json

_logger = logging.getLogger(__name__)

Identity = tuple[str, str]  # (email, organizationUuid), as usage_store.Identity

SCHEMA_VERSION = 1

# Retention, applied on every write.
FULL_RES_S = 24 * 3600.0  # keep every sample younger than this...
THIN_BUCKET_S = 3600.0  # ...then one sample per hour...
MAX_AGE_S = 8 * 86400.0  # ...and nothing older than this (covers a 7d window)
MAX_SAMPLES = 500  # hard cap per slot, oldest dropped first
ATTEMPT_WINDOW_S = 3600.0  # the endpoint's budget horizon

# Every scoped window is stored, whatever the configured models: which models
# matter is a read-time question (estimator ``models``), and a config change
# must not start from an empty history.
_ALL_WINDOWS = ("all",)


@dataclass(frozen=True)
class Point:
    """One measurement of one window."""

    t: float  # epoch the measurement was taken
    pct: float
    resets_at: float | None  # epoch, None when the API sent none


def _parse_reset(resets_at: str | None) -> float | None:
    # Local import: poll_policy imports the estimator, which imports Point.
    from claude_rotater.poll_policy import parse_reset_ts

    return parse_reset_ts(resets_at)


def usage_windows(usage: dict | None) -> dict[str, list]:
    """``{label: [pct, resets_at_epoch | None]}`` for every window in ``usage``."""
    return {
        label: [pct, _parse_reset(resets_at)]
        for label, pct, resets_at in oauth.relevant_windows(usage, _ALL_WINDOWS)
    }


def _retain(samples: list, now: float) -> list:
    """Apply the retention rules to time-sorted samples."""
    recent = [s for s in samples if s[0] >= now - FULL_RES_S]
    older = [s for s in samples if now - MAX_AGE_S <= s[0] < now - FULL_RES_S]
    # One sample per hour bucket for the older part: the newest in each.
    by_bucket: dict[int, list] = {}
    for s in older:
        by_bucket[int(s[0] // THIN_BUCKET_S)] = s
    kept = sorted(by_bucket.values(), key=lambda s: s[0]) + recent
    return kept[-MAX_SAMPLES:]


def _valid_sample(s: object) -> bool:
    return (
        isinstance(s, list)
        and len(s) == 2
        and isinstance(s[0], (int, float))
        and not isinstance(s[0], bool)
        and isinstance(s[1], dict)
    )


class UsageHistory:
    def __init__(self, cache_dir: Path, clock: Callable[[], float] = time.time):
        self.path = cache_dir / "usage_history.json"
        self._lock_path = cache_dir / ".usage_history.lock"
        self.clock = clock

    # -- raw I/O --------------------------------------------------------------

    def _read_rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
            return {}
        rows = raw.get("accounts")
        return rows if isinstance(rows, dict) else {}

    @staticmethod
    def _matches(row: object, identity: Identity) -> bool:
        return (
            isinstance(row, dict)
            and row.get("email") == identity[0]
            and row.get("organizationUuid", "") == identity[1]
        )

    def _row_for(self, rows: dict, num: str, identity: Identity) -> dict:
        """The slot's row, replaced by a fresh one when it belongs to another
        account (a reused slot never inherits history)."""
        row = rows.get(num)
        if not self._matches(row, identity):
            row = {"email": identity[0], "organizationUuid": identity[1]}
            rows[num] = row
        samples = row.get("samples")
        row["samples"] = [s for s in samples if _valid_sample(s)] if isinstance(samples, list) else []
        attempts = row.get("attempts")
        row["attempts"] = (
            [a for a in attempts if isinstance(a, (int, float)) and not isinstance(a, bool)]
            if isinstance(attempts, list)
            else []
        )
        return row

    def _mutate(self, mutator: Callable[[dict], None]) -> None:
        """Read-modify-write under the lock; never raises."""
        try:
            with FileLock(self._lock_path):
                rows = self._read_rows()
                mutator(rows)
                atomic_write_json(
                    self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows}
                )
        except Exception as e:  # best effort: history must never fail a fetch
            _logger.warning(f"Usage history write failed: {e!r}")

    # -- writes ---------------------------------------------------------------

    def record(
        self,
        usages: dict[str, dict | None],
        identities: dict[str, Identity],
        at: float,
    ) -> None:
        """Add one sample per slot (taken at ``at``), in one locked write.

        ``usage=None`` and usages with no window data add nothing. A sample at
        the same ``t`` as an existing one is dropped (the same measurement
        seen twice)."""
        batch = {
            num: windows
            for num, usage in usages.items()
            if usage is not None and num in identities
            and (windows := usage_windows(usage))
        }
        if not batch:
            return

        def apply(rows: dict) -> None:
            now = self.clock()
            for num, windows in batch.items():
                row = self._row_for(rows, num, identities[num])
                samples = row["samples"]
                if any(s[0] == at for s in samples):
                    continue
                samples.append([at, windows])
                samples.sort(key=lambda s: s[0])
                row["samples"] = _retain(samples, now)
                row["attempts"] = self._trim_attempts(row["attempts"], now)

        self._mutate(apply)

    def record_attempts(
        self, nums: Iterable[str], identities: dict[str, Identity], at: float
    ) -> None:
        """Log one fetch attempt at ``at`` per slot (successes and failures)."""
        targets = [num for num in nums if num in identities]
        if not targets:
            return

        def apply(rows: dict) -> None:
            now = self.clock()
            for num in targets:
                row = self._row_for(rows, num, identities[num])
                row["attempts"] = self._trim_attempts(row["attempts"] + [at], now)

        self._mutate(apply)

    @staticmethod
    def _trim_attempts(attempts: list, now: float) -> list:
        return sorted(a for a in attempts if a > now - ATTEMPT_WINDOW_S)

    # -- reads (lock-free: the file is replaced atomically) -------------------

    def series(
        self, identities: dict[str, Identity]
    ) -> dict[str, dict[str, list[Point]]]:
        """``{slot: {window_label: [Point oldest -> newest]}}``.

        A slot with no history, or whose row belongs to another account,
        maps to ``{}``."""
        rows = self._read_rows()
        out: dict[str, dict[str, list[Point]]] = {}
        for num, identity in identities.items():
            row = rows.get(num)
            per_window: dict[str, list[Point]] = {}
            if self._matches(row, identity) and isinstance(row.get("samples"), list):
                samples = sorted(
                    (s for s in row["samples"] if _valid_sample(s)), key=lambda s: s[0]
                )
                for t, windows in samples:
                    for label, value in windows.items():
                        point = _point(t, value)
                        if point is not None:
                            per_window.setdefault(label, []).append(point)
            out[num] = per_window
        return out

    def attempts(self, num: str, identity: Identity, since: float) -> list[float]:
        """Attempt epochs for the slot at or after ``since``, oldest first."""
        row = self._read_rows().get(num)
        if not self._matches(row, identity) or not isinstance(row.get("attempts"), list):
            return []
        return sorted(
            float(a)
            for a in row["attempts"]
            if isinstance(a, (int, float)) and not isinstance(a, bool) and a >= since
        )


def _point(t: float, value: object) -> Point | None:
    if not (isinstance(value, list) and len(value) == 2):
        return None
    pct, resets_at = value
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    if resets_at is not None and (
        isinstance(resets_at, bool) or not isinstance(resets_at, (int, float))
    ):
        resets_at = None
    return Point(float(t), float(pct), None if resets_at is None else float(resets_at))
