#!/usr/bin/env python3
# coding: utf-8
"""
Telegram moderation bot
- Статусы пользователя: pending, approved, banned
- Пользователь создаёт одну анкету (application) в статусе pending
- В анкете собираются медиа: media_type = normal | intimate (по одному и более)
- Анкета видна только админам; админ принимает единое решение:
    approve -> user.status = approved (полный доступ)
    reject  -> user.status = banned (полный бан)
    fix     -> возвращает возможность доработать анкету
- Разделы (menu) скрыты от пользователей в статусе pending/banned, видны только approved
- Пользователь может выбрать ОДИН раздел при создании анкеты; смена запрещена пока не сбросит админ/пользователь (по команде)
"""
import os
import sys
import logging
import threading
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
from flask import Flask, request
import signal
import time
import json

import telebot
from telebot import apihelper
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------- НАСТРОЙКИ ----------

# Токен бота (строго в кавычках)
BOT_TOKEN = "8485486677:AAHqx7YjGMn5pn2pDTADwllNDjJmYAK-KFI"

# ID администраторов (список чисел)
ADMIN_IDS = [5064426902]  # можешь добавить через запятую несколько ID

# Лимит частоты (минуты). Если не используешь — оставляй как есть.
RATE_LIMIT_MINUTES = 5

# Ключ для внутреннего API админов (можешь оставить любое значение)
ADMIN_API_KEY = "secret"

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ---------- БАЗА ДАННЫХ (потокобезопасно) ----------
DB_PATH = os.getenv("DB_PATH", "moderation_bot.db")
_db_lock = threading.Lock()

def _conn():
    # отдельное соединение на вызов
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db_lock:
        conn = _conn()
        cur = conn.cursor()
        # users: статус pending/approved/banned
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            status TEXT DEFAULT 'pending', -- pending|approved|banned
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # applications: одна заявка/пользователь (можно создавать новые, но только одна активная pending)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            status INTEGER DEFAULT 0, -- 0 pending, 1 approved, -1 rejected, 2 needs_fix
            moderator_id INTEGER,
            moderated_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """)
        # media: прикреплённые файлы к application
        cur.execute("""
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            media_type TEXT NOT NULL, -- normal | intimate
            kind TEXT NOT NULL,       -- photo | video | animation
            file_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
        )
        """)
        # user_state: временное состояние для загрузки медиа и выбора действий
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            current_app_id INTEGER,
            awaiting_media_type TEXT, -- normal | intimate | None
            last_action TEXT, -- for debug
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_app_user ON applications(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_media_app ON media(application_id)")
        conn.commit()
        conn.close()

init_db()

# ---------- Утилиты работы с БД ----------
def db_execute(query: str, params: Tuple = (), fetchone: bool = False, fetchall: bool = False, return_id: bool = False):
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

# ---------- Основные функции модели ----------
def ensure_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str] = ""):
    """Создать пользователя или обновить поля"""
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
        # уведомляем админов о новом пользователе
        notify_admins_new_user(user_id, username, first_name, last_name)

def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)

def set_user_status(user_id: int, status: str):
    db_execute("UPDATE users SET status = ?, last_activity = CURRENT_TIMESTAMP WHERE user_id = ?", (status, user_id))

def get_active_application_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    return db_execute("""
        SELECT * FROM applications WHERE user_id = ? AND status = 0 ORDER BY created_at DESC LIMIT 1
    """, (user_id,), fetchone=True)

def create_application(user_id: int, section: str) -> int:
    # запрещаем создавать новую pending, если уже есть одна
    active = get_active_application_for_user(user_id)
    if active:
        return active['id']
    app_id = db_execute("""
        INSERT INTO applications (user_id, section, status) VALUES (?, ?, 0)
    """, (user_id, section), return_id=True)
    # create/update user_state
    db_execute("""
        INSERT OR REPLACE INTO user_state (user_id, current_app_id, awaiting_media_type, last_action, updated_at)
        VALUES (?, ?, NULL, 'created_app', CURRENT_TIMESTAMP)
    """, (user_id, app_id))
    return app_id

def add_media(application_id: int, media_type: str, kind: str, file_id: str):
    return db_execute("""
        INSERT INTO media (application_id, media_type, kind, file_id) VALUES (?, ?, ?, ?)
    """, (application_id, media_type, kind, file_id), return_id=True)

def get_media_counts(application_id: int) -> Dict[str, int]:
    rows = db_execute("""
        SELECT media_type, COUNT(*) as cnt FROM media WHERE application_id = ? GROUP BY media_type
    """, (application_id,), fetchall=True)
    counts = {'normal': 0, 'intimate': 0}
    for r in rows:
        counts[r['media_type']] = r['cnt']
    return counts

def get_application(application_id: int) -> Optional[Dict[str, Any]]:
    return db_execute("SELECT * FROM applications WHERE id = ?", (application_id,), fetchone=True)

def set_application_status(application_id: int, new_status: int, moderator_id: Optional[int] = None):
    now = datetime.now().isoformat(sep=' ')
    db_execute("""
        UPDATE applications SET status = ?, moderator_id = ?, moderated_at = ? WHERE id = ?
    """, (new_status, moderator_id, now, application_id))
    # если approved -> переводим пользователя в approved
    app = get_application(application_id)
    if not app:
        return
    uid = app['user_id']
    if new_status == 1:
        set_user_status(uid, 'approved')
    elif new_status == -1:
        set_user_status(uid, 'banned')
    elif new_status == 2:
        # needs_fix -> оставляем пользователя pending
        set_user_status(uid, 'pending')

def get_user_state(user_id: int) -> Optional[Dict[str, Any]]:
    return db_execute("SELECT * FROM user_state WHERE user_id = ?", (user_id,), fetchone=True)

def set_user_state(user_id: int, current_app_id: Optional[int], awaiting_media_type: Optional[str], last_action: str):
    db_execute("""
        INSERT OR REPLACE INTO user_state (user_id, current_app_id, awaiting_media_type, last_action, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (user_id, current_app_id, awaiting_media_type, last_action))

