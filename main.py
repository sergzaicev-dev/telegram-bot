import os
import telebot
from telebot import apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import threading
import logging
from flask import Flask, request
import signal
import sys
from datetime import datetime, timedelta
import json
import time

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
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("❌ ОШИБКА: BOT_TOKEN не задан в переменных окружения.")
    sys.exit(1)

BOT_TOKEN = BOT_TOKEN.strip()

if ':' not in BOT_TOKEN:
    logger.error(f"❌ НЕПРАВИЛЬНЫЙ ФОРМАТ ТОКЕНА")
    sys.exit(1)

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "5064426902")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip().isdigit()]
if not ADMIN_IDS:
    ADMIN_IDS = [5064426902]

RATE_LIMIT_MINUTES = int(os.getenv("RATE_LIMIT_MINUTES", "5"))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

logger.info(f"✅ Бот инициализирован. ID: {BOT_TOKEN.split(':')[0]}")
logger.info(f"👨‍💼 Админы: {ADMIN_IDS}")
logger.info(f"⏱️ Лимит отправки: {RATE_LIMIT_MINUTES} мин")

# --- Потокобезопасная работа с базой данных ---
class DatabaseManager:
    def __init__(self, db_path='users.db'):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Инициализация базы данных с новой структурой"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            
            # Таблица пользователей (новая структура со статусами)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    status TEXT DEFAULT 'pending',  -- pending/active/banned
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица выбранных разделов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    section TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица заявок (анкет)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    section TEXT NOT NULL,
                    media_type TEXT NOT NULL,  -- regular/intimate
                    file_ids TEXT NOT NULL,    -- JSON массив file_id
                    approved INTEGER DEFAULT 0,  -- 0=ожидает, 1=одобрено, -1=отклонено
                    moderator_id INTEGER,
                    moderated_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            
            # Таблица медиафайлов (для хранения file_id отдельно)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS media_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id INTEGER,
                    file_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,  -- photo/video/animation
                    content_type TEXT NOT NULL, -- regular/intimate
                    FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
                )
            """)
            
            # Индексы
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(approved)")
            
            conn.commit()
            conn.close()
    
    def execute(self, query, params=(), return_id=False):
        """Безопасное выполнение запроса"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                conn.commit()
                result = cursor.lastrowid if return_id else cursor.rowcount
            except Exception as e:
                logger.error(f"Ошибка БД при выполнении запроса: {e}")
                result = None
            finally:
                conn.close()
            return result
    
    def fetchone(self, query, params=()):
        """Безопасное получение одной записи"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                result = cursor.fetchone()
                if result:
                    result = dict(result)
            except Exception as e:
                logger.error(f"Ошибка БД при fetchone: {e}")
                result = None
            finally:
                conn.close()
            return result
    
    def fetchall(self, query, params=()):
        """Безопасное получение всех записей"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                results = cursor.fetchall()
                if results:
                    results = [dict(row) for row in results]
                else:
                    results = []
            except Exception as e:
                logger.error(f"Ошибка БД при fetchall: {e}")
                results = []
            finally:
                conn.close()
            return results

# Инициализация менеджера БД
db = DatabaseManager()

