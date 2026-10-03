"""General commands: help, about, ping, stats. Each works as /name and !name."""

import platform
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from ui import NAVY, RED, REPO_URL, SITE_URL, Paginator, link_buttons, make_embed

HELP_PAGE_SIZE = 5
SLOW_PING_MS = 250


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


def _usage(command: commands.Command) -> str:
    return f"{command.name} {command.signature}".strip()


class General(commands.Cog):
    """The basics: help, info and diagnostics."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started_at = datetime.now(timezone.utc)
        self.commit = _git_commit()

    def _help_pages(self) -> list[discord.Embed]:
        categories: dict[str, list[commands.Command]] = {}
        for command in self.bot.commands:
            if not command.hidden:
                categories.setdefault(command.cog_name or "Other", []).append(command)

        overview = make_embed(
            "📖  ElectIndex Bot help",
            "Every command works two ways:\n"
            "> **Slash** — type `/` and pick from the menu\n"
            "> **Prefix** — type `!` followed by the command, like `!ping`\n\n"
            "Use the buttons below to flip through the commands.",
        )
        for name, cmds in sorted(categories.items()):
            cog = self.bot.get_cog(name)
            listing = " ".join(f"`{c.name}`" for c in sorted(cmds, key=lambda c: c.name))
            overview.add_field(name=f"{name} ({len(cmds)})", value=f"{cog.description if cog else ''}\n{listing}".strip(), inline=False)
        if self.bot.user:
            overview.set_thumbnail(url=self.bot.user.display_avatar.url)

        pages = [overview]
        for name, cmds in sorted(categories.items()):
            cmds = sorted(cmds, key=lambda c: c.name)
            chunks = [cmds[i:i + HELP_PAGE_SIZE] for i in range(0, len(cmds), HELP_PAGE_SIZE)]
            for n, chunk in enumerate(chunks, start=1):
                suffix = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
                page = make_embed(f"📂  {name}{suffix}")
                for command in chunk:
                    page.add_field(
                        name=f"/{command.name}",
                        value=f"{command.description or '—'}\n`/{_usage(command)}`  ·  `!{_usage(command)}`",
                        inline=False,
                    )
                pages.append(page)
        return pages

    @commands.hybrid_command(name="help", description="List the bot's commands")
    async def help(self, ctx: commands.Context):
        await Paginator(self._help_pages(), ctx.author).send(ctx)

    @commands.hybrid_command(name="staffhelp", description="Staff: list the staff-only commands", hidden=True)
    @discord.app_commands.default_permissions(moderate_members=True)
    @commands.guild_only()
    async def staffhelp(self, ctx: commands.Context):
        perms = ctx.author.guild_permissions
        from config import STAFF_ROLE_IDS
        if not (perms.administrator or perms.moderate_members or perms.manage_messages
                or STAFF_ROLE_IDS & {r.id for r in ctx.author.roles}):
            raise commands.MissingPermissions(["moderate_members"])
        staff_cmds = sorted((c for c in self.bot.commands if c.hidden), key=lambda c: (c.cog_name or "", c.name))
        pages = []
        for i in range(0, len(staff_cmds), HELP_PAGE_SIZE + 3):
            page = make_embed("🛡️  Staff commands", "Visible only to staff. Every moderation action needs a reason and is logged in #server-logs." if i == 0 else None)
            for command in staff_cmds[i:i + HELP_PAGE_SIZE + 3]:
                page.add_field(name=f"/{command.name}", value=f"{command.description.removeprefix('Staff: ')}\n`!{_usage(command)}`", inline=False)
            pages.append(page)
        view = Paginator(pages, ctx.author)
        if len(pages) == 1:
            await ctx.send(embed=pages[0], ephemeral=True)
        else:
            view.message = await ctx.send(embed=pages[0], view=view, ephemeral=True)

    @commands.hybrid_command(name="about", description="About ElectIndex and this bot")
    async def about(self, ctx: commands.Context):
        embed = make_embed(
            "🗳️  About ElectIndex",
            "**ElectIndex** is an independent home for election forecasts, interactive maps, "
            "the Election Night Simulator and early vote tracking.\n\n"
            "This bot runs the ElectIndex community server. It's open source, so suggestions "
            "and pull requests are welcome.",
        )
        embed.add_field(name="Forecasts", value="Senate, House, governors and state legislatures", inline=True)
        embed.add_field(name="Tools", value="Simulator, map atlas, early vote tracker", inline=True)
        embed.add_field(name="Bot version", value=f"`{self.commit}` · discord.py {discord.__version__}", inline=False)
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        view = link_buttons(
            ("Website", SITE_URL, "🌐"),
            ("Forecasts", f"{SITE_URL}/forecast/", "📊"),
            ("Early vote", f"{SITE_URL}/early-vote/", "🗳️"),
            ("Source code", REPO_URL, "💻"),
        )
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="ping", description="Check the bot's latency")
    async def ping(self, ctx: commands.Context):
        start = time.perf_counter()
        message = await ctx.send(embed=make_embed("🏓  Pinging…"))
        roundtrip_ms = (time.perf_counter() - start) * 1000
        gateway_ms = self.bot.latency * 1000

        slow = max(gateway_ms, roundtrip_ms) > SLOW_PING_MS
        embed = make_embed(
            "🏓  Pong!",
            "Running a little slow right now." if slow else "All systems normal.",
            color=RED if slow else NAVY,
        )
        embed.add_field(name="Gateway", value=f"```{gateway_ms:.0f} ms```", inline=True)
        embed.add_field(name="Round trip", value=f"```{roundtrip_ms:.0f} ms```", inline=True)
        embed.add_field(name="Uptime", value=f"```{_format_duration(self._uptime())}```", inline=True)
        await message.edit(embed=embed)

    def _uptime(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    @commands.hybrid_command(name="stats", description="Bot and server statistics")
    @commands.guild_only()
    async def stats(self, ctx: commands.Context):
        guild = ctx.guild
        embed = make_embed(f"📈  {guild.name} stats")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="👥  Members", value=f"**{guild.member_count:,}**", inline=True)
        embed.add_field(
            name="💬  Channels",
            value=f"**{len(guild.text_channels)}** text\n**{len(guild.voice_channels)}** voice",
            inline=True,
        )
        embed.add_field(name="🏷️  Roles", value=f"**{len(guild.roles) - 1}**", inline=True)
        embed.add_field(name="🚀  Boosts", value=f"**{guild.premium_subscription_count}** · level {guild.premium_tier}", inline=True)
        embed.add_field(name="📅  Created", value=discord.utils.format_dt(guild.created_at, "D"), inline=True)
        embed.add_field(name="​", value="​", inline=True)

        embed.add_field(name="​", value="**ElectIndex Bot**", inline=False)
        embed.add_field(name="⏱️  Uptime", value=f"**{_format_duration(self._uptime())}**", inline=True)
        embed.add_field(name="📡  Latency", value=f"**{self.bot.latency * 1000:.0f} ms**", inline=True)
        embed.add_field(name="🧠  Memory", value=f"**{_memory_mb():.0f} MB**", inline=True)
        embed.add_field(name="⌨️  Commands", value=f"**{len(self.bot.commands)}**", inline=True)
        embed.add_field(name="🐍  Runtime", value=f"Python {platform.python_version()}\ndiscord.py {discord.__version__}", inline=True)
        embed.add_field(name="🔖  Version", value=f"[`{self.commit}`]({REPO_URL}/commit/{self.commit})", inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
