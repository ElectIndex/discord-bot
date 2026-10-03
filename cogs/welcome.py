"""Welcome and goodbye messages in #welcome-users (replaces Welcomer).

Welcomes fire on `member_admitted`, not on join, so someone the members-only
gate turns away is never welcomed — and their departure isn't announced either.
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

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rejected: set[int] = set()

    def channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        ch = guild.get_channel(WELCOME_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def card(self, member: discord.Member, tier: str | None) -> discord.File:
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except discord.HTTPException:
            avatar = None
        name = welcome_card.printable_name(member.display_name, member.name)
        png = await asyncio.to_thread(welcome_card.render, avatar, name, member.guild.member_count or 0, tier)
        return discord.File(fp=io.BytesIO(png), filename=f"welcome-{member.id}.png")

    @commands.Cog.listener()
    async def on_member_admitted(self, member: discord.Member, tier: str | None):
        ch = self.channel(member.guild)
        if ch is None:
            return
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
    async def on_gate_rejected(self, member: discord.Member):
        self.rejected.add(member.id)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        if payload.user.bot or payload.user.id in self.rejected:
            self.rejected.discard(payload.user.id)
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
