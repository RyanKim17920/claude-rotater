"""Turn demo_capture.py's screens into the README video, GIF and stills.

    uv run --with pillow python scripts/demo_video.py STORY SCREENS_DIR OUT_DIR

Needs ffmpeg. STORY is the one given to demo_capture.py; here its steps may
also carry ``shots``, shown in order over that step's screen::

    {"hold": seconds, "crop": [col, row, cols, rows],
     "caption": [headline, detail], "mark": ["text on the screen", ...]}

and the story itself ``stills`` ({file name: screen}) and ``end`` (the last
card). A caption lives outside the terminal. Marked text must be inside the
crop, so a shot cannot point at something the screen does not say.

The screens are drawn cell by cell: braille, block and line glyphs as
shapes (as terminals do), so charts stay solid at README width.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rich.text import Text

W, H, FPS = 1920, 1080, 30
STAGE = (30, 206, W - 30, 1032)  # where the terminal goes, under the caption
MAX_CELL_W = 30.0  # on screen: a small crop is not blown up past this
EASE_S = 0.5
CELL_W, CELL_H = 24, 48  # the size screens are drawn at
BG, INK, MUTED, MARK = (10, 13, 18), (242, 245, 248), (154, 166, 181), (255, 199, 87)
SCREEN_BG, SCREEN_FG = (25, 32, 42), (221, 227, 234)
FOOTER = "real usage, replayed · names replaced · costs are API-equivalent estimates"
GIF_W, GIF_FPS, GIF_MAX_BYTES = 960, 10, 10_000_000
SHEET_W = 640

MONO = ("/System/Library/Fonts/Menlo.ttc", "DejaVuSansMono.ttf")
SANS = {False: ("Inter-Regular.otf", "Helvetica.ttc", "DejaVuSans.ttf"),
        True: ("Inter-Bold.otf", "HelveticaNeue.ttc", "DejaVuSans-Bold.ttf")}
POSITION = re.compile(r"\x1b\[(\d+);(\d+)H")
EIGHTHS_UP, EIGHTHS_LEFT = "▁▂▃▄▅▆▇█", "▏▎▍▌▋▊▉█"
BRAILLE = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3))
VERTICAL = {"│": 1, "╎": 2, "┊": 4}  # dashes per cell
HORIZONTAL = {"─": 1, "╌": 2, "┄": 3}


@cache
def mono(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in MONO:
        try:
            return ImageFont.truetype(name, size, index=int(bold and name.endswith(".ttc")))
        except OSError:
            continue
    raise SystemExit("no monospaced font found")


@cache
def sans(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    home = Path("~/Library/Fonts").expanduser()
    for name in SANS[bold]:
        for path in (home / name, name):
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    raise SystemExit("no caption font found")


# -- the terminal -----------------------------------------------------------------


def draw_cell(draw: ImageDraw.ImageDraw, x: int, y: int, ch: str, fg, bold: bool) -> None:
    w, h = CELL_W, CELL_H
    code = ord(ch)
    if 0x2800 <= code < 0x2900:
        for bit, (col, row) in enumerate(BRAILLE):
            if code - 0x2800 >> bit & 1:
                cx, cy = x + w * (col + 0.5) / 2, y + h * (row + 0.5) / 4
                draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=fg)
    elif ch in EIGHTHS_UP:
        draw.rectangle((x, y + h * (7 - EIGHTHS_UP.index(ch)) / 8, x + w - 1, y + h - 1), fill=fg)
    elif ch in EIGHTHS_LEFT:
        draw.rectangle((x, y, x + w * (EIGHTHS_LEFT.index(ch) + 1) / 8 - 1, y + h - 1), fill=fg)
    elif ch == "▕":
        draw.rectangle((x + w - 3, y, x + w - 1, y + h - 1), fill=fg)
    elif ch in VERTICAL:
        n = VERTICAL[ch]
        for i in range(n):
            end = (i + (1.0 if n == 1 else 0.6)) / n
            draw.rectangle((x + w // 2 - 1, y + h * i / n, x + w // 2 + 1, y + h * end - 1), fill=fg)
    elif ch in HORIZONTAL:
        n = HORIZONTAL[ch]
        for i in range(n):
            end = (i + (1.0 if n == 1 else 0.6)) / n
            draw.rectangle((x + w * i / n, y + h // 2 - 1, x + w * end - 1, y + h // 2 + 1), fill=fg)
    elif ch == "┤":
        draw.rectangle((x + w // 2 - 1, y, x + w // 2 + 1, y + h - 1), fill=fg)
        draw.rectangle((x, y + h // 2 - 1, x + w // 2, y + h // 2 + 1), fill=fg)
    elif ch == "·":
        # a projection's dot: round, and big enough to see
        draw.ellipse((x + w / 2 - 4, y + h / 2 - 4, x + w / 2 + 4, y + h / 2 + 4), fill=fg)
    elif not ch.isspace():
        draw.text((x, y + 36), ch, font=mono(40, bold), fill=fg, anchor="ls")


@dataclass
class Screen:
    image: Image.Image
    lines: list[str]

    def find(self, text: str, crop: list[float]) -> tuple[int, int, int]:
        """``(col, row, cols)`` of ``text`` inside ``crop``."""
        x, y, w, h = crop
        for row in range(int(y), min(int(y + h), len(self.lines))):
            col = self.lines[row].find(text, int(x))
            if col >= 0 and col + len(text) <= x + w:
                return col, row, len(text)
        raise SystemExit(f"a marked text is not on its screen: {text!r}")


def read_screen(path: Path, size: list[int], hide=None) -> Screen:
    """``hide(col, row, ch)`` true leaves that cell's glyph out (its colour stays)."""
    cols, rows = size
    image = Image.new("RGB", (cols * CELL_W, rows * CELL_H), SCREEN_BG)
    draw = ImageDraw.Draw(image)
    grid = [[" "] * cols for _ in range(rows)]
    parts = POSITION.split(path.read_text())
    for row, col, body in zip(parts[1::3], parts[2::3], parts[3::3]):
        text = Text.from_ansi(body.replace("\n", ""), no_wrap=True)
        styles: list = [None] * len(text.plain)
        for span in text.spans:
            for i in range(span.start, min(span.end, len(styles))):
                styles[i] = span.style if styles[i] is None else styles[i] + span.style
        for i, (ch, style) in enumerate(zip(text.plain, styles)):
            c, r = int(col) - 1 + i, int(row) - 1
            if c >= cols or r >= rows:
                continue
            grid[r][c] = ch
            x, y = c * CELL_W, r * CELL_H
            fg, bg = SCREEN_FG, SCREEN_BG
            bold = under = False
            if style is not None:
                fg = style.color.get_truecolor() if style.color else fg
                bg = style.bgcolor.get_truecolor(foreground=False) if style.bgcolor else bg
                if style.reverse:
                    fg, bg = bg, fg
                bold, under = bool(style.bold), bool(style.underline)
            draw.rectangle((x, y, x + CELL_W - 1, y + CELL_H - 1), fill=tuple(bg))
            if hide is not None and hide(c, r, ch):
                continue
            draw_cell(draw, x, y, ch, tuple(fg), bold)
            if under:
                draw.rectangle((x, y + 41, x + CELL_W - 1, y + 42), fill=tuple(fg))
    return Screen(image, ["".join(line) for line in grid])


