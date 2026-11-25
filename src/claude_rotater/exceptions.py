"""Custom exceptions for rota."""


class ClaudeSwitchError(Exception):
    """Base exception for rota errors."""

    pass


class CredentialError(ClaudeSwitchError):
    """Error related to credential operations."""

    pass


class CredentialReadError(CredentialError):
    """Failed to read credentials."""

    pass


class CredentialWriteError(CredentialError):
    """Failed to write credentials."""

    pass


class KeychainLockedError(CredentialWriteError):
    """The macOS Keychain refused a write and still holds the old login.

    Claude Code reads the Keychain before the plaintext file, so a file
    fallback would switch nothing once the Keychain unlocks. Nothing was
    changed; retry after unlocking it. ``action`` names what waits for the
    unlock ("switch", or a refresh of the live login).
    """

    def __init__(self, detail: str = "", *, action: str = "switch"):
        message = f"Keychain locked: unlock it to {action}"
        super().__init__(f"{message} ({detail})" if detail else message)


class ConfigError(ClaudeSwitchError):
    """Error related to configuration operations."""

    pass


class SwitchError(ClaudeSwitchError):
    """Error during account switch operation."""

    pass


class SessionError(ClaudeSwitchError):
    """Error setting up or launching a session-mode profile."""

    pass


class LockError(ClaudeSwitchError):
    """Error acquiring lock."""

    pass


class ClaudeCodeLockTimeout(LockError):
    """Timed out acquiring one of Claude Code's own advisory locks.

    Raised when ``~/.claude.lock`` / ``~/.claude.json.lock`` stays held past
    our bounded wait — usually Claude Code mid-token-refresh. Nothing has been
    mutated when this raises; the operation is safe to retry.
    """

    pass


class AccountNotFoundError(ClaudeSwitchError):
    """Account not found."""

    pass


class ValidationError(ClaudeSwitchError):
    """Validation error."""

    pass


class MigrationError(ClaudeSwitchError):
    """Error migrating the backup directory between layouts (e.g. legacy → XDG)."""

    pass


class LoginError(ClaudeSwitchError):
    """An in-app (TUI) login failed; nothing was stored."""

    pass


class LoginCancelled(LoginError):
    """The user cancelled an in-app login; nothing was stored."""

    pass
