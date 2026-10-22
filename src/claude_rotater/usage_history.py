"""Per-account usage history: a sample ring plus a fetch-attempt ledger.

``UsageStore`` keeps exactly one row per slot (the last good measurement),
which is enough to serve and gate fetches but not to say how fast an account
is burning. This module keeps the recent past next to it, in its own file
(``cache/usage_history.json``) under its own lock, so the store's lock stays
short and the large store module stays untouched.

History belongs to the account, not to a profile: one file per machine
(:func:`claude_rotater.paths.get_shared_cache_dir`, the default data dir's
``cache`` unless ``ROTA_SHARED_HOME`` says otherwise), keyed by identity
(``email|organizationUuid``), read and written by every rota on the machine,
whatever its data dir. An account that two profiles both hold therefore has
one history, one attempt ledger and one forecast, and a reading either
profile takes serves the other (``latest``, adopted by the collector instead
of a second request). A profile's older slot-keyed file is merged in once
and renamed ``*.merged``.

Per identity:

- ``samples``: ``[t, {window_label: [pct, resets_at_epoch_or_null]}]`` for
  every successful measurement. The window label is the
  ``oauth.relevant_windows`` label (``5h``, ``7d``, or a scoped model name),
  the one key the estimator and the TUI share.
- ``attempts``: epochs of every fetch attempt any rota on this machine made
  for the account (successes and failures alike), trimmed to the trailing
  hour. The poll planner reads it to enforce the hourly request budget from
  shared on-disk state, so every process and profile draws on one budget.
- ``latest``: ``[t, usage, data_dir]``, the newest full reading and which
  data dir took it.

Beside them, ``duty`` says per data dir which account it has on duty, since
when, and which accounts it holds (:meth:`UsageHistory.set_duty`), so every
profile reads an account it shares the same way (:func:`shared_duty`).

Past the sample ring's eight days, a downsampled long-term history lives in
``cache/usage_longterm.json``, keyed by account identity and bounded to
``LONG_MAX_AGE_S`` (90 days):

- ``h``: per window label, the hourly peak pct, one integer of tenths per
  hour from hour index ``h0`` on (``-1`` marks an hour with no sample).
- ``p``: per window label, ``[resets_at, peak_tenths]`` for every window
  seen, the current one included.

Completed hours are folded in from the sample ring at most once an hour per
account (``foldedHour`` in the row says how far), so the long-term file is
rewritten hourly, not on every sample. Readers merge it with the
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

SCHEMA_VERSION = 2  # 2: rows keyed by identity (1: by slot, one file per profile)

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
    def __init__(
        self,
        cache_dir: Path,
        clock: Callable[[], float] = time.time,
        *,
        legacy_dirs: Iterable[Path] = (),
    ):
        """History in ``cache_dir`` (the machine's shared cache dir); each of
        ``legacy_dirs`` (a profile's own cache dir) has its old slot-keyed
        files merged in once, best effort."""
        self.path = cache_dir / "usage_history.json"
        self._lock_path = cache_dir / ".usage_history.lock"
        self.long_path = cache_dir / "usage_longterm.json"
        self.clock = clock
        for legacy in legacy_dirs:
            self._merge_legacy(Path(legacy))

    # -- raw I/O --------------------------------------------------------------

    def _read_raw(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _read_rows(self) -> dict[str, dict]:
        return _rows_of(self._read_raw())

    @staticmethod
    def _matches(row: object, identity: Identity) -> bool:
        return (
            isinstance(row, dict)
            and row.get("email") == identity[0]
            and row.get("organizationUuid", "") == identity[1]
        )

    def _row_for(self, rows: dict, identity: Identity) -> dict:
        """The account's row, created (or cleaned) on demand."""
        key = _identity_key(identity)
        row = rows.get(key)
        if not self._matches(row, identity):
            row = {"email": identity[0], "organizationUuid": identity[1]}
            rows[key] = row
        _clean_row(row)
        return row

    def _mutate(
        self, mutator: Callable[[dict], None], *, duty: Callable[[dict], bool] | None = None
    ) -> None:
        """Read-modify-write under the lock; never raises. ``duty`` edits the
        ``duty`` table and says whether it changed anything (nothing is
        written when only it ran and changed nothing)."""
        try:
            with FileLock(self._lock_path):
                raw = self._read_raw()
                rows = _rows_of(raw)
                table = _duty_of(raw)
                changed = True
                mutator(rows)
                if duty is not None:
                    changed = duty(table)
                if not changed:
                    return
                atomic_write_json(
                    self.path,
                    {"schemaVersion": SCHEMA_VERSION, "accounts": rows, "duty": table},
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
        *,
        by: Path | str | None = None,
    ) -> None:
        """Add one sample per slot (taken at ``at``), in one locked write.

        ``usage=None`` and usages with no window data add nothing. A sample at
        the same ``t`` as an existing one is dropped (the same measurement
        seen twice). ``by`` (the data dir that took the reading) also makes
        it the account's ``latest`` when it is the newest, for another
        profile to adopt instead of fetching."""
        batch = {
            num: (usage, windows)
            for num, usage in usages.items()
            if usage is not None and num in identities
            and (windows := usage_windows(usage))
        }
        if not batch:
            return

        def apply(rows: dict) -> None:
            now = self.clock()
            due: list[tuple[dict, int]] = []
            for num, (usage, windows) in batch.items():
                row = self._row_for(rows, identities[num])
                if by is not None and at > _latest_t(row):
                    row["latest"] = [at, usage, str(by)]
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

    def set_duty(
        self,
        data_dir: Path | str,
        *,
        active: Identity | None,
        since: float | None,
        members: Iterable[Identity],
    ) -> None:
        """Say which account ``data_dir`` has on duty (``active``), since its
        last switch (``since``), and which accounts it holds. Written only
        when something changed, so calling it every pass costs a read."""
        record = {
            "active": None if active is None else _identity_key(active),
            "since": since,
            "members": sorted({_identity_key(i) for i in members}),
        }
        key = str(data_dir)
        if _duty_of(self._read_raw()).get(key) == record:
            return

        def edit(table: dict) -> bool:
            if table.get(key) == record:
                return False
            table[key] = record
            for other in list(table):  # a data dir removed since
                if other != key and not Path(other).is_dir():
                    del table[other]
            return True

        self._mutate(lambda _rows: None, duty=edit)

    # -- legacy merge ---------------------------------------------------------

    def _merge_legacy(self, legacy: Path) -> None:
        """Merge a profile's own (pre-sharing) history files into this one,
        then rename them ``*.merged``. A no-op when there is nothing to
        merge or ``legacy`` is this cache dir (its slot-keyed rows are
        re-keyed on read and written back keyed by identity). Never raises."""
        old = legacy / "usage_history.json"
        old_long = legacy / "usage_longterm.json"
        try:
            if legacy.resolve() == self.path.parent.resolve():
                return
            if not old.exists() and not old_long.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self._lock_path):
                try:
                    theirs = json.loads(old.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    theirs = {}
                raw = self._read_raw()
                rows = _rows_of(raw)
                for key, row in _rows_of(theirs if isinstance(theirs, dict) else {}).items():
                    _merge_row(rows, key, row)
                atomic_write_json(
                    self.path,
                    {"schemaVersion": SCHEMA_VERSION, "accounts": rows, "duty": _duty_of(raw)},
                    compact=True,
                )
                long_theirs = _read_long_file(old_long)
                if long_theirs:
                    accounts = self._read_long()
                    cutoff = _hour(self.clock() - LONG_MAX_AGE_S)
                    for key, account in long_theirs.items():
                        _merge_long_account(accounts, key, account, cutoff)
                    atomic_write_json(
                        self.long_path,
                        {"schemaVersion": LONG_SCHEMA_VERSION, "accounts": accounts},
                        compact=True,
                    )
            for path in (old, old_long):
                if path.exists():
                    path.replace(path.with_name(path.name + ".merged"))
            _logger.info(f"Merged usage history from {legacy} into {self.path.parent}")
        except Exception as e:
            _logger.warning(f"Usage history merge from {legacy} failed: {e!r}")

    # -- long-term history ----------------------------------------------------

    def _read_long(self) -> dict[str, dict]:
        return _read_long_file(self.long_path)

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
                row = self._row_for(rows, identities[num])
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

        A slot whose account has no history maps to ``{}``."""
        rows = self._read_rows()
        out: dict[str, dict[str, list[Point]]] = {}
        for num, identity in identities.items():
            row = rows.get(_identity_key(identity))
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
        """Attempt epochs for the account at or after ``since``, oldest
        first, whichever rota on this machine made them."""
        row = self._read_rows().get(_identity_key(identity))
        if not self._matches(row, identity) or not isinstance(row.get("attempts"), list):
            return []
        return sorted(
            float(a)
            for a in row["attempts"]
            if isinstance(a, (int, float)) and not isinstance(a, bool) and a >= since
        )

    def latest(self, identities: dict[str, Identity]) -> dict[str, Latest]:
        """Per slot, the newest full reading any rota here recorded for its
        account (see :meth:`record`'s ``by``); slots with none are left out."""
        rows = self._read_rows()
        out: dict[str, Latest] = {}
        for num, identity in identities.items():
            row = rows.get(_identity_key(identity))
            if not self._matches(row, identity):
                continue
            value = row.get("latest")
            if (
                isinstance(value, list) and len(value) == 3
                and isinstance(value[0], (int, float)) and not isinstance(value[0], bool)
                and isinstance(value[1], dict) and isinstance(value[2], str)
            ):
                out[num] = Latest(float(value[0]), value[1], value[2])
        return out

    def duty(self) -> dict[str, dict]:
        """``{data_dir: {"active", "since", "members"}}`` (see :meth:`set_duty`)."""
        return _duty_of(self._read_raw())


@dataclass(frozen=True)
class Latest:
    """An account's newest full reading: when, the usage dict, and the data
    dir of the rota that took it."""

    t: float
    usage: dict
    by: str


def shared_duty(
    duty: dict[str, dict],
    identities: dict[str, Identity],
    data_dir: Path | str,
) -> dict[str, tuple[bool, float | None]]:
    """Per slot whose account another data dir also holds: ``(on_duty,
    since)`` read the same way from every profile — on duty when any data
    dir holding it has it on duty, ``since`` the latest switch among those
    data dirs. The estimator reads such an account from these instead of
    this profile's own switch, so every window shows it the same forecast.
    Slots this profile alone holds are left out."""
    own = str(data_dir)
    out: dict[str, tuple[bool, float | None]] = {}
    for num, identity in identities.items():
        key = _identity_key(identity)
        holders = {
            owner: record for owner, record in duty.items()
            if key in record.get("members", ()) or record.get("active") == key
        }
        if not set(holders) - {own}:
            continue
        on_duty = any(record.get("active") == key for record in holders.values())
        stamps = [
            float(record["since"]) for record in holders.values()
            if isinstance(record.get("since"), (int, float))
            and not isinstance(record.get("since"), bool)
        ]
        out[num] = (on_duty, max(stamps) if stamps else None)
    return out


def _rows_of(raw: dict) -> dict[str, dict]:
    """The identity-keyed rows of a history file of either schema: a
    version-1 (slot-keyed, one per profile) file is re-keyed, rows of one
    account merged."""
    rows = raw.get("accounts")
    if not isinstance(rows, dict):
        return {}
    version = raw.get("schemaVersion")
    if version == SCHEMA_VERSION:
        return rows
    if version != 1:
        return {}
    out: dict[str, dict] = {}
    for row in rows.values():
        if isinstance(row, dict) and isinstance(row.get("email"), str):
            key = _identity_key((row["email"], row.get("organizationUuid", "") or ""))
            _merge_row(out, key, row)
    return out


def _duty_of(raw: dict) -> dict[str, dict]:
    table = raw.get("duty") if raw.get("schemaVersion") == SCHEMA_VERSION else None
    if not isinstance(table, dict):
        return {}
    return {k: v for k, v in table.items() if isinstance(v, dict)}


def _clean_row(row: dict) -> None:
    samples = row.get("samples")
    row["samples"] = [s for s in samples if _valid_sample(s)] if isinstance(samples, list) else []
    attempts = row.get("attempts")
    row["attempts"] = (
        [a for a in attempts if isinstance(a, (int, float)) and not isinstance(a, bool)]
        if isinstance(attempts, list)
        else []
    )


def _latest_t(row: dict) -> float:
    value = row.get("latest")
    if isinstance(value, list) and value and isinstance(value[0], (int, float)):
        return float(value[0])
    return float("-inf")


def _merge_row(rows: dict, key: str, row: dict) -> None:
    """Merge one account's row into ``rows[key]``: samples and attempts by
    time, the newer ``latest``, and the earlier ``foldedHour`` (folding a
    sample twice is harmless: the long-term file keeps peaks)."""
    if not isinstance(row, dict) or not isinstance(row.get("email"), str):
        return
    row = dict(row)
    _clean_row(row)
    mine = rows.get(key)
    if not isinstance(mine, dict):
        rows[key] = {
            k: v for k, v in row.items()
            if k in ("email", "organizationUuid", "samples", "attempts", "foldedHour", "latest")
        }
        return
    _clean_row(mine)
    by_t = {s[0]: s for s in mine["samples"]}
    for sample in row["samples"]:
        by_t.setdefault(sample[0], sample)
    mine["samples"] = sorted(by_t.values(), key=lambda s: s[0])[-MAX_SAMPLES:]
    mine["attempts"] = sorted(set(mine["attempts"]) | set(row["attempts"]))
    folded = [h for h in (mine.get("foldedHour"), row.get("foldedHour"))
              if isinstance(h, int) and not isinstance(h, bool)]
    if folded:
        mine["foldedHour"] = min(folded)
    if _latest_t(row) > _latest_t(mine):
        mine["latest"] = row["latest"]


def _read_long_file(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("schemaVersion") != LONG_SCHEMA_VERSION:
        return {}
    accounts = raw.get("accounts")
    return accounts if isinstance(accounts, dict) else {}


def _merge_long_account(accounts: dict, key: str, theirs: object, cutoff: int) -> None:
    """Merge one account's long-term entry: per label, the higher hourly
    peak and every window's higher peak."""
    if not isinstance(theirs, dict) or not isinstance(theirs.get("w"), dict):
        return
    account = accounts.get(key)
    if not isinstance(account, dict) or not isinstance(account.get("w"), dict):
        accounts[key] = {"w": dict(theirs["w"])}
        return
    labels = account["w"]
    for label, entry in theirs["w"].items():
        hours, peaks = _decode(labels.get(label))
        their_hours, their_peaks = _decode(entry)
        for hour, pct in their_hours.items():
            hours[hour] = max(hours.get(hour, 0.0), pct)
        for resets_at, pct in their_peaks:
            _merge_window(peaks, resets_at, pct)
        merged = _encode(hours, peaks, cutoff)
        if merged:
            labels[label] = merged


def _hour(t: float) -> int:
    return int(t // HOUR_S)


def _folded_hour(row: dict) -> int:
    value = row.get("foldedHour")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


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
