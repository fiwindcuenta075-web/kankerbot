from telegram.ext import CommandHandler

async def debug_chatid(update, context):
    chat = update.effective_chat
    await update.message.reply_text(f"Chat ID: {chat.id}")

app.add_handler(CommandHandler("chatid", debug_chatid))
