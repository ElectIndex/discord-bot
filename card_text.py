"""Draw a name in any script on the welcome card.

The card's brand font (Inter) only covers Latin. For everything else this walks a
chain of fallback fonts — Noto for the world's scripts, a colour emoji font for
emoji — and draws the name as runs, each in the first font that has its glyphs.
Only if no installed font can draw a character does the caller fall back to the
username.

Fallback fonts are discovered, not bundled (Noto CJK alone is ~90 MB). On the
server they come from apt (`fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji`);
set CARD_FONT_DIRS to point somewhere else.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont
from PIL import Image, ImageDraw, ImageFont, features

DEFAULT_DIRS = (
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto",
    "/System/Library/Fonts",  # macOS, for local development
)
# Discovery order after the primary font: general Noto first, then emoji (so emoji
# codepoints aren't drawn by a monochrome symbol font), then CJK, then every other script.
PATTERNS = (
    "NotoSans-Bold.ttf",
    "NotoColorEmoji.ttf",
    "Apple Color Emoji.ttc",
    "NotoSansCJK-Bold.ttc",
    "NotoSansCJK*-Bold.otf",
    "NotoSans*-Bold.ttf",
    "NotoSansSymbols2-Regular.ttf",
    "NotoSansMath-Regular.ttf",
    "NotoSans*-Regular.ttf",
)
EMOJI_STRIKES = (109, 160, 96, 64, 136, 128)  # bitmap emoji fonts only load at fixed sizes
RAQM = features.check("raqm")

# Code points that never start a cluster: they attach to the character before them.
_JOINERS = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}


def _attaches(cp: int) -> bool:
    return (
        cp in _JOINERS
        or 0x1F3FB <= cp <= 0x1F3FF  # skin tones
        or 0xE0020 <= cp <= 0xE007F  # tag sequences (flag subdivisions)
        or unicodedata.category(chr(cp)).startswith("M")  # combining marks
    )


def clusters(text: str) -> list[str]:
    """Split into user-perceived characters, closely enough for font choice:
    emoji ZWJ sequences, skin tones, flags and combining marks stay whole."""
    out: list[str] = []
    prev_ri = False
    for ch in text:
        cp = ord(ch)
        is_ri = 0x1F1E6 <= cp <= 0x1F1FF  # regional indicators pair into flags
        joined_by_zwj = out and out[-1].endswith("‍")
        if out and (_attaches(cp) or joined_by_zwj or (is_ri and prev_ri)):
            out[-1] += ch
            prev_ri = False if is_ri and prev_ri else is_ri
        else:
            out.append(ch)
            prev_ri = is_ri
    return out


@dataclass(frozen=True)
class FontFace:
    path: Path
    index: int = 0

    @property
    def is_emoji(self) -> bool:
        return "emoji" in self.path.name.lower()


@lru_cache(maxsize=None)
def coverage(face: FontFace) -> frozenset[int]:
    if face.path.suffix.lower() == ".ttc":
        font = TTCollection(str(face.path), lazy=True).fonts[face.index]
    else:
        font = TTFont(str(face.path), lazy=True, fontNumber=face.index)
    return frozenset(font.getBestCmap() or {})


@lru_cache(maxsize=1)
def fallback_faces() -> tuple[FontFace, ...]:
    dirs = [Path(d) for d in os.environ.get("CARD_FONT_DIRS", "").split(":") if d] or [Path(d) for d in DEFAULT_DIRS]
    seen: dict[Path, None] = {}
    for pattern in PATTERNS:
        for d in dirs:
            for p in sorted(d.glob(pattern)) if d.is_dir() else []:
                seen.setdefault(p, None)
    return tuple(FontFace(p) for p in seen)


def covers(face: FontFace, cluster: str) -> bool:
    cmap = coverage(face)
    base = [ord(c) for c in cluster if ord(c) not in _JOINERS and not (0xE0020 <= ord(c) <= 0xE007F)]
    return all(cp in cmap for cp in base[:1]) and all(cp in cmap or _attaches(cp) for cp in base[1:])


def plan_runs(text: str, primary: FontFace, fallbacks: tuple[FontFace, ...] | None = None) -> list[tuple[FontFace, str]] | None:
    """[(font, text), ...] covering every cluster, or None if something can't be drawn."""
    chain = (primary,) + (fallback_faces() if fallbacks is None else fallbacks)
    runs: list[tuple[FontFace, str]] = []
    for cl in clusters(text):
        if cl.isspace():
            face = runs[-1][0] if runs else primary  # spaces join the current run
        else:
            emojiish = any(ord(c) > 0x2000 and unicodedata.category(c) == "So" for c in cl) or "️" in cl
            ordered = sorted(chain, key=lambda f: not f.is_emoji) if emojiish else chain
            face = next((f for f in ordered if covers(f, cl)), None)
            if face is None:
                return None
        if runs and runs[-1][0] == face:
            runs[-1] = (face, runs[-1][1] + cl)
        else:
            runs.append((face, cl))
    return runs


def _is_rtl(text: str) -> bool:
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ("R", "AL"):
            return True
        if bidi == "L":
            return False
    return False


@lru_cache(maxsize=64)
def _font(face: FontFace, size: int) -> tuple[ImageFont.FreeTypeFont, float]:
    """(font, scale). Bitmap emoji fonts load only at a strike size, so they're
    drawn at that size and scaled by `scale`."""
    engine = ImageFont.Layout.RAQM if RAQM else ImageFont.Layout.BASIC
    try:
        return ImageFont.truetype(str(face.path), size, index=face.index, layout_engine=engine), 1.0
    except OSError:
        for strike in EMOJI_STRIKES:
            try:
                return ImageFont.truetype(str(face.path), strike, index=face.index, layout_engine=engine), size / strike
            except OSError:
                continue
        raise


def _run_image(face: FontFace, text: str, size: int, color) -> tuple[Image.Image, int]:
    """Render one run to its own RGBA image. Returns (image, baseline y within it)."""
    font, scale = _font(face, size)
    kwargs = {"embedded_color": True} if face.is_emoji else {}
    if RAQM and _is_rtl(text):
        kwargs["direction"] = "rtl"
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font, anchor="ls", **kwargs)
    pad = 4
    w, h = max(1, right - left + pad * 2), max(1, bottom - top + pad * 2)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - left, pad - top), text, font=font, fill=color, anchor="ls", **kwargs)
    baseline = pad - top
    if scale != 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        baseline = round(baseline * scale)
    # Advance width rather than ink width, so spacing matches normal text.
    advance = probe.textlength(text, font=font, **({"direction": kwargs["direction"]} if "direction" in kwargs else {}))
    img.info["advance"] = round(advance * scale)
    img.info["ink_left"] = round((left - pad) * scale)
    return img, baseline


def render_runs(runs: list[tuple[FontFace, str]], size: int, color) -> tuple[list[tuple[Image.Image, int]], int]:
    """Render runs in visual order. Returns ([(image, baseline)], total advance width)."""
    if _is_rtl("".join(t for _, t in runs)):
        runs = list(reversed(runs))
    rendered = [_run_image(face, text, size, color) for face, text in runs]
    return rendered, sum(img.info["advance"] for img, _ in rendered)


def paste_runs(card: Image.Image, rendered: list[tuple[Image.Image, int]], x: int, baseline_y: int):
    for img, baseline in rendered:
        card.alpha_composite(img, (max(0, x + img.info["ink_left"]), baseline_y - baseline))
        x += img.info["advance"]
