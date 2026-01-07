from telegram.ext import CommandHandler

async def debug_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    logging.info("CHAT DEBUG -> id=%s type=%s title=%s", chat.id, chat.type, getattr(chat, "title", None))
    await update.message.reply_text(f"Chat ID: {chat.id}")

# in main(), na je andere handlers:
app.add_handler(CommandHandler("chatid", debug_chatid))
