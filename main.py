import telebot
import sqlite3
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI"
ADMIN_ID = 5064426902

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ================= БАЗА =================
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    status TEXT DEFAULT 'pending'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS applications (
    user_id INTEGER,
    section TEXT,
    normal_count INTEGER DEFAULT 0,
    intimate_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending'
)
""")

conn.commit()

# ================= ВСПОМОГАТЕЛЬНЫЕ =================
def get_user(uid):
    cursor.execute("SELECT status FROM users WHERE user_id=?", (uid,))
    return cursor.fetchone()

def set_user(uid, status):
    cursor.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, status))
    conn.commit()

def get_app(uid):
    cursor.execute("SELECT * FROM applications WHERE user_id=?", (uid,))
    return cursor.fetchone()

# ================= START =================
@bot.message_handler(commands=["start"])
def start(msg):
    uid = msg.from_user.id
    if not get_user(uid):
        set_user(uid, "pending")

    status = get_user(uid)[0]

    if status == "banned":
        bot.send_message(uid, "🚫 Вы заблокированы.")
        return

    if status == "approved":
        bot.send_message(uid, "✅ Доступ открыт.")
        return

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📝 Создать анкету", callback_data="create"))
    bot.send_message(uid, "Вы в ожидании одобрения.", reply_markup=kb)

# ================= СОЗДАНИЕ АНКЕТЫ =================
@bot.callback_query_handler(func=lambda c: c.data == "create")
def create(call):
    uid = call.from_user.id
    cursor.execute("DELETE FROM applications WHERE user_id=?", (uid,))
    conn.commit()

    kb = InlineKeyboardMarkup()
    for s in ["Пары", "Будуар", "Гараж"]:
        kb.add(InlineKeyboardButton(s, callback_data=f"sec_{s}"))

    bot.send_message(uid, "Выберите раздел:", reply_markup=kb)

# ================= ВЫБОР РАЗДЕЛА =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("sec_"))
def select(call):
    uid = call.from_user.id
    section = call.data.replace("sec_", "")
    cursor.execute(
        "INSERT INTO applications (user_id, section) VALUES (?,?)",
        (uid, section)
    )
    conn.commit()

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("➕ Обычное", callback_data="normal"),
        InlineKeyboardButton("➕ Интимное", callback_data="intimate"),
    )
    kb.add(InlineKeyboardButton("✅ Отправить админу", callback_data="submit"))

    bot.send_message(uid, f"Анкета: {section}", reply_markup=kb)

# ================= ПРИЁМ МЕДИА =================
@bot.message_handler(content_types=["photo"])
def media(msg):
    uid = msg.from_user.id
    app = get_app(uid)
    if not app:
        return

    if app[3] == "normal":
        cursor.execute("UPDATE applications SET normal_count=normal_count+1 WHERE user_id=?", (uid,))
    elif app[3] == "intimate":
        cursor.execute("UPDATE applications SET intimate_count=intimate_count+1 WHERE user_id=?", (uid,))
    conn.commit()
    bot.send_message(uid, "Файл сохранён.")

# ================= КНОПКИ МЕДИА =================
@bot.callback_query_handler(func=lambda c: c.data in ["normal", "intimate"])
def set_type(call):
    cursor.execute("UPDATE applications SET status=? WHERE user_id=?", (call.data, call.from_user.id))
    conn.commit()
    bot.send_message(call.from_user.id, f"Отправьте {call.data} фото")

# ================= ОТПРАВКА АДМИНУ =================
@bot.callback_query_handler(func=lambda c: c.data == "submit")
def submit(call):
    uid = call.from_user.id
    cursor.execute("SELECT * FROM applications WHERE user_id=?", (uid,))
    app = cursor.fetchone()

    if app[2] < 1 or app[3] < 1:
        bot.send_message(uid, "Нужно минимум 1 обычное и 1 интимное фото.")
        return

    # ЖЁСТКАЯ ДОСТАВКА АДМИНУ
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{uid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{uid}")
    )

    try:
        bot.send_message(
            ADMIN_ID,
            f"📥 Новая анкета\nUser ID: {uid}\nРаздел: {app[1]}",
            reply_markup=kb
        )
    except Exception as e:
        raise RuntimeError(f"НЕ ДОСТАВЛЕНО АДМИНУ: {e}")

    bot.send_message(uid, "✅ Анкета отправлена админу.")

# ================= РЕШЕНИЕ АДМИНА =================
@bot.callback_query_handler(func=lambda c: c.data.startswith(("approve_", "reject_")))
def decision(call):
    if call.from_user.id != ADMIN_ID:
        return

    uid = int(call.data.split("_")[1])

    if call.data.startswith("approve"):
        set_user(uid, "approved")
        bot.send_message(uid, "🎉 Вы одобрены.")
        bot.edit_message_text("✅ Одобрено", call.message.chat.id, call.message.message_id)
    else:
        set_user(uid, "banned")
        bot.send_message(uid, "🚫 Вы отклонены.")
        bot.edit_message_text("❌ Отклонено", call.message.chat.id, call.message.message_id)

# ================= ЗАПУСК =================
bot.infinity_polling()