# -- frames -----------------------------------------------------------------------


@dataclass
class Shot:
    scene: str
    screen: Screen
    hold: float
    crop: list[float]
    caption: list[str]
    mark: list[str]


def shots_of(story: dict, screens: Path) -> list[Shot]:
    out = []
    for scene in story["scenes"]:
        for i, step in enumerate(scene["steps"]):
            if not step.get("shots"):
                continue
            screen = read_screen(screens / f"{scene['id']}-{i:03d}.ansi", scene["size"])
            for s in step["shots"]:
                crop = s.get("crop") or [0, 0, *scene["size"]]
                out.append(Shot(scene["id"], screen, s["hold"], crop,
                                s.get("caption", ["", ""]), s.get("mark", [])))
    return out


def fitted(draw: ImageDraw.ImageDraw, y: int, text: str, size: int, bold: bool, fill) -> None:
    while draw.textlength(text, font=sans(size, bold)) > W - 100:
        size -= 2
    draw.text((W / 2, y), text, font=sans(size, bold), fill=fill, anchor="mm")


def frame(shot: Shot, crop: list[float], *, marks: bool) -> Image.Image:
    page = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(page)
    head, detail = (*shot.caption, "", "")[:2]
    fitted(draw, 74, head, 66, True, INK)
    fitted(draw, 151, detail, 44, False, MUTED)
    fitted(draw, 1057, FOOTER, 27, False, MUTED)
    x, y, cols, rows = crop
    left, top, right, bottom = STAGE
    cell = min((right - left) / cols, (bottom - top) / rows / 2, MAX_CELL_W)
    w, h = round(cols * cell), round(rows * cell * 2)
    ox, oy = left + (right - left - w) // 2, top + (bottom - top - h) // 2
    part = shot.screen.image.resize(
        (w, h), Image.Resampling.LANCZOS,
        box=(x * CELL_W, y * CELL_H, (x + cols) * CELL_W, (y + rows) * CELL_H))
    page.paste(part, (ox, oy))
    draw.rectangle((ox - 2, oy - 2, ox + w + 1, oy + h + 1), outline=(49, 59, 72), width=2)
    for text in shot.mark if marks else ():
        col, row, n = shot.screen.find(text, crop)
        box = (ox + (col - x) * cell - 6, oy + (row - y) * cell * 2 - 3,
               ox + (col + n - x) * cell + 6, oy + (row + 1 - y) * cell * 2 + 3)
        draw.rounded_rectangle(box, radius=10, outline=MARK, width=4)
    return page


