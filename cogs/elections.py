"""Fun election commands: /simulate, /history and /forecast.

Data comes from electindex.com and the public forecast repo (electdata.py), the
maths from electsim.py, and the maps from electmap.py.
"""

import asyncio
import io
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

import electmap
from electdata import (EV_ADJUSTMENTS, PRES_CANDIDATES, PRES_YEARS, STATE_NAMES, ElectionData,
                       normalise_state, parse_lean)
from electsim import CHAOS, fmt_margin, historical, margin, simulate_president, simulate_state, winner
from ui import NAVY, RED, SITE_URL, error_embed, link_buttons, make_embed

log = logging.getLogger("electindex-bot.elections")

CHAOS_CHOICES = [app_commands.Choice(name=f"{k} ({'small' if k == 'calm' else 'big' if k == 'wild' else 'typical'} swings)", value=k) for k in CHAOS]
CHAMBER_CHOICES = [app_commands.Choice(name=n, value=n.lower()) for n in ("Senate", "House", "Governors")]
RACE_TYPE_LABEL = {"senate": "Senate", "governor": "Governor", "house": "House"}


def _png(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=name)


def _clean_name(name: str | None, default: str) -> str:
    name = (name or "").strip()
    return discord.utils.escape_mentions(name[:32]) if name else default


def _pct(part: float, total: float) -> str:
    return f"{part / total * 100:.1f}%" if total else "—"


