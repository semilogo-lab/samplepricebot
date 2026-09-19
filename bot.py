"""
Crypto Price Alert Telegram Bot
--------------------------------
Lets users set price alerts for any coin listed on CoinGecko and get
pinged in Telegram the moment the price crosses their target.

Commands:
    /start                          Welcome + quick instructions
    /help                           Show command list
    /price <symbol>                 Current price, e.g. /price btc
    /alert <symbol> <above|below> <price>
                                     Create an alert, e.g. /alert btc above 70000
    /myalerts                       List your active alerts
    /remove <id>                    Cancel one of your alerts

Storage: local SQLite file (alerts.db). Prices: CoinGecko public API.
"""

import logging
import os
import sqlite3
import time
from contextlib import closing

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "alerts.db")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("crypto-alert-bot")

# In-memory cache: symbol (lowercase) -> coingecko coin id
_SYMBOL_CACHE: dict[str, str] = {}


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
                direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
                target_price REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def add_alert(chat_id: int, symbol: str, coin_id: str, direction: str, target_price: float) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            """
            INSERT INTO alerts (chat_id, symbol, coin_id, direction, target_price, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (chat_id, symbol.upper(), coin_id, direction, target_price, int(time.time())),
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


def deactivate_alert(alert_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (alert_id,))
        conn.commit()


# --------------------------------------------------------------------------
# CoinGecko helpers
# --------------------------------------------------------------------------

def resolve_symbol(symbol: str) -> str | None:
    """Map a ticker like 'btc' to a CoinGecko coin id like 'bitcoin'.

    Uses CoinGecko's search endpoint and picks the top market-cap match,
    since a symbol like ETH can technically map to multiple listed coins.
    Result is cached in memory for the life of the process.
    """
    symbol = symbol.lower().strip()
    if symbol in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[symbol]

    try:
        resp = requests.get(f"{COINGECKO_BASE}/search", params={"query": symbol}, timeout=10)
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
    except requests.RequestException as exc:
        logger.warning("CoinGecko search failed for %s: %s", symbol, exc)
        return None

    # Prefer an exact symbol match, ranked by market cap (search already
    # returns results ordered by relevance/market cap rank).
    exact = [c for c in coins if c.get("symbol", "").lower() == symbol]
    match = exact[0] if exact else (coins[0] if coins else None)
    if not match:
        return None

    coin_id = match["id"]
    _SYMBOL_CACHE[symbol] = coin_id
    return coin_id


def get_prices(coin_ids: list[str]) -> dict[str, float]:
    """Batch-fetch USD prices for multiple coin ids in a single API call."""
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


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Crypto price alert bot.\n\n"
        "/price btc — current price\n"
        "/alert btc above 70000 — notify me when BTC goes above $70,000\n"
        "/alert eth below 2000 — notify me when ETH drops below $2,000\n"
        "/myalerts — list your active alerts\n"
        "/remove <id> — cancel an alert\n\n"
        f"Prices are checked every {CHECK_INTERVAL_SECONDS} seconds."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /price btc")
        return

    symbol = context.args[0]
    coin_id = resolve_symbol(symbol)
    if not coin_id:
        await update.message.reply_text(f"Couldn't find a coin matching '{symbol}'.")
        return

    prices = get_prices([coin_id])
    price = prices.get(coin_id)
    if price is None:
        await update.message.reply_text("Price lookup failed, try again in a moment.")
        return

    await update.message.reply_text(f"{symbol.upper()}: ${price:,.4f}")


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 3 or args[1].lower() not in ("above", "below"):
        await update.message.reply_text(
            "Usage: /alert <symbol> <above|below> <price>\nExample: /alert btc above 70000"
        )
        return

    symbol, direction, price_str = args[0], args[1].lower(), args[2]
    try:
        target_price = float(price_str)
    except ValueError:
        await update.message.reply_text("Price must be a number, e.g. /alert btc above 70000")
        return

    coin_id = resolve_symbol(symbol)
    if not coin_id:
        await update.message.reply_text(f"Couldn't find a coin matching '{symbol}'.")
        return

    alert_id = add_alert(update.effective_chat.id, symbol, coin_id, direction, target_price)
    await update.message.reply_text(
        f"Alert #{alert_id} set: {symbol.upper()} {direction} ${target_price:,.2f}"
    )


async def myalerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_alerts(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no active alerts. Set one with /alert.")
        return

    lines = [
        f"#{r['id']}: {r['symbol']} {r['direction']} ${r['target_price']:,.2f}" for r in rows
    ]
    await update.message.reply_text("Your active alerts:\n" + "\n".join(lines))


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /remove <id> — see /myalerts for ids.")
        return

    alert_id = int(context.args[0])
    if remove_alert(update.effective_chat.id, alert_id):
        await update.message.reply_text(f"Alert #{alert_id} removed.")
    else:
        await update.message.reply_text(f"No active alert #{alert_id} found for you.")


# --------------------------------------------------------------------------
# Background job: check prices, fire alerts
# --------------------------------------------------------------------------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = all_active_alerts()
    if not rows:
        return

    coin_ids = [r["coin_id"] for r in rows]
    prices = get_prices(coin_ids)
    if not prices:
        return

    for row in rows:
        price = prices.get(row["coin_id"])
        if price is None:
            continue

        triggered = (
            (row["direction"] == "above" and price >= row["target_price"])
            or (row["direction"] == "below" and price <= row["target_price"])
        )
        if not triggered:
            continue

        deactivate_alert(row["id"])
        try:
            await context.bot.send_message(
                chat_id=row["chat_id"],
                text=(
                    f"🔔 {row['symbol']} is now ${price:,.4f} "
                    f"({row['direction']} your target of ${row['target_price']:,.2f})"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - don't let one bad chat kill the loop
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
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("myalerts", myalerts_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))

    app.job_queue.run_repeating(check_alerts, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot starting (checking prices every %ss)...", CHECK_INTERVAL_SECONDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
