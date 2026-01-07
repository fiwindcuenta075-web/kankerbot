import os
import asyncio
import random
import logging
import string
import time as _time
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo
from io import BytesIO
from typing import Optional, List, Dict

import asyncpg

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest

logging.basicConfig(level=logging.INFO)

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

TZ = ZoneInfo("Europe/Amsterdam")
RESET_AT = time(5, 0)  # 05:00 Amsterdam boundary

# ✅ Zet hier je target chat/channel ID in (meestal -100...)
# Tip: post /chatid in je channel om hem te vinden.
CHAT_ID = int(os.getenv("CHAT_ID", "-1003328329377"))

DAILY_THREAD_ID = None
VERIFY_THREAD_ID = 4

# ✅ Alles in 1 channel/chat: forceer geen thread_id mee te sturen
FORCE_SINGLE_CHANNEL = os.getenv("FORCE_SINGLE_CHANNEL", "1") == "1"

PHOTO_PATH = "image (6).png"

# TEST intervals (seconds)
DAILY_SECONDS = 17
VERIFY_SECONDS = 30
JOIN_DELAY_SECONDS = 5 * 60
ACTIVITY_SECONDS = 25

DELETE_DAILY_SECONDS = 17
DELETE_LEAVE_SECONDS = 10000 * 6000

# ✅ Nieuw: pinned tekstbericht elke 10 uur
PINNED_TEXT = "hallo doei seconde"
PINNED_TEXT_SECONDS = 10 * 60 * 60  # 10 uur (test). Normaal: 24*60*60

# ===== retention/cap logging =====
BOT_MSG_RETENTION_DAYS = 2
BOT_MSG_MAX_ROWS = 20000
BOT_MSG_PRUNE_EVERY = 200
BOT_MSG_PRUNE_COUNTER = 0
BOT_ALLMSG_PRUNE_COUNTER = 0

# ===== ENABLE FLAGS via Railway Variables =====
ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
ENABLE_CLEANUP = os.getenv("ENABLE_CLEANUP", "1") == "1"
ENABLE_PINNED_TEXT = os.getenv("ENABLE_PINNED_TEXT", "1") == "1"

# (bestaan nog, maar loops zijn hard disabled verderop)
ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "0") == "1"
ENABLE_ACTIVITY = os.getenv("ENABLE_ACTIVITY", "0") == "1"

# ===== Telegram circuit breaker =====
TELEGRAM_PAUSE_UNTIL = 0.0  # epoch seconds

# ===== Throttle log spam for "paused" =====
_LAST_PAUSE_LOG_AT: Dict[str, float] = {}
_PAUSE_LOG_COOLDOWN = 15.0  # seconds per "what"

# ================== DB GLOBALS ==================
DB_POOL: Optional[asyncpg.Pool] = None
JOINED_NAMES: List[str] = []

# ================== CONTENT ==================
WELCOME_TEXT = (
    "Welcome to THE 18+ HUB Telegram group 😈\n\n"
    "⚠️ To be admitted to the group, please share the link!\n"
    "Also, confirm you're not a bot by clicking the \"Open group✅\" button\n"
    "below, and invite 5 people by sharing the link with them – via TELEGRAM "
    "REDDIT.COM or X.COM"
)

SHARE_URL = (
    "https://t.me/share/url?url=%20all%20exclusive%E2%80%94content%20"
    "https%3A%2F%2Ft.me%2F%2BAiDsi2LccXJmMjlh"
)


def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 0/3", url=SHARE_URL)],
        [InlineKeyboardButton("Open group✅", callback_data="open_group")]
    ])


def unlocked_text(name: str) -> str:
    return f"{name} Successfully unlocked the group✅"


# ================== DEBUG: CHAT ID (CHANNEL-SAFE) ==================
async def chatid_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Works for: private/group messages AND channel posts.
    In a channel, you must post: /chatid
    Bot must be admin with permission to post messages.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    text = (msg.text or "").strip()
    if not text:
        return

    cmd = text.split()[0]
    if not (cmd == "/chatid" or cmd.startswith("/chatid@")):
        return

    title = getattr(chat, "title", None)
    out = f"Chat ID: {chat.id}\nType: {chat.type}\nTitle: {title}"
    logging.info("CHATID -> id=%s type=%s title=%s", chat.id, chat.type, title)

    # In channels: reply_text is often not usable; send a normal message to the channel.
    await safe_send(lambda: context.bot.send_message(chat_id=chat.id, text=out), "debug_send_chatid")


