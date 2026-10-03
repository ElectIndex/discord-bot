"""ElectIndex community Discord bot.

Runs only on the ElectIndex server. If GUILD_ID is set, the bot leaves any
other server it gets added to.
"""

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None

log = logging.getLogger("electindex-bot")


class ElectIndexBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())

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