def clear_user_state(user_id: int):
    db_execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))

def check_rate_limit(user_id: int) -> Tuple[bool, int]:
    """Последняя заявка (любая) — не раньше, чем RATE_LIMIT_MINUTES"""
    last = db_execute("""
        SELECT created_at FROM applications WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
    """, (user_id,), fetchone=True)
    if not last:
        return True, 0
    last_time = datetime.fromisoformat(last['created_at'])
    diff = datetime.now() - last_time
    minutes_passed = diff.total_seconds() / 60.0
    if minutes_passed < RATE_LIMIT_MINUTES:
        return False, int(RATE_LIMIT_MINUTES - minutes_passed) + 1
    return True, 0

def notify_admins_new_user(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
    text = (
        f"🆕 Новый пользователь: `{user_id}`\n"
        f"Имя: {first_name or '-'} {last_name or '-'}\n"
        f"Ник: @{username or '-'}\n"
        f"Статус: pending\n"
        f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text)
        except Exception as e:
            logger.debug("Не удалось уведомить админа %s: %s", aid, e)

def notify_admins_new_application(app_id: int):
    app = get_application(app_id)
    if not app:
        return
    uid = app['user_id']
    user = get_user(uid)
    text = (
        f"📨 Новая анкета #{app_id}\n"
        f"Пользователь: `{uid}` ({user['first_name'] or '-'}) @{user['username'] or '-'}\n"
        f"Раздел: {app['section']}\n"
        f"Время: {app['created_at']}\n"
    )
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"mod_app_appr_{app_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_app_rej_{app_id}"),
    )
    kb.add(
        InlineKeyboardButton("✏️ Запросить правки", callback_data=f"mod_app_fix_{app_id}"),
        InlineKeyboardButton("👁️ Просмотреть", callback_data=f"mod_app_view_{app_id}")
    )
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logger.debug("Не удалось отправить заявку админу %s: %s", aid, e)

# ---------- Клавиатуры ----------
def kb_start_pending():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📝 Создать анкету", callback_data="create_app"))
    kb.add(InlineKeyboardButton("ℹ️ Статус", callback_data="show_status"))
    return kb

def section_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("Пары", callback_data="sec_пары"),
        InlineKeyboardButton("Будуар", callback_data="sec_будуар"),
        InlineKeyboardButton("Гараж", callback_data="sec_гараж")
    )
    return kb

def kb_media_actions(application_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Добавить обычное", callback_data=f"add_normal_{application_id}"),
        InlineKeyboardButton("➕ Добавить интимное", callback_data=f"add_intimate_{application_id}")
    )
    kb.add(
        InlineKeyboardButton("✅ Готово (отправить на модерацию)", callback_data=f"submit_app_{application_id}"),
        InlineKeyboardButton("🔄 Сбросить анкету", callback_data=f"reset_app_{application_id}")
    )
    return kb

