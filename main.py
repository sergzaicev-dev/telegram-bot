#!/usr/bin/env python3
# coding: utf-8
"""
Telegram moderation bot - упрощённая версия с 3 разделами и пошаговой анкетой
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
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = "8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI"
ADMIN_IDS = [5064426902]
GROUP_CHAT_ID = -1003262980832

# Три основных раздела (можно менять названия)
SECTIONS = {
    'pairs': 'Пары',
    'garage': 'Гараж', 
    'boudoir': 'Будуар'
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)  # Без разметки

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
            description TEXT DEFAULT '',
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
            file_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
        )
        """)
        
        # Новая таблица для пошагового состояния
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            current_section TEXT,
            step INTEGER DEFAULT 0,
            photo1_file_id TEXT,
            photo2_file_id TEXT,
            description TEXT DEFAULT '',
            last_action TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # Таблица для группы
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
        db_execute("""
            INSERT INTO group_tracking 
            (user_id, in_group, last_seen_in_group, join_count, last_join_time, verification_required)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, 1 if in_group else 0, now, 1 if in_group else 0, now if in_group else None, 1))
    else:
        if in_group and not tracking['in_group']:
            join_count = tracking['join_count'] + 1
            verification_required = 1 if force_verification else tracking['verification_required']
            
            db_execute("""
                UPDATE group_tracking 
                SET in_group = 1, last_seen_in_group = ?, 
                    join_count = ?, last_join_time = ?,
                    verification_required = ?
                WHERE user_id = ?
            """, (now, join_count, now, verification_required, user_id))
        elif not in_group and tracking['in_group']:
            db_execute("""
                UPDATE group_tracking 
                SET in_group = 0 
                WHERE user_id = ?
            """, (user_id,))

def get_user_group_status(user_id: int) -> Dict[str, Any]:
    """Получить статус пользователя в группе"""
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
    """Обработка вступления пользователя в группу"""
    logger.info(f"[GROUP] Пользователь {user_id} вступил в группу")
    
    # Создаем/обновляем запись пользователя
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not user:
        try:
            member_info = bot.get_chat_member(chat_id, user_id) if chat_id else None
            username = member_info.user.username if member_info else None
            first_name = member_info.user.first_name if member_info else None
            last_name = member_info.user.last_name if member_info else None
            
            db_execute("""
                INSERT INTO users (user_id, username, first_name, last_name, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (user_id, username, first_name, last_name))
        except:
            db_execute("""
                INSERT INTO users (user_id, status) VALUES (?, 'pending')
            """, (user_id,))
    
    # Обновляем статус в группе
    update_group_status(user_id, True, force_verification=True)
    
    # ОГРАНИЧИВАЕМ ПОЛЬЗОВАТЕЛЯ В ГРУППЕ
    if chat_id:
        try:
            permissions = ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False
            )
            
            bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=permissions,
                until_date=int(time.time()) + 86400  # 24 часа
            )
            logger.info(f"[GROUP] Пользователь {user_id} ограничен в правах")
        except Exception as e:
            logger.error(f"[GROUP] Ошибка ограничения прав: {e}")
    
    # Отправляем уведомление админам
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not user:
        user = {'first_name': 'Неизвестный', 'username': None, 'status': 'pending'}
    
    group_status = get_user_group_status(user_id)
    
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
    
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, message_text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {aid}: {e}")

def handle_group_leave(user_id: int, chat_id: int = None):
    """Обработка выхода пользователя из группы"""
    logger.info(f"[GROUP] Пользователь {user_id} вышел из группы")
    update_group_status(user_id, False)
    
    user = db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not user:
        user = {'first_name': 'Неизвестный', 'username': None}
    
    message_text = (
        f"⚠️ Пользователь вышел из группы:\n"
        f"ID: {user_id}\n"
        f"Имя: {user['first_name'] or '-'}\n"
        f"Ник: @{user['username'] or '-'}"
    )
    
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, message_text)
        except:
            pass

# ---------- ФУНКЦИИ ДЛЯ АНКЕТ ----------
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

def reset_user_state(user_id: int):
    db_execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))

def create_application(user_id: int, section: str, description: str, photo1_file_id: str, photo2_file_id: str) -> int:
    """Создать анкету с двумя фото и описанием"""
    # Создаем анкету
    app_id = db_execute(
        "INSERT INTO applications (user_id, section, description) VALUES (?, ?, ?)",
        (user_id, section, description),
        return_id=True
    )
    
    # Сохраняем фото 1
    db_execute(
        "INSERT INTO media (application_id, media_type, file_id) VALUES (?, ?, ?)",
        (app_id, "photo", photo1_file_id)
    )
    
    # Сохраняем фото 2
    db_execute(
        "INSERT INTO media (application_id, media_type, file_id) VALUES (?, ?, ?)",
        (app_id, "photo", photo2_file_id)
    )
    
    # Сбрасываем состояние пользователя
    reset_user_state(user_id)
    
    return app_id

