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

# ✅ GROEP ID (vast)
CHAT_ID = -1003418364423

# ✅ JOUW FOTO NAAM
PHOTO_PATH = "image (6).png"

# TEST intervals (seconds)
DAILY_SECONDS = 17
VERIFY_SECONDS = 30
JOIN_DELAY_SECONDS = 5 * 60
ACTIVITY_SECONDS = 25

DELETE_DAILY_SECONDS = 17
DELETE_LEAVE_SECONDS = 10000 * 6000

# ===== retention/cap logging =====
BOT_MSG_RETENTION_DAYS = 2
BOT_MSG_MAX_ROWS = 20000
BOT_MSG_PRUNE_EVERY = 200
BOT_ALLMSG_PRUNE_COUNTER = 0

# ===== ENABLE FLAGS via Railway Variables =====
ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "1") == "1"
ENABLE_ACTIVITY = os.getenv("ENABLE_ACTIVITY", "1") == "1"
ENABLE_CLEANUP = os.getenv("ENABLE_CLEANUP", "1") == "1"

# ===== pinned caption loop =====
ENABLE_PINNED_TEXT = os.getenv("ENABLE_PINNED_TEXT", "1") == "1"
PINNED_TEXT_SECONDS = int(os.getenv("PINNED_TEXT_SECONDS", "100"))  # test. normaal: 10*60*60 of 24*60*60
PINNED_BANNER_PATH = os.getenv("PINNED_BANNER_PATH", "IMG_1211.jpg")

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

# pinned/share config
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/+D8FCvP2JTYVlZTZk")
GATEWAY_LINK = os.getenv("GATEWAY_LINK", "https://t.me/pareltjesGW")

CAPTION = """
✨ <b>Pareltjes – Community & Handel</b> ✨

<b>Gateway (link):</b> 🔥
https://t.me/pareltjesGW

🔔 <b>Tip:</b> Deel naar 3 mensen voor instant accept 0/3 ✅👇
""".strip()

SHARE_TEXT = (
    "✨ Pareltjes – Community & Handel ✨\n\n"
    "Gateway (link): 🔥\n"
    "https://t.me/pareltjesGW\n\n"
)

SHARE_URL_PINNED = (
    "https://t.me/share/url?"
    + "url=" + urllib.parse.quote(GROUP_LINK)
    + "&text=" + urllib.parse.quote(SHARE_TEXT)
)


def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 0/3", url=SHARE_URL)],
        [InlineKeyboardButton("Open group✅", callback_data="open_group")]
    ])


def build_share_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Delen", url=SHARE_URL_PINNED)],
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
    local = now.astimezone(TZ)
    if local.time() < RESET_AT:
        return local.date() - timedelta(days=1)
    return local.date()


# ================== DB ==================
async def db_init():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in je Railway Variables.")

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


# ================== TELEGRAM SEND ==================
# ✅ FIX: accepteer elke Exception (soms komt delete-not-found binnen als NetworkError)
def _is_delete_not_found(e: Exception) -> bool:
    msg = (str(e) or "").lower()
    return "message to delete not found" in msg or "message can't be deleted" in msg


def _throttled_pause_log(what: str, msg: str):
    now = _time.time()
    last = _LAST_PAUSE_LOG_AT.get(what, 0.0)
    if (now - last) >= _PAUSE_LOG_COOLDOWN:
        _LAST_PAUSE_LOG_AT[what] = now
        logging.warning(msg)


# ===== Sentinel for delete-not-found success =====
_DELETE_SKIPPED_OK = object()


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
            # ✅ FIX: delete "not found" is normaal -> NIET retryen / NIET circuit-breaken
            if "delete_message" in what and _is_delete_not_found(e):
                logging.info("%s: delete skipped (not found/already deleted).", what)
                return _DELETE_SKIPPED_OK

            if "chat not found" in str(e).lower():
                logging.error("%s failed: Chat not found (check bot is in the group + CHAT_ID)", what)
                return None

            failures += 1
            backoff = min(30, 2 ** attempt)
            logging.warning(
                "%s transient network error: %s. backoff %ss (attempt %s/%s)",
                what, e, backoff, attempt, max_retries
            )
            await asyncio.sleep(backoff)

        except Forbidden as e:
            logging.exception("%s forbidden (rights/bot kicked?): %s", what, e)
            return None

        except BadRequest as e:
            if "delete_message" in what and _is_delete_not_found(e):
                logging.info("%s: delete skipped (not found/already deleted).", what)
                return _DELETE_SKIPPED_OK

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


