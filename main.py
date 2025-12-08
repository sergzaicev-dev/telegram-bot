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
import time
import multiprocessing

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
# Получаем токен ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ Render
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Если токен не найден - критическая ошибка
if not BOT_TOKEN:
    logger.error("❌ ОШИБКА: BOT_TOKEN не задан в переменных окружения.")
    logger.info("📝 Как настроить на Render:")
    logger.info("1. Dashboard → ваш_сервис → Environment")
    logger.info("2. Add Environment Variable")
    logger.info("3. Key: BOT_TOKEN")
    logger.info("4. Value: ваш_токен_из_BotFather")
    logger.info("5. Сохранить и перезапустить сервис")
    sys.exit(1)

# Убираем пробелы по краям (если есть)
BOT_TOKEN = BOT_TOKEN.strip()

# Проверяем формат токена
if ':' not in BOT_TOKEN:
    logger.error(f"❌ НЕПРАВИЛЬНЫЙ ФОРМАТ ТОКЕНА")
    logger.error(f"Токен должен содержать двоеточие: 1234567890:ABCdefGHI...")
    logger.error(f"Ваш токен: '{BOT_TOKEN}'")
    sys.exit(1)

ADMIN_IDS = [5064426902]  # Замените на ваш ID
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

logger.info(f"✅ Бот инициализирован. ID: {BOT_TOKEN.split(':')[0]}")
# --- КОНЕЦ НАСТРОЕК ---

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
    """Клавиатура выбора раздела для новых пользователей"""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Пары", callback_data="sec_пары"),
        InlineKeyboardButton("Будуар", callback_data="sec_будуар"),
        InlineKeyboardButton("Гараж", callback_data="sec_гараж")
    )
    return kb

def approved_user_kb():
    """Клавиатура для одобренных пользователей"""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📤 Отправить контент в свой раздел", callback_data="send_content"),
        InlineKeyboardButton("🔄 Сменить раздел", callback_data="change_section"),
        InlineKeyboardButton("📊 Мой статус", callback_data="my_status")
    )
    return kb

def mod_kb(user_id):
    """Клавиатура модерации для админов"""
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"app_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{user_id}")
    )
    return markup

# --- Хендлеры бота ---
@bot.message_handler(commands=["start", "help"])
def start(message):
    """Обработка команды /start - РАЗНЫЕ СООБЩЕНИЯ ДЛЯ РАЗНЫХ СТАТУСОВ"""
    uid = message.from_user.id
    
    # Проверяем статус пользователя
    user_data = db.fetchone(
        "SELECT section, approved FROM users WHERE user_id = ?",
        (uid,)
    )
    
    if user_data:
        section_name, approved = user_data
        
        if approved == 1:
            # ОДОБРЕННЫЙ пользователь
            welcome_text = (
                "🎉 *Добро пожаловать обратно!*\n\n"
                f"✅ *Ваш статус:* **Одобрен**\n"
                f"📂 *Ваш раздел:* **{section_name}**\n\n"
                "*Доступные действия:*\n"
                "• 📤 Отправлять контент в свой раздел\n"
                "• 🔄 Сменить раздел (если нужно)\n"
                "• 📊 Проверить свой статус\n\n"
                "_Используйте кнопки ниже или команды:_\n"
                "/content - Отправить контент\n"
                "/change - Сменить раздел\n"
                "/status - Мой статус"
            )
            
            bot.send_message(
                message.chat.id,
                welcome_text,
                reply_markup=approved_user_kb()
            )
            
        elif approved == -1:
            # ЗАБЛОКИРОВАННЫЙ пользователь
            bot.send_message(
                message.chat.id,
                "❌ *Вы заблокированы.*\n\n"
                "Обратитесь к администратору для разблокировки."
            )
            
        else:
            # НА МОДЕРАЦИИ (approved = 0)
            welcome_text = (
                "⏳ *Ваша анкета на модерации.*\n\n"
                f"📂 *Выбранный раздел:* {section_name}\n"
                "📊 *Статус:* Ожидает проверки администратором\n\n"
                "Пожалуйста, подождите решения. "
                "Вы получите уведомление как только администратор проверит вашу анкету."
            )
            bot.send_message(message.chat.id, welcome_text)
            
    else:
        # НОВЫЙ пользователь
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
        
        bot.send_message(
            message.chat.id,
            welcome_text,
            reply_markup=section_kb()
        )
    
    logger.info(f"Пользователь {uid} начал диалог")

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
        )
        
        if approved == 1:
            response += (
                "🎉 *Вы одобрены!*\n"
                "Теперь вы можете отправлять контент в свой раздел.\n"
                "Используйте /content чтобы начать."
            )
        elif approved == 0:
            response += "⏳ Ожидайте решения администратора."
        else:
            response += "❌ Вы заблокированы. Обратитесь к администратору."
            
    else:
        response = (
            "❌ *Вы еще не выбрали раздел.*\n\n"
            "Используйте /start для выбора раздела."
        )
    
    bot.send_message(message.chat.id, response)

