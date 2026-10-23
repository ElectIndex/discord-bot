"""/poll: turn a link, pasted text, screenshots or a PDF into Cards.

Claude reads the source and returns the poll as JSON (structured outputs,
so the shape is guaranteed); everything about how a card LOOKS is decided
here in code, by the same card builder the NYT path uses. Claude only reads
numbers, it never writes the post.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlparse

import anthropic
import certifi
import urllib3
from bs4 import BeautifulSoup

from .card import Block, Card, race_tag
from .meta import Meta
from .model import Answer, Question, RaceKey
from .nyt import lines_for, race_code

log = logging.getLogger(__name__)

MODEL = os.environ.get("POLPOLLSNEWS_EXTRACT_MODEL") or "claude-opus-5-5"   # reads poll numbers: accuracy first
FETCH_UA = {"User-Agent": "Mozilla/5.0 (compatible; PolPollsNewsBot/1.0)"}
MAX_BYTES = 20 * 1024 * 1024          # one download
MAX_TOTAL_BYTES = 30 * 1024 * 1024    # everything one /poll downloads
MAX_TEXT_CHARS = 400_000
URL_RE = re.compile(r"https?://[^\s<>\"']+")
OFFICES = ["HOUSE", "SEN", "GOV", "PRES", "GCB", "APPROVAL", "FAV", "OTHER"]


class ExtractError(RuntimeError):
    """Shown to the Discord user as-is."""


# ---------------------------------------------------------------- schema

def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


ANSWER = _obj({
    "name": {"type": "string", "description": "Full name as written, e.g. 'Jared Moskowitz'. "
             "For undecided/not sure use 'Undecided'; for someone else/other use 'Someone else'. "
             "For approval/favorability use 'Approve'/'Disapprove' or 'Favorable'/'Unfavorable'."},
    "party": {"type": "string", "description": "DEM, REP, IND, LIB, GRE, or a short code for "
              "another party. UND for undecided/not sure, OTH for someone else/other. "
              "YES for approve/favorable, NO for disapprove/unfavorable."},
    "pct": {"type": "number"},
    "incumbent": {"type": "boolean"},
})
QUESTION = _obj({
    "label": _nullable({"type": "string", "description": "Only when a race has several "
                        "questions that need telling apart: 'Round 1', 'Final round', "
                        "'Head-to-head', 'With leaners'. Null for a single question."}),
    "answers": {"type": "array", "items": ANSWER},
})
RACE = _obj({
    "office": {"type": "string", "enum": OFFICES,
               "description": "HOUSE district race, SEN, GOV, PRES, GCB (generic congressional "
               "ballot), APPROVAL (job approval), FAV (favorability), OTHER (mayor, ballot "
               "measure, state legislature, ...)."},
    "state": {"type": "string", "description": "Two-letter postal code, or US for national."},
    "district": _nullable({"type": "integer", "description": "House district number; 0 for "
                           "at-large. Null otherwise."}),
    "stage": {"type": "string", "enum": ["general", "primary", "runoff", "special"]},
    "subject": _nullable({"type": "string", "description": "APPROVAL/FAV: the person rated."}),
    "other_tag": _nullable({"type": "string", "description": "OTHER only: a short CamelCase "
                            "hashtag for the race with the state prefix, e.g. 'NYCMayor', "
                            "'FLAmendment4', 'VAHD41'."}),
    "population": {"type": "string", "enum": ["lv", "rv", "v", "a"],
                   "description": "lv likely voters, rv registered voters, v voters, a adults."},
    "sample_size": _nullable({"type": "integer"}),
    "questions": {"type": "array", "items": QUESTION},
})
SCHEMA = _obj({
    "found": {"type": "boolean", "description": "False if the source has no poll results."},
    "problem": _nullable({"type": "string", "description": "If found is false, or something "
                          "important was unreadable, one sentence saying what."}),
    "pollster": {"type": "string"},
    "sponsor": {"type": "string", "description": "Who paid for it; empty if the pollster self-funded."},
    "partisan": {"type": "string", "enum": ["DEM", "REP", ""],
                 "description": "DEM/REP if it is a campaign or party internal, or sponsored by "
                 "a partisan group; empty otherwise."},
    "start_date": _nullable({"type": "string", "description": "Fieldwork start, YYYY-MM-DD."}),
    "end_date": _nullable({"type": "string", "description": "Fieldwork end, YYYY-MM-DD."}),
    "url": _nullable({"type": "string", "description": "The poll's own release URL if the "
                      "source names one."}),
    "races": {"type": "array", "items": RACE},
})

SYSTEM = """You read election polls and transcribe them as data.

