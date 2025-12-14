import telebot
import sqlite3
import logging
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import Optional, Tuple

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI"
ADMIN_ID = 5064426902

# Настройка логирования для отслеживания ошибок
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ================= БАЗА ДАННЫХ =================
def init_db():
    """Инициализация базы данных с правильной структурой"""
    conn = sqlite3.connect("users.db", check_same_thread=False)
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        section TEXT,
        normal_count INTEGER DEFAULT 0,
        intimate_count INTEGER DEFAULT 0,
        photo_type TEXT DEFAULT NULL,  -- 'normal' или 'intimate'
        app_status TEXT DEFAULT 'pending',  -- статус заявки
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    """)
    
    # Создание индексов для ускорения запросов
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_applications_user_id ON applications (user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_applications_status ON applications (app_status)")
    
    conn.commit()
    return conn, cursor

conn, cursor = init_db()

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def get_user_status(user_id: int) -> Optional[str]:
    """Получить статус пользователя"""
    cursor.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else None

def set_user_status(user_id: int, status: str) -> None:
    """Установить статус пользователя"""
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, status) VALUES (?, ?)",
        (user_id, status)
    )
    conn.commit()

def get_application(user_id: int) -> Optional[Tuple]:
    """Получить активную заявку пользователя"""
    cursor.execute("""
        SELECT section, normal_count, intimate_count, photo_type, app_status 
        FROM applications 
        WHERE user_id = ? AND app_status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    return cursor.fetchone()

def create_application(user_id: int, section: str) -> None:
    """Создать новую заявку"""
    cursor.execute("""
        INSERT INTO applications (user_id, section, app_status) 
        VALUES (?, ?, 'pending')
    """, (user_id, section))
    conn.commit()

def update_photo_count(user_id: int, photo_type: str) -> bool:
    """Обновить счетчик фотографий в зависимости от типа"""
    if photo_type == "normal":
        cursor.execute("""
            UPDATE applications 
            SET normal_count = normal_count + 1 
            WHERE user_id = ? AND app_status = 'pending'
        """, (user_id,))
    elif photo_type == "intimate":
        cursor.execute("""
            UPDATE applications 
            SET intimate_count = intimate_count + 1 
            WHERE user_id = ? AND app_status = 'pending'
        """, (user_id,))
    else:
        return False
    
    conn.commit()
    return cursor.rowcount > 0

def set_photo_type(user_id: int, photo_type: str) -> bool:
    """Установить тип фотографий для текущей заявки"""
    cursor.execute("""
        UPDATE applications 
        SET photo_type = ? 
        WHERE user_id = ? AND app_status = 'pending'
    """, (photo_type, user_id))
    conn.commit()
    return cursor.rowcount > 0

def get_photo_type(user_id: int) -> Optional[str]:
    """Получить текущий тип фотографий для заявки"""
    cursor.execute("""
        SELECT photo_type FROM applications 
        WHERE user_id = ? AND app_status = 'pending'
    """, (user_id,))
    result = cursor.fetchone()
    return result[0] if result else None

