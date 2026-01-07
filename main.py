def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ontbreekt. Zet BOT_TOKEN in je environment variables.")

    # ✅ FIX voor Python 3.14+: zorg dat er een event loop bestaat in MainThread
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # ✅ Debug logger (altijd)
    app.add_handler(MessageHandler(filters.ALL, log_updates), group=-1)

    # Track alles
    app.add_handler(MessageHandler(filters.ALL, on_any_message), group=0)

    app.add_handler(CallbackQueryHandler(on_verify, pattern="^verify$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    app.run_polling(drop_pending_updates=True)
