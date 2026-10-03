"""Starboard: a message with 3 or more ⭐ reactions is reposted to #starboard
(replaces Carl-bot's starboard). The count stays live; the board post comes
down if stars drop below the threshold or the original is deleted.

Only messages from channels everyone can read are eligible, so starring a
message in a staff channel can't publish it to the whole server.
"""

import asyncio
import logging
from collections import defaultdict
from pathlib import Path

import discord
from discord.ext import commands

from config import STARBOARD_CHANNEL_ID
from starboard_core import STAR, StarStore, board_action, star_count

log = logging.getLogger("electindex-bot.starboard")

GOLD = discord.Color(0xE6B94E)
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "starboard.db"
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _is_star(emoji: discord.PartialEmoji | str) -> bool:
    return str(emoji).replace("️", "") == STAR


class Starboard(commands.Cog):
    """⭐ 3 or more → #starboard."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = StarStore(DB_PATH)
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def board(self, guild: discord.Guild) -> discord.TextChannel | None:
        ch = guild.get_channel(STARBOARD_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    @staticmethod
    def eligible(channel) -> bool:
        if channel is None or channel.id == STARBOARD_CHANNEL_ID:
            return False
        if getattr(channel, "is_nsfw", lambda: False)():
            return False
        everyone = channel.guild.default_role
        return channel.permissions_for(everyone).view_channel

    def render(self, message: discord.Message, count: int) -> tuple[str, discord.Embed, discord.ui.View]:
        content = f"⭐ **{count}** · {message.channel.mention}"
        embed = discord.Embed(description=message.content[:4000] or None, color=GOLD, timestamp=message.created_at)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        image = next((a.url for a in message.attachments if (a.content_type or "") in IMAGE_TYPES), None)
        if image is None:
            image = next((e.image.url or e.thumbnail.url for e in message.embeds if e.image or e.thumbnail), None)
        if image:
            embed.set_image(url=image)
        others = [a.filename for a in message.attachments if (a.content_type or "") not in IMAGE_TYPES]
        if others:
            embed.add_field(name="Attachments", value="\n".join(others)[:1024], inline=False)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Jump to message", url=message.jump_url))
        return content, embed, view

    async def refresh(self, channel_id: int, message_id: int):
        channel = self.bot.get_channel(channel_id)
        if not self.eligible(channel) or self.store.is_board_message(message_id):
            return
        board = self.board(channel.guild)
        if board is None:
            return
        async with self.locks[message_id]:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await self.remove(board, message_id)
                return
            reaction = next((r for r in message.reactions if _is_star(r.emoji)), None)
            reactors = [u async for u in reaction.users()] if reaction else []
            count = star_count([u.id for u in reactors], message.author.id, {u.id for u in reactors if u.bot})
            board_id = self.store.get(message_id)
            action = board_action(count, board_id is not None)
            if action == "post":
                content, embed, view = self.render(message, count)
                posted = await board.send(content=content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                self.store.put(message_id, posted.id)
            elif action == "update":
                content, embed, view = self.render(message, count)
                try:
                    await board.get_partial_message(board_id).edit(content=content, embed=embed, view=view)
                except discord.NotFound:  # someone deleted the board post; repost it
                    posted = await board.send(content=content, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                    self.store.put(message_id, posted.id)
            elif action == "remove":
                await self.remove(board, message_id)

    async def remove(self, board: discord.TextChannel, message_id: int):
        board_id = self.store.get(message_id)
        if board_id is None:
            return
        self.store.drop(message_id)
        try:
            await board.get_partial_message(board_id).delete()
        except discord.NotFound:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id and _is_star(payload.emoji):
            await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id and _is_star(payload.emoji):
            await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent):
        if _is_star(payload.emoji):
            await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id and self.store.get(payload.message_id) is not None:
            guild = self.bot.get_guild(payload.guild_id)
            board = guild and self.board(guild)
            if board:
                await self.remove(board, payload.message_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Starboard(bot))
