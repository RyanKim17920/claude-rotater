"""rota's own Textual themes ("rota-dark", "rota-light") and shared colors.

Direction: a duty roster read at a glance, not a dashboard. Cool slate
backgrounds (blue-grey, never the warm charcoal or cream most terminal tools
default to), one periwinkle accent that means "rota itself": the brand chip,
the account on duty, the key you press. Usage severity has its own ramp,
sea glass → saffron → rose, so the accent is never mistaken for "fine" or
"danger". Every text color clears WCAG AA (4.5:1) on all three background
tiers in both themes; tests/test_theme.py holds that line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.theme import Theme

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#a3abff"  # periwinkle: brand, on-duty account, keys
FOREGROUND = "#dde3ea"  # cool off-white
MUTED = "#8a96a6"  # slate grey for labels and secondary text
BACKGROUND = "#11151c"  # blue-black slate
SURFACE = "#19202a"  # header band, highlighted row
PANEL = "#232c38"  # most elevated: chips

# Usage severity ramp.
SEV_OK = "#6fc4aa"  # sea glass: plenty of headroom
SEV_WARN = "#f2b443"  # saffron: climbing (>= 70%); saturated, never beige
SEV_CRIT = "#f08c82"  # rose: near the limit (>= 90%)
TRACK = "#2a3442"  # unfilled meter track
# The row under the cursor: the accent blended over the page at this
# strength (rota.tcss: ``background: $primary 14%``). Strong enough to see at
# a glance, weak enough that every text colour keeps AA contrast on it.
CURSOR_ALPHA = 0.14

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str
    background: str = BACKGROUND

    DARK: ClassVar["Palette"]

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
            background=theme.background or BACKGROUND,
        )


def _variables(panel: str, foreground: str, track: str) -> dict[str, str]:
    return {
        "block-cursor-background": panel,
        "block-cursor-foreground": foreground,
        "block-cursor-text-style": "none",
        "track": track,
    }


ROTA_DARK = Theme(
    name="rota-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables=_variables(PANEL, FOREGROUND, TRACK),
)

# Light companion: the same roles on a cool near-white, deepened for AA.
ACCENT_LIGHT = "#454bbf"  # indigo
FOREGROUND_LIGHT = "#1b2330"
MUTED_LIGHT = "#505c6d"
BACKGROUND_LIGHT = "#f5f7fa"
SURFACE_LIGHT = "#e8ecf2"
PANEL_LIGHT = "#d9e0e9"  # most elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#17705a"  # deep sea green
SEV_WARN_LIGHT = "#7d5600"  # dark saffron
SEV_CRIT_LIGHT = "#ad3229"  # brick rose
TRACK_LIGHT = "#bcc6d3"  # deep enough to read as a bar on the near-white page

ROTA_LIGHT = Theme(
    name="rota-light",
    primary=ACCENT_LIGHT,
    secondary=MUTED_LIGHT,
    accent=ACCENT_LIGHT,
    foreground=FOREGROUND_LIGHT,
    background=BACKGROUND_LIGHT,
    surface=SURFACE_LIGHT,
    panel=PANEL_LIGHT,
    success=SEV_OK_LIGHT,
    warning=SEV_WARN_LIGHT,
    error=SEV_CRIT_LIGHT,
    dark=False,
    variables=_variables(PANEL_LIGHT, FOREGROUND_LIGHT, TRACK_LIGHT),
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
    background=BACKGROUND,
)


def blend(top: str, bottom: str, alpha: float) -> str:
    """``top`` at ``alpha`` over ``bottom`` (both ``#rrggbb``): a tint."""
    a = [int(top[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(bottom[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(y + (x - y) * alpha):02x}" for x, y in zip(a, b))


def brand_chip(palette: Palette) -> Text:
    """The ``rota`` mark: a solid accent chip, the one loud thing on screen."""
    chip = Text()
    chip.append(" rota ", style=f"bold {palette.background} on {palette.accent}")
    return chip
