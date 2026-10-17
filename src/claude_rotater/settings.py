"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable rota preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch``, ``ui``, ``run`` and ``accounts`` sections; other sections
can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_rotater.exceptions import ConfigError
from claude_rotater.fsutil import replace_with_retry
from claude_rotater.poll_policy import CADENCE_SETTING_KEYS, PollCadence, load_cadence

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("rota")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``rota auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick) and beat the active account's utilization by at
    least ``hysteresis_pct``, so two accounts hovering at the line never
    ping-pong while a strictly better account is always taken.
    """

    threshold: float = 90.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    # "expiring-first" (spend the account whose weekly window resets soonest,
    # balancing 5h windows when resets are similar; see expiring_first.py),
    # "best" (most headroom, only at the threshold) or "consume-first"
    # (soonest weekly reset, strictly). A data dir that never set the key
    # follows this default; an explicit value in settings.json is kept.
    strategy: str = "expiring-first"
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None
    # expiring-first: 7-day resets this close together count as tied, and
    # the tie is broken by 5h headroom instead of reset order.
    similar_reset_hours: float = 12.0
    # expiring-first: after a manual switch, no proactive move for this long
    # (threshold and failover moves still happen).
    manual_hold_minutes: float = 60.0


@dataclass(frozen=True)
class UiSettings:
    """TUI preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection.
    ``login_warn_days`` flags an account whose stored login (refresh token)
    lapses within that many days, so it can be re-logged in ahead of time."""

    theme: str = "auto"
    login_warn_days: int = 3


@dataclass(frozen=True)
class RunSettings:
    """``rota run`` defaults (``run`` section). ``share_default``: whether a
    session profile shares settings, CLAUDE.md, skills and the like from the
    main Claude config when the call says neither ``--share`` nor
    ``--no-share``. A profile kept apart from the main config (a separate
    work setup) turns it off once instead of passing ``--no-share`` every
    time."""

    share_default: bool = True


@dataclass(frozen=True)
class AccountsSettings:
    """Account policy for this data dir (``accounts`` section).
    ``oauth_only`` holds managed API-key accounts out of every switch: bare
    and strategy rotation, explicit ``rota switch``/TUI switches and the auto
    engine (whatever ``autoswitch.includeApiKeyAccounts`` says). They stay
    managed and listed."""

    oauth_only: bool = False


# Where each spec's default (and effective value) lives, by ``SettingSpec.source``.
# The poll cadence keys sit in the ``autoswitch`` JSON section but are owned
# by ``poll_policy.PollCadence``, so their defaults are never restated here.
_DEFAULT_SOURCES = {
    "autoswitch": AutoSwitchSettings,
    "ui": UiSettings,
    "cadence": PollCadence,
    "run": RunSettings,
    "accounts": AccountsSettings,
}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation on the TUI config page
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case field on the source dataclass
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""
    group: str = ""  # SETTING_GROUPS key: the Config page section it renders in
    label: str = ""  # short UI name shown on the Config page
    # _DEFAULT_SOURCES key owning the field; empty means the section's own
    # dataclass (AutoSwitchSettings / UiSettings).
    source_key: str = ""
    step: float | None = None  # Config page ←/→ increment for float/int kinds
    unit: str = ""  # Config page suffix for a number: "%", "s", "d"

    @property
    def step_size(self) -> float:
        """The ←/→ increment: the explicit ``step``, else 1."""
        return self.step if self.step is not None else 1.0

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def source(self) -> str:
        return self.source_key or self.section

    @property
    def default(self):
        return getattr(_DEFAULT_SOURCES[self.source](), self.field)


# Config page text for the poll cadence keys. Field, kind and bounds come from
# poll_policy.CADENCE_SETTING_KEYS (the lenient loader's table), so the strict
# `config set` validation and the clamp on load share one range.
_CADENCE_TEXT: dict[str, tuple[str, str]] = {
    "pollSpareSeconds": (
        "Poll interval",
        "Seconds between usage polls of each account; the active one is "
        "skipped while its statusline feed is fresh",
    ),
    "pollHourlyBudget": (
        "Hourly poll budget",
        "Most usage polls per account in any trailing hour (hard cap)",
    ),
    "feedStaleSeconds": (
        "Feed stale after",
        "Poll the active account again once rota feed has been quiet this long",
    ),
}


def _cadence_specs() -> tuple[SettingSpec, ...]:
    return tuple(
        SettingSpec(
            "autoswitch", json_key, field, kind, lo, hi,
            help=_CADENCE_TEXT[json_key][1],
            group="polling", label=_CADENCE_TEXT[json_key][0],
            source_key="cadence",
            step=30.0 if json_key.endswith("Seconds") else 1.0,
            unit="s" if json_key.endswith("Seconds") else "",
        )
        for json_key, (field, kind, lo, hi) in CADENCE_SETTING_KEYS.items()
    )


# Auto-switch strategies, the default first. cli.py's --strategy reads this
# too, so the flag and the setting accept the same names.
STRATEGY_CHOICES: tuple[str, ...] = ("expiring-first", "best", "consume-first")

# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this %",
            group="autoswitch", label="Threshold", step=1.0, unit="%",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
            group="autoswitch", label="Cooldown", step=30.0, unit="s",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many % points",
            group="autoswitch", label="Hysteresis", step=1.0, unit="%",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=STRATEGY_CHOICES,
            help="How auto-switch picks the target account: expiring-first "
            "spends the account whose weekly quota expires soonest",
            group="autoswitch", label="Strategy",
        ),
        SettingSpec(
            "autoswitch", "similarResetHours", "similar_reset_hours", "float",
            0.0, 168.0,
            help="expiring-first: weekly resets this close count as tied and "
            "are balanced by 5h headroom",
            group="autoswitch", label="Similar resets within", step=1.0, unit="h",
        ),
        SettingSpec(
            "autoswitch", "manualHoldMinutes", "manual_hold_minutes", "float",
            0.0, 1440.0,
            help="expiring-first: no proactive switch for this long after a "
            "manual switch",
            group="autoswitch", label="Manual switch hold", step=5.0, unit="m",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
            group="autoswitch", label="Include API-key accounts",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
            group="autoswitch", label="Unhealthy after", step=1.0, unit=" polls",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
            group="autoswitch", label="Model limits",
        ),
        *_cadence_specs(),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
            group="ui", label="Theme",
        ),
        SettingSpec(
            "ui", "loginWarnDays", "login_warn_days", "int", 0, 60,
            help="Warn when an account's login ends within this many days (0 = never)",
            group="ui", label="Login warning", step=1.0, unit="d",
        ),
        SettingSpec(
            "run", "shareDefault", "share_default", "bool",
            help="rota run shares settings, CLAUDE.md and skills from the main "
            "config unless told --share/--no-share",
            group="profile", label="rota run shares config",
        ),
        SettingSpec(
            "accounts", "oauthOnly", "oauth_only", "bool",
            help="Never switch onto API-key accounts, by hand or automatically",
            group="profile", label="OAuth accounts only",
        ),
    )
}

# Config page sections, in display order: (group key, heading). Every spec's
# ``group`` must be one of these keys (enforced by a test), so a new spec
# lands in a section without any UI code.
SETTING_GROUPS: tuple[tuple[str, str], ...] = (
    ("autoswitch", "Auto-switch"),
    ("polling", "Polling"),
    ("profile", "Profile"),
    ("ui", "Appearance"),
)

# AutoSwitchSettings fields only: the cadence keys share the JSON section but
# are loaded by poll_policy.load_cadence.
_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.source == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into the SETTING_SPECS ranges; bad types → the default."""

    def num(value, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.source != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}
    return raw


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or unknown theme → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        theme = default.theme
    spec = SETTING_SPECS["ui.loginWarnDays"]
    days = section.get(spec.json_key, default.login_warn_days)
    if isinstance(days, bool) or not isinstance(days, (int, float)):
        days = default.login_warn_days
    days = int(min(max(days, spec.lo), spec.hi))
    return UiSettings(theme=theme, login_warn_days=days)


