"""When a subscription account's plan next bills, estimated from its start.

Claude Code stores the org's ``subscriptionCreatedAt`` in each profile's
``.claude.json`` ``oauthAccount`` (from ``/api/oauth/profile``); rota keeps
a copy per account. No renewal date or billing interval is stored, so the
next bill is the next anniversary of the start date, assuming monthly
billing. Every date this module gives is therefore an estimate, shown with
a ``~``: a plan change or a billing anchor moved by support would not show.

A start on the 29th-31st bills on the last day of a shorter month and goes
back to its own day after (Stripe's anchor rule), so each anniversary is
clamped from the start day, never from the month before.
"""

from __future__ import annotations

import calendar
import json
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

MONTHLY = 1
ANNUAL = 12


def parse_started_at(value: object) -> float | None:
    """Epoch seconds of an ISO ``subscriptionCreatedAt``, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def started_at_from_config(config: object) -> float | None:
    """The subscription start in a parsed ``.claude.json``, or None."""
    if not isinstance(config, dict):
        return None
    account = config.get("oauthAccount")
    if not isinstance(account, dict):
        return None
    return parse_started_at(account.get("subscriptionCreatedAt"))


@lru_cache(maxsize=64)
def _started_at_cached(path: str, _mtime_ns: int, _size: int) -> float | None:
    try:
        return started_at_from_config(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def started_at_from_file(path: Path) -> float | None:
    """The subscription start stored in a config file, or None.

    Cached on the file's mtime and size: the snapshot asks on every pass,
    and a stored config can be hundreds of KB of JSON.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _started_at_cached(str(path), st.st_mtime_ns, st.st_size)


def _add_months(start: date, months: int) -> date:
    """``start`` moved ``months`` on, its day clamped to that month's last."""
    year, month0 = divmod(start.month - 1 + months, 12)
    year += start.year
    last = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(start.day, last))


def next_bill_date(start: date, today: date, period_months: int = MONTHLY) -> date:
    """The first anniversary of ``start`` on or after ``today``.

    The start day itself is the first charge, so the earliest renewal is one
    period after it, even when ``today`` is before ``start``.
    """
    if period_months < 1:
        raise ValueError("period_months must be at least 1")
    elapsed = (today.year - start.year) * 12 + today.month - start.month
    k = max(1, elapsed // period_months)
    while True:
        candidate = _add_months(start, k * period_months)
        if candidate >= today:
            return candidate
        k += 1


def bill_note(started_at: float | None, now: float,
              period_months: int = MONTHLY) -> str:
    """``bills ~Nov 14`` for a known start, else ``""``; dates are local."""
    if started_at is None:
        return ""
    start = datetime.fromtimestamp(started_at).date()
    today = datetime.fromtimestamp(now).date()
    nxt = next_bill_date(start, today, period_months)
    return f"bills ~{nxt.strftime('%b')} {nxt.day}"