def get_application(app_id: int):
    return db_execute("SELECT * FROM applications WHERE id = ?", (app_id,), fetchone=True)

def get_application_media(app_id: int):
    return db_execute("SELECT * FROM media WHERE application_id = ? ORDER BY created_at", (app_id,), fetchall=True)

def submit_application(app_id: int):
    db_execute("UPDATE applications SET status = 1 WHERE id = ?", (app_id,))

def notify_admins_new_application(app_id: int):
    """Уведомить админов о новой анкете с превью первого фото"""
    app = get_application(app_id)
    if not app:
        return
    
    uid = app['user_id']
    user = get_user(uid)
    media = get_application_media(app_id)
    
    if not media or len(media) < 2:
        logger.error(f"Анкета #{app_id} имеет недостаточно медиа")
        return
    
    # Получаем первое фото для превью
    first_photo = media[0]
    
    caption = (
        f"📨 Новая анкета #{app_id}\n"
        f"Пользователь: {uid} ({user['first_name'] or '-'})\n"
        f"Ник: @{user['username'] or '-'}\n"
        f"Раздел: {SECTIONS.get(app['section'], app['section'])}\n"
    )
    
    if app['description']:
        caption += f"Описание: {app['description']}\n"
    
    caption += f"Фото: {len(media)} шт."
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"appr_{app_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{app_id}"),
    )
    kb.add(
        InlineKeyboardButton("✏️ Запросить правки", callback_data=f"fix_{app_id}"),
        InlineKeyboardButton(f"👁️ Все фото ({len(media)})", callback_data=f"view_{app_id}")
    )
    
    for aid in ADMIN_IDS:
        try:
            bot.send_photo(aid, first_photo['file_id'], caption=caption, reply_markup=kb)
        except Exception as e:
            logger.error(f"Ошибка отправки админу {aid}: {e}")

def notify_user_about_application(app_id: int, message: str):
    app = get_application(app_id)
    if app:
        try:
            bot.send_message(app['user_id'], message)
        except:
            pass

# ---------- ОБРАБОТЧИКИ ГРУППЫ ----------
@bot.message_handler(content_types=["new_chat_members"])
def handle_new_chat_members(message):
    """Обработка новых участников группы"""
    for new_member in message.new_chat_members:
        if not new_member.is_bot:
            handle_group_join(new_member.id, message.chat.id)

@bot.message_handler(content_types=["left_chat_member"])
def handle_left_chat_member(message):
    """Обработка выхода участника из группы"""
    if not message.left_chat_member.is_bot:
        handle_group_leave(message.left_chat_member.id, message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith(("gallow_", "gdeny_")))
def handle_group_decision(call):
    """Обработка решений по группе"""
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет прав", show_alert=True)
        return
    
    parts = call.data.split("_")
    action_type = parts[0]
    decision = parts[1] if len(parts) > 2 else None
    user_id = int(parts[-1])
    
    now = datetime.now().isoformat()
    
    if action_type == "gallow":
        if decision == "noverify":
            db_execute("""
                UPDATE group_tracking 
                SET verification_required = 0,
                    admin_decision = 'allow_no_verify',
                    decided_by = ?, decided_at = ?
                WHERE user_id = ?
            """, (call.from_user.id, now, user_id))
            
            db_execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (user_id,))
            
            bot.answer_callback_query(call.id, "Вход разрешен (без верификации)")
            
            # СНИМАЕМ ОГРАНИЧЕНИЯ В ГРУППЕ
            try:
                permissions = ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                )
                bot.restrict_chat_member(GROUP_CHAT_ID, user_id, permissions=permissions)
            except:
                pass
            
        elif decision == "verify":
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
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"✅ Решение принято для пользователя {user_id}"
            )
        except:
            pass
    
    elif action_type == "gdeny":
        db_execute("""
            UPDATE group_tracking 
            SET verification_required = 1,
                admin_decision = 'deny',
                decided_by = ?, decided_at = ?
            WHERE user_id = ?
        """, (call.from_user.id, now, user_id))
        
        db_execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (user_id,))
        
        bot.answer_callback_query(call.id, "Вход запрещен")
        
        # Кикаем из группы
        try:
            bot.ban_chat_member(GROUP_CHAT_ID, user_id)
            time.sleep(1)
            bot.unban_chat_member(GROUP_CHAT_ID, user_id)
        except:
            pass
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=f"❌ Вход запрещен для {user_id}"
            )
        except:
            pass

