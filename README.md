# Crypto Price Alert Telegram Bot

A Telegram bot that watches coin prices (via CoinGecko, no API key needed) and
messages you the moment a price crosses a target you set.

## Commands

| Command | Example | What it does |
|---|---|---|
| `/start` | | Welcome message + quick reference |
| `/price <symbol>` | `/price btc` | Current USD price |
| `/alert <symbol> <above|below> <price>` | `/alert btc above 70000` | Create an alert |
| `/myalerts` | | List your active alerts with their IDs |
| `/remove <id>` | `/remove 3` | Cancel an alert |

Prices are checked every 60 seconds by default (configurable). All active
alerts are batched into a single CoinGecko API call per check, so this
scales fine even with a lot of alerts set.

## 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts.
3. Copy the token it gives you (looks like `123456:ABC-DEF...`).

## 2. Run it locally (optional, to test first)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your token into TELEGRAM_BOT_TOKEN

export $(cat .env | xargs)      # or use python-dotenv / your OS's env setup
python bot.py
```

Message your bot on Telegram and try `/start`.

## 3. Deploy — Railway

1. Push this folder to a GitHub repo.
2. On [railway.app](https://railway.app), **New Project → Deploy from GitHub repo**.
3. Railway detects `requirements.txt` and the `Procfile` automatically and
   runs the `worker` process (no HTTP port needed — this bot only polls
   Telegram, it doesn't serve web traffic).
4. In the service's **Variables** tab, add `TELEGRAM_BOT_TOKEN` with your token.
5. Deploy. Check the logs for `Bot starting...`.

**Persistence note:** Railway's filesystem is ephemeral across redeploys by
default. If you want alerts to survive a redeploy, add a
[Railway Volume](https://docs.railway.app/reference/volumes) mounted at the
app directory, and it'll keep `alerts.db` between deploys.

## 4. Deploy — Render

1. Push this folder to a GitHub repo.
2. On [render.com](https://render.com), **New → Background Worker** (not
   "Web Service" — this bot has no HTTP endpoint).
3. Connect the repo. Build command: `pip install -r requirements.txt`.
   Start command: `python bot.py`.
4. Under **Environment**, add `TELEGRAM_BOT_TOKEN` with your token.
5. Deploy. Check the logs for `Bot starting...`.

**Persistence note:** Render's free background workers also have an
ephemeral disk. Add a [Render Disk](https://render.com/docs/disks) (available
on paid plans) if you need alerts to survive restarts/redeploys long-term.

## Notes / things you might want to extend

- **Multi-user by design**: alerts are keyed by Telegram chat ID, so anyone
  who messages the bot can set their own alerts independently.
- **One-shot alerts**: an alert deactivates once it fires (so you don't get
  spammed every check cycle after the target is hit). Easy to change to
  "repeat every time it crosses" by removing the `deactivate_alert` call
  in `check_alerts`.
- **Symbol resolution**: `/alert eth ...` resolves "eth" to CoinGecko's
  `ethereum` via their search API and caches the mapping in memory. For
  obscure tickers with several coins sharing a symbol, it picks the
  top market-cap match.
- **Rate limits**: CoinGecko's free tier is generous but not unlimited. If
  you have many users, raise `CHECK_INTERVAL_SECONDS` rather than lowering
  it.
