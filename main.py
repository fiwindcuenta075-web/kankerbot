import os
import asyncio
import random
import logging
import string
import time as _time
import urllib.parse
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
    filters,
)
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest

logging.basicConfig(level=logging.INFO)

# ================== CONFIG ==================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

TZ = ZoneInfo("Europe/Amsterdam")
RESET_AT = time(5, 0)  # 05:00 Amsterdam boundary

# ✅ Groep-id (meestal -100...)
CHAT_ID = int(os.getenv("CHAT_ID", "-1003418364423"))

# Topics/threads
DAILY_THREAD_ID = None
VERIFY_THREAD_ID = 4

# ✅ Alles in 1 chat
FORCE_SINGLE_CHANNEL = os.getenv("FORCE_SINGLE_CHANNEL", "1") == "1"

# ✅ WELCOME FOTO
PHOTO_PATH = "image (6).png"

# TEST intervals (seconds)
DAILY_SECONDS = 17
VERIFY_SECONDS = 30
JOIN_DELAY_SECONDS = 5 * 60
ACTIVITY_SECONDS = 25

DELETE_DAILY_SECONDS = 17
DELETE_LEAVE_SECONDS = 10000 * 6000

# ===== LINKS / SHARE =====
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/+D8FCvP2JTYVlZTZk")
GATEWAY_LINK = os.getenv("GATEWAY_LINK", "https://t.me/pareltjesGW")

# ✅ PINNED FOTO (veranderd naar IMG_1211.jpg)
PINNED_BANNER_PATH = os.getenv("PINNED_BANNER_PATH", "IMG_1211.jpg")

# ===== CAPTION (HTML) =====
CAPTION = """
✨ <b>Pareltjes – Community & Handel</b> ✨

<b>Gateway (link):</b> 🔥
https://t.me/pareltjesGW

🔔 <b>Tip:</b> Deel naar 3 mensen voor instant accept 0/3 ✅👇
""".strip()

# ===== SHARE TEKST (zelfde volgorde, met emojis) =====
SHARE_TEXT = (
    "✨ Pareltjes – Community & Handel ✨\n\n"
    "Gateway (link): 🔥\n"
    "https://t.me/pareltjesGW\n\n"
)

# Deel-link (Telegram share)
SHARE_URL = (
    "https://t.me/share/url?"
    + "url=" + urllib.parse.quote(GROUP_LINK)
    + "&text=" + urllib.parse.quote(SHARE_TEXT)
)

# ✅ Interval pinned (test/normal)
PINNED_TEXT_SECONDS = 20  # test. normaal: 10*60*60 of 24*60*60

# ===== retention/cap logging =====
BOT_MSG_RETENTION_DAYS = 2
BOT_MSG_MAX_ROWS = 20000
BOT_MSG_PRUNE_EVERY = 200
BOT_ALLMSG_PRUNE_COUNTER = 0

# ✅ extra: verify-topic pruning counter
BOT_VERIFYMSG_PRUNE_COUNTER = 0

# ===== ENABLE FLAGS via Railway Variables =====
ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
ENABLE_CLEANUP = os.getenv("ENABLE_CLEANUP", "1") == "1"
ENABLE_PINNED_TEXT = os.getenv("ENABLE_PINNED_TEXT", "1") == "1"

# ✅ jij had deze op 0 -> blijft default 0
ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "0") == "1"
ENABLE_ACTIVITY = os.getenv("ENABLE_ACTIVITY", "0") == "1"

# ✅ nieuw: alleen verify-topic cleanup (optioneel)
ENABLE_VERIFY_TOPIC_CLEANUP = os.getenv("ENABLE_VERIFY_TOPIC_CLEANUP", "0") == "1"

# ✅ nieuw: daily post pinnen (optioneel)
PIN_DAILY_POST = os.getenv("PIN_DAILY_POST", "0") == "1"

# ✅ optioneel: jouw oude reminder loops apart aan/uit
ENABLE_REMINDER_VERIFY = os.getenv("ENABLE_REMINDER_VERIFY", "0") == "1"
ENABLE_REMINDER_ACTIVITY = os.getenv("ENABLE_REMINDER_ACTIVITY", "0") == "1"

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


def build_share_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Delen", url=SHARE_URL)],
    ])


def build_welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 0/3", url=SHARE_URL)],
        [InlineKeyboardButton("Open group✅", callback_data="open_group")]
    ])


# ✅ uit andere code: dezelfde keyboard naam (alias)
def build_keyboard():
    return build_welcome_keyboard()


# ✅ uit andere code
def unlocked_text(name: str) -> str:
    return f"{name} Successfully unlocked the group✅"


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


# ================== TELEGRAM SEND ==================
def _is_delete_not_found(e: BadRequest) -> bool:
    msg = (str(e) or "").lower()
    return "message to delete not found" in msg or "message can't be deleted" in msg


def _throttled_pause_log(what: str, msg: str):
    now = _time.time()
    last = _LAST_PAUSE_LOG_AT.get(what, 0.0)
    if (now - last) >= _PAUSE_LOG_COOLDOWN:
        _LAST_PAUSE_LOG_AT[what] = now
        logging.warning(msg)


