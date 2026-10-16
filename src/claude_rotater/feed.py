"""``cswap feed``: live usage from Claude Code's statusline, at no API cost.

Claude Code pipes a JSON document to its statusline command on every
refresh. Since 2.1 it carries ``rate_limits.five_hour`` and
``rate_limits.seven_day`` (``used_percentage``, ``resets_at`` in epoch
seconds), read from the rate-limit headers of the session's own API
responses. That is the active account's usage, current to the last request,
and it costs no request against the usage endpoint's ~30/hour budget.

``cswap feed`` turns each refresh into one compact JSON line appended to
``<backup>/cache/feed/statusline.jsonl``. The engine (TUI, ``cswap auto``)
watches that directory and ingests new lines
(:mod:`claude_rotater.feed_ingest`), so the active account no longer needs
polling while a session is running.

It runs on every statusline refresh, so it must be fast and must never break
the statusline: stdlib-only imports (``claude_rotater.paths`` is ~15 ms), one
``O_APPEND`` write, every error swallowed and exit status 0. The backup root
comes from ``paths.get_backup_root()``, looked up at call time, so a wrapper
that patches it (a second profile with its own backup dir) is honored.

Record shape (keys with no value are omitted)::

    {"t": 1760000000.123,                 # when cswap feed ran (epoch s)
     "session_id": "...",
     "config_dir": "/Users/me/.claude",   # the session's CLAUDE_CONFIG_DIR
     "five_hour": {"pct": 42.0, "resets_at": 1760003600.0},
     "seven_day": {"pct": 12.0, "resets_at": 1760400000.0},
     "model": "Opus", "cost": 1.23}
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass

FEED_DIRNAME = "feed"  # under <backup>/cache: throwaway, never blocks migration
FEED_FILENAME = "statusline.jsonl"
ROTATED_SUFFIX = ".1"

# Rotate once the file passes this size, keeping one older generation: a
# record is ~250 bytes, so this is ~1000 refreshes, far more than ingestion
# ever needs (only the newest record per account matters).
MAX_FEED_BYTES = 256 * 1024

# Statusline payloads are a few KB. Anything bigger is not one, and is
# neither parsed nor buffered beyond this.
MAX_INPUT_BYTES = 1024 * 1024

# What a reader takes from a file it has not seen before: the tail only.
INITIAL_TAIL_BYTES = 64 * 1024

WINDOW_KEYS = ("five_hour", "seven_day")
_MAX_TEXT = 128
_COPY_CHUNK = 64 * 1024


def feed_dir(backup_root) -> str:
    return os.path.join(os.fspath(backup_root), "cache", FEED_DIRNAME)


def feed_path(backup_root) -> str:
    return os.path.join(feed_dir(backup_root), FEED_FILENAME)


# -- parsing ------------------------------------------------------------------


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _epoch(value: object) -> float | None:
    """``resets_at`` as epoch seconds: a number (seconds, or milliseconds if
    implausibly large) or an ISO-8601 string."""
    number = _number(value)
    if number is not None:
        if number > 1e11:  # milliseconds
            number /= 1000.0
        return number if number > 0 else None
    if isinstance(value, str) and value:
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value[:_MAX_TEXT]
    return None


def _window(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pct = _number(raw.get("used_percentage"))
    if pct is None or pct < 0:
        return None
    window: dict = {"pct": min(pct, 100.0)}
    resets_at = _epoch(raw.get("resets_at"))
    if resets_at is not None:
        window["resets_at"] = resets_at
    return window


def _config_dir(payload: dict, environ) -> str:
    """The session's config home: the directory above ``projects/`` in its
    transcript path (Claude Code's own layout, unaffected by any wrapper that
    rewrites this process's environment), else ``CLAUDE_CONFIG_DIR``, else
    the default ``~/.claude``."""
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        project_dir = os.path.dirname(transcript)
        projects = os.path.dirname(project_dir)
        if os.path.basename(projects) == "projects":
            return os.path.dirname(projects)
    env = environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".claude")


def parse_payload(raw: bytes | str, now: float, environ=os.environ) -> dict | None:
    """One feed record from a statusline payload, or None when it carries no
    usable rate-limit window (API-key auth, before the first response,
    garbage). Never raises."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    record: dict = {"t": round(now, 3)}
    for key in WINDOW_KEYS:
        window = _window(limits.get(key))
        if window is not None:
            record[key] = window
    if not any(key in record for key in WINDOW_KEYS):
        return None
    session_id = _text(payload.get("session_id"))
    if session_id is not None:
        record["session_id"] = session_id
    record["config_dir"] = _config_dir(payload, environ)
    model = payload.get("model")
    if isinstance(model, dict):
        name = _text(model.get("display_name")) or _text(model.get("id"))
        if name is not None:
            record["model"] = name
    cost = payload.get("cost")
    if isinstance(cost, dict):
        total = _number(cost.get("total_cost_usd"))
        if total is not None:
            record["cost"] = total
    return record


def valid_record(record: object) -> bool:
    """Whether a line read back from the feed has the shape ingestion relies
    on (the file is user-writable, so it is checked, not trusted)."""
    if not isinstance(record, dict) or _number(record.get("t")) is None:
        return False
    seen = False
    for key in WINDOW_KEYS:
        window = record.get(key)
        if window is None:
            continue
        if not isinstance(window, dict) or _number(window.get("pct")) is None:
            return False
        if "resets_at" in window and _number(window["resets_at"]) is None:
            return False
        seen = True
    config_dir = record.get("config_dir")
    if config_dir is not None and not isinstance(config_dir, str):
        return False
    return seen


# -- writing ------------------------------------------------------------------


def append_record(path: str, record: dict, max_bytes: int = MAX_FEED_BYTES) -> None:
    """Append one line with a single ``O_APPEND`` write (atomic against other
    writers for a line this short), rotating the file to ``.1`` first once it
    has outgrown ``max_bytes``. Two writers racing a rotation can lose the
    older generation's lines, which costs nothing: readers want the newest."""
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.fstat(fd).st_size >= max_bytes:
            os.close(fd)
            fd = -1
            os.replace(path, path + ROTATED_SUFFIX)
            fd = os.open(path, flags, 0o600)
        os.write(fd, line)
    finally:
        if fd >= 0:
            os.close(fd)


# -- reading ------------------------------------------------------------------


@dataclass(frozen=True)
class FeedCursor:
    """Where a reader stopped: the file's identity and the byte after the
    last complete line consumed."""

    ident: tuple[int, int] | None = None  # (st_dev, st_ino)
    offset: int = 0


def read_new(path: str, cursor: FeedCursor | None) -> tuple[list[dict], FeedCursor]:
    """Complete, valid records appended since ``cursor``, oldest first.

    A new file identity (rotation, recreation) or a shrunken file restarts at
    the top; a first read (``cursor`` None) takes only the last
    ``INITIAL_TAIL_BYTES``. A trailing partial line is left for the next
    read. Never raises: an unreadable file reads as nothing new."""
    try:
        with open(path, "rb") as handle:
            st = os.fstat(handle.fileno())
            ident = (st.st_dev, st.st_ino)
            size = st.st_size
            if cursor is None:
                start = max(0, size - INITIAL_TAIL_BYTES)
            elif cursor.ident != ident or size < cursor.offset:
                start = 0
            else:
                start = cursor.offset
            # Never buffer more than the cap, however far behind we are.
            skip_partial = start > 0 and (cursor is None or start != cursor.offset)
            if size - start > MAX_INPUT_BYTES:
                start = size - MAX_INPUT_BYTES
                skip_partial = True
            handle.seek(start)
            data = handle.read(size - start)
    except OSError:
        return [], cursor or FeedCursor()
    end = data.rfind(b"\n")
    if end < 0:
        return [], FeedCursor(ident, start)
    body = data[: end + 1]
    if skip_partial:
        # Started mid-file: the first line may be a fragment.
        first = body.find(b"\n")
        body = body[first + 1 :]
    records: list[dict] = []
    for line in body.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if valid_record(record):
            records.append(record)
    return records, FeedCursor(ident, start + end + 1)


# -- command --------------------------------------------------------------------

_USAGE = """usage: cswap feed [--tee]

Record the usage Claude Code reports to its statusline. Reads the statusline
JSON on stdin and appends a compact record to the feed the auto-switcher
watches; never fails (exit 0). --tee also copies stdin to stdout so it can sit
in the middle of a pipe. See the README section on live usage.
"""


def _drain(stdin, stdout) -> None:
    while True:
        chunk = stdin.read(_COPY_CHUNK)
        if not chunk:
            return
        if stdout is not None:
            stdout.write(chunk)


def main(argv: list[str], stdin=None, stdout=None, *, now: float | None = None) -> int:
    """Entry point for ``cswap feed``. Always returns 0."""
    if "-h" in argv or "--help" in argv:
        try:
            sys.stdout.write(_USAGE)
        except Exception:
            pass
        return 0
    tee = "--tee" in argv
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else (sys.stdout.buffer if tee else None)
    data = b""
    try:
        data = stdin.read(MAX_INPUT_BYTES + 1)
        if tee and data:
            stdout.write(data)
        oversized = len(data) > MAX_INPUT_BYTES
        if oversized:
            # Not a statusline payload. Keep the pipe flowing, record nothing.
            _drain(stdin, stdout if tee else None)
        if tee:
            stdout.flush()
        if oversized:
            return 0
    except Exception:
        return 0
    try:
        import time

        record = parse_payload(data, time.time() if now is None else now)
        if record is None:
            return 0
        from claude_rotater import paths

        backup_root = paths.get_backup_root()
        if not os.path.isdir(backup_root):
            return 0  # cswap is not set up for this profile: nothing to feed
        append_record(feed_path(backup_root), record)
    except Exception:
        pass
    return 0
