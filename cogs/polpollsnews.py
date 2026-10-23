"""#polpollsnews: new NYT polls become cards for staff to approve, and an
approved card is tweeted from @polpollsnews.

Every POLL_MINUTES the cog reads the NYT poll feeds and posts each new race
as a card with ✅ and ❌:

  ✅ by staff  -> tweet the card's text and image, then edit the card to link it
  ❌ by staff  -> delete the card
  ✏️ Edit      -> change the text (the image is redrawn from the new numbers)
  🖼️ Image     -> turn the image off / back on
  reply to a card with a picture -> that picture replaces the image

Each card's image (pollnews/graphic.py) is drawn after the card is posted,
because finding headshots (pollnews/photos.py: a web search, then Claude
checks the photo) takes a while the first time a person appears; ✅ waits
for it.

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

Every tweet the bot posts is then liked and reposted by @polpollsnews, and
a quote of one of them by a watched account (POLPOLLSNEWS_QUOTE_WATCH,
default @davidslosttt) is liked and reposted too.

Every night at midnight New York time the channel is cleared: every message
goes, except a card that is mid-tweet. Cards nobody decided on become
'cleared' (they are not re-carded).

Off unless POLPOLLSNEWS_CHANNEL_ID is set.
"""

import asyncio
import io
import json
import logging
import os
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import POLL_APPROVER_ROLE_IDS, POLPOLLSNEWS_CHANNEL_ID
from pollnews.card import Card, render, reparse, split_thread, tweet_length
from pollnews.graphic import context, photo_scope
from pollnews.graphic import render as render_image
from pollnews.photos import Photos
from pollnews.extract import (MAX_FILES, MAX_TOTAL_BYTES, ExtractError, build_source, expand,
                              extract, is_short, sniff_type)
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
DATA = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA / "polpollsnews.db"
CLEAR_AT = dtime(0, 0, tzinfo=ET)   # midnight Eastern, EST or EDT
IMAGE_WAIT = 180
QUOTE_WATCH = [u.strip().lstrip("@") for u in
               os.environ.get("POLPOLLSNEWS_QUOTE_WATCH", "davidslosttt").split(",") if u.strip()]
QUOTE_LOOKBACK_DAYS = 7   # quotes of older posts are ignored (and not looked for)
QUOTE_MAX_TRIES = 5    # seconds ✅ waits for a card's image to finish drawing
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
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


def _claude_client(sync: bool = False):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    # A key that isn't scoped to one workspace has to name it on every request.
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    headers = {"anthropic-workspace-id": ws} if ws else None
    return (anthropic.Anthropic if sync else anthropic.AsyncAnthropic)(default_headers=headers)


def _png(image: bytes) -> discord.File:
    return discord.File(io.BytesIO(image), filename="poll.png")


class CardView(discord.ui.View):
    """The buttons under a pending card. Persistent (fixed custom ids, no
    timeout): one registered instance answers for every card, across restarts."""

    def __init__(self, cog: "PolPollsNews", image_on: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        self.image_button.label = "Remove image" if image_on else "Add image"

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary,
                       custom_id="ppn:edit")
    async def edit_button(self, inter: discord.Interaction, _):
        await self.cog.open_editor(inter)

    @discord.ui.button(label="Remove image", emoji="🖼️", style=discord.ButtonStyle.secondary,
                       custom_id="ppn:image")
    async def image_button(self, inter: discord.Interaction, _):
        await self.cog.toggle_image(inter)


