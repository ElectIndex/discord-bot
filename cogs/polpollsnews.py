"""#polpollsnews: new NYT polls become cards for staff to approve, and an
approved card is tweeted from @polpollsnews.

Every POLL_MINUTES the cog reads the NYT poll feeds and posts each new race
as a card with ✅ and ❌:

  ✅ by staff  -> tweet the card's text, then edit the card to link the tweet
  ❌ by staff  -> delete the card

/poll <link or text> [image ×3] and the "Format as poll" message menu make
the same cards from anything else (a pollster's release, screenshots, a PDF);
Claude reads the numbers, pollnews/card.py writes the post.

Staff = administrators, Manage Messages, or a role in POLL_APPROVER_ROLE_IDS
(default STAFF_ROLE_IDS). Anyone else's ✅/❌ is removed.

Tweets use X OAuth 2.0: an administrator runs /xlogin once, signs in to
@polpollsnews on X's login screen and pastes back where X redirected them;
the bot refreshes the token itself after that (pollnews/xauth.py). Tweeting
is a dry run unless POLPOLLSNEWS_TWEETS_ENABLED=true: ✅ then only says what
would have gone out.

Off unless POLPOLLSNEWS_CHANNEL_ID is set.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import POLL_APPROVER_ROLE_IDS, POLPOLLSNEWS_CHANNEL_ID
from pollnews.card import Card, render, split_thread, tweet_length
from pollnews.extract import MAX_FILES, MAX_TOTAL_BYTES, ExtractError, build_source, extract, sniff_type
from pollnews.feeds import FeedError, load_live
from pollnews.meta import Meta, load_meta
from pollnews.nyt import card_for, new_races
from pollnews.store import Store, now_iso
from pollnews.twitter import PostError, XClient
from pollnews.xauth import AuthError, Login, Tokens, login_url
from ui import error_embed, make_embed

log = logging.getLogger("electindex-bot.polpollsnews")

ET = ZoneInfo("America/New_York")
YES, NO = "✅", "❌"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "polpollsnews.db"
POLL_MINUTES = float(os.environ.get("POLPOLLSNEWS_POLL_MINUTES") or 5)
MAX_CARDS_PER_PASS = int(os.environ.get("POLPOLLSNEWS_MAX_CARDS_PER_PASS") or 10)
META_REFRESH_SECONDS = 6 * 3600
X_HANDLE = os.environ.get("POLPOLLSNEWS_X_HANDLE", "polpollsnews").lstrip("@")


def _x_tokens() -> Tokens | None:
    cid = os.environ.get("POLPOLLSNEWS_X_CLIENT_ID", "")
    secret = os.environ.get("POLPOLLSNEWS_X_CLIENT_SECRET", "")
    if not (cid and secret):
        return None
    return Tokens(str(DB_PATH), cid, secret, X_HANDLE)


def _claude_client() -> anthropic.AsyncAnthropic | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    # A key that isn't scoped to one workspace has to name it on every request.
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return anthropic.AsyncAnthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)


class PolPollsNews(commands.Cog):
    """NYT polls → staff approval → @polpollsnews on X."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(str(DB_PATH))
        self.tokens = _x_tokens()
        self.x = XClient(self.tokens, X_HANDLE) if self.tokens else None
        self.logins: dict[int, Login] = {}   # user id -> their /xlogin in progress
        self.tweets_enabled = os.environ.get("POLPOLLSNEWS_TWEETS_ENABLED", "").lower() == "true"
        self.claude = _claude_client()
        self.meta: Meta | None = None
        self.meta_at = 0.0
        self.pending: dict[str, str] = {}
        self.posting = asyncio.Lock()
        self.menu = app_commands.ContextMenu(name="Format as poll", callback=self.poll_menu)
        self.menu.default_permissions = discord.Permissions(manage_messages=True)
        self.menu.guild_only = True
        self.bot.tree.add_command(self.menu)
        self.poll_feed.change_interval(minutes=POLL_MINUTES)

    async def cog_load(self):
        if not POLPOLLSNEWS_CHANNEL_ID:
            log.info("POLPOLLSNEWS_CHANNEL_ID not set; NYT poll cards are off")
            return
        log.info("polling the NYT feeds every %g min; tweets %s", POLL_MINUTES,
                 "ON" if self.tweets_enabled and self.x else "off (dry run)")
        self.poll_feed.start()

    async def cog_unload(self):
        self.poll_feed.cancel()
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    def channel(self) -> discord.TextChannel | None:
        ch = self.bot.get_channel(POLPOLLSNEWS_CHANNEL_ID)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def get_meta(self) -> Meta:
        now = asyncio.get_running_loop().time()
        if self.meta is None or now - self.meta_at > META_REFRESH_SECONDS:
            self.meta = await asyncio.to_thread(load_meta)
            self.meta_at = now
        return self.meta

    def is_approver(self, member, ch: discord.TextChannel) -> bool:
        if not isinstance(member, discord.Member) or member.bot or member.guild.id != ch.guild.id:
            return False
        perms = ch.permissions_for(member)
        return (perms.administrator or perms.manage_messages
                or bool(POLL_APPROVER_ROLE_IDS & {r.id for r in member.roles}))

    # ------------------------------------------------------------ cards

    async def post_card(self, ch: discord.TextChannel, card: Card, source: str,
                        poll_id: str | None = None, ident: str | None = None) -> discord.Message:
        text = render(card)
        async with self.posting:
            msg = await ch.send(text, suppress_embeds=True,
                                allowed_mentions=discord.AllowedMentions.none())
            self.store.add_card(msg.id, ch.id, text, source, poll_id, ident)
        await msg.add_reaction(YES)
        await msg.add_reaction(NO)
        n = len(split_thread(text))
        if n > 1:
            await msg.reply(f"-# {tweet_length(text)}/280 characters: this goes out as a "
                            f"{n}-tweet thread.", mention_author=False, silent=True)
        return msg

    @tasks.loop(minutes=5)
    async def poll_feed(self):
        try:
            await self.feed_pass()
        except Exception:
            log.exception("NYT feed pass crashed; trying again next pass")

    @poll_feed.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def feed_pass(self):
        ch = self.channel()
        if ch is None:
            log.error("#polpollsnews (%s) not found or not a text channel", POLPOLLSNEWS_CHANNEL_ID)
            return
        try:
            polls = await asyncio.to_thread(load_live)
        except FeedError as e:
            log.error("NYT feed fetch failed: %s", e)
            return
        if not self.store.get_meta("initialized_at"):
            self.store.mark_seen(((pid, k.ident()) for pid, p in polls.items() for k in p.races),
                                 "backlog at first run")
            self.store.set_meta("initialized_at", now_iso())
            log.info("first run: marked %d existing polls as seen; carding from now on", len(polls))
            return
        meta = await self.get_meta()
        d = new_races(polls, self.store.is_seen, self.pending, meta, datetime.now(ET).date())
        self.pending = d.pending
        for pid, key, reason in d.skipped:
            log.info("skip %s %s: %s", pid[:8], key.ident(), reason)
        self.store.mark_seen([(pid, key.ident()) for pid, key, _ in d.skipped], "skipped")
        # Past the cap, a race stays unseen with its fingerprint in
        # self.pending, so it is ready again next pass.
        for pid, key in d.ready[:MAX_CARDS_PER_PASS]:
            try:
                await self.post_card(ch, card_for(polls[pid], key, meta), "nyt", pid, key.ident())
            except discord.HTTPException as e:
                log.error("couldn't post card for %s %s: %s", pid[:8], key.ident(), e)
                break  # nothing was marked seen; next pass tries again
            self.store.mark_seen([(pid, key.ident())], "carded")

    # ------------------------------------------------------------ reactions

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, ev: discord.RawReactionActionEvent):
        emoji = str(ev.emoji)
        if (emoji not in (YES, NO) or ev.channel_id != POLPOLLSNEWS_CHANNEL_ID
                or ev.user_id == self.bot.user.id):
            return
        row = self.store.card(ev.message_id)
        if row is None or row["status"] not in ("pending", "failed"):
            return
        ch = self.channel()
        if ch is None:
            return
        msg = await ch.fetch_message(ev.message_id)
        if not self.is_approver(ev.member or ch.guild.get_member(ev.user_id), ch):
            await msg.remove_reaction(ev.emoji, discord.Object(id=ev.user_id))
            return
        if emoji == NO:
            if self.store.claim(ev.message_id, ev.user_id, ("pending", "failed"), "rejected"):
                await msg.delete()
            return
        if self.store.claim(ev.message_id, ev.user_id, ("pending", "failed"), "posting"):
            await self.approve(msg, row["text"], ev.user_id)
        # else: another approver's click is already posting it

    async def approve(self, msg: discord.Message, text: str, user_id: int):
        thread = split_thread(text)
        if not self.tweets_enabled or self.x is None:
            self.store.finish(msg.id, "approved-dry-run")
            why = ("POLPOLLSNEWS_TWEETS_ENABLED is off" if self.x
                   else "POLPOLLSNEWS_X_CLIENT_ID / _SECRET aren't set")
            await msg.clear_reactions()
            await msg.reply(f"-# Approved by <@{user_id}>, but not tweeted ({why}). "
                            f"Would have posted {len(thread)} tweet(s).",
                            mention_author=False, silent=True)
            return
        try:
            ids = await asyncio.to_thread(self.x.post_thread, thread)
        except PostError as e:
            log.error("tweet failed for card %s: %s", msg.id, e)
            if e.posted:
                # Part of a thread went out: never re-post the root.
                self.store.finish(msg.id, "partial", e.posted, str(e))
                await msg.clear_reactions()
                await msg.reply(f"⚠️ Thread broke after {len(e.posted)}/{len(thread)} tweets: "
                                f"<{self.x.url(e.posted[0])}>\n`{str(e)[:300]}`",
                                mention_author=False)
            else:
                self.store.finish(msg.id, "failed", None, str(e))
                await msg.remove_reaction(YES, discord.Object(id=user_id))
                await msg.reply(f"⚠️ Tweet failed, nothing was posted. React {YES} again to "
                                f"retry.\n`{str(e)[:300]}`", mention_author=False)
            return
        self.store.finish(msg.id, "posted", ids)
        log.info("card %s tweeted by %s: %s", msg.id, user_id, ids)
        await msg.clear_reactions()
        await msg.edit(content=f"{text}\n\n-# {YES} Posted by <@{user_id}>: <{self.x.url(ids[0])}>",
                       suppress=True, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ /xlogin

    @commands.hybrid_command(name="xlogin", hidden=True,
                             description="Staff: log the bot in to @polpollsnews on X")
    @app_commands.describe(url="The address of the page X sent you to after you clicked "
                           "Authorize (step 2)")
    @app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    async def xlogin(self, ctx: commands.Context, *, url: str = ""):
        if not ctx.author.guild_permissions.administrator:
            raise commands.MissingPermissions(["administrator"])
        if self.tokens is None:
            await ctx.send(embed=error_embed("Set POLPOLLSNEWS_X_CLIENT_ID and "
                                             "POLPOLLSNEWS_X_CLIENT_SECRET first."), ephemeral=True)
            return
        if not url:
            login = login_url(self.tokens.client_id)
            self.logins[ctx.author.id] = login
            st = await asyncio.to_thread(self.tokens.status)
            now = (f"Currently logged in as **@{st['username']}** (since {st['since']})."
                   if st else "Not logged in yet.")
            embed = make_embed("Log in to @polpollsnews", f"{now}\n\n"
                               f"**1.** [Open the X login screen]({login.url}) and sign in as "
                               f"**@{X_HANDLE}** (a private window avoids your own account), "
                               f"then click **Authorize app**.\n"
                               f"**2.** X sends you to a page that won't load "
                               f"(`127.0.0.1:8765/callback?...`). That's expected. Copy its "
                               f"whole address and run `/xlogin url:<paste>`.\n\n"
                               f"The link works once, for 15 minutes.")
            await ctx.send(embed=embed, ephemeral=True)
            return
        login = self.logins.get(ctx.author.id)
        if login is None:
            await ctx.send(embed=error_embed("Run `/xlogin` with no url first to get a login link."),
                           ephemeral=True)
            return
        try:
            username = await asyncio.to_thread(self.tokens.finish, login, url)
        except AuthError as e:
            await ctx.send(embed=error_embed(str(e)), ephemeral=True)
            return
        finally:
            if ctx.interaction is None:   # the code was pasted in the open; tidy it away
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
        self.logins.pop(ctx.author.id, None)
        log.info("X login: @%s authorized by %s", username, ctx.author)
        tweets = ("Approved cards will tweet." if self.tweets_enabled else
                  "Tweeting is still a dry run until POLPOLLSNEWS_TWEETS_ENABLED=true.")
        await ctx.send(embed=make_embed("Logged in", f"The bot now posts as **@{username}**. "
                                        f"{tweets}"), ephemeral=True)

    # ------------------------------------------------------------ /poll

    @commands.hybrid_command(name="poll", hidden=True,
                             description="Staff: card a poll from a link, text, screenshots or a PDF")
    @app_commands.describe(source="A link to the poll, or the results pasted as text",
                           image="A screenshot (or PDF) of the poll",
                           image2="Another screenshot, e.g. a second page",
                           image3="Another screenshot")
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    async def poll(self, ctx: commands.Context, image: discord.Attachment | None = None,
                   image2: discord.Attachment | None = None,
                   image3: discord.Attachment | None = None, *, source: str = ""):
        files = [f for f in (image, image2, image3) if f]
        if ctx.interaction is None:
            # !poll: everything attached to the message counts
            files = list(ctx.message.attachments)
        await self.manual(ctx, source, files, [])

    async def poll_menu(self, inter: discord.Interaction, message: discord.Message):
        # Pasted image links and forwarded images arrive as embeds, not attachments.
        embedded = [u for e in message.embeds
                    for u in ((e.image.url if e.image else None),
                              (e.thumbnail.url if e.thumbnail and e.type == "image" else None)) if u]
        ctx = await commands.Context.from_interaction(inter)
        await self.manual(ctx, message.content, list(message.attachments), embedded)

    async def manual(self, ctx: commands.Context, text: str, files: list[discord.Attachment],
                     image_urls: list[str]):
        ch = self.channel()
        if ch is None:
            await ctx.send(embed=error_embed("#polpollsnews isn't set up."), ephemeral=True)
            return
        if not self.is_approver(ctx.author, ch):
            raise commands.MissingPermissions(["manage_messages"])
        if self.claude is None:
            await ctx.send(embed=error_embed("ANTHROPIC_API_KEY isn't set, so I can't read polls."),
                           ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        try:
            if len(files) + len(image_urls) > MAX_FILES:
                raise ExtractError(f"Send at most {MAX_FILES} images/files at once")
            if sum(f.size for f in files) > MAX_TOTAL_BYTES:
                raise ExtractError("Those files are too big to read for one poll")
            attachments = [(await f.read(), sniff_type(f.content_type, f.filename)) for f in files]
            src = await asyncio.to_thread(build_source, text, attachments, image_urls)
            cards = await extract(self.claude, src, await self.get_meta(), datetime.now(ET).date())
        except ExtractError as e:
            await ctx.send(embed=error_embed(f"Couldn't make a poll card: {e}"), ephemeral=True)
            return
        links = [(await self.post_card(ch, c, "manual")).jump_url for c in cards]
        await ctx.send(f"Carded {len(cards)} race(s) for approval: " + " ".join(links),
                       ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PolPollsNews(bot))