Transcribe exactly what the source reports. Never estimate, round differently, or fill in a \
number that is not there. Use the topline: the full sample for that race, not a crosstab or \
subgroup. If a pollster reports both likely and registered voters, use likely voters.

Every race the poll asked about is its own entry in races (a poll of Florida with Senate and \
Governor questions has two). Several matchups in one race (hypothetical head-to-heads, a version \
with and without third parties, ranked-choice rounds) are several questions in that race.

Include every answer option reported for a question: third-party and independent candidates, \
undecided, and someone else. Leave out "would not vote" and "refused".

Mark incumbent true only for the current officeholder of the seat being polled.

Today's date is {today}; polls are usually from the last few weeks, which tells you the year \
when the source leaves it out."""


# ---------------------------------------------------------------- fetching

def _pinned_ip(host: str, port: int) -> str:
    """Resolve once and return an address to connect to, or refuse if ANY
    address the name resolves to isn't public (private, loopback, link-local
    cloud-metadata ...). The caller connects to this exact IP, so a second
    lookup can't be rebound to an internal address."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise ExtractError(f"Can't resolve {host}")
    ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not ips or not all(ip.is_global for ip in ips):
        raise ExtractError(f"Refusing to fetch {host} (not a public address)")
    return str(next((ip for ip in ips if ip.version == 4), ips[0]))


def _get_pinned(url: str) -> urllib3.BaseHTTPResponse:
    # Parse with urllib3 (what makes the connection below) and refuse anything
    # parsers disagree on, so the host checked is the host connected to.
    if "\\" in url or any(ord(c) < 33 for c in url):
        raise ExtractError("Can't fetch that link")
    try:
        u = urllib3.util.parse_url(url)
    except urllib3.exceptions.LocationParseError:
        raise ExtractError("Can't fetch that link")
    if u.scheme not in ("http", "https") or not u.host or u.auth:
        raise ExtractError("Can't fetch that link")
    host = u.host.strip("[]")
    port = u.port or (443 if u.scheme == "https" else 80)
    ip = _pinned_ip(host, port)
    common = dict(port=port, timeout=urllib3.Timeout(connect=10, read=30), retries=False)
    if u.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(ip, server_hostname=host, assert_hostname=host,
                                           cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
                                           **common)
    else:
        pool = urllib3.HTTPConnectionPool(ip, **common)
    netloc = host if u.port is None else f"{host}:{u.port}"
    if ":" in host:
        netloc = f"[{host}]" if u.port is None else f"[{host}]:{u.port}"
    return pool.urlopen("GET", u.request_uri, headers={**FETCH_UA, "Host": netloc},
                        redirect=False, preload_content=False)


def fetch(url: str, budget: list[int] | None = None) -> tuple[str, bytes, str]:
    """GET a user-supplied URL, refusing non-public addresses on every
    redirect hop (the bot runs on a server). budget: a one-item list of bytes
    still allowed across all fetches for one request; it is spent here."""
    limit = MAX_BYTES if budget is None else min(MAX_BYTES, budget[0])
    for _ in range(5):
        try:
            r = _get_pinned(url)
        except urllib3.exceptions.HTTPError as e:
            raise ExtractError(f"Couldn't fetch the link: {e}") from e
        try:
            if r.status in (301, 302, 303, 307, 308):
                url = urljoin(url, r.headers.get("location", ""))
                continue
            if r.status != 200:
                host = urlparse(url).hostname or ""
                raise ExtractError(f"The link returned HTTP {r.status}"
                                   + (" (X/Twitter needs a screenshot or pasted text)"
                                      if host.endswith(("x.com", "twitter.com")) else ""))
            body = b""
            for chunk in r.stream(65536):
                body += chunk
                if len(body) > limit:
                    raise ExtractError("That's more than I can download for one poll "
                                       f"({limit // (1024 * 1024)} MB)")
            if budget is not None:
                budget[0] -= len(body)
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            return url, body, ctype
        except urllib3.exceptions.HTTPError as e:
            raise ExtractError(f"Couldn't fetch the link: {e}") from e
        finally:
            r.release_conn()
    raise ExtractError("Too many redirects")


SHORTENERS = {"t.co", "bit.ly", "tinyurl.com", "ow.ly", "buff.ly", "lnkd.in", "dlvr.it",
              "trib.al", "shorturl.at", "rb.gy", "is.gd", "goo.gl"}
X_HOSTS = ("x.com", "twitter.com")
META_REFRESH = re.compile(rb"""http-equiv=["']?refresh["']?[^>]*?url=([^"'>\s]+)""", re.I)


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_short(url: str) -> bool:
    return _host(url) in SHORTENERS


def is_x(url: str) -> bool:
    h = _host(url)
    return h in X_HOSTS or h.endswith(tuple("." + x for x in X_HOSTS))


def expand(url: str) -> str:
    """The real address behind a t.co (or other shortener) link; anything
    else, or a link that won't resolve, comes back unchanged. Follows redirect
    hops and the meta-refresh page t.co serves browsers, never reading more
    than a few KB, through the same public-address-only fetcher."""
    cur = url
    for _ in range(6):
        if not is_short(cur):
            return cur
        try:
            r = _get_pinned(cur)
        except (ExtractError, urllib3.exceptions.HTTPError):
            return cur
        try:
            nxt = None
            if r.status in (301, 302, 303, 307, 308):
                nxt = r.headers.get("location")
            elif r.status == 200:
                m = META_REFRESH.search(r.read(16384, decode_content=True) or b"")
                nxt = m.group(1).decode("utf-8", "replace") if m else None
        except urllib3.exceptions.HTTPError:
            nxt = None
        finally:
            r.release_conn()
        if not nxt:
            return cur
        cur = urljoin(cur, nxt)
    return cur


def html_text(body: bytes) -> str:
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = soup.get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return f"{title}\n\n{text}" if title else text


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024     # Claude's per-image limit
MAX_FILES = 5
EXT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}
MAGIC = [(b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
         (b"%PDF", "application/pdf")]


def sniff_type(ctype: str | None, filename: str = "", data: bytes = b"") -> str:
    """Content type from the header, else the file's bytes, else its extension."""
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype in IMAGE_TYPES or ctype == "application/pdf":
        return ctype
    for magic, t in MAGIC:
        if data.startswith(magic):
            return t
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    ext = os.path.splitext(urlparse(filename).path.lower())[1]
    return EXT_TYPES.get(ext, ctype)


