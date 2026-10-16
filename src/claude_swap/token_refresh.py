"""Proactive token refresh for stored accounts nobody is using (upstream #415).

An inactive account's access token is only renewed when something happens to
need it (a usage poll, a switch). An account left alone long enough is found
expired at the worst moment, and a refresh token unused for long enough can
lapse. This keeps every idle account warm on its own timer, independent of
usage polling: each one is refreshed ``LEAD_S`` before its ``expiresAt``.

Rules:

- Never the active account, and never one held by a live ``cswap run``
  session. Refresh tokens rotate: consuming one retires the copy Claude Code
  is holding, which would log that process out. Both are re-checked right
  before each refresh, not only when the plan was made.
- Every refresh goes through ``switcher.consume_backup_grant``, the gate that
  serializes grant consumption per slot (re-read, POST, fingerprint CAS), so
  this can race the auto engine, the collector or another cswap safely.
- ``invalid_grant`` / ``no_refresh_token`` mean the login is dead: the strike
  is recorded in the usage store (the row shows "re-login needed") and that
  credential is never retried. A new login replaces the credential, which
  changes its fingerprint and makes the account eligible again.
- Event driven: the loop sleeps until the earliest due time. The sleep is
  capped at ``MAX_SLEEP_S`` because the monotonic clock behind
  ``Event.wait`` stops while a laptop sleeps, and a refresh due during the
  nap must not wait out the rest of the original timeout after it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator

from claude_swap import oauth
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.usage_store import FetchRecord

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("claude-swap")

LEAD_S = 30 * 60.0  # refresh this long before the access token expires
RETRY_S = 5 * 60.0  # after a transient failure (network, locked keychain)
BUSY_RETRY_S = 60.0  # another gate is consuming this slot right now
HELD_RECHECK_S = 10 * 60.0  # a live `cswap run` session holds the slot
MAX_SLEEP_S = 15 * 60.0
_NEVER = float("inf")
DEAD_ERRORS = frozenset({"invalid_grant", "no_refresh_token"})


@dataclass(frozen=True)
class Candidate:
    number: str
    email: str
    org_uuid: str
    creds: str


@dataclass(frozen=True)
class RefreshEvent:
    """One thing the refresher did. ``kind``: refreshed | dead | failed | held."""

    kind: str
    number: str
    email: str
    detail: str = ""

    def to_json(self, ts: float) -> dict:
        """One ``cswap auto --json`` line: ``event`` is ``token-<kind>``."""
        return {
            "schemaVersion": SCHEMA_VERSION,
            "event": f"token-{self.kind}",
            "ts": ts,
            "account": self.number,
            "email": self.email,
            "detail": self.detail,
        }

    def human(self) -> str:
        who = f"account {self.number} ({self.email})"
        if self.kind == "refreshed":
            return f"refreshed {who}'s token ahead of expiry"
        if self.kind == "dead":
            return f"{who} needs a re-login: its refresh token is dead (press l in cswap)"
        if self.kind == "held":
            return f"left {who} alone: a cswap run session is using it"
        return f"could not refresh {who} ({self.detail}); will retry"


def due_at(creds: str, *, lead_s: float = LEAD_S, now: float) -> float | None:
    """When this credential should be refreshed, or None when it cannot be.

    None for anything without a refresh token or an ``expiresAt``, and for a
    login whose ``refreshTokenExpiresAt`` has passed (only a new login helps).
    """
    data = oauth.extract_oauth_data(creds)
    if not data or not data.get("refreshToken"):
        return None
    expires = data.get("expiresAt")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    rt_expires = data.get("refreshTokenExpiresAt")
    if (
        isinstance(rt_expires, (int, float))
        and not isinstance(rt_expires, bool)
        and rt_expires > 0
        and rt_expires / 1000 <= now
    ):
        return None
    return expires / 1000 - lead_s


class TokenRefresher:
    """Keeps idle accounts' tokens fresh. ``run_loop`` in a thread; ``stop``
    and ``wake`` from any thread."""

    def __init__(
        self,
        switcher: "ClaudeAccountSwitcher",
        *,
        on_event: Callable[[RefreshEvent], None] = lambda _e: None,
        clock: Callable[[], float] = time.time,
        lead_s: float = LEAD_S,
    ) -> None:
        self.switcher = switcher
        self.on_event = on_event
        self.clock = clock
        self.lead_s = lead_s
        self._stopping = False
        self._wake = threading.Event()
        self._retry_at: dict[str, float] = {}
        self._dead_fp: dict[str, str | None] = {}

    # -- control -----------------------------------------------------------

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def run_loop(self) -> int:
        while not self._stopping:
            try:
                next_at = self.tick()
            except Exception:  # noqa: BLE001 -- a bad pass must not end the loop
                _logger.warning("token refresh pass failed", exc_info=True)
                next_at = self.clock() + RETRY_S
            timeout = min(MAX_SLEEP_S, max(1.0, next_at - self.clock()))
            self._wake.wait(timeout)
            self._wake.clear()
        return 0

    # -- one pass ------------------------------------------------------------

    def candidates(self) -> Iterator[Candidate]:
        """Every stored OAuth account except the active one."""
        active = self.switcher.current_account_number()
        for num, email, _org_name, org_uuid, is_active, creds, _alias in (
            self.switcher._build_accounts_info()
        ):
            number = str(num)
            if is_active or number == active or not creds:
                continue
            if self.switcher._account_kind(number) != "oauth":
                continue
            yield Candidate(number, email, org_uuid or "", creds)

    def _blocked(self, cand: Candidate) -> str | None:
        """Why this account must not be refreshed right now, or None."""
        if self.switcher.current_account_number() == cand.number:
            return "active"
        if self.switcher._live_session_pids(cand.number, cand.email):
            return "held"
        return None

    def tick(self) -> float:
        """Refresh whatever is due; returns when the next one is due."""
        now = self.clock()
        next_at = _NEVER
        cands = list(self.candidates())
        entries = self.switcher._usage_store.entries(
            {c.number: (c.email, c.org_uuid) for c in cands}
        )
        for cand in cands:
            if self._stopping:
                break
            fp = oauth.credential_fingerprint(cand.creds)
            if cand.number in self._dead_fp and self._dead_fp[cand.number] == fp:
                continue  # this exact login already came back dead
            entry = entries.get(cand.number)
            if entry is not None and entry.token_dead(stored_fp=fp):
                continue  # struck earlier (by anyone) and not re-logged since
            due = due_at(cand.creds, lead_s=self.lead_s, now=now)
            if due is None:
                continue
            due = max(due, self._retry_at.get(cand.number, 0.0))
            if due > now:
                next_at = min(next_at, due)
                continue
            blocked = self._blocked(cand)
            if blocked is not None:
                self._retry_at[cand.number] = now + HELD_RECHECK_S
                next_at = min(next_at, now + HELD_RECHECK_S)
                if blocked == "held":
                    self.on_event(RefreshEvent("held", cand.number, cand.email))
                continue
            next_at = min(next_at, self._refresh(cand, fp, now))
        # Nothing scheduled: the loop's MAX_SLEEP_S cap re-lists periodically.
        return next_at if next_at != _NEVER else now + MAX_SLEEP_S

    def _refresh(self, cand: Candidate, fp: str | None, now: float) -> float:
        outcome = self.switcher.consume_backup_grant(cand.number, cand.email, cand.creds)
        if outcome.error is None and outcome.credentials:
            self._retry_at.pop(cand.number, None)
            self._dead_fp.pop(cand.number, None)
            self.on_event(RefreshEvent("refreshed", cand.number, cand.email))
            fresh = due_at(outcome.credentials, lead_s=self.lead_s, now=now)
            # A successor that is somehow already due must not spin the loop.
            return max(fresh, now + RETRY_S) if fresh is not None else _NEVER
        if outcome.error in DEAD_ERRORS:
            self._dead_fp[cand.number] = fp
            self._record_dead(cand, outcome.consumed_fp or fp, outcome.error)
            self.on_event(RefreshEvent("dead", cand.number, cand.email, outcome.error))
            return _NEVER
        wait = BUSY_RETRY_S if outcome.error == "consume-busy" else RETRY_S
        self._retry_at[cand.number] = now + wait
        self.on_event(
            RefreshEvent("failed", cand.number, cand.email, outcome.error or "no credential")
        )
        return now + wait

    def _record_dead(self, cand: Candidate, struck_fp: str | None, error: str) -> None:
        """Strike the dead generation so every surface shows "re-login needed"."""
        try:
            self.switcher._usage_store.record(
                {cand.number: FetchRecord(error=error, struck_fp=struck_fp)},
                {cand.number: (cand.email, cand.org_uuid)},
            )
        except Exception:  # noqa: BLE001 -- the verdict is still in memory
            _logger.warning(
                "could not record account %s's dead token", cand.number, exc_info=True
            )
