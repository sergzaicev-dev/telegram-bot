#!/usr/bin/env python3
# coding: utf-8
"""
Telegram moderation bot с полной защитой от повторного вступления + анкеты + ОТЛАДКА
"""
import os
import sys
import logging
import threading
import sqlite3
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
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

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")  # Меняем на HTML или убираем parse_mode

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
        
        # Основные таблицы
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
            ban_history TEXT DEFAULT '[]',
            unban_history TEXT DEFAULT '[]',
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

# ---------- ФУНКЦИИ ДЛЯ ГРУППЫ ----------
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
        logger.info(f"[GROUP] Создана запись для пользователя {user_id}, in_group={in_group}")
    else:
        if in_group and not tracking['in_group']:
            # Пользователь вступил в группу
            join_count = tracking['join_count'] + 1
            
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
            logger.info(f"[GROUP] Пользователь {user_id} вступил, verification_required={verification_required}")
        elif not in_group and tracking['in_group']:
            # Пользователь вышел из группы
            db_execute("""
                UPDATE group_tracking 
                SET in_group = 0 
                WHERE user_id = ?
            """, (user_id,))
            logger.info(f"[GROUP] Пользователь {user_id} вышел")

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
    logger.info(f"[DEBUG] ======= НАЧАЛО handle_group_join для user_id={user_id} =======")
    
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    
    logger.info(f"[DEBUG] Пользователь {user_id} в БД users: {user}")
    
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
            logger.info(f"[DEBUG] Создан новый пользователь: {user}")
        except Exception as e:
            logger.error(f"[DEBUG] Ошибка при создании пользователя: {e}")
            user = {'user_id': user_id, 'first_name': 'Неизвестный', 'username': None, 'status': 'pending'}
    
    # Получаем статус в группе
    group_status = get_user_group_status(user_id)
    logger.info(f"[DEBUG] Статус в группе для {user_id}: {group_status}")
    
    # КРИТИЧЕСКАЯ ПРОВЕРКА: если пользователь approved И ему не требуется верификация
    logger.info(f"[DEBUG] Проверка условия: user['status']={user['status']}, group_status['verification_required']={group_status['verification_required']}")
    
    if user['status'] == 'approved' and not group_status['verification_required']:
        logger.info(f"[DEBUG] УСЛОВИЕ СРАБОТАЛО: пользователь approved и верификация не требуется. УВЕДОМЛЕНИЯ НЕ ОТПРАВЛЯЕМ!")
        update_group_status(user_id, True)
        return
    
    logger.info(f"[DEBUG] УСЛОВИЕ НЕ СРАБОТАЛО: user['status']={user['status']}, verification_required={group_status['verification_required']}")
    logger.info(f"[DEBUG] Продолжаем обработку для отправки уведомлений админам...")
    
    # ВСЕ ОСТАЛЬНЫЕ СЛУЧАИ - требуют верификации
    update_group_status(user_id, True, force_verification=True)
    
    # ОТПРАВЛЯЕМ УВЕДОМЛЕНИЕ АДМИНАМ (ИСПРАВЛЕНО - без Markdown)
    message_text = (
        f"🔄 Пользователь вступил в группу:\n"
        f"ID: {user_id}\n"
        f"Имя: {user['first_name'] or '-'}\n"
        f"Ник: @{user['username'] or '-'}\n"
        f"Статус: {user['status']}\n"
        f"Вступлений: {group_status.get('join_count', 1)}\n\n"
        f"Выберите действие:"
    )
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Разрешить (без верификации)", callback_data=f"gallow_noverify_{user_id}"),
        InlineKeyboardButton("📝 Требовать верификацию", callback_data=f"gallow_verify_{user_id}")
    )
    kb.add(
        InlineKeyboardButton("❌ Запретить вход", callback_data=f"gdeny_{user_id}")
    )
    
    admin_count = 0
    for aid in ADMIN_IDS:
        try:
            logger.info(f"[DEBUG] Пытаюсь отправить уведомление админу {aid}")
            bot.send_message(
                aid,
                message_text,
                reply_markup=kb,
                parse_mode=None  # ОТКЛЮЧАЕМ Markdown
            )
            admin_count += 1
            logger.info(f"[DEBUG] Уведомление успешно отправлено админу {aid}")
        except Exception as e:
            logger.error(f"[DEBUG] Не удалось уведомить админа {aid}: {e}")
    
    logger.info(f"[DEBUG] Отправлено уведомлений админам: {admin_count}/{len(ADMIN_IDS)}")
    
    # Если пользователь banned - пытаемся кикнуть
    if user['status'] == 'banned':
        logger.info(f"[DEBUG] Пользователь {user_id} имеет статус 'banned', пытаюсь кикнуть...")
        try:
            if chat_id:
                bot.ban_chat_member(chat_id, user_id)
                time.sleep(1)
                bot.unban_chat_member(chat_id, user_id)
                logger.info(f"[DEBUG] Пользователь {user_id} забанен и разбанен")
        except Exception as e:
            logger.error(f"[DEBUG] Ошибка при кике пользователя {user_id}: {e}")
    
    logger.info(f"[DEBUG] ======= КОНЕЦ handle_group_join для user_id={user_id} =======")

