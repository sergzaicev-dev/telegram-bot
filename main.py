import os
import telebot
from telebot import types
import sqlite3
import threading
import logging
from datetime import datetime
import time

# ========== НАСТРОЙКА ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
if not ADMIN_IDS:
    ADMIN_IDS = [5064426902]  # Fallback

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self, db_path='bot.db'):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()
    
    def init_db(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            
            # Пользователи
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    status TEXT DEFAULT 'pending',  # pending, approved, rejected, banned
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Анкеты
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    section TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',  # pending, approved, rejected
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    moderated_at TIMESTAMP,
                    moderator_id INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Фото анкет
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS application_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL,
                    photo_type TEXT NOT NULL,  # normal, intimate
                    file_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
                )
            ''')
            
            conn.commit()
            conn.close()
        logger.info("База данных инициализирована")
    
    def execute(self, query, params=(), return_id=False):
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                conn.commit()
                result = cursor.lastrowid if return_id else cursor.rowcount
            except Exception as e:
                logger.error(f"Ошибка БД: {e}")
                result = None
            finally:
                conn.close()
            return result
    
    def fetchone(self, query, params=()):
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as e:
                logger.error(f"Ошибка fetchone: {e}")
                return None
            finally:
                conn.close()
    
    def fetchall(self, query, params=()):
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            try:
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                logger.error(f"Ошибка fetchall: {e}")
                return []
            finally:
                conn.close()

db = Database()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def update_user_activity(user_id):
    db.execute("UPDATE users SET last_activity = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))

def get_user_status(user_id):
    user = db.fetchone("SELECT status FROM users WHERE user_id = ?", (user_id,))
    return user['status'] if user else 'new'

# ========== КЛАВИАТУРЫ ==========
def section_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("Пары", callback_data="section_пары"),
        types.InlineKeyboardButton("Будуар", callback_data="section_будуар"),
        types.InlineKeyboardButton("Гараж", callback_data="section_гараж")
    )
    return keyboard

def moderation_keyboard(application_id):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{application_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{application_id}")
    )
    return keyboard

# ========== ОСНОВНЫЕ ХЕНДЛЕРЫ ==========
@bot.message_handler(commands=['start'])
def start_handler(message):
    """Первый контакт пользователя с ботом"""
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    
    logger.info(f"Пользователь {user_id} ({first_name}) начал работу")
    
    # Проверяем, есть ли пользователь в БД
    user = db.fetchone("SELECT status FROM users WHERE user_id = ?", (user_id,))
    
    if not user:
        # НОВЫЙ пользователь
        db.execute(
            "INSERT INTO users (user_id, username, first_name, status) VALUES (?, ?, ?, 'pending')",
            (user_id, username, first_name)
        )
        
        response = (
            "🔒 <b>ДОСТУП ЗАБЛОКИРОВАН</b>\n\n"
            "Вы новый участник группы. Для получения доступа необходимо:\n\n"
            "1. 📝 <b>Выбрать раздел</b> для своей анкеты\n"
            "2. 📸 <b>Загрузить фото</b> (обычные + откровенные)\n"
            "3. ⏳ <b>Ожидать проверки</b> администратором\n\n"
            "📌 <b>Ваша анкета будет проверена администратором.</b>\n"
            "При одобрении вы получите полный доступ ко всем разделам группы.\n"
            "При отклонении - будете удалены из группы.\n\n"
            "👇 <b>Выберите раздел для анкеты:</b>"
        )
        
        bot.send_message(user_id, response, reply_markup=section_keyboard())
        
        # Уведомление админам
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"🆕 <b>Новый пользователь ожидает доступа</b>\n\n"
                    f"👤 ID: <code>{user_id}</code>\n"
                    f"📛 Ник: @{username if username else 'нет'}\n"
                    f"👤 Имя: {first_name}\n"
                    f"🕒 Время: {datetime.now().strftime('%H:%M:%S')}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
    
    elif user['status'] == 'pending':
        # Пользователь уже создал анкету, но она на проверке
        bot.send_message(
            user_id,
            "⏳ <b>Ваша анкета находится на проверке</b>\n\n"
            "Администратор проверяет вашу анкету. "
            "Ожидайте решения.\n\n"
            "Вы получите уведомление о результате.",
            parse_mode="HTML"
        )
    
    elif user['status'] == 'approved':
        # Пользователь одобрен и имеет доступ
        bot.send_message(
            user_id,
            "✅ <b>ДОСТУП РАЗРЕШЕН</b>\n\n"
            "Ваша анкета одобрена администратором.\n"
            "Теперь у вас есть полный доступ ко всем разделам группы.\n\n"
            "🎉 Добро пожаловать в сообщество!",
            parse_mode="HTML"
        )
    
    elif user['status'] in ['rejected', 'banned']:
        # Пользователь отклонен или забанен
        bot.send_message(
            user_id,
            "🚫 <b>ДОСТУП ЗАПРЕЩЕН</b>\n\n"
            "Ваша анкета была отклонена администратором "
            "или вы были забанены.\n\n"
            "❌ Вы не можете использовать бота.",
            parse_mode="HTML"
        )
    
    update_user_activity(user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('section_'))
def section_handler(call):
    """Обработка выбора раздела"""
    user_id = call.from_user.id
    section = call.data.split('_')[1]
    
    # Проверяем статус пользователя
    user_status = get_user_status(user_id)
    
    if user_status != 'pending':
        bot.answer_callback_query(call.id, "❌ Неверный статус пользователя")
        return
    
    # Создаем новую анкету
    application_id = db.execute(
        "INSERT INTO applications (user_id, section) VALUES (?, ?)",
        (user_id, section),
        return_id=True
    )
    
    if not application_id:
        bot.answer_callback_query(call.id, "❌ Ошибка создания анкеты", show_alert=True)
        return
    
    bot.answer_callback_query(call.id, f"✅ Выбран раздел: {section}")
    
    # Инструкции пользователю
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(
            f"📂 <b>Раздел выбран: {section}</b>\n\n"
            "📋 <b>Требования к анкете:</b>\n"
            "1. 📸 <b>Обычные фото</b> (1 или более)\n"
            "   • Без посторонних лиц\n"
            "   • Не интимные\n\n"
            "2. 🔞 <b>Интимные фото</b> (1 или более)\n"
            "   • Откровенные фото\n\n"
            "👇 <b>Загружайте фото по одному.</b>\n"
            "После загрузки всех фото анкета отправится на проверку.\n\n"
            "<i>Используйте команду /cancel для отмены</i>"
        ),
        parse_mode="HTML"
    )

@bot.message_handler(commands=['cancel'])
def cancel_handler(message):
    """Отмена создания анкеты"""
    user_id = message.from_user.id
    user_status = get_user_status(user_id)
    
    if user_status == 'pending':
        # Удаляем неотправленные анкеты
        db.execute("DELETE FROM applications WHERE user_id = ? AND status = 'pending'", (user_id,))
        
        bot.send_message(
            user_id,
            "❌ <b>Создание анкеты отменено</b>\n\n"
            "Вы можете начать заново командой /start",
            parse_mode="HTML"
        )
    else:
        bot.send_message(
            user_id,
            "ℹ️ <b>Нет активного создания анкеты</b>",
            parse_mode="HTML"
        )

# ========== ОБРАБОТКА ФОТО ==========
user_temp_data = {}  # Временное хранение данных пользователя

@bot.message_handler(content_types=['photo'])
def photo_handler(message):
    """Обработка загружаемых фото"""
    user_id = message.from_user.id
    user_status = get_user_status(user_id)
    
    if user_status != 'pending':
        bot.reply_to(message, "❌ Сначала выберите раздел для анкеты (/start)")
        return
    
    # Получаем активную анкету пользователя
    application = db.fetchone(
        "SELECT id, section FROM applications WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    
    if not application:
        bot.reply_to(message, "❌ Сначала выберите раздел для анкеты (/start)")
        return
    
    application_id = application['id']
    
    # Определяем тип фото
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("📸 Обычное", callback_data=f"photo_normal_{application_id}"),
        types.InlineKeyboardButton("🔞 Интимное", callback_data=f"photo_intimate_{application_id}")
    )
    
    # Сохраняем file_id временно
    file_id = message.photo[-1].file_id
    if user_id not in user_temp_data:
        user_temp_data[user_id] = {}
    user_temp_data[user_id]['last_photo'] = file_id
    user_temp_data[user_id]['application_id'] = application_id
    
    bot.reply_to(
        message,
        "📸 <b>Фото получено!</b>\n\n"
        "Выберите тип фото:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('photo_'))
def photo_type_handler(call):
    """Обработка выбора типа фото"""
    user_id = call.from_user.id
    parts = call.data.split('_')
    photo_type = parts[1]  # normal или intimate
    application_id = int(parts[2])
    
    if user_id not in user_temp_data or 'last_photo' not in user_temp_data[user_id]:
        bot.answer_callback_query(call.id, "❌ Фото не найдено", show_alert=True)
        return
    
    file_id = user_temp_data[user_id]['last_photo']
    
    # Сохраняем фото в БД
    db.execute(
        "INSERT INTO application_photos (application_id, photo_type, file_id) VALUES (?, ?, ?)",
        (application_id, photo_type, file_id)
    )
    
    # Получаем статистику по анкете
    photos = db.fetchall(
        "SELECT photo_type, COUNT(*) as count FROM application_photos WHERE application_id = ? GROUP BY photo_type",
        (application_id,)
    )
    
    normal_count = 0
    intimate_count = 0
    for photo in photos:
        if photo['photo_type'] == 'normal':
            normal_count = photo['count']
        else:
            intimate_count = photo['count']
    
    bot.answer_callback_query(call.id, f"✅ Добавлено {photo_type} фото")
    
    # Показываем статистику
    stats_text = (
        f"📊 <b>Текущая анкета:</b>\n\n"
        f"📸 Обычных фото: {normal_count}\n"
        f"🔞 Интимных фото: {intimate_count}\n\n"
    )
    
    if normal_count >= 1 and intimate_count >= 1:
        # Все требования выполнены
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("📤 Отправить на проверку", callback_data=f"submit_{application_id}"))
        stats_text += "✅ <b>Все требования выполнены!</b>\nМожно отправить анкету на проверку."
    else:
        keyboard = None
        stats_text += "👇 <b>Продолжайте загружать фото</b>\nТребуется минимум 1 обычное и 1 интимное фото."
    
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=stats_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('submit_'))
def submit_application_handler(call):
    """Отправка анкеты на проверку"""
    application_id = int(call.data.split('_')[1])
    user_id = call.from_user.id
    
    # Получаем данные анкеты
    application = db.fetchone(
        """SELECT a.*, u.username, u.first_name 
           FROM applications a 
           JOIN users u ON a.user_id = u.user_id 
           WHERE a.id = ?""",
        (application_id,)
    )
    
    if not application:
        bot.answer_callback_query(call.id, "❌ Анкета не найдена", show_alert=True)
        return
    
    # Меняем статус анкеты на "на проверке"
    db.execute("UPDATE applications SET status = 'pending' WHERE id = ?", (application_id,))
    
    # Получаем все фото анкеты
    photos = db.fetchall(
        "SELECT photo_type, file_id FROM application_photos WHERE application_id = ?",
        (application_id,)
    )
    
    # Отправляем админам
    for admin_id in ADMIN_IDS:
        try:
            # Информация об анкете
            info_msg = (
                f"📨 <b>НОВАЯ АНКЕТА НА ПРОВЕРКУ</b>\n\n"
                f"👤 <b>Пользователь:</b>\n"
                f"ID: <code>{application['user_id']}</code>\n"
                f"Ник: @{application['username'] if application['username'] else 'нет'}\n"
                f"Имя: {application['first_name']}\n\n"
                f"📂 <b>Раздел:</b> {application['section']}\n"
                f"📸 <b>Фото:</b> {len(photos)} шт.\n"
                f"🕒 <b>Время:</b> {application['created_at'][:16]}\n\n"
                f"👇 <b>Фото анкеты:</b>"
            )
            
            bot.send_message(admin_id, info_msg, parse_mode="HTML")
            
            # Отправляем фото
            for photo in photos:
                caption = "📸 Обычное фото" if photo['photo_type'] == 'normal' else "🔞 Интимное фото"
                bot.send_photo(admin_id, photo['file_id'], caption=caption)
            
            # Клавиатура модерации
            bot.send_message(
                admin_id,
                "📋 <b>Решение по анкете:</b>",
                reply_markup=moderation_keyboard(application_id),
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id}: {e}")
    
    # Подтверждение пользователю
    bot.answer_callback_query(call.id, "✅ Анкета отправлена на проверку")
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=(
            "✅ <b>Анкета отправлена на проверку!</b>\n\n"
            "Администратор проверит вашу анкету. "
            "Ожидайте решения.\n\n"
            "📌 <b>Примечание:</b>\n"
            "• При одобрении - полный доступ ко всем разделам\n"
            "• При отклонении - удаление из группы\n\n"
            "Вы получите уведомление о результате."
        ),
        parse_mode="HTML"
    )

# ========== МОДЕРАЦИЯ ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith(('approve_', 'reject_')))
def moderation_handler(call):
    """Обработка модерации админом"""
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "❌ Нет прав", show_alert=True)
        return
    
    action = call.data.split('_')[0]  # approve или reject
    application_id = int(call.data.split('_')[1])
    
    # Получаем данные анкеты
    application = db.fetchone(
        """SELECT a.*, u.user_id, u.username, u.first_name 
           FROM applications a 
           JOIN users u ON a.user_id = u.user_id 
           WHERE a.id = ?""",
        (application_id,)
    )
    
    if not application:
        bot.answer_callback_query(call.id, "❌ Анкета не найдена", show_alert=True)
        return
    
    user_id = application['user_id']
    
    if action == 'approve':
        # ОДОБРЕНИЕ
        db.execute(
            "UPDATE applications SET status = 'approved', moderated_at = CURRENT_TIMESTAMP, moderator_id = ? WHERE id = ?",
            (call.from_user.id, application_id)
        )
        db.execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (user_id,))
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                "🎉 <b>ВАША АНКЕТА ОДОБРЕНА!</b>\n\n"
                "✅ Администратор проверил и одобрил вашу анкету.\n\n"
                "🎊 <b>Теперь у вас есть:</b>\n"
                "• Полный доступ ко всем разделам группы\n"
                "• Возможность просматривать анкеты других участников\n"
                "• Полная свобода действий согласно правилам группы\n\n"
                "Добро пожаловать в сообщество! 🎉",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        bot.answer_callback_query(call.id, "✅ Анкета одобрена")
        
        # Обновляем сообщение админу
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    f"✅ <b>АНКЕТА ОДОБРЕНА</b>\n\n"
                    f"👤 Пользователь: <code>{user_id}</code>\n"
                    f"📛 Ник: @{application['username'] if application['username'] else 'нет'}\n"
                    f"📂 Раздел: {application['section']}\n"
                    f"👨‍💼 Модератор: {call.from_user.first_name}\n"
                    f"🕒 Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"<i>Пользователь получил полный доступ ко всем разделам</i>"
                ),
                parse_mode="HTML"
            )
        except:
            pass
        
    else:
        # ОТКЛОНЕНИЕ
        db.execute(
            "UPDATE applications SET status = 'rejected', moderated_at = CURRENT_TIMESTAMP, moderator_id = ? WHERE id = ?",
            (call.from_user.id, application_id)
        )
        db.execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (user_id,))
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                "🚫 <b>ВАША АНКЕТА ОТКЛОНЕНА</b>\n\n"
                "❌ Администратор отклонил вашу анкету.\n\n"
                "📌 <b>Последствия:</b>\n"
                "• Вы удалены из группы\n"
                "• Доступ к боту заблокирован\n"
                "• Повторная подача анкеты невозможна\n\n"
                "Ваш аккаунт добавлен в чёрный список.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        bot.answer_callback_query(call.id, "❌ Анкета отклонена")
        
        # Обновляем сообщение админу
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    f"❌ <b>АНКЕТА ОТКЛОНЕНА</b>\n\n"
                    f"👤 Пользователь: <code>{user_id}</code>\n"
                    f"📛 Ник: @{application['username'] if application['username'] else 'нет'}\n"
                    f"📂 Раздел: {application['section']}\n"
                    f"👨‍💼 Модератор: {call.from_user.first_name}\n"
                    f"🕒 Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"<i>Пользователь забанен и удалён из системы</i>"
                ),
                parse_mode="HTML"
            )
        except:
            pass

# ========== АДМИН КОМАНДЫ ==========
@bot.message_handler(commands=['admin'])
def admin_handler(message):
    """Панель администратора"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    # Статистика
    total_users = db.fetchone("SELECT COUNT(*) as count FROM users")['count']
    pending_apps = db.fetchone("SELECT COUNT(*) as count FROM applications WHERE status = 'pending'")['count']
    approved_users = db.fetchone("SELECT COUNT(*) as count FROM users WHERE status = 'approved'")['count']
    
    response = (
        f"👨‍💼 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• 👥 Всего пользователей: {total_users}\n"
        f"• ✅ Одобренных: {approved_users}\n"
        f"• ⏳ Ожидают проверки: {pending_apps}\n\n"
        f"🛠 <b>Доступные команды:</b>\n"
        f"/users - Список пользователей\n"
        f"/pending - Анкеты на проверке\n"
        f"/stats - Подробная статистика"
    )
    
    bot.reply_to(message, response, parse_mode="HTML")

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    logger.info(f"🤖 Бот запускается...")
    logger.info(f"👨‍💼 Админы: {ADMIN_IDS}")
    
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Ошибка бота: {e}")
