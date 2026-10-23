"""Candidate headshots for the tweet images, found with a web search.

For each person (cached once found, so every face costs one lookup):

1. Claude + web search finds pages about that exact person that carry their
   photo (official bio, Wikipedia, Ballotpedia, campaign site, a profile).
2. Each page's preview image (og:image / twitter:image) is downloaded through
   the same public-address-only fetcher /poll uses.
3. Claude looks at all of them at once and picks the one that is a clear
   photo of this one person, or none, and says where the face is; the photo
   is cropped square around the face.

A wrong face is worse than none: anything doubtful is a miss, and the image
draws initials instead. Misses are retried after a few days.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import anthropic
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

from .extract import ExtractError, fetch, sniff_type, IMAGE_TYPES
from .text import fold

log = logging.getLogger(__name__)

# Finding a face is a simple job done for every new person, so it runs on the
# cheap model; the basic web search tool (the dynamic-filtering one isn't on
# Haiku, and it bills code execution on top).
MODEL = os.environ.get("POLPOLLSNEWS_PHOTO_MODEL") or "claude-haiku-5-5"
SEARCHES = 2      # web searches per person ($10 / 1,000)
MISS_TTL = 3 * 86400
MAX_PAGES = 4
MAX_IMAGES = 6
MIN_SIDE = 160                 # smaller previews are icons, not photos
OUT = 600                      # cached square headshot, px
BUDGET = 40 * 1024 * 1024      # bytes downloaded per person

PAGES_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["pages"],
                "properties": {"pages": {"type": "array", "items": {"type": "string"}}}}
BOX = {"type": "object", "additionalProperties": False, "required": ["x0", "y0", "x1", "y1"],
       "properties": {k: {"type": "number"} for k in ("x0", "y0", "x1", "y1")}}
PICK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["index", "face"],
               "properties": {"index": {"type": "integer"},
                              "face": {"anyOf": [BOX, {"type": "null"}]}}}

SEARCH_SYSTEM = """Find web pages about one specific politician that show their photo: their \
official government bio, their Wikipedia article, their Ballotpedia page, their campaign \
site's about page, or a news profile of them. Return up to 4 page URLs, best first. Only pages \
clearly about this exact person; watch for other people with the same name. Return an empty \
list if you can't tell."""

PICK_SYSTEM = """You choose a headshot for a poll graphic. You are shown numbered images found \
on pages about a politician. Pick the image that is a clear photo of exactly one person who is \
plausibly that politician: a portrait or head-and-shoulders shot, face visible. Never pick a \
logo, a graphic with text over the face, a group photo, a crowd, a building, or an image of \
someone else (if pages disagree about who is shown, pick none). Return index -1 if none \
qualifies. For the picked image give the bounding box of the face (forehead to chin) as \
fractions of the image width and height, 0 to 1."""


class Photos:
    def __init__(self, cache_dir: Path, client: anthropic.Anthropic | None):
        self.dir = cache_dir / "headshots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.client = client
        self.lock = threading.Lock()
        self.busy: dict[str, threading.Lock] = {}
        try:
            self.index: dict = json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            self.index = {}

    @staticmethod
    def key(name: str, state: str) -> str:
        return f"{fold(name)}|{(state or '').upper()}"

    def _remember(self, key: str, entry: dict) -> None:
        with self.lock:
            self.index[key] = entry
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.index, indent=1))
            tmp.replace(self.index_path)

    def cached(self, name: str, state: str) -> bytes | None:
        hit = self.index.get(self.key(name, state))
        if hit and hit.get("file") and (self.dir / hit["file"]).exists():
            return (self.dir / hit["file"]).read_bytes()
        return None

    def set_override(self, name: str, state: str, image: bytes) -> None:
        """Staff supplied a photo: use it for this person from now on."""
        key = self.key(name, state)
        f = re.sub(r"[^a-z0-9]+", "-", key)[:80] + ".jpg"
        (self.dir / f).write_bytes(image)
        self._remember(key, {"file": f, "source": "staff", "at": time.time()})

    # ---- the lookup
    def headshot(self, name: str, state: str, context: str) -> bytes | None:
        """A square JPEG of this person, or None. Never raises."""
        if not name:
            return None
        key = self.key(name, state)
        with self.lock:
            busy = self.busy.setdefault(key, threading.Lock())
        with busy:   # two cards with the same person: search once
            hit = self.index.get(key)
            if hit:
                if hit.get("file") and (self.dir / hit["file"]).exists():
                    return (self.dir / hit["file"]).read_bytes()
                if not hit.get("file") and time.time() - hit.get("at", 0) < MISS_TTL:
                    return None
            if self.client is None:
                return None
            try:
                found = self._find(name, context)
            except Exception:
                log.exception("headshot search for %s failed", name)
                return None   # transient: don't cache
            if found is None:
                self._remember(key, {"file": None, "at": time.time()})
                return None
            jpeg, source = found
            f = re.sub(r"[^a-z0-9]+", "-", key)[:80] + ".jpg"
            (self.dir / f).write_bytes(jpeg)
            self._remember(key, {"file": f, "source": source, "at": time.time()})
            log.info("headshot for %s from %s", name, source)
            return jpeg

    def _ask(self, system: str, content: list, schema: dict, tools: list | None = None) -> dict:
        kw = {"tools": tools} if tools else {}
        r = self.client.messages.create(
            model=MODEL, max_tokens=4000, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **kw)
        if r.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"Claude stopped: {r.stop_reason}")
        return json.loads(next(b.text for b in r.content if b.type == "text"))

    def _find(self, name: str, context: str) -> tuple[bytes, str] | None:
        pages = self._ask(SEARCH_SYSTEM, [{"type": "text", "text": f"{name}: {context}"}],
                          PAGES_SCHEMA,
                          [{"type": "web_search_20250305", "name": "web_search", "max_uses": SEARCHES}]
                          )["pages"][:MAX_PAGES]
        budget = [BUDGET]
        urls: list[tuple[str, str]] = []    # (image url, page)
        for page in pages:
            try:
                final, body, ctype = fetch(page, budget)
            except ExtractError as e:
                log.info("headshot page %s: %s", page, e)
                continue
            if ctype in IMAGE_TYPES:
                urls.append((final, page))
                continue
            soup = BeautifulSoup(body, "html.parser")
            for m in soup.find_all("meta"):
                prop = str(m.get("property") or m.get("name") or "").lower()
                if prop in ("og:image", "og:image:url", "twitter:image") and m.get("content"):
                    u = str(m["content"]).strip()
                    if u.startswith("//"):
                        u = "https:" + u
                    if u.startswith("http") and u not in (x for x, _ in urls):
                        urls.append((u, page))
        images: list[tuple[Image.Image, str]] = []
        for u, page in urls[:MAX_IMAGES * 2]:
            if len(images) >= MAX_IMAGES:
                break
            try:
                _, body, ctype = fetch(u, budget)
                if sniff_type(ctype, u, body) not in IMAGE_TYPES:
                    continue
                im = ImageOps.exif_transpose(Image.open(io.BytesIO(body))).convert("RGB")
            except Exception:
                continue
            if min(im.size) >= MIN_SIDE:
                images.append((im, page))
        if not images:
            return None
        content: list[dict] = []
        for i, (im, page) in enumerate(images):
            small = im.copy()
            small.thumbnail((768, 768))
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=85)
            content += [{"type": "text", "text": f"Image {i} (from {page}):"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.standard_b64encode(buf.getvalue()).decode()}}]
        content.append({"type": "text", "text": f"Which image is {name} ({context})?"})
        pick = self._ask(PICK_SYSTEM, content, PICK_SCHEMA)
        i, face = pick.get("index", -1), pick.get("face")
        if not isinstance(i, int) or not 0 <= i < len(images) or not face:
            return None
        im, page = images[i]
        return crop_face(im, face), page


def crop_face(im: Image.Image, face: dict) -> bytes:
    """Square around the face: face height ×2.4, face a little above centre."""
    w, h = im.size
    x0, x1 = sorted((max(0.0, min(1.0, face["x0"])), max(0.0, min(1.0, face["x1"]))))
    y0, y1 = sorted((max(0.0, min(1.0, face["y0"])), max(0.0, min(1.0, face["y1"]))))
    fw, fh = (x1 - x0) * w, (y1 - y0) * h
    side = min(max(fh * 2.4, fw * 2.2, 120), w, h)
    cx, cy = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h + side * 0.08
    left = min(max(0, cx - side / 2), w - side)
    top = min(max(0, cy - side / 2), h - side)
    sq = im.crop((int(left), int(top), int(left + side), int(top + side))).resize((OUT, OUT), Image.LANCZOS)
    buf = io.BytesIO()
    sq.save(buf, "JPEG", quality=90)
    return buf.getvalue()
