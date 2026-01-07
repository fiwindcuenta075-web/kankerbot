import os
import asyncio
import logging
import time as _time
import json
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo
from io import BytesIO

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

# ✅ tijdelijk None: we lezen de echte groeps-id uit DEBUG_UPDATE logs
CHAT_ID = None

# ✅ JOUW FOTO NAAM
PHOTO_PATH = "image (6).png"

# TEST intervals (seconds) — zet later terug naar echte tijden
DAILY_SECONDS = 17
JOIN_DELAY_SECONDS = 5 * 60
DELETE_DAILY_SECONDS = 17

# ===== Pinned message loop =====
PIN_EVERY_SECONDS = 20
DELETE_PIN_SECONDS = 0
PIN_TEXT = "(15 seconds)"

# ===== DB retention for tracked messages =====
TRACK_RETENTION_DAYS = 3
TRACK_MAX_ROWS = 500000
TRACK_PRUNE_EVERY = 500
TRACK_PRUNE_COUNTER = 0

# ===== ENABLE FLAGS via Railway Variables =====
ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "1") == "1"
ENABLE_PIN = os.getenv("ENABLE_PIN", "1") == "1"
ENABLE_PURGE_AT_5 = os.getenv("ENABLE_PURGE_AT_5", "1") == "1"

# ===== Telegram circuit breaker =====
TELEGRAM_PAUSE_UNTIL = 0.0  # epoch seconds

# ===== Throttle log spam for "paused" =====
_LAST_PAUSE_LOG_AT: dict[str, float] = {}
_PAUSE_LOG_COOLDOWN = 15.0  # seconds per "what"

# ================== DB GLOBALS ==================
DB_POOL: asyncpg.Pool | None = None

# ================== CONTENT ==================
WELCOME_TEXT = (
    "Welcome to the Telegram group 😈\n\n"
    "⚠️ Please verify you're human by clicking the button below.\n"
)

def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Verify ✅", callback_data="verify")]
    ])

def verified_text(name: str) -> str:
    return f"{name} verified ✅"

# ================== DEBUG: LOG RAW UPDATES ==================
async def log_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Logt raw update JSON zodat je de chat.id van de andere groep ziet.
    Zoek in logs naar: DEBUG_UPDATE:
    """
    try:
        j = update.to_dict()
        chat = (j.get("message", {}) or {}).get("chat", {}) or (j.get("channel_post", {}) or {}).get("chat", {})
        chat_id = chat.get("id")
        title = chat.get("title")
        logging.info("DEBUG_UPDATE: chat_id=%s title=%s raw=%s", chat_id, title, json.dumps(j, ensure_ascii=False))
    except Exception:
        logging.exception("DEBUG_UPDATE failed")

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
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in je service environment variables.")

    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with DB_POOL.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at
        ON chat_messages(created_at);
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS used_names (
            cycle_id DATE NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY (cycle_id, name)
        );
        """)

    logging.info("DB initialized ok")

async def db_track_chat_message_id(message_id: int):
    """Track elk message_id zodat we later kunnen deleten."""
    global TRACK_PRUNE_COUNTER
    if not message_id:
        return

    for attempt in range(3):
        try:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    "INSERT INTO chat_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    int(message_id),
                )

                TRACK_PRUNE_COUNTER += 1
                if TRACK_PRUNE_COUNTER % TRACK_PRUNE_EVERY != 0:
                    return

                await conn.execute(
                    f"DELETE FROM chat_messages "
                    f"WHERE created_at < NOW() - INTERVAL '{TRACK_RETENTION_DAYS} days';"
                )

                await conn.execute(
                    """
                    DELETE FROM chat_messages
                    WHERE message_id IN (
                        SELECT message_id
                        FROM chat_messages
                        ORDER BY created_at DESC
                        OFFSET $1
                    );
                    """,
                    TRACK_MAX_ROWS
                )
            return
        except Exception:
            logging.exception("DB track chat message failed attempt=%s", attempt + 1)
            await asyncio.sleep(1 + attempt)

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

# ================== TELEGRAM SEND (MAX RETRIES + CIRCUIT BREAKER) ==================
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

async def delete_later(bot, chat_id, message_id, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    await safe_send(lambda: bot.delete_message(chat_id=chat_id, message_id=message_id), "delete_message")

# ================== SINGLE-CHAT SEND HELPERS (NO THREADS) ==================
async def send_text(bot, chat_id, text):
    if chat_id is None:
        return None
    msg = await safe_send(lambda: bot.send_message(chat_id=chat_id, text=text), "send_message(main)")
    if msg:
        await db_track_chat_message_id(msg.message_id)
    return msg

async def send_photo(bot, chat_id, photo_path, caption, reply_markup):
    if chat_id is None:
        return None

    try:
        with open(photo_path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        logging.error("PHOTO_PATH not found: %s (staat hij echt in je repo root?)", photo_path)
        return None

    if not data:
        logging.error("PHOTO_PATH is empty (0 bytes): %s", photo_path)
        return None

    async def _do_send():
        bio = BytesIO(data)
        bio.name = os.path.basename(photo_path)
        bio.seek(0)
        return await bot.send_photo(
            chat_id=chat_id,
            photo=bio,
            caption=caption,
            reply_markup=reply_markup,
            has_spoiler=True,
        )

    msg = await safe_send(_do_send, "send_photo(main)")
    if msg:
        await db_track_chat_message_id(msg.message_id)
    return msg

# ================== LOOPS ==================
async def reset_loop():
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))
        logging.info("Cycle boundary reached at 05:00")