def handle_group_leave(user_id: int, chat_id: int = None):
    """Обработка выхода пользователя из группы"""
    logger.info(f"[DEBUG] handle_group_leave для user_id={user_id}")
    update_group_status(user_id, False)
    
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not user:
        user = {'first_name': 'Неизвестный', 'username': None}
    
    # ИСПРАВЛЕНО - без Markdown
    message_text = (
        f"⚠️ Пользователь вышел из группы:\n"
        f"ID: {user_id}\n"
        f"Имя: {user['first_name'] or '-'}\n"
        f"Ник: @{user['username'] or '-'}"
    )
    
    for aid in ADMIN_IDS:
        try:
            bot.send_message(
                aid,
                message_text,
                parse_mode=None  # ОТКЛЮЧАЕМ Markdown
            )
        except Exception as e:
            logger.debug(f"Не удалось уведомить админа: {e}")

# ---------- ОСНОВНЫЕ ФУНКЦИИ ДЛЯ АНКЕТ ----------
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

def get_user_state(user_id: int):
    return db_execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,), fetchone=True)

def update_user_state(user_id: int, **kwargs):
    existing = get_user_state(user_id)
    if existing:
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [user_id]
        db_execute(f"UPDATE user_state SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", values)
    else:
        columns = ["user_id"] + list(kwargs.keys())
        placeholders = ["?"] * (len(kwargs) + 1)
        values = [user_id] + list(kwargs.values())
        db_execute(f"INSERT INTO user_state ({', '.join(columns)}) VALUES ({', '.join(placeholders)})", values)

def create_application(user_id: int, section: str) -> int:
    app_id = db_execute(
        "INSERT INTO applications (user_id, section) VALUES (?, ?)",
        (user_id, section),
        return_id=True
    )
    update_user_state(user_id, current_app_id=app_id, awaiting_media_type="normal")
    return app_id

def get_application(app_id: int):
    return db_execute("SELECT * FROM applications WHERE id = ?", (app_id,), fetchone=True)

def get_application_media(app_id: int):
    return db_execute("SELECT * FROM media WHERE application_id = ? ORDER BY kind, created_at", (app_id,), fetchall=True)

def add_media_to_app(app_id: int, media_type: str, kind: str, file_id: str):
    db_execute(
        "INSERT INTO media (application_id, media_type, kind, file_id) VALUES (?, ?, ?, ?)",
        (app_id, media_type, kind, file_id)
    )

def submit_application(app_id: int):
    db_execute("UPDATE applications SET status = 1 WHERE id = ?", (app_id,))
    app = get_application(app_id)
    update_user_state(app['user_id'], current_app_id=None, awaiting_media_type=None)

def notify_admins_new_application(app_id: int):
    app = get_application(app_id)
    if not app:
        return
    uid = app['user_id']
    user = get_user(uid)
    
    # ИСПРАВЛЕНО - без Markdown
    text = (
        f"📨 Новая анкета #{app_id}\n"
        f"Пользователь: {uid} ({user['first_name'] or '-'}) @{user['username'] or '-'}\n"
        f"Раздел: {app['section']}\n"
    )
    
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
            bot.send_message(aid, text, reply_markup=kb, parse_mode=None)
        except Exception as e:
            logger.error(f"Ошибка отправки админу {aid}: {e}")

def notify_user_about_application(app_id: int, message: str):
    app = get_application(app_id)
    if app:
        try:
            bot.send_message(app['user_id'], message, parse_mode=None)
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {app['user_id']}: {e}")

