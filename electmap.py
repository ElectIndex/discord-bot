"""Map images for the election commands: a national state map with an
electoral-vote bar, a single state's county map, and a forecast map."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = Path(__file__).resolve().parent / "assets" / "fonts"
BG = (13, 26, 49)
PANEL = (22, 38, 66)
TEXT = (255, 255, 255)
MUTED = (160, 178, 204)
BORDER = (13, 26, 49)
NO_DATA = (52, 66, 92)
NOT_UP = (40, 55, 82)
THIRD = (201, 162, 39)
D_SHADES = [(28, 60, 120), (63, 108, 181), (137, 169, 216), (196, 212, 238)]  # safe, likely, lean, tilt
R_SHADES = [(158, 28, 28), (201, 95, 95), (226, 160, 160), (242, 204, 204)]
TOSSUP = (165, 150, 190)
MARGIN_LEGEND = [(D_SHADES[0], "D+15"), (D_SHADES[1], "D+5"), (D_SHADES[2], "D+1"),
                 (R_SHADES[2], "R+1"), (R_SHADES[1], "R+5"), (R_SHADES[0], "R+15")]
SS = 2  # supersampling factor


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"inter-{weight}.ttf"), size * SS)


def margin_color(m: float | None, who: str | None = None):
    if m is None:
        return NO_DATA
    if who == "O":
        return THIRD
    shades = D_SHADES if m >= 0 else R_SHADES
    a = abs(m)
    return shades[0] if a >= 15 else shades[1] if a >= 5 else shades[2] if a >= 1 else shades[3]


def prob_color(p: float | None):
    """Win probability (0–100) for the D side → colour."""
    if p is None:
        return NOT_UP
    if 40 <= p <= 60:
        return TOSSUP
    shades = D_SHADES if p > 50 else R_SHADES
    q = p if p > 50 else 100 - p
    return shades[0] if q >= 95 else shades[1] if q >= 75 else shades[2]


def _draw_shapes(draw, shapes: dict, colors: dict, transform, outline=BORDER, width=1):
    for key, polys in shapes.items():
        fill = colors.get(key)
        if fill is None:
            continue
        for poly in polys:
            for i, ring in enumerate(poly):
                pts = [transform(x, y) for x, y in ring]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=fill if i == 0 else BG, outline=outline, width=width * SS)


def _finish(img: Image.Image) -> bytes:
    img = img.resize((img.width // SS, img.height // SS), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _legend(draw, x, y, items):
    f = font("medium", 15)
    for color, label in items:
        draw.rounded_rectangle((x, y, x + 18 * SS, y + 18 * SS), radius=4 * SS, fill=color)
        draw.text((x + 26 * SS, y + 9 * SS), label, font=f, fill=MUTED, anchor="lm")
        x += (34 + int(draw.textlength(label, font=f) / SS) + 22) * SS


def _ev_bar(draw, x0, y0, width, ev: dict, names: tuple[str, str, str | None], need: int):
    d, r, o = ev.get("D", 0), ev.get("R", 0), ev.get("O", 0)
    total = max(d + r + o, 1)
    h = 30 * SS
    big, small = font("extrabold", 34), font("semibold", 17)
    draw.text((x0, y0), f"{d}", font=big, fill=(110, 166, 239))
    draw.text((x0 + width, y0), f"{r}", font=big, fill=(240, 96, 90), anchor="ra")
    draw.text((x0, y0 + 46 * SS), names[0], font=small, fill=TEXT)
    draw.text((x0 + width, y0 + 46 * SS), names[1], font=small, fill=TEXT, anchor="ra")
    if o and names[2]:
        draw.text((x0 + width // 2, y0 + 46 * SS), f"{names[2]} · {o}", font=small, fill=THIRD, anchor="ma")
    by = y0 + 76 * SS
    draw.rounded_rectangle((x0, by, x0 + width, by + h), radius=8 * SS, fill=PANEL)
    dw, ow = int(width * d / total), int(width * o / total)
    if d:
        draw.rounded_rectangle((x0, by, x0 + dw, by + h), radius=8 * SS, fill=D_SHADES[1])
    if o:
        draw.rectangle((x0 + dw, by, x0 + dw + ow, by + h), fill=THIRD)
    if r:
        draw.rounded_rectangle((x0 + dw + ow, by, x0 + width, by + h), radius=8 * SS, fill=R_SHADES[1])
    mid = x0 + int(width * need / total) if total else x0 + width // 2
    draw.line((mid, by - 6 * SS, mid, by + h + 6 * SS), fill=TEXT, width=2 * SS)
    draw.text((mid, by + h + 10 * SS), f"{need} to win", font=font("medium", 13), fill=MUTED, anchor="ma")


def national(state_shapes: dict, margins: dict[str, float], winners: dict[str, str], ev: dict,
             names: tuple[str, str, str | None], title: str, subtitle: str, need: int) -> bytes:
    W, H = 1200 * SS, 960 * SS
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 8 * SS, H), fill=(224, 36, 31))
    draw.text((48 * SS, 34 * SS), title, font=font("extrabold", 30), fill=TEXT)
    draw.text((48 * SS, 76 * SS), subtitle, font=font("medium", 17), fill=MUTED)
    _ev_bar(draw, 48 * SS, 116 * SS, W - 96 * SS, ev, names, need)
    scale = 1.0 * SS
    ox, oy = (W - 975 * scale) / 2, 270 * SS
    colors = {st: margin_color(margins.get(st), winners.get(st)) for st in state_shapes}
    _draw_shapes(draw, state_shapes, colors, lambda x, y: (ox + x * scale, oy + y * scale))
    _legend(draw, 48 * SS, H - 52 * SS, MARGIN_LEGEND + ([(THIRD, "Third party")] if ev.get("O") else []))
    draw.text((W - 48 * SS, H - 43 * SS), "electindex.com", font=font("semibold", 15), fill=MUTED, anchor="rm")
    return _finish(img)


def state_counties(county_shapes: dict, fips_list: list[str], margins: dict[str, float], winners: dict[str, str],
                   title: str, subtitle: str, result_line: str) -> bytes:
    W, H = 1200 * SS, 900 * SS
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 8 * SS, H), fill=(224, 36, 31))
    draw.text((48 * SS, 34 * SS), title, font=font("extrabold", 30), fill=TEXT)
    draw.text((48 * SS, 76 * SS), subtitle, font=font("medium", 17), fill=MUTED)
    draw.text((48 * SS, 112 * SS), result_line, font=font("extrabold", 24), fill=TEXT)
    shapes = {f: county_shapes[f] for f in fips_list if f in county_shapes}
    xs = [x for polys in shapes.values() for poly in polys for ring in poly for x, _ in ring]
    ys = [y for polys in shapes.values() for poly in polys for ring in poly for _, y in ring]
    if xs:
        box = (48 * SS, 170 * SS, W - 48 * SS, H - 80 * SS)
        bw, bh = box[2] - box[0], box[3] - box[1]
        s = min(bw / (max(xs) - min(xs) or 1), bh / (max(ys) - min(ys) or 1))
        ox = box[0] + (bw - (max(xs) - min(xs)) * s) / 2 - min(xs) * s
        oy = box[1] + (bh - (max(ys) - min(ys)) * s) / 2 - min(ys) * s
        colors = {f: margin_color(margins.get(f), winners.get(f)) for f in shapes}
        _draw_shapes(draw, shapes, colors, lambda x, y: (ox + x * s, oy + y * s), width=1)
    _legend(draw, 48 * SS, H - 52 * SS, MARGIN_LEGEND + ([(THIRD, "Third party")] if "O" in winners.values() else []))
    draw.text((W - 48 * SS, H - 43 * SS), "electindex.com", font=font("semibold", 15), fill=MUTED, anchor="rm")
    return _finish(img)


def forecast(state_shapes: dict, probs: dict[str, float], title: str, subtitle: str) -> bytes:
    W, H = 1200 * SS, 820 * SS
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 8 * SS, H), fill=(224, 36, 31))
    draw.text((48 * SS, 34 * SS), title, font=font("extrabold", 30), fill=TEXT)
    draw.text((48 * SS, 76 * SS), subtitle, font=font("medium", 17), fill=MUTED)
    scale = 1.0 * SS
    ox, oy = (W - 975 * scale) / 2, 128 * SS
    colors = {st: prob_color(probs.get(st)) for st in state_shapes}
    _draw_shapes(draw, state_shapes, colors, lambda x, y: (ox + x * scale, oy + y * scale))
    _legend(draw, 48 * SS, H - 52 * SS, [(D_SHADES[0], "Safe D"), (D_SHADES[1], "Likely D"), (D_SHADES[2], "Lean D"),
                                        (TOSSUP, "Toss-up"), (R_SHADES[2], "Lean R"), (R_SHADES[1], "Likely R"),
                                        (R_SHADES[0], "Safe R"), (NOT_UP, "Not up")])
    draw.text((W - 48 * SS, H - 43 * SS), "electindex.com", font=font("semibold", 15), fill=MUTED, anchor="rm")
    return _finish(img)