async def safe_send(coro_factory, what: str, max_retries: int = 5):
    global TELEGRAM_PAUSE_UNTIL

    now = _time.time()
    if now < TELEGRAM_PAUSE_UNTIL:
        wait = int(TELEGRAM_PAUSE_UNTIL - now)
        _throttled_pause_log(what, f"{what} skipped, Telegram paused for {wait}s")
        return None

    failures = 0

    for attempt in range(1, max_retries + 1):
        try:
            return await coro_factory()

        except RetryAfter as e:
            sleep_s = e.retry_after + 1
            logging.warning("%s rate limited. Sleep %ss (attempt %s/%s)", what, sleep_s, attempt, max_retries)
            await asyncio.sleep(sleep_s)

        except (TimedOut, NetworkError) as e:
            failures += 1
            backoff = min(30, 2 ** attempt)
            logging.warning("%s transient network error: %s. backoff %ss (attempt %s/%s)", what, e, backoff, attempt, max_retries)
            await asyncio.sleep(backoff)

        except Forbidden as e:
            logging.exception("%s forbidden (rights/bot kicked?): %s", what, e)
            return None

        except BadRequest as e:
            if "delete_message" in what and _is_delete_not_found(e):
                logging.info("%s: delete skipped (not found/already deleted).", what)
                return None
            logging.exception("%s bad request: %s", what, e)
            return None

        except Exception as e:
            logging.exception("%s unexpected error: %s", what, e)
            await asyncio.sleep(2)

    if failures >= 3:
        TELEGRAM_PAUSE_UNTIL = _time.time() + 300
        logging.error("Telegram lijkt onbereikbaar. Pauzeer sends voor 5 minuten.")

    logging.error("%s failed after %s retries - skipping", what, max_retries)
    return None


# ================== DEBUG: LOG ALL UPDATES + /chatid ANYWHERE ==================
async def log_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    chat_id = getattr(chat, "id", None)
    chat_type = getattr(chat, "type", None)
    title = getattr(chat, "title", None)
    text = getattr(msg, "text", None)

    logging.info("UPDATE IN -> chat_id=%s type=%s title=%s text=%r", chat_id, chat_type, title, text)


async def chatid_anywhere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    text = (msg.text or "").strip()
    if not text:
        return

    if not (text == "/chatid" or text.startswith("/chatid@")):
        return

    title = getattr(chat, "title", None)
    out = f"Chat ID: {chat.id}\nType: {chat.type}\nTitle: {title}"
    logging.info("CHATID OUT -> %s", out)

    await safe_send(lambda: context.bot.send_message(chat_id=chat.id, text=out), "send_chatid_anywhere")


# ================== DB ==================
async def db_init():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in je Railway Variables.")

    # ✅ Railway Postgres vereist soms SSL. We proberen eerst ssl=require en vallen terug.
    try:
        DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require")
    except Exception:
        logging.warning("DB connect with ssl=require failed, retry without ssl...")
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

        # ✅ jouw bestaande: alle bot messages (pinned/daily/etc)
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

        # ✅ toegevoegd uit andere code: verify-topic bot messages
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


async def db_is_used(key: str) -> bool:
    cid = current_cycle_date(datetime.now(TZ))
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_names WHERE cycle_id = $1 AND name = $2 LIMIT 1;",
            cid, key
        )
    return row is not None


async def db_mark_used(key: str):
    cid = current_cycle_date(datetime.now(TZ))
    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO used_names(cycle_id, name) VALUES($1, $2) ON CONFLICT DO NOTHING;",
                    cid, key
                )
            return
        except Exception:
            logging.exception("DB mark used failed attempt=%s", attempt + 1)
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
                    BOT_MSG_MAX_ROWS
                )
            return
        except Exception:
            logging.exception("DB track bot message failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)


# ✅ toegevoegd: track verify-topic bot messages (optioneel cleanup)
async def db_track_bot_verify_message_id(message_id: int):
    global BOT_VERIFYMSG_PRUNE_COUNTER

    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO bot_verify_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    int(message_id)
                )

                BOT_VERIFYMSG_PRUNE_COUNTER += 1
                if BOT_VERIFYMSG_PRUNE_COUNTER % BOT_MSG_PRUNE_EVERY != 0:
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
            logging.exception("DB track bot verify message failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)


# ================== SEND HELPERS ==================
async def delete_later(bot, chat_id, message_id, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    await safe_send(lambda: bot.delete_message(chat_id=chat_id, message_id=message_id), "delete_message(later)")


# ✅ aangepast: thread_id support + tracking
async def send_text(bot, chat_id, text, thread_id: Optional[int] = None):
    if thread_id is None:
        msg = await safe_send(lambda: bot.send_message(chat_id=chat_id, text=text), "send_message(main)")
    else:
        msg = await safe_send(
            lambda: bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text),
            f"send_message(thread={thread_id})"
        )

    if msg:
        await db_track_bot_message_id(msg.message_id)
        if thread_id == VERIFY_THREAD_ID:
            await db_track_bot_verify_message_id(msg.message_id)

    return msg


# ✅ aangepast: thread_id + has_spoiler parameter (default False)
async def send_photo(
    bot,
    chat_id,
    photo_path,
    caption,
    reply_markup,
    parse_mode: Opt_
