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

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None

log = logging.getLogger("electindex-bot")

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
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self._last_status = None

    async def setup_hook(self):
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

    async def _enforce_guild(self, guild: discord.Guild):
        if GUILD_ID and guild.id != GUILD_ID:
            log.warning("Leaving unauthorized guild %s (%s)", guild.name, guild.id)
            await guild.leave()


if __name__ == "__main__":
    ElectIndexBot().run(TOKEN, root_logger=True)
