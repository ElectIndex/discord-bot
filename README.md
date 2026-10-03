# ElectIndex Discord Bot

The bot for the private [ElectIndex](https://electindex.com) community Discord server.
It only runs on the ElectIndex server. Any other server it's added to, it leaves.

## Commands

Every command works as a slash command (`/ping`) and with the `!` prefix (`!ping`).

| Command | What it does |
| --- | --- |
| `help` | List the bot's commands |
| `about` | About ElectIndex and this bot |
| `ping` | Gateway and round-trip latency |
| `stats` | Server and bot statistics |
| `sync` | Staff only: sync member roles with electindex.com now |

New commands go in a cog under `cogs/`. Use `@commands.hybrid_command` so they get both forms.
The `!` prefix needs **Message Content Intent**, and the members-only gate needs **Server Members Intent**.
Turn both on in the Developer Portal (Bot page).

## Members-only access

The ElectIndex Community server is for paid ElectIndex Members, and electindex.com is the only way in.

1. A member connects Discord on their electindex.com account page (OAuth2, `identify guilds.join`).
2. If they're a Supporter, Patron or Founder, the website adds them to the server directly. There are no invite links.
3. The bot gives them the matching **Supporter / Patron / Founder** role. It creates these roles if they're missing.
4. Every 2 minutes the bot syncs roles with the website's list of linked members:
   upgrades swap the role, and a lapsed membership or a Discord disconnect takes the role away.
5. Anyone who joins **after `GATE_SINCE`** without coming through the website is DM'd a link and removed.
   Members who were in the server before the gate went live are grandfathered.

Safety: if a sync would strip roles from more than a quarter of tier members, or remove more than five people,
the bot skips those role removals and logs an error — that almost always means the website API returned a bad list.
Removals of uninvited joiners are never capped, so a burst of them can't switch the gate off.

The decisions live in `membership_plan.py` (pure, unit-tested); `cogs/membership.py` applies them.
The website half lives in the ElectIndex theme (`inc/discord.php`).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in DISCORD_TOKEN and GUILD_ID
.venv/bin/python bot.py
```

## Deploy (systemd)

```bash
git clone https://github.com/ElectIndex/discord-bot.git ~/discord-bot
cd ~/discord-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# create .env (never commit it)
sudo cp deploy/electindex-bot.service /etc/systemd/system/
sudo systemctl enable --now electindex-bot
journalctl -u electindex-bot -f
```

Update: `git pull && sudo systemctl restart electindex-bot`.

## License

MIT
