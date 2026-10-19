"""Logins that have died or are about to: the header note, a desktop
notification and a log line, so a lapsed login is never found out by a
failing claude.

Two shapes, told apart because only one can be warned about ahead:

- **ending / ended**: the stored login (refresh token) carries its own end
  date (``refreshTokenExpiresAt``). Within ``ui.loginWarnDays`` it is
  "ending"; past it, "ended".
- **dead**: the server refused the login before any end date — rota's own
  refresh got ``invalid_grant``, or Claude Code cleared the live tokens after
  its refresh was refused (a revocation: signed in elsewhere, a stale copy of
  the token spent by another holder). Nothing can predict it; rota can only
  say so the moment it is seen.

:func:`collect` is pure (the TUI header calls it on every paint);
:class:`LoginNotifier` adds the side effects, rate-limited to once a day per
account and state, and is driven by the auto-switch engine, so it works the
same in the TUI and in headless ``rota auto``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

DAY_S = 86400.0
# An ending login is worth interrupting for only on its last day; before
# that the header note carries it.
NOTIFY_ENDING_WITHIN_S = DAY_S
NOTIFY_EVERY_S = DAY_S
STATE_FILENAME = "login_alerts.json"
OSASCRIPT_TIMEOUT_S = 5.0

DEAD, ENDED, ENDING = "dead", "ended", "ending"
_SEVERITY = {DEAD: 0, ENDED: 1, ENDING: 2}


@dataclass(frozen=True)
class LoginState:
    """What :func:`collect` needs to know about one account's login.

    ``dead_cause`` is the server's verdict when the login was refused
    ("invalid_grant", ...), else None; ``ends_at`` is the stored login's end
    (epoch seconds), None when unknown."""

    number: str
    email: str
    dead_cause: str | None = None
    ends_at: float | None = None


@dataclass(frozen=True)
class LoginAlert:
    number: str
    email: str
    kind: str  # DEAD | ENDED | ENDING
    cause: str = ""
    remaining_s: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.number}:{self.email}:{self.kind}"

    def message(self) -> str:
        """One plain sentence: what happened and the key that fixes it."""
        who = f"#{self.number}"
        if self.kind == DEAD:
            return f"{who} login was revoked (signed in elsewhere?) — press l to log in again"
        if self.kind == ENDED:
            return f"{who} login ended — press l to log in again"
        return f"{who} login ends in {duration_words(self.remaining_s)} — press l to log in again"


def duration_words(seconds: float) -> str:
    """``2d`` / ``5h`` / ``40m``: whole units, rounded down, never ``0``."""
    s = max(0.0, seconds)
    if s >= DAY_S:
        return f"{int(s // DAY_S)}d"
    if s >= 3600:
        return f"{int(s // 3600)}h"
    return f"{max(1, int(s // 60))}m"


REFUSED_CAUSE = "the server refused its refresh"


def dead_cause(sentinel: str | None, last_error: str | None) -> str | None:
    """The cause to report when a usage row says the login is dead, else None."""
    from claude_rotater.json_output import USAGE_RELOGIN_REQUIRED

    if sentinel != USAGE_RELOGIN_REQUIRED:
        return None
    return last_error or REFUSED_CAUSE


def states_from_snapshot(accounts: Iterable) -> list[LoginState]:
    """:class:`LoginState` per OAuth account of an ``AccountsSnapshot``.

    Accounts held out of rotation (``disabled``) are left out: the owner
    parked them, and nagging about them would train the eye to skip the
    note."""
    return [
        LoginState(
            acc.number, acc.email,
            dead_cause=dead_cause(acc.usage.sentinel, acc.usage.last_error),
            ends_at=acc.login_expires_at,
        )
        for acc in accounts
        if acc.kind == "oauth" and not acc.disabled
    ]


def collect(states: Iterable[LoginState], now: float, warn_days: int) -> list[LoginAlert]:
    """Every login worth a word, most severe first (then by account).

    A dead login wins over its end date: it already failed. ``warn_days``
    0 turns off the "ending" warning only — a login that is already dead or
    ended is always reported."""
    alerts: list[LoginAlert] = []
    for st in states:
        if st.dead_cause is not None:
            alerts.append(LoginAlert(st.number, st.email, DEAD, cause=st.dead_cause))
            continue
        if st.ends_at is None:
            continue
        remaining = st.ends_at - now
        if remaining <= 0:
            alerts.append(LoginAlert(st.number, st.email, ENDED, cause="its login reached its end date"))
        elif warn_days > 0 and remaining <= warn_days * DAY_S:
            alerts.append(LoginAlert(
                st.number, st.email, ENDING, cause="its login reaches its end date",
                remaining_s=remaining,
            ))
    alerts.sort(key=lambda a: (_SEVERITY[a.kind], a.remaining_s, _num_key(a.number)))
    return alerts


def _num_key(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (1 << 30, number)


def header_note(alerts: list[LoginAlert]) -> str:
    """The header's line for the most severe alert, naming the rest by count:
    ``#3 login was revoked (signed in elsewhere?) — press l to log in again``,
    ``login ends in 2d: #3 — press l`` (+ ``, #1 5h`` for more endings)."""
    if not alerts:
        return ""
    first = alerts[0]
    if first.kind == ENDING:
        ending = [a for a in alerts if a.kind == ENDING]
        rest = "".join(f", #{a.number} {duration_words(a.remaining_s)}" for a in ending[1:])
        return f"login ends in {duration_words(first.remaining_s)}: #{first.number}{rest} — press l"
    note = first.message()
    others = len(alerts) - 1
    return f"{note} (+{others} more)" if others else note


