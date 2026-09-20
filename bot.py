"""
Crypto Price Alert Telegram Bot
--------------------------------
Set a target price for any coin CoinGecko lists. When the price gets there,
you get a message. Keeps checking in periodically while it stays there,
and pauses on its own if you go quiet for a few days.

Commands:
    /start                  What this bot does
    /p <symbol>             Check a price, e.g. /p BTC
    /a <symbol> <price>     Set an alert, e.g. /a saga 0.034
    /my                     See your alerts
    /rm <number>            Remove one

Alert numbers in /my always run 1, 2, 3... with no gaps - removing one
shifts the rest down, so the numbers stay easy to read at a glance.

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
PRICE_MATCH_TOLERANCE = float(os.environ.get("PRICE_MATCH_TOLERANCE", "0.005"))  # 0.5%
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("crypto-alert-bot")


# --------------------------------------------------------------------------
# Database
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


def add_alert(chat_id: int, symbol: str, coin_id: str, target_price: float) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO alerts (chat_id, symbol, coin_id, target_price, active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (chat_id, symbol, coin_id, target_price, int(time.time())),
        )
        conn.commit()
        return cur.lastrowid


def list_alerts(chat_id: int) -> list[sqlite3.Row]:
    """Active alerts for this chat, oldest first - this order is what
    /my and /rm's numbering is built on."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM alerts WHERE chat_id = ? AND active = 1 ORDER BY id", (chat_id,)
        ).fetchall()


def remove_alert_by_db_id(chat_id: int, db_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE alerts SET active = 0 WHERE id = ? AND chat_id = ? AND active = 1",
            (db_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0


def all_active_alerts() -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM alerts WHERE active = 1").fetchall()


def mark_notified(db_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE alerts SET last_notified = ? WHERE id = ?", (int(time.time()), db_id)
        )
        conn.commit()


def touch_user(chat_id: int) -> None:
    """Record that this chat just interacted with the bot."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO users (chat_id, last_seen) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_seen = excluded.last_seen",
            (chat_id, int(time.time())),
        )
        conn.commit()


def get_last_seen(chat_ids: list[int]) -> dict[int, int]:
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
    """Look up a ticker like 'saga' and return CoinGecko's coin id for it."""
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
    """Show enough decimals that small prices don't just read as $0.00."""
    if price >= 1:
        return f"{price:,.2f}"
    text = f"{price:,.8f}".rstrip("0")
    return text if not text.endswith(".") else text + "0"


def price_matches(price: float, target: float) -> bool:
    """Close enough to the target to count as 'reached' - an exact match
    on a live price practically never happens."""
    return abs(price - target) <= target * PRICE_MATCH_TOLERANCE


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Crypto price alert bot.\n\n"
        "/p <symbol> - check a price\n"
        "/a <symbol> <price> - alert me at this price\n"
        "/my - see your alerts\n"
        "/rm <number> - remove an alert\n\n"
        "Example: /a saga 0.034\n\n"
        "I'll message you when it gets there, and check in again every "
        "so often while it stays close."
    )


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /p <symbol>")
        return

    symbol = context.args[0].upper()
    coin_id = resolve_symbol(symbol)
    if not coin_id:
        await update.message.reply_text(f"Couldn't find {symbol}.")
        return

    price = get_prices([coin_id]).get(coin_id)
    if price is None:
        await update.message.reply_text("Couldn't get the price, try again in a bit.")
        return

    await update.message.reply_text(f"{symbol}: ${format_price(price)}")


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /a <symbol> <price>\nExample: /a saga 0.034")
        return

    symbol = args[0].upper()

    try:
        target_price = float(args[1])
    except ValueError:
        await update.message.reply_text("Price must be a number, e.g. /a saga 0.034")
        return

    if not math.isfinite(target_price) or target_price <= 0:
        await update.message.reply_text("Price has to be a positive number.")
        return

    coin_id = resolve_symbol(symbol)
    if not coin_id:
        await update.message.reply_text(f"Couldn't find {symbol}.")
        return

    add_alert(update.effective_chat.id, symbol, coin_id, target_price)
    position = len(list_alerts(update.effective_chat.id))  # the one just added is last
    await update.message.reply_text(
        f"Alert #{position} set: {symbol} at ${format_price(target_price)}\n"
        f"I'll let you know when it gets there."
    )


async def myalerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_alerts(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No alerts yet. Set one with /a.")
        return

    lines = [
        f"#{i}: {r['symbol']} at ${format_price(r['target_price'])}"
        for i, r in enumerate(rows, start=1)
    ]
    await update.message.reply_text("Your alerts:\n" + "\n".join(lines))


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /rm <number> - see /my for the list.")
        return

    position = int(context.args[0])
    rows = list_alerts(update.effective_chat.id)

    if position < 1 or position > len(rows):
        await update.message.reply_text(f"No alert #{position} - check /my.")
        return

    target_row = rows[position - 1]
    remove_alert_by_db_id(update.effective_chat.id, target_row["id"])
    await update.message.reply_text(f"Alert #{position} removed.")


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("I didn't understand that. Send /start to see what I can do.")


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        touch_user(update.effective_chat.id)


# --------------------------------------------------------------------------
# Background job
# --------------------------------------------------------------------------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = all_active_alerts()
    if not rows:
        return

    chat_ids = list({row["chat_id"] for row in rows})
    last_seen = get_last_seen(chat_ids)
    inactivity_cutoff = time.time() - (INACTIVITY_DAYS * 86400)

    prices = get_prices([r["coin_id"] for r in rows])
    if not prices:
        return

    for row in rows:
        if last_seen.get(row["chat_id"], 0) < inactivity_cutoff:
            continue  # this chat has gone quiet, don't message them

        price = prices.get(row["coin_id"])
        if price is None:
            continue

        if not price_matches(price, row["target_price"]):
            continue

        last_notified = row["last_notified"] or 0
        if time.time() - last_notified < RENOTIFY_COOLDOWN_SECONDS:
            continue

        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text=f"🔔 {row['symbol']} price now at ${format_price(price)}",
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
    app.add_handler(MessageHandler(filters.ALL, fallback))
    app.add_handler(MessageHandler(filters.ALL, track_activity), group=1)

    app.job_queue.run_repeating(check_alerts, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot starting (checking prices every %ss)...", CHECK_INTERVAL_SECONDS)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
