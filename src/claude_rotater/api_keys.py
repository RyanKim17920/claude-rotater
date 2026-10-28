"""What rota knows about each API-key account: out of credit, its budget,
and what it has spent.

Anthropic has no API for a key's remaining prepaid credit, so "out of
credit" is learnt the way Claude Code learns it: the API refuses the
request. Claude Code writes the refusal into the session transcript as an
assistant entry with ``"isApiErrorMessage": true``, its category in
``error`` and the API's words in the text, e.g. ::

    {"type": "assistant", "isApiErrorMessage": true, "error": "billing_error",
     "apiErrorStatus": 400, "message": {"model": "<synthetic>", "content":
     [{"type": "text", "text": "API Error: 400 ... Your credit balance is too
     low to access the Anthropic API. ..."}]}, "timestamp": "..."}

A spend limit set on the Console org or workspace reads "You have reached
your specified API usage limits", and Anthropic's own tier cap is a 429
with ``enforced_spend_limit_reached``. :func:`scan_refusals` finds these
in the transcripts written since the key went on duty.

A budget is optional: a top-up ("$50 since <date>") or a monthly cap. Spend
against it is estimated by the costs code (on demand, in the TUI) and
recorded here with :func:`record_spend`, so the engine (which never loads
the costs code) can stop short of the hard refusal, ``reserve`` before it.

State lives in ``<data dir>/api-keys.json``. Engine-side and stdlib-only.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "api-keys.json"
DEFAULT_RESERVE_USD = 1.0
# account-first: how long a key stays on duty before a subscription with
# room takes over again, so a brief window of room cannot flap the
# profile between a key and a login (each flip rebuilds prompt caches).
RETURN_DWELL_S = 600.0
# A subscription counts as having room again with this much binding
# headroom left.
RETURN_HEADROOM_PCT = 5.0

SCAN_MAX_FILES = 40
SCAN_TAIL_BYTES = 64 * 1024
_NEEDLE = b'"isApiErrorMessage":true'

OUT_OF_CREDIT = "out of credit"
USAGE_LIMIT = "at its usage limit"
SPEND_LIMIT = "at its spend limit"
_TEXT_RULES = (
    (re.compile(r"credit balance is too low", re.I), OUT_OF_CREDIT),
    (re.compile(r"specified (?:workspace )?API usage limits", re.I), USAGE_LIMIT),
    (re.compile(r"enforced_spend_limit", re.I), SPEND_LIMIT),
)


@dataclass(frozen=True)
class Budget:
    """``kind`` "topup" (``amount`` since ``since``) or "monthly"."""

    kind: str
    amount: float
    since: float = 0.0

    def period_start(self, now: float) -> float:
        if self.kind == "monthly":
            local = datetime.fromtimestamp(now).astimezone()
            return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.since

    def label(self) -> str:
        if self.kind == "monthly":
            return f"${self.amount:,.0f}/month"
        day = datetime.fromtimestamp(self.since).strftime("%b %-d")
        return f"${self.amount:,.0f} since {day}"


@dataclass(frozen=True)
class KeyState:
    exhausted_at: float | None = None
    exhausted_reason: str = ""
    budget: Budget | None = None
    reserve: float = DEFAULT_RESERVE_USD
    spent_usd: float | None = None  # in the budget period (or this month)
    spent_since: float | None = None
    spent_at: float | None = None

    @property
    def exhausted(self) -> bool:
        return self.exhausted_at is not None

    def left(self, now: float) -> float | None:
        """Budget left, or None without a budget or a spend estimate for
        its current period."""
        if self.budget is None or self.spent_usd is None or self.spent_since is None:
            return None
        if abs(self.spent_since - self.budget.period_start(now)) > 1.0:
            return None  # the estimate is for another period
        return self.budget.amount - self.spent_usd

    def blocked(self, now: float) -> str:
        """Why the key must not take (or keep) duty, or ""."""
        if self.exhausted:
            return self.exhausted_reason or OUT_OF_CREDIT
        left = self.left(now)
        if left is not None and left <= self.reserve:
            return f"budget nearly spent (${max(left, 0):,.2f} left)"
        return ""


# -- state file -------------------------------------------------------------------


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILENAME


def _read_raw(data_dir: Path) -> dict:
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_raw(data_dir: Path, raw: dict) -> None:
    from claude_rotater.settings import atomic_write_json

    Path(data_dir).mkdir(parents=True, exist_ok=True)
    atomic_write_json(_path(data_dir), raw)


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_budget(raw) -> Budget | None:
    if not isinstance(raw, dict) or raw.get("kind") not in ("topup", "monthly"):
        return None
    amount = _num(raw.get("amount"))
    if amount is None or amount <= 0:
        return None
    return Budget(raw["kind"], amount, _num(raw.get("since")) or 0.0)


def load(data_dir: Path) -> dict[str, KeyState]:
    out: dict[str, KeyState] = {}
    for number, item in _read_raw(data_dir).items():
        if not isinstance(item, dict):
            continue
        reserve = _num(item.get("reserve"))
        reason = item.get("exhaustedReason")
        out[str(number)] = KeyState(
            exhausted_at=_num(item.get("exhaustedAt")),
            exhausted_reason=reason if isinstance(reason, str) else "",
            budget=_parse_budget(item.get("budget")),
            reserve=DEFAULT_RESERVE_USD if reserve is None else max(reserve, 0.0),
            spent_usd=_num(item.get("spentUsd")),
            spent_since=_num(item.get("spentSince")),
            spent_at=_num(item.get("spentAt")),
        )
    return out


def state_of(data_dir: Path, number: str) -> KeyState:
    return load(data_dir).get(str(number), KeyState())


def _update(data_dir: Path, number: str, **fields) -> None:
    raw = _read_raw(data_dir)
    item = raw.get(str(number))
    item = dict(item) if isinstance(item, dict) else {}
    for key, value in fields.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    if item:
        raw[str(number)] = item
    else:
        raw.pop(str(number), None)
    _write_raw(data_dir, raw)


def mark_exhausted(data_dir: Path, number: str, reason: str, at: float) -> None:
    _update(data_dir, number, exhaustedAt=at, exhaustedReason=reason)


def clear_exhausted(data_dir: Path, number: str) -> None:
    """A top-up, or the owner picking the key by hand: try it again."""
    _update(data_dir, number, exhaustedAt=None, exhaustedReason=None)


def set_budget(data_dir: Path, number: str, budget: Budget | None) -> None:
    """Set (None: clear) the key's budget. A new top-up also means the key
    has credit again."""
    value = None
    if budget is not None:
        value = {"kind": budget.kind, "amount": budget.amount}
        if budget.kind == "topup":
            value["since"] = budget.since
    _update(data_dir, number, budget=value, spentUsd=None, spentSince=None, spentAt=None)
    if budget is not None:
        clear_exhausted(data_dir, number)


def record_spend(data_dir: Path, number: str, usd: float, since: float, at: float) -> None:
    _update(data_dir, number, spentUsd=round(usd, 4), spentSince=since, spentAt=at)


def forget(data_dir: Path, number: str) -> None:
    raw = _read_raw(data_dir)
    if raw.pop(str(number), None) is not None:
        _write_raw(data_dir, raw)


def key_numbers(data_dir: Path) -> list[str]:
    """The profile's API-key account numbers, from its ``sequence.json``."""
    try:
        raw = json.loads((Path(data_dir) / "sequence.json").read_text(encoding="utf-8"))
        accounts = raw.get("accounts") or {}
        return sorted(
            str(n) for n, rec in accounts.items()
            if isinstance(rec, dict) and rec.get("kind") == "api_key"
        )
    except (OSError, ValueError, AttributeError):
        return []


