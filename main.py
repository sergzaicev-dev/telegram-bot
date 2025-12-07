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

# ===== ОТЛАДОЧНЫЙ КОД (можно удалить после исправления) =====
print("\n" + "="*70)
print("🔍 ОТЛАДКА: Проверка BOT_TOKEN")
print("="*70)

# Проверяем все переменные окружения
all_env_vars = os.environ
print(f"📋 Всего переменных окружения: {len(all_env_vars)}")

# Ищем BOT_TOKEN
if 'BOT_TOKEN' in all_env_vars:
    token = all_env_vars['BOT_TOKEN']
    print(f"✅ BOT_TOKEN найден в окружении")
    
    # Покажем токен (скрывая середину для безопасности)
    if len(token) > 20:
        masked_token = token[:10] + "..." + token[-10:]
        print(f"🔐 Токен (скрытый): {masked_token}")
    else:
        print(f"⚠️ Токен слишком короткий: {token}")
    
    print(f"📏 Длина токена: {len(token)} символов")
    
    # Проверяем формат
    if ':' in token:
        parts = token.split(':')
        print(f"✅ Формат правильный: есть двоеточие")
        print(f"   ID бота: {parts[0]}")
        print(f"   Хэш токена начинается с: {parts[1][:10]}")
    else:
        print(f"❌ Нет двоеточия в токене!")
    
    # Проверяем на пробелы
    if ' ' in token:
        print(f"⚠️ В токене есть пробелы!")
    else:
        print(f"✅ Пробелов нет")
else:
    print(f"❌ BOT_TOKEN не найден в переменных окружения!")

print("="*70 + "\n")
# ===== КОНЕЦ ОТЛАДОЧНОГО КОДА =====

# --- Настройки ---
# Берем токен из переменных окружения
BOT_TOKEN = os.getenv("8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI")

# Если токен не найден, логируем и выходим
if not BOT_TOKEN:
    logger.error("❌ ОШИБКА: BOT_TOKEN не задан в переменных окружения.")
    logger.info("📝 Проверьте настройки на Render:")
    logger.info("1. Dashboard → telegram-bot-dn13 → Environment")
    logger.info("2. Убедитесь, что есть переменная BOT_TOKEN")
    logger.info("3. Перезапустите сервис: Manual Deploy → Deploy latest commit")
    sys.exit(1)

ADMIN_IDS = [5064426902]  # Замените на свой ID
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Логируем успешный запуск
logger.info(f"✅ Бот инициализирован с токеном. ID бота: {BOT_TOKEN.split(':')[0] if ':' in BOT_TOKEN else 'неизвестно'}")

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
        "👋 *Привет! Я бот для отправки контента.*\n\n"
        "📋 *Как пользоваться:*\n"
        "1. 👇 Нажмите кнопку ниже и выберите раздел\n"
        "2. 📸 Отправьте фото или видео прямо в этот чат\n"
        "3. ⏳ Дождитесь модерации\n"
        "4. ✅ Получите уведомление о результате\n\n"
        "⚠️ *Важно:* Ваш контент увидят только после проверки администратором.\n\n"
        "📊 Проверить статус: /status\n"
        "🔄 Сбросить раздел: /reset"
    )
    
    try:
        bot.send_message(
            message.chat.id,
            welcome_text,
            reply_markup=section_kb()
        )
        logger.info(f"Пользователь {message.from_user.id} начал диалог")
    except Exception as e:
        logger.error(f"Ошибка при отправке приветствия: {e}")

@bot.message_handler(commands=["status"])
def status_command(message):
    """Проверка статуса пользователя"""
    uid = message.from_user.id
    user_data = db.fetchone(
        "SELECT section, approved FROM users WHERE user_id = ?",
        (uid,)
    )
    
    if user_data:
        section_name, approved = user_data
        status_text = {
            0: "⏳ Ожидает модерации",
            1: "✅ Одобрено",
            -1: "❌ Заблокирован"
        }.get(approved, "❓ Неизвестный статус")
        
        response = (
            f"📊 *Ваш статус:*\n\n"
            f"👤 ID: `{uid}`\n"
            f"📂 Раздел: {section_name}\n"
            f"📈 Статус: {status_text}\n\n"
            f"_Используйте /start для смены раздела_"
        )
    else:
        response = (
            "❌ *Вы еще не выбрали раздел.*\n\n"
            "Используйте /start для выбора раздела."
        )
    
    bot.reply_to(message, response)

