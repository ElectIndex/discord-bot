"""Moderation commands (replaces YAGPDB's moderation).

Every action requires a reason, gets a numbered case, DMs the member, and is
written to #server-logs. Commands are staff-only: hidden from /help, and hidden
from the slash menu for anyone without the matching permission.

Who counts as staff: administrators, anyone holding the action's permission, or a
role in STAFF_ROLE_IDS. (Slash menus follow Discord's own permissions; a staff
role without the permission can still use the `!` form, or be granted the slash
commands under Server Settings → Integrations → ElectIndex Bot.)
"""

import logging
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import LOG_CHANNEL_ID, STAFF_ROLE_IDS
from moderation_core import MAX_TIMEOUT, CaseStore, hierarchy_problem, human_duration, parse_duration
from ui import NAVY, RED, error_embed, make_embed

log = logging.getLogger("electindex-bot.moderation")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "moderation.db"
ACTION_STYLE = {
    "warn": ("⚠️", "Warning", NAVY),
    "timeout": ("🔇", "Timeout", NAVY),
    "untimeout": ("🔊", "Timeout removed", NAVY),
    "kick": ("👢", "Kick", RED),
    "ban": ("🔨", "Ban", RED),
    "unban": ("🕊️", "Unban", NAVY),
    "unwarn": ("🧽", "Warning removed", NAVY),
}


def staff(permission: str):
    """Allowed: administrators, holders of `permission`, or a staff role."""
    def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        perms = ctx.author.guild_permissions
        if perms.administrator or getattr(perms, permission) or STAFF_ROLE_IDS & {r.id for r in ctx.author.roles}:
            return True
        raise commands.MissingPermissions([permission])

    def decorator(func):
        func = app_commands.default_permissions(**{permission: True})(func)
        return commands.check(predicate)(func)

    return decorator