def file_block(data: bytes, ctype: str) -> dict | None:
    ctype = sniff_type(ctype, "", data)
    if ctype in IMAGE_TYPES and len(data) > MAX_IMAGE_BYTES:
        raise ExtractError("That image is over 5 MB; crop it or send a smaller screenshot")
    if ctype == "application/pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(data)}}
    if ctype in IMAGE_TYPES:
        return {"type": "image",
                "source": {"type": "base64", "media_type": ctype, "data": _b64(data)}}
    return None


@dataclass
class Source:
    blocks: list[dict]     # content blocks for Claude
    link: str | None       # what "Link to poll:" should say


def build_source(text: str, attachments: list[tuple[bytes, str]],
                 image_urls: list[str] = ()) -> Source:
    """text: what the user typed (a link, pasted results, or both).
    attachments: (bytes, content type) for screenshots / PDFs.
    image_urls: images embedded in a Discord message (pasted image links)."""
    blocks: list[dict] = []
    typed = list(dict.fromkeys(u.rstrip(").,") for u in URL_RE.findall(text or "")))
    if sum(map(is_short, typed)) > MAX_FILES:
        raise ExtractError(f"Send at most {MAX_FILES} links at once")
    # t.co & co. -> the real address, so the card links (and the bot reads)
    # the actual page.
    urls = [expand(u) for u in typed]
    # A link that IS a screenshot is an attachment, not the poll's link.
    image_links = [u for u in urls if u not in image_urls and sniff_type("", u) in IMAGE_TYPES]
    others = [u for u in urls if u not in image_urls and u not in image_links]
    # A pasted tweet carries t.co links to its own photos and to the release:
    # the release is the poll's link; an X link is the fallback.
    link = next((u for u in others if not is_x(u)), None) or next(iter(others), None)
    # Count everything before downloading anything.
    if len(attachments) + len(image_urls) + len(image_links) > MAX_FILES:
        raise ExtractError(f"Send at most {MAX_FILES} images/files at once")
    budget = [MAX_TOTAL_BYTES - sum(len(d) for d, _ in attachments)]
    for u in [*image_urls, *image_links]:
        _, body, ctype = fetch(u, budget)
        attachments = [*attachments, (body, sniff_type(ctype, u, body))]
    for data, ctype in attachments:
        b = file_block(data, ctype)
        if b is None:
            raise ExtractError(f"Can't read a {ctype or 'file'} attachment; "
                               "send a screenshot, PDF or text")
        blocks.append(b)
    if link and is_x(link):
        pass  # X pages need a login to read; the link still goes on the card
    elif link:
        final, body, ctype = fetch(link, budget)
        b = file_block(body, ctype)
        if b is not None:
            blocks.append(b)
        else:
            page = html_text(body) if "html" in ctype or body.lstrip()[:1] == b"<" \
                else body.decode("utf-8", "replace")
            if len(page) > MAX_TEXT_CHARS:
                raise ExtractError("That page is too long to read; paste the results instead")
            blocks.append({"type": "text", "text": f"Page at {final}:\n\n{page}"})
    if text and text.strip() not in ("", *typed, *urls):
        blocks.append({"type": "text", "text": f"Message from the user:\n\n{text}"})
    if not blocks:
        if link and is_x(link):
            raise ExtractError("I can't read X/Twitter posts; attach a screenshot of the "
                               "tweet (the link will still go on the card)")
        raise ExtractError("Give me a link, pasted poll text, or an attachment")
    blocks.append({"type": "text", "text": "Transcribe the poll above."})
    return Source(blocks, link)


