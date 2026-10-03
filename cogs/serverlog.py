"""Server activity log in #server-logs (replaces Carl-bot's logging)."""

import logging
from datetime import timedelta

import discord
from discord.ext import commands

from config import LOG_CHANNEL_ID
from ui import NAVY, RED, make_embed

log = logging.getLogger("electindex-bot.serverlog")

GREEN = discord.Color(0x16A34A)
NEW_ACCOUNT = timedelta(days=7)


def _clip(text: str | None, limit: int = 1000) -> str:
    text = text or "*(no text)*"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ServerLog(commands.Cog):
    """Message, member, role and channel activity, written to #server-logs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def post(self, guild: discord.Guild | None, embed: discord.Embed):
        ch = guild and guild.get_channel(LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _ignored(self, message: discord.Message) -> bool:
        return message.guild is None or message.author.bot or message.channel.id == LOG_CHANNEL_ID

    @staticmethod
    def _who(embed: discord.Embed, user: discord.abc.User) -> discord.Embed:
        embed.set_author(name=f"{user} · {user.id}", icon_url=user.display_avatar.url)
        return embed

    # ---- messages -------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if self._ignored(message):
            return
        e = make_embed("🗑️  Message deleted", f"In {message.channel.mention}", color=RED)
        e.add_field(name="Content", value=_clip(message.content), inline=False)
        if message.attachments:
            e.add_field(name="Attachments", value=_clip("\n".join(a.filename for a in message.attachments), 500), inline=False)
        await self.post(message.guild, self._who(e, message.author))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # The log is the moderation record: removing an entry from it is itself logged.
        if payload.channel_id == LOG_CHANNEL_ID and payload.guild_id:
            await self.post(self.bot.get_guild(payload.guild_id),
                            make_embed("⚠️  A server-log entry was deleted", f"Message `{payload.message_id}` — check the audit log for who did it.", color=RED))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if payload.channel_id == LOG_CHANNEL_ID:
            title = f"⚠️  {len(payload.message_ids)} server-log entries were bulk-deleted"
            await self.post(guild, make_embed(title, "Check the audit log for who did it.", color=RED))
            return
        await self.post(guild, make_embed("🗑️  Messages bulk-deleted", f"**{len(payload.message_ids)}** in <#{payload.channel_id}>", color=RED))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if self._ignored(after) or before.content == after.content:
            return
        e = make_embed("✏️  Message edited", f"In {after.channel.mention} · [jump]({after.jump_url})")
        e.add_field(name="Before", value=_clip(before.content), inline=False)
        e.add_field(name="After", value=_clip(after.content), inline=False)
        await self.post(after.guild, self._who(e, after.author))

    # ---- members --------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        age = discord.utils.utcnow() - member.created_at
        e = make_embed("📥  Member joined", f"{member.mention} · member #{member.guild.member_count}", color=GREEN)
        e.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R")
                    + ("  ⚠️ new account" if age < NEW_ACCOUNT else ""), inline=False)
        await self.post(member.guild, self._who(e, member))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(payload.guild_id)
        user = payload.user
        e = make_embed("📤  Member left", f"{user.mention}", color=RED)
        if isinstance(user, discord.Member) and user.joined_at:
            e.add_field(name="Joined", value=discord.utils.format_dt(user.joined_at, "R"), inline=True)
            roles = [r.mention for r in user.roles if not r.is_default()]
            if roles:
                e.add_field(name="Roles", value=_clip(" ".join(roles), 1000), inline=False)
        await self.post(guild, self._who(e, user))

    @commands.Cog.listener()
    async def on_gate_rejected(self, member: discord.Member):
        e = make_embed("🚪  Removed by the members-only gate", f"{member.mention} joined without a linked ElectIndex membership.", color=RED)
        await self.post(member.guild, self._who(e, member))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self.post(guild, self._who(make_embed("🔨  Member banned", user.mention, color=RED), user))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self.post(guild, self._who(make_embed("🕊️  Member unbanned", user.mention), user))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            e = make_embed("🏷️  Nickname changed", after.mention)
            e.add_field(name="Before", value=before.nick or "*(none)*", inline=True)
            e.add_field(name="After", value=after.nick or "*(none)*", inline=True)
            await self.post(after.guild, self._who(e, after))
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            e = make_embed("🎭  Roles changed", after.mention)
            if added:
                e.add_field(name="Added", value=" ".join(r.mention for r in added), inline=False)
            if removed:
                e.add_field(name="Removed", value=" ".join(r.mention for r in removed), inline=False)
            await self.post(after.guild, self._who(e, after))
        if before.timed_out_until != after.timed_out_until:
            if after.is_timed_out():
                e = make_embed("🔇  Timed out", f"{after.mention} until {discord.utils.format_dt(after.timed_out_until, 'f')}")
            else:
                e = make_embed("🔊  Timeout ended", after.mention)
            await self.post(after.guild, self._who(e, after))

    # ---- channels + roles -----------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        await self.post(channel.guild, make_embed("➕  Channel created", f"{channel.mention} · `{channel.name}`", color=GREEN))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self.post(channel.guild, make_embed("➖  Channel deleted", f"`#{channel.name}`", color=RED))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if before.name != after.name:
            await self.post(after.guild, make_embed("✏️  Channel renamed", f"`#{before.name}` → {after.mention}"))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        await self.post(role.guild, make_embed("➕  Role created", f"{role.mention} · `{role.name}`", color=GREEN))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self.post(role.guild, make_embed("➖  Role deleted", f"`{role.name}`", color=RED))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"Color: `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            gained = [p for p, v in after.permissions if v and not getattr(before.permissions, p)]
            lost = [p for p, v in before.permissions if v and not getattr(after.permissions, p)]
            if gained:
                changes.append("Permissions added: " + ", ".join(f"`{p}`" for p in gained))
            if lost:
                changes.append("Permissions removed: " + ", ".join(f"`{p}`" for p in lost))
        if changes:
            await self.post(after.guild, make_embed("🎭  Role updated", after.mention + "\n" + _clip("\n".join(changes), 3500)))


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerLog(bot))