def spend_period(state: KeyState, now: float) -> float:
    """Where the key's recorded spend counts from: its budget's period,
    else the start of this month."""
    return (state.budget or Budget("monthly", 1.0)).period_start(now)


def parse_budget(text: str, now: float | None = None) -> Budget | None:
    """``"50"`` / ``"$50"`` a top-up from now, ``"100/m"`` (or ``/mo``,
    ``/month``) a monthly cap; ``""`` clears. Raises ValueError otherwise."""
    value = text.strip().lower().replace(",", "")
    if not value:
        return None
    monthly = False
    for suffix in ("/month", "/mo", "/m"):
        if value.endswith(suffix):
            monthly, value = True, value[: -len(suffix)].strip()
            break
    value = value.removeprefix("$").strip()
    try:
        amount = float(value)
    except ValueError:
        raise ValueError(f"{text!r} is not an amount (50, $50 or 100/m)") from None
    if not amount > 0 or amount != amount or amount == float("inf"):
        raise ValueError("a budget is an amount above $0")
    if monthly:
        return Budget("monthly", amount)
    return Budget("topup", amount, time.time() if now is None else now)


# -- refusals in transcripts ---------------------------------------------------------


def _when(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def classify(entry: dict) -> str:
    """Why the API refused, if this transcript entry says it is a key's
    credit or spend limit; else ""."""
    if not isinstance(entry, dict) or entry.get("isApiErrorMessage") is not True:
        return ""
    text = ""
    message = entry.get("message")
    if isinstance(message, dict):
        for part in message.get("content") or ():
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text += part["text"]
    for pattern, reason in _TEXT_RULES:
        if pattern.search(text):
            return reason
    if entry.get("error") == "billing_error":
        return OUT_OF_CREDIT
    return ""


def _transcripts(config_dir: Path, since: float) -> list[tuple[float, str]]:
    found: list[tuple[float, str]] = []
    projects = Path(config_dir) / "projects"
    try:
        dirs = list(os.scandir(projects))
    except OSError:
        return []
    stack = [d.path for d in dirs if d.is_dir(follow_symlinks=False)]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for item in entries:
            try:
                if item.is_dir(follow_symlinks=False):
                    stack.append(item.path)
                elif item.name.endswith(".jsonl"):
                    mtime = item.stat().st_mtime
                    if mtime >= since:
                        found.append((mtime, item.path))
            except OSError:
                continue
    found.sort(reverse=True)
    return found[:SCAN_MAX_FILES]


def _tail(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - SCAN_TAIL_BYTES))
            return handle.read()
    except OSError:
        return b""


def scan_refusals(config_dir: Path, since: float) -> tuple[str, float] | None:
    """The newest credit or spend-limit refusal in the transcripts written
    since ``since``, as ``(reason, when)``; None if there is none. Reads
    only the tails of the most recently written transcripts."""
    newest: tuple[str, float] | None = None
    for _mtime, path in _transcripts(config_dir, since):
        for line in _tail(path).splitlines():
            if _NEEDLE not in line.replace(b" ", b""):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            reason = classify(entry)
            when = _when(entry.get("timestamp")) if reason else None
            if reason and when is not None and when >= since:
                if newest is None or when > newest[1]:
                    newest = (reason, when)
    return newest
