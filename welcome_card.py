"""The welcome card: a branded PNG with the new member's avatar and name."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from card_text import FontFace, clusters, paste_runs, plan_runs, render_runs

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


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _name_runs(display_name: str, username: str):
    """Runs for the display name in any script; the username if some character
    has no font at all (Discord usernames are ASCII, which Inter always covers)."""
    primary = FontFace(FONT_BOLD)
    for candidate in (display_name.strip(), username):
        if candidate and (runs := plan_runs(candidate, primary)) is not None:
            return runs, candidate
    return [(primary, username)], username


def _fit_name(display_name: str, username: str, max_width: int, sizes=range(74, 33, -4)):
    """Shrink until the name fits; at the smallest size, drop characters and add an ellipsis."""
    runs, text = _name_runs(display_name, username)
    for size in sizes:
        rendered, width = render_runs(runs, size, WHITE)
        if width <= max_width:
            return rendered
    primary = FontFace(FONT_BOLD)
    parts = clusters(text)
    while parts:
        parts.pop()
        runs = plan_runs("".join(parts).rstrip() + "…", primary) or [(primary, "…")]
        rendered, width = render_runs(runs, size, WHITE)
        if width <= max_width:
            return rendered
    return render_runs([(primary, "…")], size, WHITE)[0]


def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4, size * 4), fill=255)
    out = Image.new("RGBA", (size, size))
    out.paste(img, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return out


def render(avatar_png: bytes | None, display_name: str, username: str, member_number: int, tier: str | None = None) -> bytes:
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
    paste_runs(card, _fit_name(display_name, username, max_w), tx, 226)  # fixed baseline, whatever the size
    draw = ImageDraw.Draw(card)
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
