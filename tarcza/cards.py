from __future__ import annotations

import math
import sys
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]

BASE_W, BASE_H = 940, 310
OUT_SCALE = 2
SUPERSAMPLE = 2
K = OUT_SCALE * SUPERSAMPLE

GREEN = (67, 181, 97)
CARD_BG = (18, 18, 18)
CARD_BORDER = (42, 42, 42)
BOX_BG = (21, 21, 21)
BOX_BORDER = (58, 58, 58)
DIVIDER = (44, 44, 44)
TITLE = (236, 236, 236)
VALUE = (242, 242, 242)
LABEL = (170, 170, 170)
FOOTER = (155, 155, 155)
AVATAR_RING = (38, 38, 38)

_FONT_FILES = {
    "bold": ["DejaVuSans-Bold.ttf", "verdanab.ttf", "segoeuib.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"],
    "regular": ["DejaVuSans.ttf", "verdana.ttf", "segoeui.ttf", "arial.ttf", "LiberationSans-Regular.ttf"],
}
_FONT_DIRS = [
    ROOT / "fonts",
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/usr/share/fonts/TTF"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("C:/Windows/Fonts"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
]
_SIZE_FIX = {"verdana.ttf": 0.94, "verdanab.ttf": 0.92}


@lru_cache(maxsize=None)
def _font_path(kind: str) -> Optional[Path]:
    for name in _FONT_FILES[kind]:
        for folder in _FONT_DIRS:
            path = folder / name
            if path.exists():
                return path
    return None


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(kind)
    if path is None:
        return ImageFont.load_default(size=size)
    return ImageFont.truetype(str(path), max(1, round(size * _SIZE_FIX.get(path.name.lower(), 1.0))))


def font_name() -> str:
    path = _font_path("bold")
    return path.name if path else "domyślna czcionka Pillow"


def s(value: float) -> int:
    return round(value * K)


def _fit(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: float) -> str:
    if draw.textlength(text, font=fnt) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_width:
        text = text[:-1]
    return text + "…"


def _star(cx: float, cy: float, outer: float, inner: float) -> list[tuple[float, float]]:
    points = []
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _circle_image(data: bytes, size: int) -> Image.Image:
    img = Image.open(BytesIO(data)).convert("RGBA")
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _rounded_image(data: bytes, size: int, radius: int) -> Image.Image:
    img = Image.open(BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def render_card(
    title: str,
    fields: Sequence[tuple[str, str]],
    *,
    avatar: Optional[bytes] = None,
    footer: str = "",
    footer_icon: Optional[bytes] = None,
    accent: tuple[int, int, int] = GREEN,
) -> bytes:
    width, height = s(BASE_W), s(BASE_H)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle((s(16), s(16), s(924), s(294)), radius=s(5), fill=CARD_BG, outline=CARD_BORDER, width=s(1))
    d.rounded_rectangle((s(26), s(26), s(30), s(284)), radius=s(2), fill=accent)

    cx, cy, r = 470, 75, 29
    d.ellipse((s(cx - r), s(cy - r), s(cx + r), s(cy + r)), outline=accent, width=s(2.5))
    d.polygon([(s(x), s(y)) for x, y in _star(cx, cy + 1, 16.5, 7)], fill=accent)

    d.text((s(470), s(131)), title, font=font("bold", s(27)), fill=TITLE, anchor="mm")

    ax, ay, ar = 872, 74, 29
    d.ellipse((s(ax - ar), s(ay - ar), s(ax + ar), s(ay + ar)), fill=AVATAR_RING)
    if avatar:
        try:
            face = _circle_image(avatar, s(2 * (ar - 1.5)))
            img.alpha_composite(face, (s(ax - ar + 1.5), s(ay - ar + 1.5)))
        except Exception:
            d.ellipse((s(ax - ar + 1), s(ay - ar + 1), s(ax + ar - 1), s(ay + ar - 1)), fill=(80, 80, 80))
    else:
        d.ellipse((s(ax - ar + 1), s(ay - ar + 1), s(ax + ar - 1), s(ay + ar - 1)), fill=(80, 80, 80))

    fields = list(fields)[:3] or [("", "")]
    left, right, gap = 39, 900, 9
    box_w = (right - left - gap * (len(fields) - 1)) / len(fields)
    label_font = font("regular", s(12.5))
    value_font = font("bold", s(18.5))
    for i, (label, value) in enumerate(fields):
        bx = left + i * (box_w + gap)
        d.rounded_rectangle(
            (s(bx), s(161), s(bx + box_w), s(234)), radius=s(4), fill=BOX_BG, outline=BOX_BORDER, width=s(1.5)
        )
        d.text((s(bx + 13), s(182)), label, font=label_font, fill=LABEL, anchor="lm")
        d.text((s(bx + 13), s(206)), _fit(d, value, value_font, s(box_w - 26)), font=value_font, fill=VALUE, anchor="lm")

    d.line((s(32), s(256), s(908), s(256)), fill=DIVIDER, width=s(1.5))
    if footer or footer_icon:
        footer_font = font("regular", s(13.5))
        text = _fit(d, footer, footer_font, s(800))
        icon_size = s(16) if footer_icon else 0
        spacing = s(8) if footer_icon and text else 0
        total = icon_size + spacing + d.textlength(text, font=footer_font)
        x = (width - total) / 2
        if footer_icon:
            try:
                icon = _rounded_image(footer_icon, icon_size, s(3))
                img.alpha_composite(icon, (round(x), s(274) - icon_size // 2))
            except Exception:
                pass
        d.text((x + icon_size + spacing, s(274)), text, font=footer_font, fill=FOOTER, anchor="lm")

    img = img.resize((BASE_W * OUT_SCALE, BASE_H * OUT_SCALE), Image.LANCZOS)
    out = BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


if __name__ == "__main__":
    avatar_bytes = Path(sys.argv[1]).read_bytes() if len(sys.argv) > 1 else None
    png = render_card(
        "Awans poziomu",
        [("Użytkownik", "czekolotsoko"), ("Nowy poziom", "64"), ("Exp", "0 / 250")],
        avatar=avatar_bytes,
        footer="Statystyki • 2026-11-07 00:53",
    )
    (ROOT / "podglad_karty.png").write_bytes(png)
    print("Zapisano podglad_karty.png, czcionka:", font_name())