# ---------- ОСНОВНЫЕ ОБРАБОТЧИКИ АНКЕТ ----------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    """Начало работы с ботом"""
    uid = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    
    ensure_user(uid, username, first_name, last_name)
    user = get_user(uid)
    
    # Если пользователь в группе
    if message.chat.type in ['group', 'supergroup']:
        group_status = get_user_group_status(uid)
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
    
    # Сбрасываем состояние и показываем выбор раздела
    reset_user_state(uid)
    
    kb = InlineKeyboardMarkup(row_width=1)
    for key, name in SECTIONS.items():
        kb.add(InlineKeyboardButton(name, callback_data=f"sec_{key}"))
    
    bot.send_message(uid, "📝 Выберите раздел для анкеты:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def callback_select_section(call):
    """Выбор раздела - шаг 1"""
    uid = call.from_user.id
    section_key = call.data.replace("sec_", "")
    
    if section_key not in SECTIONS:
        bot.answer_callback_query(call.id, "Неизвестный раздел", show_alert=True)
        return
    
    # Начинаем новую анкету
    update_user_state(
        user_id=uid,
        current_section=section_key,
        step=1,
        photo1_file_id=None,
        photo2_file_id=None,
        description=''
    )
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"📸 Шаг 1/4\nЗагрузите первое фото для раздела '{SECTIONS[section_key]}':"
    )
    bot.answer_callback_query(call.id, "Теперь загрузите первое фото")

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    """Обработка загрузки фото"""
    uid = message.from_user.id
    state = get_user_state(uid)
    
    if not state or state['step'] == 0:
        bot.reply_to(message, "Сначала выберите раздел для анкеты (команда /start)")
        return
    
    file_id = message.photo[-1].file_id
    
    if state['step'] == 1:
        # Первое фото
        update_user_state(
            user_id=uid,
            step=2,
            photo1_file_id=file_id
        )
        
        bot.reply_to(message, "✅ Первое фото принято!\n\n📸 Шаг 2/4\nТеперь загрузите второе фото:")
        
    elif state['step'] == 2:
        # Второе фото
        update_user_state(
            user_id=uid,
            step=3,
            photo2_file_id=file_id
        )
        
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✏️ Добавить описание", callback_data="add_description"),
            InlineKeyboardButton("⏭️ Пропустить", callback_data="skip_description")
        )
        
        bot.reply_to(message, "✅ Второе фото принято!\n\n📝 Шаг 3/4\nХотите добавить описание к анкете?", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data in ["add_description", "skip_description"])
def callback_description_choice(call):
    """Выбор: добавить описание или пропустить"""
    uid = call.from_user.id
    state = get_user_state(uid)
    
    if not state or state['step'] != 3:
        bot.answer_callback_query(call.id, "Ошибка состояния", show_alert=True)
        return
    
    if call.data == "skip_description":
        # Пропускаем описание
        update_user_state(user_id=uid, step=4)
        
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Отправить на модерацию", callback_data="submit_final"))
        kb.add(InlineKeyboardButton("🔄 Начать заново", callback_data="restart"))
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="📋 Анкета готова:\n"
                 f"Раздел: {SECTIONS.get(state['current_section'], state['current_section'])}\n"
                 f"Фото: 2 шт.\n"
                 f"Описание: не добавлено\n\n"
                 f"Отправляем на модерацию?",
            reply_markup=kb
        )
        
    else:  # add_description
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="📝 Шаг 4/4\nНапишите описание для анкеты (или /cancel для отмены):"
        )
        bot.answer_callback_query(call.id, "Напишите описание")

@bot.message_handler(func=lambda m: m.text and m.text != "/cancel")
def handle_description(message):
    """Обработка текста описания"""
    uid = message.from_user.id
    state = get_user_state(uid)
    
    if not state or state['step'] != 3:
        return
    
    description = message.text.strip()
    
    update_user_state(
        user_id=uid,
        step=4,
        description=description
    )
    
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Отправить на модерацию", callback_data="submit_final"))
    kb.add(InlineKeyboardButton("🔄 Начать заново", callback_data="restart"))
    
    bot.reply_to(message, 
        f"📋 Анкета готова:\n"
        f"Раздел: {SECTIONS.get(state['current_section'], state['current_section'])}\n"
        f"Фото: 2 шт.\n"
        f"Описание: {description[:50]}{'...' if len(description) > 50 else ''}\n\n"
        f"Отправляем на модерацию?",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data == "restart")
def callback_restart(call):
    """Начать анкету заново"""
    reset_user_state(call.from_user.id)
    
    kb = InlineKeyboardMarkup(row_width=1)
    for key, name in SECTIONS.items():
        kb.add(InlineKeyboardButton(name, callback_data=f"sec_{key}"))
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="📝 Выберите раздел для анкеты:",
        reply_markup=kb
    )
    bot.answer_callback_query(call.id, "Начинаем заново")

