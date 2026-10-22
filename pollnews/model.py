"""Vendored from ElectIndex/autoposter (pollposter.model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import NamedTuple


class RaceKey(NamedTuple):
    office: str      # SEN GOV HOUSE PRES GCB APPROVAL
    state: str       # postal code, "US" for national
    seat: int        # House district number, else 0
    stage: str       # general / primary / primary runoff / special primary ...
    subject: str = ""  # approval: the politician

    def ident(self) -> str:
        return "|".join(map(str, self))


@dataclass(frozen=True)
class Answer:
    name: str
    party: str   # DEM REP IND LIB ... or YES/NO for approval
    pct: float
    cid: str     # candidate id (stable across polls); name for approval


@dataclass(frozen=True)
class Question:
    qid: str
    population: str
    n: float | None
    rcv_round: int | None
    realloc: bool
    order: int
    answers: tuple[Answer, ...]

    def cset(self) -> frozenset:
        return frozenset(a.cid for a in self.answers)


@dataclass
class Poll:
    poll_id: str
    pollster_id: str
    pollster: str
    pollster_names: tuple[str, ...]
    sponsors: str
    partisan: str
    internal: bool
    start: date | None
    end: date | None
    created: datetime | None
    url: str
    tracking: bool = False
    races: dict[RaceKey, list[Question]] = field(default_factory=dict)
