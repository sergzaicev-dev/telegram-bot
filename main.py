#!/usr/bin/env python3
# coding: utf-8
"""
Telegram moderation bot с полной защитой от повторного вступления
"""
import os
import sys
import logging
import threading
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any
from flask import Flask, request
import signal
import time

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = "8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI"
ADMIN_IDS = [5064426902]
GROUP_CHAT_ID = -1003262980832  # УКАЖИ СЮДА ID СВОЕЙ ГРУППЫ (например: -1001234567890)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ---------- БАЗА ДАННЫХ ----------
DB_PATH = os.getenv("DB_PATH", "moderation_bot.db")
_db_lock = threading.Lock()

def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db_lock:
        conn = _conn()
        cur = conn.cursor()
        
        # Основные таблицы (как в работающем коде)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            status TEXT DEFAULT 'pending',
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            status INTEGER DEFAULT 0,
            moderator_id INTEGER,
            moderated_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """)
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
        )
        """)
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            current_app_id INTEGER,
            awaiting_media_type TEXT,
            last_action TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Расширенная таблица для группы
        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_tracking (
            user_id INTEGER PRIMARY KEY,
            in_group BOOLEAN DEFAULT 0,
            last_seen_in_group TIMESTAMP,
            join_count INTEGER DEFAULT 0,
            last_join_time TIMESTAMP,
            verification_required BOOLEAN DEFAULT 1,
            admin_decision TEXT DEFAULT NULL,
            decided_by INTEGER,
            decided_at TIMESTAMP,
            ban_history TEXT DEFAULT '[]', -- JSON массив дат банов
            unban_history TEXT DEFAULT '[]', -- JSON массив дат разбанов
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """)
        
        # Индексы
        cur.execute("CREATE INDEX IF NOT EXISTS idx_group_track ON group_tracking(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_group_in ON group_tracking(in_group)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_group_verify ON group_tracking(verification_required)")
        
        conn.commit()
        conn.close()

init_db()

def db_execute(query: str, params=(), fetchone=False, fetchall=False, return_id=False):
    with _db_lock:
        conn = _conn()
        cur = conn.cursor()
        try:
            cur.execute(query, params)
            conn.commit()
            if return_id:
                return cur.lastrowid
            if fetchone:
                row = cur.fetchone()
                return dict(row) if row else None
            if fetchall:
                rows = cur.fetchall()
                return [dict(r) for r in rows] if rows else []
            return cur.rowcount
        except Exception as e:
            logger.error("DB error: %s | Q: %s | P: %s", e, query, params)
            return None
        finally:
            conn.close()

# ---------- ФУНКЦИИ ДЛЯ ГРУППЫ (ПОЛНОЕ РЕШЕНИЕ) ----------
def update_group_status(user_id: int, in_group: bool, force_verification: bool = False):
    """Обновить статус пользователя в группе"""
    now = datetime.now().isoformat()
    
    tracking = db_execute("SELECT * FROM group_tracking WHERE user_id = ?", (user_id,), fetchone=True)
    
    if not tracking:
        # Первая запись
        db_execute("""
            INSERT INTO group_tracking 
            (user_id, in_group, last_seen_in_group, join_count, last_join_time, verification_required)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, 1 if in_group else 0, now, 1 if in_group else 0, now if in_group else None, 1))
    else:
        if in_group and not tracking['in_group']:
            # Пользователь вступил в группу
            join_count = tracking['join_count'] + 1
            
            # Если был забанен и разбанен - требуется верификация
            if force_verification:
                verification_required = 1
            else:
                verification_required = tracking['verification_required']
            
            db_execute("""
                UPDATE group_tracking 
                SET in_group = 1, last_seen_in_group = ?, 
                    join_count = ?, last_join_time = ?,
                    verification_required = ?
                WHERE user_id = ?
            """, (now, join_count, now, verification_required, user_id))
        elif not in_group and tracking['in_group']:
            # Пользователь вышел из группы
            db_execute("""
                UPDATE group_tracking 
                SET in_group = 0 
                WHERE user_id = ?
            """, (user_id,))

def get_user_group_status(user_id: int) -> Dict[str, Any]:
    """Получить полный статус пользователя в группе"""
    tracking = db_execute("SELECT * FROM group_tracking WHERE user_id = ?", (user_id,), fetchone=True)
    if not tracking:
        return {
            'in_group': False,
            'verification_required': True,
            'join_count': 0,
            'admin_decision': None
        }
    return dict(tracking)