def end_card(end: dict) -> Image.Image:
    page = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(page)
    draw.text((W / 2, 250), "rota", font=sans(150, True), fill=(163, 171, 255), anchor="mm")
    fitted(draw, 405, end["title"], 60, True, INK)
    draw.rounded_rectangle((60, 500, W - 60, 648), radius=22, fill=SCREEN_BG)
    size = 48
    while draw.textlength(end["command"], font=mono(size)) > W - 180:
        size -= 1
    draw.text((W / 2, 574), end["command"], font=mono(size), fill=(120, 203, 169), anchor="mm")
    fitted(draw, 740, end["note"], 44, False, MUTED)
    fitted(draw, 1057, FOOTER, 27, False, MUTED)
    return page


def timeline(shots: list[Shot], end: dict) -> Iterator[tuple[Image.Image, int]]:
    """``(page, frames)``: a crop eases into the next one of the same scene."""
    before = None
    for shot in shots:
        n, eased = round(shot.hold * FPS), 0
        if before and before.scene == shot.scene and before.crop != shot.crop:
            eased = min(round(EASE_S * FPS), n // 2)
            for i in range(eased):
                t = (i + 1) / (eased + 1)
                t = t * t * (3 - 2 * t)
                at = [a + (b - a) * t for a, b in zip(before.crop, shot.crop)]
                yield frame(shot, at, marks=False), 1
        yield frame(shot, shot.crop, marks=True), n - eased
        before = shot
    yield end_card(end), round(end["hold"] * FPS)


def ffmpeg(*args: str) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "3", *args]