# ================= ОБРАБОТЧИКИ КОМАНД =================
@bot.message_handler(commands=["start"])
def handle_start(message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    # Регистрируем пользователя, если его нет
    if get_user_status(user_id) is None:
        set_user_status(user_id, "pending")
        logger.info(f"Зарегистрирован новый пользователь: {user_id}")
    
    status = get_user_status(user_id)
    
    if status == "banned":
        bot.send_message(user_id, "🚫 Вы заблокированы и не можете пользоваться ботом.")
        return
    
    if status == "approved":
        bot.send_message(user_id, "✅ Ваш доступ уже активирован.")
        return
    
    # Пользователь в ожидании
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📝 Создать анкету", callback_data="create_app"))
    
    bot.send_message(
        user_id,
        "👋 Добро пожаловать!\n"
        "Ваш статус: *Ожидание одобрения*\n\n"
        "Для подачи заявки нажмите кнопку ниже:",
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data == "create_app")
def handle_create_application(call):
    """Создание новой анкеты"""
    user_id = call.from_user.id
    
    # Проверяем, нет ли уже активной заявки
    if get_application(user_id):
        bot.send_message(user_id, "⚠️ У вас уже есть активная заявка.")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    sections = ["Пары", "Будуар", "Гараж"]
    for section in sections:
        keyboard.add(InlineKeyboardButton(section, callback_data=f"section_{section}"))
    
    bot.edit_message_text(
        "📋 *Создание анкеты*\n\n"
        "Выберите раздел для вашей анкеты:",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("section_"))
def handle_section_selection(call):
    """Обработка выбора раздела"""
    user_id = call.from_user.id
    section = call.data.replace("section_", "")
    
    # Создаем новую заявку
    create_application(user_id, section)
    
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("➕ Обычные фото", callback_data="type_normal"),
        InlineKeyboardButton("➕ Интимные фото", callback_data="type_intimate")
    )
    keyboard.add(InlineKeyboardButton("✅ Отправить на проверку", callback_data="submit_app"))
    
    bot.edit_message_text(
        f"📂 *Раздел:* {section}\n\n"
        "Теперь добавьте фотографии:\n"
        "1. Выберите тип фото (обычные/интимные)\n"
        "2. Отправьте фото соответствующего типа\n"
        "3. Повторите для другого типа\n\n"
        "*Требование:* минимум 1 обычное и 1 интимное фото",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("type_"))
def handle_photo_type_selection(call):
    """Установка типа фотографий"""
    user_id = call.from_user.id
    photo_type = call.data.replace("type_", "")  # "normal" или "intimate"
    
    if not set_photo_type(user_id, photo_type):
        bot.answer_callback_query(call.id, "❌ Сначала создайте анкету!")
        return
    
    type_name = "обычные" if photo_type == "normal" else "интимные"
    bot.answer_callback_query(call.id, f"✅ Теперь отправляйте {type_name} фото")
    
    bot.send_message(
        user_id,
        f"📸 Теперь отправляйте *{type_name} фото*.\n"
        "Каждое отправленное фото будет автоматически добавлено к вашей анкете."
    )

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    """Обработка фотографий"""
    user_id = message.from_user.id
    
    # Получаем текущий тип фото для заявки
    photo_type = get_photo_type(user_id)
    
    if not photo_type:
        bot.send_message(user_id, "⚠️ Сначала выберите тип фото (обычные/интимные)")
        return
    
    # Обновляем счетчик
    if update_photo_count(user_id, photo_type):
        type_name = "обычных" if photo_type == "normal" else "интимных"
        
        # Получаем текущие счетчики
        app = get_application(user_id)
        if app:
            normal_count, intimate_count = app[1], app[2]
            
            bot.send_message(
                user_id,
                f"✅ Фото сохранено как {type_name}.\n\n"
                f"📊 *Текущий прогресс:*\n"
                f"• Обычные фото: {normal_count}\n"
                f"• Интимные фото: {intimate_count}"
            )
    else:
        bot.send_message(user_id, "❌ Не удалось сохранить фото. У вас есть активная заявка?")

