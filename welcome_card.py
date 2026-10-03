"""The welcome card: a branded PNG with the new member's avatar and name."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parent / "assets"
FONT_BOLD = ASSETS / "fonts" / "inter-extrabold.ttf"
FONT_SEMI = ASSETS / "fonts" / "inter-semibold.ttf"
FONT_MED = ASSETS / "fonts" / "inter-medium.ttf"

W, H = 1100, 440
NAVY_DARK = (18, 42, 79)
NAVY = (27, 75, 143)
RED = (224, 36, 31)
GOLD = (230, 185, 78)
WHITE = (255, 255, 255)
MUTED = (170, 189, 214)
TIER_COLORS = {"supporter": (77, 143, 224), "patron": RED, "founder": GOLD, "team": (230, 237, 245)}


@lru_cache(maxsize=None)
def _glyphs(path: Path) -> frozenset[int]:
    return frozenset(TTFont(path).getBestCmap())


def printable_name(display_name: str, username: str, font_path: Path = FONT_BOLD) -> str:
    """The bundled Inter is a Latin subset. A name it can't draw would render as
    boxes, so fall back to the username (Discord usernames are ASCII)."""
    glyphs = _glyphs(font_path)
    name = display_name.strip()
    if name and all(ord(c) in glyphs or c.isspace() for c in name):
        return name
    return username


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, size: int, max_width: int) -> tuple[ImageFont.FreeTypeFont, str]:
    """Shrink the font until the text fits; if even the floor size is too wide, truncate."""
    while size > 34:
        font = ImageFont.truetype(str(path), size)
        if draw.textlength(text, font=font) <= max_width:
            return font, text
        size -= 4
    font = ImageFont.truetype(str(path), size)
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return font, text + "…"


def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    out = Image.new("RGBA", (size, size))
    out.paste(img, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return out


def render(avatar_png: bytes | None, name: str, member_number: int, tier: str | None = None) -> bytes:
    card = Image.new("RGBA", (W, H), NAVY_DARK)

    # A navy glow from the top right.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((W - 620, -260, W + 240, 520), fill=NAVY + (255,))
    card.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))

    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 0, 14, H), fill=RED)  # the red spine

    # Avatar with a ring in the member's tier colour (white if none).
    ring = TIER_COLORS.get(tier or "", WHITE)
    size, x, y = 230, 80, (H - 230) // 2
    draw.ellipse((x - 9, y - 9, x + size + 9, y + size + 9), fill=ring)
    draw.ellipse((x - 3, y - 3, x + size + 3, y + size + 3), fill=NAVY_DARK)
    if avatar_png:
        try:
            card.alpha_composite(_circle(Image.open(io.BytesIO(avatar_png)), size), (x, y))
        except OSError:
            avatar_png = None
    if not avatar_png:
        draw.ellipse((x, y, x + size, y + size), fill=NAVY)

    tx = x + size + 60
    max_w = W - tx - 60
    draw.text((tx, 108), "WELCOME TO THE ELECTINDEX COMMUNITY", font=ImageFont.truetype(str(FONT_SEMI), 22), fill=MUTED)
    name_font, shown = _fit(draw, name, FONT_BOLD, 74, max_w)
    draw.text((tx, 226), shown, font=name_font, fill=WHITE, anchor="ls")  # fixed baseline, whatever the size
    draw.rectangle((tx, 258, tx + 64, 263), fill=RED)

    line = f"You're our {ordinal(member_number)} member"
    draw.text((tx, 284), line, font=ImageFont.truetype(str(FONT_MED), 30), fill=WHITE)
    if tier in ("supporter", "patron", "founder"):
        label = tier.upper()
        f = ImageFont.truetype(str(FONT_BOLD), 20)
        bw = int(draw.textlength(label, font=f)) + 36
        draw.rounded_rectangle((tx, 336, tx + bw, 372), radius=18, fill=TIER_COLORS[tier])
        draw.text((tx + 18, 343), label, font=f, fill=NAVY_DARK if tier == "founder" else WHITE)

    out = io.BytesIO()
    card.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
