"""General commands: help, about, ping, stats. Each works as /name and !name."""

import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

BRAND_COLOR = discord.Color(0xCAA03A)
SITE_URL = "https://electindex.com"
REPO_URL = "https://github.com/ElectIndex/discord-bot"


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _memory_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KB, macOS reports bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [f"{days}d"] if days else []
    parts += [f"{hours}h"] if days or hours else []
    parts += [f"{minutes}m", f"{seconds}s"]
    return " ".join(parts)


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started_at = datetime.now(timezone.utc)
        self.commit = _git_commit()

    @commands.hybrid_command(name="help", description="List the bot's commands")
    async def help(self, ctx: commands.Context):
        embed = discord.Embed(title="ElectIndex Bot commands", color=BRAND_COLOR)
        for command in sorted(self.bot.commands, key=lambda c: c.name):
            if not command.hidden:
                embed.add_field(name=f"/{command.name}  ·  !{command.name}", value=command.description or "—", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="about", description="About ElectIndex and this bot")
    async def about(self, ctx: commands.Context):
        embed = discord.Embed(
            title="ElectIndex Bot",
            description=(
                "The bot for the ElectIndex community server.\n"
                "ElectIndex is home to election forecasts, the Election Night Simulator, "
                "early vote tracking and more."
            ),
            url=SITE_URL,
            color=BRAND_COLOR,
        )
        embed.add_field(name="Website", value=SITE_URL)
        embed.add_field(name="Source code", value=REPO_URL)
        embed.set_footer(text=f"Version {self.commit} · discord.py {discord.__version__}")
        if self.bot.user.display_avatar:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="ping", description="Check the bot's latency")
    async def ping(self, ctx: commands.Context):
        start = time.perf_counter()
        message = await ctx.send("Pinging…")
        roundtrip_ms = (time.perf_counter() - start) * 1000
        await message.edit(
            content=f"Pong! Gateway **{self.bot.latency * 1000:.0f} ms** · round trip **{roundtrip_ms:.0f} ms**"
        )

    @commands.hybrid_command(name="stats", description="Bot and server statistics")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context):
        guild = ctx.guild
        uptime = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        embed = discord.Embed(title="Stats", color=BRAND_COLOR)
        embed.add_field(
            name="Server",
            value=(
                f"Members: **{guild.member_count:,}**\n"
                f"Channels: **{len(guild.text_channels)}** text · **{len(guild.voice_channels)}** voice\n"
                f"Roles: **{len(guild.roles) - 1}**\n"
                f"Boosts: **{guild.premium_subscription_count}** (level {guild.premium_tier})\n"
                f"Created: {discord.utils.format_dt(guild.created_at, 'D')}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Bot",
            value=(
                f"Uptime: **{_format_duration(uptime)}**\n"
                f"Latency: **{self.bot.latency * 1000:.0f} ms**\n"
                f"Memory: **{_memory_mb():.0f} MB**\n"
                f"Commands: **{len(self.bot.commands)}**\n"
                f"Python {platform.python_version()} · discord.py {discord.__version__} · {self.commit}"
            ),
            inline=False,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