async def state_autocomplete(_: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    cur = current.lower()
    hits = [(c, n) for c, n in STATE_NAMES.items() if cur in n.lower() or cur == c.lower()] if cur else list(STATE_NAMES.items())
    return [app_commands.Choice(name=f"{n} ({c})", value=c) for c, n in hits[:25]]


class Elections(commands.Cog):
    """Simulate elections, look up history, and check the ElectIndex forecast."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = ElectionData()

    async def cog_load(self):
        asyncio.get_running_loop().create_task(self._prefetch())

    async def cog_unload(self):
        await self.data.close()

    async def _prefetch(self):
        # Warm the caches so the first command isn't the slow one.
        for loader in (self.data.state_shapes, self.data.electoral_votes, self.data.pres, self.data.county_shapes,
                       self.data.congress, self.data.races, self.data.chambers):
            try:
                await loader()
            except Exception:  # noqa: BLE001 — a cold cache is fine; commands retry.
                log.warning("Prefetch of %s failed", loader.__name__, exc_info=True)

    async def _fail(self, ctx: commands.Context, message: str):
        await ctx.send(embed=error_embed(message), ephemeral=True)

    # ================================================================ /simulate

    @commands.hybrid_group(name="simulate", description="Simulate a random hypothetical election")
    async def simulate(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=make_embed("🎲  Simulate", "`/simulate president` for a national map, or `/simulate state` for one state's counties."))

    @simulate.command(name="president", description="A random hypothetical presidential election, with a map")
    @app_commands.describe(
        lean="National popular vote, e.g. D+3, R+2 or even. Leave out for a random close race",
        chaos="How much states move independently",
        baseline="Which year's map to start from (1928–2024, default 2024)",
        dem="The Democratic candidate's name", rep="The Republican candidate's name",
        seed="Replay a specific simulation",
    )
    @app_commands.choices(chaos=CHAOS_CHOICES)
    async def simulate_president(self, ctx: commands.Context, lean: str | None = None, chaos: str = "normal",
                                 baseline: int = 2024, dem: str | None = None, rep: str | None = None,
                                 seed: int | None = None):
        if baseline not in PRES_YEARS:
            return await self._fail(ctx, "Baseline must be a presidential year from 1928 to 2024.")
        if chaos not in CHAOS:
            return await self._fail(ctx, "Chaos must be calm, normal or wild.")
        try:
            lean_value = parse_lean(lean)
        except ValueError as e:
            return await self._fail(ctx, str(e))
        await ctx.defer()
        seed = seed if seed is not None else random.randint(1, 999_999)
        pres, ev, shapes = await self.data.pres(), await self.data.electoral_votes(), await self.data.state_shapes()
        ev_map = ev[2024]
        result = simulate_president(pres["state"][baseline], ev_map, lean_value, chaos, random.Random(seed))
        names = (_clean_name(dem, "Democrat"), _clean_name(rep, "Republican"), None)
        need = sum(ev_map.values()) // 2 + 1
        sub = f"Popular vote {fmt_margin(result.popular)} · {baseline} map · {chaos} chaos · seed {seed}"
        png = await asyncio.to_thread(electmap.national, shapes, result.margins, result.winners, result.ev, names,
                                      "Hypothetical presidential election", sub, need)

        d, r = result.ev["D"], result.ev["R"]
        if d == r:
            headline = f"**It's a {d}–{r} tie** — the House decides."
        else:
            win_name, win_ev, lose_ev = (names[0], d, r) if d > r else (names[1], r, d)
            headline = f"**{win_name} wins, {win_ev}–{lose_ev}.**"
        closest = sorted(result.margins, key=lambda s: abs(result.margins[s]))[:5]
        embed = make_embed("🎲  Hypothetical presidential election",
                           f"{headline}\nPopular vote **{fmt_margin(result.popular)}** · tipping point **{STATE_NAMES.get(result.tipping, result.tipping or '—')}**",
                           color=NAVY if d >= r else RED)
        embed.add_field(name="Closest states", value="\n".join(f"{STATE_NAMES[s]} {fmt_margin(result.margins[s])}" for s in closest), inline=True)
        embed.add_field(name="Settings", value=f"Map: {baseline}\nChaos: {chaos}\nSeed: `{seed}`", inline=True)
        embed.set_image(url="attachment://simulation.png")
        embed.set_footer(text="A random hypothetical, not a forecast · current electoral votes; ME and NE awarded statewide")
        await ctx.send(embed=embed, file=_png(png, "simulation.png"))

    @simulate.command(name="state", description="A random hypothetical result in one state, with a county map")
    @app_commands.describe(
        state="Which state", lean="Statewide margin, e.g. D+3 or R+2. Leave out for a random result near the baseline",
        chaos="How much counties move independently", baseline="Which year's results to start from (1928–2024)",
        dem="The Democratic candidate's name", rep="The Republican candidate's name", seed="Replay a specific simulation",
    )
    @app_commands.choices(chaos=CHAOS_CHOICES)
    @app_commands.autocomplete(state=state_autocomplete)
    async def simulate_state_cmd(self, ctx: commands.Context, state: str, lean: str | None = None, chaos: str = "normal",
                                 baseline: int = 2024, dem: str | None = None, rep: str | None = None,
                                 seed: int | None = None):
        st = normalise_state(state)
        if st is None:
            return await self._fail(ctx, f"I don't know the state '{state}'.")
        if baseline not in PRES_YEARS:
            return await self._fail(ctx, "Baseline must be a presidential year from 1928 to 2024.")
        if chaos not in CHAOS:
            return await self._fail(ctx, "Chaos must be calm, normal or wild.")
        try:
            lean_value = parse_lean(lean)
        except ValueError as e:
            return await self._fail(ctx, str(e))
        await ctx.defer()
        seed = seed if seed is not None else random.randint(1, 999_999)
        pres, counties = await self.data.pres(), await self.data.county_shapes()
        fips = [f for f, s in pres["county_state"].items() if s == st]
        base = {f: pres["county"][f][baseline] for f in fips if baseline in pres["county"][f] and sum(pres["county"][f][baseline]) > 0}
        if not base:
            return await ctx.send(embed=error_embed(f"There's no {baseline} county data for {STATE_NAMES[st]}."))
        county_margins, statewide = simulate_state(base, lean_value, chaos, random.Random(seed))
        names = (_clean_name(dem, "Democrat"), _clean_name(rep, "Republican"))
        win_name = names[0] if statewide >= 0 else names[1]
        result_line = f"{win_name} wins, {fmt_margin(statewide)}"
        sub = f"{baseline} baseline · {chaos} chaos · seed {seed}"
        winners = {f: ("D" if m >= 0 else "R") for f, m in county_margins.items()}
        png = await asyncio.to_thread(electmap.state_counties, counties, fips, county_margins, winners,
                                      f"{STATE_NAMES[st]}: hypothetical", sub, result_line)
        d_counties = sum(1 for w in winners.values() if w == "D")
        embed = make_embed(f"🎲  {STATE_NAMES[st]}: hypothetical", f"**{result_line}**", color=NAVY if statewide >= 0 else RED)
        embed.add_field(name="Counties", value=f"{names[0]} {d_counties} · {names[1]} {len(winners) - d_counties}", inline=True)
        embed.add_field(name="Settings", value=f"Baseline: {baseline}\nChaos: {chaos}\nSeed: `{seed}`", inline=True)
        embed.set_image(url="attachment://simulation.png")
        embed.set_footer(text="A random hypothetical, not a forecast")
        await ctx.send(embed=embed, file=_png(png, "simulation.png"))

    # ================================================================= /history

    @commands.hybrid_group(name="history", description="Look up past election results")
    async def history(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=make_embed("📜  History", "`/history president`, `/history senate` or `/history house`."))

    @history.command(name="president", description="A presidential election from 1928 to 2024, with a map")
    @app_commands.describe(year="Election year (1928–2024)", state="Optional: one state's result and county map")
    @app_commands.autocomplete(state=state_autocomplete)
    async def history_president(self, ctx: commands.Context, year: int, state: str | None = None):
        if year not in PRES_YEARS:
            return await self._fail(ctx, "Pick a presidential year from 1928 to 2024 (every four years).")
        st = normalise_state(state) if state else None
        if state and st is None:
            return await self._fail(ctx, f"I don't know the state '{state}'.")
        await ctx.defer()
        pres, ev = await self.data.pres(), await self.data.electoral_votes()
        dn, rn, tn = PRES_CANDIDATES[year]
        if st is None:
            shapes = await self.data.state_shapes()
            o = historical(pres["state"][year], ev[year], EV_ADJUSTMENTS.get(year, ()))
            need = sum(ev[year].values()) // 2 + 1
            names = (dn, rn, tn or "Other")
            sub = f"Popular vote {fmt_margin(o.popular)} · tipping point {o.tipping or '—'}"
            png = await asyncio.to_thread(electmap.national, shapes, o.margins, o.winners, o.ev, names,
                                          f"{year} presidential election", sub, need)
            won = dn if o.ev["D"] > o.ev["R"] else rn
            embed = make_embed(f"📜  {year} presidential election",
                               f"**{won}** won, **{max(o.ev['D'], o.ev['R'])}** electoral votes to **{min(o.ev['D'], o.ev['R'])}**"
                               + (f" ({tn or 'others'}: {o.ev['O']})" if o.ev["O"] else "")
                               + f".\nPopular vote **{fmt_margin(o.popular)}** · tipping point **{STATE_NAMES.get(o.tipping, '—')}**",
                               color=NAVY if o.ev["D"] > o.ev["R"] else RED)
            closest = sorted(o.margins, key=lambda s: abs(o.margins[s]))[:5]
            embed.add_field(name="Closest states", value="\n".join(f"{STATE_NAMES[s]} {fmt_margin(o.margins[s])}" for s in closest), inline=True)
            embed.add_field(name="Tickets", value=f"D: {dn}\nR: {rn}" + (f"\nOther: {tn}" if tn else ""), inline=True)
            embed.set_image(url="attachment://history.png")
            return await ctx.send(embed=embed, file=_png(png, "history.png"))

        counties = await self.data.county_shapes()
        votes = pres["state"][year].get(st)
        if not votes:
            return await ctx.send(embed=error_embed(f"{STATE_NAMES[st]} has no {year} presidential result (it wasn't a state yet, or didn't vote for president)."))
        fips = [f for f, s in pres["county_state"].items() if s == st]
        cv = {f: pres["county"][f][year] for f in fips if year in pres["county"][f] and sum(pres["county"][f][year]) > 0}
        cm = {f: margin(v) for f, v in cv.items()}
        cw = {f: winner(v) for f, v in cv.items()}
        w = winner(votes)
        who = {"D": dn, "R": rn, "O": tn or "a third-party candidate"}[w]
        total = sum(votes)
        line = f"{who} won, {fmt_margin(margin(votes))}" if w != "O" else f"{who} won"
        png = await asyncio.to_thread(electmap.state_counties, counties, fips, cm, cw, f"{STATE_NAMES[st]}, {year}",
                                      f"Presidential election · {ev[year].get(st, 0)} electoral votes", line)
        embed = make_embed(f"📜  {STATE_NAMES[st]}, {year}", f"**{line}**", color=NAVY if w == "D" else RED if w == "R" else NAVY)
        embed.add_field(name="Vote share", value=f"{dn} (D) {_pct(votes[0], total)}\n{rn} (R) {_pct(votes[1], total)}\nOther {_pct(votes[2], total)}", inline=True)
        embed.add_field(name="Counties", value=f"D {sum(1 for x in cw.values() if x == 'D')} · R {sum(1 for x in cw.values() if x == 'R')}"
                        + (f" · Other {sum(1 for x in cw.values() if x == 'O')}" if "O" in cw.values() else ""), inline=True)
        embed.set_image(url="attachment://history.png")
        await ctx.send(embed=embed, file=_png(png, "history.png"))

    def _race_line(self, row: dict) -> str:
        total = row["tv"] or (row["dv"] + row["rv"] + row["ov"])
        dn = row["dn"] or "No Democrat"
        rn = row["rn"] or "No Republican"
        special = " (special)" if row.get("sp") else ""
        if row["rv"] == 0 and row["dv"] > 0:
            return f"**{dn}** (D) unopposed by a Republican{special}"
        if row["dv"] == 0 and row["rv"] > 0:
            return f"**{rn}** (R) unopposed by a Democrat{special}"
        d_pct, r_pct = _pct(row["dv"], total), _pct(row["rv"], total)
        win = row.get("win")
        if (win == "DEM" and row["dv"] < row["rv"]) or (win == "REP" and row["rv"] < row["dv"]):
            # The stored votes are the first count, summed by party (several candidates
            # of one party can split it), so they're labelled as party totals.
            how = "the ranked-choice count" if row["st"] in ("AK", "ME") else "the runoff"
            name, party = (dn, "D") if win == "DEM" else (rn, "R")
            return f"**{name}** ({party}) won {how}{special} · first-round party totals: D {d_pct}, R {r_pct}"
        if win == "DEM":
            return f"**{dn}** (D) {d_pct} def. {rn} (R) {r_pct}{special}"
        if win == "REP":
            return f"**{rn}** (R) {r_pct} def. {dn} (D) {d_pct}{special}"
        return f"Won by another candidate · {dn} (D) {d_pct}, {rn} (R) {r_pct}{special}"

    @staticmethod
    def _district_key(d: str) -> str:
        """'AL', 'at-large', '0', '00' → '0'; '07' → '7'."""
        d = d.strip().upper().replace(" ", "")
        if d in ("AL", "ATLARGE", "AT-LARGE", ""):
            return "0"
        return d.lstrip("0") or "0"

    async def _congress_lookup(self, ctx, chamber: str, st: str, district: str | None, year: int | None):
        hist = (await self.data.congress())[chamber]
        want = None if district is None else self._district_key(district)
        rows = [(y, r) for y, rs in sorted(hist.items()) for r in rs
                if r["st"] == st and (want is None or self._district_key(str(r.get("d", ""))) == want)]
        if year is not None:
            rows = [(y, r) for y, r in rows if y == year]
        label = STATE_NAMES[st] + ("" if chamber == "senate" else (" at-large" if want == "0" else f"-{want}"))
        office = "Senate" if chamber == "senate" else "House"
        if not rows:
            when = f" in {year}" if year else " from 1976 to 2024"
            return await ctx.send(embed=error_embed(f"No {office} results for {label}{when}."))
        lines = [f"`{y}` {self._race_line(r)}" for y, r in rows]
        if year is None:
            lines = lines[::-1]
        text, shown = "", 0
        for line in lines:
            if len(text) + len(line) + 1 > 3900:
                break
            text += line + "\n"
            shown += 1
        title = f"📜  {office}: {label}" + (f", {year}" if year else "")
        embed = make_embed(title, text)
        if shown < len(lines):
            embed.set_footer(text=f"Showing the {shown} most recent of {len(lines)} results · add a year to narrow it")
        elif chamber == "house":
            embed.set_footer(text="District numbers follow each decade's lines, so a district isn't the same place in every year")
        await ctx.send(embed=embed)

    @history.command(name="senate", description="Senate results for a state, 1976–2024")
    @app_commands.describe(state="Which state", year="Optional: just one election year")
    @app_commands.autocomplete(state=state_autocomplete)
    async def history_senate(self, ctx: commands.Context, state: str, year: int | None = None):
        st = normalise_state(state)
        if st is None:
            return await self._fail(ctx, f"I don't know the state '{state}'.")
        await ctx.defer()
        await self._congress_lookup(ctx, "senate", st, None, year)

    @history.command(name="house", description="House results for a district, 1976–2024")
    @app_commands.describe(state="Which state", district="District number (AL for at-large)", year="Optional: just one election year")
    @app_commands.autocomplete(state=state_autocomplete)
    async def history_house(self, ctx: commands.Context, state: str, district: str, year: int | None = None):
        st = normalise_state(state)
        if st is None:
            return await self._fail(ctx, f"I don't know the state '{state}'.")
        await ctx.defer()
        await self._congress_lookup(ctx, "house", st, district.strip(), year)

    # ================================================================ /forecast

    @commands.hybrid_group(name="forecast", description="The ElectIndex 2026 forecast")
    async def forecast(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=make_embed("📊  Forecast", "`/forecast chamber` for the Senate, House or governors, or `/forecast race` for one race."))

    @forecast.command(name="chamber", description="Who's favoured to control the Senate, the House or the governorships")
    @app_commands.choices(chamber=CHAMBER_CHOICES)
    async def forecast_chamber(self, ctx: commands.Context, chamber: str):
        key = chamber.strip().lower()
        key = {"governor": "governors", "gov": "governors"}.get(key, key)
        if key not in ("senate", "house", "governors"):
            return await self._fail(ctx, "Pick senate, house or governors.")
        await ctx.defer()
        chambers, races = await self.data.chambers(), await self.data.races()
        c = chambers[key]
        d_pct = float(c["dem_control_pct"])
        fav = "Democrats" if d_pct >= 50 else "Republicans"
        title = {"senate": "Senate", "house": "House", "governors": "Governorships"}[key]
        embed = make_embed(f"📊  2026 {title} forecast",
                           f"**{fav}** are favoured, with a **{max(d_pct, 100 - d_pct):.0f}%** chance of {'control' if key != 'governors' else 'holding the majority'}.",
                           color=NAVY if d_pct >= 50 else RED)
        embed.add_field(name="Democratic chance", value=f"**{d_pct:.0f}%**", inline=True)
        embed.add_field(name="Projected seats", value=f"D {c['projected_dem_seats']} · R {int(round(float(c['avg_dem_seats']) + float(c['avg_gop_seats']))) - int(c['projected_dem_seats'])}", inline=True)
        embed.add_field(name="Average", value=f"D {float(c['avg_dem_seats']):.1f} · R {float(c['avg_gop_seats']):.1f}", inline=True)
        embed.add_field(name="Majority", value=f"{c['needed']} seats · {c['races']} races up", inline=True)
        view = link_buttons(("Full forecast", f"{SITE_URL}/forecasts/", "📊"))
        if key == "house":
            return await ctx.send(embed=embed, view=view)
        race_type = "senate" if key == "senate" else "governor"
        probs: dict[str, float] = {}
        for r in races.values():
            if r["race_type"] != race_type:
                continue
            p = self._d_side_prob(r)
            st = r["state"]
            if st not in probs or abs(p - 50) < abs(probs[st] - 50):
                probs[st] = p
        shapes = await self.data.state_shapes()
        png = await asyncio.to_thread(electmap.forecast, shapes, probs, f"2026 {title} forecast",
                                      f"Democrats {d_pct:.0f}% to control · shaded by each race's odds")
        embed.set_image(url="attachment://forecast.png")
        await ctx.send(embed=embed, view=view, file=_png(png, "forecast.png"))

    @staticmethod
    def _d_side_prob(r: dict) -> float:
        """Chance the non-Republican side wins — the Democrat, or an independent running instead of one."""
        if r.get("threeway") == "True" and r.get("ind_name") and r.get("dem_name", "").startswith("(No"):
            return float(r.get("ind_prob") or 0)
        return float(r.get("dem_prob") or 0)

    async def race_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            races = await self.data.races()
        except Exception:  # noqa: BLE001
            return []
        cur = current.lower().replace(" ", "")
        starts, contains = [], []
        for code, r in races.items():
            label = f"{code}: {r['dem_name']} vs {r['rep_name']}"
            flat = code.lower().replace("-", "")
            if not cur or flat.startswith(cur.replace("-", "")):
                starts.append(app_commands.Choice(name=label[:100], value=code))
            elif cur in label.lower().replace(" ", ""):
                contains.append(app_commands.Choice(name=label[:100], value=code))
        return (starts + contains)[:25]

    @forecast.command(name="race", description="The forecast for one race, like GA-SEN, PA-GOV or PA-07")
    @app_commands.describe(race="Race code: state-SEN, state-GOV, or state-district")
    async def forecast_race(self, ctx: commands.Context, race: str):
        await ctx.defer()
        races = await self.data.races()
        code = race.strip().upper()
        r = races.get(code) or races.get(code.replace(" ", "-"))
        if r is None:
            return await ctx.send(embed=error_embed(f"No 2026 race called `{race}`. Try a code like GA-SEN, PA-GOV or PA-07."))
        kind = RACE_TYPE_LABEL.get(r["race_type"], r["race_type"].title())
        place = STATE_NAMES.get(r["state"], r["state"])
        title = f"{place} {kind}" if r["race_type"] != "house" else f"{code} (House)"
        m = float(r["margin"] or 0)
        rows = []
        if not r["dem_name"].startswith("(No"):
            rows.append((r["dem_name"], "D", float(r["dem_prob"] or 0)))
        if r.get("threeway") == "True" and r.get("ind_name"):
            rows.append((r["ind_name"], "I", float(r["ind_prob"] or 0)))
        rows.append((r["rep_name"], "R", float(r["rep_prob"] or 0)))
        lead = max(rows, key=lambda x: x[2])
        bar = lambda p: "█" * round(p / 10) + "░" * (10 - round(p / 10))  # noqa: E731
        embed = make_embed(f"📊  {title}", f"**{lead[0]}** ({lead[1]}) is favoured · **{r['rating']}**",
                           color=NAVY if lead[1] != "R" else RED)
        embed.add_field(name="Chance of winning", value="\n".join(f"`{bar(p)}` **{p:.0f}%** {n} ({party})" for n, party, p in rows), inline=False)
        challenger = rows[0][0]  # the Democrat, or the independent standing in for one
        def lead(x: float) -> str:
            return "Even" if abs(x) < 0.05 else f"{challenger if x > 0 else r['rep_name']} +{abs(x):.1f}"
        embed.add_field(name="Projected margin", value=lead(m), inline=True)
        if r.get("poll_count") and r["poll_count"] not in ("0", ""):
            embed.add_field(name="Polling average", value=f"{lead(float(r['polling_avg']))} ({r['poll_count']} polls)", inline=True)
        holder = {"DEM": "Democratic", "REP": "Republican"}.get(r.get("incumbent_party"), r.get("incumbent_party") or "—")
        embed.add_field(name="Held by", value=holder, inline=True)
        view = link_buttons(("Full race forecast", f"{SITE_URL}/forecasts/#{code.replace('-', '').lower()}", "📊"))
        await ctx.send(embed=embed, view=view)

    @forecast_race.autocomplete("race")
    async def _race_ac(self, interaction: discord.Interaction, current: str):
        return await self.race_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(Elections(bot))