def handle_group_join(user_id: int, chat_id: int = None):
    """Обработка вступления пользователя в группу - ПОЛНАЯ ПРОВЕРКА"""
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    
    if not user:
        # Создаем запись пользователя
        try:
            member_info = bot.get_chat_member(chat_id, user_id) if chat_id else None
            username = member_info.user.username if member_info else None
            first_name = member_info.user.first_name if member_info else None
            last_name = member_info.user.last_name if member_info else None
            
            db_execute("""
                INSERT INTO users (user_id, username, first_name, last_name, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (user_id, username, first_name, last_name))
            user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        except:
            user = {'user_id': user_id, 'first_name': 'Неизвестный', 'username': None, 'status': 'pending'}
    
    # Получаем статус в группе
    group_status = get_user_group_status(user_id)
    
    # КРИТИЧЕСКАЯ ПРОВЕРКА: если пользователь approved И ему не требуется верификация
    if user['status'] == 'approved' and not group_status['verification_required']:
        # Все ок, пользователь может остаться в группе
        update_group_status(user_id, True)
        return
    
    # ВСЕ ОСТАЛЬНЫЕ СЛУЧАИ - требуют верификации
    update_group_status(user_id, True, force_verification=True)
    
    # Отправляем уведомление админам
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Разрешить (без верификации)", callback_data=f"gallow_noverify_{user_id}"),
        InlineKeyboardButton("📝 Требовать верификацию", callback_data=f"gallow_verify_{user_id}")
    )
    kb.add(
        InlineKeyboardButton("❌ Запретить вход", callback_data=f"gdeny_{user_id}")
    )
    
    for aid in ADMIN_IDS:
        try:
            bot.send_message(
                aid,
                f"🔄 Пользователь вступил в группу:\n"
                f"ID: `{user_id}`\n"
                f"Имя: {user['first_name'] or '-'}\n"
                f"Ник: @{user['username'] or '-'}\n"
                f"Статус: {user['status']}\n"
                f"Вступлений: {group_status.get('join_count', 1)}\n\n"
                f"Выберите действие:",
                reply_markup=kb
            )
        except Exception as e:
            logger.debug("Не удалось уведомить админа: %s", e)
    
    # Если пользователь banned - пытаемся кикнуть
    if user['status'] == 'banned':
        try:
            if chat_id:
                bot.ban_chat_member(chat_id, user_id)
                time.sleep(1)
                bot.unban_chat_member(chat_id, user_id)
        except:
            pass

def handle_group_leave(user_id: int, chat_id: int = None):
    """Обработка выхода пользователя из группы"""
    update_group_status(user_id, False)
    
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not user:
        user = {'first_name': 'Неизвестный', 'username': None}
    
    for aid in ADMIN_IDS:
        try:
            bot.send_message(
                aid,
                f"⚠️ Пользователь вышел из группы:\n"
                f"ID: `{user_id}`\n"
                f"Имя: {user['first_name'] or '-'}\n"
                f"Ник: @{user['username'] or '-'}"
            )
        except Exception as e:
            logger.debug("Не удалось уведомить админа: %s", e)

# ---------- ОСНОВНЫЕ ФУНКЦИИ (из работающего кода) ----------
def ensure_user(user_id: int, username: str = None, first_name: str = None, last_name: str = ""):
    existing = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if existing:
        db_execute("""
            UPDATE users SET username = ?, first_name = ?, last_name = ?, last_activity = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (username, first_name, last_name, user_id))
    else:
        db_execute("""
            INSERT INTO users (user_id, username, first_name, last_name, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (user_id, username, first_name, last_name))

def get_user(user_id: int):
    return db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)

def notify_admins_new_application(app_id: int):
    app = db_execute("SELECT * FROM applications WHERE id = ?", (app_id,), fetchone=True)
    if not app:
        return
    uid = app['user_id']
    user = get_user(uid)
    text = f"📨 Новая анкета #{app_id}\nПользователь: `{uid}` ({user['first_name'] or '-'}) @{user['username'] or '-'}\nРаздел: {app['section']}\n"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"appr_{app_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{app_id}"),
    )
    kb.add(
        InlineKeyboardButton("✏️ Запросить правки", callback_data=f"fix_{app_id}"),
        InlineKeyboardButton("👁️ Просмотреть", callback_data=f"view_{app_id}")
    )
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text, reply_markup=kb)
        except:
            pass

# ---------- ОБРАБОТЧИКИ ГРУППЫ ----------
@bot.message_handler(content_types=["new_chat_members"])
def handle_new_chat_members(message):
    """Обработка новых участников группы"""
    for new_member in message.new_chat_members:
        if not new_member.is_bot:
            logger.info(f"Новый участник: {new_member.id} в группе {message.chat.id}")
            handle_group_join(new_member.id, message.chat.id)

@bot.message_handler(content_types=["left_chat_member"])
def handle_left_chat_member(message):
    """Обработка выхода участника из группы"""
    if not message.left_chat_member.is_bot:
        logger.info(f"Участник вышел: {message.left_chat_member.id} из группы {message.chat.id}")
        handle_group_leave(message.left_chat_member.id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(("gallow_", "gdeny_")))
def handle_group_decision(call):
    """Обработка решений по группе"""
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет прав", show_alert=True)
        return
    
    parts = call.data.split("_")
    action_type = parts[0]  # gallow или gdeny
    decision = parts[1] if len(parts) > 2 else None
    user_id = int(parts[-1])
    
    now = datetime.now().isoformat()
    
    if action_type == "gallow":
        # Разрешить вход
        if decision == "noverify":
            # Без верификации в будущем
            db_execute("""
                UPDATE group_tracking 
                SET verification_required = 0,
                    admin_decision = 'allow_no_verify',
                    decided_by = ?, decided_at = ?
                WHERE user_id = ?
            """, (call.from_user.id, now, user_id))
            
            # Меняем статус пользователя на approved
            db_execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (user_id,))
            
            bot.answer_callback_query(call.id, "Вход разрешен (без верификации)")
            try:
                bot.send_message(user_id, "✅ Вам разрешен вход в группу. Верификация не требуется.")
            except:
                pass
            
        elif decision == "verify":
            # Требовать верификацию
            db_execute("""
                UPDATE group_tracking 
                SET verification_required = 1,
                    admin_decision = 'allow_verify',
                    decided_by = ?, decided_at = ?
                WHERE user_id = ?
            """, (call.from_user.id, now, user_id))
            
            bot.answer_callback_query(call.id, "Требуется верификация")
            try:
                bot.send_message(user_id, "📝 Для доступа к группе требуется пройти верификацию. Напишите /start")
            except:
                pass
        
        # Обновляем сообщение админа
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"✅ Решение принято для пользователя {user_id}"
            )
        except:
            pass
    
    elif action_type == "gdeny":
        # Запретить вход
        db_execute("""
            UPDATE group_tracking 
            SET verification_required = 1,
                admin_decision = 'deny',
                decided_by = ?, decided_at = ?
            WHERE user_id = ?
        """, (call.from_user.id, now, user_id))
        
        # Баним пользователя
        db_execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (user_id,))
        
        bot.answer_callback_query(call.id, "Вход запрещен")
        
        # Пытаемся кикнуть из группы
        try:
            chat_id = call.message.chat.id if hasattr(call.message, 'chat') else None
            if chat_id and chat_id < 0:
                bot.ban_chat_member(chat_id, user_id)
                time.sleep(1)
                bot.unban_chat_member(chat_id, user_id)
        except:
            pass
        
        # Обновляем сообщение админа
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"❌ Вход запрещен для {user_id}"
            )
        except:
            pass

# ---------- ОСНОВНЫЕ ОБРАБОТЧИКИ (из работающего кода) ----------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    ensure_user(uid, username, first_name, last_name)
    user = get_user(uid)
    
    # КРИТИЧЕСКАЯ ПРОВЕРКА: если пользователь в группе
    if message.chat.type in ['group', 'supergroup']:
        group_status = get_user_group_status(uid)
        
        # Если пользователь в группе, но не прошел верификацию
        if group_status['in_group'] and group_status['verification_required']:
            if user['status'] == 'pending':
                handle_group_join(uid, message.chat.id)
                bot.send_message(uid, 
                    "⚠️ Вы в группе, но не прошли верификацию.\n"
                    "Администраторы получили уведомление.\n"
                    "Ожидайте решения."
                )
                return
    
    if user['status'] == 'banned':
        bot.send_message(uid, "🚫 Вы заблокированы.")
        return
    
    if user['status'] == 'approved':
        bot.send_message(uid, "✅ Доступ открыт.")
        return
    
    # Показываем интерфейс для создания анкеты
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📝 Создать анкету", callback_data="create_app"))
    bot.send_message(uid, "📝 Вы в режиме ожидания (pending).", reply_markup=kb)

# [Остальные обработчики из работающего кода остаются БЕЗ ИЗМЕНЕНИЙ]
# @bot.callback_query_handler(func=lambda call: call.data == "create_app")
# @bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
# @bot.callback_query_handler(func=lambda call: call.data.startswith(("add_normal_", "add_intimate_")))
# @bot.message_handler(content_types=["photo", "video", "animation"])
# @bot.callback_query_handler(func=lambda call: call.data.startswith("submit_app_"))
# @bot.callback_query_handler(func=lambda call: call.data.startswith("reset_app_"))
# @bot.callback_query_handler(func=lambda call: call.data.startswith(("appr_", "rej_", "fix_", "view_")))

# ---------- Flask и запуск ----------
app = Flask(__name__)
@app.route("/")
def health():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def signal_handler(signum, frame):
    logger.info("Получен сигнал %s. Завершение.", signum)
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info("Запуск бота с полной защитой группы...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error("Критическая ошибка: %s", e)
        sys.exit(1)