class Moderation(commands.Cog):
    """Staff moderation tools."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cases = CaseStore(DB_PATH)

    # ---- helpers --------------------------------------------------------

    def _blocked(self, ctx: commands.Context, member: discord.Member) -> str | None:
        guild = ctx.guild
        return hierarchy_problem(
            ctx.author.id, ctx.author.top_role.position, member.id, member.top_role.position,
            guild.me.id, guild.me.top_role.position, guild.owner_id,
        )

    async def _dm(self, user: discord.abc.User, guild: discord.Guild, action: str, reason: str, duration: timedelta | None = None):
        icon, label, color = ACTION_STYLE[action]
        embed = make_embed(f"{icon}  {label} in {guild.name}", color=color)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if duration:
            embed.add_field(name="Duration", value=human_duration(duration), inline=True)
        try:
            await user.send(embed=embed)
            return True
        except discord.HTTPException:
            return False

    async def _record(self, ctx: commands.Context, action: str, user: discord.abc.User, reason: str,
                      duration: timedelta | None = None, dmed: bool | None = None):
        case = self.cases.add(action, user.id, str(user), ctx.author.id, reason, duration)
        icon, label, color = ACTION_STYLE[action]
        embed = make_embed(f"{icon}  Case #{case.id} · {label}", color=color)
        embed.add_field(name="Member", value=f"{user.mention}\n`{user}` · `{user.id}`", inline=True)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        if duration:
            embed.add_field(name="Duration", value=human_duration(duration), inline=True)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if dmed is False:
            embed.set_footer(text="Couldn't DM the member (DMs closed)")
        log_ch = ctx.guild.get_channel(LOG_CHANNEL_ID)
        if isinstance(log_ch, discord.TextChannel):
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        confirm = make_embed(f"{icon}  Case #{case.id} · {label}", f"{user.mention} — {reason}", color=color)
        await ctx.send(embed=confirm, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return case

    async def _refuse(self, ctx: commands.Context, why: str):
        await ctx.send(embed=error_embed(why), ephemeral=True)

    # ---- commands -------------------------------------------------------

    @commands.hybrid_command(name="warn", description="Staff: warn a member", hidden=True)
    @staff("moderate_members")
    @commands.guild_only()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if why := self._blocked(ctx, member):
            return await self._refuse(ctx, why)
        dmed = await self._dm(member, ctx.guild, "warn", reason)
        await self._record(ctx, "warn", member, reason, dmed=dmed)

    @commands.hybrid_command(name="warnings", description="Staff: list a member's warnings and cases", hidden=True)
    @staff("moderate_members")
    @commands.guild_only()
    async def warnings(self, ctx: commands.Context, user: discord.User):
        cases = self.cases.for_user(user.id)
        active = [c for c in cases if c.action == "warn" and c.active]
        embed = make_embed(f"📋  {user}", f"**{len(active)}** active warning(s) · **{len(cases)}** case(s) total")
        for c in cases[:15]:
            icon, label, _ = ACTION_STYLE.get(c.action, ("•", c.action, NAVY))
            struck = "" if c.active or c.action != "warn" else " (removed)"
            extra = f" · {human_duration(timedelta(seconds=c.duration_seconds))}" if c.duration_seconds else ""
            embed.add_field(
                name=f"#{c.id} · {icon} {label}{struck}{extra}",
                value=f"{c.reason[:200]}\n<t:{c.created_at}:R> by <@{c.moderator_id}>",
                inline=False,
            )
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="unwarn", description="Staff: remove a warning by case number", hidden=True)
    @staff("moderate_members")
    @commands.guild_only()
    async def unwarn(self, ctx: commands.Context, case_id: int, *, reason: str):
        case = self.cases.get(case_id)
        if not case or case.action != "warn":
            return await self._refuse(ctx, f"Case #{case_id} isn't a warning.")
        if not self.cases.deactivate(case_id):
            return await self._refuse(ctx, f"Warning #{case_id} was already removed.")
        user = await self.bot.fetch_user(case.user_id)
        await self._record(ctx, "unwarn", user, f"Removed warning #{case_id}: {reason}")

    @commands.hybrid_command(name="case", description="Staff: show one moderation case", hidden=True)
    @staff("moderate_members")
    @commands.guild_only()
    async def case(self, ctx: commands.Context, case_id: int):
        c = self.cases.get(case_id)
        if not c:
            return await self._refuse(ctx, f"There's no case #{case_id}.")
        icon, label, color = ACTION_STYLE.get(c.action, ("•", c.action, NAVY))
        embed = make_embed(f"{icon}  Case #{c.id} · {label}", color=color)
        embed.add_field(name="Member", value=f"<@{c.user_id}>\n`{c.user_name}`", inline=True)
        embed.add_field(name="Moderator", value=f"<@{c.moderator_id}>", inline=True)
        embed.add_field(name="When", value=f"<t:{c.created_at}:f>", inline=True)
        if c.duration_seconds:
            embed.add_field(name="Duration", value=human_duration(timedelta(seconds=c.duration_seconds)), inline=True)
        embed.add_field(name="Reason", value=c.reason[:1024], inline=False)
        await ctx.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="timeout", description="Staff: time a member out (e.g. 10m, 2h, 1d)", hidden=True)
    @staff("moderate_members")
    @commands.guild_only()
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str):
        if why := self._blocked(ctx, member):
            return await self._refuse(ctx, why)
        try:
            length = parse_duration(duration)
        except ValueError as e:
            return await self._refuse(ctx, str(e))
        if length > MAX_TIMEOUT:
            return await self._refuse(ctx, "Discord caps timeouts at 28 days.")
        await member.timeout(length, reason=f"{ctx.author}: {reason}")
        dmed = await self._dm(member, ctx.guild, "timeout", reason, length)
        await self._record(ctx, "timeout", member, reason, length, dmed=dmed)

    @commands.hybrid_command(name="untimeout", description="Staff: lift a member's timeout", hidden=True)
    @staff("moderate_members")
    @commands.guild_only()
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if not member.is_timed_out():
            return await self._refuse(ctx, f"{member.mention} isn't timed out.")
        await member.timeout(None, reason=f"{ctx.author}: {reason}")
        await self._record(ctx, "untimeout", member, reason)

    @commands.hybrid_command(name="kick", description="Staff: kick a member", hidden=True)
    @staff("kick_members")
    @commands.guild_only()
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if why := self._blocked(ctx, member):
            return await self._refuse(ctx, why)
        dmed = await self._dm(member, ctx.guild, "kick", reason)
        await member.kick(reason=f"{ctx.author}: {reason}")
        await self._record(ctx, "kick", member, reason, dmed=dmed)

    @commands.hybrid_command(name="ban", description="Staff: ban a member or user id", hidden=True)
    @app_commands.describe(delete_days="Delete their messages from the last 0–7 days")
    @staff("ban_members")
    @commands.guild_only()
    async def ban(self, ctx: commands.Context, user: discord.User, delete_days: commands.Range[int, 0, 7] | None = None, *, reason: str):
        # Optional so `!ban @user spamming` reads "spamming" as the reason, not a bad day count.
        delete_days = delete_days or 0
        member = ctx.guild.get_member(user.id)
        if member and (why := self._blocked(ctx, member)):
            return await self._refuse(ctx, why)
        dmed = await self._dm(user, ctx.guild, "ban", reason) if member else None
        await ctx.guild.ban(user, reason=f"{ctx.author}: {reason}", delete_message_seconds=delete_days * 86400)
        await self._record(ctx, "ban", user, reason, dmed=dmed)

    @commands.hybrid_command(name="unban", description="Staff: unban a user by id", hidden=True)
    @staff("ban_members")
    @commands.guild_only()
    async def unban(self, ctx: commands.Context, user: discord.User, *, reason: str):
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        except discord.NotFound:
            return await self._refuse(ctx, f"{user} isn't banned.")
        await self._record(ctx, "unban", user, reason)

    @commands.hybrid_command(name="purge", description="Staff: delete recent messages in this channel", hidden=True)
    @app_commands.describe(amount="How many messages to scan (1–100)", member="Only delete this member's messages")
    @staff("manage_messages")
    @commands.guild_only()
    async def purge(self, ctx: commands.Context, amount: commands.Range[int, 1, 100], member: discord.Member | None = None):
        await ctx.defer(ephemeral=True)
        if ctx.interaction is None:
            await ctx.message.delete()
        check = (lambda m: m.author.id == member.id) if member else (lambda m: True)
        deleted = await ctx.channel.purge(limit=amount, check=check, reason=f"Purge by {ctx.author}")
        who = f" from {member.mention}" if member else ""
        log_ch = ctx.guild.get_channel(LOG_CHANNEL_ID)
        if isinstance(log_ch, discord.TextChannel):
            await log_ch.send(
                embed=make_embed("🧹  Messages purged", f"**{len(deleted)}** message(s){who} in {ctx.channel.mention} by {ctx.author.mention}", color=RED),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await ctx.send(embed=make_embed("🧹  Purged", f"Deleted **{len(deleted)}** message(s){who}."), ephemeral=True, delete_after=None if ctx.interaction else 5)

    @commands.hybrid_command(name="slowmode", description="Staff: set this channel's slowmode (0 to turn off)", hidden=True)
    @app_commands.describe(seconds="Seconds between messages, 0–21600")
    @staff("manage_channels")
    @commands.guild_only()
    async def slowmode(self, ctx: commands.Context, seconds: commands.Range[int, 0, 21600]):
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {ctx.author}")
        text = "Slowmode is off." if seconds == 0 else f"Slowmode set to **{seconds}s**."
        await ctx.send(embed=make_embed("🐢  Slowmode", text), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