def _load_flag_section(backup_root: Path, section: str):
    """Load a section made only of bool specs (``run``, ``accounts``); a
    missing section or a non-bool value reads as the default."""
    cls = _DEFAULT_SOURCES[section]
    raw = _read_raw(settings_path(backup_root)).get(section)
    if not isinstance(raw, dict):
        return cls()
    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.source != section or spec.json_key not in raw:
            continue
        value = raw[spec.json_key]
        if isinstance(value, bool):
            kwargs[spec.field] = value
        else:
            _logger.warning(
                "settings.json: %s expects true or false, got %r; using %r",
                spec.dotted, value, spec.default,
            )
    return cls(**kwargs)


def load_run_settings(backup_root: Path) -> RunSettings:
    """Load the ``run`` section; missing/corrupt values → defaults."""
    return _load_flag_section(backup_root, "run")


def load_accounts_settings(backup_root: Path) -> AccountsSettings:
    """Load the ``accounts`` section; missing/corrupt values → defaults."""
    return _load_flag_section(backup_root, "accounts")


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        section = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a string typed on the TUI config page.

    Unlike the forgiving clamp on load, out-of-range or mistyped values raise
    ConfigError so the user learns about the problem when setting the value,
    not by silently degraded behavior at `rota auto` time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; press ctrl+r on "
                "rota's config page (c) to clear it"
            )
        return value
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key from the TUI config page; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get(spec.section)
    if not isinstance(section, dict):
        section = {}
    section[spec.json_key] = value
    raw[spec.section] = section
    atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    section = raw.get(spec.section)
    if not isinstance(section, dict) or spec.json_key not in section:
        return False
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    del section[spec.json_key]
    if not section:
        del raw[spec.section]
    atomic_write_json(path, raw)
    return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so the config page's "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "ui": load_ui_settings(backup_root),
        "cadence": load_cadence(backup_root),
        "run": load_run_settings(backup_root),
        "accounts": load_accounts_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.source], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict, *, compact: bool = False) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them). ``compact`` drops the
    indentation, for machine-only files where size matters (history).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory rota owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, and ``mkstemp``
      creates it 0600 to begin with, so the secret is never exposed.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        text = (
            json.dumps(data, separators=(",", ":"))
            if compact
            else json.dumps(data, indent=2)
        )
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
        if sys.platform != "win32":
            os.chmod(str(target), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
