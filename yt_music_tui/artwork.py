"""Album artwork downloader, truecolor half-block renderer, and dynamic palette extractor."""

from __future__ import annotations

import colorsys
import io
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops
from rich.style import Style
from rich.text import Text


@dataclass(frozen=True)
class ThemePalette:
    name: str
    bg_main: str
    bg_panel: str
    bg_highlight: str
    primary: str
    secondary: str
    playing: str
    paused: str
    fg: str
    fg_muted: str


DRACULA_PALETTE = ThemePalette(
    name="Dracula",
    bg_main="#1e1f29",
    bg_panel="#282a36",
    bg_highlight="#44475a",
    primary="#bd93f9",
    secondary="#ff79c6",
    playing="#50fa7b",
    paused="#ffb86c",
    fg="#f8f8f2",
    fg_muted="#6272a4",
)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _clamp_hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return _rgb_to_hex(int(r * 255), int(g * 255), int(b * 255))


def _trim_black_borders(img: Image.Image) -> Image.Image:
    """Removes letterboxing (black bars on 16:9 YouTube thumbnails) and center-crops to square."""
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, (12, 12, 12))
    diff = ImageChops.subtract(rgb, bg)
    bbox = diff.getbbox()
    if bbox:
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        if bw >= rgb.width * 0.4 and bh >= rgb.height * 0.4:
            rgb = rgb.crop(bbox)

    w, h = rgb.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return rgb.crop((left, top, left + side, top + side))


def fetch_image(url: str, timeout: float = 2.5) -> Image.Image | None:
    if not url:
        return None
    try:
        if url.startswith("file://"):
            local_path = Path(urllib.parse.unquote(url[7:]))
            if local_path.exists():
                return Image.open(local_path).convert("RGB")
            return None
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yt-music-tui/0.2"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def extract_dynamic_palette(img: Image.Image) -> ThemePalette:
    """Extracts an Ambient/Material-style dark palette from album artwork."""
    small = _trim_black_borders(img).resize((36, 36), Image.Resampling.BILINEAR)
    pixels = list(small.getdata())

    # Score pixels by vibrancy (saturation + balanced brightness)
    scored: list[tuple[float, float, float, float]] = []
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if v < 0.15 or (s < 0.12 and v > 0.85):
            continue
        score = (s * 1.6) + (1.0 - abs(v - 0.65))
        scored.append((score, h, s, v))

    if not scored:
        return DRACULA_PALETTE

    scored.sort(key=lambda x: x[0], reverse=True)
    _, h1, s1, v1 = scored[0]

    # Pick a secondary hue distinct from primary if possible
    h2, s2, v2 = (h1 + 0.12) % 1.0, max(0.55, s1), min(0.95, v1 + 0.1)
    for _, hc, sc, vc in scored[1:]:
        hue_dist = min(abs(hc - h1), 1.0 - abs(hc - h1))
        if hue_dist > 0.08 and sc > 0.25:
            h2, s2, v2 = hc, sc, vc
            break

    primary = _clamp_hsv_to_hex(h1, max(0.55, min(0.88, s1)), max(0.78, min(0.96, v1)))
    secondary = _clamp_hsv_to_hex(h2, max(0.50, min(0.85, s2)), max(0.78, min(0.95, v2)))
    bg_main = _clamp_hsv_to_hex(h1, min(0.38, s1 * 0.55), 0.11)
    bg_panel = _clamp_hsv_to_hex(h1, min(0.42, s1 * 0.60), 0.16)
    bg_highlight = _clamp_hsv_to_hex(h1, min(0.45, s1 * 0.65), 0.28)
    fg_muted = _clamp_hsv_to_hex(h1, min(0.30, s1 * 0.45), 0.62)

    return ThemePalette(
        name="Dynamic Artwork",
        bg_main=bg_main,
        bg_panel=bg_panel,
        bg_highlight=bg_highlight,
        primary=primary,
        secondary=secondary,
        playing="#50fa7b",
        paused="#ffb86c",
        fg="#f8f8f2",
        fg_muted=fg_muted,
    )


def render_image_halfblocks(img: Image.Image, width_cells: int = 22, height_cells: int = 11) -> Text:
    """Renders a PIL image into 24-bit Truecolor Unicode half-blocks (▀)."""
    square = _trim_black_borders(img)
    px_w = max(4, width_cells)
    px_h = max(4, height_cells * 2)
    resized = square.resize((px_w, px_h), Image.Resampling.LANCZOS)

    out = Text()
    for y in range(0, px_h, 2):
        for x in range(px_w):
            r1, g1, b1 = resized.getpixel((x, y))
            if y + 1 < px_h:
                r2, g2, b2 = resized.getpixel((x, y + 1))
            else:
                r2, g2, b2 = r1, g1, b1
            style = Style(
                color=_rgb_to_hex(r1, g1, b1),
                bgcolor=_rgb_to_hex(r2, g2, b2),
            )
            out.append("▀", style=style)
        if y + 2 < px_h:
            out.append("\n")
    return out


def render_placeholder_vinyl(
    palette: ThemePalette = DRACULA_PALETTE,
    width_cells: int = 22,
    height_cells: int = 11,
) -> Text:
    """Renders a stylized vinyl disc placeholder when no artwork is available."""
    px_w = width_cells
    px_h = height_cells * 2
    cx = (px_w - 1) / 2.0
    cy = (px_h - 1) / 2.0
    max_r = min(cx, cy) * 0.92

    img = Image.new("RGB", (px_w, px_h), (30, 31, 41))
    prim = tuple(int(palette.primary.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    sec = tuple(int(palette.secondary.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    bg = tuple(int(palette.bg_panel.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))

    for y in range(px_h):
        for x in range(px_w):
            dx = x - cx
            dy = y - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > max_r:
                img.putpixel((x, y), bg)
            elif dist < max_r * 0.14:
                img.putpixel((x, y), bg)
            elif dist < max_r * 0.38:
                img.putpixel((x, y), prim if (x + y) % 2 == 0 else sec)
            else:
                ring = int(dist * 2.2) % 3
                shade = 38 if ring == 0 else (52 if ring == 1 else 28)
                img.putpixel((x, y), (shade, shade, min(255, shade + 14)))

    return render_image_halfblocks(img, width_cells=width_cells, height_cells=height_cells)