def kb_admin_main():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⏳ Ожидают", callback_data="admin_pending"),
        InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
    )
    kb.add(InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"))
    return kb

# ---------- Хендлеры ----------

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or ""
    last_name = message.from_user.last_name or ""
    ensure_user(uid, username, first_name, last_name)
    user = get_user(uid)
    # Забанен
    if user and user['status'] == 'banned':
        bot.send_message(uid, "🚫 Вы заблокированы и не можете использовать бота. Для вопросов обратитесь к администратору.")
        return
    # Approved -> показать разделы и инструкции
    if user and user['status'] == 'approved':
        text = (
            "✅ Доступ открыт. Вы можете работать во всех разделах.\n\n"
            "Выберите действие:\n"
            "- Отправить контент прямо в чат\n"
            "- /status — проверить статус\n"
            "- /my — мои анкеты"
        )
        bot.send_message(uid, text)  # можно добавить keyboard если нужно
        return
    # pending
    text = (
        "📝 Вы в режиме ожидания (pending).\n\n"
        "1) Нажмите «Создать анкету» — выберите раздел и загрузите фото.\n"
        "2) Анкета будет скрыта от остальных и отправлена админам.\n"
        "3) Админ принимает единое решение: одобрить / отклонить / запросить правки.\n\n"
        "⚠️ Пока анкета не одобрена — разделы скрыты, писать в общие разделы нельзя."
    )
    bot.send_message(uid, text, reply_markup=kb_start_pending())

@bot.callback_query_handler(func=lambda call: call.data == "show_status")
def cb_show_status(call):
    uid = call.from_user.id
    user = get_user(uid)
    if not user:
        bot.answer_callback_query(call.id, "Не найден пользователь", show_alert=True)
        return
    app = get_active_application_for_user(uid)
    app_text = f"Активная анкета: #{app['id']} / раздел: {app['section']}" if app else "Активная анкета: нет"
    bot.send_message(uid,
                     f"👤 ID: `{uid}`\nСтатус: {user['status']}\n{app_text}"
                     )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "create_app")
def cb_create_app(call):
    uid = call.from_user.id
    user = get_user(uid)
    if not user:
        bot.answer_callback_query(call.id, "Нужен /start сначала", show_alert=True)
        return
    if user['status'] == 'approved':
        bot.answer_callback_query(call.id, "У вас уже открыт доступ (approved).", show_alert=True)
        return
    if user['status'] == 'banned':
        bot.answer_callback_query(call.id, "Вы заблокированы.", show_alert=True)
        return
    # показать клавиатуру разделов
    bot.send_message(uid, "Выберите раздел для анкеты (можно выбрать только один):", reply_markup=section_kb())
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("sec_"))
def cb_section_select(call):
    uid = call.from_user.id
    user = get_user(uid)
    if not user:
        bot.answer_callback_query(call.id, "Нужен /start сначала", show_alert=True)
        return
    if user['status'] != 'pending':
        bot.answer_callback_query(call.id, "Нельзя создавать анкету в текущем статусе.", show_alert=True)
        return
    section = call.data.split("_", 1)[1]
    # rate limit (между созданием анкет)
    can_create, wait = check_rate_limit(uid)
    if not can_create:
        bot.answer_callback_query(call.id, f"Нельзя создавать новую анкету. Подождите {wait} мин.", show_alert=True)
        return
    app_id = create_application(uid, section)
    # send instructions and media keyboard
    text = (
        f"📝 Анкета #{app_id} создана. Раздел: *{section}*.\n\n"
        "Теперь нужно загрузить медиа:\n"
        "• Обычные фото — 1 или более\n"
        "• Интимные фото — 1 или более\n\n"
        "Порядок любой. Нажимайте кнопки ниже, чтобы добавить соответствующий тип и отправить файлы.\n"
        "Когда всё готово — нажмите *Готово (отправить на модерацию)*."
    )
    bot.send_message(uid, text, reply_markup=kb_media_actions(app_id))
    notify_admins_new_application(app_id)
    bot.answer_callback_query(call.id, "Анкета создана. Проверьте инструкции в личных сообщениях.")

