import os
import telebot
from telebot import apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import threading
import logging
from flask import Flask
import signal
import sys
from datetime import datetime

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("❌ ОШИБКА: BOT_TOKEN не задан. Добавьте переменную окружения в Render.")
    sys.exit(1)

ADMIN_IDS = [5064426902]  # Замените на свой ID
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# --- Потокобезопасная работа с базой данных ---
class DatabaseManager:
    def __init__(self, db_path='users.db'):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Инициализация базы данных"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    section TEXT,
                    approved INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()
    
    def execute(self, query, params=()):
        """Безопасное выполнение запроса"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                conn.commit()
                result = cursor.lastrowid
            except Exception as e:
                logger.error(f"Ошибка БД: {e}")
                result = None
            finally:
                conn.close()
            return result
    
    def fetchone(self, query, params=()):
        """Безопасное получение одной записи"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                result = cursor.fetchone()
            except Exception as e:
                logger.error(f"Ошибка БД: {e}")
                result = None
            finally:
                conn.close()
            return result
    
    def fetchall(self, query, params=()):
        """Безопасное получение всех записей"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                result = cursor.fetchall()
            except Exception as e:
                logger.error(f"Ошибка БД: {e}")
                result = []
            finally:
                conn.close()
            return result

# Инициализация менеджера БД
db = DatabaseManager()

# --- Клавиатуры ---
def section_kb():
    """Клавиатура выбора раздела"""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Пары", callback_data="sec_пары"),
        InlineKeyboardButton("Будуар", callback_data="sec_будуар"),
        InlineKeyboardButton("Гараж", callback_data="sec_гараж")
    )
    return kb

def mod_kb(user_id):
    """Клавиатура модерации для админов"""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"app_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{user_id}")
    )
    return kb

# --- Хендлеры бота ---
@bot.message_handler(commands=["start", "help"])
def start(message):
    """Обработка команды /start"""
    welcome_text = (
        "👋 Привет! Я бот для отправки контента.\n\n"
        "📋 **Как пользоваться:**\n"
        "1. Нажмите кнопку ниже и выберите раздел\n"
        "2. Отправьте фото или видео\n"
        "3. Дождитесь модерации\n\n"
        "Ваш контент увидят только после проверки администратором."
    )
    
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=section_kb()
    )
    logger.info(f"Пользователь {message.from_user.id} начал диалог")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def section_handler(call):
    """Обработка выбора раздела"""
    try:
        bot.answer_callback_query(call.id, "Раздел выбран!")
        
        if not call.data or "_" not in call.data:
            bot.send_message(call.message.chat.id, "❌ Ошибка выбора раздела")
            return
            
        section_name = call.data.split("_", 1)[1]
        uid = call.from_user.id
        
        # Проверяем валидность раздела
        valid_sections = ["пары", "будуар", "гараж"]
        if section_name.lower() not in valid_sections:
            bot.send_message(call.message.chat.id, "❌ Неверный раздел")
            return
        
        # Сохраняем в БД
        db.execute(
            "INSERT OR REPLACE INTO users (user_id, section, approved) VALUES (?, ?, 0)",
            (uid, section_name)
        )
        
        logger.info(f"Пользователь {uid} выбрал раздел: {section_name}")
        
        # Пытаемся отправить сообщение в личку
        try:
            bot.send_message(
                uid,
                f"✅ Вы выбрали раздел: **{section_name}**\n\n"
                "📸 Теперь отправьте фото или видео для модерации.",
                parse_mode="Markdown"
            )
        except apihelper.ApiTelegramException as e:
            if e.error_code == 403 and "blocked" in e.description.lower():
                error_msg = "❌ Бот заблокирован. Разблокируйте бота, чтобы продолжить."
            elif e.error_code == 403 and "can't initiate conversation" in e.description:
                error_msg = (
                    "⚠️ **Внимание!**\n\n"
                    "Бот не может написать вам первым сообщением.\n"
                    "1. Нажмите кнопку 'Начать' (@username бота)\n"
                    "2. Или отправьте /start прямо мне"
                )
            else:
                error_msg = "⚠️ Ошибка отправки сообщения"
            
            bot.send_message(
                call.message.chat.id,
                error_msg,
                reply_markup=section_kb()
            )
            logger.warning(f"Не удалось отправить сообщение пользователю {uid}: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка в section_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка", show_alert=True)

@bot.message_handler(content_types=["photo", "video", "animation"])
def media_handler(message):
    """Обработка медиафайлов"""
    try:
        uid = message.from_user.id
        
        # Проверяем, выбрал ли пользователь раздел
        user_data = db.fetchone(
            "SELECT section, approved FROM users WHERE user_id = ?",
            (uid,)
        )
        
        if not user_data:
            bot.reply_to(
                message,
                "❌ Сначала выберите раздел!",
                reply_markup=section_kb()
            )
            return
        
        section_name, approved = user_data
        
        # Проверяем, не забанен ли пользователь
        if approved == -1:
            bot.reply_to(message, "❌ Вы заблокированы и не можете отправлять контент.")
            return
        
        logger.info(f"Медиа от пользователя {uid}, раздел: {section_name}")
        
        # Отправляем админам
        for admin_id in ADMIN_IDS:
            try:
                # Отправляем информацию о пользователе
                user_info = (
                    f"📨 **Новая анкета на модерацию**\n"
                    f"👤 ID: `{uid}`\n"
                    f"📂 Раздел: {section_name}\n"
                    f"🕒 Время: {datetime.now().strftime('%H:%M:%S')}"
                )
                
                bot.send_message(admin_id, user_info, parse_mode="Markdown")
                
                # Пересылаем медиа
                bot.forward_message(admin_id, message.chat.id, message.message_id)
                
                # Клавиатура модерации
                bot.send_message(admin_id, "📋 Модерация:", reply_markup=mod_kb(uid))
                
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")
        
        # Подтверждение пользователю
        bot.reply_to(
            message,
            "✅ Ваша анкета отправлена на модерацию.\n"
            "Ожидайте решения администратора."
        )
        
    except Exception as e:
        logger.error(f"Ошибка в media_handler: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при обработке медиа")

@bot.callback_query_handler(func=lambda call: call.data.startswith(("app_", "rej_")))
def moderation_handler(call):
    """Обработка модерации"""
    try:
        # Проверяем права администратора
        if call.from_user.id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ У вас нет прав!", show_alert=True)
            return
        
        bot.answer_callback_query(call.id, "Решение принято!")
        
        # Разбираем callback data
        parts = call.data.split("_")
        if len(parts) != 2:
            return
        
        action, uid_str = parts
        uid = int(uid_str)
        
        # Обновляем статус в БД
        if action == "app":
            db.execute(
                "UPDATE users SET approved = 1 WHERE user_id = ?",
                (uid,)
            )
            status_text = "✅ Одобрена"
            user_message = (
                "🎉 **Ваша анкета одобрена!**\n\n"
                "Теперь ваш контент будет доступен другим пользователям."
            )
        else:  # rej
            db.execute(
                "UPDATE users SET approved = -1 WHERE user_id = ?",
                (uid,)
            )
            status_text = "❌ Отклонена"
            user_message = "❌ Ваша анкета отклонена администратором."
        
        # Отправляем решение пользователю
        try:
            bot.send_message(uid, user_message)
        except apihelper.ApiTelegramException as e:
            if e.error_code != 403:  # Игнорируем, если пользователь заблокировал бота
                logger.warning(f"Не удалось уведомить пользователя {uid}: {e}")
        
        # Обновляем сообщение админу
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"📋 **Модерация завершена**\n\n"
                     f"👤 Пользователь: `{uid}`\n"
                     f"📊 Решение: {status_text}\n"
                     f"👨‍💼 Модератор: {call.from_user.first_name}",
                parse_mode="Markdown"
            )
        except:
            pass  # Если не удалось редактировать сообщение
        
        logger.info(f"Модерация: {action} для пользователя {uid}")
        
    except Exception as e:
        logger.error(f"Ошибка в moderation_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!", show_alert=True)

@bot.message_handler(func=lambda message: True)
def other_messages(message):
    """Обработка всех остальных сообщений"""
    if message.text.startswith('/'):
        bot.reply_to(message, "❌ Неизвестная команда. Используйте /start")
    else:
        bot.reply_to(
            message,
            "Отправьте фото или видео после выбора раздела.",
            reply_markup=section_kb()
        )

# --- Flask health-check server (для Render Web Service) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "🤖 Бот работает!", 200

@app.route('/health')
def health():
    return {
        "status": "alive",
        "timestamp": datetime.now().isoformat(),
        "service": "telegram-bot"
    }, 200

def run_flask():
    """Запуск Flask в отдельном потоке"""
    # Отключаем логирование Flask
    import logging as flask_logging
    flask_logging.getLogger('werkzeug').setLevel(flask_logging.WARNING)
    
    port = int(os.getenv("PORT", 10000))  # Render использует порт 10000
    logger.info(f"Запуск Flask сервера на порту {port}")
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True
    )

def signal_handler(signum, frame):
    """Обработка сигналов завершения"""
    logger.info(f"Получен сигнал {signum}. Завершение работы...")
    sys.exit(0)

# --- Запуск ---
if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 50)
    logger.info("🚀 Запуск Telegram бота")
    logger.info(f"🤖 Бот: @{bot.get_me().username}")
    logger.info(f"👨‍💼 Админы: {ADMIN_IDS}")
    logger.info("=" * 50)
    
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Критическая ошибка бота: {e}")
    finally:
        logger.info("Бот остановлен")