@bot.message_handler(commands=["content", "send"])
def content_command(message):
    """Команда для отправки контента одобренными пользователями"""
    uid = message.from_user.id
    user_data = db.fetchone(
        "SELECT section, approved FROM users WHERE user_id = ?",
        (uid,)
    )
    
    if not user_data:
        bot.send_message(
            message.chat.id,
            "❌ *Сначала выберите раздел!*\n"
            "Используйте /start для начала работы.",
            reply_markup=section_kb()
        )
        return
    
    section_name, approved = user_data
    
    if approved != 1:
        bot.send_message(
            message.chat.id,
            f"❌ *Вы не можете отправлять контент.*\n\n"
            f"📊 Ваш статус: {'⏳ Ожидает модерации' if approved == 0 else '❌ Заблокирован'}\n"
            f"Дождитесь одобрения администратора."
        )
        return
    
    # ОДОБРЕННЫЙ пользователь может отправлять контент
    bot.send_message(
        message.chat.id,
        f"📤 *Отправка контента*\n\n"
        f"📂 *Ваш раздел:* **{section_name}**\n\n"
        "Теперь вы можете отправлять фото или видео.\n"
        "Весь контент будет автоматически направлен в ваш раздел.\n\n"
        "📸 *Просто отправьте фото или видео прямо сейчас.*"
    )
    logger.info(f"Одобренный пользователь {uid} запросил отправку контента в раздел {section_name}")

@bot.message_handler(commands=["change", "change_section"])
def change_section_command(message):
    """Смена раздела для одобренных пользователей"""
    uid = message.from_user.id
    user_data = db.fetchone(
        "SELECT approved FROM users WHERE user_id = ?",
        (uid,)
    )
    
    if not user_data:
        bot.send_message(
            message.chat.id,
            "❌ *Сначала выберите раздел!*\n"
            "Используйте /start для начала работы.",
            reply_markup=section_kb()
        )
        return
    
    approved = user_data[0]
    
    if approved != 1:
        bot.send_message(
            message.chat.id,
            "❌ *Смена раздела доступна только одобренным пользователям.*\n"
            "Дождитесь одобрения администратора."
        )
        return
    
    bot.send_message(
        message.chat.id,
        "🔄 *Смена раздела*\n\n"
        "Выберите новый раздел для отправки контента:",
        reply_markup=section_kb()
    )

