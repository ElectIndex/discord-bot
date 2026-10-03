"""Election data for the fun commands, fetched from electindex.com and the public
forecast repo at runtime and cached on disk — the bot never vendors a copy.

Everything here is parsing and caching; the maths lives in electsim.py and the
drawing in electmap.py.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

import aiohttp

log = logging.getLogger("electindex-bot.electdata")

ASSETS = "https://electindex.com/wp-content/themes/electindex/assets"
FORECAST = "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/main/output"
WEEK, QUARTER_HOUR = 7 * 86400, 15 * 60
SOURCES = {
    "states": (f"{ASSETS}/forecasts/states-albers.js", WEEK),
    "counties": (f"{ASSETS}/shuffler/counties-albers-10m.js", WEEK),
    "pres": (f"{ASSETS}/shuffler/shuffler-data.js", WEEK),
    "ev": (f"{ASSETS}/mapper/ev-tables.js", WEEK),
    "congress": (f"{ASSETS}/simulator/sim-history-data.js", WEEK),
    "races": (f"{FORECAST}/races_summary.csv", QUARTER_HOUR),
    "chambers": (f"{FORECAST}/chambers.csv", QUARTER_HOUR),
}
CACHE_DIR = Path(__file__).resolve().parent / "data" / "cache"
USER_AGENT = "ElectIndexBot/1.0 (+https://github.com/ElectIndex/discord-bot)"

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
FIPS_TO_USPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY",
}

# Presidential tickets [Democrat, Republican, notable third] — public record.
PRES_CANDIDATES = {
    1928: ("Al Smith", "Herbert Hoover", None), 1932: ("Franklin D. Roosevelt", "Herbert Hoover", None),
    1936: ("Franklin D. Roosevelt", "Alf Landon", None), 1940: ("Franklin D. Roosevelt", "Wendell Willkie", None),
    1944: ("Franklin D. Roosevelt", "Thomas E. Dewey", None), 1948: ("Harry S. Truman", "Thomas E. Dewey", "Strom Thurmond"),
    1952: ("Adlai Stevenson", "Dwight D. Eisenhower", None), 1956: ("Adlai Stevenson", "Dwight D. Eisenhower", None),
    1960: ("John F. Kennedy", "Richard Nixon", "Harry F. Byrd"), 1964: ("Lyndon B. Johnson", "Barry Goldwater", None),
    1968: ("Hubert Humphrey", "Richard Nixon", "George Wallace"), 1972: ("George McGovern", "Richard Nixon", None),
    1976: ("Jimmy Carter", "Gerald Ford", None), 1980: ("Jimmy Carter", "Ronald Reagan", "John Anderson"),
    1984: ("Walter Mondale", "Ronald Reagan", None), 1988: ("Michael Dukakis", "George H. W. Bush", None),
    1992: ("Bill Clinton", "George H. W. Bush", "Ross Perot"), 1996: ("Bill Clinton", "Bob Dole", "Ross Perot"),
    2000: ("Al Gore", "George W. Bush", "Ralph Nader"), 2004: ("John Kerry", "George W. Bush", None),
    2008: ("Barack Obama", "John McCain", None), 2012: ("Barack Obama", "Mitt Romney", None),
    2016: ("Hillary Clinton", "Donald Trump", None), 2020: ("Joe Biden", "Donald Trump", None),
    2024: ("Kamala Harris", "Donald Trump", None),
}
# Electors who didn't follow the statewide result (faithless/unpledged), from the
# Simulator's EC_FIXES, plus the Maine/Nebraska district splits — so historical
# totals match what was actually certified. (state, d, r, other)
EV_ADJUSTMENTS = {
    1948: [("TN", -1, 0, 1)], 1956: [("AL", -1, 0, 1)], 1960: [("AL", -6, 0, 6), ("OK", 0, -1, 1)],
    1968: [("NC", 0, -1, 1)], 1972: [("VA", 0, -1, 1)], 1976: [("WA", 0, -1, 1)], 1988: [("WV", -1, 0, 1)],
    2000: [("DC", -1, 0, 1)], 2004: [("MN", -1, 0, 1)], 2008: [("NE", 1, -1, 0)],
    2016: [("TX", 0, -2, 2), ("WA", -4, 0, 4), ("HI", -1, 0, 1), ("ME", -1, 1, 0)],
    2020: [("ME", -1, 1, 0), ("NE", 1, -1, 0)], 2024: [("ME", -1, 1, 0), ("NE", 1, -1, 0)],
}
PRES_YEARS = tuple(range(1928, 2025, 4))


def parse_js_json(text: str):
    """`window.X = {...};` → the object."""
    start = text.index("=", text.index("window.")) + 1
    body = text[start:].strip().rstrip(";").strip()
    return json.loads(body)


def decode_topojson(topo: dict, object_name: str) -> dict[str, list[list[list[tuple[float, float]]]]]:
    """TopoJSON object → {id: [polygon, ...]}, each polygon a list of rings of (x, y).
    Handles quantized arcs (delta-encoded with a transform)."""
    tf = topo.get("transform")
    arcs = []
    for arc in topo["arcs"]:
        pts, x, y = [], 0, 0
        for p in arc:
            if tf:
                x, y = x + p[0], y + p[1]
                pts.append((x * tf["scale"][0] + tf["translate"][0], y * tf["scale"][1] + tf["translate"][1]))
            else:
                pts.append((p[0], p[1]))
        arcs.append(pts)

    def ring(indices):
        out = []
        for i in indices:
            seg = arcs[i] if i >= 0 else list(reversed(arcs[~i]))
            out.extend(seg if not out else seg[1:])
        return out

    shapes = {}
    for g in topo["objects"][object_name]["geometries"]:
        if g["type"] == "Polygon":
            polys = [[ring(r) for r in g["arcs"]]]
        elif g["type"] == "MultiPolygon":
            polys = [[ring(r) for r in poly] for poly in g["arcs"]]
        else:
            continue
        shapes[str(g.get("id"))] = polys
    return shapes


class ElectionData:
    def __init__(self, session_factory=None):
        self._session: aiohttp.ClientSession | None = None
        self._parsed: dict[str, tuple[float, object]] = {}
        self._locks = defaultdict(asyncio.Lock)

    async def close(self):
        if self._session:
            await self._session.close()

    async def _raw(self, key: str) -> str:
        url, ttl = SOURCES[key]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{key}.cache"
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return path.read_text(encoding="utf-8")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers={"User-Agent": USER_AGENT})
        try:
            async with self._session.get(url) as r:
                r.raise_for_status()
                text = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if path.exists():  # stale beats nothing
                log.warning("Refetch of %s failed; using the cached copy", key)
                return path.read_text(encoding="utf-8")
            raise
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return text

    async def _get(self, key: str, parse):
        ttl = SOURCES[key][1]
        async with self._locks[key]:
            hit = self._parsed.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            text = await self._raw(key)
            value = await asyncio.to_thread(parse, text)
            self._parsed[key] = (time.time(), value)
            return value

    # ---- geometry -------------------------------------------------------

    async def state_shapes(self) -> dict[str, list]:
        def parse(text):
            fc = parse_js_json(text)
            return {f["properties"]["state"]: (f["geometry"]["coordinates"] if f["geometry"]["type"] == "MultiPolygon"
                                                else [f["geometry"]["coordinates"]]) for f in fc["features"]}
        return await self._get("states", parse)

    async def county_shapes(self) -> dict[str, list]:
        return await self._get("counties", lambda t: decode_topojson(parse_js_json(t), "counties"))

    # ---- presidential archive (1928–2024, county level) -------------------

    async def pres(self) -> dict:
        """{'county': {fips: {year: (d, r, o)}}, 'state': {year: {st: (d, r, o)}}, 'names': {fips: name}, 'county_state': {fips: st}}"""
        def parse(text):
            data = parse_js_json(text)
            county, state, names, cstate = {}, defaultdict(lambda: defaultdict(lambda: [0, 0, 0])), {}, {}
            for row in data["counties"]:
                f = row["f"]
                names[f] = row.get("n", "").strip()
                cstate[f] = row["s"]
                county[f] = {int(y): tuple(v) for y, v in (row.get("r") or {}).items()}
                for y, v in county[f].items():
                    acc = state[y][row["s"]]
                    acc[0] += v[0]; acc[1] += v[1]; acc[2] += v[2]
            return {"county": county, "state": {y: {s: tuple(v) for s, v in m.items()} for y, m in state.items()},
                    "names": names, "county_state": cstate}
        return await self._get("pres", parse)

    async def electoral_votes(self) -> dict[int, dict[str, int]]:
        return await self._get("ev", lambda t: {int(y): m for y, m in parse_js_json(t)["ev"].items()})

    # ---- Congress (1976–2024) ----------------------------------------------

    async def congress(self) -> dict:
        """{'senate'|'house': {year: [row, ...]}} with rows {st, d, sp, dv, rv, ov, tv, dn, rn, win}."""
        return await self._get("congress", lambda t: {k: {int(y): v for y, v in m.items()} for k, m in parse_js_json(t).items()})

    # ---- 2026 forecast ----------------------------------------------------

    async def races(self) -> dict[str, dict]:
        return await self._get("races", lambda t: {r["race_code"]: r for r in csv.DictReader(io.StringIO(t))})

    async def chambers(self) -> dict[str, dict]:
        return await self._get("chambers", lambda t: {r["chamber"].lower(): r for r in csv.DictReader(io.StringIO(t))})


def normalise_state(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if t.upper() in STATE_NAMES:
        return t.upper()
    for code, name in STATE_NAMES.items():
        if name.lower() == t.lower():
            return code
    return None


def parse_lean(text: str | None) -> float | None:
    """'D+3' → 3.0, 'R+2.5' → -2.5, 'even'/'tie' → 0, '4' → 4 (positive = Democratic)."""
    if text is None or not text.strip():
        return None
    t = text.strip().upper().replace(" ", "")
    if t in ("EVEN", "TIE", "TIED", "0"):
        return 0.0
    m = re.fullmatch(r"([DR])\+?(\d+(?:\.\d+)?)", t) or re.fullmatch(r"(\d+(?:\.\d+)?)([DR])", t)
    if m:
        a, b = m.groups()
        party, value = (a, b) if a in "DR" else (b, a)
        return float(value) if party == "D" else -float(value)
    try:
        return float(t)
    except ValueError:
        raise ValueError(f"Couldn't read lean '{text}'. Try D+3, R+2 or even.")
