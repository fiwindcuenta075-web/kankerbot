# ================== INIT ==================
import sys
logging.info("BOOT_MARKER v2 | file=%s | python=%s", __file__, sys.version)

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


# ================== APP START (PYTHON 3.14 SAFE) ==================
async def amain():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ontbreekt. Zet BOT_TOKEN in je environment variables.")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # ✅ Debug logger: logt alle updates incl. chat_id
    app.add_handler(MessageHandler(filters.ALL, log_updates), group=-1)

    # Track alles
    app.add_handler(MessageHandler(filters.ALL, on_any_message), group=0)

    app.add_handler(CallbackQueryHandler(on_verify, pattern="^verify$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    # ✅ Start zonder run_polling (fix voor Python 3.14)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # blijf runnen
    await asyncio.Event().wait()


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
