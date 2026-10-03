"""Welcome and goodbye messages in #welcome-users (replaces Welcomer).

Every human who joins is welcomed, and every departure announced. The welcome
waits a few seconds for the members-only gate so the card can show the member's
tier; if the gate hasn't decided by then (or turns them away), it goes out
without one.
"""

import asyncio
import io
import logging

import discord
from discord.ext import commands

import welcome_card
from config import WELCOME_CHANNEL_ID
from ui import NAVY, make_embed

log = logging.getLogger("electindex-bot.welcome")


class Welcome(commands.Cog):
    """Welcome cards and goodbye messages."""

    TIER_WAIT_SECONDS = 6

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tiers: dict[int, str | None] = {}  # admissions seen, member id -> tier
        self.waiting: dict[int, asyncio.Event] = {}

    def channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        ch = guild.get_channel(WELCOME_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def card(self, member: discord.Member, tier: str | None) -> discord.File:
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except discord.HTTPException:
            avatar = None
        png = await asyncio.to_thread(welcome_card.render, avatar, member.display_name, member.name, member.guild.member_count or 0, tier)
        return discord.File(fp=io.BytesIO(png), filename=f"welcome-{member.id}.png")

    @commands.Cog.listener()
    async def on_member_admitted(self, member: discord.Member, tier: str | None):
        self.tiers[member.id] = tier
        if event := self.waiting.get(member.id):
            event.set()
        else:
            # Admitted by a later sync, after the welcome already went out: don't keep it forever.
            asyncio.get_running_loop().call_later(30, self.tiers.pop, member.id, None)

    async def _tier_for(self, member: discord.Member) -> str | None:
        if member.id not in self.tiers:
            event = self.waiting.setdefault(member.id, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), self.TIER_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
            finally:
                self.waiting.pop(member.id, None)
        return self.tiers.pop(member.id, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        ch = self.channel(member.guild)
        if ch is None:
            return
        tier = await self._tier_for(member)
        n = welcome_card.ordinal(member.guild.member_count or 0)
        embed = discord.Embed(color=NAVY)
        file = await self.card(member, tier)
        embed.set_image(url=f"attachment://{file.filename}")
        await ch.send(
            content=f"Welcome {member.mention} to **ElectIndex Community**! You are the {n} member!",
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        if payload.user.bot:
            return
        guild = self.bot.get_guild(payload.guild_id)
        ch = guild and self.channel(guild)
        if ch:
            await ch.send(
                f"**{discord.utils.escape_markdown(payload.user.display_name)}** has left the server. "
                f"We now have {guild.member_count} members.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.hybrid_command(name="welcomepreview", description="Staff: preview the welcome card", hidden=True)
    @discord.app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def welcome_preview(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        file = await self.card(ctx.author, None)
        embed = make_embed("👋  Welcome card preview")
        embed.set_image(url=f"attachment://{file.filename}")
        await ctx.send(embed=embed, file=file, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
