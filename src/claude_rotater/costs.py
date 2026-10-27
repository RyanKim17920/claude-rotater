"""Claude Code spend from local transcripts, priced by ccusage, on demand.

rota does no pricing of its own: ccusage (https://github.com/ryoppippi/ccusage)
reads Claude Code's transcripts and prices every response, deduplicating
streamed repeats the way it always does. What rota adds is *when*: ccusage
only filters by date, while attribution to accounts and the $ per 1% fit
need cost per quarter hour. So rota copies the usage records of the
transcripts into a throwaway config dir laid out as
``projects/<source>/<slot>.jsonl``, one file per 15-minute slot, and asks
``ccusage claude session --json --breakdown``: ccusage names a session after
its file, so every "session" it reports is one slot of one source.

Nothing here runs in the background. The TUI's costs view calls it when it
opens or on ``r``; the engine, the feed and ``rota auto`` never import this
module (a test guards that). Node is not required: without an installed
``ccusage``, ``bunx`` or ``npx``, :func:`find_runner` returns None and the
view says how to get one.

The cache (``<data>/cache/costs.json``) keeps the priced slots of the last
:data:`RANGE_DAYS` days (older ones folded into spend per day and model,
for the costs view's whole-history chart) plus, for every transcript written since the start
of the day it was built, the byte offset where that day begins. Transcripts
are append-only, so a refresh rereads only the lines from that offset on and
reprices only the slots from that day on; a reopen with no transcript
changed since (same sizes) prices nothing at all.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from claude_rotater.settings import atomic_write_json

_logger = logging.getLogger(__name__)

# The ccusage release rota is tested against, fetched by bunx/npx when no
# ccusage is installed. An installed ccusage wins, whatever its version.
CCUSAGE_VERSION = "20.0.34"
RUNNER_TIMEOUT_S = 300.0  # a first npx/bunx run downloads the package

SLOT_S = 900  # 15 minutes: fine enough for switches, aligned to every tz offset
RANGE_DAYS = 30  # today and the 29 days before it
ARCHIVE_DAYS = 800  # days kept folded (per day and model) once they leave the range
CACHE_FILENAME = "costs.json"
CACHE_SCHEMA = 1

MISSING_HINT = "install ccusage or Node to see costs"

# What ccusage reads from a transcript line; everything else (message
# content, tool output) is dropped from the copy. A test checks ccusage
# prices the copy exactly like the original.
_TOP_KEYS = ("timestamp", "requestId", "costUSD", "isApiErrorMessage", "type", "version")
_MESSAGE_KEYS = ("id", "model", "usage")
_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')


class CostError(Exception):
    """ccusage failed or printed something rota cannot read."""


# -- runner -------------------------------------------------------------------


def find_runner(which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """The command prefix that runs ccusage, or None when there is no way to.

    An installed ``ccusage`` first; else the pinned release through ``bunx``
    or ``npx`` (which fetch it once into their own cache).
    """
    if path := which("ccusage"):
        return [path]
    if path := which("bunx"):
        return [path, f"ccusage@{CCUSAGE_VERSION}"]
    if path := which("npx"):
        return [path, "-y", f"ccusage@{CCUSAGE_VERSION}"]
    return None


def run_ccusage(runner: list[str], config_dir: Path, *, timeout: float = RUNNER_TIMEOUT_S) -> dict:
    """``ccusage claude session --json --breakdown`` over ``config_dir``.

    Offline pricing (ccusage's bundled table): no network call per refresh.
    An installed ccusage older than v20 has no ``claude`` subcommand, so a
    failure retries the plain ``session`` report, Claude-only before v20.
    """
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config_dir), NO_COLOR="1")
    args = ["session", "--json", "--breakdown", "--offline"]
    last = ""
    for prefix in (["claude"], []):
        try:
            proc = subprocess.run(
                [*runner, *prefix, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=config_dir,  # never a project dir with its own ccusage config
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CostError(f"ccusage did not run: {e}") from e
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout)
            except ValueError as e:
                raise CostError("ccusage printed no JSON") from e
            if isinstance(payload, dict):
                return payload
            raise CostError("ccusage printed unexpected JSON")
        last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        last = last[0]
    raise CostError(f"ccusage failed: {last[:200]}")


# -- priced slots -------------------------------------------------------------


# ccusage's four token counts per model (inputTokens, cacheCreationTokens,
# cacheReadTokens, outputTokens), in the order the costs view shows them
TOKEN_KINDS = ("input", "cache write", "cache read", "output")
Tokens = tuple[int, int, int, int]  # one count per TOKEN_KINDS entry


@dataclass(frozen=True)
class Row:
    """One model's spend in one 15-minute slot of one transcript source."""

    source: str  # Source.key
    slot: int  # epoch of the slot start, a multiple of SLOT_S
    model: str  # ccusage's model name, e.g. claude-opus-4-7
    cost: float  # USD, API-equivalent
    input: int
    output: int
    cache_write: int
    cache_read: int

    @property
    def tokens(self) -> "Tokens":
        """The four counts in :data:`TOKEN_KINDS` order."""
        return (self.input, self.cache_write, self.cache_read, self.output)

    def to_json(self) -> list:
        return [self.source, self.slot, self.model, round(self.cost, 6),
                self.input, self.output, self.cache_write, self.cache_read]

    @classmethod
    def from_json(cls, item: object) -> "Row | None":
        if not (isinstance(item, list) and len(item) == 8):
            return None
        source, slot, model, cost, *tokens = item
        if not (isinstance(source, str) and isinstance(model, str) and _num(slot) and _num(cost)
                and all(_num(t) for t in tokens)):
            return None
        return cls(source, int(slot), model, float(cost), *(int(t) for t in tokens))


def _num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_sessions(payload: dict, sources: Iterable[str]) -> list[Row]:
    """The rows of a ``ccusage ... session --json --breakdown`` report.

    Only sessions this module laid out (``<source>/<slot>``) and only
    ``claude-*`` models count: anything else (another agent's data, the
    ``<synthetic>`` placeholder model) is not Claude Code spend.
    """
    keys = set(sources)
    rows: list[Row] = []
    sessions = payload.get("sessions")
    for session in sessions if isinstance(sessions, list) else []:
        if not isinstance(session, dict):
            continue
        source = os.path.basename(str(session.get("projectPath", "")).rstrip("/"))
        slot = str(session.get("sessionId", ""))
        if source not in keys or not slot.isdigit():
            continue
        breakdowns = session.get("modelBreakdowns")
        for b in breakdowns if isinstance(breakdowns, list) else []:
            if not isinstance(b, dict):
                continue
            model = str(b.get("modelName", ""))
            if not model.startswith("claude-"):
                continue
            rows.append(Row(
                source, int(slot), model, _float(b.get("cost")),
                _int(b.get("inputTokens")), _int(b.get("outputTokens")),
                _int(b.get("cacheCreationTokens")), _int(b.get("cacheReadTokens")),
            ))
    return rows


def _float(value: object) -> float:
    return float(value) if _num(value) else 0.0


def _int(value: object) -> int:
    return int(value) if _num(value) else 0


# -- transcript sources --------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """A ``projects/`` dir of transcripts, and whose they are.

    ``account`` is set for a ``rota run`` session profile, whose transcripts
    are that account's by construction; None means "whoever was on duty",
    decided per slot from the switch history.
    """

    key: str  # short stable name, the slice's project dir
    path: str  # resolved projects dir
    account: str | None = None


def transcript_sources(
    backup_dir: Path, config_homes: Iterable[str | Path]
) -> list[Source]:
    """Every transcript dir of this rota profile, each once.

    ``config_homes`` are the Claude config dirs on duty accounts run in
    (the profile's ``CLAUDE_CONFIG_DIR``, plus any the statusline feed saw);
    session profiles under ``<data>/sessions/<num>-<email>`` add their own.
    Dirs are deduplicated by resolved path, so a session that shares the
    main history (a symlinked ``projects/``) is read once, as the main one.
    """
    seen: set[str] = set()
    out: list[Source] = []

    def add(projects: Path, account: str | None) -> None:
        try:
            real = os.path.realpath(projects)
        except (OSError, ValueError):
            return
        if real in seen or not os.path.isdir(real):
            return
        seen.add(real)
        out.append(Source(f"s{len(out)}", real, account))

    for home in config_homes:
        add(Path(home) / "projects", None)
    sessions = backup_dir / "sessions"
    try:
        entries = sorted(os.scandir(sessions), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        number = entry.name.split("-", 1)[0]
        if entry.is_dir() and number.isdigit():
            add(Path(entry.path) / "projects", number)
    return out


def sources_from_paths(backup_dir: Path, paths: Iterable[str]) -> list[Source]:
    """The sources a cache was built from (its ``sources``, in key order),
    each session profile's again credited to its account."""
    sessions = os.path.realpath(backup_dir / "sessions") + os.sep
    out = []
    for i, path in enumerate(paths):
        account = None
        if path.startswith(sessions):
            number = path[len(sessions):].split(os.sep, 1)[0].split("-", 1)[0]
            account = number if number.isdigit() else None
        out.append(Source(f"s{i}", path, account))
    return out


def feed_config_dirs(backup_dir: Path) -> list[str]:
    """Config dirs the statusline feed of this data dir has reported.

    A profile may run Claude Code from more than one config dir; the feed
    knows which. Session profiles are skipped (:func:`transcript_sources`
    adds those with their account).
    """
    feed = backup_dir / "cache" / "feed"
    found: dict[str, None] = {}
    for name in ("statusline.jsonl.1", "statusline.jsonl"):
        try:
            data = (feed / name).read_bytes()
        except OSError:
            continue
        for match in re.finditer(rb'"config_dir"\s*:\s*"((?:[^"\\]|\\.)*)"', data):
            try:
                value = json.loads(b'"' + match.group(1) + b'"')
            except ValueError:
                continue
            found[value] = None
    sessions = os.path.realpath(backup_dir / "sessions")
    return [d for d in found if not os.path.realpath(d).startswith(sessions + os.sep)]


# -- slicing -------------------------------------------------------------------


def parse_ts(raw: str) -> float | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _trim(record: dict) -> dict:
    out = {k: record[k] for k in _TOP_KEYS if k in record}
    message = record.get("message")
    if isinstance(message, dict):
        out["message"] = {k: message[k] for k in _MESSAGE_KEYS if k in message}
    return out


def _jsonl_files(root: str, since: float) -> Iterable[tuple[str, os.stat_result]]:
    """Every ``*.jsonl`` under ``root`` modified at or after ``since``.

    A file last written before ``since`` holds no line stamped after it.
    """
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime >= since:
                yield path, st


Offsets = dict[str, list[int]]  # path -> [inode, offset, size seen]


@dataclass
class SliceResult:
    offsets: Offsets = field(default_factory=dict)
    records: int = 0


def slice_transcripts(
    sources: list[Source],
    since: float,
    next_boundary: float,
    dst: Path,
    offsets: Offsets | None = None,
) -> SliceResult:
    """Copy the usage records stamped at or after ``since`` into ``dst``.

    Reading starts at a file's stored offset when it is still valid (same
    inode, not shrunk), else at 0. The result maps each file read to where
    the first record stamped at or after ``next_boundary`` starts (or the
    end of its last complete line), so the next refresh can begin there.
    """
    offsets = offsets or {}
    result = SliceResult()
    buffers: dict[Path, list[bytes]] = {}
    buffered = 0

    def flush() -> None:
        nonlocal buffered
        for path, lines in buffers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.writelines(lines)
        buffers.clear()
        buffered = 0

    for source in sources:
        out_dir = dst / "projects" / source.key
        for path, st in _jsonl_files(source.path, since):
            start = 0
            prior = offsets.get(path)
            if prior and prior[0] == st.st_ino and prior[1] <= st.st_size:
                start = prior[1]
            resume: int | None = None
            pos = start
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    for line in fh:
                        if not line.endswith(b"\n"):
                            break  # being written: next time
                        line_at, pos = pos, pos + len(line)
                        if b'"usage"' not in line:
                            continue
                        m = _TS_RE.search(line)
                        ts = parse_ts(m.group(1).decode("ascii", "replace")) if m else None
                        if ts is None:
                            continue
                        if resume is None and ts >= next_boundary:
                            resume = line_at
                        if ts < since:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        slot = int(ts // SLOT_S) * SLOT_S
                        # Compact, as Claude Code writes it: ccusage skips lines
                        # spelled with spaces (`"type": "assistant"`).
                        data = json.dumps(_trim(record), separators=(",", ":")).encode() + b"\n"
                        buffers.setdefault(out_dir / f"{slot}.jsonl", []).append(data)
                        buffered += len(data)
                        result.records += 1
                        if buffered > 1024 * 1024:
                            flush()
            except OSError as e:
                _logger.warning(f"costs: cannot read a transcript: {e!r}")
                continue
            result.offsets[path] = [st.st_ino, pos if resume is None else resume, st.st_size]
    flush()
    return result


# -- cache ---------------------------------------------------------------------


@dataclass
class CostCache:
    boundary: float  # rows from here on are repriced on the next refresh
    sources: list[str]  # Source.path, in key order
    rows: list[Row]
    offsets: Offsets
    built_at: float
    took_s: float = 0.0  # the last refresh's wall time
    # Spend per local day and model from before the priced range: the
    # slots that aged out, folded so the costs view can show all of it.
    archive: dict[float, dict[str, float]] = field(default_factory=dict)
    # the same days' token counts per model (Tokens), kept apart so an older
    # rota that rewrites the cache keeps the dollars it knows
    archive_tokens: dict[float, dict[str, Tokens]] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "schemaVersion": CACHE_SCHEMA,
            "boundary": self.boundary,
            "sources": self.sources,
            "builtAt": self.built_at,
            "tookS": round(self.took_s, 3),
            "offsets": self.offsets,
            "rows": [row.to_json() for row in self.rows],
            "archive": {str(int(day)): {m: round(usd, 4) for m, usd in models.items()}
                        for day, models in sorted(self.archive.items())},
            "archiveTokens": {str(int(day)): {m: list(t) for m, t in models.items()}
                              for day, models in sorted(self.archive_tokens.items())},
        }

    @classmethod
    def from_json(cls, raw: object) -> "CostCache | None":
        if not isinstance(raw, dict) or raw.get("schemaVersion") != CACHE_SCHEMA:
            return None
        boundary, built, sources = raw.get("boundary"), raw.get("builtAt"), raw.get("sources")
        offsets, rows = raw.get("offsets"), raw.get("rows")
        if not (_num(boundary) and _num(built) and isinstance(sources, list)
                and isinstance(offsets, dict) and isinstance(rows, list)):
            return None
        return cls(
            boundary=float(boundary),
            sources=[s for s in sources if isinstance(s, str)],
            rows=[r for r in map(Row.from_json, rows) if r is not None],
            offsets={
                k: [int(x) for x in v] for k, v in offsets.items()
                if isinstance(v, list) and len(v) == 3 and all(_num(x) for x in v)
            },
            built_at=float(built),
            took_s=float(raw.get("tookS") or 0.0) if _num(raw.get("tookS")) else 0.0,
            archive=_archive_from_json(raw.get("archive")),
            archive_tokens=_archive_tokens_from_json(raw.get("archiveTokens")),
        )


def _archive_from_json(raw: object) -> dict[float, dict[str, float]]:
    out: dict[float, dict[str, float]] = {}
    for day, models in (raw.items() if isinstance(raw, dict) else ()):
        if not (isinstance(day, str) and day.isdigit() and isinstance(models, dict)):
            continue
        kept = {m: float(v) for m, v in models.items() if isinstance(m, str) and _num(v)}
        if kept:
            out[float(day)] = kept
    return out


def _archive_tokens_from_json(raw: object) -> dict[float, dict[str, Tokens]]:
    out: dict[float, dict[str, Tokens]] = {}
    for day, models in (raw.items() if isinstance(raw, dict) else ()):
        if not (isinstance(day, str) and day.isdigit() and isinstance(models, dict)):
            continue
        kept = {m: tuple(int(x) for x in t) for m, t in models.items()
                if isinstance(m, str) and isinstance(t, list) and len(t) == len(TOKEN_KINDS)
                and all(_num(x) for x in t)}
        if kept:
            out[float(day)] = kept
    return out


def fold_tokens(archive: dict[float, dict[str, Tokens]],
                rows: Iterable[Row]) -> dict[float, dict[str, Tokens]]:
    """As :func:`fold`, for the token counts."""
    out = {day: dict(models) for day, models in archive.items()}
    for r in rows:
        day = out.setdefault(day_start(r.slot), {})
        before = day.get(r.model, (0, 0, 0, 0))
        day[r.model] = tuple(a + b for a, b in zip(before, r.tokens))
    for day in sorted(out)[:-ARCHIVE_DAYS]:
        del out[day]
    return out


def fold(archive: dict[float, dict[str, float]], rows: Iterable[Row]) -> dict[float, dict[str, float]]:
    """``archive`` plus ``rows`` summed per local day and model, the oldest
    days past :data:`ARCHIVE_DAYS` dropped."""
    out = {day: dict(models) for day, models in archive.items()}
    for r in rows:
        day = out.setdefault(day_start(r.slot), {})
        day[r.model] = day.get(r.model, 0.0) + r.cost
    for day in sorted(out)[:-ARCHIVE_DAYS]:
        del out[day]
    return out


def cache_path(backup_dir: Path) -> Path:
    return backup_dir / "cache" / CACHE_FILENAME


def load_cache(backup_dir: Path) -> CostCache | None:
    try:
        raw = json.loads(cache_path(backup_dir).read_text())
    except (OSError, ValueError):
        return None
    return CostCache.from_json(raw)


def save_cache(backup_dir: Path, cache: CostCache) -> None:
    try:
        atomic_write_json(cache_path(backup_dir), cache.to_json(), compact=True)
    except OSError as e:
        _logger.warning(f"costs: cache not written: {e!r}")


def day_start(t: float, days_back: int = 0) -> float:
    """Local midnight of the day ``t`` falls on, ``days_back`` days earlier."""
    d = datetime.fromtimestamp(t).date() - timedelta(days=days_back)
    return datetime(d.year, d.month, d.day).timestamp()


def range_start(now: float) -> float:
    return day_start(now, RANGE_DAYS - 1)


def is_current(cache: CostCache | None, sources: list[Source], now: float) -> bool:
    """Whether ``cache`` already holds everything: same sources, built today,
    and no transcript written since it was built (same sizes)."""
    if cache is None or cache.sources != [s.path for s in sources]:
        return False
    if cache.boundary != day_start(now):
        return False
    for source in sources:
        for path, st in _jsonl_files(source.path, cache.boundary):
            known = cache.offsets.get(path)
            if known is None or known[0] != st.st_ino or known[2] != st.st_size:
                return False
    return True


def refresh(
    cache: CostCache | None,
    sources: list[Source],
    runner: list[str],
    now: float,
    *,
    run: Callable[[list[str], Path], dict] = run_ccusage,
) -> CostCache:
    """Reprice what changed since ``cache`` was built (everything without one).

    Slots before the cache's boundary are kept as priced; the ones after are
    rebuilt from the transcripts' stored offsets on. Slots older than
    :data:`RANGE_DAYS` days are folded into the per-day archive, which
    bounds the cache.
    """
    started = time.monotonic()
    lo = range_start(now)
    paths = [s.path for s in sources]
    aged = [r for r in cache.rows if r.slot < lo] if cache else []
    archive = fold(cache.archive, aged) if cache else {}
    archive_tokens = fold_tokens(cache.archive_tokens, aged) if cache else {}
    if cache is None or cache.sources != paths or cache.boundary < lo:
        since, offsets, kept = lo, {}, []
    else:
        since, offsets = cache.boundary, cache.offsets
        kept = [r for r in cache.rows if lo <= r.slot < since]
    boundary = day_start(now)
    with tempfile.TemporaryDirectory(prefix="rota-costs-") as tmp:
        dst = Path(tmp)
        (dst / "projects").mkdir()
        sliced = slice_transcripts(sources, since, boundary, dst, offsets)
        fresh = parse_sessions(run(runner, dst), [s.key for s in sources]) if sliced.records else []
    rows = kept + [r for r in fresh if r.slot >= since]
    return CostCache(
        boundary=boundary,
        sources=paths,
        rows=sorted(rows, key=lambda r: (r.slot, r.source, r.model)),
        offsets=sliced.offsets,
        built_at=now,
        took_s=time.monotonic() - started,
        archive=archive,
        archive_tokens=archive_tokens,
    )