@bot.callback_query_handler(func=lambda call: call.data.startswith(("add_normal_", "add_intimate_")))
def cb_add_media_start(call):
    uid = call.from_user.id
    parts = call.data.split("_")
    kind = parts[0]  # add
    media_tag = parts[1] if len(parts) > 1 else None
    # call.data formats: add_normal_{appid} or add_intimate_{appid}
    if call.data.startswith("add_normal_"):
        app_id = int(call.data.split("_", 2)[2])
        media_type = "normal"
    else:
        app_id = int(call.data.split("_", 2)[2])
        media_type = "intimate"
    # verify app exists and belongs to user and is pending
    app = get_application(app_id)
    if not app or app['user_id'] != uid or app['status'] != 0:
        bot.answer_callback_query(call.id, "Анкета не найдена или недоступна.", show_alert=True)
        return
    # set user_state awaiting media
    set_user_state(uid, app_id, media_type, f"awaiting_{media_type}")
    bot.send_message(uid, f"Отправьте файл(ы) для типа *{media_type}*. Поддерживаются: фото, видео, GIF. Можно отправлять несколько сообщений по одному файлу.")
    bot.answer_callback_query(call.id, f"Отправьте файлы для {media_type}")

@bot.message_handler(content_types=["photo", "video", "animation"])
def media_receive(message):
    uid = message.from_user.id
    user = get_user(uid)
    if not user:
        bot.reply_to(message, "Нужен /start сначала.")
        return
    if user['status'] == 'banned':
        bot.reply_to(message, "🚫 Вы заблокированы.")
        return
    state = get_user_state(uid)
    if not state or not state.get('current_app_id') or not state.get('awaiting_media_type'):
        bot.reply_to(message, "ℹ️ Сначала нажмите кнопку 'Добавить обычное' или 'Добавить интимное' в меню анкеты.")
        return
    app_id = state['current_app_id']
    app = get_application(app_id)
    if not app or app['user_id'] != uid or app['status'] != 0:
        bot.reply_to(message, "Анкета не найдена или уже отправлена.")
        return
    media_type = state['awaiting_media_type']  # normal | intimate
    # determine file_id and kind
    if message.content_type == 'photo':
        file_id = message.photo[-1].file_id
        kind = 'photo'
    elif message.content_type == 'video':
        file_id = message.video.file_id
        kind = 'video'
    elif message.content_type == 'animation':
        file_id = message.animation.file_id
        kind = 'animation'
    else:
        bot.reply_to(message, "Неподдерживаемый тип.")
        return
    mid = add_media(app_id, media_type, kind, file_id)
    if not mid:
        bot.reply_to(message, "Ошибка при сохранении файла.")
        return
    bot.reply_to(message, f"Файл сохранён (тип: {media_type}). Чтобы добавить другой тип — нажмите соответствующую кнопку. Готово — нажмите «Готово (отправить на модерацию)» в меню анкеты.")
    # обновим user_state.updated_at
    set_user_state(uid, app_id, media_type, f"added_media_{media_type}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("submit_app_"))
def cb_submit_app(call):
    uid = call.from_user.id
    app_id = int(call.data.split("_", 2)[2])
    app = get_application(app_id)
    if not app or app['user_id'] != uid:
        bot.answer_callback_query(call.id, "Анкета не найдена.", show_alert=True)
        return
    if app['status'] != 0:
        bot.answer_callback_query(call.id, "Анкета уже обработана.", show_alert=True)
        return
    counts = get_media_counts(app_id)
    if counts.get('normal', 0) < 1 or counts.get('intimate', 0) < 1:
        bot.answer_callback_query(call.id, "Нужно минимум 1 обычное и 1 интимное фото.", show_alert=True)
        return
    # Помечаем как pending (она уже pending), уведомляем админов (если не отправляли)
    notify_admins_new_application(app_id)
    # очистим состояние
    clear_user_state(uid)
    bot.send_message(uid, f"✅ Анкета #{app_id} отправлена на модерацию. Ожидайте решения администратора.")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("reset_app_"))
def cb_reset_app(call):
    uid = call.from_user.id
    app_id = int(call.data.split("_", 2)[2])
    app = get_application(app_id)
    if not app or app['user_id'] != uid:
        bot.answer_callback_query(call.id, "Анкета не найдена.", show_alert=True)
        return
    # удаляем медиа и саму анкету (пользователь может создать новую)
    db_execute("DELETE FROM media WHERE application_id = ?", (app_id,))
    db_execute("DELETE FROM applications WHERE id = ?", (app_id,))
    clear_user_state(uid)
    bot.send_message(uid, "🔄 Ваша анкета сброшена. Можете создать новую анкету.")
    bot.answer_callback_query(call.id)