# ---------------------------------------------------------------- Claude

async def ask_claude(client: anthropic.AsyncAnthropic, source: Source, today: date) -> dict:
    try:
        resp = await client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM.format(today=today.isoformat()),
            messages=[{"role": "user", "content": source.blocks}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as e:
        raise ExtractError("Claude is rate-limited right now; try again in a minute") from e
    except anthropic.APIStatusError as e:
        log.error("Claude API %s: %s", e.status_code, e.message)
        raise ExtractError(f"Claude API error {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise ExtractError("Couldn't reach the Claude API") from e
    if resp.stop_reason == "refusal":
        raise ExtractError("Claude declined to read that source")
    if resp.stop_reason == "max_tokens":
        raise ExtractError("The poll was too long to transcribe in one go")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractError("Claude returned unreadable output") from e


# ---------------------------------------------------------------- -> Cards

def _date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _primary_party(blocks: list[Block]) -> str:
    parties = {l.party for b in blocks for l in b.lines if l.party in ("DEM", "REP")}
    return parties.pop() if len(parties) == 1 else ""


def cards_from(data: dict, meta: Meta, link: str | None) -> list[Card]:
    if not data.get("found") or not data.get("races"):
        raise ExtractError(data.get("problem") or "No poll results found in that source")
    cards = []
    for race in data["races"]:
        office = race["office"]
        state = (race.get("state") or "US").upper()
        seat = race.get("district") or 0
        key = RaceKey(office if office != "HOUSE" or state != "US" else "GCB",
                      state, seat, race.get("stage") or "general", race.get("subject") or "")
        inc = meta.incumbent(race_code(key))
        blocks = []
        for i, q in enumerate(race.get("questions") or []):
            answers = tuple(Answer(a["name"], (a["party"] or "").upper(), float(a["pct"]),
                                   a["name"]) for a in q["answers"])
            question = Question(qid=str(i), population=race["population"],
                                n=race.get("sample_size"), rcv_round=None, realloc=False,
                                order=i, answers=answers)
            # races.csv knows the incumbent for federal/governor races; otherwise
            # take Claude's word for it.
            claimed = next((a["name"] for a in q["answers"] if a.get("incumbent")), None)
            lines = lines_for(question, key, inc or claimed)
            if lines:
                blocks.append(Block(lines, q.get("label") if len(race["questions"]) > 1 else None))
        if not blocks:
            continue
        tag = race_tag(key.office, state, seat)
        if office == "OTHER" and race.get("other_tag"):
            tag = re.sub(r"[^A-Za-z0-9]", "", race["other_tag"]) or tag
        cards.append(Card(
            tag=tag, pollster=data.get("pollster") or "Unknown pollster",
            sponsor=data.get("sponsor") or "", partisan=data.get("partisan") or "",
            blocks=blocks, start=_date(data.get("start_date")), end=_date(data.get("end_date")),
            n=race.get("sample_size"), population=race["population"],
            url=link or data.get("url") or "",
            office=key.office, state=state, race_code=race_code(key), subject=key.subject,
            stage=key.stage,
            primary_party=_primary_party(blocks) if "primary" in key.stage else ""))
    if not cards:
        raise ExtractError(data.get("problem") or "No poll results found in that source")
    return cards


async def extract(client: anthropic.AsyncAnthropic, source: Source, meta: Meta,
                  today: date) -> list[Card]:
    data = await ask_claude(client, source, today)
    if not source.link and data.get("url") and is_short(data["url"]):
        data["url"] = await asyncio.to_thread(expand, data["url"])
    return cards_from(data, meta, source.link)