def is_failed(alert: LoginAlert) -> bool:
    return alert.kind in (DEAD, ENDED)


def notify_macos(title: str, body: str) -> bool:
    """Post a macOS notification; False when it could not be posted.

    The strings travel as ``argv``, never spliced into the AppleScript
    source, so no quoting can turn text into script."""
    if sys.platform != "darwin":
        return False
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, title, body],
            check=True, capture_output=True, timeout=OSASCRIPT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        _logger.debug("Could not post a notification: %r", e)
        return False
    return True


class LoginNotifier:
    """Logs and notifies each alert at most once a day per account and state.

    The log line is written whether or not notifications are on — the log
    is the record of what died and why. State lives in the data dir so a
    restarted engine does not repeat itself; one engine owns a data dir, so
    there is a single writer."""

    def __init__(
        self,
        data_dir: Path,
        *,
        send: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.path = data_dir / "cache" / STATE_FILENAME
        # Looked up at call time, so the test suite's guard replaces it for
        # every notifier, including the ones engines build themselves.
        self.send = send or (lambda title, body: notify_macos(title, body))

    def _read(self) -> dict[str, float]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            k: float(v) for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
        }

    def _write(self, state: dict[str, float]) -> None:
        from claude_rotater.settings import atomic_write_json

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, state)
        except OSError as e:
            _logger.debug("Could not save login alert state: %r", e)

    def observe(self, alerts: list[LoginAlert], now: float, *, notify: bool) -> list[LoginAlert]:
        """Log (and, with ``notify``, post) the alerts that are due; return them.

        Due: a dead or ended login, or one ending within a day, not already
        reported in the last day. State for accounts that no longer alert is
        dropped, so a login that dies again after a re-login is reported at
        once."""
        state = self._read()
        live = {a.key for a in alerts}
        kept = {k: t for k, t in state.items() if k in live and now - t < NOTIFY_EVERY_S}
        due = [
            a for a in alerts
            if (is_failed(a) or a.remaining_s <= NOTIFY_ENDING_WITHIN_S) and a.key not in kept
        ]
        for alert in due:
            _logger.warning(
                "Account %s (%s): %s (cause: %s); press l in rota to log in again.",
                alert.number, alert.email, alert.message().split(" — ")[0], alert.cause,
            )
            if notify:
                self.send("rota", alert.message())
            kept[alert.key] = now
        if kept != state:
            self._write(kept)
        return due
