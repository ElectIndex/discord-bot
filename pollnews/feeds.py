"""Vendored from ElectIndex/autoposter (pollposter.feeds), with one change:
"Don't know" / "Someone else" rows are kept as UND / OTH answers instead of
dropped, because the Discord cards show undecideds.

Fetch the NYT poll feeds and turn their rows into Poll objects.

The NYT publishes 538-format CSVs: one row per poll x question x candidate.
Every feed is folded into the same shape, so a survey that asked about
Senate, Governor and the 2028 primary (three feeds, one shared poll_id)
becomes one Poll with three races.

Approval rows are one-per-question with yes/no columns. They are unrolled
into two answers ("Approve"/"Disapprove") so the rest of the pipeline never
special-cases them.
"""

from __future__ import annotations

import csv
import io
import time
from collections import Counter, defaultdict
from datetime import date, datetime

import requests

from .model import Answer, Poll, Question, RaceKey
from .text import fold

BASE = "https://www.nytimes.com/newsgraphics/polls/"
FEEDS = {
    "senate": BASE + "senate.csv",
    "governor": BASE + "governor.csv",
    "house": BASE + "house.csv",          # districts + national generic ballot (state US)
    "president": BASE + "president.csv",  # 2028 primaries + general
    "approval": BASE + "approval/president.csv",
}
UA = {"User-Agent": "Mozilla/5.0 (compatible; ElectIndexPollBot/1.0; +https://electindex.com)"}

OFFICE = {"U.S. Senate": "SEN", "Governor": "GOV", "U.S. House": "HOUSE",
          "U.S. President": "PRES"}


# NYT rows with party NONE, by folded answer text. Anything unlisted
# ("Would not vote", "Refused") is dropped.
NONE_ANSWERS = {"don't know": "UND", "dont know": "UND", "undecided": "UND",
                "not sure": "UND", "unsure": "UND", "someone else": "OTH",
                "other": "OTH", "another candidate": "OTH"}


class FeedError(RuntimeError):
    pass


def fetch_text(url: str, tries: int = 3) -> str:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            if r.status_code == 200 and r.text.lstrip().startswith('"poll_id"'):
                return r.text
            last = FeedError(f"{url}: HTTP {r.status_code}, {len(r.text)} bytes")
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (i + 1))
    raise FeedError(str(last))


def parse_date(s: str | None) -> date | None:
    s = (s or "").strip()
    for fmt in ("%m/%d/%y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_dt(s: str | None) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%m/%d/%y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _num(s) -> float | None:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN -> None


def _truthy(s) -> bool:
    return str(s).strip().lower() in ("true", "1", "yes")


def _race_key(feed: str, row: dict) -> RaceKey | None:
    state = (row.get("state") or "").strip().upper() or "US"
    stage = (row.get("stage") or "general").strip().lower()
    if feed == "approval":
        return RaceKey("APPROVAL", "US", 0, "general", row.get("politician") or "")
    office = OFFICE.get((row.get("office_type") or "").strip())
    if office is None:
        return None
    if office == "HOUSE" and state == "US":
        return RaceKey("GCB", "US", 0, stage)
    seat = 0
    if office == "HOUSE":
        n = _num(row.get("seat_number"))
        if n is None:
            return None
        seat = int(n)
    return RaceKey(office, state, seat, stage)


def build_polls(feeds: dict[str, str]) -> dict[str, Poll]:
    """feeds: feed name -> CSV text. Returns poll_id -> Poll."""
    polls: dict[str, Poll] = {}
    # (poll_id, race key, question_id) -> list of answers, kept in feed order
    qmeta: dict[tuple, dict] = {}
    qans: dict[tuple, list[Answer]] = defaultdict(list)

    for feed, text in feeds.items():
        for row in csv.DictReader(io.StringIO(text)):
            pid = row.get("poll_id")
            key = _race_key(feed, row)
            if not pid or key is None:
                continue
            if pid not in polls:
                polls[pid] = Poll(
                    poll_id=pid,
                    pollster_id=row.get("pollster_id") or "",
                    pollster=(row.get("display_name") or row.get("pollster") or "").strip(),
                    pollster_names=tuple(filter(None, {
                        (row.get("display_name") or "").strip(),
                        (row.get("pollster") or "").strip(),
                        (row.get("pollster_rating_name") or "").strip()})),
                    sponsors=(row.get("sponsors") or "").strip(),
                    partisan=(row.get("partisan") or "").strip().upper(),
                    internal=_truthy(row.get("internal")),
                    start=parse_date(row.get("start_date")),
                    end=parse_date(row.get("end_date")),
                    created=parse_dt(row.get("created_at")),
                    url=(row.get("url") or row.get("url_article")
                         or row.get("url_topline") or row.get("url_crosstab") or "").strip(),
                    tracking=_truthy(row.get("tracking")),
                )
            qid = (pid, key, row.get("question_id") or "")
            if qid not in qmeta:
                rnd = _num(row.get("ranked_choice_round"))
                qmeta[qid] = dict(
                    population=(row.get("population") or "").strip().lower(),
                    n=_num(row.get("sample_size")),
                    rcv_round=int(rnd) if rnd is not None else None,
                    realloc=_truthy(row.get("ranked_choice_reallocated")),
                    order=len(qmeta),
                )
            if feed == "approval":
                for name, party, col in (("Approve", "YES", "yes"), ("Disapprove", "NO", "no")):
                    pct = _num(row.get(col))
                    if pct is not None:
                        qans[qid].append(Answer(name, party, pct, name))
                continue
            party = (row.get("party") or "").strip().upper()
            pct = _num(row.get("pct"))
            if pct is None:
                continue
            name = (row.get("candidate_name") or row.get("answer") or "").strip()
            if party == "NONE":
                # Undecided / someone else are part of the topline; non-voters are not.
                party = NONE_ANSWERS.get(fold(name), "")
                if not party or pct <= 0:
                    continue
                name = {"UND": "Undecided", "OTH": "Someone else"}[party]
            qans[qid].append(Answer(name, party, pct,
                                    row.get("candidate_id") or name))

    for qid, meta in qmeta.items():
        pid, key, question_id = qid
        answers = qans.get(qid) or []
        if not answers:
            continue
        if "primary" in key.stage:
            # One poll often asks both parties' primaries; each is its own race.
            key = key._replace(subject=primary_party(answers))
        polls[pid].races.setdefault(key, []).append(
            Question(qid=question_id, answers=tuple(answers), **meta))
    return {pid: p for pid, p in polls.items() if p.races}


def primary_party(answers) -> str:
    """DEM / REP for a party primary, "" for a top-two / jungle primary."""
    counts = Counter(a.party for a in answers)
    majors = [p for p in ("DEM", "REP") if counts.get(p)]
    return majors[0] if len(majors) == 1 else ""


def load_live() -> dict[str, Poll]:
    return build_polls({name: fetch_text(url) for name, url in FEEDS.items()})
