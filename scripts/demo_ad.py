"""Cut demo_capture.py's screens into a short advert: one idea per beat.

    uv run --with pillow python scripts/demo_ad.py STORY SCREENS_DIR OUT_DIR

STORY is the one given to demo_capture.py, plus ``ad`` (the video) and
``gif`` (the shorter README loop), each a list of beats::

    {"head": "a short headline", "count": "$13,007",
     "frames": [{"screen": "cost-000", "crop": [col, row, cols, rows],
                 "hold": seconds, "mark": ["text on the screen"],
                 "draw": [first_row, last_row]}]}

and ``end`` (the last card). Frames of one beat that crop differently ease
from one crop to the next; beats cut hard. ``draw`` draws a bar chart's
bars in from the left over its rows; ``count`` ticks that part of the
headline up (from the lowest number with as many digits). Marked text must be inside the crop, so a beat
cannot point at something the screen does not say.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import demo_video as dv

W, H, FPS = dv.W, dv.H, dv.FPS
PAGE = (10, 12, 16)
INK, MUTED = (240, 243, 247), (128, 139, 153)
ACCENT = (163, 171, 255)  # rota's periwinkle (tui/theme.py ACCENT)
EDGE = (40, 48, 60)
LEFT, TOP, RIGHT, BOTTOM = 112, 214, W - 112, H - 96  # where the screen goes
HEAD_Y = 118
MAX_CELL = 30.0
EASE_S, MARK_IN_S, DRAW_S, COUNT_S = 0.35, 0.18, 0.7, 0.8
NOTE = "Real usage, names replaced"
BARS = set(dv.EIGHTHS_UP)
SHEET_W = 640


@cache
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for path in (Path("~/Library/Fonts").expanduser() / name, Path(name)):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return dv.sans(size, "Bold" in name)


def head_font(size: int = 84) -> ImageFont.FreeTypeFont:
    return font("InterDisplay-Bold.otf", size)


def body_font(size: int) -> ImageFont.FreeTypeFont:
    return font("Inter-Medium.otf", size)


# -- beats ------------------------------------------------------------------------


@dataclass
class Frame:
    screen: str
    crop: list[float]
    hold: float
    mark: list[str] = field(default_factory=list)
    draw: list[int] | None = None


@dataclass
class Beat:
    head: str
    frames: list[Frame]
    count: str = ""

    @property
    def hold(self) -> float:
        return sum(f.hold for f in self.frames)


def beats_of(spec: list[dict]) -> list[Beat]:
    return [Beat(b["head"], [Frame(**f) for f in b["frames"]], b.get("count", "")) for b in spec]


class Screens:
    """The captured screens, drawn once (and once per drawn-in column)."""

    def __init__(self, story: dict, root: Path) -> None:
        self.sizes = {s["id"]: s["size"] for s in story["scenes"]}
        self.root = root

    def size(self, name: str) -> list[int]:
        return self.sizes[name.rsplit("-", 1)[0]]

    @cache
    def get(self, name: str, upto: int | None = None, rows: tuple[int, int] | None = None) -> dv.Screen:
        hide = None
        if upto is not None and rows is not None:
            first, last = rows

            def hide(c: int, r: int, ch: str) -> bool:
                return first <= r <= last and c >= upto and ch in BARS
        return dv.read_screen(self.root / f"{name}.ansi", self.size(name), hide)


# -- drawing ----------------------------------------------------------------------


def fit_cell(crop: list[float]) -> float:
    _, _, cols, rows = crop
    return min((RIGHT - LEFT) / cols, (BOTTOM - TOP) / rows / 2, MAX_CELL)


def place(crop: list[float], cell: float) -> tuple[int, int, int, int]:
    w, h = round(crop[2] * cell), round(crop[3] * cell * 2)
    return LEFT + (RIGHT - LEFT - w) // 2, TOP + (BOTTOM - TOP - h) // 2, w, h


def headline(draw: ImageDraw.ImageDraw, text: str, count: str, t: float) -> None:
    size = 84
    while draw.textlength(text, font=head_font(size)) > RIGHT - LEFT:
        size -= 2
    shown = text
    if count and count in text:
        digits = int(re.sub(r"\D", "", count))
        k = min(1.0, t / COUNT_S)
        k = 1 - (1 - k) ** 3
        start = 10 ** (len(str(digits)) - 1)  # same digit count: the line does not jump
        now = round(start + (digits - start) * k)
        ticking = re.sub(r"\d[\d,]*", f"{now:,}", count, count=1)
        shown = text.replace(count, ticking)
    x = LEFT
    if count and count in text:
        before, _, after = text.partition(count)
        ticking = shown[len(before):len(shown) - len(after)]
        f = head_font(size)
        draw.text((x, HEAD_Y), before, font=f, fill=INK, anchor="ls")
        x += draw.textlength(before, font=f)
        draw.text((x, HEAD_Y), ticking, font=f, fill=ACCENT, anchor="ls")
        x += draw.textlength(count, font=f)  # the final width: nothing after it jumps
        draw.text((x, HEAD_Y), after, font=f, fill=INK, anchor="ls")
        return
    draw.text((x, HEAD_Y), shown, font=head_font(size), fill=INK, anchor="ls")


def chrome(draw: ImageDraw.ImageDraw) -> None:
    draw.text((LEFT, H - 44), NOTE, font=body_font(26), fill=MUTED, anchor="ls")
    draw.text((RIGHT, H - 44), "rota", font=font("InterDisplay-Bold.otf", 34), fill=ACCENT, anchor="rs")


def panel(screen: dv.Screen, crop: list[float], cell: float) -> tuple[Image.Image, tuple[int, int]]:
    x, y, cols, rows = crop
    ox, oy, w, h = place(crop, cell)
    part = screen.image.resize(
        (w, h), Image.Resampling.LANCZOS,
        box=(x * dv.CELL_W, y * dv.CELL_H, (x + cols) * dv.CELL_W, (y + rows) * dv.CELL_H))
    return part, (ox, oy)


def page(beat: Beat, frame: Frame, screen: dv.Screen, crop: list[float], t: float, marks: float) -> Image.Image:
    """One frame: ``t`` seconds into the beat, marks drawn at strength ``marks``."""
    out = Image.new("RGB", (W, H), PAGE)
    draw = ImageDraw.Draw(out)
    headline(draw, beat.head, beat.count, t)
    chrome(draw)
    cell = fit_cell(crop)
    part, (ox, oy) = panel(screen, crop, cell)
    pad = 18
    draw.rounded_rectangle((ox - pad, oy - pad, ox + part.width + pad, oy + part.height + pad),
                           radius=16, fill=(17, 21, 28), outline=EDGE, width=2)
    out.paste(part, (ox, oy))
    if marks > 0:
        colour = tuple(round(p + (a - p) * marks) for a, p in zip(ACCENT, (17, 21, 28)))
        for text in frame.mark:
            col, row, n = screen.find(text, crop)
            box = (ox + (col - crop[0]) * cell - 8, oy + (row - crop[1]) * cell * 2 - 4,
                   ox + (col + n - crop[0]) * cell + 8, oy + (row + 1 - crop[1]) * cell * 2 + 4)
            draw.rounded_rectangle(box, radius=10, outline=colour, width=4)
    return out


def check(beats: list[Beat], screens: Screens) -> None:
    for beat in beats:
        assert len(beat.head.split()) <= 6, f"headline over six words: {beat.head!r}"
        for f in beat.frames:
            for text in f.mark:
                screens.get(f.screen).find(text, f.crop)


def timeline(beats: list[Beat], screens: Screens, end: dict, end_hold: float) -> Iterator[tuple[Image.Image, int]]:
    """``(page, repeats)`` in order."""
    for beat in beats:
        t, before = 0.0, None
        for f in beat.frames:
            n = round(f.hold * FPS)
            eased = min(round(EASE_S * FPS), n // 2) if before and before.crop != f.crop else 0
            for i in range(eased):
                k = (i + 1) / (eased + 1)
                k = k * k * (3 - 2 * k)
                at = [a + (b - a) * k for a, b in zip(before.crop, f.crop)]
                yield page(beat, f, screens.get(f.screen), at, t, 0), 1
                t += 1 / FPS
            for i in range(eased, n):
                lively = (beat.count and t < COUNT_S) or i - eased < round(MARK_IN_S * FPS) or (
                    f.draw and t < DRAW_S)
                screen = screens.get(f.screen)
                if f.draw:
                    left, right = f.crop[0], f.crop[0] + f.crop[2]
                    k = min(1.0, (i - eased) / (DRAW_S * FPS))
                    upto = round(left + (right - left) * (1 - (1 - k) ** 2))
                    screen = screens.get(f.screen, upto, tuple(f.draw)) if k < 1 else screen
                marks = 0.0 if not f.mark else min(1.0, (i - eased + 1) / (MARK_IN_S * FPS))
                if lively:
                    yield page(beat, f, screen, f.crop, t, marks), 1
                    t += 1 / FPS
                    continue
                yield page(beat, f, screen, f.crop, t, marks), n - i
                t += (n - i) / FPS
                break
            before = f
    card = end_card(end)
    yield card, round(end_hold * FPS)


def end_card(end: dict) -> Image.Image:
    out = Image.new("RGB", (W, H), PAGE)
    draw = ImageDraw.Draw(out)
    draw.text((W / 2, 360), "rota", font=font("InterDisplay-Bold.otf", 190), fill=ACCENT, anchor="ms")
    draw.text((W / 2, 470), end["title"], font=head_font(64), fill=INK, anchor="ms")
    size = 40
    while draw.textlength(end["command"], font=dv.mono(size)) > W - 320:
        size -= 1
    box_w = draw.textlength(end["command"], font=dv.mono(size)) + 96
    draw.rounded_rectangle(((W - box_w) / 2, 576, (W + box_w) / 2, 696), radius=18,
                           fill=(17, 21, 28), outline=EDGE, width=2)
    draw.text((W / 2, 636), end["command"], font=dv.mono(size), fill=INK, anchor="mm")
    draw.text((W / 2, 800), end["note"], font=body_font(38), fill=MUTED, anchor="ms")
    draw.text((LEFT, H - 44), NOTE, font=body_font(26), fill=MUTED, anchor="ls")
    return out


# -- output -----------------------------------------------------------------------


def encode(frames: Iterator[tuple[Image.Image, int]], out: Path) -> float:
    proc = subprocess.Popen(dv.ffmpeg(
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart",
        "-an", "-map_metadata", "-1", str(out)), stdin=subprocess.PIPE)
    total = 0
    for image, n in frames:
        raw = image.tobytes()
        for _ in range(n):
            proc.stdin.write(raw)
        total += n
    proc.stdin.close()
    if proc.wait():
        raise SystemExit("ffmpeg could not write the video")
    return total / FPS


def sheet(beats: list[Beat], screens: Screens, end: dict, out: Path) -> None:
    """Each beat's settled last frame, labelled with when the beat starts."""
    tiles, at = [], 0.0
    for beat in beats:
        f = beat.frames[-1]
        tiles.append((at, page(beat, f, screens.get(f.screen), f.crop, 99, 1.0)))
        print(f"{int(at // 60)}:{at % 60:04.1f}  {beat.head}")
        at += beat.hold
    tiles.append((at, end_card(end)))
    print(f"{int(at // 60)}:{at % 60:04.1f}  end card")
    h = SHEET_W * H // W
    grid = Image.new("RGB", (3 * SHEET_W, (len(tiles) + 2) // 3 * h), PAGE)
    for i, (start, image) in enumerate(tiles):
        tile = image.resize((SHEET_W, h), Image.Resampling.LANCZOS)
        ImageDraw.Draw(tile).text((SHEET_W - 12, 30), f"{int(start // 60)}:{int(start % 60):02d}",
                                  font=body_font(22), fill=ACCENT, anchor="rs")
        grid.paste(tile, (i % 3 * SHEET_W, i // 3 * h))
    grid.save(out, optimize=True)


def still(screens: Screens, name: str, crop: list[int] | None, out: Path, scale: float = 0.75) -> None:
    """A screen (or part of it) as a PNG, on the same page colour with a margin."""
    image = screens.get(name).image
    if crop:
        x, y, cols, rows = crop
        image = image.crop((x * dv.CELL_W, y * dv.CELL_H, (x + cols) * dv.CELL_W, (y + rows) * dv.CELL_H))
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    image.save(out, optimize=True)


def main() -> None:
    story_path, root, out = map(Path, sys.argv[1:4])
    story = json.loads(story_path.read_text())
    out.mkdir(parents=True, exist_ok=True)
    screens = Screens(story, root)
    ad, loop = beats_of(story["ad"]), beats_of(story["gif"])
    check(ad + loop, screens)
    end = story["end"]
    sheet(ad, screens, end, out / "contact-sheet-ad.png")
    for name, spec in story.get("stills", {}).items():
        still(screens, spec["screen"], spec.get("crop"), out / name)
    seconds = encode(timeline(ad, screens, end, end["hold"]), out / "rota-ad.mp4")
    print(f"rota-ad.mp4: {seconds:.1f} s")
    gif_src = out / "gif-source.mp4"
    seconds = encode(timeline(loop, screens, end, end["gif_hold"]), gif_src)
    print(f"gif source: {seconds:.1f} s")
    dv.write_gif(gif_src, out / "rota-ad.gif", max_bytes=5_000_000,
                 tries=((960, 12), (960, 10), (960, 8)))
    gif_src.unlink()


if __name__ == "__main__":
    main()
