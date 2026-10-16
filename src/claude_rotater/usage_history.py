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

Past the sample ring's eight days, a downsampled long-term history lives in
``cache/usage_longterm.json``, keyed by account identity (not slot) and
bounded to ``LONG_MAX_AGE_S`` (90 days):

- ``h``: per window label, the hourly peak pct, one integer of tenths per
  hour from hour index ``h0`` on (``-1`` marks an hour with no sample).
- ``p``: per window label, ``[resets_at, peak_tenths]`` for every window
  seen, the current one included.

Completed hours are folded in from the sample ring at most once an hour per
account (``foldedHour`` in the slot row says how far), so the long-term
file is rewritten hourly, not on every sample. Readers merge it with the
not-yet-folded samples: :meth:`UsageHistory.long_series` and
:meth:`UsageHistory.windows`.

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

# Long-term history.
LONG_SCHEMA_VERSION = 1
HOUR_S = 3600.0
LONG_MAX_AGE_S = 90 * 86400.0
# Two readings of one window's resets_at farther apart than this are two
# windows (the estimator's RESET_MATCH_S; not imported: estimator imports
# this module).
WINDOW_MATCH_S = 120.0
_GAP = -1
_NO_ROW_HOUR = 1 << 62

# Every scoped window is stored, whatever the configured models: which models
# matter is a read-time question (estimator ``models``), and a config change
# must not start from an empty history.
_ALL_WINDOWS = ("all",)