@bot.message_handler(commands=["reset"])
def reset_command(message):
    """Сброс выбранного раздела (только для неодобренных)"""
    uid = message.from_user.id
    user_data = db.fetchone(
        "SELECT approved FROM users WHERE user_id = ?",
        (uid,)
    )
    
    if user_data and user_data[0] == 1:
        bot.send_message(
            message.chat.id,
            "❌ *Вы не можете сбросить раздел, так как уже одобрены.*\n"
            "Используйте /change для смены раздела."
        )
        return
    
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
        
        # Проверяем текущий статус пользователя
        user_data = db.fetchone(
            "SELECT approved FROM users WHERE user_id = ?",
            (uid,)
        )
        
        if user_data and user_data[0] == 1:
            # Одобренный пользователь меняет раздел
            db.execute(
                "UPDATE users SET section = ? WHERE user_id = ?",
                (section_name, uid)
            )
            
            success_text = (
                f"✅ *Раздел изменен на: {section_name}*\n\n"
                "Теперь весь ваш контент будет направляться в этот раздел.\n\n"
                "📸 *Отправьте фото или видео, чтобы поделиться контентом.*"
            )
            
            try:
                bot.edit_message_text(
                    success_text,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None
                )
            except:
                bot.send_message(call.message.chat.id, success_text)
            
            logger.info(f"Одобренный пользователь {uid} сменил раздел на: {section_name}")
            
        else:
            # Новый пользователь или на модерации
            db.execute(
                "INSERT OR REPLACE INTO users (user_id, section, approved) VALUES (?, ?, 0)",
                (uid, section_name)
            )
            
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
                    reply_markup=None
                )
            except:
                bot.send_message(call.message.chat.id, success_text)
            
            logger.info(f"Пользователь {uid} выбрал раздел: {section_name}")
            
    except Exception as e:
        logger.error(f"Ошибка в section_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data in ["send_content", "change_section", "my_status"])
def approved_user_actions(call):
    """Обработка действий одобренных пользователей"""
    try:
        uid = call.from_user.id
        
        if call.data == "send_content":
            bot.answer_callback_query(call.id, "Отправьте фото или видео")
            
            user_data = db.fetchone(
                "SELECT section FROM users WHERE user_id = ? AND approved = 1",
                (uid,)
            )
            
            if user_data:
                section_name = user_data[0]
                bot.send_message(
                    call.message.chat.id,
                    f"📤 *Отправка контента в раздел: {section_name}*\n\n"
                    "📸 *Просто отправьте фото или видео прямо сейчас.*\n"
                    "Оно будет автоматически направлено в ваш раздел."
                )
            else:
                bot.send_message(
                    call.message.chat.id,
                    "❌ У вас нет доступа к отправке контента."
                )
                
        elif call.data == "change_section":
            bot.answer_callback_query(call.id, "Выберите новый раздел")
            bot.send_message(
                call.message.chat.id,
                "🔄 *Смена раздела*\n\n"
                "Выберите новый раздел для отправки контента:",
                reply_markup=section_kb()
            )
            
        elif call.data == "my_status":
            bot.answer_callback_query(call.id, "Проверяем статус...")
            
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
                )
                
                if approved == 1:
                    response += "🎉 *Вы одобрены!* Можете отправлять контент."
                elif approved == 0:
                    response += "⏳ Ожидайте решения администратора."
                else:
                    response += "❌ Вы заблокированы."
                    
                bot.send_message(call.message.chat.id, response)
                
    except Exception as e:
        logger.error(f"Ошибка в approved_user_actions: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка", show_alert=True)