async def send_text(bot, chat_id, text):
    msg = await safe_send(lambda: bot.send_message(chat_id=chat_id, text=text), "send_message")
    if msg:
        await db_track_bot_message_id(msg.message_id)
    return msg


async def send_photo(
    bot,
    chat_id,
    photo_path,
    caption,
    reply_markup,
    parse_mode: Optional[str] = None,
    has_spoiler: bool = True,
):
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

        kwargs = dict(
            chat_id=chat_id,
            photo=bio,
            caption=caption,
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
        )
        if parse_mode:
            kwargs["parse_mode"] = parse_mode

        return await bot.send_photo(**kwargs)

    msg = await safe_send(_do_send, "send_photo")
    if msg:
        await db_track_bot_message_id(msg.message_id)
    return msg


# ================== HANDLERS ==================
async def on_open_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "Can’t acces the group, because unfortunately you haven’t shared the group 3 times yet.",
        show_alert=True
    )


# ✅ delete the "pinned a photo" service message immediately
async def on_pinned_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.id != CHAT_ID:
        return
    if not update.message:
        return

    service_mid = update.message.message_id
    await safe_send(
        lambda: context.bot.delete_message(chat_id=CHAT_ID, message_id=service_mid),
        "delete_message(pinned_service)"
    )


async def announce_join_after_delay(context: ContextTypes.DEFAULT_TYPE, name: str):
    await asyncio.sleep(JOIN_DELAY_SECONDS)
    name = (name or "").strip()
    if not name:
        return

    if await db_is_used(name):
        return

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
            safe_create_task(announce_join_after_delay(context, name), "announce_join_after_delay")


# ================== LOOPS ==================
async def reset_loop():
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))
        logging.info("Cycle boundary reached at 05:00")


async def cleanup_all_bot_messages_loop(app: Application):
    while True:
        now = datetime.now(TZ)
        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
        if now >= target:
            target = target + timedelta(days=1)

        await asyncio.sleep(max(1, int((target - now).total_seconds())))

        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT message_id FROM bot_messages;")

        ids = [int(r["message_id"]) for r in rows]
        kept = []

        for mid in ids:
            ok = await safe_send(
                lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=mid),
                "delete_message(cleanup_all)"
            )
            # _DELETE_SKIPPED_OK counts as success, so don't keep it
            if ok is None:
                kept.append(mid)

        async with DB_POOL.acquire() as conn:
            await conn.execute("TRUNCATE TABLE bot_messages;")
            if kept:
                await conn.executemany(
                    "INSERT INTO bot_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
                    [(m,) for m in kept]
                )

        logging.info("Cleanup ALL bot messages at 05:00 done. kept=%d", len(kept))


async def pinned_caption_loop(app: Application):
    last_pinned_msg_id = None
    while True:
        msg = await send_photo(
            app.bot,
            CHAT_ID,
            PINNED_BANNER_PATH,
            CAPTION,
            build_share_keyboard(),
            parse_mode="HTML",
            has_spoiler=False,
        )

        if msg:
            await safe_send(
                lambda: app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id),
                "pin_chat_message(pinned_caption)"
            )

            if last_pinned_msg_id:
                await safe_send(
                    lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=last_pinned_msg_id),
                    "delete_message(old_pinned_caption)"
                )

            last_pinned_msg_id = msg.message_id

        await asyncio.sleep(PINNED_TEXT_SECONDS)


async def daily_post_loop(app: Application):
    last_msg_id = None

    while True:
        msg = await send_photo(
            app.bot,
            CHAT_ID,
            PHOTO_PATH,
            WELCOME_TEXT,
            build_keyboard(),
            parse_mode=None,
            has_spoiler=True,
        )

        if last_msg_id:
            safe_create_task(delete_later(app.bot, CHAT_ID, last_msg_id, DELETE_DAILY_SECONDS), "delete_old_daily")

        if msg:
            last_msg_id = msg.message_id
            # ✅ DAILY WORDT NIET GEPINNED (alleen Pareltjes caption loop)

        await asyncio.sleep(DAILY_SECONDS)


async def verify_random_joiner_loop(app: Application):
    while True:
        if JOINED_NAMES:
            name = random.choice([n for n in JOINED_NAMES if n])
            if name and (not await db_is_used(name)):
                await send_text(app.bot, CHAT_ID, unlocked_text(name))
                await db_mark_used(name)

        await asyncio.sleep(VERIFY_SECONDS)