# --- Вспомогательные функции ---
def update_user_info(user_id, username, first_name, last_name=""):
    """Обновление информации о пользователе"""
    # Проверяем, существует ли пользователь
    existing = db.fetchone("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    
    if existing:
        # Обновляем существующего
        db.execute(
            """UPDATE users 
               SET username = ?, first_name = ?, last_name = ?, last_activity = CURRENT_TIMESTAMP 
               WHERE user_id = ?""",
            (username, first_name, last_name, user_id)
        )
    else:
        # Создаем нового со статусом pending
        db.execute(
            """INSERT INTO users 
               (user_id, username, first_name, last_name, status, last_activity) 
               VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
            (user_id, username, first_name, last_name)
        )

def get_user_status(user_id):
    """Получение статуса пользователя"""
    user_data = db.fetchone(
        "SELECT status FROM users WHERE user_id = ?",
        (user_id,)
    )
    return user_data['status'] if user_data else 'pending'

def set_user_status(user_id, status):
    """Установка статуса пользователя"""
    db.execute(
        "UPDATE users SET status = ? WHERE user_id = ?",
        (status, user_id)
    )

def get_user_section(user_id):
    """Получение активного раздела пользователя"""
    section = db.fetchone(
        """SELECT section FROM user_sections 
           WHERE user_id = ? AND is_active = 1 
           ORDER BY created_at DESC LIMIT 1""",
        (user_id,)
    )
    return section['section'] if section else None

def set_user_section(user_id, section_name):
    """Установка раздела для пользователя"""
    # Деактивируем предыдущий активный раздел
    db.execute(
        "UPDATE user_sections SET is_active = 0 WHERE user_id = ? AND is_active = 1",
        (user_id,)
    )
    
    # Добавляем новый раздел
    db.execute(
        "INSERT INTO user_sections (user_id, section) VALUES (?, ?)",
        (user_id, section_name)
    )

def get_user_stats(user_id):
    """Получение статистики пользователя"""
    stats = db.fetchone("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END) as approved,
            SUM(CASE WHEN approved = 0 THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN approved = -1 THEN 1 ELSE 0 END) as rejected
        FROM submissions 
        WHERE user_id = ?
    """, (user_id,))
    
    return stats or {'total': 0, 'approved': 0, 'pending': 0, 'rejected': 0}

def can_user_access_sections(user_id):
    """Проверка, может ли пользователь видеть разделы"""
    status = get_user_status(user_id)
    return status == 'active'

def notify_admins_about_new_user(user_id, username, first_name, last_name):
    """Уведомление админов о новом пользователе"""
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"🆕 *Новый пользователь ожидает одобрения!*\n\n"
                f"👤 ID: `{user_id}`\n"
                f"👤 Имя: {first_name} {last_name}\n"
                f"📛 Ник: @{username if username else 'нет'}\n"
                f"🕒 Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
                f"📋 *Действия:*\n"
                f"• Проверить профиль пользователя\n"
                f"• Использовать /admin для управления"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

# --- Клавиатуры ---
def section_kb():
    """Клавиатура выбора раздела (только для активных пользователей)"""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Пары", callback_data="sec_пары"),
        InlineKeyboardButton("Будуар", callback_data="sec_будуар"),
        InlineKeyboardButton("Гараж", callback_data="sec_гараж")
    )
    return kb

def admin_approve_kb(user_id):
    """Клавиатура одобрения пользователя для админов"""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{user_id}"),
        InlineKeyboardButton("👁️ Просмотреть", callback_data=f"admin_view_{user_id}"),
        InlineKeyboardButton("💬 Написать", callback_data=f"admin_msg_{user_id}")
    )
    return kb

def submission_type_kb():
    """Клавиатура выбора типа контента для анкеты"""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📸 Обычные фото", callback_data="type_regular"),
        InlineKeyboardButton("🔞 Интимные фото", callback_data="type_intimate"),
        InlineKeyboardButton("✅ Готово", callback_data="type_done")
    )
    return kb

def admin_main_kb():
    """Главная клавиатура админа"""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton("⏳ Ожидают", callback_data="admin_pending_users"),
        InlineKeyboardButton("📨 Заявки", callback_data="admin_pending_subs"),
        InlineKeyboardButton("👥 Активные", callback_data="admin_active_users")
    )
    return kb

