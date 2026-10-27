"""The three palette families (accounts, models, token kinds) stay apart.

A color must never mean two things on one screen: each family is measured
for distinctness within itself (CIELAB ΔE, also under simulated
deuteranopia), against the other families, against the reserved colors
(severity, the accent, muted, text), and for contrast on the theme's
backgrounds. The floors sit a little under what the chosen hues measure, so
a retune that makes things worse fails here.
"""
from __future__ import annotations

import itertools
import math

import pytest

from claude_rotater.tui import theme

# Machado et al. 2009, deuteranopia at full severity, on linear RGB.
_DEUTAN = ((0.367322, 0.860646, -0.227968),
           (0.280085, 0.672501, 0.047413),
           (-0.011820, 0.042940, 0.968881))


def _linear(hex_: str) -> tuple[float, float, float]:
    def f(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return tuple(f(int(hex_[i:i + 2], 16) / 255) for i in (1, 3, 5))


def _lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = rgb
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116  # noqa: E731
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    """CIE76 distance; about 2 is a just-noticeable difference."""
    return math.dist(_lab(_linear(a)), _lab(_linear(b)))


def delta_e_deutan(a: str, b: str) -> float:
    def sim(h: str) -> tuple[float, float, float]:
        rgb = _linear(h)
        return tuple(min(1.0, max(0.0, sum(m * c for m, c in zip(row, rgb)))) for row in _DEUTAN)
    return math.dist(_lab(sim(a)), _lab(sim(b)))


def contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        r, g, b = _linear(h)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    lo, hi = sorted((lum(a), lum(b)))
    return (hi + 0.05) / (lo + 0.05)


def lightness(h: str) -> float:
    return _lab(_linear(h))[0]


def _min(pairs, metric) -> float:
    return min(metric(a, b) for a, b in pairs)


THEMES = {
    "dark": dict(
        accounts=theme.ACCOUNT_HUES, models=theme.MODEL_SHADES, tokens=theme.TOKEN_HUES,
        reserved=(theme.SEV_OK, theme.SEV_WARN, theme.SEV_CRIT, theme.MUTED, theme.ACCENT,
                  theme.FOREGROUND),
        backgrounds=(theme.BACKGROUND, theme.SURFACE, theme.PANEL,
                     theme.blend(theme.ACCENT, theme.BACKGROUND, theme.CURSOR_ALPHA)),
        token_contrast=3.0,
    ),
    "light": dict(
        accounts=theme.ACCOUNT_HUES_LIGHT, models=theme.MODEL_SHADES_LIGHT,
        tokens=theme.TOKEN_HUES_LIGHT,
        reserved=(theme.SEV_OK_LIGHT, theme.SEV_WARN_LIGHT, theme.SEV_CRIT_LIGHT,
                  theme.MUTED_LIGHT, theme.ACCENT_LIGHT, theme.FOREGROUND_LIGHT),
        backgrounds=(theme.BACKGROUND_LIGHT, theme.SURFACE_LIGHT, theme.PANEL_LIGHT,
                     theme.blend(theme.ACCENT_LIGHT, theme.BACKGROUND_LIGHT,
                                 theme.CURSOR_ALPHA)),
        # The palest step is input, a sliver of every bar; the steps that
        # carry the bars (cache read, output) clear 4.5.
        token_contrast=2.0,
    ),
}


@pytest.fixture(params=sorted(THEMES))
def t(request):
    return THEMES[request.param]


def _model_colors(t) -> list[str]:
    return [c for _, shades in t["models"] for c in shades]


def test_accounts_are_far_apart_even_for_deuteranopes(t):
    pairs = list(itertools.combinations(t["accounts"], 2))
    assert len(t["accounts"]) >= 5
    assert _min(pairs, delta_e) >= 20
    assert _min(pairs, delta_e_deutan) >= 10


def test_model_families_are_far_apart_and_their_versions_step_apart(t):
    newest = [shades[0] for _, shades in t["models"]]
    assert _min(itertools.combinations(newest, 2), delta_e) >= 20
    assert _min(itertools.combinations(newest, 2), delta_e_deutan) >= 9
    for _, shades in t["models"]:
        assert _min(zip(shades, shades[1:]), delta_e) >= 13
    assert _min(itertools.combinations(_model_colors(t), 2), delta_e) >= 13


def test_token_kinds_are_one_sequential_ramp(t):
    tokens = t["tokens"]
    assert len(tokens) == 4
    assert _min(itertools.combinations(tokens, 2), delta_e) >= 20
    assert _min(itertools.combinations(tokens, 2), delta_e_deutan) >= 12
    steps = [lightness(c) for c in tokens]
    assert steps == sorted(steps, reverse=True)  # input palest, output deepest


def test_no_family_borrows_another_familys_or_a_reserved_color(t):
    families = {"accounts": t["accounts"], "models": _model_colors(t), "tokens": t["tokens"]}
    for (_, a), (_, b) in itertools.combinations(families.items(), 2):
        assert _min(itertools.product(a, b), delta_e) >= 15
    for colors in families.values():
        assert _min(itertools.product(colors, t["reserved"]), delta_e) >= 15


def test_every_family_reads_on_the_themes_backgrounds(t):
    def worst(colors):
        return min(contrast(c, bg) for c in colors for bg in t["backgrounds"])
    assert worst(t["accounts"]) >= 4.5  # #N and legend text
    assert worst([shades[0] for _, shades in t["models"]]) >= 4.5  # a model's name in titles
    assert worst(_model_colors(t)) >= 3.0  # bars and swatches
    assert worst(t["tokens"]) >= t["token_contrast"]


def test_palette_resolves_the_theme_families():
    dark = theme.Palette.from_theme(theme.ROTA_DARK)
    light = theme.Palette.from_theme(theme.ROTA_LIGHT)
    assert (dark.models, dark.tokens) == (theme.MODEL_SHADES, theme.TOKEN_HUES)
    assert (light.models, light.tokens) == (theme.MODEL_SHADES_LIGHT, theme.TOKEN_HUES_LIGHT)
    assert dark.model(0, 1) == theme.MODEL_SHADES[0][1][1]
    assert dark.model(0, 9) == dark.model(-1) == dark.model(99) == dark.muted
