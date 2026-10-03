# ElectIndex Discord Bot

The bot for the private [ElectIndex](https://electindex.com) community Discord server.
It only runs on the ElectIndex server. Any other server it's added to, it leaves.

## Commands

Every command works as a slash command (`/ping`) and with the `!` prefix (`!ping`).

| Command | What it does |
| --- | --- |
| `help` | List the public commands |
| `about` | About ElectIndex and this bot |
| `ping` | Gateway and round-trip latency |
| `stats` | Server and bot statistics |
| `simulate president [lean] [chaos] [baseline] [dem] [rep] [seed]` | A random hypothetical presidential election, with a map |
| `simulate state <state> [lean] [chaos] [baseline] [dem] [rep] [seed]` | A random hypothetical result in one state, with a county map |
| `history president <year> [state]` | Any presidential election from 1928 to 2024, with a map |
| `history senate <state> [year]` | Senate results for a state, 1976–2024 |
| `history house <state> <district> [year]` | House results for a district, 1976–2024 |
| `forecast chamber <senate/house/governors>` | The ElectIndex 2026 chamber forecast |
| `forecast race <code>` | The forecast for one race, e.g. `GA-SEN`, `PA-GOV`, `PA-07` |

### Staff commands

Hidden from `/help` and, in the slash menu, from anyone without the matching permission.
`/staffhelp` lists them for staff. Every moderation action needs a reason, gets a numbered case,
DMs the member, and is logged in #server-logs.

| Command | Permission | What it does |
| --- | --- | --- |
| `warn <member> <reason>` | Timeout Members | Warn a member |
| `warnings <user>` | Timeout Members | A member's warnings and cases |
| `unwarn <case> <reason>` | Timeout Members | Remove a warning |
| `case <id>` | Timeout Members | Show one case |
| `timeout <member> <duration> <reason>` | Timeout Members | Time out for e.g. `10m`, `2h`, `1d` (max 28d) |
| `untimeout <member> <reason>` | Timeout Members | Lift a timeout |
| `mute <member> [duration] <reason>` | Timeout Members | Give the Muted role, optionally for e.g. `2h` |
| `unmute <member> <reason>` | Timeout Members | Lift a mute |
| `kick <member> <reason>` | Kick Members | Kick |
| `ban <user> [delete_days] <reason>` | Ban Members | Ban a member or a user id |
| `unban <user> <reason>` | Ban Members | Unban |
| `purge <amount> [member]` | Manage Messages | Delete up to 100 recent messages |
| `slowmode <seconds>` | Manage Channels | Set channel slowmode |
| `sync` | Manage Roles | Sync member roles with electindex.com now |
| `welcomepreview` | Manage Server | Preview the welcome card |

Staff = administrators, anyone with the command's permission, or a role in `STAFF_ROLE_IDS`.

## Server features

- **Welcome** — everyone who joins gets a branded card and message in #welcome-users, and leaves are
  announced. The card shows the member's tier when the gate admits them within a few seconds, and draws
  names in any script and emoji using installed Noto fonts (on Ubuntu:
  `apt install fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji`), falling back to the username only
  if no font can draw a character.
- **Autoroles** — everyone admitted gets the Member role. Reacting on the role menu in #info-and-about
  toggles ElectIndex Updates / Community Updates.
- **Starboard** — a message with 3 or more ⭐ reactions is reposted to #starboard with a live count. The
  author's own star and bots' stars don't count; the post comes down if stars drop below 3 or the original
  is deleted. Only messages every #starboard reader could already see are eligible (checked per role; never
  private threads).
- **Muted role** — denied sending, threads, reactions and voice in every channel (new channels too).
  Mutes survive leaving and rejoining, expire on time, and hand-applied mutes are tracked as well.
- **Server log** — message edits and deletes, joins and leaves, bans, nickname, role and timeout changes,
  and channel and role changes, all in #server-logs.

- **Election commands** — `/simulate` shifts a real baseline year by correlated national, regional and
  state noise (`lean` pins the national or statewide margin; `seed` replays a run). `/history` reads the
  presidential archive (1928–2024, county level) and Senate/House results (1976–2024); presidential
  electoral-vote totals include faithless electors and Maine/Nebraska splits, and match every certified
  result. `/forecast` reads the live ElectIndex model. All data is fetched at runtime from electindex.com and
  [ElectIndex/26_us_forecast_data](https://github.com/ElectIndex/26_us_forecast_data) and cached in
  `data/cache/` (`electdata.py`); the model is `electsim.py`, the maps `electmap.py`.

Channel, role and menu ids live in `config.py` (each overridable from the environment).

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