# --- Хендлеры бота ---
@bot.message_handler(commands=["start", "help"])
def start(message):
    """Обработка команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name or ""
    
    # Обновляем информацию о пользователе
    update_user_info(user_id, username, first_name, last_name)
    
    # Получаем статус
    status = get_user_status(user_id)
    
    if status == 'banned':
        bot.reply_to(
            message,
            "❌ *Вы заблокированы!*\n\n"
            "Вы не можете использовать бота.\n"
            "Обратитесь к администратору для разблокировки."
        )
        return
    
    elif status == 'pending':
        # НОВЫЙ ПОЛЬЗОВАТЕЛЬ: ПОЛНАЯ БЛОКИРОВКА
        welcome_text = (
            "👋 *Привет! Добро пожаловать в группу.*\n\n"
            "📋 *Процесс одобрения:*\n"
            "1. ⏳ Вы находитесь в статусе ожидания\n"
            "2. 📋 Администратор проверит ваш профиль\n"
            "3. ✅ После одобрения вы получите полный доступ\n\n"
            "⚠️ *Пока вы не можете:*\n"
            "• Видеть разделы группы\n"
            "• Отправлять контент\n"
            "• Просматривать анкеты других\n\n"
            "📊 *Ваш статус:* Ожидание одобрения\n"
            "👨‍💼 *Администраторы уведомлены*\n\n"
            "⏳ *Ожидайте решения...*"
        )
        
        bot.reply_to(message, welcome_text)
        
        # Уведомляем админов о новом пользователе
        notify_admins_about_new_user(user_id, username, first_name, last_name)
        
        logger.info(f"Новый пользователь {user_id} ожидает одобрения")
        
    elif status == 'active':
        # АКТИВНЫЙ ПОЛЬЗОВАТЕЛЬ: ПОЛНЫЙ ДОСТУП
        welcome_text = (
            "👋 *С возвращением!*\n\n"
            "✅ *Ваш статус:* Полный доступ\n\n"
            "📋 *Доступные действия:*\n"
            "1. 📂 Выбрать раздел для анкеты\n"
            "2. 📸 Отправить обычные фото (1+)\n"
            "3. 🔞 Отправить интимные фото (1+)\n"
            "4. ⏳ Дождаться модерации\n\n"
            "⚠️ *Важно:*\n"
            f"• Лимит: 1 анкета в {RATE_LIMIT_MINUTES} минут\n"
            "• Анкета должна содержать оба типа фото\n"
            "• После одобрения анкеты - полный доступ ко всем разделам\n\n"
            "👇 *Выберите раздел для анкеты:*"
        )
        
        bot.reply_to(message, welcome_text, reply_markup=section_kb())
        
    else:
        # Неизвестный статус
        bot.reply_to(
            message,
            "❓ *Неизвестный статус.*\n\n"
            "Обратитесь к администратору."
        )

@bot.message_handler(commands=["status"])
def status_command(message):
    """Проверка статуса пользователя"""
    user_id = message.from_user.id
    
    # Обновляем активность
    db.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))
    
    # Получаем информацию
    user_data = db.fetchone(
        "SELECT status, created_at FROM users WHERE user_id = ?",
        (user_id,)
    )
    
    if not user_data:
        bot.reply_to(
            message,
            "❌ *Вы еще не начали работу с ботом.*\n\n"
            "Используйте /start для начала работы."
        )
        return
    
    status = user_data['status']
    section = get_user_section(user_id)
    stats = get_user_stats(user_id)
    
    # Определяем текстовое описание статуса
    if status == 'pending':
        status_text = "⏳ ОЖИДАНИЕ ОДОБРЕНИЯ"
        status_desc = "*Вы ожидаете проверки администратором.*\n\nПосле одобрения вы сможете создавать анкеты и получить доступ к разделам."
    elif status == 'active':
        status_text = "✅ АКТИВЕН (ПОЛНЫЙ ДОСТУП)"
        status_desc = "*У вас есть полный доступ ко всем разделам.*\n\nВы можете создавать анкеты и просматривать контент других участников."
    elif status == 'banned':
        status_text = "❌ ЗАБЛОКИРОВАН"
        status_desc = "*Вы заблокированы и не можете использовать бота.*\n\nОбратитесь к администратору для разблокировки."
    else:
        status_text = "❓ НЕИЗВЕСТНЫЙ СТАТУС"
        status_desc = "Обратитесь к администратору."
    
    response = (
        f"📊 *Ваш статус*\n\n"
        f"👤 *ID:* `{user_id}`\n"
        f"📈 *Статус:* {status_text}\n"
        f"📂 *Раздел:* {section if section else 'не выбран'}\n"
        f"📅 *Зарегистрирован:* {user_data['created_at'][:10]}\n\n"
        f"📨 *Статистика анкет:*\n"
        f"• Всего: {stats['total']}\n"
        f"• ✅ Одобрено: {stats['approved']}\n"
        f"• ⏳ Ожидает: {stats['pending']}\n"
        f"• ❌ Отклонено: {stats['rejected']}\n\n"
        f"{status_desc}"
    )
    
    # Добавляем кнопки в зависимости от статуса
    if status == 'active':
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📂 Выбрать раздел", callback_data="choose_section"))
        bot.reply_to(message, response, reply_markup=markup)
    else:
        bot.reply_to(message, response)

@bot.message_handler(commands=["admin"])
def admin_command(message):
    """Админ-панель"""
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        bot.reply_to(message, "❌ У вас нет прав администратора.")
        return
    
    # Получаем статистику
    pending_users = db.fetchone("SELECT COUNT(*) as count FROM users WHERE status = 'pending'")['count']
    active_users = db.fetchone("SELECT COUNT(*) as count FROM users WHERE status = 'active'")['count']
    banned_users = db.fetchone("SELECT COUNT(*) as count FROM users WHERE status = 'banned'")['count']
    
    pending_subs = db.fetchone("SELECT COUNT(*) as count FROM submissions WHERE approved = 0")['count']
    
    response = (
        f"👨‍💼 *Админ-панель*\n\n"
        f"📊 *Статистика пользователей:*\n"
        f"• ⏳ Ожидают: {pending_users}\n"
        f"• ✅ Активных: {active_users}\n"
        f"• ❌ Заблокированных: {banned_users}\n\n"
        f"📨 *Статистика анкет:*\n"
        f"• ⏳ Ожидают модерации: {pending_subs}\n\n"
        f"🛠️ *Действия:*"
    )
    
    bot.reply_to(message, response, reply_markup=admin_main_kb())

# --- Обработчики callback-запросов ---
user_sessions = {}  # Временное хранилище для сессий пользователей

@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def section_handler(call):
    """Обработка выбора раздела"""
    try:
        user_id = call.from_user.id
        
        # Проверяем статус пользователя
        status = get_user_status(user_id)
        if status != 'active':
            bot.answer_callback_query(call.id, "❌ У вас нет доступа к разделам!")
            return
        
        if not call.data or "_" not in call.data:
            bot.answer_callback_query(call.id, "❌ Ошибка выбора раздела")
            return
            
        section_name = call.data.split("_", 1)[1]
        
        # Проверяем валидность раздела
        valid_sections = ["пары", "будуар", "гараж"]
        if section_name.lower() not in valid_sections:
            bot.answer_callback_query(call.id, "❌ Неверный раздел")
            return
        
        # Устанавливаем раздел
        set_user_section(user_id, section_name)
        
        # Инициализируем сессию для создания анкеты
        user_sessions[user_id] = {
            'section': section_name,
            'regular_photos': [],
            'intimate_photos': [],
            'step': 'waiting_type'  # waiting_type, receiving_regular, receiving_intimate
        }
        
        bot.answer_callback_query(call.id, f"✅ Раздел {section_name} выбран!")
        
        # Обновляем сообщение
        bot.edit_message_text(
            f"✅ *Вы выбрали раздел: {section_name}*\n\n"
            "📋 *Теперь создадим вашу анкету:*\n\n"
            "1. 📸 *Обычные фото* (минимум 1)\n"
            "   • Без посторонних лиц\n"
            "   • Не интимные\n\n"
            "2. 🔞 *Интимные фото* (минимум 1)\n"
            "   • Откровенные фото\n\n"
            "👇 *Начнем с обычных фото. Нажмите кнопку ниже:*",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=submission_type_kb()
        )
        
        logger.info(f"Пользователь {user_id} начал создание анкеты в разделе {section_name}")
        
    except Exception as e:
        logger.error(f"Ошибка в section_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("type_"))
def submission_type_handler(call):
    """Обработка выбора типа контента"""
    try:
        user_id = call.from_user.id
        
        if user_id not in user_sessions:
            bot.answer_callback_query(call.id, "❌ Сессия устарела. Начните заново.")
            return
        
        action = call.data.split("_")[1]
        session = user_sessions[user_id]
        
        if action == 'regular':
            session['step'] = 'receiving_regular'
            bot.answer_callback_query(call.id, "📸 Отправьте обычные фото")
            
            bot.edit_message_text(
                "📸 *Отправьте ОБЫЧНЫЕ ФОТО*\n\n"
                "❌ *Запрещено:*\n"
                "• Посторонние лица\n"
                "• Интимный контент\n"
                "• Низкое качество\n\n"
                "✅ *Требования:*\n"
                "• Минимум 1 фото\n"
                "• Четкое изображение\n"
                "• Соответствие разделу\n\n"
                "📎 *Отправьте фото одним или несколькими сообщениями*\n"
                "💾 *Когда закончите, нажмите кнопку ниже*",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("✅ Готово с обычными фото", callback_data="type_regular_done")
                )
            )
            
        elif action == 'intimate':
            if len(session['regular_photos']) == 0:
                bot.answer_callback_query(call.id, "❌ Сначала отправьте обычные фото!")
                return
            
            session['step'] = 'receiving_intimate'
            bot.answer_callback_query(call.id, "🔞 Отправьте интимные фото")
            
            bot.edit_message_text(
                "🔞 *Отправьте ИНТИМНЫЕ ФОТО*\n\n"
                "⚠️ *Внимание:*\n"
                "• Только для совершеннолетних\n"
                "• Контент 18+\n"
                "• Откровенные фото\n\n"
                "✅ *Требования:*\n"
                "• Минимум 1 фото\n"
                "• Четкое изображение\n"
                "• Соответствие разделу\n\n"
                "📎 *Отправьте фото одним или несколькими сообщениями*\n"
                "💾 *Когда закончите, нажмите кнопку ниже*",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("✅ Готово с интимными фото", callback_data="type_intimate_done")
                )
            )
            
        elif action == 'regular_done':
            if len(session['regular_photos']) == 0:
                bot.answer_callback_query(call.id, "❌ Нужно отправить хотя бы 1 обычное фото!")
                return
            
            # Переходим к интимным фото
            session['step'] = 'receiving_intimate'
            bot.answer_callback_query(call.id, "✅ Переходим к интимным фото")
            
            bot.edit_message_text(
                f"✅ *Обычные фото сохранены: {len(session['regular_photos'])}*\n\n"
                "👇 *Теперь отправьте интимные фото:*",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔞 Интимные фото", callback_data="type_intimate")
                )
            )
            
        elif action == 'intimate_done':
            if len(session['intimate_photos']) == 0:
                bot.answer_callback_query(call.id, "❌ Нужно отправить хотя бы 1 интимное фото!")
                return
            
            # Завершаем создание анкеты
            bot.answer_callback_query(call.id, "✅ Анкета готова!")
            
            # Сохраняем анкету в БД
            save_submission(user_id, session)
            
            # Очищаем сессию
            del user_sessions[user_id]
            
            bot.edit_message_text(
                f"🎉 *Анкета создана и отправлена на модерацию!*\n\n"
                f"📂 *Раздел:* {session['section']}\n"
                f"📸 *Обычные фото:* {len(session['regular_photos'])}\n"
                f"🔞 *Интимные фото:* {len(session['intimate_photos'])}\n\n"
                f"⏳ *Ожидайте решения администратора.*\n\n"
                f"📊 *После одобрения анкеты вы получите:*\n"
                f"• ✅ Полный доступ ко всем разделам\n"
                f"• 👁️ Возможность просматривать анкеты других\n"
                f"• 💬 Доступ к общению во всех разделах\n\n"
                f"_Вы получите уведомление о результате._",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            
        elif action == 'done':
            # Проверяем, что есть оба типа фото
            if len(session['regular_photos']) == 0 or len(session['intimate_photos']) == 0:
                bot.answer_callback_query(call.id, "❌ Нужны оба типа фото!")
                return
            
            # Завершаем создание анкеты
            bot.answer_callback_query(call.id, "✅ Анкета готова!")
            
            # Сохраняем анкету в БД
            save_submission(user_id, session)
            
            # Очищаем сессию
            del user_sessions[user_id]
            
            bot.edit_message_text(
                f"🎉 *Анкета создана и отправлена на модерацию!*\n\n"
                f"📂 *Раздел:* {session['section']}\n"
                f"📸 *Обычные фото:* {len(session['regular_photos'])}\n"
                f"🔞 *Интимные фото:* {len(session['intimate_photos'])}\n\n"
                f"⏳ *Ожидайте решения администратора.*\n\n"
                f"_Вы получите уведомление о результате._",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            
    except Exception as e:
        logger.error(f"Ошибка в submission_type_handler: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка", show_alert=True)

def save_submission(user_id, session):
    """Сохранение анкеты в БД"""
    try:
        # Сохраняем основную информацию об анкете
        submission_id = db.execute(
            """INSERT INTO submissions 
               (user_id, section, media_type, file_ids, approved) 
               VALUES (?, ?, 'mixed', '[]', 0)""",
            (user_id, session['section']),
            return_id=True
        )
        
        if not submission_id:
            logger.error(f"Не удалось сохранить анкету для пользователя {user_id}")
            return False
        
        # Сохраняем обычные фото
        for file_id in session['regular_photos']:
            db.execute(
                """INSERT INTO media_files 
                   (submission_id, file_id, media_type, content_type) 
                   VALUES (?, ?, 'photo', 'regular')""",
                (submission_id, file_id)
            )
        
        # Сохраняем интимные фото
        for file_id in session['intimate_photos']:
            db.execute(
                """INSERT INTO media_files 
                   (submission_id, file_id, media_type, content_type) 
                   VALUES (?, ?, 'photo', 'intimate')""",
                (submission_id, file_id)
            )
        
        # Обновляем file_ids в основной таблице
        all_files = session['regular_photos'] + session['intimate_photos']
        db.execute(
            "UPDATE submissions SET file_ids = ? WHERE id = ?",
            (json.dumps(all_files), submission_id)
        )
        
        # Уведомляем админов
        notify_admins_about_submission(submission_id, user_id, session)
        
        logger.info(f"Анкета #{submission_id} сохранена для пользователя {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении анкеты: {e}")
        return False

def notify_admins_about_submission(submission_id, user_id, session):
    """Уведомление админов о новой анкете"""
    user_data = db.fetchone(
        "SELECT username, first_name FROM users WHERE user_id = ?",
        (user_id,)
    )
    
    username = user_data['username'] if user_data and user_data['username'] else 'нет'
    first_name = user_data['first_name'] if user_data else 'Неизвестно'
    
    for admin_id in ADMIN_IDS:
        try:
            message = (
                f"📨 *Новая анкета #{submission_id}*\n\n"
                f"👤 *Пользователь:*\n"
                f"ID: `{user_id}`\n"
                f"Имя: {first_name}\n"
                f"Ник: @{username}\n\n"
                f"📂 *Раздел:* {session['section']}\n"
                f"📸 *Обычные фото:* {len(session['regular_photos'])}\n"
                f"🔞 *Интимные фото:* {len(session['intimate_photos'])}\n\n"
                f"🛠️ *Модерация:*"
            )
            
            # Отправляем одно обычное фото для примера
            if session['regular_photos']:
                bot.send_photo(admin_id, session['regular_photos'][0], caption=message)
            else:
                bot.send_message(admin_id, message)
            
            # Клавиатура для модерации
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("✅ Одобрить анкету", callback_data=f"sub_approve_{submission_id}"),
                InlineKeyboardButton("❌ Отклонить анкету", callback_data=f"sub_reject_{submission_id}"),
                InlineKeyboardButton("👁️ Просмотреть все фото", callback_data=f"sub_view_{submission_id}"),
                InlineKeyboardButton("👤 Инфо о пользователе", callback_data=f"sub_info_{user_id}")
            )
            
            bot.send_message(admin_id, "📋 *Действия:*", reply_markup=kb)
            
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

# --- Обработка медиафайлов ---
@bot.message_handler(content_types=["photo"])
def photo_handler(message):
    """Обработка фото"""
    try:
        user_id = message.from_user.id
        
        # Проверяем статус
        status = get_user_status(user_id)
        if status != 'active':
            bot.reply_to(message, "❌ У вас нет доступа для отправки контента.")
            return
        
        # Проверяем, есть ли активная сессия
        if user_id not in user_sessions:
            bot.reply_to(
                message,
                "❌ *Сначала выберите раздел для анкеты!*\n\n"
                "Используйте /start для начала."
            )
            return
        
        session = user_sessions[user_id]
        file_id = message.photo[-1].file_id
        
        # Добавляем фото в соответствующую категорию
        if session['step'] == 'receiving_regular':
            session['regular_photos'].append(file_id)
            count = len(session['regular_photos'])
            bot.reply_to(message, f"✅ Обычное фото #{count} сохранено")
            
        elif session['step'] == 'receiving_intimate':
            session['intimate_photos'].append(file_id)
            count = len(session['intimate_photos'])
            bot.reply_to(message,
