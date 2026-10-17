"""Tests for the Palette value object and the light/dark themes."""
from __future__ import annotations

from claude_rotater.tui import theme
from claude_rotater.tui.theme import ROTA_DARK, ROTA_LIGHT, Palette


def test_dark_palette_matches_constants():
    p = Palette.DARK
    assert (p.accent, p.foreground, p.muted, p.sev_ok, p.sev_warn, p.sev_crit, p.track) == (
        theme.ACCENT,
        theme.FOREGROUND,
        theme.MUTED,
        theme.SEV_OK,
        theme.SEV_WARN,
        theme.SEV_CRIT,
        theme.TRACK,
    )


def test_from_theme_reads_theme_object_including_track():
    p = Palette.from_theme(ROTA_LIGHT)
    assert p.accent == theme.ACCENT_LIGHT
    assert p.sev_crit == theme.SEV_CRIT_LIGHT
    assert p.track == theme.TRACK_LIGHT  # from Theme.variables["track"], not app cache


def test_severity_ramp_and_none():
    p = Palette.DARK
    assert p.severity(None) == p.muted
    assert p.severity(95.0) == p.sev_crit
    assert p.severity(75.0) == p.sev_warn
    assert p.severity(10.0) == p.sev_ok


def test_both_themes_expose_track_variable():
    assert ROTA_DARK.variables["track"] == theme.TRACK
    assert ROTA_LIGHT.variables["track"] == theme.TRACK_LIGHT
    assert ROTA_LIGHT.dark is False


def _contrast(hex_a: str, hex_b: str) -> float:
    def lum(h: str) -> float:
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    la, lb = sorted((lum(hex_a), lum(hex_b)))
    return (lb + 0.05) / (la + 0.05)


def _blend(top: str, bottom: str, alpha: float) -> str:
    """``top`` at ``alpha`` over ``bottom``, as Textual's ``$primary 14%`` paints it."""
    a = [int(top[i:i+2], 16) for i in (1, 3, 5)]
    b = [int(bottom[i:i+2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(y + (x - y) * alpha):02x}" for x, y in zip(a, b))


def test_cursor_band_is_visible_in_both_themes():
    """The row under the cursor must read as highlighted against the page
    and stand apart from the header band ($surface)."""
    for accent, bg, surface in (
        (theme.ACCENT, theme.BACKGROUND, theme.SURFACE),
        (theme.ACCENT_LIGHT, theme.BACKGROUND_LIGHT, theme.SURFACE_LIGHT),
    ):
        band = _blend(accent, bg, theme.CURSOR_ALPHA)
        assert _contrast(band, bg) >= 1.15, band
        assert band not in (bg, surface)


def test_light_text_meets_AA_on_all_backgrounds():
    # Accent and severity colors render as PERCENTAGE TEXT on highlighted
    # ($surface) and flash ($panel) rows, not just the base background — so
    # every text color must clear the 4.5:1 text bar against all three.
    from claude_rotater.tui import theme
    text_colors = (
        theme.FOREGROUND_LIGHT,
        theme.MUTED_LIGHT,
        theme.ACCENT_LIGHT,
        theme.SEV_OK_LIGHT,
        theme.SEV_WARN_LIGHT,
        theme.SEV_CRIT_LIGHT,
    )
    backgrounds = (theme.BACKGROUND_LIGHT, theme.SURFACE_LIGHT, theme.PANEL_LIGHT,
                   _blend(theme.ACCENT_LIGHT, theme.BACKGROUND_LIGHT, theme.CURSOR_ALPHA))
    for color in text_colors:
        for bg in backgrounds:
            assert _contrast(color, bg) >= 4.5, f"{color} on {bg}"


def test_dark_text_meets_AA_on_all_backgrounds():
    text_colors = (
        theme.FOREGROUND, theme.MUTED, theme.ACCENT,
        theme.SEV_OK, theme.SEV_WARN, theme.SEV_CRIT,
    )
    for color in text_colors:
        for bg in (theme.BACKGROUND, theme.SURFACE, theme.PANEL,
                   _blend(theme.ACCENT, theme.BACKGROUND, theme.CURSOR_ALPHA)):
            assert _contrast(color, bg) >= 4.5, f"{color} on {bg}"


def test_accent_is_distinct_from_every_severity_color():
    # The accent means "rota / on duty"; it must never read as a usage level.
    for accent, ramp in (
        (theme.ACCENT, (theme.SEV_OK, theme.SEV_WARN, theme.SEV_CRIT)),
        (theme.ACCENT_LIGHT, (theme.SEV_OK_LIGHT, theme.SEV_WARN_LIGHT, theme.SEV_CRIT_LIGHT)),
    ):
        assert accent not in ramp


def test_palette_carries_theme_background():
    assert Palette.from_theme(ROTA_LIGHT).background == theme.BACKGROUND_LIGHT
    assert Palette.DARK.background == theme.BACKGROUND


def test_brand_chip_style_does_not_leak_into_appended_text():
    from claude_rotater.tui.theme import brand_chip

    text = brand_chip(Palette.DARK)
    text.append(" settings")
    assert text.plain == " rota  settings"
    assert str(text.style) == ""  # the chip style lives on its span only


def test_bar_track_reads_against_the_page_in_both_themes():
    assert _contrast(theme.TRACK, theme.BACKGROUND) >= 1.4
    assert _contrast(theme.TRACK_LIGHT, theme.BACKGROUND_LIGHT) >= 1.55


def test_blend_matches_the_test_reference():
    assert theme.blend("#ffffff", "#000000", 0.5) == _blend("#ffffff", "#000000", 0.5) == "#808080"
    assert theme.blend(theme.SEV_OK, theme.TRACK, 0.0) == theme.TRACK
