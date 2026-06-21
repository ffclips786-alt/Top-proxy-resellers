import os
import sqlite3
import logging
from datetime import datetime
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# --- OWNER_ID parsing -------------------------------------------------------
# This is parsed defensively because the most common cause of "bot does not
# recognize me as owner" is a bad/empty OWNER_ID value (extra spaces, not set
# on the host, etc). If it can't be parsed, we fall back to 0 and log loudly
# instead of crashing, so you can see the problem in the logs immediately.
_raw_owner_id = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID = int(_raw_owner_id) if _raw_owner_id else 0
except ValueError:
    OWNER_ID = 0

DB_PATH = os.getenv("DB_PATH", "reseller_bot.db")
OWNER_CONTACT_URL = "https://t.me/wgstrikes"

VARIANTS = {
    "1day": "1 Day",
    "7days": "7 Days",
    "15days": "15 Days",
    "30days": "30 Days",
}

DIVIDER = "━━━━━━━━━━━━━━"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if OWNER_ID == 0:
    logger.warning(
        "OWNER_ID is not set (or invalid). Owner-only features will not work "
        "until a valid Telegram numeric ID is set in the OWNER_ID environment "
        "variable on your host (e.g. Render dashboard -> Environment)."
    )

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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT DEFAULT 'user',
            balance REAL DEFAULT 0,
            joined_date TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant TEXT NOT NULL,
            key_value TEXT NOT NULL,
            is_sold INTEGER DEFAULT 0,
            sold_to INTEGER,
            sold_date TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            variant TEXT PRIMARY KEY,
            price REAL NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            variant TEXT,
            key_value TEXT,
            price REAL,
            date TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_user(user_id: int, username: str):
    """Create the user if missing, and keep the stored username up to date."""
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
    else:
        cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        # If this Telegram ID matches OWNER_ID but the row was created before
        # OWNER_ID was set correctly, auto-promote it to owner on next /start.
        if user_id == OWNER_ID:
            cur.execute("UPDATE users SET role = 'owner' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def find_user_by_identifier(identifier: str):
    """Look up a user by numeric ID or by @username / username."""
    identifier = identifier.strip().lstrip("@")
    conn = get_conn()
    cur = conn.cursor()
    if identifier.isdigit():
        cur.execute("SELECT * FROM users WHERE user_id = ?", (int(identifier),))
        row = cur.fetchone()
        conn.close()
        return row
    cur.execute("SELECT * FROM users WHERE username = ?", (identifier,))
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
    cur.execute("SELECT variant, COUNT(*) as cnt FROM stock WHERE is_sold = 0 GROUP BY variant")
    rows = cur.fetchall()
    conn.close()
    return rows


def pop_one_key(variant: str):
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


def count_purchases(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM transactions WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def list_resellers():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE role IN ('reseller', 'owner')")
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Small helpers shared by both command and button handlers
# ---------------------------------------------------------------------------


async def reply(update: Update, text: str, reply_markup=None):
    """Reply to either a normal message or a callback-query button press."""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )


def clear_awaiting(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    context.user_data.pop("temp", None)


async def notify_owner_of_sale(context, buyer, label, key, price, new_balance, when):
    if not OWNER_ID:
        return
    username = f"@{buyer.username}" if buyer.username else buyer.first_name
    text = (
        f"🛎️ *New Sale!*\n{DIVIDER}\n"
        f"👤 Reseller: {username} (`{buyer.id}`)\n"
        f"🔑 Plan: {label}\n"
        f"🔐 Key: `{key}`\n"
        f"💲 Price: ${price:.2f}\n"
        f"📅 Date: {when.strftime('%Y-%m-%d')}\n"
        f"🕒 Time: {when.strftime('%H:%M:%S UTC')}\n{DIVIDER}\n"
        f"💰 Reseller balance now: ${new_balance:.2f}"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logger.exception("Failed to notify owner of sale.")


async def notify_balance_change(context, target_id, old_balance, amount, new_balance, added: bool):
    verb = "Added" if added else "Deducted"
    emoji = "➕" if added else "➖"
    text = (
        f"💳 *Balance Update*\n{DIVIDER}\n"
        f"{emoji} {verb}: ${amount:.2f}\n"
        f"📊 Old balance: ${old_balance:.2f}\n"
        f"📊 New balance: ${new_balance:.2f}\n{DIVIDER}"
    )
    try:
        await context.bot.send_message(chat_id=target_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logger.exception("Failed to notify user of balance change.")


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def main_menu_keyboard(is_owner: bool):
    rows = [
        [InlineKeyboardButton("🔑 Buy Key", callback_data="menu_buy")],
        [
            InlineKeyboardButton("💰 My Balance", callback_data="menu_balance"),
            InlineKeyboardButton("📜 My History", callback_data="menu_history"),
        ],
        [InlineKeyboardButton("📞 Contact Owner", url=OWNER_CONTACT_URL)],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton("⚙️ Owner Panel", callback_data="menu_owner")])
    return InlineKeyboardMarkup(rows)


def back_keyboard(target: str = "back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=target)]])


def variant_buttons(prefix: str):
    rows = []
    items = list(VARIANTS.items())
    for i in range(0, len(items), 2):
        row = []
        for key, label in items[i : i + 2]:
            row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_owner")])
    return InlineKeyboardMarkup(rows)


def buy_menu_keyboard():
    rows = []
    items = list(VARIANTS.items())
    for i in range(0, len(items), 2):
        row = []
        for key, label in items[i : i + 2]:
            price = get_price(key)
            price_text = f"${price:.2f}" if price is not None else "N/A"
            row.append(InlineKeyboardButton(f"{label} - {price_text}", callback_data=f"buyvar_{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def owner_menu_keyboard():
    rows = [
        [
            InlineKeyboardButton("➕ Add Balance", callback_data="owner_addbalance"),
            InlineKeyboardButton("➖ Deduct Balance", callback_data="owner_deductbalance"),
        ],
        [
            InlineKeyboardButton("👤 Check User Info", callback_data="owner_userinfo"),
            InlineKeyboardButton("👥 Resellers List", callback_data="owner_resellers"),
        ],
        [
            InlineKeyboardButton("📦 Add Stock", callback_data="owner_addstock"),
            InlineKeyboardButton("💲 Set Price", callback_data="owner_setprice"),
        ],
        [InlineKeyboardButton("📊 Stock Overview", callback_data="owner_stock")],
        [
            InlineKeyboardButton("➕ Add Reseller", callback_data="owner_addreseller"),
            InlineKeyboardButton("➖ Remove Reseller", callback_data="owner_removereseller"),
        ],
        [
            InlineKeyboardButton("🚫 Ban User", callback_data="owner_ban"),
            InlineKeyboardButton("✅ Unban User", callback_data="owner_unban"),
        ],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(rows)


def confirm_buy_keyboard(variant_key: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"confirmbuy_{variant_key}"),
                InlineKeyboardButton("❌ Cancel", callback_data="menu_buy"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_reseller_or_owner(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user and user["role"] in ("reseller", "owner"))


def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await reply(update, "⛔ This action is for the bot owner only.")
            return
        return await func(update, context)

    return wrapper


def reseller_or_owner(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_reseller_or_owner(update.effective_user.id):
            await reply(update, "⛔ This action is for resellers only.")
            return
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Core screens (used by both /commands and buttons)
# ---------------------------------------------------------------------------


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_awaiting(context)
    user_id = update.effective_user.id
    owner = is_owner(user_id)
    text = f"🏠 *Main Menu*\n{DIVIDER}\nChoose an option below 👇"
    await reply(update, text, reply_markup=main_menu_keyboard(owner))


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await reply(update, "Please send /start first.")
        return
    text = f"💰 *Wallet Balance*\n{DIVIDER}\n${user['balance']:.2f}\n{DIVIDER}"
    await reply(update, text, reply_markup=back_keyboard())


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 10",
        (update.effective_user.id,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await reply(update, "No purchase history found.", reply_markup=back_keyboard())
        return
    text = f"🧾 *Purchase History* (last 10)\n{DIVIDER}\n"
    for r in rows:
        label = VARIANTS.get(r["variant"], r["variant"])
        text += (
            f"🔑 *{label}*\n"
            f"💲 ${r['price']:.2f}   🗓 {r['date'][:19]}\n"
            f"🔐 `{r['key_value']}`\n{DIVIDER}\n"
        )
    await reply(update, text, reply_markup=back_keyboard())


async def show_buy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_reseller_or_owner(update.effective_user.id):
        await reply(update, "⛔ Only resellers can buy keys. Contact the owner to become a reseller.",
                    reply_markup=back_keyboard())
        return
    text = f"🔑 *Buy Key*\n{DIVIDER}\nSelect a plan:"
    await reply(update, text, reply_markup=buy_menu_keyboard())


async def show_buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, variant_key: str):
    label = VARIANTS.get(variant_key, variant_key)
    price = get_price(variant_key)
    user = get_user(update.effective_user.id)
    if price is None:
        await reply(update, f"❌ Price for *{label}* is not set yet. Contact the owner.",
                    reply_markup=back_keyboard("menu_buy"))
        return
    text = (
        f"🔑 *{label}*\n{DIVIDER}\n"
        f"💲 Price: ${price:.2f}\n"
        f"💰 Your balance: ${user['balance']:.2f}\n{DIVIDER}\n"
        "Confirm purchase?"
    )
    await reply(update, text, reply_markup=confirm_buy_keyboard(variant_key))


async def do_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE, variant_key: str):
    user = get_user(update.effective_user.id)
    label = VARIANTS.get(variant_key, variant_key)
    price = get_price(variant_key)
    if price is None:
        await reply(update, f"❌ Price for *{label}* is not set.", reply_markup=back_keyboard("menu_buy"))
        return
    if user["balance"] < price:
        await reply(
            update,
            f"❌ Insufficient balance.\nRequired: ${price:.2f}\nYour balance: ${user['balance']:.2f}",
            reply_markup=back_keyboard("menu_buy"),
        )
        return
    key = pop_one_key(variant_key)
    if not key:
        await reply(update, f"❌ *{label}* is out of stock. Contact the owner.",
                    reply_markup=back_keyboard("menu_buy"))
        return

    now = datetime.utcnow()
    update_balance(user["user_id"], -price)
    log_transaction(user["user_id"], variant_key, key, price)
    new_balance = user["balance"] - price

    text = (
        f"🎉 *Purchase Successful!*\n{DIVIDER}\n"
        f"🔑 Plan: {label}\n"
        f"🔐 Key: `{key}`\n"
        f"💰 New balance: ${new_balance:.2f}\n{DIVIDER}"
    )
    await reply(update, text, reply_markup=back_keyboard())

    await notify_owner_of_sale(context, update.effective_user, label, key, price, new_balance, now)


# ---------------------------------------------------------------------------
# General commands
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    if user and user["role"] == "banned":
        await update.message.reply_text("🚫 You are banned. Please contact the owner.")
        return

    await update.message.reply_text(
        f"👋 Welcome, {u.first_name}!\n\n"
        "This is a reseller key-shop bot.\n\n"
        f"🆔 Your Telegram ID: `{u.id}`\n"
        "(Give this ID to the owner to become a reseller)",
        parse_mode=ParseMode.MARKDOWN,
    )
    await show_main_menu(update, context)


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"🆔 Your Telegram ID is: `{u.id}`", parse_mode=ParseMode.MARKDOWN)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_awaiting(context)
    await update.message.reply_text("✅ Cancelled.")
    await show_main_menu(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Use the buttons below to navigate the bot.\n"
        "/start - open the main menu\n"
        "/myid - show your Telegram ID\n"
        "/cancel - cancel the current action"
    )
    await show_main_menu(update, context)


# ---------------------------------------------------------------------------
# Text input handler (for owner multi-step actions)
# ---------------------------------------------------------------------------


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return  # not in any flow, ignore stray text

    if not is_owner(update.effective_user.id):
        clear_awaiting(context)
        return

    text = update.message.text.strip()
    temp = context.user_data.setdefault("temp", {})

    # --- Add balance: step 1 (target) -> step 2 (amount) -------------------
    if awaiting == "addbalance_id":
        target = find_user_by_identifier(text)
        if not target:
            await update.message.reply_text(
                "❌ User not found. Make sure they have sent /start to the bot at least once, then try again."
            )
            return
        temp["target_id"] = target["user_id"]
        context.user_data["awaiting"] = "addbalance_amount"
        await update.message.reply_text(f"Enter the amount to add for `{target['user_id']}`:",
                                         parse_mode=ParseMode.MARKDOWN)
        return

    if awaiting == "addbalance_amount":
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("❌ Please send a valid number.")
            return
        target_id = temp.get("target_id")
        target_before = get_user(target_id)
        old_balance = target_before["balance"]
        update_balance(target_id, amount)
        new_balance = old_balance + amount
        clear_awaiting(context)
        await update.message.reply_text(
            f"✅ Added ${amount:.2f} to user `{target_id}`.\n"
            f"📊 Old: ${old_balance:.2f} → New: ${new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_keyboard())
        await notify_balance_change(context, target_id, old_balance, amount, new_balance, added=True)
        return

    # --- Deduct balance: step 1 (target) -> step 2 (amount) ----------------
    if awaiting == "deductbalance_id":
        target = find_user_by_identifier(text)
        if not target:
            await update.message.reply_text("❌ User not found.")
            return
        temp["target_id"] = target["user_id"]
        context.user_data["awaiting"] = "deductbalance_amount"
        await update.message.reply_text(f"Enter the amount to deduct from `{target['user_id']}`:",
                                         parse_mode=ParseMode.MARKDOWN)
        return

    if awaiting == "deductbalance_amount":
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("❌ Please send a valid number.")
            return
        target_id = temp.get("target_id")
        target_before = get_user(target_id)
        old_balance = target_before["balance"]
        update_balance(target_id, -amount)
        new_balance = old_balance - amount
        clear_awaiting(context)
        await update.message.reply_text(
            f"✅ Deducted ${amount:.2f} from user `{target_id}`.\n"
            f"📊 Old: ${old_balance:.2f} → New: ${new_balance:.2f}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_keyboard())
        await notify_balance_change(context, target_id, old_balance, amount, new_balance, added=False)
        return

    # --- Check user info -----------------------------------------------------
    if awaiting == "userinfo_id":
        target = find_user_by_identifier(text)
        clear_awaiting(context)
        if not target:
            await update.message.reply_text("❌ User not found.", reply_markup=owner_menu_keyboard())
            return
        purchases = count_purchases(target["user_id"])
        info = (
            f"👤 *User Info*\n{DIVIDER}\n"
            f"🆔 ID: `{target['user_id']}`\n"
            f"📛 Username: {target['username']}\n"
            f"🏷 Role: {target['role']}\n"
            f"💰 Balance: ${target['balance']:.2f}\n"
            f"🔑 Total Keys Purchased: {purchases}\n"
            f"📅 Joined: {target['joined_date'][:19]}\n{DIVIDER}"
        )
        await update.message.reply_text(info, parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_keyboard())
        return

    # --- Add stock: variant already chosen, now expecting keys -------------
    if awaiting == "addstock_keys":
        variant_key = temp.get("variant")
        raw = text.replace(",", "\n")
        keys = [k.strip() for k in raw.split("\n") if k.strip()]
        add_stock(variant_key, keys)
        clear_awaiting(context)
        label = VARIANTS.get(variant_key, variant_key)
        await update.message.reply_text(f"✅ Added {len(keys)} key(s) to *{label}*.",
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_keyboard())
        return

    # --- Set price: variant already chosen, now expecting price ------------
    if awaiting == "setprice_value":
        try:
            price = float(text)
        except ValueError:
            await update.message.reply_text("❌ Please send a valid number.")
            return
        variant_key = temp.get("variant")
        set_price(variant_key, price)
        clear_awaiting(context)
        label = VARIANTS.get(variant_key, variant_key)
        await update.message.reply_text(f"✅ Price for *{label}* set to ${price:.2f}.",
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_keyboard())
        return

    # --- Add / remove reseller, ban / unban ---------------------------------
    if awaiting in ("addreseller_id", "removereseller_id", "ban_id", "unban_id"):
        target = find_user_by_identifier(text)
        if not target:
            # allow adding a reseller by raw numeric ID even if they haven't /start'ed yet
            if text.strip().isdigit():
                ensure_user(int(text.strip()), "unknown")
                target = get_user(int(text.strip()))
            else:
                await update.message.reply_text("❌ User not found.")
                return

        role_map = {
            "addreseller_id": "reseller",
            "removereseller_id": "user",
            "ban_id": "banned",
            "unban_id": "user",
        }
        set_role(target["user_id"], role_map[awaiting])
        clear_awaiting(context)
        action_label = {
            "addreseller_id": "promoted to reseller",
            "removereseller_id": "removed from resellers",
            "ban_id": "banned",
            "unban_id": "unbanned",
        }[awaiting]
        await update.message.reply_text(f"✅ User `{target['user_id']}` has been {action_label}.",
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=owner_menu_keyboard())
        return


# ---------------------------------------------------------------------------
# Callback query (button) handler
# ---------------------------------------------------------------------------


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "back_main":
        await show_main_menu(update, context)
        return
    if data == "menu_balance":
        await show_balance(update, context)
        return
    if data == "menu_history":
        await show_history(update, context)
        return
    if data == "menu_buy":
        await show_buy_menu(update, context)
        return
    if data.startswith("buyvar_"):
        variant_key = data.split("_", 1)[1]
        await show_buy_confirm(update, context, variant_key)
        return
    if data.startswith("confirmbuy_"):
        variant_key = data.split("_", 1)[1]
        await do_purchase(update, context, variant_key)
        return

    # ---- Owner-only screens ----
    if data == "menu_owner":
        if not is_owner(user_id):
            await query.answer("Owner only.", show_alert=True)
            return
        clear_awaiting(context)
        await reply(update, f"⚙️ *Owner Control Panel*\n{DIVIDER}\nManage your shop:", reply_markup=owner_menu_keyboard())
        return

    if not is_owner(user_id):
        await query.answer("Owner only.", show_alert=True)
        return

    if data == "owner_addbalance":
        context.user_data["awaiting"] = "addbalance_id"
        context.user_data["temp"] = {}
        await reply(update, "Send the user's Telegram ID or @username to add balance to:")
        return

    if data == "owner_deductbalance":
        context.user_data["awaiting"] = "deductbalance_id"
        context.user_data["temp"] = {}
        await reply(update, "Send the user's Telegram ID or @username to deduct balance from:")
        return

    if data == "owner_userinfo":
        context.user_data["awaiting"] = "userinfo_id"
        await reply(update, "Send the user's Telegram ID or @username to look up:")
        return

    if data == "owner_addstock":
        await reply(update, "📦 Choose a plan to add keys to:", reply_markup=variant_buttons("addstockvar"))
        return

    if data.startswith("addstockvar_"):
        variant_key = data.split("_", 1)[1]
        context.user_data["awaiting"] = "addstock_keys"
        context.user_data["temp"] = {"variant": variant_key}
        label = VARIANTS.get(variant_key, variant_key)
        await reply(update, f"Send the keys for *{label}* (one per line, or comma separated):")
        return

    if data == "owner_setprice":
        await reply(update, "💲 Choose a plan to set the price for:", reply_markup=variant_buttons("setpricevar"))
        return

    if data.startswith("setpricevar_"):
        variant_key = data.split("_", 1)[1]
        context.user_data["awaiting"] = "setprice_value"
        context.user_data["temp"] = {"variant": variant_key}
        label = VARIANTS.get(variant_key, variant_key)
        await reply(update, f"Send the new price for *{label}* (numbers only, e.g. 5 or 5.50):")
        return

    if data == "owner_stock":
        rows = stock_counts()
        if not rows:
            await reply(update, "📦 Stock is empty.", reply_markup=back_keyboard("menu_owner"))
            return
        text = f"📊 *Available Stock*\n{DIVIDER}\n" + "\n".join(
            f"🔑 {VARIANTS.get(r['variant'], r['variant'])}: *{r['cnt']}* keys" for r in rows
        ) + f"\n{DIVIDER}"
        await reply(update, text, reply_markup=back_keyboard("menu_owner"))
        return

    if data == "owner_resellers":
        rows = list_resellers()
        if not rows:
            await reply(update, "No resellers found.", reply_markup=back_keyboard("menu_owner"))
            return
        text = f"👥 *Resellers*\n{DIVIDER}\n"
        for r in rows:
            text += f"• {r['username']} (`{r['user_id']}`) — ${r['balance']:.2f} — {r['role']}\n"
        text += DIVIDER
        await reply(update, text, reply_markup=back_keyboard("menu_owner"))
        return

    if data == "owner_addreseller":
        context.user_data["awaiting"] = "addreseller_id"
        await reply(update, "Send the Telegram ID of the user to make a reseller:")
        return

    if data == "owner_removereseller":
        context.user_data["awaiting"] = "removereseller_id"
        await reply(update, "Send the Telegram ID or @username of the reseller to remove:")
        return

    if data == "owner_ban":
        context.user_data["awaiting"] = "ban_id"
        await reply(update, "Send the Telegram ID or @username of the user to ban:")
        return

    if data == "owner_unban":
        context.user_data["awaiting"] = "unban_id"
        await reply(update, "Send the Telegram ID or @username of the user to unban:")
        return


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Check your environment variables.")
    if not OWNER_ID:
        logger.warning("Starting with OWNER_ID = 0. Owner features are disabled until this is fixed.")

    logger.info(f"Loaded OWNER_ID = {OWNER_ID}")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
