"""Vendored from ElectIndex/autoposter (pollposter.meta).

Reference data that is not in the NYT feed: ElectIndex pollster ratings
(grades + the banned list) and 2026 incumbents, both read from the public
forecast data repo so they never drift from what the site shows."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

import requests

from .feeds import UA
from .text import fold

DATA_RAW = "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/main/"
log = logging.getLogger(__name__)

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "D.C.", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming", "PR": "Puerto Rico",
}
AT_LARGE = {"AK", "DE", "ND", "SD", "VT", "WY"}

# Short display names. Anything unlisted gets the generic suffix trim below.
SHORT = {
    "The New York Times/Siena University": "NYT/Siena",
    "The New York Times/Siena College": "NYT/Siena",
    "Marquette University Law School": "Marquette",
    "Marquette Law School": "Marquette",
    "Public Policy Polling": "PPP",
    "Beacon Research/Shaw & Company Research": "Beacon/Shaw",
    "Trafalgar Group/InsiderAdvantage": "Trafalgar/InsiderAdvantage",
    "McLaughlin & Associates": "McLaughlin",
    "University of New Hampshire Survey Center": "UNH",
    "Saint Anselm College Survey Center": "Saint Anselm",
    "Hart Research Associates": "Hart Research",
    "Lake Research Partners": "Lake Research",
    "Public Opinion Strategies": "POS",
    "Global Strategy Group": "GSG",
    "AP-NORC": "AP-NORC",
    "CNN/SSRS": "CNN/SSRS",
    "University of Houston Hobby School of Public Affairs": "UH Hobby School",
    "Texas Southern University School of Public Affairs": "TSU",
}
_TRIM = (" University", " College", " Group", " Inc.", ", Inc.")


MAX_POLLSTER = 40


def short_pollster(name: str) -> str:
    if name in SHORT:
        return SHORT[name]
    for suffix in _TRIM:
        if name.endswith(suffix) and len(name) > len(suffix) + 3:
            name = name[: -len(suffix)]
            break
    if len(name) > MAX_POLLSTER and "/" in name:
        name = name.split("/")[0]
    if len(name) > MAX_POLLSTER:
        name = name[: MAX_POLLSTER - 1].rstrip() + "…"
    return name


# ElectIndex rates pollsters 0-3 (538 scale). Letter grades for display.
GRADES = [(2.9, "A+"), (2.7, "A"), (2.5, "A-"), (2.3, "B+"), (2.1, "B"),
          (1.9, "B-"), (1.7, "C+"), (1.5, "C"), (1.3, "C-"), (1.1, "D+"), (0.5, "D")]


def grade(rating: float | None) -> str | None:
    if rating is None:
        return None
    for floor, letter in GRADES:
        if rating >= floor - 1e-9:
            return letter
    return None


def _name_parts(name: str) -> tuple[str, str]:
    toks = [t for t in fold(name).split() if t not in ("jr", "sr", "ii", "iii", "iv")]
    return (toks[0] if toks else "", toks[-1] if toks else "")


def same_person(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if fold(a) == fold(b):
        return True
    fa, la = _name_parts(a)
    fb, lb = _name_parts(b)
    return bool(la) and la == lb and fa[:1] == fb[:1]


@dataclass
class Meta:
    ratings: dict[str, float] = field(default_factory=dict)  # folded name -> rating
    banned: set[str] = field(default_factory=set)            # folded names
    incumbents: dict[str, str] = field(default_factory=dict)  # race_code -> name

    def rating_for(self, names) -> float | None:
        for n in names:
            if fold(n) in self.ratings:
                return self.ratings[fold(n)]
        return None

    def is_banned(self, names) -> bool:
        return any(fold(n) in self.banned for n in names)

    def incumbent(self, race_code: str) -> str | None:
        return self.incumbents.get(race_code)


def parse_meta(ratings_csv: str | None, races_csv: str | None) -> Meta:
    m = Meta()
    if ratings_csv:
        for r in csv.DictReader(io.StringIO(ratings_csv)):
            name = fold(r.get("pollster", ""))
            try:
                rating = float(r.get("rating") or "nan")
            except ValueError:
                continue
            if str(r.get("banned", "")).strip().lower() in ("1", "true", "yes"):
                m.banned.add(name)
            elif rating == rating and rating > 0:
                m.ratings[name] = rating
    if races_csv:
        col = {"DEM": "dem_name", "REP": "rep_name", "IND": "ind_name"}
        for r in csv.DictReader(io.StringIO(races_csv)):
            c = col.get((r.get("incumbent_party") or "").strip().upper())
            if c and (r.get(c) or "").strip():
                m.incumbents[r["race_code"]] = r[c].strip()
    return m


def _get(path: str) -> str | None:
    try:
        r = requests.get(DATA_RAW + path, headers=UA, timeout=30)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:  # grades/(i) are cosmetic; never block posting
        log.warning("could not fetch %s: %s", path, e)
        return None


def load_meta() -> Meta:
    return parse_meta(_get("pollster_ratings.csv"), _get("races.csv"))