# ---------- Модерация (админ) ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("mod_app_"))
def cb_mod_action(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет прав.", show_alert=True)
        return
    parts = call.data.split("_", 2)
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Некорректно.", show_alert=True)
        return
    action = parts[1]
    rest = parts[2]
    # форматы: mod_app_appr_{id}, mod_app_rej_{id}, mod_app_fix_{id}, mod_app_view_{id}
    if action == "appr":
        app_id = int(rest)
        process_mod_decision(call, app_id, "approve")
    elif action == "rej":
        app_id = int(rest)
        process_mod_decision(call, app_id, "reject")
    elif action == "fix":
        app_id = int(rest)
        process_mod_decision(call, app_id, "fix")
    elif action == "view":
        app_id = int(rest)
        admin_view_application(call, app_id)
    else:
        bot.answer_callback_query(call.id, "Неизвестная операция.", show_alert=True)

def process_mod_decision(call, app_id: int, decision: str):
    app = get_application(app_id)
    if not app:
        bot.answer_callback_query(call.id, "Анкета не найдена.", show_alert=True)
        return
    uid = app['user_id']
    # ensure there are both types
    counts = get_media_counts(app_id)
    if decision == "approve":
        if counts.get('normal', 0) < 1 or counts.get('intimate', 0) < 1:
            bot.answer_callback_query(call.id, "Анкета неполная (требуется обычное + интимное).", show_alert=True)
            return
        set_application_status(app_id, 1, call.from_user.id)
        # notify user
        try:
            bot.send_message(uid,
                             f"🎉 Ваша анкета #{app_id} одобрена. Вам открыт доступ ко всем разделам.")
        except Exception as e:
            logger.debug("Не удалось уведомить пользователя %s: %s", uid, e)
        bot.answer_callback_query(call.id, "Анкета одобрена.")
        # обновить сообщение модератора
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"✅ Анкета #{app_id} одобрена администратором {call.from_user.first_name}")
        except Exception:
            pass
    elif decision == "reject":
        # полный бан пользователя
        set_application_status(app_id, -1, call.from_user.id)
        set_user_status(uid, 'banned')
        try:
            bot.send_message(uid, f"❌ Ваша анкета #{app_id} отклонена. Вы заблокированы.")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Анкета отклонена и пользователь заблокирован.")
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"❌ Анкета #{app_id} отклонена. Пользователь заблокирован.")
        except Exception:
            pass
    elif decision == "fix":
        set_application_status(app_id, 2, call.from_user.id)  # needs_fix
        set_user_status(uid, 'pending')
        try:
            bot.send_message(uid, f"✏️ Анкета #{app_id} требует исправлений. Пожалуйста, добавьте/замените файлы и нажмите 'Готово'.")
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Запрошены правки.")
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=f"✏️ Анкета #{app_id} помечена как needs_fix.")
        except Exception:
            pass

def admin_view_application(call, app_id: int):
    app = get_application(app_id)
    if not app:
        bot.answer_callback_query(call.id, "Анкета не найдена.", show_alert=True)
        return
    medias = db_execute("SELECT * FROM media WHERE application_id = ?", (app_id,), fetchall=True)
    text = f"📋 Анкета #{app_id}\nПользователь: `{app['user_id']}`\nРаздел: {app['section']}\nСтатус: {app['status']}\n\nМедиа:\n"
    counts = get_media_counts(app_id)
    text += f"Обычных: {counts.get('normal',0)}, Интимных: {counts.get('intimate',0)}\n"
    try:
        bot.send_message(call.from_user.id, text)
        # отправляем медиа (если есть)
        for m in medias:
            try:
                if m['kind'] == 'photo':
                    bot.send_photo(call.from_user.id, m['file_id'])
                elif m['kind'] == 'video':
                    bot.send_video(call.from_user.id, m['file_id'])
                elif m['kind'] == 'animation':
                    bot.send_animation(call.from_user.id, m['file_id'])
            except Exception as e:
                logger.debug("Не удалось отправить медиа админу: %s", e)
    except Exception as e:
        logger.error("Ошибка при отправке заявки админу: %s", e)
    bot.answer_callback_query(call.id, "Отправлено в личку.")

