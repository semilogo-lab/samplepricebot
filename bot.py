"""
Crypto Price Alert Telegram Bot
--------------------------------
Everything after /start happens by tapping buttons - no other commands
to remember. Set a target price for any coin CoinGecko lists; when the
price gets there, you get a message. It keeps checking in periodically
while it stays there, and pauses on its own if you go quiet for a while.

/start is the only command. It shows a menu with three buttons:
    Check Price   - look up a coin's current price
    Set Alert     - pick a coin and a target price
    My Alerts     - see your alerts, with a Remove button on each

Storage: local SQLite file (alerts.db). Prices: CoinGecko public API.
"""

import logging
import math
import os
import sqlite3
import time
from contextlib import closing

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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


def add_alert(chat_id: int, symbol: str, coin_id: str, target_price: float) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO alerts (chat_id, symbol, coin_id, target_price, active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (chat_id, symbol, coin_id, target_price, int(time.time())),
        )
        conn.commit()


def list_alerts(chat_id: int) -> list[sqlite3.Row]:
    """Active alerts for this chat, oldest first - this order is what
    the on-screen numbering (#1, #2, ...) is built on."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM alerts WHERE chat_id = ? AND active = 1 ORDER BY id", (chat_id,)
        ).fetchall()


def remove_alert_by_db_id(chat_id: int, db_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE alerts SET active = 0 WHERE id = ? AND chat_id = ?", (db_id, chat_id)
        )
        conn.commit()


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
    if price >= 1:
        return f"{price:,.2f}"
    text = f"{price:,.8f}".rstrip("0")
    return text if not text.endswith(".") else text + "0"


def price_matches(price: float, target: float) -> bool:
    return abs(price - target) <= target * PRICE_MATCH_TOLERANCE


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💰 Check Price", callback_data="menu:price")],
            [InlineKeyboardButton("🔔 Set Alert", callback_data="menu:alert")],
            [InlineKeyboardButton("📋 My Alerts", callback_data="menu:myalerts")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:cancel")]])


def alerts_keyboard(count: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"❌ Remove #{i}", callback_data=f"remove:{i}")]
        for i in range(1, count + 1)
    ]
    buttons.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:cancel")])
    return InlineKeyboardMarkup(buttons)


# --------------------------------------------------------------------------
# /start - the only command
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Crypto price alert bot.\n\n"
        "Check prices, set alerts, and I'll message you when they're "
        "reached. Use the buttons below.",
        reply_markup=main_menu_keyboard(),
    )


# --------------------------------------------------------------------------
# Button taps
# --------------------------------------------------------------------------

async def show_my_alerts(chat_id: int, edit_target) -> None:
    """Renders the alerts list in place. edit_target is anything with an
    async edit_message_text(...) method - here, always a callback query."""
    rows = list_alerts(chat_id)
    if not rows:
        await edit_target.edit_message_text(
            "No alerts yet. Tap Set Alert to add one.", reply_markup=main_menu_keyboard()
        )
        return

    lines = [
        f"#{i}: {r['symbol']} at ${format_price(r['target_price'])}"
        for i, r in enumerate(rows, start=1)
    ]
    await edit_target.edit_message_text(
        "Your alerts:\n" + "\n".join(lines), reply_markup=alerts_keyboard(len(rows))
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    if data == "menu:cancel":
        context.user_data.clear()
        await query.edit_message_text("Main menu:", reply_markup=main_menu_keyboard())
        return

    if data == "menu:price":
        context.user_data.clear()
        context.user_data["state"] = "awaiting_price_symbol"
        await query.edit_message_text(
            "Send me the coin symbol (e.g. BTC):", reply_markup=cancel_keyboard()
        )
        return

    if data == "menu:alert":
        context.user_data.clear()
        context.user_data["state"] = "awaiting_alert_symbol"
        await query.edit_message_text(
            "Send me the coin symbol (e.g. BTC):", reply_markup=cancel_keyboard()
        )
        return

    if data == "menu:myalerts":
        context.user_data.clear()
        await show_my_alerts(chat_id, query)
        return

    if data.startswith("remove:"):
        position = int(data.split(":")[1])
        rows = list_alerts(chat_id)
        if 1 <= position <= len(rows):
            remove_alert_by_db_id(chat_id, rows[position - 1]["id"])
        await show_my_alerts(chat_id, query)
        return


# --------------------------------------------------------------------------
# Plain text replies - only meaningful while a button flow set a state
# --------------------------------------------------------------------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("state")
    text = update.message.text.strip()

    if state == "awaiting_price_symbol":
        symbol = text.upper()
        coin_id = resolve_symbol(symbol)
        context.user_data.clear()
        if not coin_id:
            await update.message.reply_text(
                f"Couldn't find {symbol}.", reply_markup=main_menu_keyboard()
            )
            return
        price = get_prices([coin_id]).get(coin_id)
        if price is None:
            await update.message.reply_text(
                "Couldn't get the price, try again in a bit.", reply_markup=main_menu_keyboard()
            )
            return
        await update.message.reply_text(
            f"{symbol}: ${format_price(price)}", reply_markup=main_menu_keyboard()
        )
        return

    if state == "awaiting_alert_symbol":
        symbol = text.upper()
        coin_id = resolve_symbol(symbol)
        if not coin_id:
            await update.message.reply_text(
                f"Couldn't find {symbol}. Try another symbol:", reply_markup=cancel_keyboard()
            )
            return  # stay in this state so they can retry
        context.user_data["pending_symbol"] = symbol
        context.user_data["pending_coin_id"] = coin_id
        context.user_data["state"] = "awaiting_alert_price"
        await update.message.reply_text(
            f"What price should trigger the alert for {symbol}?", reply_markup=cancel_keyboard()
        )
        return

    if state == "awaiting_alert_price":
        try:
            target_price = float(text)
        except ValueError:
            await update.message.reply_text(
                "That's not a number - send the target price:", reply_markup=cancel_keyboard()
            )
            return
        if not math.isfinite(target_price) or target_price <= 0:
            await update.message.reply_text(
                "Price has to be positive - send the target price:", reply_markup=cancel_keyboard()
            )
            return

        symbol = context.user_data.pop("pending_symbol")
        coin_id = context.user_data.pop("pending_coin_id")
        context.user_data.clear()

        add_alert(update.effective_chat.id, symbol, coin_id, target_price)
        position = len(list_alerts(update.effective_chat.id))
        await update.message.reply_text(
            f"Alert #{position} set: {symbol} at ${format_price(target_price)}\n"
            f"I'll let you know when it gets there.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # No pending flow - just re-show the menu instead of a bare "?" reply.
    await update.message.reply_text("Use the buttons below:", reply_markup=main_menu_keyboard())


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Use the buttons below:", reply_markup=main_menu_keyboard())


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
            continue

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
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.ALL, track_activity), group=1)

    app.job_queue.run_repeating(check_alerts, interval=CHECK_INTERVAL_SECONDS, first=10)

    logger.info("Bot starting (checking prices every %ss)...", CHECK_INTERVAL_SECONDS)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