@bot.callback_query_handler(func=lambda call: call.data == "submit_final")
def callback_submit_final(call):
    """Отправка анкеты на модерацию"""
    uid = call.from_user.id
    state = get_user_state(uid)
    
    if not state or state['step'] != 4:
        bot.answer_callback_query(call.id, "Анкета не готова", show_alert=True)
        return
    
    if not state['photo1_file_id'] or not state['photo2_file_id']:
        bot.answer_callback_query(call.id, "Не хватает фото", show_alert=True)
        return
    
    # Создаем анкету
    app_id = create_application(
        user_id=uid,
        section=state['current_section'],
        description=state['description'],
        photo1_file_id=state['photo1_file_id'],
        photo2_file_id=state['photo2_file_id']
    )
    
    # Отправляем на модерацию
    submit_application(app_id)
    notify_admins_new_application(app_id)
    
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Анкета #{app_id} отправлена на модерацию!\nОжидайте решения администратора."
    )
    bot.answer_callback_query(call.id, "Анкета отправлена")

@bot.callback_query_handler(func=lambda call: call.data.startswith(("appr_", "rej_", "fix_", "view_")))
def handle_moderation_decision(call):
    """Обработка решений модерации"""
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
        # Просмотр всех фото
        media = get_application_media(app_id)
        if not media or len(media) < 2:
            bot.answer_callback_query(call.id, "В анкете нет фото", show_alert=True)
            return
        
        # Отправляем первое фото с описанием
        caption = (
            f"Анкета #{app_id}\n"
            f"Раздел: {SECTIONS.get(app['section'], app['section'])}\n"
            f"Пользователь: {app['user_id']}\n"
        )
        if app['description']:
            caption += f"Описание: {app['description']}\n"
        
        try:
            bot.send_photo(call.from_user.id, media[0]['file_id'], caption=caption)
            # Отправляем второе фото
            bot.send_photo(call.from_user.id, media[1]['file_id'])
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            bot.answer_callback_query(call.id, "Ошибка отправки фото", show_alert=True)
            return
        
        bot.answer_callback_query(call.id, f"Отправлено {len(media)} фото")
        return
    
    # Обновляем статус анкеты
    now = datetime.now().isoformat()
    
    if action == "appr":
        db_execute(
            "UPDATE applications SET status = 2, moderator_id = ?, moderated_at = ? WHERE id = ?",
            (call.from_user.id, now, app_id)
        )
        db_execute("UPDATE users SET status = 'approved' WHERE user_id = ?", (app['user_id'],))
        
        # Снимаем ограничения в группе если пользователь там есть
        group_status = get_user_group_status(app['user_id'])
        if group_status['in_group']:
            db_execute("UPDATE group_tracking SET verification_required = 0 WHERE user_id = ?", (app['user_id'],))
            try:
                permissions = ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                )
                bot.restrict_chat_member(GROUP_CHAT_ID, app['user_id'], permissions=permissions)
            except:
                pass
        
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
        notify_user_about_application(app_id, "✏️ Требуются правки в анкете. Создайте новую анкету через /start")
        bot.answer_callback_query(call.id, "Запрошены правки")
    
    # Обновляем сообщение админа
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{'✅' if action == 'appr' else '❌' if action == 'rej' else '✏️'} "
                 f"Анкета #{app_id} {'одобрена' if action == 'appr' else 'отклонена' if action == 'rej' else 'требует правок'}"
        )
    except:
        pass

# ---------- ОТЛАДОЧНЫЕ КОМАНДЫ ----------
@bot.message_handler(commands=["debug_user"])
def debug_user_cmd(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        user_id = int(message.text.split()[1]) if len(message.text.split()) > 1 else message.from_user.id
    except:
        user_id = message.from_user.id
    
    user = get_user(user_id)
    group_status = get_user_group_status(user_id)
    state = get_user_state(user_id)
    
    response = f"🔍 ДЕБАГ пользователя {user_id}:\n"
    response += f"В БД users: {user}\n"
    response += f"В БД group_tracking: {group_status}\n"
    response += f"Текущее состояние: {state}\n"
    
    bot.reply_to(message, response)

@bot.message_handler(commands=["reset_user"])
def reset_user_cmd(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    try:
        user_id = int(message.text.split()[1])
    except:
        bot.reply_to(message, "Использование: /reset_user USER_ID")
        return
    
    db_execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    db_execute("DELETE FROM group_tracking WHERE user_id = ?", (user_id,))
    reset_user_state(user_id)
    
    bot.reply_to(message, f"✅ Пользователь {user_id} полностью сброшен")

@bot.message_handler(commands=["ping"])
def ping_cmd(message):
    bot.reply_to(message, "🏓 Бот работает!")

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
    
    logger.info("Запуск упрощённого бота с 3 разделами...")
    logger.info(f"Разделы: {', '.join(SECTIONS.values())}")
    
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error("Критическая ошибка: %s", e)
        sys.exit(1)
