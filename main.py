--- a/bot.py
+++ b/bot.py
@@ -84,6 +84,10 @@
 BOT_MSG_PRUNE_EVERY = 200
 BOT_MSG_PRUNE_COUNTER = 0
 
+# ✅ extra counter voor algemene bot-berichten tabel
+BOT_ALLMSG_PRUNE_COUNTER = 0
+
 # ===== ENABLE FLAGS via Railway Variables =====
 ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
 ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "1") == "1"
@@ -140,6 +144,7 @@
 async def db_init():
     global DB_POOL
     if not DATABASE_URL:
         raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in je BOT service variables.")
@@ -175,6 +180,21 @@
         CREATE INDEX IF NOT EXISTS idx_bot_verify_messages_created_at
         ON bot_verify_messages(created_at);
         """)
 
+        # ✅ Nieuw: log alle bot-bericht IDs zodat we ze om 05:00 kunnen verwijderen
+        await conn.execute("""
+        CREATE TABLE IF NOT EXISTS bot_messages (
+            message_id BIGINT PRIMARY KEY,
+            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
+        );
+        """)
+
+        await conn.execute("""
+        CREATE INDEX IF NOT EXISTS idx_bot_messages_created_at
+        ON bot_messages(created_at);
+        """)
+
     logging.info("DB initialized ok")
 
@@ -235,6 +255,52 @@
 async def db_track_bot_verify_message_id(message_id: int):
     global BOT_MSG_PRUNE_COUNTER
 
@@ -302,6 +368,60 @@
             await asyncio.sleep(1 + attempt)
 
+async def db_track_bot_message_id(message_id: int):
+    """
+    ✅ Track ALLE bot-berichten (text/photo etc) voor daily cleanup om 05:00.
+    Zelfde retention/cap mechanisme als verify logging.
+    """
+    global BOT_ALLMSG_PRUNE_COUNTER
+
+    for attempt in range(3):
+        try:
+            async with DB_POOL.acquire() as conn:
+                await conn.execute(
+                    "INSERT INTO bot_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
+                    int(message_id)
+                )
+
+                BOT_ALLMSG_PRUNE_COUNTER += 1
+                if BOT_ALLMSG_PRUNE_COUNTER % BOT_MSG_PRUNE_EVERY != 0:
+                    return
+
+                # retention
+                await conn.execute(
+                    f"DELETE FROM bot_messages "
+                    f"WHERE created_at < NOW() - INTERVAL '{BOT_MSG_RETENTION_DAYS} days';"
+                )
+
+                # cap rows
+                await conn.execute(
+                    """
+                    DELETE FROM bot_messages
+                    WHERE message_id IN (
+                        SELECT message_id
+                        FROM bot_messages
+                        ORDER BY created_at DESC
+                        OFFSET $1
+                    );
+                    """,
+                    BOT_MSG_MAX_ROWS
+                )
+            return
+        except Exception:
+            logging.exception("DB track bot message failed attempt=%s", attempt + 1)
+            await asyncio.sleep(1 + attempt)
+
@@ -216,6 +327,7 @@
 async def send_text(bot, chat_id, thread_id, text):
@@ -241,6 +353,10 @@
     if msg and thread_id == VERIFY_THREAD_ID:
         await db_track_bot_verify_message_id(msg.message_id)
 
+    # ✅ altijd tracken voor 05:00 cleanup
+    if msg:
+        await db_track_bot_message_id(msg.message_id)
+
     return msg
 
@@ -288,6 +404,10 @@
     if msg and thread_id == VERIFY_THREAD_ID:
         await db_track_bot_verify_message_id(msg.message_id)
 
+    # ✅ altijd tracken voor 05:00 cleanup
+    if msg:
+        await db_track_bot_message_id(msg.message_id)
+
     return msg
 
@@ -310,6 +430,64 @@
 async def cleanup_verify_topic_loop(app: Application):
     while True:
@@ -363,6 +541,79 @@
         logging.info("Cleanup verify-topic bot messages at 05:00 done. kept=%d", len(kept))
 
+async def cleanup_all_bot_messages_loop(app: Application):
+    """
+    ✅ Nieuw: verwijder alle door de bot verstuurde berichten (die we tracken in bot_messages)
+    elke dag om 05:00 Amsterdam.
+    """
+    while True:
+        now = datetime.now(TZ)
+        target = datetime.combine(now.date(), RESET_AT, tzinfo=TZ)
+        if now >= target:
+            target = target + timedelta(days=1)
+
+        await asyncio.sleep(max(1, int((target - now).total_seconds())))
+
+        async with DB_POOL.acquire() as conn:
+            rows = await conn.fetch("SELECT message_id FROM bot_messages;")
+
+        ids = [int(r["message_id"]) for r in rows]
+        kept = []
+
+        for mid in ids:
+            ok = await safe_send(
+                lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=mid),
+                "cleanup_delete_message(all)"
+            )
+            # delete_message returns True on success; None on failure/skip
+            if ok is None:
+                kept.append(mid)
+
+        async with DB_POOL.acquire() as conn:
+            await conn.execute("TRUNCATE TABLE bot_messages;")
+            if kept:
+                await conn.executemany(
+                    "INSERT INTO bot_messages(message_id) VALUES($1) ON CONFLICT DO NOTHING;",
+                    [(m,) for m in kept]
+                )
+
+        logging.info("Cleanup ALL bot messages at 05:00 done. kept=%d", len(kept))
+
@@ -466,6 +717,11 @@
     if ENABLE_CLEANUP:
         safe_create_task(cleanup_verify_topic_loop(app), "cleanup_verify_topic_loop")
+        # ✅ Nieuw: daily cleanup van alle bot-berichten om 05:00
+        safe_create_task(cleanup_all_bot_messages_loop(app), "cleanup_all_bot_messages_loop")
     else:
         logging.info("ENABLE_CLEANUP=0 -> cleanup disabled")

--- a/bot.py
+++ b/bot.py
@@ -1,6 +1,6 @@
 import os
 import asyncio
 import random
 import logging
 import string
 import time as _time
@@ -40,6 +40,12 @@
 DAILY_THREAD_ID = None
 VERIFY_THREAD_ID = 4
 
+# ✅ Alles in 1 chat/channel: negeer topics/threads bij send_message/send_photo
+FORCE_SINGLE_CHANNEL = os.getenv("FORCE_SINGLE_CHANNEL", "1") == "1"
+
 # ✅ JOUW FOTO NAAM
 PHOTO_PATH = "image (6).png"
 
@@ -49,6 +55,10 @@
 DAILY_SECONDS = 17
 VERIFY_SECONDS = 30
 JOIN_DELAY_SECONDS = 5 * 60
 ACTIVITY_SECONDS = 25
+
+# ✅ Nieuw: pinned tekstbericht elke 10 uur
+PINNED_TEXT = "hallo doei seconde"
+PINNED_TEXT_SECONDS = 10 * 60 * 60  # 10 uur (test); prod: 24*60*60
 
 DELETE_DAILY_SECONDS = 17
 DELETE_LEAVE_SECONDS = 10000 * 6000
@@ -67,6 +77,7 @@
 ENABLE_DAILY = os.getenv("ENABLE_DAILY", "1") == "1"
 ENABLE_VERIFY = os.getenv("ENABLE_VERIFY", "1") == "1"
 ENABLE_ACTIVITY = os.getenv("ENABLE_ACTIVITY", "1") == "1"
 ENABLE_CLEANUP = os.getenv("ENABLE_CLEANUP", "1") == "1"
+ENABLE_PINNED_TEXT = os.getenv("ENABLE_PINNED_TEXT", "1") == "1"
 
 # ===== Telegram circuit breaker =====
 TELEGRAM_PAUSE_UNTIL = 0.0  # epoch seconds
@@ -216,26 +227,31 @@
 async def send_text(bot, chat_id, thread_id, text):
-    if thread_id is None:
-        return await safe_send(lambda: bot.send_message(chat_id=chat_id, text=text), "send_message(main)")
-
-    msg = await safe_send(
-        lambda: bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text),
-        f"send_message(thread={thread_id})"
-    )
+    effective_thread_id = None if FORCE_SINGLE_CHANNEL else thread_id
+
+    if effective_thread_id is None:
+        msg = await safe_send(
+            lambda: bot.send_message(chat_id=chat_id, text=text),
+            "send_message(main)"
+        )
+    else:
+        msg = await safe_send(
+            lambda: bot.send_message(chat_id=chat_id, message_thread_id=effective_thread_id, text=text),
+            f"send_message(thread={effective_thread_id})"
+        )
 
     if msg and thread_id == VERIFY_THREAD_ID:
         await db_track_bot_verify_message_id(msg.message_id)
 
     return msg
 
 
 # ✅ FIX: send_photo retries sturen nooit een lege file meer
 async def send_photo(bot, chat_id, thread_id, photo_path, caption, reply_markup):
@@ -255,7 +271,9 @@
 
     async def _do_send():
         bio = BytesIO(data)
         bio.name = os.path.basename(photo_path)
         bio.seek(0)
 
+        effective_thread_id = None if FORCE_SINGLE_CHANNEL else thread_id
+
         kwargs = dict(
             chat_id=chat_id,
             photo=bio,
@@ -263,8 +281,8 @@
             reply_markup=reply_markup,
             has_spoiler=True
         )
-        if thread_id is not None:
-            kwargs["message_thread_id"] = thread_id
+        if effective_thread_id is not None:
+            kwargs["message_thread_id"] = effective_thread_id
 
         return await bot.send_photo(**kwargs)
 
@@ -321,6 +347,30 @@
         logging.info("Cleanup verify-topic bot messages at 05:00 done. kept=%d", len(kept))
 
+
+async def pinned_text_loop(app: Application):
+    """
+    ✅ Elke 10 uur: stuur PINNED_TEXT, pin het, en verwijder het vorige pinned bericht.
+    ✅ Wordt altijd in dezelfde chat verstuurd (threads genegeerd via FORCE_SINGLE_CHANNEL).
+    """
+    last_pinned_msg_id = None
+
+    while True:
+        msg = await send_text(app.bot, CHAT_ID, None, PINNED_TEXT)
+
+        if msg:
+            await safe_send(
+                lambda: app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id),
+                "pin_chat_message(pinned_text)"
+            )
+
+            if last_pinned_msg_id:
+                await safe_send(
+                    lambda: app.bot.delete_message(chat_id=CHAT_ID, message_id=last_pinned_msg_id),
+                    "delete_message(old_pinned_text)"
+                )
+
+            last_pinned_msg_id = msg.message_id
+
+        await asyncio.sleep(PINNED_TEXT_SECONDS)
+
 
 async def daily_post_loop(app: Application):
     last_msg_id = None
@@ -337,9 +387,10 @@
         if msg:
             last_msg_id = msg.message_id
-            await safe_send(lambda: app.bot.pin_chat_message(chat_id=CHAT_ID, message_id=msg.message_id), "pin_chat_message")
+            # ✅ Welcome/daily photo message hoeft niet meer gepind te worden (zoals gevraagd)
 
         await asyncio.sleep(DAILY_SECONDS)
@@ -466,6 +517,11 @@
     if ENABLE_ACTIVITY:
         safe_create_task(activity_loop(app), "activity_loop")
     else:
         logging.info("ENABLE_ACTIVITY=0 -> activity disabled")
+
+    if ENABLE_PINNED_TEXT:
+        safe_create_task(pinned_text_loop(app), "pinned_text_loop")
+    else:
+        logging.info("ENABLE_PINNED_TEXT=0 -> pinned text disabled")
