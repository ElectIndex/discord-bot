"""NYT feed -> Cards, and which races are new since the last pass.

Question selection is vendored from pollposter.build (same rules, so the
Discord cards and the @ElectIndex autoposter never disagree about which
numbers a poll "has"):

* One population per race: LV > RV > voters > adults, never a mix.
* Ranked-choice ladders show round 1 and the final round only.
* Questions with different candidate sets are alternate matchups and are
  all shown; duplicate candidate sets (with/without leaners) collapse.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .card import Block, Card, Line, race_tag
from .meta import Meta, same_person
from .model import Poll, Question, RaceKey
from .text import short_name

POP_PREF = ["lv", "rv", "v", "a"]
OFFICE_ORDER = ["SEN", "GOV", "HOUSE", "GCB", "APPROVAL", "FAV", "PRES", "OTHER"]
STALE_DAYS = 30
GCB_NAMES = {"DEM": "Democrats", "REP": "Republicans"}
TAIL = ("UND", "OTH")
VERBS = {"APPROVAL": {"YES": "approve", "NO": "disapprove"},
         "FAV": {"YES": "favorable", "NO": "unfavorable"}}   # always listed last, after the candidates


def pick_population(qs: list[Question]) -> str:
    pops = {q.population for q in qs}
    for p in POP_PREF:
        if p in pops:
            return p
    return sorted(pops)[0]


def _dedupe(qs: list[Question]) -> list[Question]:
    seen, out = set(), []
    for q in sorted(qs, key=lambda q: (q.realloc, q.order)):
        if q.cset() not in seen:
            seen.add(q.cset())
            out.append(q)
    return sorted(out, key=lambda q: q.order)


def select_questions(qs: list[Question]) -> list[tuple[str | None, Question]]:
    rounds = [q for q in qs if q.rcv_round is not None]
    if any(q.rcv_round >= 2 for q in rounds):
        r1 = [q for q in rounds if q.rcv_round == 1]
        last_round = max(q.rcv_round for q in rounds)
        out: list[tuple[str | None, Question]] = []
        if r1:
            out.append(("Round 1", min(r1, key=lambda q: (q.realloc, q.order))))
        out.append(("Final round", min((q for q in rounds if q.rcv_round == last_round),
                                       key=lambda q: (q.realloc, q.order))))
        rcv_sets = {r.cset() for r in rounds}
        rcv_ids = {q.qid for q in rounds}
        out += [(None, q) for q in _dedupe([q for q in qs if q.qid not in rcv_ids])
                if q.cset() not in rcv_sets]
        return out
    qs = sorted(_dedupe(qs), key=lambda q: (-len(q.answers), q.order))
    return [(None, q) for q in qs]


def lines_for(q: Question, key: RaceKey, incumbent: str | None) -> list[Line]:
    if key.office in VERBS:
        who = short_name(key.subject) if key.subject else "President"
        verb = VERBS[key.office]
        return [Line(f"{who} {verb[a.party]}", a.party, a.pct)
                for a in q.answers if a.party in verb]
    ranked = sorted(q.answers, key=lambda a: (a.party in TAIL, a.party == "OTH", -a.pct))
    out = []
    for a in ranked:
        if a.party in TAIL:
            name = a.name
        elif key.office == "GCB":
            name = GCB_NAMES.get(a.party, a.name)
        else:
            name = short_name(a.name)
        out.append(Line(name, a.party, a.pct,
                        incumbent=bool(incumbent) and a.party not in TAIL
                        and same_person(a.name, incumbent)))
    # Two "Smith"s in one primary: last names alone would be ambiguous.
    names = [l.name for l in out]
    for l, a in zip(out, ranked):
        if names.count(l.name) > 1 and a.party not in TAIL:
            l.name = a.name
    return out


def race_code(key: RaceKey) -> str:
    """The races.csv code the incumbent lookup is keyed by."""
    if key.office in ("SEN", "GOV"):
        return f"{key.state}-{key.office}"
    if key.office == "HOUSE":
        return f"{key.state}-{max(key.seat, 1):02d}"
    return ""


def card_for(poll: Poll, key: RaceKey, meta: Meta) -> Card:
    qs = poll.races[key]
    pop = pick_population(qs)
    picked = select_questions([q for q in qs if q.population == pop])
    inc = meta.incumbent(race_code(key))
    blocks = [Block(lines_for(q, key, inc), label) for label, q in picked]
    n = max((q.n for _, q in picked if q.n), default=None)
    return Card(
        tag=race_tag(key.office, key.state, key.seat),
        pollster=poll.pollster,
        sponsor=", ".join(s.strip() for s in poll.sponsors.split(",") if s.strip()),
        partisan=poll.partisan,
        blocks=[b for b in blocks if b.lines],
        start=poll.start, end=poll.end,
        n=int(n) if n else None,
        population=pop,
        url=poll.url,
    )


def ordered_keys(keys) -> list[RaceKey]:
    return sorted(keys, key=lambda k: (OFFICE_ORDER.index(k.office), k.state, k.seat,
                                       k.stage != "general", k.stage, k.subject))


# ---------------------------------------------------------------- new races

def fingerprint(poll: Poll, keys) -> str:
    h = hashlib.sha256()
    for k in sorted(keys, key=lambda k: k.ident()):
        for q in sorted(poll.races[k], key=lambda q: q.qid):
            h.update(f"{k.ident()}|{q.qid}|{q.population}|{q.n}".encode())
            for a in sorted(q.answers, key=lambda a: a.cid):
                h.update(f"|{a.cid}={a.pct}".encode())
    return h.hexdigest()[:16]


@dataclass
class Decision:
    ready: list[tuple[str, RaceKey]]     # post these
    skipped: list[tuple[str, RaceKey, str]]  # mark seen, never post (reason)
    pending: dict[str, str]              # poll_id -> fingerprint of its unseen rows


def new_races(polls: dict[str, Poll], is_seen, pending: dict[str, str], meta: Meta,
              today: date) -> Decision:
    """A poll's unseen races go out only after two consecutive passes saw the
    same rows, so a poll the NYT is still typing in posts whole."""
    ready, skipped, next_pending = [], [], {}
    for pid, p in polls.items():
        keys = [k for k in p.races if not is_seen(pid, k.ident())]
        if not keys:
            continue
        reason = None
        if meta.is_banned(p.pollster_names):
            reason = "banned pollster"
        elif p.end and p.end < today - timedelta(days=STALE_DAYS):
            reason = f"ended {p.end}, older than {STALE_DAYS} days"
        if reason:
            skipped += [(pid, k, reason) for k in keys]
            continue
        fp = fingerprint(p, keys)
        next_pending[pid] = fp
        if pending.get(pid) != fp:
            continue
        ready += [(pid, k) for k in ordered_keys(keys)]
    ready.sort(key=lambda t: (polls[t[0]].created or datetime.min, t[0]))
    return Decision(ready, skipped, next_pending)
