"""The #info-and-about and #community-rules channels: server info, about, links,
the role menu, and the rules.

The text lives here so either channel can be rebuilt with `/postinfo` after an
edit. The role menu is buttons with fixed custom ids, registered as a persistent
view, so they keep working across restarts.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR_ROLES, INFO_CHANNEL_ID, RULES_CHANNEL_ID, SELF_ROLES
from ui import NAVY, RED, SITE_URL, link_buttons, make_embed

log = logging.getLogger("electindex-bot.info")


def info_embeds() -> list[tuple[discord.Embed, discord.ui.View | None]]:
    server = make_embed(
        "🏛️  Server Information",
        "Welcome to the official **ElectIndex community server**!\n\n"
        "This server is the home for the ElectIndex community, where members can discuss elections, "
        "polling, political data, forecasting, and current events. Connect with other people interested "
        "in politics, elections, and data-driven analysis.",
    )
    server.add_field(
        name="Who can join",
        value="The server is for ElectIndex Members — **Supporters, Patrons and Founders**. "
              f"To bring a friend in, send them to **[electindex.com/discord]({SITE_URL}/discord/)**.",
        inline=False,
    )

    about = make_embed(
        "🗳️  About ElectIndex",
        "ElectIndex is your one-stop shop for everything elections, dedicated to election coverage, "
        "polling analysis, forecasting, and political data. Our goal is to provide accessible, accurate, "
        "and data-focused election information while encouraging thoughtful discussion.",
        color=RED,
    )

    links = make_embed("🔗  Links", "Follow ElectIndex everywhere we post.")
    links.add_field(name="Website", value=f"[electindex.com]({SITE_URL})", inline=True)
    links.add_field(name="X (Twitter)", value="[@ElectIndex](https://x.com/ElectIndex)", inline=True)
    links.add_field(name="Bluesky", value="[@electindex](https://bsky.app/profile/electindex.bsky.social)", inline=True)
    links_view = link_buttons(
        ("Website", SITE_URL, "🌐"),
        ("X", "https://x.com/ElectIndex", None),
        ("Bluesky", "https://bsky.app/profile/electindex.bsky.social", None),
    )

    roles = make_embed(
        "🎭  Pick your roles",
        "**Pings** — tap a button to opt in, tap it again to opt out.\n"
        + "\n".join(f"{emoji}  **{name}** — {desc}" for _, name, emoji, desc in SELF_ROLES)
        + "\n\n**Name colour** — pick one from the menu below to colour your name. "
        "Picking another swaps it; choose *No colour* to go back. Staff colours always show over these.",
    )
    return [(server, None), (about, None), (links, links_view), (roles, RoleMenu())]


RULES = (
    ("Be Respectful", "Treat everyone with respect. Personal attacks, harassment, discrimination, or targeted insults are not allowed. Debate ideas—not people."),
    ("Keep Discussions Civil", "Political disagreements are expected, but excessive hostility, trolling, baiting, or flame wars are not. If a conversation becomes unproductive, moderators may step in."),
    ("No Misinformation", "Do not knowingly spread false information, fabricated polls, fake election results, or edited screenshots presented as real. When possible, provide a source for claims."),
    ("Stay On Topic", "Keep discussions relevant to the channel you're in. Use the appropriate channels for elections, polling, policy, off-topic discussion, and support."),
    ("No Spam or Self-Promotion", "Do not spam messages, emojis, mentions, or links. Advertising, referral links, and self-promotion require moderator approval."),
    ("Appropriate Conduct", "Profanity is allowed in moderation. Hate speech, slurs, threats, harassment, or discriminatory language are prohibited."),
    ("No NSFW or Illegal Content", "Pornographic, excessively graphic, or illegal content is not allowed."),
    ("Respect Privacy", "Do not share personal information about yourself or others. Doxxing or encouraging harassment will result in an immediate ban."),
    ("No Impersonation", "Do not impersonate ElectIndex staff, public figures, journalists, campaigns, or other members."),
    ("Data & Polling Standards", "• Cite sources whenever possible.\n• Separate opinions from facts.\n• Clearly label satire or jokes.\n• Do not fabricate or manipulate polling or election data."),
    ("Follow Staff Instructions", "Moderators have the final say on rule enforcement. Public arguments over moderation decisions are not permitted."),
    ("Follow Discord's Terms", "All members must follow Discord's [Terms of Service](https://discord.com/terms) and [Community Guidelines](https://discord.com/guidelines)."),
    ("Subscribe to On Point Politics", "If you're not subscribed to [On Point Politics](https://www.youtube.com/@OnPointPoliticsOfficial), you're banned. *(Joke rule, per rule 10.)*"),
)


def rules_embeds() -> list[tuple[discord.Embed, discord.ui.View | None]]:
    embed = make_embed("📜  Server Rules", "By taking part in the ElectIndex Community you agree to these rules.")
    for i, (title, text) in enumerate(RULES, start=1):
        embed.add_field(name=f"{i}. {title}", value=text, inline=False)
    embed.add_field(
        name="\u200b",
        value="Breaking the rules can lead to a warning, a timeout, or removal from the server. "
              "Questions? Ask a member of the ElectIndex Team.",
        inline=False,
    )
    return [(embed, None)]


NO_COLOR = "none"


def color_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    """The configured colour roles that exist and are safe to hand out (no
    permissions, not an integration's role) — never trust a role by name alone."""
    by_name = {r.name: r for r in guild.roles if not r.managed and r.permissions.value == 0}
    return {name: by_name[name] for name, _, _ in COLOR_ROLES if name in by_name}


async def ensure_color_roles(guild: discord.Guild) -> list[discord.Role]:
    """Create any missing colour roles (no permissions, not hoisted)."""
    existing = {r.name for r in guild.roles}
    created = []
    for name, color, _ in COLOR_ROLES:
        if name not in existing:
            created.append(await guild.create_role(
                name=name, color=discord.Color(color), permissions=discord.Permissions.none(),
                hoist=False, mentionable=False, reason="Name-colour role (self-assigned in #info-and-about)"))
    return created


class ColorSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=name, value=name, emoji=emoji) for name, _, emoji in COLOR_ROLES]
        options.append(discord.SelectOption(label="No colour", value=NO_COLOR, emoji="✖️"))
        super().__init__(placeholder="🎨  Pick a name colour", options=options, min_values=1, max_values=1,
                         custom_id="ei:color", row=1)

    async def callback(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        roles = color_roles(guild)
        choice = self.values[0]
        target = roles.get(choice)
        if choice != NO_COLOR and target is None:
            await interaction.response.send_message("That colour isn't available right now.", ephemeral=True)
            return
        stale = [r for r in roles.values() if r in member.roles and r != target]
        if stale:
            await member.remove_roles(*stale, reason="Name colour changed")
        if target and target not in member.roles:
            await member.add_roles(target, reason="Name colour picked")
        text = f"Your name is now **{target.name}**." if target else "Name colour removed."
        await interaction.response.send_message(embed=make_embed(None, text, color=target.color if target else NAVY), ephemeral=True)


class RoleMenu(discord.ui.View):
    """Toggle buttons for the self-assignable roles. Persistent: no timeout, fixed ids."""

    def __init__(self):
        super().__init__(timeout=None)
        for role_id, name, emoji, _ in SELF_ROLES:
            button = discord.ui.Button(label=name, emoji=emoji, style=discord.ButtonStyle.secondary,
                                       custom_id=f"ei:selfrole:{role_id}", row=0)
            button.callback = self._toggle
            self.add_item(button)
        self.add_item(ColorSelect())

    async def _toggle(self, interaction: discord.Interaction):
        role_id = int(interaction.data["custom_id"].rsplit(":", 1)[1])
        allowed = {r for r, *_ in SELF_ROLES}
        role = interaction.guild.get_role(role_id) if interaction.guild and role_id in allowed else None
        if role is None:
            await interaction.response.send_message("That role isn't available any more.", ephemeral=True)
            return
        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role, reason="Self-role menu")
            text = f"Removed **{role.name}**."
        else:
            await member.add_roles(role, reason="Self-role menu")
            text = f"Added **{role.name}**."
        await interaction.response.send_message(embed=make_embed(None, text), ephemeral=True)


class Info(commands.Cog):
    """The info channel and its role menu."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(RoleMenu())  # re-attach handlers to menus posted before a restart

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            created = await ensure_color_roles(guild)
            if created:
                log.info("Created colour roles: %s", ", ".join(r.name for r in created))

    @commands.hybrid_command(name="postinfo", description="Staff: (re)post the info or rules embeds", hidden=True)
    @app_commands.describe(which="Which channel's embeds to post")
    @app_commands.choices(which=[app_commands.Choice(name="info", value="info"), app_commands.Choice(name="rules", value="rules")])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def postinfo(self, ctx: commands.Context, which: str = "info"):
        target = {"info": (INFO_CHANNEL_ID, info_embeds), "rules": (RULES_CHANNEL_ID, rules_embeds)}.get(which)
        channel = target and ctx.guild.get_channel(target[0])
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("Use `info` or `rules`.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        await post_embeds(channel, target[1]())
        await ctx.send(embed=make_embed("✅  Posted", f"{which.capitalize()} embeds posted in {channel.mention}."), ephemeral=True)


async def post_embeds(channel: discord.TextChannel, items):
    for embed, view in items:
        if view:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
