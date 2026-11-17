"""Accounts the provider refuses to serve: an organization that turned off
subscription access for the CLI, a payment past due, a suspension.

rota's own usage polls can keep working for a while after this happens; it
is the CLI's sessions that stop. The CLI writes the refusal into the session
transcript as an API error entry, e.g. (observed, Nov 2026)::

    {"type": "assistant", "isApiErrorMessage": true,
     "error": "oauth_org_not_allowed", "apiErrorStatus": 403,
     "apiErrorType": "permission_error",
     "apiErrorCode": "oauth_not_allowed_for_organization",
     "message": {"model": "<synthetic>", "content": [{"type": "text",
     "text": "Your organization has disabled ... subscription access for
     <the CLI> · Use an API key instead, or ask your admin to enable
     access"}]}}

and, for a lapsed payment, the same code with "... usually because a
payment is past due". :func:`classify` reads those (and only those: an
expired login, a rate limit, an overload or a network error is never a
block). The usage endpoint answering 403 again and again is a second,
weaker signal (see :data:`USAGE_STRIKES`).

A block belongs to the account, not the profile: it is kept by identity
(email, organization) in the shared cache, so every profile holding the
account sees it. It ends when the account is seen working again (a
successful turn, or a usage reading at the daily recheck well after the
block), or when the user says so (picking the account by hand, or ``d``).

Engine-side and stdlib-only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from claude_rotater import api_keys
from claude_rotater.exceptions import LockError
from claude_rotater.locking import FileLock
from claude_rotater.paths import get_shared_cache_dir

_logger = logging.getLogger(__name__)

STORE_FILENAME = "provider_blocks.json"
SCHEMA_VERSION = 1

# A blocked account's usage is polled once a day, not every few minutes:
# enough to notice the provider lifting the block, without spending
# requests on an account that cannot work.
RECHECK_S = 24 * 3600.0
# A usage reading clears a block only this long after it: the endpoint
# started refusing ~10 minutes after the sessions did in the observed case,
# so a reading straight after a refusal says nothing.
RECOVERY_MIN_AGE_S = 3600.0
# The usage endpoint's 403s alone declare a block only when repeated: this
# many in a row (no success between), spread over at least USAGE_SPAN_S.
USAGE_STRIKES = 3
USAGE_SPAN_S = 1800.0

ORG_DISABLED = "org turned off subscription access"
PAST_DUE = "payment past due"
SUSPENDED = "account suspended"
REFUSED = "refused by the provider (403)"
USAGE_REFUSED = "usage endpoint refuses it (403)"
# A turn the API answered: the account works.
WORKING = "working"

SOURCE_TRANSCRIPT = "transcript"
SOURCE_USAGE = "usage"

_REFUSAL_ERRORS = frozenset({"oauth_org_not_allowed"})
_REFUSAL_CODES = frozenset({"oauth_not_allowed_for_organization"})
_TEXT_RULES = (
    (re.compile(r"organization has disabled", re.I), ORG_DISABLED),
    (re.compile(r"payment is past due", re.I), PAST_DUE),
    (re.compile(r"\b(?:account|organization)\b[^.·]{0,40}\b(?:suspended|banned|deactivated)\b",
                re.I), SUSPENDED),
)
_ASSISTANT = b'"type":"assistant"'

Identity = tuple[str, str]  # (email, organization uuid)


@dataclass(frozen=True)
class Block:
    reason: str
    source: str
    at: float


def _text(entry: dict) -> str:
    text = ""
    message = entry.get("message")
    if isinstance(message, dict):
        for part in message.get("content") or ():
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text += part["text"]
    return text


def classify(entry: object) -> str:
    """What a transcript entry says about the account it ran on: a block
    reason, :data:`WORKING` for a turn the API answered, else ""."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return ""
    if entry.get("isApiErrorMessage") is not True:
        message = entry.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        return WORKING if isinstance(model, str) and model != "<synthetic>" else ""
    known = (
        entry.get("error") in _REFUSAL_ERRORS
        or entry.get("apiErrorCode") in _REFUSAL_CODES
    )
    forbidden = (
        entry.get("apiErrorStatus") == 403
        and entry.get("apiErrorType") == "permission_error"
    )
    if not known and not forbidden:
        return ""
    text = _text(entry)
    for pattern, reason in _TEXT_RULES:
        if pattern.search(text):
            return reason
    # A bare 403 permission error is also what a token without a scope
    # gets: only the provider's own refusal code makes it a block.
    return REFUSED if known else ""


def scan(config_dir: Path, since: float) -> tuple[str, float] | None:
    """The newest verdict (a block reason or :data:`WORKING`) in this
    profile's transcripts written since ``since``; None when there is none."""
    return api_keys.newest_in_transcripts(
        config_dir, since, classify, needles=(api_keys._NEEDLE, _ASSISTANT)
    )


def usage_refusal(strikes: int, first_at: float | None, now: float) -> bool:
    """Whether a run of usage-endpoint 403s is long enough to be a block."""
    return strikes >= USAGE_STRIKES and first_at is not None and now - first_at >= USAGE_SPAN_S


# -- the store -----------------------------------------------------------------


def _key(identity: Identity) -> str:
    return f"{identity[0]}|{identity[1] or ''}"


def _store_path(path: Path | None) -> Path:
    return path or get_shared_cache_dir() / STORE_FILENAME


def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    return accounts if isinstance(accounts, dict) else {}


def load(path: Path | None = None) -> dict[str, Block]:
    """Every block, keyed by ``email|orgUuid``."""
    out: dict[str, Block] = {}
    for key, row in _read(_store_path(path)).items():
        if not isinstance(row, dict):
            continue
        try:
            out[key] = Block(str(row["reason"]), str(row.get("source", "")), float(row["at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def block_of(identity: Identity, blocks: dict[str, Block] | None = None,
             path: Path | None = None) -> Block | None:
    return (load(path) if blocks is None else blocks).get(_key(identity))


def _update(path: Path | None, change) -> bool:
    path = _store_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_suffix(".lock")):
            accounts = _read(path)
            if not change(accounts):
                return False
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"schemaVersion": SCHEMA_VERSION, "accounts": accounts}, indent=1, sort_keys=True
            ))
            tmp.replace(path)
            return True
    except (OSError, LockError) as e:
        _logger.warning(f"could not keep provider blocks: {e!r}")
        return False


def mark(identity: Identity, reason: str, source: str, at: float,
         path: Path | None = None) -> bool:
    """Record that the provider refuses ``identity``. True when this is
    news (no block before, or a different reason from the transcripts)."""
    def change(accounts: dict) -> bool:
        row = accounts.get(_key(identity))
        if isinstance(row, dict) and (row.get("reason") == reason or source == SOURCE_USAGE):
            return False  # the weaker signal never rewrites a known block
        accounts[_key(identity)] = {"reason": reason, "source": source, "at": at}
        return True

    return _update(path, change)


def clear(identity: Identity, path: Path | None = None) -> bool:
    """Lift a block. True when there was one."""
    return _update(path, lambda accounts: accounts.pop(_key(identity), None) is not None)
