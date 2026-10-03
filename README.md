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

New commands go in a cog under `cogs/`. Use `@commands.hybrid_command` so they get both forms.
The `!` prefix needs **Message Content Intent** enabled in the Developer Portal (Bot page).

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