# ---------- ОБРАБОТЧИКИ ГРУППЫ ----------
@bot.message_handler(content_types=["new_chat_members"])
def handle_new_chat_members(message):
    """Обработка новых участников группы"""
    logger.info(f"[DEBUG] Получено событие new_chat_members в чате {message.chat.id}")
    for new_member in message.new_chat_members:
        if not new_member.is_bot:
            logger.info(f"[DEBUG] Обрабатываю нового участника: {new_member.id} ({new_member.first_name})")
            handle_group_join(new_member.id, message.chat.id)

@bot.message_handler(content_types=["left_chat_member"])
def handle_left_chat_member(message):
    """Обработка выхода участника из группы"""
    logger.info(f"[DEBUG] Получено событие left_chat_member в чате {message.chat.id}")
    if not message.left_chat_member.is_bot:
        logger.info(f"[DEBUG] Обрабатываю выход участника: {message.left_chat_member.id}")
        handle_group_leave(message.left_chat_member.id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(("gallow_", "gdeny_")))
def handle_group_decision(call):
    """Обработка решений по группе"""
    logger.info(f"[DEBUG] Получен колбэк: {call.data} от пользователя {call.from_user.id}")
    
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
            
            db_execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (user_id,))
            
            bot.answer_callback_query(call.id, "Вход разрешен (без верификации)")
            try:
                bot.send_message(user_id, "✅ Вам разрешен вход в группу. Верификация не требуется.", parse_mode=None)
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
                bot.send_message(user_id, "📝 Для доступа к группе требуется пройти верификацию. Напишите /start", parse_mode=None)
            except:
                pass
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"✅ Решение принято для пользователя {user_id}",
                parse_mode=None
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
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"❌ Вход запрещен для {user_id}",
                parse_mode=None
            )
        except:
            pass

# ---------- ОСНОВНЫЕ ОБРАБОТЧИКИ АНКЕТ ----------
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
        
        if group_status['in_group'] and group_status['verification_required']:
            if user['status'] == 'pending':
                handle_group_join(uid, message.chat.id)
                bot.send_message(uid, 
                    "⚠️ Вы в группе, но не прошли верификацию.\n"
                    "Администраторы получили уведомление.\n"
                    "Ожидайте решения.",
                    parse_mode=None
                )
                return
    
    if user['status'] == 'banned':
        bot.send_message(uid, "🚫 Вы заблокированы.", parse_mode=None)
        return
    
    if user['status'] == 'approved':
        bot.send_message(uid, "✅ Доступ открыт.", parse_mode=None)
        return
    
    # Показываем интерфейс для создания анкеты
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📝 Создать анкету", callback_data="create_app"))
    bot.send_message(uid, "📝 Вы в режиме ожидания (pending).", reply_markup=kb, parse_mode=None)