@bot.callback_query_handler(func=lambda call: call.data == "submit_app")
def handle_submission(call):
    """Отправка заявки на проверку"""
    user_id = call.from_user.id
    
    # Проверяем заявку
    app = get_application(user_id)
    if not app:
        bot.answer_callback_query(call.id, "❌ У вас нет активной заявки!")
        return
    
    section, normal_count, intimate_count, photo_type, status = app
    
    # Проверяем минимальные требования
    if normal_count < 1 or intimate_count < 1:
        bot.answer_callback_query(
            call.id,
            f"❌ Нужно минимум 1 обычное и 1 интимное фото (сейчас: {normal_count}/{intimate_count})"
        )
        return
    
    # Отправляем админу
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{user_id}")
    )
    
    try:
        bot.send_message(
            ADMIN_ID,
            f"📨 *Новая заявка*\n\n"
            f"👤 *Пользователь:* {user_id}\n"
            f"📂 *Раздел:* {section}\n"
            f"📷 *Обычных фото:* {normal_count}\n"
            f"🔞 *Интимных фото:* {intimate_count}\n\n"
            f"🕒 Время: {call.message.date}",
            reply_markup=keyboard
        )
        
        # Обновляем статус заявки
        cursor.execute("""
            UPDATE applications 
            SET app_status = 'submitted' 
            WHERE user_id = ? AND app_status = 'pending'
        """, (user_id,))
        conn.commit()
        
        bot.edit_message_text(
            "✅ *Заявка отправлена на проверку!*\n\n"
            "Администратор получил вашу анкету и скоро примет решение.\n"
            "Ожидайте уведомления.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        
        logger.info(f"Заявка отправлена: пользователь {user_id}, раздел {section}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки админу: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка отправки. Попробуйте позже.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def handle_admin_decision(call):
    """Обработка решения администратора"""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Только администратор может это делать!")
        return
    
    action, user_id_str = call.data.split("_")[1], call.data.split("_")[2]
    user_id = int(user_id_str)
    
    # Получаем информацию о заявке
    cursor.execute("""
        SELECT section FROM applications 
        WHERE user_id = ? AND app_status = 'submitted'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    result = cursor.fetchone()
    
    if not result:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена!")
        return
    
    section = result[0]
    
    if action == "approve":
        # Одобряем пользователя
        set_user_status(user_id, "approved")
        
        # Обновляем статус заявки
        cursor.execute("""
            UPDATE applications 
            SET app_status = 'approved' 
            WHERE user_id = ? AND app_status = 'submitted'
        """, (user_id,))
        conn.commit()
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                "🎉 *Поздравляем!*\n\n"
                "Ваша заявка *одобрена* администратором.\n"
                f"Раздел: {section}\n\n"
                "Теперь вам доступен полный функционал бота."
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        bot.edit_message_text(
            f"✅ Заявка {user_id} одобрена\nРаздел: {section}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        
        logger.info(f"Заявка одобрена: пользователь {user_id}")
        
    elif action == "reject":
        # Отклоняем пользователя
        set_user_status(user_id, "banned")
        
        # Обновляем статус заявки
        cursor.execute("""
            UPDATE applications 
            SET app_status = 'rejected' 
            WHERE user_id = ? AND app_status = 'submitted'
        """, (user_id,))
        conn.commit()
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                "🚫 *Заявка отклонена*\n\n"
                "Администратор отклонил вашу заявку.\n"
                f"Раздел: {section}\n\n"
                "Вы больше не можете пользоваться ботом."
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        bot.edit_message_text(
            f"❌ Заявка {user_id} отклонена\nРаздел: {section}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
        
        logger.info(f"Заявка отклонена: пользователь {user_id}")

# ================= ЗАПУСК БОТА =================
def main():
    """Основная функция запуска бота"""
    logger.info("Бот запускается...")
    
    try:
        # Проверка подключения к базе данных
        cursor.execute("SELECT 1")
        logger.info("База данных подключена успешно")
        
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
        
    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных: {e}")
    except telebot.apihelper.ApiTelegramException as e:
        if "409" in str(e):
            logger.error("Конфликт: уже запущен другой экземпляр бота!")
            print("⚠️  ОШИБКА 409: Убедитесь, что бот запущен только в одном месте!")
        else:
            logger.error(f"Ошибка Telegram API: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
    finally:
        conn.close()
        logger.info("Бот остановлен")

if __name__ == "__main__":
    # Важно: проверяем, что бот запускается только один раз
    print("Запуск Telegram бота...")
    main()
