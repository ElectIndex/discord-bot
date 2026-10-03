"""Automatic and self-assigned roles (replaces the YAGPDB / Welcomer autoroles
and YAGPDB's reaction-role menu).

* Everyone admitted gets the Member role.
* Reacting on the role menu in #info-and-about toggles the matching role. It's
  the menu YAGPDB posted, kept in place so existing reactions still count.
"""

import logging

import discord
from discord.ext import commands

from config import MEMBER_ROLE_ID, REACTION_ROLES

log = logging.getLogger("electindex-bot.roles")


def _emoji_key(emoji: discord.PartialEmoji) -> str:
    # Unicode emoji compare by text; custom ones by id.
    return str(emoji.id) if emoji.id else emoji.name


def _normalise(text: str) -> str:
    return text.replace("️", "")  # 🗳️ arrives with or without the variation selector


class Roles(commands.Cog):
    """Member role on join, and reaction roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_admitted(self, member: discord.Member, tier: str | None):
        role = member.guild.get_role(MEMBER_ROLE_ID)
        if role and role not in member.roles:
            await member.add_roles(role, reason="Autorole: new member")

    def _role_for(self, payload: discord.RawReactionActionEvent) -> discord.Role | None:
        menu = REACTION_ROLES.get(payload.message_id)
        if not menu or payload.guild_id is None:
            return None
        key = _normalise(_emoji_key(payload.emoji))
        role_id = next((rid for emoji, rid in menu.items() if _normalise(emoji) == key), None)
        guild = self.bot.get_guild(payload.guild_id)
        return guild.get_role(role_id) if guild and role_id else None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        role = self._role_for(payload)
        member = payload.member
        if role and member and not member.bot and role not in member.roles:
            await member.add_roles(role, reason="Reaction role")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        role = self._role_for(payload)
        if role is None:
            return
        member = role.guild.get_member(payload.user_id)
        if member and not member.bot and role in member.roles:
            await member.remove_roles(role, reason="Reaction role removed")


async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
