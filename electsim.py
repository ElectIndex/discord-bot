"""Pure election maths for the fun commands: historical tallies and random
hypotheticals. No I/O — unit-tested in tests/test_electsim.py."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

REGIONS = {
    "Northeast": "CT ME MA NH RI VT NJ NY PA",
    "Midwest": "IL IN MI OH WI IA KS MN MO NE ND SD",
    "South": "DE DC FL GA MD NC SC VA WV AL KY MS TN AR LA OK TX",
    "West": "AZ CO ID MT NV NM UT WY AK CA HI OR WA",
}
REGION_OF = {st: region for region, sts in REGIONS.items() for st in sts.split()}
CHAOS = {  # standard deviations, in margin points: national, regional, state/county
    "calm": (3.0, 1.5, 1.5),
    "normal": (5.0, 3.0, 3.0),
    "wild": (9.0, 6.0, 6.0),
}


def margin(votes: tuple[float, float, float]) -> float:
    """D−R as a share of all votes, in points."""
    d, r, o = votes
    total = d + r + o
    return 0.0 if total <= 0 else (d - r) / total * 100


def winner(votes: tuple[float, float, float]) -> str:
    d, r, o = votes
    if o > d and o > r:
        return "O"
    return "D" if d >= r else "R"


@dataclass
class Outcome:
    margins: dict[str, float]  # state -> D−R points
    winners: dict[str, str]  # state -> D|R|O
    ev: dict[str, int]  # D/R/O -> electoral votes
    popular: float  # national D−R points
    tipping: str | None = None
    totals: dict[str, float] = field(default_factory=dict)  # state -> total votes


def tally(winners: dict[str, str], ev_map: dict[str, int], adjustments=()) -> dict[str, int]:
    out = {"D": 0, "R": 0, "O": 0}
    for st, w in winners.items():
        out[w] += ev_map.get(st, 0)
    for _st, d, r, o in adjustments:
        out["D"] += d
        out["R"] += r
        out["O"] += o
    return out


def tipping_point(margins: dict[str, float], winners: dict[str, str], ev_map: dict[str, int], side: str) -> str | None:
    """The state that delivers the winner's majority, counting from their best state down."""
    need = sum(ev_map.values()) // 2 + 1
    ordered = sorted((st for st in margins if winners.get(st) == side),
                     key=lambda s: -margins[s] if side == "D" else margins[s])
    acc = 0
    for st in ordered:
        acc += ev_map.get(st, 0)
        if acc >= need:
            return st
    return None


def historical(state_votes: dict[str, tuple], ev_map: dict[str, int], adjustments=()) -> Outcome:
    margins = {st: margin(v) for st, v in state_votes.items() if st in ev_map}
    winners = {st: winner(v) for st, v in state_votes.items() if st in ev_map}
    ev = tally(winners, ev_map, adjustments)
    d = sum(v[0] for v in state_votes.values())
    r = sum(v[1] for v in state_votes.values())
    t = sum(sum(v) for v in state_votes.values())
    lead = "D" if ev["D"] > ev["R"] else "R"
    return Outcome(margins, winners, ev, (d - r) / t * 100 if t else 0.0,
                   tipping_point(margins, winners, ev_map, lead), {st: sum(v) for st, v in state_votes.items()})


def _noise(rng: random.Random, chaos: str):
    if chaos not in CHAOS:
        raise ValueError(f"chaos must be one of {', '.join(CHAOS)}")
    return CHAOS[chaos]


def simulate_president(base: dict[str, tuple], ev_map: dict[str, int], lean: float | None,
                       chaos: str = "normal", rng: random.Random | None = None) -> Outcome:
    """Shift a baseline year's state results by correlated national, regional and
    state noise. `lean` pins the national popular-vote margin (D+ positive); left
    out, the national result is drawn near a tie so the map is a real contest."""
    rng = rng or random.Random()
    sd_nat, sd_reg, sd_state = _noise(rng, chaos)
    states = [st for st in base if st in ev_map]
    totals = {st: sum(base[st]) for st in states}
    region_shift = {r: rng.gauss(0, sd_reg) for r in REGIONS}
    raw = {st: margin(base[st]) + region_shift.get(REGION_OF.get(st, ""), 0) + rng.gauss(0, sd_state) for st in states}
    grand = sum(totals.values()) or 1
    national_now = sum(raw[st] * totals[st] for st in states) / grand
    target = lean if lean is not None else rng.gauss(0, sd_nat)
    offset = target - national_now
    margins = {st: max(-99.0, min(99.0, raw[st] + offset)) for st in states}
    winners = {st: ("D" if m >= 0 else "R") for st, m in margins.items()}
    ev = tally(winners, ev_map)
    lead = "D" if ev["D"] > ev["R"] else "R"
    return Outcome(margins, winners, ev, target, tipping_point(margins, winners, ev_map, lead), totals)


def simulate_state(base: dict[str, tuple], lean: float | None, chaos: str = "normal",
                   rng: random.Random | None = None) -> tuple[dict[str, float], float]:
    """County margins for one state, and the statewide margin. `lean` pins the
    statewide margin; left out, it's drawn around the baseline's own result."""
    rng = rng or random.Random()
    sd_nat, _sd_reg, sd_county = _noise(rng, chaos)
    totals = {f: sum(v) for f, v in base.items() if sum(v) > 0}
    raw = {f: margin(base[f]) + rng.gauss(0, sd_county) for f in totals}
    grand = sum(totals.values()) or 1
    statewide_now = sum(raw[f] * totals[f] for f in totals) / grand
    base_statewide = sum(margin(base[f]) * totals[f] for f in totals) / grand
    target = lean if lean is not None else base_statewide + rng.gauss(0, sd_nat)
    offset = target - statewide_now
    return {f: max(-99.0, min(99.0, raw[f] + offset)) for f in totals}, target


def fmt_margin(m: float) -> str:
    if abs(m) < 0.05:
        return "Even"
    return f"{'D' if m > 0 else 'R'}+{abs(m):.1f}"