@bot.callback_query_handler(func=lambda call: call.data == "create_app")
def callback_create_app(call):
    uid = call.from_user.id
    user = get_user(uid)
    
    if user['status'] == 'banned':
        bot.answer_callback_query(call.id, "Вы заблокированы.", show_alert=True)
        return
    
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Парм", callback_data="sec_parm"),
        InlineKeyboardButton("Будуар", callback_data="sec_buduar"),
        InlineKeyboardButton("Гараж", callback_data="sec_garage"),
        InlineKeyboardButton("Болталка", callback_data="sec_chat")
    )
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Выберите раздел для анкеты:",
        reply_markup=kb,
        parse_mode=None
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def callback_select_section(call):
    uid = call.from_user.id
    section = call.data.replace("sec_", "")
    
    # Создаем новую анкету
    app_id = create_application(uid, section)
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Обычные фото/видео", callback_data=f"add_normal_{app_id}"),
        InlineKeyboardButton("➕ Интим фото/видео", callback_data=f"add_intimate_{app_id}")
    )
    kb.add(InlineKeyboardButton("✅ Отправить на модерацию", callback_data=f"submit_app_{app_id}"))
    kb.add(InlineKeyboardButton("🔄 Начать заново", callback_data=f"reset_app_{app_id}"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📝 Создана анкета #{app_id} для раздела '{section}'\n\n"
             f"Добавьте материалы:\n"
             f"• Обычные фото/видео - ваши фото, видео, гифки\n"
             f"• Интим фото/видео - контент 18+\n\n"
             f"После добавления всех материалов нажмите 'Отправить на модерацию'.",
        reply_markup=kb,
        parse_mode=None
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith(("add_normal_", "add_intimate_")))
def callback_add_media_type(call):
    app_id = int(call.data.split("_")[-1])
    kind = "normal" if "normal" in call.data else "intimate"
    
    # Обновляем состояние пользователя
    update_user_state(call.from_user.id, current_app_id=app_id, awaiting_media_type=kind)
    
    bot.answer_callback_query(
        call.id,
        f"Теперь отправьте фото, видео или GIF для {kind} части",
        show_alert=True
    )
    
    # Отправляем сообщение с инструкцией
    bot.send_message(
        call.from_user.id,
        f"📤 Отправьте фото, видео или GIF для {'обычной' if kind == 'normal' else 'интимной'} части анкеты #{app_id}\n"
        f"Можно отправить несколько файлов.\n"
        f"Когда закончите, вернитесь в меню анкеты.",
        parse_mode=None
    )

@bot.message_handler(content_types=["photo", "video", "animation"])
def handle_media(message):
    uid = message.from_user.id
    state = get_user_state(uid)
    
    if not state or not state['current_app_id'] or not state['awaiting_media_type']:
        bot.reply_to(message, "Сначала выберите тип контента в меню анкеты.", parse_mode=None)
        return
    
    app_id = state['current_app_id']
    kind = state['awaiting_media_type']
    
    # Определяем тип медиа
    if message.content_type == "photo":
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.content_type == "video":
        file_id = message.video.file_id
        media_type = "video"
    elif message.content_type == "animation":
        file_id = message.animation.file_id
        media_type = "animation"
    else:
        return
    
    # Сохраняем в БД
    add_media_to_app(app_id, media_type, kind, file_id)
    
    bot.reply_to(message, f"✅ {media_type} добавлен в {kind} часть анкеты #{app_id}", parse_mode=None)

@bot.callback_query_handler(func=lambda call: call.data.startswith("submit_app_"))
def callback_submit_app(call):
    app_id = int(call.data.split("_")[-1])
    
    # Проверяем, есть ли медиа в анкете
    media = get_application_media(app_id)
    if not media:
        bot.answer_callback_query(call.id, "Добавьте хотя бы один файл перед отправкой.", show_alert=True)
        return
    
    # Отправляем на модерацию
    submit_application(app_id)
    notify_admins_new_application(app_id)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Анкета #{app_id} отправлена на модерацию.\nОжидайте решения администратора.",
        parse_mode=None
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("reset_app_"))
def callback_reset_app(call):
    app_id = int(call.data.split("_")[-1])
    
    # Удаляем анкету и все медиа
    db_execute("DELETE FROM media WHERE application_id = ?", (app_id,))
    db_execute("DELETE FROM applications WHERE id = ?", (app_id,))
    
    # Сбрасываем состояние пользователя
    update_user_state(call.from_user.id, current_app_id=None, awaiting_media_type=None)
    
    bot.answer_callback_query(call.id, "Анкета удалена. Начните заново.", show_alert=True)
    
    # Возвращаем к выбору раздела
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Парм", callback_data="sec_parm"),
        InlineKeyboardButton("Будуар", callback_data="sec_buduar"),
        InlineKeyboardButton("Гараж", callback_data="sec_garage"),
        InlineKeyboardButton("Болталка", callback_data="sec_chat")
    )
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Выберите раздел для анкеты:",
        reply_markup=kb,
        parse_mode=None
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith(("appr_", "rej_", "fix_", "view_")))
def handle_moderation_decision(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет прав", show_alert=True)
        return
    
    action = call.data.split("_")[0]
    app_id = int(call.data.split("_")[1])
    
    app = get_application(app_id)
    if not app:
        bot.answer_callback_query(call.id, "Анкета не найдена", show_alert=True)
        return
    
    if action == "view":
        # Просмотр анкеты
        media = get_application_media(app_id)
        if not media:
            bot.answer_callback_query(call.id, "В анкете нет медиа", show_alert=True)
            return
        
        # Отправляем первое медиа
        first_media = media[0]
        caption = f"Анкета #{app_id}\nРаздел: {app['section']}\nПользователь: {app['user_id']}\nМедиа: {len(media)} шт."
        
        if first_media['media_type'] == 'photo':
            bot.send_photo(call.from_user.id, first_media['file_id'], caption=caption, parse_mode=None)
        elif first_media['media_type'] == 'video':
            bot.send_video(call.from_user.id, first_media['file_id'], caption=caption, parse_mode=None)
        elif first_media['media_type'] == 'animation':
            bot.send_animation(call.from_user.id, first_media['file_id'], caption=caption, parse_mode=None)
        
        # Отправляем остальные медиа если есть
        for m in media[1:]:
            if m['media_type'] == 'photo':
                bot.send_photo(call.from_user.id, m['file_id'], parse_mode=None)
            elif m['media_type'] == 'video':
                bot.send_video(call.from_user.id, m['file_id'], parse_mode=None)
            elif m['media_type'] == 'animation':
                bot.send_animation(call.from_user.id, m['file_id'], parse_mode=None)
        
        bot.answer_callback_query(call.id, f"Отправлено {len(media)} медиа")
        return
    
    # Обновляем статус анкеты
    now = datetime.now().isoformat()
    
    if action == "appr":
        db_execute(
            "UPDATE applications SET status = 2, moderator_id = ?, moderated_at = ? WHERE id = ?",
            (call.from_user.id, now, app_id)
        )
        db_execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (app['user_id'],))
        
        # Если пользователь в группе, снимаем требование верификации
        group_status = get_user_group_status(app['user_id'])
        if group_status['in_group']:
            db_execute("UPDATE group_tracking SET verification_required = 0 WHERE user_id = ?", (app['user_id'],))
        
        notify_user_about_application(app_id, "✅ Ваша анкета одобрена! Доступ открыт.")
        bot.answer_callback_query(call.id, "Анкета одобрена")
        
    elif action == "rej":
        db_execute(
            "UPDATE applications SET status = 3, moderator_id = ?, moderated_at = ? WHERE id = ?",
            (call.from_user.id, now, app_id)
        )
        notify_user_about_application(app_id, "❌ Ваша анкета отклонена.")
        bot.answer_callback_query(call.id, "Анкета отклонена")
        
    elif action == "fix":
        db_execute(
            "UPDATE applications SET status = 4, moderator_id = ?, moderated_at = ? WHERE id = ?",
            (call.from_user.id, now, app_id)
        )
        notify_user_about_application(app_id, "✏️ Требуются правки в анкете. Свяжитесь с администратором.")
        bot.answer_callback_query(call.id, "Запрошены правки")
    
    # Обновляем сообщение админа
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{'✅' if action == 'appr' else '❌' if action == 'rej' else '✏️'} "
                 f"Анкета #{app_id} {'одобрена' if action == 'appr' else 'отклонена' if action == 'rej' else 'требует правок'}",
            parse_mode=None
        )
    except:
        pass

