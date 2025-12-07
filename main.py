import os
import telebot
from telebot import apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import threading
from flask import Flask

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("BOT_TOKEN отсутствует")
    exit(1)

ADMIN_IDS = [5064426902]
bot = telebot.TeleBot(BOT_TOKEN)

# === SQLite ===
conn = sqlite3.connect('users.db', check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    section TEXT,
    approved INTEGER DEFAULT 0
)
""")
conn.commit()

# === Клавиатуры ===
def section_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Пары", callback_data="sec_пары"))
    kb.add(InlineKeyboardButton("Будуар", callback_data="sec_будуар"))
    kb.add(InlineKeyboardButton("Гараж", callback_data="sec_гараж"))
    return kb

def mod_kb(user_id):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Одобрить", callback_data=f"app_{user_id}"))
    kb.add(InlineKeyboardButton("Отклонить", callback_data=f"rej_{user_id}"))
    return kb

# === Handlers ===
@bot.message_handler(commands=["start"])
def start(message):
    uid = message.from_user.id

    # Проверяем есть ли пользователь
    c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()

    if not row:
        c.execute("INSERT INTO users (user_id, section, approved) VALUES (?, ?, 0)",
                  (uid, ""))
        conn.commit()

    bot.send_message(message.chat.id, "Выберите раздел:", reply_markup=section_kb())


@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def section(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id
    section_name = call.data.split("_")[1]

    # Обновляем только section — approved не трогаем
    c.execute("UPDATE users SET section=? WHERE user_id=?", (section_name, uid))
    conn.commit()

    try:
        bot.send_message(uid, "Пришлите 1 фото или видео.")
    except apihelper.ApiTelegramException:
        bot.send_message(
            call.message.chat.id,
            "Нажмите /start чтобы бот смог отправлять вам сообщения."
        )


@bot.message_handler(content_types=["photo", "video"])
def media(message):
    uid = message.from_user.id

    c.execute("SELECT section FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()

    if not row or not row[0]:
        bot.send_message(uid, "Сначала выберите раздел.", reply_markup=section_kb())
        return

    for admin in ADMIN_IDS:
        bot.send_message(admin, f"Новая анкета от {uid}")
        bot.forward_message(admin, message.chat.id, message.message_id)
        bot.send_message(admin, "Модерация:", reply_markup=mod_kb(uid))

    bot.send_message(uid, "Анкета отправлена на модерацию.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("app_") or c.data.startswith("rej_"))
def approve(call):
    bot.answer_callback_query(call.id)
    if call.from_user.id not in ADMIN_IDS:
        return

    action, uid = call.data.split("_")
    uid = int(uid)

    if action == "app":
        c.execute("UPDATE users SET approved=1 WHERE user_id=?", (uid,))
        conn.commit()
        bot.send_message(uid, "Анкета одобрена!")
    else:
        bot.send_message(uid, "Анкета отклонена.")


# === Flask для Render ===
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", 8000))
    app.run(host='0.0.0.0', port=port)

# === Запуск ===
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.infinity_polling()
