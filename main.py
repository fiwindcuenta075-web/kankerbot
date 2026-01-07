async def amain():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ontbreekt. Zet BOT_TOKEN in je environment variables.")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # ✅ Debug logger (altijd)
    app.add_handler(MessageHandler(filters.ALL, log_updates), group=-1)

    # Track alles
    app.add_handler(MessageHandler(filters.ALL, on_any_message), group=0)

    app.add_handler(CallbackQueryHandler(on_verify, pattern="^verify$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    # ✅ Start app + polling zonder run_polling (fix voor Python 3.14 event loop issue)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # blijf runnen
    await asyncio.Event().wait()


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
