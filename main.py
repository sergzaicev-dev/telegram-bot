import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

logging.basicConfig(level=logging.INFO)

# Этапы
SELECT_SECTION, WAIT_MEDIA, WAIT_APPROVAL = range(3)

# Хранилище состояния
users = {}

ADMIN_ID = 5064426902  # <-- Замените на ваш Telegram ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    users[user_id] = {
        "section": None,
        "media_uploaded": False,
        "approved": False
    }

    keyboard = [["Пары", "Будуар", "Гараж"]]
    await update.message.reply_text(
        "Выберите раздел для анкеты:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    )

    return SELECT_SECTION


async def choose_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    section = update.message.text.strip()

    if section not in ["Пары", "Будуар", "Гараж"]:
        await update.message.reply_text("Выберите один из вариантов.")
        return SELECT_SECTION

    users[user_id]["section"] = section

    await update.message.reply_text(
        f"Раздел выбран: {section}\nТеперь загрузите фото или видео."
    )

    return WAIT_MEDIA


async def receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not (update.message.photo or update.message.video):
        await update.message.reply_text("Нужно фото или видео.")
        return WAIT_MEDIA

    users[user_id]["media_uploaded"] = True

    await update.message.reply_text("Анкета отправлена на модерацию.")

    # Отправка админу
    msg = f"Новая анкета от {user_id}\nРаздел: {users[user_id]['section']}"
    await context.bot.send_message(chat_id=ADMIN_ID, text=msg)

    # Пересылка медиа админу
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        await context.bot.send_photo(chat_id=ADMIN_ID, photo=file_id)
    elif update.message.video:
        file_id = update.message.video.file_id
        await context.bot.send_video(chat_id=ADMIN_ID, video=file_id)

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"Чтобы одобрить: /approve_{user_id}"
    )

    return WAIT_APPROVAL


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if not text.startswith("/approve_"):
        return

    if update.effective_user.id != ADMIN_ID:
        return

    user_id = int(text.replace("/approve_", ""))

    if user_id not in users:
        await update.message.reply_text("Пользователь не найден.")
        return

    users[user_id]["approved"] = True

    await context.bot.send_message(
        chat_id=user_id,
        text="Ваша анкета одобрена. Доступ к разделам открыт."
    )

    await update.message.reply_text(f"Пользователь {user_id} одобрен.")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in users:
        await update.message.reply_text("Введите /start")
        return

    if not users[user_id]["approved"]:
        await update.message.reply_text("Вы ещё не прошли модерацию.")
        return

    await update.message.reply_text("Доступ открыт. Что хотите дальше?")


def main():
    app = ApplicationBuilder().token("8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI").build()

    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("approve_", approve))

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_SECTION: [MessageHandler(filters.TEXT, choose_section)],
            WAIT_MEDIA: [MessageHandler(filters.ALL, receive_media)],
            WAIT_APPROVAL: [
                MessageHandler(filters.COMMAND & filters.Regex("^/approve_"), approve)
            ],
        },
        fallbacks=[]
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.ALL, unknown))

    app.run_polling()


if __name__ == "__main__":
    main()
