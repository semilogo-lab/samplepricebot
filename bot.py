"""
Crypto Price Alert Telegram Bot
--------------------------------
Set a target price for any coin CoinGecko lists, get pinged in Telegram
every time it's checked and the price is still past your target — the
alert keeps firing until you remove it with /rm.

If you go quiet for 5 days (no commands sent), the bot pauses your
notifications rather than messaging someone who's stopped using it. It
resumes automatically the moment you send anything again.

Commands:
    /start                          Welcome + how to use
    /p <symbol>                     Current price, e.g. /p BTC
    /a <symbol> <above|below> <price>
                                     Create an alert, e.g. /a BTC above 70000
    /my                              List your active alerts
    /rm <id>                        Cancel an alert

Storage: local SQLite file (alerts.db). Prices: CoinGecko public API.
"""

import logging
import math
import os
import sqlite3
import time
from contextlib import closing

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "alerts.db")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
RENOTIFY_COOLDOWN_SECONDS = int(os.environ.get("RENOTIFY_COOLDOWN_SECONDS", "180"))  # 3 min
INACTIVITY_DAYS = int(os.environ.get("INACTIVITY_DAYS", "5"))
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("crypto-alert-bot")


# --------------------------------------------------------------------------
# Database — one table, four operations
# --------------------------------------------------------------------------

def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
                target_price REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                last_notified INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                last_seen INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def add_alert(chat_id: int, symbol: str, coin_id: str, direction: str, target_price: float) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO alerts (chat_id, symbol, coin_id, direction, target_price, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (chat_id, symbol, coin_id, direction, target_price, int(time.time())),
        )
        conn.commit()
        return cur.lastrowid


def list_alerts(chat_id: int) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM alerts WHERE chat_id = ? AND active = 1 ORDER BY id", (chat_id,)
        ).fetchall()


