"""The 1200×675 image attached to each tweet.

    ┌──────────────────────────────────────────────┐
    │ MAINE · SENATE                               │  title band
    │ Beacon/Shaw · Fox News · Oct 16-20 · 1514 LV │
    ├──────────────────────────────────────────────┤
    │      [photo]               [photo]           │  the two leaders of the
    │      Collins (R)           Jackson (D)       │  first question, photos
    │        49%                   49%             │  ringed in party colour
    │ Bennett (I) 9% · Undecided 2%  @polpollsnews │
    └──────────────────────────────────────────────┘

Approval / favorability: the subject's photo beside two bars. Anyone without
a photo (or the generic ballot) gets a party-coloured tile with initials.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .card import APPROVAL, NO_PARTY, POPULATIONS, Card, Line, fmt_dates, fmt_pct, party_letter, pollster_line
from .meta import AT_LARGE, STATES
from .text import fold, short_name

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
W, H = 1200, 675
BAND = 132
INK = (15, 23, 42)
MUTED = (71, 85, 105)
BG = (248, 248, 246)
BAND_BG = (15, 23, 42)
BAND_SUB = (203, 213, 225)
COLORS = {"DEM": (37, 99, 235), "REP": (220, 38, 38), "IND": (234, 115, 22),
          "LIB": (202, 160, 8), "GRE": (22, 163, 74), "GRN": (22, 163, 74),
          "UND": (148, 163, 184), "OTH": (55, 65, 81), "YES": (22, 163, 74), "NO": (124, 58, 237)}
THIRD = (146, 64, 14)
OFFICE_NAMES = {"SEN": "Senate", "GOV": "Governor", "PRES": "President", "GCB": "Generic Ballot"}

PhotoFn = Callable[[Card, Line], "bytes | None"]   # the headshot for this line, if any
PARTY_WORDS = {"DEM": "Democratic", "REP": "Republican", "IND": "independent",
               "LIB": "Libertarian", "GRE": "Green", "GRN": "Green"}


def photo_scope(c: Card) -> str:
    """Whose photo cache a face belongs to: national figures once, everyone
    else per state (two "John Smith"s in different states are different people)."""
    return "" if c.office in ("PRES", "APPROVAL", "FAV") or c.state in ("", "US") else c.state


def context(c: Card, l: Line) -> str:
    """Who this is, for the photo search: 'Republican candidate, Maine governor, 2026'."""
    if c.office in ("APPROVAL", "FAV"):
        return "American politician"
    party = PARTY_WORDS.get(l.party, l.party.title() if l.party else "")
    year = "2028" if c.office == "PRES" else "2026"
    race = title(c).title().replace(" · ", " ")
    stage = f" {c.stage}" if c.stage and c.stage != "general" else ""
    return f"{party} candidate{stage}, {race}, {year} election".strip()


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"inter-{weight}.ttf"), size)


def color(party: str) -> tuple[int, int, int]:
    return COLORS.get(party, THIRD)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def title(c: Card) -> str:
    place = "National" if c.state in ("", "US") else STATES.get(c.state, c.state)
    if c.office == "HOUSE":
        seat = c.race_code.split("-")[-1] if c.race_code else ""
        what = ("At-Large District" if c.state in AT_LARGE or not seat.isdigit()
                else f"{_ordinal(int(seat))} District")
    elif c.office in ("APPROVAL", "FAV"):
        who = short_name(c.subject) if c.subject else "President"
        what = f"{who} {'Approval' if c.office == 'APPROVAL' else 'Favorability'}"
    elif c.office in OFFICE_NAMES:
        what = OFFICE_NAMES[c.office]
    else:
        return f"#{c.tag}".upper()
    if "primary" in (c.stage or ""):
        what = (f"{'2028 ' if c.office == 'PRES' else what + ' · '}"
                f"{ {'DEM': 'DEM ', 'REP': 'GOP '}.get(c.primary_party, '')}{c.stage.title()}")
    elif c.office == "PRES":
        what = "2028 President"
    if c.office == "GCB" or (c.office in ("APPROVAL", "FAV", "PRES") and place == "National"):
        return what.upper() if c.office == "GCB" else f"NATIONAL · {what.upper()}"
    return f"{place.upper()} · {what.upper()}"


def subtitle(c: Card) -> str:
    who = pollster_line(c).removesuffix(" poll").replace(" poll, sponsored by ", " · ")
    bits = [who, fmt_dates(c.start, c.end)]
    if c.n or c.population:
        bits.append(" ".join(x for x in (str(c.n) if c.n else "",
                                         POPULATIONS.get(c.population, c.population.upper())) if x))
    return " · ".join(b for b in bits if b)


def _fit(draw: ImageDraw.ImageDraw, text: str, weight: str, size: int, max_w: int, min_size=18):
    while size > min_size:
        f = _font(weight, size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 2
    f = _font(weight, min_size)
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return f, text + "…"


def _initials(line: Line) -> str:
    if line.party in ("DEM", "REP") and fold(line.name) in ("democrats", "republicans"):
        return {"DEM": "DEM", "REP": "GOP"}[line.party]
    words = [w for w in (line.full or line.name).replace("-", " ").split() if w[:1].isalpha()]
    if not words:
        return party_letter(line.party)
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def _tile(photo: bytes | None, line: Line, size: int, ring: int = 8) -> Image.Image:
    """Square, rounded, party-colour ring; photo cropped to the face, else initials."""
    inner = size - 2 * ring
    face = None
    if photo:
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
            w, h = im.size
            side = min(w, h)
            top = min(h - side, int(h * 0.06)) if h > w else 0   # portraits: keep the head
            left = (w - side) // 2
            face = im.crop((left, top, left + side, top + side)).resize((inner, inner), Image.LANCZOS)
        except Exception:
            face = None
    if face is None:
        face = Image.new("RGB", (inner, inner), color(line.party))
        d = ImageDraw.Draw(face)
        initials = _initials(line)
        f = _font("extrabold", int(inner * (0.42 if len(initials) <= 2 else 0.3)))
        d.text((inner / 2, inner / 2), initials, font=f, fill=(255, 255, 255), anchor="mm")
    tile = Image.new("RGB", (size, size), color(line.party))
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, inner, inner), radius=int(inner * 0.1), fill=255)
    tile.paste(face, (ring, ring), mask)
    outer = Image.new("L", (size, size), 0)
    ImageDraw.Draw(outer).rounded_rectangle((0, 0, size, size), radius=int(size * 0.12), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(tile, (0, 0), outer)
    return out


def _label(l: Line) -> str:
    if l.party in APPROVAL or l.party in NO_PARTY:
        return l.name
    return f"{l.name} ({party_letter(l.party)})"


def _footer(img: Image.Image, draw: ImageDraw.ImageDraw, rest: list[Line]):
    handle_f = _font("semibold", 24)
    hw = draw.textlength("@polpollsnews", font=handle_f)
    draw.text((W - 48 - hw, H - 46), "@polpollsnews", font=handle_f, fill=MUTED)
    if rest:
        text = " · ".join(f"{_label(l)} {fmt_pct(l.pct)}" for l in rest)
        f, text = _fit(draw, text, "medium", 26, int(W - 96 - hw - 40))
        draw.text((48, H - 48), text, font=f, fill=MUTED)


def _matchup(img, draw, c: Card, lines: list[Line], photo: PhotoFn):
    top2, rest = lines[:2], lines[2:]
    size, y = 290, BAND + 34
    centers = [W // 4 + 20, 3 * W // 4 - 20] if len(top2) == 2 else [W // 2]
    for cx, l in zip(centers, top2):
        no_photo = c.office == "GCB" or l.party in NO_PARTY
        t = _tile(None if no_photo or not l.full else photo(c, l), l, size)
        img.paste(t, (cx - size // 2, y), t)
        label = f"{l.name} ({party_letter(l.party)}, inc.)" if l.incumbent else _label(l)
        f, name = _fit(draw, label, "extrabold", 40, 500)
        draw.text((cx, y + size + 18), name, font=f, fill=INK, anchor="mt")
        draw.text((cx, y + size + 66), fmt_pct(l.pct), font=_font("extrabold", 84),
                  fill=color(l.party), anchor="mt")
    if len(top2) == 2:
        draw.text((W // 2, y + size // 2), "vs", font=_font("semibold", 34), fill=MUTED, anchor="mm")
    _footer(img, draw, rest)


def _rating(img, draw, c: Card, lines: list[Line], photo: PhotoFn):
    size, y = 330, BAND + 40
    who = Line(short_name(c.subject) if c.subject else "?", "OTH", 0, full=c.subject)
    t = _tile(photo(c, who) if c.subject else None, who, size, ring=0)
    img.paste(t, (72, y), t)
    x0, x1 = 72 + size + 60, W - 72
    for i, l in enumerate(lines[:2]):
        ty = y + 20 + i * 160
        verb = l.name.split(" ", 1)[-1].capitalize() if " " in l.name else l.name
        draw.text((x0, ty), verb, font=_font("extrabold", 40), fill=INK)
        pct_f = _font("extrabold", 56)
        draw.text((x1, ty - 10), fmt_pct(l.pct), font=pct_f, fill=color(l.party), anchor="rt")
        draw.rounded_rectangle((x0, ty + 64, x1, ty + 100), radius=18, fill=(226, 232, 240))
        draw.rounded_rectangle((x0, ty + 64, x0 + max(36, (x1 - x0) * min(l.pct, 100) / 100),
                                ty + 100), radius=18, fill=color(l.party))
    if len(lines) >= 2:
        net = lines[0].pct - lines[1].pct
        draw.text((x0, y + 20 + 2 * 160 - 10), f"Net {'+' if net > 0 else ''}{fmt_pct(net).rstrip('%')}",
                  font=_font("semibold", 32), fill=MUTED)
    _footer(img, draw, lines[2:])


def render(c: Card, photo: PhotoFn) -> bytes:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, W, BAND), fill=BAND_BG)
    f, t = _fit(draw, title(c), "extrabold", 50, W - 96)
    draw.text((48, 24), t, font=f, fill=(255, 255, 255))
    f, t = _fit(draw, subtitle(c), "medium", 26, W - 96)
    draw.text((48, 88), t, font=f, fill=BAND_SUB)
    lines = c.blocks[0].lines if c.blocks else []
    if c.office in ("APPROVAL", "FAV") or (lines and all(l.party in APPROVAL for l in lines)):
        _rating(img, draw, c, lines, photo)
    else:
        _matchup(img, draw, c, lines, photo)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
