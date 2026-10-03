"""ElectIndex community Discord bot.

Runs only on the ElectIndex server. If GUILD_ID is set, the bot leaves any
other server it gets added to.
"""

import logging
import os
import random

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from ui import error_embed

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None

log = logging.getLogger("electindex-bot")

PREFIX = "!"

STATUS_INTERVAL_MINUTES = 15
STATUSES = [
    (discord.ActivityType.watching, "the early vote come in"),
    (discord.ActivityType.watching, "the Senate forecast"),
    (discord.ActivityType.watching, "the House forecast"),
    (discord.ActivityType.watching, "the swing states"),
    (discord.ActivityType.watching, "the polling averages"),
    (discord.ActivityType.watching, "precinct returns"),
    (discord.ActivityType.watching, "the needle"),
    (discord.ActivityType.watching, "turnout in Maricopa"),
    (discord.ActivityType.playing, "the Election Night Simulator"),
    (discord.ActivityType.playing, "with the Shuffler"),
    (discord.ActivityType.playing, "redistricting"),
    (discord.ActivityType.playing, "10,000 simulations"),
    (discord.ActivityType.listening, "concession speeches"),
    (discord.ActivityType.listening, "exit poll chatter"),
    (discord.ActivityType.listening, "the AP race calls"),
    (discord.ActivityType.competing, "the midterms"),
]


class ElectIndexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for ! prefix commands
        intents.members = True  # needed for the members-only gate
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            intents=intents,
            help_command=None,
        )
        self._last_status = None

    async def setup_hook(self):
        self.guild_id = GUILD_ID
        await self.load_extension("cogs.general")
        await self.load_extension("cogs.membership")
        await self.load_extension("cogs.roles")
        await self.load_extension("cogs.welcome")
        await self.load_extension("cogs.moderation")
        await self.load_extension("cogs.serverlog")
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
        self.rotate_status.start()

    @tasks.loop(minutes=STATUS_INTERVAL_MINUTES)
    async def rotate_status(self):
        choices = [s for s in STATUSES if s != self._last_status] or STATUSES
        self._last_status = kind, name = random.choice(choices)
        await self.change_presence(activity=discord.Activity(type=kind, name=name))

    @rotate_status.before_loop
    async def _wait_until_ready(self):
        await self.wait_until_ready()

    async def on_ready(self):
        log.info("Logged in as %s (id %s) in %d guild(s)", self.user, self.user.id, len(self.guilds))
        if GUILD_ID:
            for guild in self.guilds:
                await self._enforce_guild(guild)

    async def on_guild_join(self, guild: discord.Guild):
        await self._enforce_guild(guild)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)) and not isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=error_embed("You don't have permission to use that command."), ephemeral=True)
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=error_embed("That command only works in the server."))
            return
        if isinstance(error, commands.MissingRequiredArgument):
            usage = f"`!{ctx.command.qualified_name} {ctx.command.signature}`"
            what = "A reason is required." if error.param.name == "reason" else f"Missing `{error.param.name}`."
            await ctx.send(embed=error_embed(f"{what}\nUsage: {usage}"), ephemeral=True)
            return
        if isinstance(error, (commands.BadArgument, commands.RangeError)):
            await ctx.send(embed=error_embed(str(error)), ephemeral=True)
            return
        original = getattr(error, "original", None)
        if isinstance(original, discord.Forbidden):
            await ctx.send(embed=error_embed("Discord didn't let me do that — check my role and permissions."), ephemeral=True)
            return
        log.error("Command %s failed", ctx.command, exc_info=error)
        await ctx.send(embed=error_embed("Something went wrong running that command."))

    async def _enforce_guild(self, guild: discord.Guild):
        if GUILD_ID and guild.id != GUILD_ID:
            log.warning("Leaving unauthorized guild %s (%s)", guild.name, guild.id)
            await guild.leave()


if __name__ == "__main__":
    ElectIndexBot().run(TOKEN, root_logger=True)
