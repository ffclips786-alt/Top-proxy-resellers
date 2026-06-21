import os
import sqlite3
import logging
from datetime import datetime
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "reseller_bot.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT DEFAULT 'user',
            balance REAL DEFAULT 0,
            joined_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant TEXT NOT NULL,
            key_value TEXT NOT NULL,
            is_sold INTEGER DEFAULT 0,
            sold_to INTEGER,
            sold_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            variant TEXT PRIMARY KEY,
            price REAL NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            variant TEXT,
            key_value TEXT,
            price REAL,
            date TEXT
        )
    """)
    conn.commit()
    conn.close()


def ensure_user(user_id: int, username: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        role = "owner" if user_id == OWNER_ID else "user"
        cur.execute(
            "INSERT INTO users (user_id, username, role, balance, joined_date) VALUES (?, ?, ?, 0, ?)",
            (user_id, username, role, datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_role(user_id: int, role: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))
    conn.commit()
    conn.close()


def update_balance(user_id: int, amount: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def get_price(variant: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT price FROM prices WHERE variant = ?", (variant,))
    row = cur.fetchone()
    conn.close()
    return row["price"] if row else None


def set_price(variant: str, price: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO prices (variant, price) VALUES (?, ?) "
        "ON CONFLICT(variant) DO UPDATE SET price = excluded.price",
        (variant, price),
    )
    conn.commit()
    conn.close()


def add_stock(variant: str, keys: list):
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO stock (variant, key_value, is_sold) VALUES (?, ?, 0)",
        [(variant, k.strip()) for k in keys if k.strip()],
    )
    conn.commit()
    conn.close()


def stock_counts():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT variant, COUNT(*) as cnt FROM stock WHERE is_sold = 0 GROUP BY variant"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def pop_one_key(variant: str):
    """Fetch one unsold key for a variant and mark it sold. Returns key_value or None."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, key_value FROM stock WHERE variant = ? AND is_sold = 0 LIMIT 1",
        (variant,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    cur.execute(
        "UPDATE stock SET is_sold = 1, sold_date = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), row["id"]),
    )
    conn.commit()
    conn.close()
    return row["key_value"]


def log_transaction(user_id: int, variant: str, key_value: str, price: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (user_id, variant, key_value, price, date) VALUES (?, ?, ?, ?, ?)",
        (user_id, variant, key_value, price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def list_resellers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE role = 'reseller'")
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("⛔ Ye command sirf owner use kar sakta hai.")
            return
        return await func(update, context)
    return wrapper


def reseller_or_owner(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = get_user(update.effective_user.id)
        if not user or user["role"] not in ("reseller", "owner"):
            await update.message.reply_text("⛔ Ye command sirf resellers ke liye hai.")
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# General commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    if user and user["role"] == "banned":
        await update.message.reply_text("🚫 Aap ban hain. Owner se contact karein.")
        return
    await update.message.reply_text(
        f"👋 Welcome {u.first_name}!\n\n"
        "Yeh reseller key-shop bot hai.\n"
        "/help — saari commands dekhein\n\n"
        f"🆔 Aap ka Telegram ID: {u.id}\n"
        "(Owner ko ye ID dein reseller banne ke liye)"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    role = user["role"] if user else "user"

    text = "📖 *Available Commands*\n\n/start — bot start karein\n/balance — apna balance check karein\n/prices — variant prices dekhein\n"

    if role in ("reseller", "owner"):
        text += "\n*Reseller Commands:*\n/buy <variant> — key purchase karein (e.g. /buy 7days)\n/myhistory — apni purchase history\n"

    if role == "owner":
        text += (
            "\n*Owner Commands:*\n"
            "/addreseller <user_id>\n"
            "/removereseller <user_id> (= kick)\n"
            "/ban <user_id>\n"
            "/unban <user_id>\n"
            "/addbalance <user_id> <amount>\n"
            "/removebalance <user_id> <amount>\n"
            "/addstock <variant> <key1,key2,...>\n"
            "/setprice <variant> <price>\n"
            "/stock\n"
            "/resellers\n"
            "/userinfo <user_id>\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Pehle /start type karein.")
        return
    await update.message.reply_text(f"💰 Aapka balance: {user['balance']:.2f}")


async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM prices")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Abhi koi price set nahi hui.")
        return
    text = "💵 *Prices:*\n" + "\n".join(f"- {r['variant']}: {r['price']:.2f}" for r in rows)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Reseller commands
# ---------------------------------------------------------------------------

@reseller_or_owner
async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /buy <variant>\nExample: /buy 7days")
        return
    variant = context.args[0]
    user = get_user(update.effective_user.id)
    price = get_price(variant)
    if price is None:
        await update.message.reply_text(f"❌ '{variant}' variant ki price set nahi hai.")
        return
    if user["balance"] < price:
        await update.message.reply_text(
            f"❌ Insufficient balance. Required: {price:.2f}, Aapka balance: {user['balance']:.2f}"
        )
        return
    key = pop_one_key(variant)
    if not key:
        await update.message.reply_text(f"❌ '{variant}' stock me available nahi hai. Owner se contact karein.")
        return
    update_balance(user["user_id"], -price)
    log_transaction(user["user_id"], variant, key, price)
    await update.message.reply_text(
        f"✅ Purchase successful!\n\n🔑 Key ({variant}): `{key}`\n💰 New balance: {user['balance'] - price:.2f}",
        parse_mode=ParseMode.MARKDOWN,
    )


@reseller_or_owner
async def myhistory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 10",
        (update.effective_user.id,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Koi purchase history nahi mili.")
        return
    text = "🧾 *Last 10 Purchases:*\n\n"
    for r in rows:
        text += f"- {r['variant']} | {r['price']:.2f} | {r['date'][:19]}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Owner commands
# ---------------------------------------------------------------------------

@owner_only
async def addreseller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addreseller <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user_id.")
        return
    if not get_user(target_id):
        ensure_user(target_id, "unknown")
    set_role(target_id, "reseller")
    await update.message.reply_text(f"✅ User {target_id} ko reseller bana diya gaya.")


@owner_only
async def removereseller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removereseller <user_id>")
        return
    target_id = int(context.args[0])
    set_role(target_id, "user")
    await update.message.reply_text(f"✅ User {target_id} ko reseller list se hata diya gaya (kick).")


@owner_only
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    target_id = int(context.args[0])
    set_role(target_id, "banned")
    await update.message.reply_text(f"🚫 User {target_id} ban kar diya gaya.")


@owner_only
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    target_id = int(context.args[0])
    set_role(target_id, "user")
    await update.message.reply_text(f"✅ User {target_id} unban kar diya gaya.")


@owner_only
async def addbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addbalance <user_id> <amount>")
        return
    target_id = int(context.args[0])
    amount = float(context.args[1])
    if not get_user(target_id):
        ensure_user(target_id, "unknown")
    update_balance(target_id, amount)
    await update.message.reply_text(f"✅ {amount:.2f} balance add kar diya user {target_id} ko.")


@owner_only
async def removebalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /removebalance <user_id> <amount>")
        return
    target_id = int(context.args[0])
    amount = float(context.args[1])
    update_balance(target_id, -amount)
    await update.message.reply_text(f"✅ {amount:.2f} balance remove kar diya user {target_id} se.")


@owner_only
async def addstock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /addstock <variant> <key1,key2,key3>\nExample: /addstock 7days KEY-AAA,KEY-BBB"
        )
        return
    variant = context.args[0]
    keys_raw = " ".join(context.args[1:])
    keys = keys_raw.split(",")
    add_stock(variant, keys)
    await update.message.reply_text(f"✅ {len(keys)} keys '{variant}' variant me add ki gayi.")


@owner_only
async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setprice <variant> <price>")
        return
    variant = context.args[0]
    price = float(context.args[1])
    set_price(variant, price)
    await update.message.reply_text(f"✅ '{variant}' ki price {price:.2f} set kar di gayi.")


@owner_only
async def stock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = stock_counts()
    if not rows:
        await update.message.reply_text("Stock empty hai.")
        return
    text = "📦 *Available Stock:*\n\n" + "\n".join(f"- {r['variant']}: {r['cnt']} keys" for r in rows)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@owner_only
async def resellers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_resellers()
    if not rows:
        await update.message.reply_text("Koi reseller nahi mila.")
        return
    text = "👥 *Resellers:*\n\n"
    for r in rows:
        text += f"- {r['username']} (ID: {r['user_id']}) | Balance: {r['balance']:.2f}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@owner_only
async def userinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    target_id = int(context.args[0])
    user = get_user(target_id)
    if not user:
        await update.message.reply_text("User nahi mila.")
        return
    await update.message.reply_text(
        f"🆔 ID: {user['user_id']}\n👤 Username: {user['username']}\n"
        f"🏷 Role: {user['role']}\n💰 Balance: {user['balance']:.2f}\n📅 Joined: {user['joined_date']}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Check your .env file.")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID not set. Check your .env file.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))

    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("myhistory", myhistory_cmd))

    app.add_handler(CommandHandler("addreseller", addreseller_cmd))
    app.add_handler(CommandHandler("removereseller", removereseller_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance_cmd))
    app.add_handler(CommandHandler("removebalance", removebalance_cmd))
    app.add_handler(CommandHandler("addstock", addstock_cmd))
    app.add_handler(CommandHandler("setprice", setprice_cmd))
    app.add_handler(CommandHandler("stock", stock_cmd))
    app.add_handler(CommandHandler("resellers", resellers_cmd))
    app.add_handler(CommandHandler("userinfo", userinfo_cmd))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