NL_CITY_CODES = ["010","020","030","040","050","070","073","076","079","071","072","074","075","078"]
SEPARATORS = ["_", ".", "-"]
PREFIXES = ["x", "mr", "its", "real", "official", "the", "iam", "nl", "dm", "vip", "urban", "city", "only"]
EMOJIS = ["🔥", "💎", "👻", "⚡", "🚀", "✅"]

def _name_fragments_from_joined():
    frags = []
    for n in JOINED_NAMES:
        n = (n or "").strip().lower()
        n = "".join(ch for ch in n if ch.isalpha())
        if len(n) >= 4:
            for _ in range(2):
                start = random.randint(0, max(0, len(n) - 3))
                frag_len = random.randint(2, 4)
                frag = n[start:start+frag_len]
                if frag and frag not in frags:
                    frags.append(frag)
    return frags

def random_alias_from_joined():
    frags = _name_fragments_from_joined()
    if not frags:
        frags = ["nova", "sky", "dex", "luna", "vex", "rio", "mira", "zen"]

    code = random.choice(NL_CITY_CODES)
    sep = random.choice(SEPARATORS)
    prefix = random.choice(PREFIXES)
    frag1 = random.choice(frags)
    frag2 = random.choice(frags)
    while frag2 == frag1:
        frag2 = random.choice(frags)

    digits = "".join(random.choices(string.digits, k=random.randint(2, 4)))

    style = random.choice([
        "prefix_frag_digits",
        "frag_code_digits",
        "frag_frag_digits",
        "prefix_frag_code",
        "prefix_frag_sep_frag_digits",
        "frag_digits_emoji",
    ])

    if style == "prefix_frag_digits":
        base = f"{prefix}{sep}{frag1}{digits}"
    elif style == "frag_code_digits":
        base = f"{frag1}{sep}{code}{sep}{digits}"
    elif style == "frag_frag_digits":
        base = f"{frag1}{sep}{frag2}{digits}"
    elif style == "prefix_frag_code":
        base = f"{prefix}{sep}{frag1}{sep}{code}"
    elif style == "frag_digits_emoji":
        base = f"{frag1}{digits}{random.choice(EMOJIS)}"
    else:
        base = f"{prefix}{sep}{frag1}{sep}{frag2}{sep}{digits}"

    base = base.strip()
    if not base or base.isdigit():
        base = f"user{sep}{digits}"
    return base


async def activity_loop(app: Application):
    while True:
        alias = random_alias_from_joined()
        text = f"{alias} Successfully unlocked the group✅"

        msg = await send_text(app.bot, CHAT_ID, text)

        if msg:
            safe_create_task(delete_later(app.bot, CHAT_ID, msg.message_id, DELETE_LEAVE_SECONDS), "delete_activity_msg")

        await asyncio.sleep(ACTIVITY_SECONDS)


# ================== INIT ==================
async def post_init(app: Application):
    me = await app.bot.get_me()
    logging.info("Bot started: @%s", me.username)

    await db_init()
    await db_load_joined_names_into_memory()

    ok = await safe_send(lambda: app.bot.send_message(chat_id=CHAT_ID, text="✅ bot gestart (startup test)"), "startup_test")
    if ok is None:
        logging.error("Startup test failed - check bot is in the group + rights + CHAT_ID.")

    safe_create_task(reset_loop(), "reset_loop")

    if ENABLE_CLEANUP:
        safe_create_task(cleanup_all_bot_messages_loop(app), "cleanup_all_bot_messages_loop")
    else:
        logging.info("ENABLE_CLEANUP=0 -> cleanup disabled")

    if ENABLE_PINNED_TEXT:
        safe_create_task(pinned_caption_loop(app), "pinned_caption_loop")
    else:
        logging.info("ENABLE_PINNED_TEXT=0 -> pinned caption disabled")

    if ENABLE_DAILY:
        safe_create_task(daily_post_loop(app), "daily_post_loop")
    else:
        logging.info("ENABLE_DAILY=0 -> daily disabled")

    if ENABLE_VERIFY:
        safe_create_task(verify_random_joiner_loop(app), "verify_random_joiner_loop")
    else:
        logging.info("ENABLE_VERIFY=0 -> verify disabled")

    if ENABLE_ACTIVITY:
        safe_create_task(activity_loop(app), "activity_loop")
    else:
        logging.info("ENABLE_ACTIVITY=0 -> activity disabled")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ontbreekt. Zet BOT_TOKEN in je Railway Variables.")
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CallbackQueryHandler(on_open_group, pattern="^open_group$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    # ✅ delete "pinned a photo" service messages
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, on_pinned_service_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