class EditModal(discord.ui.Modal, title="Edit poll card"):
    def __init__(self, cog: "PolPollsNews", message_id: int, text: str):
        super().__init__(timeout=900)
        self.cog, self.message_id = cog, message_id
        self.body = discord.ui.TextInput(label="Tweet text", style=discord.TextStyle.paragraph,
                                         default=text, max_length=1800)
        self.add_item(self.body)

    async def on_submit(self, inter: discord.Interaction):
        await self.cog.apply_edit(inter, self.message_id, self.body.value)


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
        self.photos = Photos(DATA / "cache", _claude_client(sync=True))
        self.image_tasks: dict[int, asyncio.Task] = {}
        self.meta: Meta | None = None
        self.meta_at = 0.0
        self.pending: dict[str, str] = {}
        self.posting = asyncio.Lock()
        self.menu = app_commands.ContextMenu(name="Format as poll", callback=self.poll_menu)
        self.menu.default_permissions = discord.Permissions(manage_messages=True)
        self.menu.guild_only = True
        self.bot.tree.add_command(self.menu)
        self.bot.add_view(CardView(self))
        self.poll_feed.change_interval(minutes=POLL_MINUTES)

    async def cog_load(self):
        if not POLPOLLSNEWS_CHANNEL_ID:
            log.info("POLPOLLSNEWS_CHANNEL_ID not set; NYT poll cards are off")
            return
        log.info("polling the NYT feeds every %g min; tweets %s", POLL_MINUTES,
                 "ON" if self.tweets_enabled and self.x else "off (dry run)")
        self.poll_feed.start()
        self.nightly_clear.start()
        self.quote_watch.start()

    async def cog_unload(self):
        self.poll_feed.cancel()
        self.nightly_clear.cancel()
        self.quote_watch.cancel()
        for t in self.image_tasks.values():
            t.cancel()
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
            msg = await ch.send(text, suppress_embeds=True, view=CardView(self),
                                allowed_mentions=discord.AllowedMentions.none())
            self.store.add_card(msg.id, ch.id, text, source, poll_id, ident)
            self.store.set_card_json(msg.id, card.to_json())
        await msg.add_reaction(YES)
        await msg.add_reaction(NO)
        n = len(split_thread(text))
        if n > 1:
            await msg.reply(f"-# {tweet_length(text)}/280 characters: this goes out as a "
                            f"{n}-tweet thread.", mention_author=False, silent=True)
        self.start_image(msg, card)
        return msg

    # ------------------------------------------------------------ images

    def draw(self, card: Card) -> bytes:
        """Runs in a worker thread: the photo search blocks."""
        return render_image(card, lambda c, l: self.photos.headshot(
            l.full, photo_scope(c), context(c, l)))

    def start_image(self, msg: discord.Message, card: Card):
        old = self.image_tasks.pop(msg.id, None)
        if old:
            old.cancel()
        task = asyncio.create_task(self._draw_and_attach(msg, card))
        self.image_tasks[msg.id] = task
        task.add_done_callback(lambda t: self.image_tasks.pop(msg.id, None)
                               if self.image_tasks.get(msg.id) is t else None)

    async def _draw_and_attach(self, msg: discord.Message, card: Card):
        try:
            png = await asyncio.to_thread(self.draw, card)
        except Exception:
            log.exception("drawing the image for card %s failed", msg.id)
            return
        row = self.store.card(msg.id)
        if row is None or row["image_custom"] or row["status"] not in ("pending", "failed"):
            return
        self.store.set_image(msg.id, png)
        if row["image_on"]:
            try:
                await msg.edit(attachments=[_png(png)])
            except discord.HTTPException as e:
                log.warning("couldn't attach the image to card %s: %s", msg.id, e)

    async def image_for_tweet(self, message_id: int) -> bytes | None:
        """The image to post with a card: wait for one still drawing; draw one
        now if it never was (e.g. the bot restarted mid-draw)."""
        task = self.image_tasks.get(message_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), IMAGE_WAIT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        row = self.store.card(message_id)
        if row is None or not row["image_on"]:
            return None
        if row["image"] is None and row["card_json"]:
            png = await asyncio.to_thread(self.draw, Card.from_json(row["card_json"]))
            self.store.set_image(message_id, png)
            return png
        return row["image"]

    @tasks.loop(time=CLEAR_AT)
    async def nightly_clear(self):
        ch = self.channel()
        if ch is None:
            return
        try:
            async with self.posting:   # not while a card is being sent
                keep = self.store.posting_ids()
                gone = await ch.purge(limit=None, check=lambda m: m.id not in keep,
                                      reason="#polpollsnews nightly clear")
                for m in gone:
                    task = self.image_tasks.pop(m.id, None)
                    if task:
                        task.cancel()
                cleared = self.store.clear_open(m.id for m in gone)
            log.info("nightly clear: deleted %d message(s) (%d undecided card(s)); kept %d mid-tweet",
                     len(gone), cleared, len(keep))
        except Exception:
            log.exception("nightly clear of #polpollsnews failed")

    @nightly_clear.before_loop
    async def _wait_ready_clear(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ quotes

    @tasks.loop(minutes=5)
    async def quote_watch(self):
        if not (self.tweets_enabled and self.x and QUOTE_WATCH):
            return
        try:
            await self.quote_pass()
        except Exception:
            log.exception("quote watch pass crashed; trying again next pass")

    @quote_watch.before_loop
    async def _wait_ready_quotes(self):
        await self.bot.wait_until_ready()

    async def quote_pass(self):
        ours = self.store.recent_tweet_ids(QUOTE_LOOKBACK_DAYS)
        if not ours:
            return   # nothing recent to be quoted: don't spend API reads
        done = set(json.loads(self.store.get_meta("quotes_done") or "[]"))
        retry = json.loads(self.store.get_meta("quotes_retry") or "{}")   # quote id -> tries
        for name in QUOTE_WATCH:
            uid = self.store.get_meta(f"quote_uid:{name}")
            if not uid:
                uid = await asyncio.to_thread(self.x.user_id, name)
                self.store.set_meta(f"quote_uid:{name}", uid)
            since = self.store.get_meta(f"quote_since:{name}")
            quotes, newest = await asyncio.to_thread(self.x.quotes_by, uid, since)
            for qid, quoted in quotes:
                if quoted in ours and qid not in done:
                    retry.setdefault(qid, 0)
                    log.info("@%s quoted our %s with %s", name, quoted, qid)
            if newest:
                self.store.set_meta(f"quote_since:{name}", newest)
        for qid in list(retry):
            problems = await asyncio.to_thread(self.x.like_and_repost, qid)
            if not problems:
                done.add(qid)
                del retry[qid]
                log.info("liked and reposted quote %s", qid)
                continue
            if not all("/xlogin" in p for p in problems):
                retry[qid] += 1   # a missing permission waits for a re-login instead
            log.warning("like/repost of quote %s (try %d): %s", qid, retry[qid], problems)
            if retry[qid] >= QUOTE_MAX_TRIES:
                done.add(qid)
                del retry[qid]
        self.store.set_meta("quotes_done", json.dumps(sorted(done)[-500:]))
        self.store.set_meta("quotes_retry", json.dumps(retry))

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
            card = card_for(polls[pid], key, meta)
            if is_short(card.url):
                card.url = await asyncio.to_thread(expand, card.url)
            try:
                await self.post_card(ch, card, "nyt", pid, key.ident())
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
            await msg.edit(view=None)
            await msg.reply(f"-# Approved by <@{user_id}>, but not tweeted ({why}). "
                            f"Would have posted {len(thread)} tweet(s).",
                            mention_author=False, silent=True)
            return
        image = await self.image_for_tweet(msg.id)
        no_media = image is not None and not await asyncio.to_thread(self.tokens.can_upload_media)
        if no_media:
            image = None   # an older login can't upload: tweet the text, say how to fix it
        try:
            ids = await asyncio.to_thread(self.x.post_thread, thread, image)
        except PostError as e:
            log.error("tweet failed for card %s: %s", msg.id, e)
            if e.posted:
                # Part of a thread went out: never re-post the root.
                self.store.finish(msg.id, "partial", e.posted, str(e))
                await msg.clear_reactions()
                await msg.edit(view=None)
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
        problems = await asyncio.to_thread(self.x.like_and_repost, ids[0])
        if problems:
            log.warning("like/repost of %s: %s", ids[0], problems)
            await msg.reply("-# Posted, but couldn't " + "; ".join(problems)[:400],
                            mention_author=False, silent=True)
        if no_media:
            await msg.reply("-# Tweeted without the image: the X login predates image posting. "
                            "An admin can run `/xlogin` again to allow it.",
                            mention_author=False, silent=True)
        await msg.clear_reactions()
        await msg.edit(content=f"{text}\n\n-# {YES} Posted by <@{user_id}>: <{self.x.url(ids[0])}>",
                       suppress=True, view=None, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ editing

    async def _editable(self, inter: discord.Interaction, message_id: int):
        """The card row if this person may change it now, else answers why and returns None."""
        ch = self.channel()
        row = self.store.card(message_id)
        if ch is None or not self.is_approver(inter.user, ch):
            await inter.response.send_message("Only poll approvers can change cards.",
                                              ephemeral=True)
            return None
        if row is None or row["status"] not in ("pending", "failed"):
            await inter.response.send_message("This card was already posted or rejected.",
                                              ephemeral=True)
            return None
        return row

    async def open_editor(self, inter: discord.Interaction):
        row = await self._editable(inter, inter.message.id)
        if row is not None:
            await inter.response.send_modal(EditModal(self, inter.message.id, row["text"]))

    async def apply_edit(self, inter: discord.Interaction, message_id: int, text: str):
        row = await self._editable(inter, message_id)
        if row is None:
            return
        text = text.strip()
        if not text:
            await inter.response.send_message("The card can't be empty.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        msg = await self.channel().fetch_message(message_id)
        note = ""
        card = None
        if row["card_json"] and not row["image_custom"]:
            try:
                card = reparse(text, Card.from_json(row["card_json"]))
            except ValueError:
                note = " I couldn't find candidate lines in it, so the image wasn't redrawn."
        self.store.set_text(message_id, text, card.to_json() if card else None)
        await msg.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
        if card is not None:
            self.start_image(msg, card)
            note = " Redrawing the image to match."
        n = len(split_thread(text))
        if n > 1:
            note += f" It's {tweet_length(text)}/280, so it will go out as a {n}-tweet thread."
        log.info("card %s edited by %s", message_id, inter.user)
        await inter.followup.send(f"Card updated.{note}", ephemeral=True)

    async def toggle_image(self, inter: discord.Interaction):
        row = await self._editable(inter, inter.message.id)
        if row is None:
            return
        on = not row["image_on"]
        self.store.set_image_on(inter.message.id, on)
        files = [_png(row["image"])] if on and row["image"] else []
        await inter.response.edit_message(attachments=files, view=CardView(self, on))
        if on and row["image"] is None and row["card_json"]:
            self.start_image(inter.message, Card.from_json(row["card_json"]))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """A reply to a pending card with a picture replaces the card's image."""
        if (message.channel.id != POLPOLLSNEWS_CHANNEL_ID or message.author.bot
                or message.reference is None or not message.attachments):
            return
        row = self.store.card(message.reference.message_id or 0)
        ch = self.channel()
        if row is None or ch is None or row["status"] not in ("pending", "failed"):
            return
        if not self.is_approver(message.author, ch):
            return
        pic = next((a for a in message.attachments
                    if (a.content_type or "").split(";")[0] in IMAGE_TYPES), None)
        if pic is None or pic.size > 5 * 1024 * 1024:
            return
        image = await pic.read()
        task = self.image_tasks.pop(row["message_id"], None)
        if task:
            task.cancel()   # a drawing still in progress must not overwrite this
        self.store.set_image(row["message_id"], image, on=True, custom=True)
        card_msg = await ch.fetch_message(row["message_id"])
        ext = (pic.filename.rsplit(".", 1)[-1] if "." in pic.filename else "png").lower()
        await card_msg.edit(attachments=[discord.File(io.BytesIO(image), filename=f"poll.{ext}")],
                            view=CardView(self, True))
        await message.add_reaction("🖼️")
        log.info("card %s image replaced by %s", row["message_id"], message.author)

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
        tweets = ("Approved cards will tweet, with their images." if self.tweets_enabled else
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