@bot.message_handler(content_types=["photo", "video", "animation", "document"])
def media_handler(message):
    """Обработка медиафайлов - РАЗНЫЕ СЦЕНАРИИ ДЛЯ РАЗНЫХ СТАТУСОВ"""
    try:
        uid = message.from_user.id
        username = message.from_user.username or "нет"
        first_name = message.from_user.first_name or "не указано"
        
        logger.info(f"=== ПОЛУЧЕНО МЕДИА ОТ {uid} ===")
        logger.info(f"Тип: {message.content_type}, Имя: {first_name}")
        
        # Проверяем статус пользователя
        user_data = db.fetchone(
            "SELECT section, approved FROM users WHERE user_id = ?",
            (uid,)
        )
        
        if not user_data:
            # Пользователь без раздела
            bot.send_message(
                message.chat.id,
                "❌ *Сначала выберите раздел!*\n\n"
                "Нажмите на кнопку ниже:",
                reply_markup=section_kb()
            )
            return
        
        section_name, approved = user_data
        
        if approved == -1:
            # Заблокированный пользователь
            bot.send_message(
                message.chat.id, 
                "❌ *Вы заблокированы и не можете отправлять контент.*\n\n"
                "Обратитесь к администратору для разблокировки."
            )
            return
        
        elif approved == 0:
            # Пользователь на модерации - отправляем админам на проверку
            logger.info(f"Медиа от пользователя на модерации {uid}, раздел: {section_name}")
            
            # Отправляем админам на модерацию
            submission_time = datetime.now().strftime("%H:%M:%S")
            
            for admin_id in ADMIN_IDS:
                try:
                    caption = (
                        f"📨 *Новая анкета на модерацию*\n\n"
                        f"👤 *Пользователь:*\n"
                        f"ID: `{uid}`\n"
                        f"Имя: {first_name}\n"
                        f"Ник: @{username}\n\n"
                        f"📂 *Раздел:* {section_name}\n"
                        f"🕒 *Время:* {submission_time}\n\n"
                        f"📎 *Тип:* {message.content_type}"
                    )
                    
                    if message.content_type == 'photo':
                        file_id = message.photo[-1].file_id
                        bot.send_photo(
                            admin_id,
                            file_id,
                            caption=caption,
                            parse_mode="Markdown",
                            reply_markup=mod_kb(uid)
                        )
                    elif message.content_type == 'video':
                        file_id = message.video.file_id
                        bot.send_video(
                            admin_id,
                            file_id,
                            caption=caption,
                            parse_mode="Markdown",
                            reply_markup=mod_kb(uid)
                        )
                    else:
                        bot.forward_message(admin_id, message.chat.id, message.message_id)
                        bot.send_message(
                            admin_id,
                            f"{caption}\n\n📋 *Модерация:*",
                            parse_mode="Markdown",
                            reply_markup=mod_kb(uid)
                        )
                    
                    logger.info(f"✅ Анкета на модерацию отправлена админу {admin_id}")
                    
                except Exception as e:
                    logger.error(f"Не удалось отправить админу {admin_id}: {e}")
            
            # Подтверждение пользователю
            bot.send_message(
                message.chat.id,
                "✅ *Ваша анкета отправлена на модерацию!*\n\n"
                "⏳ *Ожидайте решения администратора.*\n\n"
                "_Вы получите уведомление о результате._"
            )
            
        elif approved == 1:
            # ОДОБРЕННЫЙ пользователь отправляет контент
            logger.info(f"Контент от одобренного пользователя {uid}, раздел: {section_name}")
            
            # Здесь должна быть логика отправки контента в группу/канал
            # Пока просто уведомляем пользователя
            bot.send_message(
                message.chat.id,
                f"✅ *Контент принят!*\n\n"
                f"📂 *Раздел:* **{section_name}**\n\n"
                "Ваш контент будет доступен в соответствующем разделе.\n"
                "Спасибо за участие! 🎉"
            )
            
            # TODO: Добавить отправку в группу/канал
            # bot.send_message(GROUP_ID, f"Новый контент в разделе {section_name} от @{username}")
            # bot.forward_message(GROUP_ID, message.chat.id, message.message_id)
            
        logger.info(f"✅ Обработка медиа завершена для пользователя {uid}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в media_handler: {e}")
        try:
            bot.send_message(
                message.chat.id,
                "❌ Произошла ошибка при обработке медиа. Пожалуйста, попробуйте еще раз."
            )
        except Exception as send_error:
            logger.error(f"Не удалось отправить сообщение об ошибке: {send_error}")