@bot.message_handler(commands=["reset"])
def reset_command(message):
    """Сброс выбранного раздела"""
    uid = message.from_user.id
    db.execute("DELETE FROM users WHERE user_id = ?", (uid,))
    
    response = (
        "🔄 *Раздел сброшен!*\n\n"
        "Теперь вы можете выбрать новый раздел:"
    )
    
    bot.send_message(message.chat.id, response, reply_markup=section_kb())
    logger.info(f"Пользователь {uid} сбросил раздел")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def section_handler(call):
    """Обработка выбора раздела"""
    try:
        bot.answer_callback_query(call.id, "Раздел выбран!")
        
        if not call.data or "_" not in call.data:
            bot.edit_message_text(
                "❌ Ошибка выбора раздела",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return
            
        section_name = call.data.split("_", 1)[1]
        uid = call.from_user.id
        
        # Проверяем валидность раздела
        valid_sections = ["пары", "будуар", "гараж"]
        if section_name.lower() not in valid_sections:
            bot.edit_message_text(
                "❌ Неверный раздел",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return
        
        # Сохраняем в БД
        db.execute(
            "INSERT OR REPLACE INTO users (user_id, section, approved) VALUES (?, ?, 0)",
            (uid, section_name)
        )
        
        logger.info(f"Пользователь {uid} выбрал раздел: {section_name}")
        
        # Обновляем сообщение
        success_text = (
            f"✅ *Вы выбрали раздел: {section_name}*\n\n"
            "📸 *Теперь отправьте фото или видео прямо в этот чат.*\n\n"
            "_Бот обработает вашу заявку и отправит её на модерацию._"
        )
        
        try:
            bot.edit_message_text(
                success_text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None  # Убираем клавиатуру после выбора
            )
        except Exception as e:
            # Если не удалось отредактировать сообщение
            logger.warning(f"Не удалось отредактировать сообщение: {e}")
            bot.send_message(
                call.message.chat.id,
                success_text
            )
            
    except Exception as e:
        logger.error(f"Ошибка в section_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка", show_alert=True)

@bot.message_handler(content_types=["photo", "video", "animation", "document"])
def media_handler(message):
    """Обработка медиафайлов"""
    try:
        uid = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        
        # Проверяем, выбрал ли пользователь раздел
        user_data = db.fetchone(
            "SELECT section, approved FROM users WHERE user_id = ?",
            (uid,)
        )
        
        if not user_data:
            # Если раздел не выбран, показываем клавиатуру
            bot.reply_to(
                message,
                "❌ *Сначала выберите раздел!*\n\n"
                "Нажмите на кнопку ниже:",
                reply_markup=section_kb()
            )
            return
        
        section_name, approved = user_data
        
        # Проверяем, не забанен ли пользователь
        if approved == -1:
            bot.reply_to(
                message, 
                "❌ *Вы заблокированы и не можете отправлять контент.*\n\n"
                "Обратитесь к администратору для разблокировки."
            )
            return
        
        logger.info(f"Медиа от пользователя {uid}, раздел: {section_name}")
        
        # Отправляем админам
        submission_time = datetime.now().strftime("%H:%M:%S")
        for admin_id in ADMIN_IDS:
            try:
                # Отправляем информацию о пользователе
                user_info = (
                    f"📨 *Новая анкета на модерацию*\n\n"
                    f"👤 *Пользователь:*\n"
                    f"ID: `{uid}`\n"
                    f"Имя: {first_name}\n"
                    f"Ник: @{username if username else 'нет'}\n\n"
                    f"📂 *Раздел:* {section_name}\n"
                    f"🕒 *Время:* {submission_time}\n\n"
                    f"📎 *Тип:* {message.content_type}"
                )
                
                bot.send_message(admin_id, user_info)
                
                # Пересылаем медиа
                bot.forward_message(admin_id, message.chat.id, message.message_id)
                
                # Клавиатура модерации
                bot.send_message(admin_id, "📋 *Модерация:*", reply_markup=mod_kb(uid))
                
                logger.info(f"Уведомление отправлено админу {admin_id}")
                
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")
        
        # Подтверждение пользователю
        bot.reply_to(
            message,
            "✅ *Ваша анкета отправлена на модерацию!*\n\n"
            "⏳ *Ожидайте решения администратора.*\n\n"
            "_Вы получите уведомление о результате._"
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
        
        # Получаем информацию о пользователе
        user_data = db.fetchone(
            "SELECT section FROM users WHERE user_id = ?",
            (uid,)
        )
        
        section_name = user_data[0] if user_data else "неизвестно"
        
        # Обновляем статус в БД
        if action == "app":
            db.execute(
                "UPDATE users SET approved = 1 WHERE user_id = ?",
                (uid,)
            )
            status_text = "✅ Одобрена"
            user_message = (
                "🎉 *Ваша анкета одобрена!*\n\n"
                "✅ *Статус:* Одобрено администратором\n"
                f"📂 *Раздел:* {section_name}\n\n"
                "Теперь ваш контент будет доступен другим пользователям."
            )
        else:  # rej
            db.execute(
                "UPDATE users SET approved = -1 WHERE user_id = ?",
                (uid,)
            )
            status_text = "❌ Отклонена"
            user_message = (
                "❌ *Ваша анкета отклонена.*\n\n"
                "🔄 Вы можете отправить новый контент, но сначала выберите раздел: /start"
            )
        
        # Отправляем решение пользователю
        try:
            bot.send_message(uid, user_message)
            logger.info(f"Решение отправлено пользователю {uid}: {action}")
        except apihelper.ApiTelegramException as e:
            if e.error_code == 403:
                logger.warning(f"Пользователь {uid} заблокировал бота")
            else:
                logger.error(f"Не удалось уведомить пользователя {uid}: {e}")
        
        # Обновляем сообщение админу
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    f"📋 *Модерация завершена*\n\n"
                    f"👤 *Пользователь:* `{uid}`\n"
                    f"📂 *Раздел:* {section_name}\n"
                    f"📊 *Решение:* {status_text}\n"
                    f"👨‍💼 *Модератор:* {call.from_user.first_name}\n\n"
                    f"🕒 *Время:* {datetime.now().strftime('%H:%M:%S')}"
                )
            )
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение: {e}")
        
        logger.info(f"Модерация: {action} для пользователя {uid}, раздел: {section_name}")
        
    except Exception as e:
        logger.error(f"Ошибка в moderation_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!", show_alert=True)

@bot.message_handler(func=lambda message: True)
def other_messages(message):
    """Обработка всех остальных сообщений"""
    if message.text and message.text.startswith('/'):
        bot.reply_to(
            message,
            "❌ *Неизвестная команда.*\n\n"
            "Доступные команды:\n"
            "/start - Начать работу с ботом\n"
            "/status - Проверить статус\n"
            "/reset - Сбросить раздел\n"
            "/help - Помощь"
        )
    elif message.text:
        # Если текст не команда, проверяем статус пользователя
        uid = message.from_user.id
        user_data = db.fetchone(
            "SELECT section FROM users WHERE user_id = ?",
            (uid,)
        )
        
        if user_data:
            bot.reply_to(
                message,
                "📸 *Отправьте фото или видео для модерации.*\n\n"
                f"Ваш текущий раздел: {user_data[0]}\n\n"
                "Изменить раздел: /start"
            )
        else:
            bot.reply_to(
                message,
                "👋 *Сначала выберите раздел!*\n\n"
                "Используйте /start для начала работы.",
                reply_markup=section_kb()
            )

# --- Flask health-check server (для Render Web Service) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "🤖 Бот работает!", 200

@app.route('/health')
def health():
    """Endpoint для проверки здоровья приложения"""
    # Проверяем соединение с БД
    try:
        test_result = db.fetchone("SELECT 1")
        db_status = "connected" if test_result else "error"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return {
        "status": "alive",
        "timestamp": datetime.now().isoformat(),
        "service": "telegram-bot",
        "database": db_status,
        "admins_count": len(ADMIN_IDS)
    }, 200

@app.route('/stats')
def stats():
    """Статистика бота (только для админов)"""
    try:
        total_users = db.fetchone("SELECT COUNT(*) FROM users")[0]
        pending = db.fetchone("SELECT COUNT(*) FROM users WHERE approved = 0")[0]
        approved = db.fetchone("SELECT COUNT(*) FROM users WHERE approved = 1")[0]
        rejected = db.fetchone("SELECT COUNT(*) FROM users WHERE approved = -1")[0]
        
        return {
            "total_users": total_users,
            "pending_moderation": pending,
            "approved": approved,
            "rejected": rejected,
            "timestamp": datetime.now().isoformat()
        }, 200
    except Exception as e:
        return {"error": str(e)}, 500

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
    
    try:
        bot_info = bot.get_me()
        logger.info(f"🤖 Бот: @{bot_info.username} ({bot_info.first_name})")
    except Exception as e:
        logger.error(f"Не удалось получить информацию о боте: {e}")
        sys.exit(1)
    
    logger.info(f"👨‍💼 Админы: {ADMIN_IDS}")
    logger.info("=" * 50)
    
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота
    try:
        logger.info("🔄 Начинаем polling...")
        bot.infinity_polling(
            timeout=60,
            long_polling_timeout=30,
            logger_level=logging.WARNING  # Уменьшаем логирование библиотеки
        )
    except KeyboardInterrupt:
        logger.info("⏹ Остановка по запросу пользователя...")
    except Exception as e:
        logger.error(f"Критическая ошибка бота: {e}")
        sys.exit(1)
    finally:
        logger.info("🤖 Бот остановлен")