@dataclass(frozen=True)
class WindowSummary:
    """One rate-limit window, past or current: its reset and highest pct."""

    resets_at: float  # epoch; later than now for the window still running
    peak_pct: float


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
        self.long_path = cache_dir / "usage_longterm.json"
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
                    self.path,
                    {"schemaVersion": SCHEMA_VERSION, "accounts": rows},
                    compact=True,
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
            due: list[tuple[dict, int]] = []
            for num, windows in batch.items():
                old = rows.get(num)
                if _retiring(old, identities[num]):
                    # The slot now holds another account: keep everything
                    # the old one had not folded yet.
                    due.append((old, _NO_ROW_HOUR))
                row = self._row_for(rows, num, identities[num])
                samples = row["samples"]
                if any(s[0] == at for s in samples):
                    continue
                samples.append([at, windows])
                samples.sort(key=lambda s: s[0])
                if _hour(now) > _folded_hour(row):
                    due.append((row, _hour(now)))
                row["samples"] = _retain(samples, now)
                row["attempts"] = self._trim_attempts(row["attempts"], now)
            if due:
                self._fold(due, now)

        self._mutate(apply)

    # -- long-term history ----------------------------------------------------

    def _read_long(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.long_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != LONG_SCHEMA_VERSION:
            return {}
        accounts = raw.get("accounts")
        return accounts if isinstance(accounts, dict) else {}

    def _fold(self, due: list[tuple[dict, int]], now: float) -> None:
        """Fold the due rows' samples from their ``foldedHour`` up to (not
        including) the paired hour into the long-term file (called under
        the history lock)."""
        accounts = self._read_long()
        cutoff = _hour(now - LONG_MAX_AGE_S)
        for row, current in due:
            key = _identity_key((row["email"], row.get("organizationUuid", "")))
            account = accounts.get(key)
            if not isinstance(account, dict):
                account = accounts[key] = {}
            labels = account.get("w")
            if not isinstance(labels, dict):
                labels = account["w"] = {}
            start = _folded_hour(row)
            points: dict[str, list[Point]] = {}
            for t, windows in filter(_valid_sample, row.get("samples") or ()):
                if start <= _hour(t) < current:
                    for label, value in windows.items():
                        point = _point(t, value)
                        if point is not None:
                            points.setdefault(label, []).append(point)
            for label in set(labels) | set(points):
                hours, peaks = _decode(labels.get(label))
                for point in points.get(label, ()):
                    hour = _hour(point.t)
                    hours[hour] = max(hours.get(hour, 0.0), point.pct)
                    if point.resets_at is not None:
                        _merge_window(peaks, point.resets_at, point.pct)
                entry = _encode(hours, peaks, cutoff)
                if entry:
                    labels[label] = entry
                else:
                    labels.pop(label, None)
            row["foldedHour"] = current
        for key in list(accounts):
            if not accounts[key].get("w"):
                del accounts[key]
        atomic_write_json(
            self.long_path,
            {"schemaVersion": LONG_SCHEMA_VERSION, "accounts": accounts},
            compact=True,
        )

    def _unfolded(self, identity: Identity, label: str) -> tuple[int, list[Point]]:
        """``(foldedHour, points)`` of the not-yet-folded samples of the
        slot holding ``identity`` (the newest such row if several do)."""
        # No row: everything is in the long-term file.
        best: tuple[int, list[Point]] = (_NO_ROW_HOUR, [])
        newest = -1.0
        for row in self._read_rows().values():
            if not self._matches(row, identity) or not isinstance(row.get("samples"), list):
                continue
            samples = [s for s in row["samples"] if _valid_sample(s)]
            last = max((s[0] for s in samples), default=-1.0)
            if last <= newest:
                continue
            newest = last
            folded = _folded_hour(row)
            points = sorted(
                (p for t, windows in samples
                 if _hour(t) >= folded and (p := _point(t, windows.get(label))) is not None),
                key=lambda p: p.t,
            )
            best = (folded, points)
        return best

    def _long_label(self, identity: Identity, label: str) -> object:
        account = self._read_long().get(_identity_key(identity))
        labels = account.get("w") if isinstance(account, dict) else None
        return labels.get(label) if isinstance(labels, dict) else None

    def long_series(self, identity: Identity, label: str, since: float) -> list[Point]:
        """Hourly peak pct of window ``label`` from ``since`` on, oldest
        first: one :class:`Point` per hour with a sample, ``t`` the hour's
        start, ``resets_at`` None (see :meth:`windows` for the resets).
        Covers up to ``LONG_MAX_AGE_S`` back, the current hour included.
        Lock-free; never raises."""
        try:
            folded, points = self._unfolded(identity, label)
            hours, _ = _decode(self._long_label(identity, label))
            peaks = {hour: pct for hour, pct in hours.items() if hour < folded}
            for point in points:
                hour = _hour(point.t)
                peaks[hour] = max(peaks.get(hour, 0.0), point.pct)
            first = _hour(since)
            return [
                Point(hour * HOUR_S, pct, None)
                for hour, pct in sorted(peaks.items())
                if hour >= first
            ]
        except Exception as e:  # best effort, like every read here
            _logger.warning(f"Usage history long_series failed: {e!r}")
            return []

    def windows(self, identity: Identity, label: str, since: float) -> list[WindowSummary]:
        """Every window of ``label`` resetting at or after ``since``, oldest
        first, with its peak pct; the last one is the window still running
        when its ``resets_at`` is in the future. Lock-free; never raises."""
        try:
            _, peaks = _decode(self._long_label(identity, label))
            for point in self._unfolded(identity, label)[1]:
                if point.resets_at is not None:
                    _merge_window(peaks, point.resets_at, point.pct)
            return [
                WindowSummary(resets_at, peak)
                for resets_at, peak in sorted(peaks)
                if resets_at >= since
            ]
        except Exception as e:
            _logger.warning(f"Usage history windows failed: {e!r}")
            return []

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


def _hour(t: float) -> int:
    return int(t // HOUR_S)


def _folded_hour(row: dict) -> int:
    value = row.get("foldedHour")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _retiring(row: object, identity: Identity) -> bool:
    """Whether ``row`` belongs to an account other than ``identity`` and
    still has samples worth folding."""
    return (
        isinstance(row, dict)
        and isinstance(row.get("email"), str)
        and isinstance(row.get("samples"), list)
        and bool(row["samples"])
        and not UsageHistory._matches(row, identity)
    )


def _identity_key(identity: Identity) -> str:
    return f"{identity[0]}|{identity[1]}"


def _tenths(pct: float) -> int:
    return max(0, round(pct * 10))


def _valid_pair(item: object) -> bool:
    return (
        isinstance(item, list)
        and len(item) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in item)
    )


def _hourly(entry: dict) -> list[tuple[int, int]]:
    """``(hour, peak_tenths)`` for every hour with a value."""
    h0, values = entry.get("h0"), entry.get("h")
    if isinstance(h0, bool) or not isinstance(h0, int) or not isinstance(values, list):
        return []
    return [
        (h0 + i, v)
        for i, v in enumerate(values)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0
    ]


def _decode(entry: object) -> tuple[dict[int, float], list[list[float]]]:
    """One label's stored long-term entry as ``({hour: peak_pct},
    [[resets_at, peak_pct], ...])``; garbage reads as empty."""
    if not isinstance(entry, dict):
        return {}, []
    hours = {hour: tenths / 10.0 for hour, tenths in _hourly(entry)}
    raw = entry.get("p")
    peaks = [
        [float(item[0]), item[1] / 10.0]
        for item in (raw if isinstance(raw, list) else [])
        if _valid_pair(item)
    ]
    return hours, peaks


def _encode(hours: dict[int, float], peaks: list[list[float]], cutoff_hour: int) -> dict:
    """The compact stored form, dropping what is older than ``cutoff_hour``."""
    entry: dict = {}
    kept = sorted(h for h in hours if h >= cutoff_hour)
    if kept:
        h0 = kept[0]
        values = [_GAP] * (kept[-1] - h0 + 1)
        for hour in kept:
            values[hour - h0] = _tenths(hours[hour])
        entry["h0"], entry["h"] = h0, values
    windows = sorted(
        [round(r), _tenths(p)] for r, p in peaks if r >= cutoff_hour * HOUR_S
    )
    if windows:
        entry["p"] = windows
    return entry


def _merge_window(windows: list[list[float]], resets_at: float, pct: float) -> None:
    for window in windows:
        if abs(window[0] - resets_at) <= WINDOW_MATCH_S:
            window[1] = max(window[1], pct)
            return
    windows.append([resets_at, pct])


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