def remove_alert(chat_id: int, alert_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE alerts SET active = 0 WHERE id = ? AND chat_id = ? AND active = 1",
            (alert_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0


def all_active_alerts() -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM alerts WHERE active = 1").fetchall()


def mark_notified(alert_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE alerts SET last_notified = ? WHERE id = ?", (int(time.time()), alert_id)
        )
        conn.commit()


def touch_user(chat_id: int) -> None:
    """Record that this chat just interacted with the bot (any command or
    message counts). Called on every update so 'last active' always
    reflects the most recent thing they sent."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO users (chat_id, last_seen) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_seen = excluded.last_seen",
            (chat_id, int(time.time())),
        )
        conn.commit()


def get_last_seen(chat_ids: list[int]) -> dict[int, int]:
    """Batch-fetch last-seen timestamps for a set of chats in one query."""
    if not chat_ids:
        return {}
    with closing(sqlite3.connect(DB_PATH)) as conn:
        placeholders = ",".join("?" for _ in chat_ids)
        rows = conn.execute(
            f"SELECT chat_id, last_seen FROM users WHERE chat_id IN ({placeholders})",
            chat_ids,
        ).fetchall()
        return dict(rows)


# --------------------------------------------------------------------------
# CoinGecko helpers
# --------------------------------------------------------------------------

def resolve_symbol(symbol: str) -> str | None:
    """Look up a ticker like 'btc' and return CoinGecko's coin id for it
    ('bitcoin'). Trusts the top search result — CoinGecko ranks matches by
    relevance/market cap, so the top hit is the coin most people mean.
    """
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/search", params={"query": symbol}, timeout=10
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
    except requests.RequestException as exc:
        logger.warning("CoinGecko search failed for %s: %s", symbol, exc)
        return None

    return coins[0]["id"] if coins else None


def get_prices(coin_ids: list[str]) -> dict[str, float]:
    """Batch-fetch USD prices for one or more CoinGecko coin ids."""
    if not coin_ids:
        return {}
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/simple/price",
            params={"ids": ",".join(sorted(set(coin_ids))), "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {cid: info["usd"] for cid, info in data.items() if "usd" in info}
    except requests.RequestException as exc:
        logger.warning("CoinGecko price fetch failed: %s", exc)
        return {}


def format_price(price: float) -> str:
    """Format a USD price with enough decimals to actually show it.

    Fixing this at 2 decimals works for BTC ($70,000.00) but silently
    shows $0.00 for anything under a cent (plenty of altcoins live there,
    e.g. $0.00086) - so coins under $1 get more decimal places, and
    trailing zeros are trimmed to keep it readable.
    """
    if price >= 1:
        return f"{price:,.2f}"
    text = f"{price:,.8f}".rstrip("0")
    return text if not text.endswith(".") else text + "0"


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Crypto price alert bot.\n\n"
        "Commands:\n"
        "/p (price) <symbol> - check a coin's current price\n"
        "/a (alert) <symbol> <above|below> <price> - set a price alert\n"
        "/my (my alerts) - list your active alerts\n"
        "/rm (remove) <id> - cancel an alert\n\n"
        "Note: once an alert hits, it reminds you again roughly every "
        f"{RENOTIFY_COOLDOWN_SECONDS // 60} minutes while the price stays "
        "past your target — it only stops when you /rm it. If you don't "
        f"use the bot for {INACTIVITY_DAYS} days, notifications pause "
        "automatically until you're active again.\n\n"
        "Examples:\n"
        "/p BTC\n"
        "/a BTC above 70000 - notify me when BTC goes above $70,000\n"
        "/a ETH below 2000 - notify me when ETH drops below $2,000\n"
        "/rm 3 - cancel alert #3\n\n"
        f"Prices are checked every {CHECK_INTERVAL_SECONDS} seconds."
    )


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /p BTC")
        return

    symbol = context.args[0].upper()
    coin_id = resolve_symbol(symbol)
    if not coin_id:
        await update.message.reply_text(f"Couldn't find a coin matching '{symbol}'.")
        return

    price = get_prices([coin_id]).get(coin_id)
    if price is None:
        await update.message.reply_text("Price lookup failed, try again in a moment.")
        return

    await update.message.reply_text(f"{symbol}: ${format_price(price)}")


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 3 or args[1].lower() not in ("above", "below"):
        await update.message.reply_text(
            "Usage: /a <symbol> <above|below> <price>\nExample: /a BTC above 70000"
        )
        return

    symbol = args[0].upper()
    direction = args[1].lower()

    try:
        target_price = float(args[2])
    except ValueError:
        await update.message.reply_text("Price must be a number, e.g. /a BTC above 70000")
        return

    if not math.isfinite(target_price) or target_price <= 0:
        await update.message.reply_text("Price has to be a positive number, e.g. /a BTC above 70000")
        return

    coin_id = resolve_symbol(symbol)
    if not coin_id:
        await update.message.reply_text(f"Couldn't find a coin matching '{symbol}'.")
        return

    alert_id = add_alert(update.effective_chat.id, symbol, coin_id, direction, target_price)
    await update.message.reply_text(
        f"Alert #{alert_id} set: {symbol} {direction} ${format_price(target_price)}\n"
        f"I'll remind you every {RENOTIFY_COOLDOWN_SECONDS // 60} min while it stays "
        f"{direction} that price — send /rm {alert_id} when you want it to stop."
    )


async def myalerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_alerts(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no active alerts. Set one with /a.")
        return

    lines = [
        f"#{r['id']}: {r['symbol']} {r['direction']} ${format_price(r['target_price'])}" for r in rows
    ]
    await update.message.reply_text("Your active alerts:\n" + "\n".join(lines))


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /rm <id> - see /my for ids.")
        return

    alert_id = int(context.args[0])
    if remove_alert(update.effective_chat.id, alert_id):
        await update.message.reply_text(f"Alert #{alert_id} removed.")
    else:
        await update.message.reply_text(f"No active alert #{alert_id} found for you.")


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("I didn't understand that. Send /start to see what I can do.")


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs on every single update, alongside whatever command handler
    also fires, purely to stamp 'last seen' for this chat."""
    if update.effective_chat:
        touch_user(update.effective_chat.id)


# --------------------------------------------------------------------------
# Background job: check prices every interval, fire alerts that hit
# --------------------------------------------------------------------------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = all_active_alerts()
    if not rows:
        return

    chat_ids = list({row["chat_id"] for row in rows})
    last_seen = get_last_seen(chat_ids)
    cutoff = time.time() - (INACTIVITY_DAYS * 86400)

    prices = get_prices([r["coin_id"] for r in rows])
    if not prices:
        return

    for row in rows:
        # Skip chats that have gone quiet for too long instead of
        # messaging someone who isn't using the bot anymore. Resumes
        # automatically the moment they send anything again.
        if last_seen.get(row["chat_id"], 0) < cutoff:
            continue

        price = prices.get(row["coin_id"])
        if price is None:
            continue

        hit = (
            (row["direction"] == "above" and price >= row["target_price"])
            or (row["direction"] == "below" and price <= row["target_price"])
        )
        if not hit:
            continue

        # No deactivation here on purpose: alerts keep firing on every check
        # while the price stays past the target, and only stop when the
        # user removes them with /rm. But we throttle how often a message
        # actually goes out, so "keeps firing" doesn't mean "every 60s
        # forever" - it means "reminds you periodically while it's true."
        last_notified = row["last_notified"] or 0
        if time.time() - last_notified < RENOTIFY_COOLDOWN_SECONDS:
            continue

        if row["direction"] == "above":
            phrase = f"price rise above ${format_price(row['target_price'])}"
        else:
            phrase = f"price drop to ${format_price(row['target_price'])}"

        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text=f"🔔 {row['symbol']} {phrase}\nNow at ${format_price(price)}",
            )
            mark_notified(row["id"])
        except Exception as exc:  # noqa: BLE001 - one bad chat shouldn't stop the rest
            logger.warning("Failed to notify chat %s: %s", row["chat_id"], exc)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Get a token from @BotFather on Telegram and set it before running."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("p", price_cmd))
    app.add_handler(CommandHandler("a", alert_cmd))
    app.add_handler(CommandHandler("my", myalerts_cmd))
    app.add_handler(CommandHandler("rm", remove_cmd))
    app.add_handler(MessageHandler(filters.ALL, fallback))  # anything else
    # Separate group so this runs on every update *in addition to* whatever
    # handler above already processed it, instead of competing with them.
    app.add_handler(MessageHandler(filters.ALL, track_activity), group=1)

    app.job_queue.run_repeating(check_alerts, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot starting (checking prices every %ss)...", CHECK_INTERVAL_SECONDS)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