# ================== SAFETY: TASK CRASH LOGGING ==================
def safe_create_task(coro, name: str):
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task):
        try:
            t.result()
        except Exception:
            logging.exception("Task crashed: %s", name)

    task.add_done_callback(_done)
    return task


# ================== CYCLE HELPERS ==================
def current_cycle_date(now: datetime) -> date:
    local = now.astimezone(TZ)
    if local.time() < RESET_AT:
        return local.date() - timedelta(days=1)
    return local.date()


# ================== DB ==================
async def db_init():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in je BOT service variables.")

    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS joined_names (
            name TEXT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS used_names (
            cycle_id DATE NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (cycle_id, name)
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_verify_messages (
            message_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bot_verify_messages_created_at
        ON bot_verify_messages(created_at);
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_messages (
            message_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bot_messages_created_at
        ON bot_messages(created_at);
        """)

    logging.info("DB initialized ok")


async def db_load_joined_names_into_memory():
    global JOINED_NAMES
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM joined_names ORDER BY first_seen ASC;")
    JOINED_NAMES = [r["name"] for r in rows]
    logging.info("Loaded %d joined names from DB", len(JOINED_NAMES))


async def db_remember_joined_name(name: str):
    name = (name or "").strip()
    if not name:
        return

    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO joined_names(name) VALUES($1) ON CONFLICT (name) DO NOTHING;",
                    name
                )
            break
        except Exception:
            logging.exception("DB remember name failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)

    if name not in JOINED_NAMES:
        JOINED_NAMES.append(name)


async def db_is_used(name: str) -> bool:
    cid = current_cycle_date(datetime.now(TZ))
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_names WHERE cycle_id = $1 AND name = $2 LIMIT 1;",
            cid, name
        )
    return row is not None


async def db_mark_used(name: str):
    cid = current_cycle_date(datetime.now(TZ))
    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO used_names(cycle_id, name) VALUES($1, $2) ON CONFLICT DO NOTHING;",
                    cid, name
                )
            return
        except Exception:
            logging.exception("DB mark used failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)


async def db_track_bot_verify_message_id(message_id: int):
    global BOT_MSG_PRUNE_COUNTER

    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO bot_verify_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    int(message_id)
                )

                BOT_MSG_PRUNE_COUNTER += 1
                if BOT_MSG_PRUNE_COUNTER % BOT_MSG_PRUNE_EVERY != 0:
                    return

                await conn.execute(
                    f"DELETE FROM bot_verify_messages "
                    f"WHERE created_at < NOW() - INTERVAL '{BOT_MSG_RETENTION_DAYS} days';"
                )

                await conn.execute(
                    """
                    DELETE FROM bot_verify_messages
                    WHERE message_id IN (
                        SELECT message_id
                        FROM bot_verify_messages
                        ORDER BY created_at DESC
                        OFFSET $1
                    );
                    """,
                    BOT_MSG_MAX_ROWS
                )
            return
        except Exception:
            logging.exception("DB track verify message failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)


async def db_track_bot_message_id(message_id: int):
    global BOT_ALLMSG_PRUNE_COUNTER

    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO bot_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    int(message_id)
                )

                BOT_ALLMSG_PRUNE_COUNTER += 1
                if BOT_ALLMSG_PRUNE_COUNTER % BOT_MSG_PRUNE_EVERY != 0:
                    return

                await conn.execute(
                    f"DELETE FROM bot_messages "
                    f"WHERE created_at < NOW() - INTERVAL '{BOT_MSG_RETENTION_DAYS} days';"
                )

                await conn.execute(
                    """
                    DELETE FROM bot_messages
                    WHERE message_id IN (
                        SELECT message_id
                        FROM bot_messages
                        ORDER BY created_at DESC
                        OFFSET $1
                    );
                    """,
