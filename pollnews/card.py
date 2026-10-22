"""The #polpollsnews post format, shared by the NYT feed and /poll.

    #FL25 POLL

    Emerson poll, sponsored by The Hill (DEM INTERNAL)

    Moskowitz (D, incumbent) 🔵: 48%
    Singer (R) 🔴: 44%

    Oct 16-20/n=600/LV
    Link to poll: https://...

A Card is one race from one poll. Both input paths build Cards; only this
module knows how they read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .meta import AT_LARGE, SHORT, short_pollster
from .text import fold

# party code -> (letter shown in parens, circle)
PARTIES = {
    "DEM": ("D", "🔵"),
    "REP": ("R", "🔴"),
    "IND": ("I", "🟠"),
    "LIB": ("L", "🟡"),
    "GRE": ("G", "🟢"),
    "GRN": ("G", "🟢"),
}
THIRD_PARTY_CIRCLE = "🟤"
NO_PARTY = {"UND": "⚪", "OTH": "⚫"}       # Undecided, Someone else
APPROVAL = {"YES": "🟢", "NO": "🟣"}       # approve/favorable, disapprove/unfavorable
POPULATIONS = {"lv": "LV", "rv": "RV", "a": "A", "v": "V"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
TWEET_LIMIT = 280
URL_RE = re.compile(r"https?://\S+")


@dataclass
class Line:
    name: str            # already shortened ("Moskowitz", "Trump approve", "Undecided")
    party: str           # DEM REP IND LIB ... / UND OTH / YES NO
    pct: float
    incumbent: bool = False


@dataclass
class Block:
    lines: list[Line]
    label: str | None = None   # "Round 1", "Final round", "Head-to-head"


@dataclass
class Card:
    tag: str                   # "FL25", "FLSen", "GenericBallot" ...
    pollster: str
    sponsor: str = ""
    partisan: str = ""         # DEM / REP / ""
    blocks: list[Block] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    n: int | None = None
    population: str = ""       # lv rv a v
    url: str = ""


# ---------------------------------------------------------------- pieces

def race_tag(office: str, state: str, seat: int = 0) -> str:
    """FL25 / FLSen / FLGov / FLPres; national races get a word instead."""
    state = (state or "US").upper()
    national = state == "US"
    if office == "HOUSE":
        if national:
            return "GenericBallot"
        return f"{state}AL" if state in AT_LARGE or seat <= 0 else f"{state}{seat}"
    if office == "GCB":
        return "GenericBallot"
    suffix = {"SEN": "Sen", "GOV": "Gov", "PRES": "Pres"}.get(office)
    if suffix:
        return f"US{suffix}" if national else f"{state}{suffix}"
    # approval / favorability / anything else: the place is the tag
    return "National" if national else state


def party_letter(party: str) -> str:
    if party in PARTIES:
        return PARTIES[party][0]
    words = [w for w in re.split(r"[\s-]+", party) if w and w.lower() != "party"]
    if len(words) > 1:
        return "".join(w[0] for w in words).upper()
    return party[:3].upper() or "?"


def circle(party: str) -> str:
    if party in APPROVAL:
        return APPROVAL[party]
    if party in NO_PARTY:
        return NO_PARTY[party]
    return PARTIES.get(party, ("", THIRD_PARTY_CIRCLE))[1]


def fmt_pct(p: float) -> str:
    r = round(p, 1)
    return f"{int(r)}%" if r == int(r) else f"{r}%"


def fmt_dates(start: date | None, end: date | None) -> str:
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None:
        return ""
    s = f"{MONTHS[start.month - 1]} {start.day}"
    if start == end:
        return s
    if (start.year, start.month) == (end.year, end.month):
        return f"{s}-{end.day}"
    return f"{s}-{MONTHS[end.month - 1]} {end.day}"


def fmt_line(l: Line) -> str:
    if l.party in APPROVAL or l.party in NO_PARTY:
        return f"{l.name} {circle(l.party)}: {fmt_pct(l.pct)}"
    who = party_letter(l.party) + (", incumbent" if l.incumbent else "")
    return f"{l.name} ({who}) {circle(l.party)}: {fmt_pct(l.pct)}"


def pollster_line(c: Card) -> str:
    full = "/".join(p.strip() for p in c.pollster.split("/"))   # "A / B" -> "A/B"
    name = SHORT.get(full) or "/".join(short_pollster(p) for p in full.split("/"))
    s = name if fold(name).endswith("poll") else f"{name} poll"
    sponsor = c.sponsor.strip()
    if sponsor and fold(sponsor) not in fold(c.pollster):
        s += f", sponsored by {sponsor}"
    internal = {"DEM": "DEM", "REP": "GOP"}.get(c.partisan.upper())
    if internal:
        s += f" ({internal} INTERNAL)"
    return s


def footer(c: Card) -> str:
    bits = [fmt_dates(c.start, c.end),
            f"n={c.n}" if c.n else "",
            POPULATIONS.get(c.population, c.population.upper())]
    return "/".join(b for b in bits if b)


def block_text(b: Block) -> str:
    lines = [f"{b.label}:"] if b.label else []
    return "\n".join(lines + [fmt_line(l) for l in b.lines])


def render(c: Card) -> str:
    parts = [f"#{c.tag} POLL", pollster_line(c)]
    parts += [block_text(b) for b in c.blocks]
    foot = footer(c)
    link = f"Link to poll: {c.url}" if c.url else ""
    tail = "\n".join(x for x in (foot, link) if x)
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- X length

def _weight(ch: str) -> int:
    o = ord(ch)
    if o in (0x200D, 0xFE0F) or 0x1F3FB <= o <= 0x1F3FF:
        return 0  # joiners / variation selectors / skin tones ride on the emoji
    if (o <= 0x10FF or 0x2000 <= o <= 0x200C or 0x2010 <= o <= 0x201F
            or 0x2032 <= o <= 0x2037):
        return 1
    return 2


def tweet_length(text: str) -> int:
    """X's weighted count: every URL is 23, emoji and CJK count 2."""
    n, pos = 0, 0
    for m in URL_RE.finditer(text):
        n += sum(_weight(ch) for ch in text[pos:m.start()]) + 23
        pos = m.end()
    return n + sum(_weight(ch) for ch in text[pos:])


def split_thread(text: str, limit: int = TWEET_LIMIT) -> list[str]:
    """One tweet if it fits; otherwise break at blank lines into a thread.
    A single paragraph longer than the limit is broken at line ends."""
    if tweet_length(text) <= limit:
        return [text]
    paras = []
    for p in text.split("\n\n"):
        if tweet_length(p) <= limit:
            paras.append(p)
            continue
        cur = ""
        for line in p.split("\n"):
            cand = f"{cur}\n{line}" if cur else line
            if cur and tweet_length(cand) > limit:
                paras.append(cur)
                cur = line
            else:
                cur = cand
        paras.append(cur)
    out, cur = [], ""
    for p in paras:
        cand = f"{cur}\n\n{p}" if cur else p
        if cur and tweet_length(cand) > limit:
            out.append(cur)
            cur = p
        else:
            cur = cand
    out.append(cur)
    return out
