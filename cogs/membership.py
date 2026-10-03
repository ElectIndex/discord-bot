"""The members-only gate: tier roles, joins, and the sync with electindex.com.

The website decides who is a member (GET /wp-json/electindex/v1/discord/members);
this cog makes Discord match. The decisions themselves live in membership_plan.py.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks

from membership_plan import Member, may_join, plan_sync, removals_look_unsafe
from ui import NAVY, RED, SITE_URL, link_buttons, make_embed

log = logging.getLogger("electindex-bot.membership")

API_URL = os.environ.get("EI_API_URL", f"{SITE_URL}/wp-json/electindex/v1/discord/members")
API_SECRET = os.environ.get("EI_API_SECRET", "")
GATE_SINCE = datetime.fromisoformat(os.environ["GATE_SINCE"]) if os.environ.get("GATE_SINCE") else None
STAFF_ROLE_IDS = {int(r) for r in os.environ.get("STAFF_ROLE_IDS", "").split(",") if r.strip()}
SYNC_MINUTES = 2
ADMITTED_FILE = Path(os.environ.get("ADMITTED_FILE", Path(__file__).resolve().parent.parent / "data" / "admitted.json"))
JOIN_URL = f"{SITE_URL}/discord/"

# Highest first: the order they're created in, and how they stack in the member list.
TIER_ROLES = (
    ("founder", "Founder", discord.Color(0xE6B94E)),
    ("patron", "Patron", discord.Color(0xE0241F)),
    ("supporter", "Supporter", discord.Color(0x1B4B8F)),
)


class WebsiteUnavailable(Exception):
    pass


class Membership(commands.Cog):
    """Members-only access, synced with electindex.com."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http: aiohttp.ClientSession | None = None
        self.tier_roles: dict[str, int] = {}
        self.admitted: set[int] = self._load_admitted()
        self.lock = asyncio.Lock()
        self.last_sync: str = "never"
        self.announced = False

    async def cog_load(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        if not (API_SECRET and GATE_SINCE):
            log.warning("EI_API_SECRET / GATE_SINCE not set — membership gate is OFF")
            return
        self.sync_loop.start()

    async def cog_unload(self):
        self.sync_loop.cancel()
        if self.http:
            await self.http.close()

    # ---- state ----------------------------------------------------------

    def _load_admitted(self) -> set[int]:
        try:
            return {int(i) for i in json.loads(ADMITTED_FILE.read_text())}
        except FileNotFoundError:
            return set()

    def _save_admitted(self):
        ADMITTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ADMITTED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(str(i) for i in self.admitted)))
        tmp.replace(ADMITTED_FILE)

    # ---- website --------------------------------------------------------

    async def fetch_linked(self) -> dict[int, str | None]:
        try:
            async with self.http.get(API_URL, headers={"X-EI-Bot-Secret": API_SECRET, "Cache-Control": "no-cache"}) as r:
                if r.status != 200:
                    raise WebsiteUnavailable(f"HTTP {r.status}")
                data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise WebsiteUnavailable(str(e)) from e
        members = data.get("members")
        if not isinstance(members, list):
            raise WebsiteUnavailable("malformed response")
        return {int(m["discord_id"]): m.get("tier") for m in members if str(m.get("discord_id", "")).isdigit()}

    # ---- discord --------------------------------------------------------

    def guild(self) -> discord.Guild | None:
        gid = getattr(self.bot, "guild_id", None)
        return self.bot.get_guild(gid) if gid else (self.bot.guilds[0] if self.bot.guilds else None)

    async def ensure_roles(self, guild: discord.Guild):
        """Find or create the tier roles.

        A tier role is handed to every paying member, so it must never carry a
        permission: if a "Supporter" role were edited (or pre-created) with
        Administrator, the bot would hand that to everyone who pays. Any role that
        grants permissions or belongs to an integration is refused — that tier is
        left unassigned and an error logged until someone fixes the role.
        """
        by_name: dict[str, list[discord.Role]] = {}
        for r in guild.roles:
            by_name.setdefault(r.name, []).append(r)
        for key, name, color in TIER_ROLES:
            candidates = [r for r in by_name.get(name, []) if not r.managed]
            role = candidates[0] if candidates else None
            if role is None:
                role = await guild.create_role(
                    name=name, color=color, hoist=True, mentionable=False,
                    permissions=discord.Permissions.none(),
                    reason="ElectIndex membership tier (managed by the bot)",
                )
                log.info("Created role %s (%s)", name, role.id)
            if len(candidates) > 1:
                log.error("More than one role named %s — using %s; delete the duplicates", name, role.id)
            if role.permissions.value != 0:
                log.error("Refusing to use role %s (%s): it grants permissions (%s). Clear them to re-enable this tier.",
                          name, role.id, role.permissions.value)
                self.tier_roles.pop(key, None)
                continue
            self.tier_roles[key] = role.id

    def to_member(self, m: discord.Member) -> Member:
        exempt = m.guild_permissions.administrator or bool(STAFF_ROLE_IDS & {r.id for r in m.roles})
        return Member(id=m.id, joined_at=m.joined_at, role_ids=frozenset(r.id for r in m.roles), bot=m.bot, exempt=exempt)

    async def remove_member(self, member: discord.Member, reason: str):
        embed = make_embed(
            "The ElectIndex Community is for Members",
            "This server is open to ElectIndex **Supporters, Patrons and Founders**, "
            "and the way in is through your account on electindex.com.\n\n"
            "Become a Member, then connect Discord from your account page and you'll be "
            "added straight back in — with your member role.",
            color=RED,
        )
        try:
            await member.send(embed=embed, view=link_buttons(("Join through electindex.com", JOIN_URL, "🗳️")))
        except discord.HTTPException:
            pass  # DMs closed.
        await member.kick(reason=reason)
        log.info("Removed %s (%s): %s", member, member.id, reason)

    async def apply(self, guild: discord.Guild, plan) -> dict[str, int]:
        done = {"added": 0, "removed": 0, "kicked": 0}
        for member_id, role_id in plan.add.items():
            member, role = guild.get_member(member_id), guild.get_role(role_id)
            if member and role:
                await member.add_roles(role, reason="ElectIndex membership tier")
                done["added"] += 1
        for member_id, role_ids in plan.remove.items():
            member = guild.get_member(member_id)
            roles = [guild.get_role(r) for r in role_ids]
            if member and all(roles):
                await member.remove_roles(*roles, reason="ElectIndex membership no longer covers this tier")
                done["removed"] += 1
        for member_id in plan.kick:
            member = guild.get_member(member_id)
            if member:
                await self.remove_member(member, "Joined without a linked ElectIndex membership")
                done["kicked"] += 1
        if plan.admit - self.admitted:
            self.admitted |= plan.admit
            self._save_admitted()
        return done

    async def sync(self) -> dict[str, int] | str:
        guild = self.guild()
        if guild is None:
            return "not in the server"
        async with self.lock:
            await self.ensure_roles(guild)
            linked = await self.fetch_linked()
            plan = plan_sync([self.to_member(m) for m in guild.members], linked, self.tier_roles, self.admitted, GATE_SINCE)
            holders = sum(1 for m in guild.members if {r.id for r in m.roles} & set(self.tier_roles.values()))
            if removals_look_unsafe(plan, holders):
                log.error("Skipping %d role removals out of %d tier holders — check the website API",
                          plan.removal_count, holders)
                plan.remove.clear()
            done = await self.apply(guild, plan)
            self.last_sync = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            if any(done.values()):
                log.info("Sync: %s", done)
            return done

    @tasks.loop(minutes=SYNC_MINUTES)
    async def sync_loop(self):
        try:
            result = await self.sync()
            if not self.announced and isinstance(result, dict):
                self.announced = True
                log.info("Gate on (grandfathering joins up to %s); first sync: %s", GATE_SINCE.isoformat(), result)
        except WebsiteUnavailable as e:
            log.warning("Sync skipped, website unavailable: %s", e)
        except discord.HTTPException:
            log.exception("Sync failed")

    @sync_loop.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()
        if not self.bot.intents.members:
            log.error("Members intent is off — the gate can't see joins")

    # ---- events ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not (API_SECRET and GATE_SINCE) or member.guild != self.guild():
            return
        try:
            linked = await self.fetch_linked()
        except WebsiteUnavailable as e:
            log.warning("Can't verify %s (%s) on join, will retry in the next sync: %s", member, member.id, e)
            return
        async with self.lock:
            await self.ensure_roles(member.guild)
            tier = linked.get(member.id)
            if member.id in linked and may_join(tier):
                self.admitted.add(member.id)
                self._save_admitted()
                role_id = self.tier_roles.get(tier)
                if role_id:
                    await member.add_roles(member.guild.get_role(role_id), reason="ElectIndex membership tier")
                log.info("Admitted %s (%s) as %s", member, member.id, tier)
            elif not self.to_member(member).exempt:
                await self.remove_member(member, "Joined without a linked ElectIndex membership")

    # ---- staff commands -------------------------------------------------

    @commands.hybrid_command(name="sync", description="Staff: sync member roles with electindex.com now")
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def sync_command(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        try:
            result = await self.sync()
        except WebsiteUnavailable as e:
            await ctx.send(embed=make_embed("🔄  Sync failed", f"Couldn't reach electindex.com: `{e}`", color=RED), ephemeral=True)
            return
        if isinstance(result, str):
            await ctx.send(embed=make_embed("🔄  Sync", result.capitalize(), color=RED), ephemeral=True)
            return
        guild = ctx.guild
        embed = make_embed("🔄  Synced with electindex.com", color=NAVY)
        embed.add_field(name="Roles added", value=f"**{result['added']}**", inline=True)
        embed.add_field(name="Roles removed", value=f"**{result['removed']}**", inline=True)
        embed.add_field(name="Members removed", value=f"**{result['kicked']}**", inline=True)
        for key, name, _ in TIER_ROLES:
            role = guild.get_role(self.tier_roles.get(key, 0))
            embed.add_field(name=name + "s", value=f"**{len(role.members) if role else 0}**", inline=True)
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Membership(bot))
