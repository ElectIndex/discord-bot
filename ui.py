"""Shared look and feel: brand colors, the base embed, and a button paginator."""

import discord

NAVY = discord.Color(0x1B4B8F)
RED = discord.Color(0xE0241F)

SITE_URL = "https://electindex.com"
REPO_URL = "https://github.com/ElectIndex/discord-bot"
LOGO_URL = "https://electindex.com/wp-content/themes/electindex/assets/logo-square.png"


def make_embed(title: str | None = None, description: str | None = None, *, color: discord.Color = NAVY) -> discord.Embed:
    """An embed carrying the ElectIndex header and footer."""
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    embed.set_author(name="ElectIndex", url=SITE_URL, icon_url=LOGO_URL)
    embed.set_footer(text="electindex.com", icon_url=LOGO_URL)
    return embed


def error_embed(message: str) -> discord.Embed:
    return make_embed("Something went wrong", message, color=RED)


class Paginator(discord.ui.View):
    """Prev / page counter / next buttons over a list of embeds. Only the invoker can flip pages."""

    def __init__(self, pages: list[discord.Embed], author: discord.abc.User, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author = author
        self.index = 0
        self.message: discord.Message | None = None
        for i, page in enumerate(pages, start=1):
            page.set_footer(text=f"electindex.com  •  Page {i} of {len(pages)}", icon_url=LOGO_URL)
        self._refresh()

    async def send(self, ctx) -> None:
        if len(self.pages) == 1:
            await ctx.send(embed=self.pages[0])
            return
        self.message = await ctx.send(embed=self.pages[0], view=self)

    def _refresh(self) -> None:
        self.first.disabled = self.prev.disabled = self.index == 0
        self.next.disabled = self.last.disabled = self.index == len(self.pages) - 1
        self.counter.label = f"{self.index + 1} / {len(self.pages)}"

    async def _go(self, interaction: discord.Interaction, index: int) -> None:
        self.index = index
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[index], view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author.id:
            return True
        await interaction.response.send_message("Run the command yourself to flip through pages.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _):
        await self._go(interaction, 0)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, _):
        await self._go(interaction, self.index - 1)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _):
        await self._go(interaction, self.index + 1)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _):
        await self._go(interaction, len(self.pages) - 1)


def link_buttons(*links: tuple[str, str, str | None]) -> discord.ui.View:
    """A view of URL buttons from (label, url, emoji) tuples."""
    view = discord.ui.View()
    for label, url, emoji in links:
        view.add_item(discord.ui.Button(label=label, url=url, emoji=emoji))
    return view