def write_mp4(shots: list[Shot], end: dict, out: Path) -> None:
    proc = subprocess.Popen(ffmpeg(
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart",
        "-map_metadata", "-1", str(out)), stdin=subprocess.PIPE)
    for page, n in timeline(shots, end):
        raw = page.tobytes()
        for _ in range(n):
            proc.stdin.write(raw)
    proc.stdin.close()
    if proc.wait():
        raise SystemExit("ffmpeg could not write the video")


def write_gif(video: Path, out: Path, max_bytes: int = GIF_MAX_BYTES,
              tries=((GIF_W, GIF_FPS), (GIF_W, 6), (880, 5))) -> None:
    for width, fps in tries:
        scale = f"fps={fps},scale={width}:-2:flags=lanczos"
        palette = out.with_suffix(".palette.png")
        subprocess.run(ffmpeg("-i", str(video), "-vf", f"{scale},palettegen=max_colors=96:stats_mode=full",
                              "-update", "1", "-frames:v", "1", str(palette)), check=True)
        subprocess.run(ffmpeg("-i", str(video), "-i", str(palette), "-lavfi",
                              f"{scale}[v];[v][1:v]paletteuse=dither=none:diff_mode=rectangle",
                              "-loop", "0", str(out)), check=True)
        palette.unlink()
        if out.stat().st_size < max_bytes:
            print(f"gif: {width}px at {fps} fps, {out.stat().st_size} bytes")
            return
    raise SystemExit(f"the GIF does not fit in {max_bytes} bytes")


def write_sheet(shots: list[Shot], end: dict, out: Path) -> None:
    """Every caption's last frame, in order, with the time it starts."""
    tiles: list[tuple[float, Image.Image]] = []
    at = began = 0.0
    for i, shot in enumerate(shots):
        if i and shot.caption != shots[i - 1].caption:
            began = at
        at += shot.hold
        if i + 1 == len(shots) or shots[i + 1].caption != shot.caption:
            tiles.append((began, frame(shot, shot.crop, marks=True)))
            print(f"{int(began // 60)}:{began % 60:04.1f}  {shot.scene:6}  {shot.caption[0]}")
    tiles.append((at, end_card(end)))
    print(f"{int(at // 60)}:{at % 60:04.1f}  end; {at + end['hold']:.1f}s in all")
    h = SHEET_W * H // W
    sheet = Image.new("RGB", (3 * SHEET_W, (len(tiles) + 2) // 3 * h), BG)
    for i, (start, page) in enumerate(tiles):
        tile = page.resize((SHEET_W, h), Image.Resampling.LANCZOS)
        label = ImageDraw.Draw(tile)
        label.text((8, h - 6), f"{int(start // 60)}:{int(start % 60):02d}", font=sans(20, True),
                   fill=MARK, anchor="ls")
        sheet.paste(tile, (i % 3 * SHEET_W, i // 3 * h))
    sheet.save(out, optimize=True)


def write_stills(story: dict, screens: Path, out: Path) -> None:
    sizes = {scene["id"]: scene["size"] for scene in story["scenes"]}
    for name, shot in story.get("stills", {}).items():
        image = read_screen(screens / f"{shot}.ansi", sizes[shot.rsplit("-", 1)[0]]).image
        size = (image.width * 14 // CELL_W, image.height * 14 // CELL_W)
        image.resize(size, Image.Resampling.LANCZOS).save(out / name, optimize=True)


def main() -> None:
    story_path, screens, out = map(Path, sys.argv[1:4])
    story = json.loads(story_path.read_text())
    out.mkdir(parents=True, exist_ok=True)
    shots = shots_of(story, screens)
    for shot in shots:  # a wrong mark fails here, before anything is encoded
        for text in shot.mark:
            shot.screen.find(text, shot.crop)
    write_sheet(shots, story["end"], out / "contact-sheet.png")
    write_stills(story, screens, out)
    write_mp4(shots, story["end"], out / "rota-demo.mp4")
    write_gif(out / "rota-demo.mp4", out / "rota-demo.gif")


if __name__ == "__main__":
    main()