@bot.callback_query_handler(func=lambda call: call.data.startswith(("app_", "rej_")))
def moderation_handler(call):
    """Обработка модерации"""
    try:
        logger.info(f"=== НАЧАЛО МОДЕРАЦИИ ===")
        logger.info(f"Callback от: {call.from_user.id}, data: {call.data}")
        
        # Проверяем права администратора
        if call.from_user.id not in ADMIN_IDS:
            logger.warning(f"Попытка модерации от не-админа: {call.from_user.id}")
            bot.answer_callback_query(call.id, "❌ У вас нет прав!", show_alert=True)
            return
        
        bot.answer_callback_query(call.id, "Решение принято!")
        
        # Разбираем callback data
        parts = call.data.split("_")
        if len(parts) != 2:
            logger.error(f"Неверный формат callback: {call.data}")
            return
        
        action, uid_str = parts
        uid = int(uid_str)
        
        logger.info(f"Действие: {action}, Пользователь: {uid}")
        
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
                "✅ *Статус:* **Одобрено администратором**\n"
                f"📂 *Раздел:* **{section_name}**\n\n"
                "*🎊 Поздравляем! Теперь вы можете:*\n"
                "• 📤 Отправлять контент в свой раздел\n"
                "• 🔄 Сменить раздел если нужно\n"
                "• 📊 Проверять свой статус\n\n"
                "_Используйте /start чтобы увидеть новые возможности!_"
            )
            logger.info(f"✅ Анкета {uid} одобрена")
            
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
            logger.info(f"❌ Анкета {uid} отклонена")
        
        # Отправляем решение пользователю
        try:
            bot.send_message(uid, user_message, parse_mode="Markdown")
            logger.info(f"✅ Решение отправлено пользователю {uid}")
        except apihelper.ApiTelegramException as e:
            if e.error_code == 403:
                logger.warning(f"Пользователь {uid} заблокировал бота")
            else:
                logger.error(f"Не удалось уведомить пользователя {uid}: {e}")
        
        # Отправляем админу подтверждение
        try:
            bot.send_message(
                call.from_user.id,
                f"📋 *Модерация завершена*\n\n"
                f"👤 *Пользователь:* `{uid}`\n"
                f"📂 *Раздел:* {section_name}\n"
                f"📊 *Решение:* {status_text}\n"
                f"👨‍💼 *Модератор:* {call.from_user.first_name}\n\n"
                f"🕒 *Время:* {datetime.now().strftime('%H:%M:%S')}",
                parse_mode="Markdown"
            )
            logger.info(f"✅ Уведомление админу отправлено")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу: {e}")
        
        logger.info(f"=== МОДЕРАЦИЯ ЗАВЕРШЕНА ===")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в moderation_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка!", show_alert=True)

@bot.message_handler(func=lambda message: True)
def other_messages(message):
    """Обработка всех остальных сообщений"""
    if message.text and message.text.startswith('/'):
        bot.send_message(
            message.chat.id,
            "❌ *Неизвестная команда.*\n\n"
            "Доступные команды:\n"
            "/start - Начать работу с ботом\n"
            "/status - Проверить статус\n"
            "/content - Отправить контент (для одобренных)\n"
            "/change - Сменить раздел (для одобренных)\n"
            "/reset - Сбросить раздел (для новых)\n"
            "/help - Помощь"
        )
    elif message.text:
        # Если текст не команда
        uid = message.from_user.id
        user_data = db.fetchone(
            "SELECT section, approved FROM users WHERE user_id = ?",
            (uid,)
        )
        
        if user_data:
            section_name, approved = user_data
            if approved == 1:
                bot.send_message(
                    message.chat.id,
                    f"📤 *Отправьте фото или видео для раздела {section_name}*\n\n"
                    "Или используйте команды:\n"
                    "/content - Отправить контент\n"
                    "/change - Сменить раздел\n"
                    "/status - Проверить статус"
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "📸 *Отправьте фото или видео для модерации.*\n\n"
                    f"Ваш текущий раздел: {section_name}\n"
                    f"Статус: {'⏳ На модерации' if approved == 0 else '❌ Заблокирован'}\n\n"
                    "Изменить раздел: /start"
                )
        else:
            bot.send_message(
                message.chat.id,
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

def run_bot():
    """Запуск бота с обработкой исключений"""
    try:
        logger.info("=" * 50)
        logger.info("🚀 Запуск Telegram бота")
        
        # Проверяем соединение с Telegram API
        for check_attempt in range(3):
            try:
                bot_info = bot.get_me()
                logger.info(f"✅ Проверка API: успешно")
                logger.info(f"🤖 Бот: @{bot_info.username} ({bot_info.first_name})")
                logger.info(f"👥 Админы: {ADMIN_IDS}")
                break
            except Exception as e:
                logger.warning(f"⚠️ Проверка API не удалась (попытка {check_attempt + 1}): {e}")
                time.sleep(2)
        
       
