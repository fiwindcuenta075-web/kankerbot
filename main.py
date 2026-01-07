import os
import asyncio
import random
import logging
import string
import time as _time
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

# ✅ Let op: alleen cijfers
CHAT_ID = -1003328329377

# ✅ JOUW FOTO NAAM
PHOTO_PATH = "image (6).png"

# ===== Intervals =====
# TEST: 10 uur pinned bericht
PINNED_STATUS_SECONDS = 10 * 60 * 60  # 10 hours (test). Prod: 24*60*60
JOIN_DELAY_SECONDS = 5 * 60          # test. Prod: 5*60*60 (zoals je aangaf)
# (Als je andere loops gebruikt, pas je daar ook de seconds aan.)

# ===== ENABLE FLAGS via Railway Variables =====
ENABLE_PINNED_STATUS = os.getenv("ENABLE_PINNED_STATUS", "1") == "1"

# Let op: ik start geen fake/spam loops in post_init.
# Als je iets als "activity" wilt, maak dat dan legitiem (echte events).

# ===== Telegram circuit breaker =====
TELEGRAM_PAUSE_UNTIL = 0.0  # epoch seconds

# ===== Throttle log spam for "paused" =====
_LAST_PAUSE_LOG_AT: dict[str, float] = {}
_PAUSE_LOG_COOLDOWN = 15.0  # seconds per "what"

# ================== DB GLOBALS ==================
DB_POOL: asyncpg.Pool | None = None
JOINED_NAMES: list[str] = []

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

# Nieuw: pinned status bericht tekst (zoals jij vroeg)
PINNED_STATUS_TEXT = "hallo doei seconde"


def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 0/3", url=SHARE_URL)],
        [InlineKeyboardButton("Open group✅", callback_data="open_group")]
    ])


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
    """
    ✅ FIX: return echte datetime.date (geen string)
    zodat asyncpg een DATE kolom correct kan binden.
    """
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
    """
    ✅ FIX: cycle_id param is date, not str.
    """
    cid = current_cycle_date(datetime.now(TZ))
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM used_names WHERE cycle_id = $1 AND name = $2 LIMIT 1;",
            cid, name
        )
    return row is not None


async def db_mark_used(name: str):
    """
    ✅ FIX: cycle_id param is date, not str.
    """
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
    """
    - Max retries + exponential backoff
    - Circuit breaker: als Telegram onbereikbaar lijkt, pauzeer 5 min.
    - Delete 'message not found' => gewoon skip (geen retries)
    """
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
            backoff = min(30, 2 ** attempt)  # 2,4,8,16,30
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
        TELEGRAM_PAUSE_UNTIL = _time.time() + 300  # 5 min
        logging.error("Telegram lijkt onbereikbaar. Pauzeer sends voor 5 minuten.")

    logging.error("%s failed after %s retries - skipping", what, max_retries)
    return None


async def delete_later(bot, chat_id, message_id, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    await safe_send(lambda: bot.delete_message(chat_id=chat_id, message_id=message_id), "delete_message")


# ================== SEND HELPERS (ALLES IN 1 CHAT — GEEN THREADS) ==================
async def send_text(bot, chat_id, text):
    return await safe_send(lambda: bot.send_message(chat_id=chat_id, text=text), "send_message")


async def send_photo(bot, chat_id, photo_path, caption, reply_markup):
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
            has_spoiler=True
        )

    return await safe_send(_do_send, "send_photo")


# ================== LOOPS ==================
async def reset_loop():
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))
        logging.info("Cycle boundary reached at 05:00")


async def pinned_status_loop(app: Application):
    """
    ✅ Nieuw: elke 10 uur een nieuw bericht ("hallo doei seconde") dat wordt gepind.
    ✅ Als er een nieuw bericht komt, wordt het oude pinned bericht verwijderd.
    """
    last_pinned_msg_id: int | None = None

    while True:
        msg = await send_text(app.bot, CHAT_ID, PINNED_STATUS_TEXT)

        if msg:
            # Pin het nieuwe bericht
            await safe_send(
                lambda: app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id),
                "pin_chat_message(status)"
            )

            # Verwijder het oude pinned bericht (als die er was)
            if last_pinned_msg_id:
                await safe_send(
                    lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=last_pinned_msg_id),
                    "delete_message(old_pinned_status)"
                )

            last_pinned_msg_id = msg.message_id

        await asyncio.sleep(PINNED_STATUS_SECONDS)


# ================== HANDLERS ==================
async def on_open_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "Can’t acces the group, because unfortunately you haven’t shared the group 3 times yet.",
        show_alert=True
    )


async def announce_join_after_delay(context: ContextTypes.DEFAULT_TYPE, name: str):
    await asyncio.sleep(JOIN_DELAY_SECONDS)
    name = (name or "").strip()
    if not name:
        return

    # laat dit intact (zoals je code): voorkom duplicates per cycle
    if await db_is_used(name):
        return

    # Alles in dezelfde chat (geen thread)
    await send_text(context.bot, CHAT_ID, unlocked_text(name))
    await db_mark_used(name)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    if not update.effective_chat or update.effective_chat.id != CHAT_ID:
        return

    for member in update.message.new_chat_members:
        name = (member.full_name or "").strip()
        if name:
            await db_remember_joined_name(name)

            # Welcome photo sturen (niet pinnen)
            await send_photo(context.bot, CHAT_ID, PHOTO_PATH, WELCOME_TEXT, build_keyboard())

            safe_create_task(
                announce_join_after_delay(context, name),
                f"announce_join_after_delay({name})"
            )


# ================== INIT ==================
async def post_init(app: Application):
    me = await app.bot.get_me()
    logging.info("Bot started: @%s", me.username)

    await db_init()
    await db_load_joined_names_into_memory()

    # Startup test
    ok = await safe_send(lambda: app.bot.send_message(chat_id=CHAT_ID, text="✅ bot gestart (startup test)"), "startup_test")
    if ok is None:
        logging.error("Startup test failed - check CHAT_ID, bot rights, Telegram connectivity from Railway.")

    safe_create_task(reset_loop(), "reset_loop")

    # ✅ Nieuw: pinned status loop
    if ENABLE_PINNED_STATUS:
        safe_create_task(pinned_status_loop(app), "pinned_status_loop")
    else:
        logging.info("ENABLE_PINNED_STATUS=0 -> pinned status disabled")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ontbreekt. Zet BOT_TOKEN in je Railway Variables.")
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CallbackQueryHandler(on_open_group, pattern="^open_group$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