# ---------- Доп. команды /admin, /status, /my, /reset ----------
@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        bot.reply_to(message, "Доступ запрещён.")
        return
    # show simple admin keyboard
    bot.reply_to(message, "Админ-панель:", reply_markup=kb_admin_main())

@bot.callback_query_handler(func=lambda call: call.data == "admin_pending")
def cb_admin_pending(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет прав", show_alert=True)
        return
    pending = db_execute("""
        SELECT a.id, a.user_id, a.section, a.created_at, u.username, u.first_name
        FROM applications a LEFT JOIN users u ON a.user_id = u.user_id
        WHERE a.status = 0 ORDER BY a.created_at DESC LIMIT 20
    """, (), fetchall=True)
    if not pending:
        bot.send_message(call.from_user.id, "Нет ожидающих анкет.")
        bot.answer_callback_query(call.id)
        return
    text = "⏳ Ожидающие анкеты:\n\n"
    for p in pending:
        text += f"#{p['id']} — {p['user_id']} ({p['username'] or '-'}) — {p['section']} — {p['created_at'][:16]}\n"
    bot.send_message(call.from_user.id, text)
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=["status"])
def cmd_status(message):
    uid = message.from_user.id
    user = get_user(uid)
    if not user:
        bot.reply_to(message, "Нужен /start")
        return
    app = get_active_application_for_user(uid)
    text = f"Статус: {user['status']}\n"
    if app:
        counts = get_media_counts(app['id'])
        text += f"Активная анкета #{app['id']}, раздел {app['section']}\nОбычных: {counts.get('normal',0)}, Интимных: {counts.get('intimate',0)}"
        bot.reply_to(message, text, reply_markup=kb_media_actions(app['id']))
    else:
        bot.reply_to(message, text)

@bot.message_handler(commands=["my"])
def cmd_my(message):
    uid = message.from_user.id
    rows = db_execute("""
        SELECT id, section, status, created_at FROM applications WHERE user_id = ? ORDER BY created_at DESC LIMIT 10
    """, (uid,), fetchall=True)
    if not rows:
        bot.reply_to(message, "У вас нет анкет.")
        return
    text = "Ваши анкеты:\n\n"
    status_map = {0: "pending", 1: "approved", -1: "rejected", 2: "needs_fix"}
    for r in rows:
        text += f"#{r['id']} — {r['section']} — {status_map.get(r['status'], r['status'])} — {r['created_at'][:16]}\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    uid = message.from_user.id
    # пользователь сбрасывает активную анкету (удаляем медиа и приложение)
    app = get_active_application_for_user(uid)
    if not app:
        bot.reply_to(message, "Активной анкеты нет.")
        return
    db_execute("DELETE FROM media WHERE application_id = ?", (app['id'],))
    db_execute("DELETE FROM applications WHERE id = ?", (app['id'],))
    clear_user_state(uid)
    bot.reply_to(message, "Анкета сброшена. Можете создать новую.")

# ---------- Flask health-check ----------
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

@app.route("/admin-stats")
def admin_stats():
    key = request.args.get("key")
    if not key or key != ADMIN_API_KEY:
        return {"error": "Unauthorized"}, 401
    total_users = db_execute("SELECT COUNT(*) as c FROM users", (), fetchone=True)['c']
    pending_apps = db_execute("SELECT COUNT(*) as c FROM applications WHERE status = 0", (), fetchone=True)['c']
    approved = db_execute("SELECT COUNT(*) as c FROM applications WHERE status = 1", (), fetchone=True)['c']
    return {
        "total_users": total_users,
        "pending_apps": pending_apps,
        "approved": approved,
        "timestamp": datetime.now().isoformat()
    }, 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ---------- Сигналы и запуск ----------
def signal_handler(signum, frame):
    logger.info("Получен сигнал %s. Завершение.", signum)
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    # Запуск Flask
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # polling
    logger.info("Запуск бота...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30, allowed_updates=['message', 'callback_query'])
    except Exception as e:
        logger.error("Критическая ошибка polling: %s", e)
        # уведомление админам
        for aid in ADMIN_IDS:
            try:
                bot.send_message(aid, f"🚨 Бот упал: {str(e)[:200]}")
            except Exception:
                pass
        sys.exit(1)