async def daily_post_loop(app: Application):
    last_msg_id = None
    while True:
        msg = await send_photo(app.bot, CHAT_ID, PHOTO_PATH, WELCOME_TEXT, build_keyboard())

        if last_msg_id:
            safe_create_task(
                delete_later(app.bot, CHAT_ID, last_msg_id, DELETE_DAILY_SECONDS),
                "delete_old_daily"
            )

        if msg:
            last_msg_id = msg.message_id

        await asyncio.sleep(DAILY_SECONDS)

async def pinned_post_loop(app: Application):
    while True:
        msg = await send_text(app.bot, CHAT_ID, PIN_TEXT)

        if msg:
            await safe_send(
                lambda: app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id),
                "pin_chat_message(pinned_loop)"
            )
            safe_create_task(
                delete_later(app.bot, CHAT_ID, msg.message_id, DELETE_PIN_SECONDS),
                "delete_pinned_after_15s"
            )

        await asyncio.sleep(PIN_EVERY_SECONDS)

async def purge_all_messages_at_5_loop(app: Application):
    """
    ✅ Elke dag om 05:00: probeer ALLE getrackte message_ids te verwijderen.
    Let op: alleen voor messages die de bot gezien/getrackt heeft.
    """
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))

        if CHAT_ID is None:
            logging.warning("05:00 purge skipped (CHAT_ID is None / debug mode).")
            continue

        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT message_id FROM chat_messages ORDER BY created_at ASC;")

        ids = [int(r["message_id"]) for r in rows]
        logging.info("05:00 purge starting. tracked_ids=%d", len(ids))

        kept = []
        for mid in ids:
            ok = await safe_send(
                lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=mid),
                "purge_delete_message"
            )
            if ok is None:
                kept.append(mid)

        async with DB_POOL.acquire() as conn:
            await conn.execute("TRUNCATE TABLE chat_messages;")
            if kept:
                await conn.executemany(
                    "INSERT INTO chat_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    [(m,) for m in kept]
                )

        logging.info("05:00 purge done. kept=%d", len(kept))

# ================== HANDLERS ==================
async def on_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return

    user = update.effective_user
    name = (user.full_name if user else "") or "User"

    await q.answer("Verified ✅", show_alert=True)
    await send_text(context.bot, CHAT_ID, verified_text(name))

async def announce_join_after_delay(context: ContextTypes.DEFAULT_TYPE, name: str):
    await asyncio.sleep(JOIN_DELAY_SECONDS)
    name = (name or "").strip()
    if not name:
        return

    if await db_is_used(name):
        return

    await send_text(context.bot, CHAT_ID, f"{name} joined ✅")
    await db_mark_used(name)

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    if CHAT_ID is not None:
        if not update.effective_chat or update.effective_chat.id != CHAT_ID:
            return

    await db_track_chat_message_id(update.message.message_id)

    for member in update.message.new_chat_members:
        name = (member.full_name or "").strip()
        if name:
            safe_create_task(
                announce_join_after_delay(context, name),
                f"announce_join_after_delay({name})"
            )

    if ENABLE_VERIFY:
        await send_photo(context.bot, CHAT_ID, PHOTO_PATH, WELCOME_TEXT, build_keyboard())

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Track ALLE binnenkomende messages.
    In debug mode (CHAT_ID=None) trackt hij alles, maar dat is tijdelijk.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    if CHAT_ID is not None and chat.id != CHAT_ID:
        return

    await db_track_chat_message_id(msg.message_id)

# ================== INIT ==================
async def post_init(app: Application):
    me = await app.bot.get_me()
    logging.info("Bot started: @%s", me.username)

    await db_init()

    if CHAT_ID is None:
        logging.warning("CHAT_ID is None -> debug mode. Stuur een bericht in de groep en lees chat_id uit DEBUG_UPDATE logs.")
    else:
        ok = await safe_send(
            lambda: app.bot.send_message(chat_id=CHAT_ID, text="✅ bot gestart (startup test)"),
            "startup_test"
        )
        if ok is None:
            logging.error("Startup test failed - check CHAT_ID, bot rights, Telegram connectivity.")

    safe_create_task(reset_loop(), "reset_loop")

    if ENABLE_DAILY:
        safe_create_task(daily_post_loop(app), "daily_post_loop")
    else:
        logging.info("ENABLE_DAILY=0 -> daily disabled")

    if ENABLE_PIN:
        safe_create_task(pinned_post_loop(app), "pinned_post_loop")
    else:
        logging.info("ENABLE_PIN=0 -> pinned loop disabled")

    if ENABLE_PURGE_AT_5:
        safe_create_task(purge_all_messages_at_5_loop(app), "purge_all_messages_at_5_loop")
    else:
        logging.info("ENABLE_PURGE_AT_5=0 -> purge disabled")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ontbreekt. Zet BOT_TOKEN in je environment variables.")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # ✅ Debug logger (altijd)
    app.add_handler(MessageHandler(filters.ALL, log_updates), group=-1)

    # Track alles
    app.add_handler(MessageHandler(filters.ALL, on_any_message), group=0)

    app.add_handler(CallbackQueryHandler(on_verify, pattern="^verify$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
