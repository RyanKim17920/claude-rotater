"""Log an account in from the TUI without touching the active login (upstream #107).

``claude auth login`` normally writes the default profile, which IS the active
account. To add (or re-login) an account without disturbing it, the login runs
in a throwaway profile instead:

1. create a private temp dir under ``<backup>/logins/`` and export it,
   verbatim, as ``CLAUDE_CONFIG_DIR`` (auth-override env vars dropped);
2. run ``claude auth login --claudeai`` (or ``--console``) attached to the
   terminal, so the browser / paste-code flow works;
3. confirm with ``claude auth status --json``;
4. capture the staged profile with ``add_account(source_dir=...)``: the
   credential from the profile's hashed keychain item
   (``Claude Code-credentials-<sha256(NFC dir)[:8]>``) on macOS or its
   ``.credentials.json`` elsewhere, the identity from its ``.claude.json``.
   An account that is already managed gets its stored credential replaced
   in place (the re-login of a dead slot); a new one gets the next slot;
5. ALWAYS delete the hashed keychain item and the temp dir afterwards.

Step 5 runs on success, failure and cancel alike. A process killed mid-login
can still leave a stage behind, so each new login first sweeps stages older
than ``STALE_STAGE_S`` (never a younger one: it may be a concurrent login).

The interactive step (2-3) and the capture (4) are separate calls so the TUI
can run the first with the app suspended and the second in a worker thread.
There is deliberately no CLI verb: in a shell, ``claude /login`` followed by
``cswap add`` already does this.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from claude_swap.exceptions import LoginCancelled, LoginError
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS, delete_macos_keychain_entry

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

LOGIN_METHODS = ("claudeai", "console")
STAGE_DIRNAME = "logins"
STALE_STAGE_S = 3600.0
STATUS_TIMEOUT_S = 20.0
# Besides the auth overrides, a redirected secure store would send the new
# credential somewhere other than the stage's own hashed item.
_DROPPED_ENV = (*AUTH_OVERRIDE_ENV_VARS, "CLAUDE_SECURESTORAGE_CONFIG_DIR")

Runner = Callable[..., subprocess.CompletedProcess]


def stage_root(backup_dir: Path) -> Path:
    return Path(backup_dir) / STAGE_DIRNAME


def login_env(config_dir: str) -> dict[str, str]:
    """Environment for the staged ``claude auth`` calls."""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def remove_stage(config_dir: str) -> None:
    """Delete a stage's hashed keychain item (exact string) and its dir.

    Best effort and idempotent: each half runs even if the other fails.
    """
    try:
        delete_macos_keychain_entry(config_dir)
    except Exception:  # noqa: BLE001 -- cleanup must never raise
        pass
    shutil.rmtree(config_dir, ignore_errors=True)


def sweep_stale_stages(backup_dir: Path, *, now: float, max_age_s: float = STALE_STAGE_S) -> int:
    """Remove stages a killed login left behind. Returns how many."""
    root = stage_root(backup_dir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        try:
            if not entry.is_dir() or now - entry.stat().st_mtime < max_age_s:
                continue
        except OSError:
            continue
        remove_stage(str(entry))
        removed += 1
    return removed


class StagedLogin:
    """One login into a throwaway profile. Use as a context manager, or call
    :meth:`cleanup` yourself; it is safe to call more than once."""

    def __init__(
        self,
        backup_dir: Path,
        *,
        runner: Runner = subprocess.run,
        claude_bin: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backup_dir = Path(backup_dir)
        self._run = runner
        self._claude_bin = claude_bin
        self._clock = clock
        self.config_dir: str | None = None
        self.status: dict | None = None

    def __enter__(self) -> "StagedLogin":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    @property
    def claude_bin(self) -> str:
        return self._claude_bin or shutil.which("claude") or "claude"

    def open(self) -> str:
        """Create the private stage dir; returns the exact exported string."""
        if self.config_dir is not None:
            return self.config_dir
        sweep_stale_stages(self.backup_dir, now=self._clock())
        root = stage_root(self.backup_dir)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdtemp creates the leaf 0o700 and returns an absolute path with no
        # trailing slash: this string is hashed for the keychain name as is.
        self.config_dir = tempfile.mkdtemp(prefix="login-", dir=str(root))
        return self.config_dir

    def authenticate(self, *, method: str = "claudeai", email: str | None = None) -> dict:
        """Run the interactive login, then confirm it. Blocks on the terminal.

        Raises :class:`LoginCancelled` on Ctrl+C or a non-zero exit (claude
        exits non-zero when the flow is abandoned) and :class:`LoginError`
        when claude cannot run or the status check does not confirm a login.
        """
        if method not in LOGIN_METHODS:
            raise LoginError(f"Unknown login method {method!r}; use one of {', '.join(LOGIN_METHODS)}")
        config_dir = self.open()
        env = login_env(config_dir)
        argv = [self.claude_bin, "auth", "login", f"--{method}"]
        if email:
            argv += ["--email", email]
        try:
            result = self._run(argv, env=env)
        except KeyboardInterrupt:
            raise LoginCancelled("Login cancelled; nothing was stored.") from None
        except OSError as e:
            raise LoginError(
                f"Could not run 'claude' ({e}). Is Claude Code installed and on PATH?"
            ) from e
        if result.returncode != 0:
            raise LoginCancelled(
                f"Login did not complete (claude exited with status {result.returncode}); "
                "nothing was stored."
            )
        self.status = self._confirm(env)
        return self.status

    def _confirm(self, env: dict[str, str]) -> dict:
        try:
            result = self._run(
                [self.claude_bin, "auth", "status", "--json"],
                env=env,
                capture_output=True,
                text=True,
                timeout=STATUS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise LoginError("'claude auth status' timed out; nothing was stored.") from e
        except OSError as e:
            raise LoginError(f"Could not run 'claude auth status' ({e}).") from e
        try:
            status = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError):
            status = None
        if not isinstance(status, dict) or status.get("loggedIn") is not True:
            raise LoginError("Claude Code does not report a login in the new profile; nothing was stored.")
        return status

    def import_into(
        self,
        switcher: "ClaudeAccountSwitcher",
        *,
        slot: int | None = None,
        alias: str | None = None,
        assume_yes: bool = True,
    ) -> None:
        """Store the staged login as a managed account (or refresh its slot)."""
        if self.config_dir is None or self.status is None:
            raise LoginError("No completed login to import.")
        switcher.add_account(
            slot=slot, assume_yes=assume_yes, alias=alias, source_dir=self.config_dir
        )

    def cleanup(self) -> None:
        config_dir, self.config_dir = self.config_dir, None
        if config_dir is not None:
            remove_stage(config_dir)