# ---------- ОТЛАДОЧНЫЕ КОМАНДЫ ----------
@bot.message_handler(commands=["debug_user"])
def debug_user_cmd(message):
    """Отладочная команда для проверки статуса пользователя"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        user_id = int(message.text.split()[1]) if len(message.text.split()) > 1 else message.from_user.id
    except:
        user_id = message.from_user.id
    
    user = get_user(user_id)
    group_status = get_user_group_status(user_id)
    
    response = f"🔍 ДЕБАГ пользователя {user_id}:\n"
    response += f"В БД users: {user}\n"
    response += f"В БД group_tracking: {group_status}\n"
    
    bot.reply_to(message, response, parse_mode=None)

@bot.message_handler(commands=["reset_user"])
def reset_user_cmd(message):
    """Сбросить пользователя в БД для тестирования"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        user_id = int(message.text.split()[1])
    except:
        bot.reply_to(message, "Использование: /reset_user USER_ID", parse_mode=None)
        return
    
    db_execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    db_execute("DELETE FROM group_tracking WHERE user_id = ?", (user_id,))
    
    bot.reply_to(message, f"✅ Пользователь {user_id} полностью сброшен в БД", parse_mode=None)

@bot.message_handler(commands=["ping"])
def ping_cmd(message):
    """Проверка работы бота"""
    bot.reply_to(message, "🏓 Бот работает!", parse_mode=None)

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
    
    logger.info("Запуск бота с исправлениями Markdown...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error("Критическая ошибка: %s", e)
        sys.exit(1)
