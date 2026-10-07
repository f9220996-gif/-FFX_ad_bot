# -*- coding: utf-8 -*-
"""
ربات مدیریت گروه تلگرام — تک‌فایل، دوزبانه (فارسی / English)
Telegram group management bot — single file, bilingual (Persian / English)

اجرا / Run:  python bot.py
نیازها: requirements.txt + پوشه‌ی assets (فونت و پس‌زمینه‌ی قیمت‌ها)
متغیرهای محیطی / Env vars: BOT_TOKEN, CREATOR_ID, GOOGLE_API_KEY, OPENAI_API_KEY, STATS_DB_PATH
"""

import asyncio
import html
import io
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import jdatetime
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from telegram import (
    Update, ChatMemberUpdated, ChatPermissions, ChatMember,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InputMediaPhoto, InputMediaVideo, InputMediaAudio,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler, ContextTypes, filters,
    ApplicationHandlerStop, TypeHandler,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

try:
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib import font_manager, ticker
    from matplotlib import dates as mdates
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

try:
    import google.generativeai as genai
except ImportError:
    genai = None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GROUP_TYPES = ("group", "supergroup")

# ============================================================================
# تنظیمات / CONFIG
# ============================================================================
# توکن رو توی فایل .env یا متغیر محیطی BOT_TOKEN بذار، نه توی کد.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
CREATOR_ID = int(os.environ.get("CREATOR_ID", "7353819350"))
DB_PATH = os.path.join(BASE_DIR, "bot_database.db")

DEFAULT_BAD_WORDS = [
    "جنده", "حرومزاده", "پدرسگ", "مادرجنده", "ناموس", "کونی",
    "پدرتو", "مادرتو", "خواهرتو", "ممه", "کیر",
]
# فحش‌های انگلیسی (جدا از لیست دیتابیس؛ با تطبیق مرز کلمه، همیشه فعال)
EN_BAD_WORDS = [
    "fuck", "fucker", "fucking", "motherfucker", "shit", "bitch", "bastard",
    "asshole", "dick", "dickhead", "cunt", "pussy", "slut", "whore", "cock",
    "wanker", "prick", "twat",
]
DEFAULT_WARNING_TEXTS = {
    1: "فحش نده بی‌ادب. 1/3",
    2: "دوباره فحش دادی 2/3",
    3: "خفه بی‌ادب 3/3",
}
DEFAULT_WARNING_TEXTS_EN = {
    1: "Don't swear, rude. 1/3",
    2: "You swore again. 2/3",
    3: "Shut up, rude. 3/3",
}
MUTE_DURATIONS_MIN = {4: 5, 5: 10}
DEFAULT_SHUTDOWN_MESSAGE = "🔧 ربات خاموش و در حال تعمیرات است."
DEFAULT_SHUTDOWN_MESSAGE_EN = "🔧 The bot is turned off for maintenance."
DEFAULT_UPDATE_MESSAGE = "⚠️ ربات آپدیت شده است! لطفاً ربات را دوباره به گروه اضافه کنید و ادمین کامل کنید."
DEFAULT_UPDATE_MESSAGE_EN = "⚠️ The bot has been updated! Please add the bot to your group again and make it a full admin."
DEFAULT_WELCOME_TEXT = "🌼 {user} عزیز، به گروه {group} خوش اومدی!\nامیدواریم لحظات خوبی رو اینجا بگذرونی."
DEFAULT_WELCOME_TEXT_EN = "🌼 Dear {user}, welcome to {group}!\nWe hope you have a great time here."

# ============================================================================
# چندزبانه / I18N
# ============================================================================
SUPPORTED_LANGS = ("fa", "en")
DEFAULT_LANG = "fa"
LANG_LABELS = {"fa": "🇮🇷 فارسی", "en": "🇬🇧 English"}


def L(lang, fa, en):
    """متن مناسب زبان رو برمی‌گردونه"""
    return en if lang == "en" else fa


def has_lang(chat_id) -> bool:
    return get_setting(f"bot_lang_{chat_id}") in SUPPORTED_LANGS


def get_lang(chat_id) -> str:
    value = get_setting(f"bot_lang_{chat_id}")
    return value if value in SUPPORTED_LANGS else DEFAULT_LANG


def set_lang(chat_id, code):
    if code in SUPPORTED_LANGS:
        set_setting(f"bot_lang_{chat_id}", code)


def lang_of(update) -> str:
    """پی‌وی = زبان کاربر، گروه = زبان گروه"""
    chat = update.effective_chat
    if chat:
        return get_lang(chat.id)
    user = update.effective_user
    return get_lang(user.id) if user else DEFAULT_LANG


FEATURE_LABELS = {
    "bad_words": ("فیلتر فحش", "Profanity filter"),
    "games": ("بازی‌ها", "Games"),
    "gifs": ("ارسال گیف", "Sending GIFs"),
    "stickers": ("ارسال استیکر", "Sending stickers"),
    "photos": ("ارسال عکس", "Sending photos"),
    "videos": ("ارسال فیلم", "Sending videos"),
    "documents": ("ارسال فایل", "Sending files"),
    "date": ("دستور تاریخ", "Date command"),
    "dollar": ("دستور دلار", "Dollar command"),
    "stats": ("آمار گروه", "Group stats"),
    "convert": ("تبدیل ارز", "Currency converter"),
    "chart": ("نمودار قیمت", "Price chart"),
    "instagram_dl": ("دانلودر اینستاگرام", "Downloader"),
    "welcome": ("خوش‌آمدگویی", "Welcome message"),
    "translate": ("ترجمه", "Translation"),
    "ai_chat": ("هوش مصنوعی", "AI chat"),
}


def feature_label(lang, key):
    pair = FEATURE_LABELS.get(key)
    return L(lang, *pair) if pair else key


def shutdown_text(lang):
    msg = get_shutdown_message()
    if msg == DEFAULT_SHUTDOWN_MESSAGE:
        return L(lang, DEFAULT_SHUTDOWN_MESSAGE, DEFAULT_SHUTDOWN_MESSAGE_EN)
    return msg


def update_message_text(lang):
    msg = get_setting("update_message", DEFAULT_UPDATE_MESSAGE)
    if msg == DEFAULT_UPDATE_MESSAGE:
        return L(lang, DEFAULT_UPDATE_MESSAGE, DEFAULT_UPDATE_MESSAGE_EN)
    return msg


def welcome_template(chat_id, lang):
    text = get_welcome_text(chat_id)
    if text == DEFAULT_WELCOME_TEXT:
        return L(lang, DEFAULT_WELCOME_TEXT, DEFAULT_WELCOME_TEXT_EN)
    return text


def warning_text_for(row, level, lang):
    """متن اخطار؛ اگه همون پیش‌فرضِ فارسی بود، نسخه‌ی زبان انتخابی نشون داده می‌شه"""
    text = row["text"] if row and row["text"] else None
    if text is None:
        return None
    if lang == "en" and level in DEFAULT_WARNING_TEXTS and text == DEFAULT_WARNING_TEXTS[level]:
        return DEFAULT_WARNING_TEXTS_EN[level]
    return text


# دلایل ثابتی که تو دیتابیس (فارسی) ذخیره می‌شن؛ موقع نمایش ترجمه می‌شن
REASON_REPORT = "اقدام بر اساس گزارش عضو"
REASON_MUTE_AUTO = "تکرار استفاده از الفاظ نامناسب (اخطار خودکار)"
REASON_BAN_AUTO = "تکرار سه‌باره بی‌ادبی پس از اخطار و سکوت"
REASON_BAD_WORD_PREFIX = "استفاده از کلمه نامناسب: "
NO_TEXT_SNIPPET = "[بدون متن / رسانه]"
_REASON_EN = {
    REASON_REPORT: "Action taken based on a member report",
    REASON_MUTE_AUTO: "Repeated use of inappropriate language (automatic)",
    REASON_BAN_AUTO: "Repeated rudeness after warnings and mutes",
    NO_TEXT_SNIPPET: "[no text / media]",
}


def reason_text(reason, lang):
    if not reason:
        return reason
    if lang == "en":
        if reason in _REASON_EN:
            return _REASON_EN[reason]
        if reason.startswith(REASON_BAD_WORD_PREFIX):
            return "Inappropriate word used: " + reason[len(REASON_BAD_WORD_PREFIX):]
    return reason


# ============================================================================
# تاریخ و زمان (شمسی برای فارسی، میلادی برای انگلیسی)
# ============================================================================
TEHRAN_TZ = ZoneInfo("Asia/Tehran")
TEHRAN = TEHRAN_TZ


def now_tehran() -> datetime:
    return datetime.now(TEHRAN_TZ)


def utc_from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def tehran_from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=TEHRAN_TZ)


def format_date_only(dt=None, lang="fa") -> str:
    if dt is None:
        dt = now_tehran()
    if lang == "en":
        return f"{dt.day} {dt.strftime('%B')} {dt.year}"
    jd = jdatetime.datetime.fromgregorian(datetime=dt)
    return f"{jd.day} {jd.j_months_fa[jd.month - 1]} {jd.year}"


def format_time_only(dt=None, lang="fa") -> str:
    if dt is None:
        dt = now_tehran()
    hour12 = dt.hour % 12 or 12
    minute = dt.strftime("%M")
    if lang == "en":
        return f"{hour12}:{minute} {'PM' if dt.hour >= 12 else 'AM'}"
    return f"{hour12}:{minute} {'ب.ظ' if dt.hour >= 12 else 'ق.ظ'}"


def format_datetime(dt=None, lang="fa") -> str:
    if dt is None:
        dt = now_tehran()
    sep = ", " if lang == "en" else "، "
    return f"{format_date_only(dt, lang)}{sep}{format_time_only(dt, lang)}"


def build_restriction_message(until_dt, group_title=None, lang="fa") -> str:
    when = format_datetime(until_dt, lang)
    if lang == "en":
        if group_title:
            return f"The admins of \"{group_title}\" have restricted you from sending messages until {when}."
        return f"The admins of this group have restricted you from sending messages until {when}."
    if group_title:
        return f"مدیران گروه «{group_title}»، امکان ارسال پیام را برای شما تا {when} محدود کرده‌اند."
    return f"مدیران این گروه، امکان ارسال پیام را برای شما تا {when} محدود کرده‌اند."


def build_duration_text(seconds: int, lang="fa") -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if lang == "en":
        parts = []
        if h:
            parts.append(f"{h} hour{'s' if h != 1 else ''}")
        if m:
            parts.append(f"{m} minute{'s' if m != 1 else ''}")
        if s:
            parts.append(f"{s} second{'s' if s != 1 else ''}")
        return " and ".join(parts) if parts else "a few moments"
    parts = []
    if h:
        parts.append(f"{h} ساعت")
    if m:
        parts.append(f"{m} دقیقه")
    if s:
        parts.append(f"{s} ثانیه")
    return " و ".join(parts) if parts else "چند لحظه"


FA_WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
EN_WEEKDAYS = ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


async def cmd_tarikh(update, context):
    chat = update.effective_chat
    lang = lang_of(update)
    if chat and chat.type in GROUP_TYPES and not is_feature_enabled(chat.id, "date"):
        return
    now = now_tehran()
    jd = jdatetime.datetime.fromgregorian(datetime=now)  # شنبه=0 ... جمعه=6
    if lang == "en":
        text = f"🗓 Date: {EN_WEEKDAYS[jd.weekday()]}, {format_date_only(now, 'en')}\n⏰ Time: {now.strftime('%H:%M')}"
    else:
        text = f"🗓 تاریخ: {FA_WEEKDAYS[jd.weekday()]}، {format_date_only(now, 'fa')}\n⏰ ساعت: {now.strftime('%H:%M')}"
    await update.effective_message.reply_text(text)


# ============================================================================
# دیتابیس / DATABASE  (از بیرون با db.xxx هم قابل‌دسترسیه)
# ============================================================================
_before_db = set(globals())


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY, title TEXT, added_by_user_id INTEGER, added_by_username TEXT,
            is_locked INTEGER DEFAULT 0, lock_until REAL, is_active INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, username TEXT,
            reason TEXT, level INTEGER, created_at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, username TEXT,
            reason TEXT, has_reason INTEGER DEFAULT 1, muted_at REAL, until_at REAL, active INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, username TEXT,
            reason TEXT, has_reason INTEGER DEFAULT 1, banned_at REAL, active INTEGER DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS blacklist_gifs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, file_unique_id TEXT, added_at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS blacklist_stickers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, file_unique_id TEXT, added_at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS seen_users (
            chat_id INTEGER, user_id INTEGER, username TEXT, full_name TEXT, PRIMARY KEY (chat_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS bad_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, word TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS warning_texts (
            chat_id INTEGER, level INTEGER, text TEXT, sticker_file_id TEXT, gif_file_id TEXT,
            photo_file_id TEXT, PRIMARY KEY (chat_id, level))""")
        try:
            c.execute("ALTER TABLE warning_texts ADD COLUMN photo_file_id TEXT")
        except sqlite3.OperationalError:
            pass
        c.execute("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS group_admins_extra (
            chat_id INTEGER, user_id INTEGER, granted_by INTEGER, can_manage_other_admins INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS group_features (
            chat_id INTEGER, feature_key TEXT, enabled INTEGER DEFAULT 1, PRIMARY KEY (chat_id, feature_key))""")
        c.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, reporter_id INTEGER, reporter_username TEXT,
            reported_user_id INTEGER, reported_username TEXT, message_snippet TEXT, created_at REAL,
            sent INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS welcome_messages (
            chat_id INTEGER PRIMARY KEY, text TEXT, sticker_file_id TEXT, animation_file_id TEXT)""")
        for col in ("sticker_file_id", "animation_file_id"):
            try:
                c.execute(f"ALTER TABLE welcome_messages ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass

        c.execute("SELECT COUNT(*) as cnt FROM bad_words WHERE chat_id IS NULL")
        if c.fetchone()["cnt"] == 0:
            for w in DEFAULT_BAD_WORDS:
                c.execute("INSERT INTO bad_words (chat_id, word) VALUES (NULL, ?)", (w,))
        c.execute("SELECT COUNT(*) as cnt FROM warning_texts WHERE chat_id IS NULL")
        if c.fetchone()["cnt"] == 0:
            for lvl, txt in DEFAULT_WARNING_TEXTS.items():
                c.execute("INSERT INTO warning_texts (chat_id, level, text) VALUES (NULL, ?, ?)", (lvl, txt))
        c.execute("SELECT value FROM bot_settings WHERE key = 'global_active'")
        if c.fetchone() is None:
            c.execute("INSERT INTO bot_settings (key, value) VALUES ('global_active', '1')")
        c.execute("SELECT value FROM bot_settings WHERE key = 'shutdown_message'")
        if c.fetchone() is None:
            c.execute("INSERT INTO bot_settings (key, value) VALUES ('shutdown_message', ?)",
                      (DEFAULT_SHUTDOWN_MESSAGE,))


# ===== گروه‌ها =====
def upsert_group(chat_id, title, added_by_user_id=None, added_by_username=None):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id FROM groups WHERE chat_id=?", (chat_id,))
        if c.fetchone():
            c.execute("UPDATE groups SET title=? WHERE chat_id=?", (title, chat_id))
        else:
            c.execute("INSERT INTO groups (chat_id, title, added_by_user_id, added_by_username, is_active) "
                      "VALUES (?, ?, ?, ?, 1)", (chat_id, title, added_by_user_id, added_by_username))


def get_group(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        return c.fetchone()


def get_groups_added_by(user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM groups WHERE added_by_user_id=? AND chat_id < 0", (user_id,))
        return c.fetchall()


def get_all_groups():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM groups WHERE chat_id < 0")
        return c.fetchall()


def set_group_lock(chat_id, locked: bool, until_ts=None):
    with get_conn() as conn:
        conn.execute("UPDATE groups SET is_locked=?, lock_until=? WHERE chat_id=?",
                     (1 if locked else 0, until_ts, chat_id))


def set_group_active(chat_id, active: bool):
    with get_conn() as conn:
        conn.execute("UPDATE groups SET is_active=? WHERE chat_id=?", (1 if active else 0, chat_id))


# ===== اخطارها =====
def add_warning(chat_id, user_id, username, reason, level):
    with get_conn() as conn:
        conn.execute("INSERT INTO warnings (chat_id, user_id, username, reason, level, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (chat_id, user_id, username, reason, level, time.time()))


def get_active_warning_count(chat_id, user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT banned_at FROM bans WHERE chat_id=? AND user_id=? ORDER BY banned_at DESC LIMIT 1",
                  (chat_id, user_id))
        last_ban = c.fetchone()
        since = last_ban["banned_at"] if last_ban else 0
        c.execute("SELECT COUNT(*) as cnt FROM warnings WHERE chat_id=? AND user_id=? AND created_at > ?",
                  (chat_id, user_id, since))
        return c.fetchone()["cnt"]


def get_user_warnings(chat_id, user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM warnings WHERE chat_id=? AND user_id=? ORDER BY created_at DESC", (chat_id, user_id))
        return c.fetchall()


def get_all_warned_users(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, COUNT(*) as cnt, MAX(created_at) as last_at "
                  "FROM warnings WHERE chat_id=? GROUP BY user_id ORDER BY last_at DESC", (chat_id,))
        return c.fetchall()


def get_warning_text(chat_id, level):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM warning_texts WHERE chat_id=? AND level=?", (chat_id, level))
        row = c.fetchone()
        if row:
            return row
        c.execute("SELECT * FROM warning_texts WHERE chat_id IS NULL AND level=?", (level,))
        return c.fetchone()


def set_warning_text(chat_id, level, text=None, sticker_file_id=None, gif_file_id=None, photo_file_id=None):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT level FROM warning_texts WHERE chat_id=? AND level=?", (chat_id, level))
        if c.fetchone():
            fields, values = [], []
            if text is not None:
                fields.append("text=?"); values.append(text)
            if sticker_file_id is not None:
                fields.append("sticker_file_id=?"); values.append(sticker_file_id)
                fields.append("gif_file_id=NULL"); fields.append("photo_file_id=NULL")
            if gif_file_id is not None:
                fields.append("gif_file_id=?"); values.append(gif_file_id)
                fields.append("sticker_file_id=NULL"); fields.append("photo_file_id=NULL")
            if photo_file_id is not None:
                fields.append("photo_file_id=?"); values.append(photo_file_id)
                fields.append("sticker_file_id=NULL"); fields.append("gif_file_id=NULL")
            if not fields:
                return
            values += [chat_id, level]
            c.execute(f"UPDATE warning_texts SET {', '.join(fields)} WHERE chat_id=? AND level=?", values)
        else:
            c.execute("INSERT INTO warning_texts (chat_id, level, text, sticker_file_id, gif_file_id, photo_file_id) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (chat_id, level, text, sticker_file_id, gif_file_id, photo_file_id))


def clear_warning_media(chat_id, level):
    """مدیای (استیکر/گیف/عکس) یه سطح اخطار رو واقعاً پاک می‌کنه (متن می‌مونه)"""
    with get_conn() as conn:
        conn.execute("UPDATE warning_texts SET sticker_file_id=NULL, gif_file_id=NULL, photo_file_id=NULL "
                     "WHERE chat_id=? AND level=?", (chat_id, level))


def reset_warning_text(chat_id, level):
    with get_conn() as conn:
        conn.execute("DELETE FROM warning_texts WHERE chat_id=? AND level=?", (chat_id, level))


# ===== سکوت =====
def add_mute(chat_id, user_id, username, reason, has_reason, until_at):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE mutes SET active=0 WHERE chat_id=? AND user_id=? AND active=1", (chat_id, user_id))
        c.execute("INSERT INTO mutes (chat_id, user_id, username, reason, has_reason, muted_at, until_at, active) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                  (chat_id, user_id, username, reason, 1 if has_reason else 0, time.time(), until_at))


def remove_mute(chat_id, user_id):
    with get_conn() as conn:
        conn.execute("UPDATE mutes SET active=0 WHERE chat_id=? AND user_id=? AND active=1", (chat_id, user_id))


def update_mute_duration(chat_id, user_id, new_until_ts):
    with get_conn() as conn:
        conn.execute("UPDATE mutes SET until_at=? WHERE chat_id=? AND user_id=? AND active=1",
                     (new_until_ts, chat_id, user_id))


def get_mute_record(chat_id, user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM mutes WHERE chat_id=? AND user_id=? AND active=1 ORDER BY muted_at DESC LIMIT 1",
                  (chat_id, user_id))
        return c.fetchone()


def get_active_mutes(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM mutes WHERE chat_id=? AND active=1 ORDER BY muted_at DESC", (chat_id,))
        return c.fetchall()


def is_muted(chat_id, user_id):
    return get_mute_record(chat_id, user_id)


# ===== بن =====
def add_ban(chat_id, user_id, username, reason, has_reason):
    with get_conn() as conn:
        conn.execute("INSERT INTO bans (chat_id, user_id, username, reason, has_reason, banned_at, active) "
                     "VALUES (?, ?, ?, ?, ?, ?, 1)",
                     (chat_id, user_id, username, reason, 1 if has_reason else 0, time.time()))


def get_all_banned_users(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM bans WHERE chat_id=? AND active=1 ORDER BY banned_at DESC", (chat_id,))
        return c.fetchall()


def unban_record(chat_id, user_id):
    with get_conn() as conn:
        conn.execute("UPDATE bans SET active=0 WHERE chat_id=? AND user_id=? AND active=1", (chat_id, user_id))


# ===== کاربران دیده‌شده (برای تگ) =====
def save_seen_user(chat_id, user_id, username, full_name):
    with get_conn() as conn:
        conn.execute("INSERT INTO seen_users (chat_id, user_id, username, full_name) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(chat_id, user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
                     (chat_id, user_id, username, full_name))


def get_seen_users(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, full_name FROM seen_users WHERE chat_id=?", (chat_id,))
        return c.fetchall()


# ===== لیست سیاه =====
def add_blacklist_gif(chat_id, file_unique_id):
    with get_conn() as conn:
        conn.execute("INSERT INTO blacklist_gifs (chat_id, file_unique_id, added_at) VALUES (?, ?, ?)",
                     (chat_id, file_unique_id, time.time()))


def is_gif_blacklisted(chat_id, file_unique_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM blacklist_gifs WHERE chat_id=? AND file_unique_id=?", (chat_id, file_unique_id))
        return c.fetchone() is not None


def add_blacklist_sticker(chat_id, file_unique_id):
    with get_conn() as conn:
        conn.execute("INSERT INTO blacklist_stickers (chat_id, file_unique_id, added_at) VALUES (?, ?, ?)",
                     (chat_id, file_unique_id, time.time()))


def is_sticker_blacklisted(chat_id, file_unique_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM blacklist_stickers WHERE chat_id=? AND file_unique_id=?", (chat_id, file_unique_id))
        return c.fetchone() is not None


# ===== کلمات بد =====
def get_bad_words(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT word FROM bad_words WHERE chat_id IS NULL OR chat_id=?", (chat_id,))
        return [r["word"] for r in c.fetchall()]


def add_bad_word(chat_id, word):
    with get_conn() as conn:
        conn.execute("INSERT INTO bad_words (chat_id, word) VALUES (?, ?)", (chat_id, word))


def remove_bad_word(chat_id, word):
    with get_conn() as conn:
        conn.execute("DELETE FROM bad_words WHERE word=? AND (chat_id=? OR chat_id IS NULL)", (word, chat_id))


# ===== تنظیمات کلی =====
def get_setting(key, default=None):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM bot_settings WHERE key=?", (key,))
        row = c.fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_all_keys():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT key FROM bot_settings")
        return [row["key"] for row in c.fetchall()]


def is_global_active():
    return get_setting("global_active", "1") == "1"


def set_global_active(active: bool):
    set_setting("global_active", "1" if active else "0")


def get_shutdown_message():
    return get_setting("shutdown_message", DEFAULT_SHUTDOWN_MESSAGE)


def set_shutdown_message(text):
    set_setting("shutdown_message", text)


# ===== مدیران اضافه =====
def grant_admin_extra_permission(chat_id, user_id, granted_by):
    with get_conn() as conn:
        conn.execute("INSERT INTO group_admins_extra (chat_id, user_id, granted_by, can_manage_other_admins) "
                     "VALUES (?, ?, ?, 1) ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                     "can_manage_other_admins=1, granted_by=excluded.granted_by", (chat_id, user_id, granted_by))


def revoke_admin_extra_permission(chat_id, user_id):
    with get_conn() as conn:
        conn.execute("UPDATE group_admins_extra SET can_manage_other_admins=0 WHERE chat_id=? AND user_id=?",
                     (chat_id, user_id))


def has_admin_extra_permission(chat_id, user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT can_manage_other_admins FROM group_admins_extra WHERE chat_id=? AND user_id=?",
                  (chat_id, user_id))
        row = c.fetchone()
        return bool(row and row["can_manage_other_admins"])


# ===== قابلیت‌ها =====
TOGGLEABLE_FEATURES = {
    "bad_words": "فیلتر فحش", "games": "بازی‌ها", "gifs": "ارسال گیف", "stickers": "ارسال استیکر",
    "photos": "ارسال عکس", "videos": "ارسال فیلم", "documents": "ارسال فایل", "date": "دستور تاریخ",
    "dollar": "دستور دلار", "stats": "آمار گروه", "convert": "تبدیل ارز", "chart": "نمودار قیمت",
    "instagram_dl": "دانلودر اینستاگرام",
}


def is_feature_enabled(chat_id, feature_key):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT enabled FROM group_features WHERE chat_id=? AND feature_key=?", (chat_id, feature_key))
        row = c.fetchone()
        return bool(row["enabled"]) if row else True


def set_feature_enabled(chat_id, feature_key, enabled: bool):
    with get_conn() as conn:
        conn.execute("INSERT INTO group_features (chat_id, feature_key, enabled) VALUES (?, ?, ?) "
                     "ON CONFLICT(chat_id, feature_key) DO UPDATE SET enabled=excluded.enabled",
                     (chat_id, feature_key, 1 if enabled else 0))


# ===== گزارش‌ها =====
def add_report(chat_id, reporter_id, reporter_username, reported_user_id, reported_username, message_snippet):
    with get_conn() as conn:
        conn.execute("INSERT INTO reports (chat_id, reporter_id, reporter_username, reported_user_id, "
                     "reported_username, message_snippet, created_at, sent) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                     (chat_id, reporter_id, reporter_username, reported_user_id, reported_username,
                      message_snippet, time.time()))


def get_chats_with_pending_reports():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT chat_id FROM reports WHERE sent=0")
        return [r["chat_id"] for r in c.fetchall()]


def get_pending_reports(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM reports WHERE chat_id=? AND sent=0 ORDER BY created_at", (chat_id,))
        return c.fetchall()


def mark_reports_sent(chat_id):
    with get_conn() as conn:
        conn.execute("UPDATE reports SET sent=1 WHERE chat_id=? AND sent=0", (chat_id,))


def get_all_reports(chat_id, limit=50):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM reports WHERE chat_id=? ORDER BY created_at DESC LIMIT ?", (chat_id, limit))
        return c.fetchall()


def get_report_by_id(report_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM reports WHERE id=?", (report_id,))
        return c.fetchone()


def clear_reports(chat_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM reports WHERE chat_id=?", (chat_id,))


# ===== خوش‌آمدگویی =====
def get_welcome_text(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT text FROM welcome_messages WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        return row["text"] if row else DEFAULT_WELCOME_TEXT


def set_welcome_text(chat_id, text):
    with get_conn() as conn:
        conn.execute("INSERT INTO welcome_messages (chat_id, text) VALUES (?, ?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text", (chat_id, text))


def reset_welcome_text(chat_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM welcome_messages WHERE chat_id=?", (chat_id,))


def set_welcome_media(chat_id, sticker_file_id=None, animation_file_id=None):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id FROM welcome_messages WHERE chat_id=?", (chat_id,))
        if c.fetchone():
            if sticker_file_id is not None:
                c.execute("UPDATE welcome_messages SET sticker_file_id=?, animation_file_id=NULL WHERE chat_id=?",
                          (sticker_file_id, chat_id))
            else:
                c.execute("UPDATE welcome_messages SET animation_file_id=?, sticker_file_id=NULL WHERE chat_id=?",
                          (animation_file_id, chat_id))
        else:
            c.execute("INSERT INTO welcome_messages (chat_id, text, sticker_file_id, animation_file_id) "
                      "VALUES (?, ?, ?, ?)", (chat_id, DEFAULT_WELCOME_TEXT, sticker_file_id, animation_file_id))


def get_welcome_media(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT sticker_file_id, animation_file_id FROM welcome_messages WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if not row:
            return None, None
        return row["sticker_file_id"], row["animation_file_id"]


def clear_welcome_media(chat_id):
    with get_conn() as conn:
        conn.execute("UPDATE welcome_messages SET sticker_file_id=NULL, animation_file_id=NULL WHERE chat_id=?",
                     (chat_id,))


# ===== ترجمه و زبان عکس =====
def get_translate_lang(chat_id, default="fa"):
    return get_setting(f"tr_lang_{chat_id}", default)


def set_translate_lang(chat_id, lang_code):
    set_setting(f"tr_lang_{chat_id}", lang_code)


def get_image_lang(chat_id, default="fa"):
    return get_setting(f"img_lang_{chat_id}", default)


def set_image_lang(chat_id, lang_code):
    set_setting(f"img_lang_{chat_id}", lang_code)


# ===== پاک‌سازی خودکار =====
def update_last_message_id(chat_id, message_id):
    set_setting(f"last_msg_{chat_id}", str(message_id))


def get_last_message_id(chat_id):
    value = get_setting(f"last_msg_{chat_id}")
    return int(value) if value else None


def get_cleanup_settings(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT key, value FROM bot_settings WHERE key IN (?, ?, ?, ?)",
                  (f"cleanup_on_{chat_id}", f"cleanup_interval_{chat_id}",
                   f"cleanup_count_{chat_id}", f"cleanup_last_{chat_id}"))
        vals = {row["key"]: row["value"] for row in c.fetchall()}
        enabled = vals.get(f"cleanup_on_{chat_id}") == "1"
        interval_seconds = int(vals.get(f"cleanup_interval_{chat_id}", "86400"))
        count = int(vals.get(f"cleanup_count_{chat_id}", "20"))
        last_ts = float(vals.get(f"cleanup_last_{chat_id}", "0"))
        return enabled, interval_seconds, count, last_ts


def set_cleanup_settings(chat_id, enabled=None, interval_seconds=None, count=None, last_ts=None):
    updates = []
    if enabled is not None:
        updates.append((f"cleanup_on_{chat_id}", "1" if enabled else "0"))
    if interval_seconds is not None:
        updates.append((f"cleanup_interval_{chat_id}", str(max(60, interval_seconds))))
    if count is not None:
        updates.append((f"cleanup_count_{chat_id}", str(max(1, count))))
    if last_ts is not None:
        updates.append((f"cleanup_last_{chat_id}", str(last_ts)))
    for key, value in updates:
        set_setting(key, value)


def get_all_cleanup_enabled_chats():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT key FROM bot_settings WHERE key LIKE 'cleanup_on_%' AND value='1'")
        chat_ids = []
        for row in c.fetchall():
            try:
                chat_ids.append(int(row["key"].replace("cleanup_on_", "")))
            except ValueError:
                pass
        return chat_ids


db = types.SimpleNamespace(**{
    n: v for n, v in list(globals().items())
    if n not in _before_db and not n.startswith("_")
})


# ============================================================================
# دسترسی‌ها / PERMISSIONS
# ============================================================================
TIME_PATTERN = re.compile(r"(\d+)\s*(h|m|s|ساعت|دقیقه|ثانیه)", re.IGNORECASE)
BARE_NUMBER_PATTERN = re.compile(r"^\s*(\d+)\s*$")


def parse_duration_seconds(text: str):
    """'5h' '10m' '1h30m' '45s' یا عدد تنها (=دقیقه) رو به ثانیه تبدیل می‌کنه"""
    if not text:
        return None
    total = 0
    found = False
    for amount, unit in TIME_PATTERN.findall(text):
        found = True
        amount = int(amount)
        unit = unit.lower()
        if unit in ("h", "ساعت"):
            total += amount * 3600
        elif unit in ("m", "دقیقه"):
            total += amount * 60
        elif unit in ("s", "ثانیه"):
            total += amount
    if found:
        return total
    bare = BARE_NUMBER_PATTERN.match(text.strip())
    if bare:
        return int(bare.group(1)) * 60
    return None


async def is_creator(user_id: int) -> bool:
    return CREATOR_ID != 0 and user_id == CREATOR_ID


async def is_group_owner(chat_id: int, user_id: int) -> bool:
    group = get_group(chat_id)
    return bool(group and group["added_by_user_id"] == user_id)


async def is_telegram_group_creator(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status == ChatMember.OWNER
    except Exception:
        return False


async def can_access_dm_panel(bot, chat_id: int, user_id: int) -> bool:
    """پنل پی‌وی: فقط سازنده‌ی ربات یا مالک واقعیِ گروه (creator تلگرام)"""
    if await is_creator(user_id):
        return True
    return await is_telegram_group_creator(bot, chat_id, user_id)


async def is_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    except Exception:
        return False


async def get_permission_level(bot, chat_id: int, user_id: int) -> str:
    """creator | group_owner | admin | member"""
    if await is_creator(user_id):
        return "creator"
    if await is_group_owner(chat_id, user_id) or await is_telegram_group_creator(bot, chat_id, user_id):
        return "group_owner"
    if await is_admin(bot, chat_id, user_id):
        return "admin"
    return "member"


async def can_use_moderation_commands(bot, chat_id: int, user_id: int) -> bool:
    level = await get_permission_level(bot, chat_id, user_id)
    return level in ("creator", "group_owner", "admin")


async def can_target_user(bot, chat_id: int, actor_id: int, target_id: int) -> bool:
    actor_level = await get_permission_level(bot, chat_id, actor_id)
    target_level = await get_permission_level(bot, chat_id, target_id)
    if actor_level in ("creator", "group_owner"):
        return True
    if actor_level == "admin":
        if target_level in ("creator", "group_owner"):
            return False
        if target_level == "admin":
            return has_admin_extra_permission(chat_id, actor_id)
        return True
    return False


# ============================================================================
# دستورات مدیریتی گروه / MODERATION
# ============================================================================
def _full_perms():
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True,
    )


def _mention_html(user_id, display_name):
    return f'<a href="tg://user?id={user_id}">{escape(display_name)}</a>'


async def _require_group(update: Update):
    chat = update.effective_chat
    return chat and chat.type in GROUP_TYPES


async def _reply(update, text, parse_mode=None):
    return await update.effective_message.reply_text(text, parse_mode=parse_mode)


def _display_name(target, lang="fa"):
    username = getattr(target, "username", None)
    if username:
        return f"@{username}"
    name = getattr(target, "full_name", None) or getattr(target, "first_name", None)
    return name or L(lang, "کاربر", "User")


async def _delete_message_later(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except Exception:
        pass


async def _resolve_target_user(message, bot):
    """کاربر هدف از ریپلای؛ اگه ریپلای روی پیام خودِ ربات بود، از منشن داخل متن پیدا می‌شه"""
    reply_msg = message.reply_to_message
    if not reply_msg:
        return None
    from_user = reply_msg.from_user
    if from_user and not from_user.is_bot:
        return from_user
    entities = reply_msg.entities or reply_msg.caption_entities or []
    text = reply_msg.text or reply_msg.caption or ""
    for ent in entities:
        if ent.type == "text_mention" and ent.user:
            return ent.user
        if ent.type == "mention":
            username = text[ent.offset + 1: ent.offset + ent.length]
            try:
                return await bot.get_chat(f"@{username}")
            except Exception:
                continue
    return None


_NOT_ALLOWED = {
    "lock": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند گروه را خاموش کنند.",
             "⛔️ Only admins, the group owner or the bot creator can lock the group."),
    "unlock": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند گروه را باز کنند.",
               "⛔️ Only admins, the group owner or the bot creator can unlock the group."),
    "mute": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند کاربر را سکوت بدهند.",
             "⛔️ Only admins, the group owner or the bot creator can mute users."),
    "unmute": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند سکوت را بردارند.",
               "⛔️ Only admins, the group owner or the bot creator can unmute users."),
    "ban": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند کاربر را بن کنند.",
            "⛔️ Only admins, the group owner or the bot creator can ban users."),
    "warn": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند اخطار بدهند.",
             "⛔️ Only admins, the group owner or the bot creator can warn users."),
    "del": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند پیام پاک کنند.",
            "⛔️ Only admins, the group owner or the bot creator can delete messages."),
    "gifban": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند گیف بن کنند.",
               "⛔️ Only admins, the group owner or the bot creator can ban GIFs."),
    "stickerban": ("⛔️ فقط مدیران، مالک گروه یا سازنده ربات می‌توانند استیکر بن کنند.",
                   "⛔️ Only admins, the group owner or the bot creator can ban stickers."),
}


def _not_allowed(lang, key):
    return L(lang, *_NOT_ALLOWED[key])


def _target_not_found(lang):
    return L(lang,
             "❗️ نتونستم کاربر هدف رو پیدا کنم. مطمئن شو تو پیامی که روش ریپلای کردی، یوزرنیم (@) کاربر مشخص باشه.",
             "❗️ I couldn't find the target user. Make sure the message you replied to shows the user's @username.")


# ---- خاموشی / lock ----
async def cmd_khamoshi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "lock"))
        return

    duration = parse_duration_seconds(" ".join(context.args) if context.args else "")
    try:
        await context.bot.set_chat_permissions(chat.id, ChatPermissions(can_send_messages=False))
    except Exception as e:
        await _reply(update, L(lang, f"❌ نتونستم گروه رو قفل کنم. مطمئن شو ربات ادمینه.\n{e}",
                               f"❌ I couldn't lock the group. Make sure the bot is an admin.\n{e}"))
        return

    if duration:
        until_ts = time.time() + duration
        set_group_lock(chat.id, True, until_ts)
        hm = tehran_from_ts(until_ts).strftime("%H:%M")
        dur = build_duration_text(duration, lang)
        context.job_queue.run_once(_auto_unlock_job, when=duration, chat_id=chat.id,
                                   name=f"unlock_{chat.id}", data={"chat_id": chat.id})
        await _reply(update, L(
            lang,
            f"🔒 گروه قفل شد.\n⏳ به‌صورت خودکار بعد از {dur} باز می‌شود (حدود {hm}).",
            f"🔒 Group locked.\n⏳ It will unlock automatically after {dur} (around {hm})."))
    else:
        set_group_lock(chat.id, True, None)
        await _reply(update, L(lang, "🔒 گروه قفل شد (فقط مدیران می‌توانند پیام بدهند).",
                               "🔒 Group locked (only admins can send messages)."))


async def _auto_unlock_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    group = get_group(chat_id)
    if not group or not group["is_locked"]:
        return
    try:
        await context.bot.set_chat_permissions(chat_id, _full_perms())
    except Exception:
        pass
    set_group_lock(chat_id, False, None)
    try:
        await context.bot.send_message(chat_id, L(get_lang(chat_id), "🔓 گروه به‌صورت خودکار باز شد.",
                                                  "🔓 The group was unlocked automatically."))
    except Exception:
        pass


# ---- روشن / unlock ----
async def cmd_roshan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "unlock"))
        return
    try:
        await context.bot.set_chat_permissions(chat.id, _full_perms())
    except Exception as e:
        await _reply(update, L(lang, f"❌ نتونستم گروه رو باز کنم.\n{e}", f"❌ I couldn't unlock the group.\n{e}"))
        return
    for job in context.job_queue.get_jobs_by_name(f"unlock_{chat.id}"):
        job.schedule_removal()
    set_group_lock(chat.id, False, None)
    await _reply(update, L(lang, "🔓 گروه باز شد.", "🔓 Group unlocked."))


# ---- سکوت / mute ----
def _automute_job_name(chat_id, user_id):
    return f"automute_{chat_id}_{user_id}"


async def cmd_sokoot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "mute"))
        return
    if not message.reply_to_message:
        await _reply(update, L(lang, "❗️ لطفاً روی پیام کاربر مورد نظر ریپلای کن و بنویس: سکوت 30m یا سکوت 10",
                               "❗️ Reply to the user's message and write: mute 30m or mute 10"))
        return

    target = await _resolve_target_user(message, context.bot)
    if not target:
        await _reply(update, _target_not_found(lang))
        return
    if not await can_target_user(context.bot, chat.id, user.id, target.id):
        await _reply(update, L(lang, "⛔️ شما اجازه سکوت دادن به این کاربر را ندارید.",
                               "⛔️ You are not allowed to mute this user."))
        return

    args_text = " ".join(context.args) if context.args else ""
    duration = parse_duration_seconds(args_text)
    reason = None
    if args_text:
        reason_text_ = re.sub(r"(\d+)\s*(h|m|s|ساعت|دقیقه|ثانیه)?", "", args_text, flags=re.IGNORECASE).strip()
        reason = reason_text_ or None
    if not duration:
        duration = 10 * 60

    until_ts = time.time() + duration
    until_dt_display = tehran_from_ts(until_ts)
    try:
        await context.bot.restrict_chat_member(
            chat.id, target.id, permissions=ChatPermissions(can_send_messages=False),
            until_date=utc_from_ts(until_ts))
    except Exception as e:
        await _reply(update, L(lang, f"❌ نتونستم کاربر رو سکوت بدم.\n{e}", f"❌ I couldn't mute the user.\n{e}"))
        return

    username = _display_name(target, lang)
    add_mute(chat.id, target.id, username, reason, bool(reason), until_ts)
    mention = _mention_html(target.id, username)

    for job in context.job_queue.get_jobs_by_name(_automute_job_name(chat.id, target.id)):
        job.schedule_removal()

    dur = build_duration_text(duration, lang)
    restriction = escape(build_restriction_message(until_dt_display, chat.title, lang))
    text = L(lang, f"🔇 {mention} به مدت {dur} سکوت شد.\n{restriction}",
             f"🔇 {mention} was muted for {dur}.\n{restriction}")
    if reason:
        text += f"\n{L(lang, 'دلیل', 'Reason')}: {escape(reason)}"
    sent = await _reply(update, text, parse_mode="HTML")

    context.job_queue.run_once(
        _auto_unmute_expired, when=duration, chat_id=chat.id,
        name=_automute_job_name(chat.id, target.id),
        data={"chat_id": chat.id, "user_id": target.id, "username": username,
              "announce_message_id": sent.message_id if sent else None},
    )


async def _auto_unmute_expired(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id, user_id, username = data["chat_id"], data["user_id"], data["username"]
    announce_message_id = data.get("announce_message_id")

    try:
        record = get_mute_record(chat_id, user_id)
    except Exception:
        record = None
    if not record:
        return

    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=_full_perms())
    except Exception:
        pass
    remove_mute(chat_id, user_id)

    if announce_message_id:
        try:
            await context.bot.delete_message(chat_id, announce_message_id)
        except Exception:
            pass

    lang = get_lang(chat_id)
    mention = _mention_html(user_id, username)
    try:
        freed_msg = await context.bot.send_message(
            chat_id, L(lang, f"🔊 مدت سکوت {mention} تمام شد و آزاد شد.",
                       f"🔊 {mention}'s mute is over and they have been freed."), parse_mode="HTML")
        context.job_queue.run_once(
            _delete_message_later, when=10,
            data={"chat_id": chat_id, "message_id": freed_msg.message_id},
            name=f"delautomute_{chat_id}_{freed_msg.message_id}")
    except Exception:
        pass


# ---- آزاد کن / unmute ----
async def cmd_azad_kon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "unmute"))
        return
    if not message.reply_to_message:
        await _reply(update, L(
            lang, "❗️ لطفاً روی پیام کاربر مورد نظر (یا پیام هشدار/سکوتِ ربات که اسمش توشه) ریپلای کن و بنویس: آزاد کن",
            "❗️ Reply to the user's message (or the bot's warning/mute message that contains their name) and write: unmute"))
        return

    target = await _resolve_target_user(message, context.bot)
    if not target:
        await _reply(update, _target_not_found(lang))
        return

    try:
        await context.bot.restrict_chat_member(chat.id, target.id, permissions=_full_perms())
    except Exception as e:
        await _reply(update, L(lang, f"❌ نتونستم سکوت رو بردارم.\n{e}", f"❌ I couldn't remove the mute.\n{e}"))
        return

    remove_mute(chat.id, target.id)
    for job in context.job_queue.get_jobs_by_name(_automute_job_name(chat.id, target.id)):
        job.schedule_removal()

    username = _display_name(target, lang)
    mention = _mention_html(target.id, username)
    sent_msg = await message.reply_text(
        L(lang, f"🔊 سکوت {mention} برداشته شد.", f"🔊 {mention} has been unmuted."), parse_mode="HTML")
    context.job_queue.run_once(
        _delete_message_later, when=5, data={"chat_id": chat.id, "message_id": sent_msg.message_id},
        name=f"delazad_{chat.id}_{sent_msg.message_id}")


# ---- بن کن / ban ----
async def cmd_ban_kon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "ban"))
        return
    if not message.reply_to_message:
        await _reply(update, L(lang, "❗️ لطفاً روی پیام کاربر مورد نظر ریپلای کن و بنویس: بن کن [دلیل اختیاری]",
                               "❗️ Reply to the user's message and write: ban [optional reason]"))
        return

    target = await _resolve_target_user(message, context.bot)
    if not target:
        await _reply(update, _target_not_found(lang))
        return
    if not await can_target_user(context.bot, chat.id, user.id, target.id):
        await _reply(update, L(lang, "⛔️ شما اجازه بن کردن این کاربر را ندارید.",
                               "⛔️ You are not allowed to ban this user."))
        return

    reason = " ".join(context.args) if context.args else None
    try:
        await context.bot.ban_chat_member(chat.id, target.id)
    except Exception as e:
        await _reply(update, L(lang, f"❌ نتونستم کاربر رو بن کنم.\n{e}", f"❌ I couldn't ban the user.\n{e}"))
        return

    username = _display_name(target, lang)
    add_ban(chat.id, target.id, username, reason, bool(reason))
    mention = _mention_html(target.id, username)
    if reason:
        tail = f"\n{L(lang, 'دلیل', 'Reason')}: {escape(reason)}"
    else:
        tail = L(lang, "\n(بدون دلیل ثبت‌شده)", "\n(no reason given)")
    await _reply(update, L(lang, f"⛔️ {mention} از گروه بن شد.", f"⛔️ {mention} was banned from the group.") + tail,
                 parse_mode="HTML")


# ---- اخطار / warn ----
async def cmd_akhtar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "warn"))
        return
    if not message.reply_to_message:
        await _reply(update, L(lang, "❗️ لطفاً روی پیام کاربر مورد نظر ریپلای کن و بنویس: اخطار [دلیل اختیاری]",
                               "❗️ Reply to the user's message and write: warn [optional reason]"))
        return

    target = await _resolve_target_user(message, context.bot)
    if not target:
        await _reply(update, _target_not_found(lang))
        return
    if not await can_target_user(context.bot, chat.id, user.id, target.id):
        await _reply(update, L(lang, "⛔️ شما اجازه اخطار دادن به این کاربر را ندارید.",
                               "⛔️ You are not allowed to warn this user."))
        return

    reason = " ".join(context.args) if context.args else None
    username = _display_name(target, lang)

    level = get_active_warning_count(chat.id, target.id) + 1
    add_warning(chat.id, target.id, username, reason, level)

    warn_row = get_warning_text(chat.id, level)
    mention = _mention_html(target.id, username)
    custom = warning_text_for(warn_row, level, lang)
    if custom:
        caption = f"{mention}\n{escape(custom)}"
    else:
        caption = L(lang, f"⚠️ {mention} اخطار گرفت (اخطار شماره {level}).",
                    f"⚠️ {mention} received a warning (warning #{level}).")
    if reason:
        caption += f"\n{L(lang, 'دلیل', 'Reason')}: {escape(reason)}"

    try:
        if warn_row and warn_row["sticker_file_id"]:
            await context.bot.send_sticker(chat.id, warn_row["sticker_file_id"])
            await _reply(update, caption, parse_mode="HTML")
        elif warn_row and warn_row["gif_file_id"]:
            await context.bot.send_animation(chat.id, warn_row["gif_file_id"], caption=caption, parse_mode="HTML")
        elif warn_row and warn_row["photo_file_id"]:
            await context.bot.send_photo(chat.id, warn_row["photo_file_id"], caption=caption, parse_mode="HTML")
        else:
            await _reply(update, caption, parse_mode="HTML")
    except Exception:
        await _reply(update, caption, parse_mode="HTML")


# ---- پاک / del ----
async def cmd_pak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "del"))
        return
    if not message.reply_to_message:
        await _reply(update, L(lang, "❗️ لطفاً روی پیام مورد نظر ریپلای کن و بنویس: پاک یا پاک 4",
                               "❗️ Reply to the message and write: del or del 4"))
        return

    count = 1
    if context.args and context.args[0].isdigit():
        count = max(1, min(int(context.args[0]), 200))
    reply_id = message.reply_to_message.message_id
    try:
        await message.delete()
    except Exception:
        pass
    for msg_id in range(reply_id - (count - 1), reply_id + 1):
        try:
            await context.bot.delete_message(chat.id, msg_id)
        except Exception:
            pass


# ---- گیف بن / استیکر بن ----
async def cmd_gif_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "gifban"))
        return
    target_msg = message.reply_to_message
    if not target_msg or not target_msg.animation:
        await _reply(update, L(lang, "❗️ لطفاً روی یک گیف ریپلای کن و بنویس: گیف بن",
                               "❗️ Reply to a GIF and write: gifban"))
        return
    add_blacklist_gif(chat.id, target_msg.animation.file_unique_id)
    try:
        await target_msg.delete()
    except Exception:
        pass
    await _reply(update, L(lang, "🚫 این گیف به لیست سیاه اضافه شد و از این به بعد در گروه حذف می‌شود.",
                           "🚫 This GIF was added to the blacklist and will be deleted from now on."))


async def cmd_sticker_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    lang = lang_of(update)

    if not await can_use_moderation_commands(context.bot, chat.id, user.id):
        await _reply(update, _not_allowed(lang, "stickerban"))
        return
    target_msg = message.reply_to_message
    if not target_msg or not target_msg.sticker:
        await _reply(update, L(lang, "❗️ لطفاً روی یک استیکر ریپلای کن و بنویس: استیکر بن",
                               "❗️ Reply to a sticker and write: stickerban"))
        return
    add_blacklist_sticker(chat.id, target_msg.sticker.file_unique_id)
    try:
        await target_msg.delete()
    except Exception:
        pass
    await _reply(update, L(lang, "🚫 این استیکر به لیست سیاه اضافه شد و از این به بعد در گروه حذف می‌شود.",
                           "🚫 This sticker was added to the blacklist and will be deleted from now on."))


# ---- چک خودکار مدیا ----
async def check_media_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or chat.type not in GROUP_TYPES:
        return
    if not user or user.is_bot:
        return

    level = await get_permission_level(context.bot, chat.id, user.id)
    if level in ("creator", "group_owner", "admin"):
        return

    lang = get_lang(chat.id)
    checks = [
        (message.photo, "photos", L(lang, "عکس", "photos")),
        (message.video, "videos", L(lang, "فیلم", "videos")),
        (message.document, "documents", L(lang, "فایل", "files")),
        (message.animation, "gifs", L(lang, "گیف", "GIFs")),
        (message.sticker, "stickers", L(lang, "استیکر", "stickers")),
    ]
    for present, key, label in checks:
        if present and not is_feature_enabled(chat.id, key):
            try:
                await message.delete()
            except Exception:
                pass
            uname = f"@{user.username}" if user.username else user.full_name
            try:
                warn_msg = await context.bot.send_message(
                    chat.id, L(lang, f"🚫 {uname} شما اجازه ارسال {label} رو ندارید.",
                               f"🚫 {uname}, you are not allowed to send {label}."))
                context.job_queue.run_once(
                    _delete_message_later, when=5, data={"chat_id": chat.id, "message_id": warn_msg.message_id},
                    name=f"delmedia_{chat.id}_{warn_msg.message_id}")
            except Exception:
                pass
            return


async def check_blacklisted_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in GROUP_TYPES:
        return
    lang = get_lang(chat.id)
    user = update.effective_user
    uname = f"@{user.username}" if user and user.username else (user.full_name if user else "")

    kind = None
    if message.animation and is_gif_blacklisted(chat.id, message.animation.file_unique_id):
        kind = L(lang, "این گیف تو این گروه مجاز نیست.", "this GIF is not allowed in this group.")
    elif message.sticker and is_sticker_blacklisted(chat.id, message.sticker.file_unique_id):
        kind = L(lang, "این استیکر تو این گروه مجاز نیست.", "this sticker is not allowed in this group.")
    if not kind:
        return
    try:
        await message.delete()
    except Exception:
        pass
    try:
        warn_msg = await context.bot.send_message(chat.id, f"🚫 {uname} {kind}")
        context.job_queue.run_once(
            _delete_message_later, when=5, data={"chat_id": chat.id, "message_id": warn_msg.message_id},
            name=f"delblk_{chat.id}_{warn_msg.message_id}")
    except Exception:
        pass


# ============================================================================
# فیلتر فحش و اخطار خودکار / BAD WORDS FILTER
# ============================================================================
def normalize(text: str) -> str:
    return re.sub(r"[\s\.\-_\*]+", "", text)


def _en_pattern(word):
    sep = r"[\s\.\-_\*]*"
    body = sep.join(re.escape(c) for c in word) if len(word) >= 4 else re.escape(word)
    return re.compile(rf"(?<![a-z]){body}(?:s|es|ed)?(?![a-z])")


_EN_BAD_PATTERNS = [(w, _en_pattern(w)) for w in EN_BAD_WORDS + ["bullshit", "shitty", "fucked"]]


def contains_bad_word(text: str, bad_words):
    if not text:
        return None
    norm_text = normalize(text)
    lower_text = text.lower()
    for word in bad_words:
        if not word:
            continue
        if word in text or word.lower() in lower_text:
            return word
        if normalize(word) in norm_text:
            return word
    # فحش‌های انگلیسی: با مرز کلمه، تا «class» یا «document» اشتباهاً گرفته نشن
    for word, pattern in _EN_BAD_PATTERNS:
        if pattern.search(lower_text):
            return word
    return None


async def check_message_for_bad_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or chat.type not in GROUP_TYPES:
        return
    if not user or user.is_bot:
        return

    group = get_group(chat.id)
    if group and not group["is_active"]:
        return
    if not is_feature_enabled(chat.id, "bad_words"):
        return

    level = await get_permission_level(context.bot, chat.id, user.id)
    if level in ("creator", "group_owner", "admin"):
        return

    text = message.text or message.caption or ""
    matched = contains_bad_word(text, get_bad_words(chat.id))
    if not matched:
        return

    try:
        await message.delete()
    except Exception:
        pass

    lang = get_lang(chat.id)
    username = _display_name(user, lang)
    mention = _mention_html(user.id, username)

    new_level = get_active_warning_count(chat.id, user.id) + 1
    add_warning(chat.id, user.id, username, f"{REASON_BAD_WORD_PREFIX}{matched}", new_level)

    if new_level <= 3:
        wt = get_warning_text(chat.id, new_level)
        text_out = warning_text_for(wt, new_level, lang) or L(lang, f"اخطار {new_level}/3", f"Warning {new_level}/3")
        warning_msg = await context.bot.send_message(chat.id, f"⚠️ {mention}\n{escape(text_out)}", parse_mode="HTML")
        context.job_queue.run_once(
            _delete_message_later, when=60, data={"chat_id": chat.id, "message_id": warning_msg.message_id},
            name=f"delwarn_{chat.id}_{warning_msg.message_id}")
        if wt and wt["sticker_file_id"]:
            try:
                await context.bot.send_sticker(chat.id, wt["sticker_file_id"])
            except Exception:
                pass
        if wt and wt["gif_file_id"]:
            try:
                await context.bot.send_animation(chat.id, wt["gif_file_id"])
            except Exception:
                pass
        if wt and wt["photo_file_id"]:
            try:
                await context.bot.send_photo(chat.id, wt["photo_file_id"])
            except Exception:
                pass

    elif new_level in (4, 5):
        minutes = MUTE_DURATIONS_MIN[new_level]
        until_ts = time.time() + minutes * 60
        try:
            await context.bot.restrict_chat_member(
                chat.id, user.id, permissions=ChatPermissions(can_send_messages=False),
                until_date=utc_from_ts(until_ts))
        except Exception:
            pass
        add_mute(chat.id, user.id, username, REASON_MUTE_AUTO, True, until_ts)
        which = L(lang, "اول" if new_level == 4 else "دوم", "first" if new_level == 4 else "second")
        restriction = escape(build_restriction_message(tehran_from_ts(until_ts), chat.title, lang))
        mute_msg = await context.bot.send_message(
            chat.id,
            L(lang, f"🔇 {mention}\nبه دلیل تکرار بی‌ادبی، سکوت {which} فعال شد.\n{restriction}",
              f"🔇 {mention}\nDue to repeated rudeness, the {which} mute was activated.\n{restriction}"),
            parse_mode="HTML")
        context.job_queue.run_once(
            _delete_message_later, when=minutes * 60,
            data={"chat_id": chat.id, "message_id": mute_msg.message_id},
            name=f"delmute_{chat.id}_{mute_msg.message_id}")

    else:
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
        except Exception:
            pass
        add_ban(chat.id, user.id, username, REASON_BAN_AUTO, True)
        await context.bot.send_message(
            chat.id,
            L(lang, f"⛔️ {mention}\nبه دلیل تکرار سه‌باره بی‌ادبی، به‌صورت کامل از گروه بن شد.",
              f"⛔️ {mention}\nBanned from the group for repeated rudeness."),
            parse_mode="HTML")


# ============================================================================
# ویرایش اخطارها / WARNINGS EDITOR
# ============================================================================
def _warnedit_panel_content(chat_id, lang):
    text = L(lang,
             "✏️ ویرایش اخطارها\n\nبرای هر سطح اخطار، می‌توانید متن، استیکر، گیف یا عکس تنظیم کنید.\n\n"
             "سطح ۱ تا ۳: اخطار\nسطح ۴: سکوت ۵ دقیقه\nسطح ۵: سکوت ۱۰ دقیقه\nسطح ۶: بن کامل\n\n"
             "روی هر دکمه کلیک کنید تا آن سطح را ویرایش کنید:",
             "✏️ Edit warnings\n\nFor each warning level you can set a text, sticker, GIF or photo.\n\n"
             "Levels 1-3: warning\nLevel 4: 5-minute mute\nLevel 5: 10-minute mute\nLevel 6: full ban\n\n"
             "Tap a button to edit that level:")
    n = lambda x: L(lang, "۱۲۳"[x - 1], str(x))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚠️ {n(i)}", callback_data=f"warnedit_lvl:{chat_id}:{i}") for i in (1, 2, 3)],
        [InlineKeyboardButton(L(lang, "🔇 سکوت ۵ دقیقه", "🔇 Mute 5 min"), callback_data=f"warnedit_lvl:{chat_id}:4"),
         InlineKeyboardButton(L(lang, "🔇 سکوت ۱۰ دقیقه", "🔇 Mute 10 min"), callback_data=f"warnedit_lvl:{chat_id}:5")],
        [InlineKeyboardButton(L(lang, "⛔️ بن", "⛔️ Ban"), callback_data=f"warnedit_lvl:{chat_id}:6")],
        [InlineKeyboardButton(L(lang, "🔄 بازنشانی همه", "🔄 Reset all"), callback_data=f"warnedit_reset:{chat_id}")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")],
    ])
    return text, kb


def _deny(lang):
    return L(lang, "⛔️ اجازه ندارید.", "⛔️ You don't have permission.")


async def open_warnedit_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    text, kb = _warnedit_panel_content(chat_id, lang)
    await query.edit_message_text(text, reply_markup=kb)


def _level_panel_content(chat_id, level, lang):
    wt = get_warning_text(chat_id, level)
    shown = warning_text_for(wt, level, lang) if wt else None
    unset = L(lang, "تعیین نشده", "not set")
    text = L(lang, f"✏️ ویرایش اخطار سطح {level}\n\n", f"✏️ Edit warning level {level}\n\n")
    text += L(lang, "📝 متن فعلی: ", "📝 Current text: ") + (shown if shown else unset) + "\n"
    has_media = bool(wt and (wt["sticker_file_id"] or wt["gif_file_id"] or wt["photo_file_id"]))
    text += L(lang, "🖼 مدیا: ", "🖼 Media: ") + ("✔" if has_media else "✘") + "\n\n"
    text += L(lang, "برای ویرایش، روی دکمه‌های زیر کلیک کنید:", "Tap the buttons below to edit:")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "📝 ویرایش متن", "📝 Edit text"), callback_data=f"warnedit_text:{chat_id}:{level}")],
        [InlineKeyboardButton(L(lang, "🖼 افزودن مدیا", "🖼 Add media"), callback_data=f"warnedit_media:{chat_id}:{level}:any"),
         InlineKeyboardButton(L(lang, "🗑 پاک کردن مدیا", "🗑 Remove media"), callback_data=f"warnedit_media:{chat_id}:{level}:clear")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"warnedit_panel:{chat_id}")],
    ])
    return text, kb


async def open_level_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, chat_id, level = query.data.split(":")
    chat_id, level = int(chat_id), int(level)
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    text, kb = _level_panel_content(chat_id, level, lang)
    await query.edit_message_text(text, reply_markup=kb)


async def ask_warn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, chat_id, level = query.data.split(":")
    chat_id, level = int(chat_id), int(level)
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return

    context.user_data["waiting_for_warn_text"] = (chat_id, level)
    context.user_data["warnedit_prompt_chat_id"] = query.message.chat_id
    context.user_data["warnedit_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"),
                                                     callback_data=f"warnedit_lvl:{chat_id}:{level}")]])
    await query.edit_message_text(
        L(lang, f"📝 ویرایش متن اخطار سطح {level}\n\nلطفاً متن جدید را ارسال کنید.\n\nبرای لغو، دستور /cancel را بفرستید.",
          f"📝 Edit the text of warning level {level}\n\nPlease send the new text.\n\nTo cancel, send /cancel."),
        reply_markup=kb)


async def _return_to_level_panel(update, context, chat_id, level):
    prompt_chat_id = context.user_data.pop("warnedit_prompt_chat_id", None)
    prompt_message_id = context.user_data.pop("warnedit_prompt_message_id", None)
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    text, kb = _level_panel_content(chat_id, level, lang_of(update))
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id,
                                                text=text, reply_markup=kb)
            return
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=kb)


async def receive_warn_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_warn_text"):
        return False
    chat_id, level = context.user_data["waiting_for_warn_text"]
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    text = update.effective_message.text
    context.user_data["waiting_for_warn_text"] = None
    if text == "/cancel":
        await _return_to_level_panel(update, context, chat_id, level)
        return True
    set_warning_text(chat_id, level, text=text)
    await _return_to_level_panel(update, context, chat_id, level)
    return True


async def ask_warn_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, chat_id, level, media_type = query.data.split(":")
    chat_id, level = int(chat_id), int(level)
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return

    if media_type == "clear":
        clear_warning_media(chat_id, level)
        await query.answer(L(lang, "✔ مدیا پاک شد", "✔ Media removed"))
        await open_level_panel(update, context)
        return

    media_names = {
        "sticker": L(lang, "استیکر", "sticker"), "gif": L(lang, "گیف", "GIF"), "photo": L(lang, "عکس", "photo"),
        "any": L(lang, "استیکر، گیف یا عکس", "sticker, GIF or photo"),
    }
    context.user_data["waiting_for_warn_media"] = (chat_id, level, media_type)
    context.user_data["warnedit_prompt_chat_id"] = query.message.chat_id
    context.user_data["warnedit_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"),
                                                     callback_data=f"warnedit_lvl:{chat_id}:{level}")]])
    name = media_names.get(media_type, L(lang, "مدیا", "media"))
    await query.edit_message_text(
        L(lang, f"🖼 افزودن مدیا به اخطار سطح {level}\n\nلطفاً یک {name} ارسال کنید.\n\nبرای لغو، دستور /cancel را بفرستید.",
          f"🖼 Add media to warning level {level}\n\nPlease send a {name}.\n\nTo cancel, send /cancel."),
        reply_markup=kb)


async def receive_warn_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_warn_media"):
        return False
    chat_id, level, media_type = context.user_data["waiting_for_warn_media"]
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    message = update.effective_message
    lang = lang_of(update)

    if message.text == "/cancel":
        context.user_data["waiting_for_warn_media"] = None
        await _return_to_level_panel(update, context, chat_id, level)
        return True

    file_kind = file_id = None
    if media_type in ("sticker", "any") and message.sticker:
        file_kind, file_id = "sticker", message.sticker.file_id
    elif media_type in ("gif", "any") and message.animation:
        file_kind, file_id = "gif", message.animation.file_id
    elif media_type in ("photo", "any") and message.photo:
        file_kind, file_id = "photo", message.photo[-1].file_id
    else:
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text(L(lang, "❌ نوع مدیا اشتباه است. لطفاً یه استیکر، گیف یا عکس بفرست، یا /cancel رو بفرست.",
                                   "❌ Wrong media type. Please send a sticker, GIF or photo, or send /cancel."))
        return True

    if file_kind == "sticker":
        set_warning_text(chat_id, level, sticker_file_id=file_id)
    elif file_kind == "gif":
        set_warning_text(chat_id, level, gif_file_id=file_id)
    else:
        set_warning_text(chat_id, level, photo_file_id=file_id)
    context.user_data["waiting_for_warn_media"] = None
    await _return_to_level_panel(update, context, chat_id, level)
    return True


async def reset_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    for level in range(1, 7):
        reset_warning_text(chat_id, level)
    await query.answer(L(lang, "✔ بازنشانی شد", "✔ Reset done"))
    await open_warnedit_panel(update, context)


# ============================================================================
# خوش‌آمدگویی / WELCOME
# ============================================================================
WAITING_WELCOME_TEXT_KEY = "waiting_welcome_text_chat_id"
WAITING_WELCOME_MEDIA_KEY = "waiting_welcome_media_chat_id"


def render_welcome_text(template: str, user_name: str, group_title: str, lang="fa") -> str:
    now = now_tehran()
    return (template.replace("{user}", user_name).replace("{group}", group_title or "")
            .replace("{date}", format_date_only(now, lang)).replace("{time}", format_time_only(now, lang)))


async def on_new_member_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not message.new_chat_members:
        return
    me = await context.bot.get_me()
    real_members = [m for m in message.new_chat_members if m.id != me.id and not m.is_bot]
    if not real_members:
        return
    if not is_feature_enabled(chat.id, "welcome"):
        return

    lang = get_lang(chat.id)
    template = welcome_template(chat.id, lang)
    sticker_id, animation_id = get_welcome_media(chat.id)
    for member in real_members:
        user_name = f"@{member.username}" if member.username else member.full_name
        text = render_welcome_text(template, user_name, chat.title, lang)
        try:
            if animation_id:
                await context.bot.send_animation(chat.id, animation_id, caption=text)
            elif sticker_id:
                await context.bot.send_sticker(chat.id, sticker_id)
                await context.bot.send_message(chat.id, text)
            else:
                await context.bot.send_message(chat.id, text)
        except Exception:
            pass


def _welcome_panel_keyboard(chat_id, lang):
    enabled = is_feature_enabled(chat_id, "welcome")
    toggle = (
        InlineKeyboardButton(L(lang, "❌ خاموش کردن خوش‌آمدگویی", "❌ Turn welcome off"), callback_data=f"wc_off:{chat_id}")
        if enabled else
        InlineKeyboardButton(L(lang, "✅ روشن کردن خوش‌آمدگویی", "✅ Turn welcome on"), callback_data=f"wc_on:{chat_id}")
    )
    sticker_id, animation_id = get_welcome_media(chat_id)
    media_row = (
        [InlineKeyboardButton(L(lang, "🗑 حذف گیف/استیکر", "🗑 Remove GIF/sticker"), callback_data=f"wc_media_clear:{chat_id}")]
        if (sticker_id or animation_id) else
        [InlineKeyboardButton(L(lang, "🖼 افزودن گیف/استیکر", "🖼 Add GIF/sticker"), callback_data=f"wc_media:{chat_id}")]
    )
    return InlineKeyboardMarkup([
        [toggle],
        [InlineKeyboardButton(L(lang, "✏️ نوشتن متن دلخواه", "✏️ Write custom text"), callback_data=f"wc_edit:{chat_id}")] + media_row,
        [InlineKeyboardButton(L(lang, "👁 دیدن متن فعلی", "👁 Preview current text"), callback_data=f"wc_preview:{chat_id}"),
         InlineKeyboardButton(L(lang, "↩️ برگردوندن به پیش‌فرض", "↩️ Reset to default"), callback_data=f"wc_reset:{chat_id}")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")],
    ])


def _welcome_panel_text(chat_id, lang, extra_line=None):
    on = is_feature_enabled(chat_id, "welcome")
    status = L(lang, "✅ فعال" if on else "❌ غیرفعال", "✅ On" if on else "❌ Off")
    if lang == "en":
        text = ("👋 Welcome message settings\n\nStatus: " + status +
                "\n\nYou can write your own text and use these placeholders:\n"
                "{user} = new member's name\n{group} = group name\n{date} = date\n{time} = time")
    else:
        text = ("👋 تنظیمات خوش‌آمدگویی\n\nوضعیت: " + status +
                "\n\nمی‌تونی متن دلخواه بنویسی و از این کلمات استفاده کنی:\n"
                "{user} = اسم عضو جدید\n{group} = اسم گروه\n{date} = تاریخ\n{time} = ساعت")
    if extra_line:
        text = f"{extra_line}\n\n{text}"
    return text


async def _welcome_guard(update, context, chat_id, answer_first=False):
    """بررسی دسترسی؛ True یعنی اجازه داره"""
    query = update.callback_query
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang_of(update)), show_alert=True)
        return False
    return True


async def open_welcome_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _welcome_guard(update, context, chat_id):
        return
    await query.edit_message_text(_welcome_panel_text(chat_id, lang), reply_markup=_welcome_panel_keyboard(chat_id, lang))


async def toggle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, chat_id = query.data.split(":")
    chat_id = int(chat_id)
    if not await _welcome_guard(update, context, chat_id):
        return
    set_feature_enabled(chat_id, "welcome", action == "wc_on")
    await query.answer(L(lang_of(update), "ذخیره شد ✅", "Saved ✅"))
    await open_welcome_panel(update, context)


async def preview_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    user = update.effective_user
    if not await _welcome_guard(update, context, chat_id):
        return
    await query.answer()

    glang = get_lang(chat_id)  # نمونه با زبان خودِ گروه ساخته می‌شه
    template = welcome_template(chat_id, glang)
    group = get_group(chat_id)
    sample = render_welcome_text(template, L(glang, "@نمونه_کاربر", "@sample_user"),
                                 group["title"] if group else "", glang)
    sticker_id, animation_id = get_welcome_media(chat_id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"wc_panel:{chat_id}")]])
    if animation_id:
        try:
            await context.bot.send_animation(chat_id=user.id, animation=animation_id, caption=sample)
        except Exception:
            pass
    elif sticker_id:
        try:
            await context.bot.send_sticker(chat_id=user.id, sticker=sticker_id)
        except Exception:
            pass
    await query.edit_message_text(L(lang, f"👁 نمونه‌ی پیام خوش‌آمد:\n\n{sample}", f"👁 Welcome message preview:\n\n{sample}"),
                                  reply_markup=kb)


async def reset_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    if not await _welcome_guard(update, context, chat_id):
        return
    reset_welcome_text(chat_id)
    await query.answer(L(lang_of(update), "به پیش‌فرض برگشت ✅", "Reset to default ✅"))
    await open_welcome_panel(update, context)


async def ask_edit_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _welcome_guard(update, context, chat_id):
        return
    await query.answer()
    context.user_data[WAITING_WELCOME_TEXT_KEY] = chat_id
    context.user_data["welcome_prompt_chat_id"] = query.message.chat_id
    context.user_data["welcome_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ انصراف", "⬅️ Cancel"), callback_data=f"wc_panel:{chat_id}")]])
    await query.edit_message_text(
        L(lang, "✏️ متن جدید خوش‌آمدگویی رو همینجا بفرست.\n\nمی‌تونی از {user}، {group}، {date}، {time} استفاده کنی.",
          "✏️ Send the new welcome text here.\n\nYou can use {user}, {group}, {date}, {time}."),
        reply_markup=kb)


async def _return_to_welcome_panel(update, context, chat_id, confirm_line):
    prompt_chat_id = context.user_data.pop("welcome_prompt_chat_id", None)
    prompt_message_id = context.user_data.pop("welcome_prompt_message_id", None)
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    lang = lang_of(update)
    text = _welcome_panel_text(chat_id, lang, extra_line=confirm_line)
    kb = _welcome_panel_keyboard(chat_id, lang)
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id,
                                                text=text, reply_markup=kb)
            return
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=kb)


async def receive_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = context.user_data.get(WAITING_WELCOME_TEXT_KEY)
    if not chat_id:
        return False
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    lang = lang_of(update)
    context.user_data[WAITING_WELCOME_TEXT_KEY] = None
    new_text = update.effective_message.text
    if new_text == "/cancel":
        await _return_to_welcome_panel(update, context, chat_id, L(lang, "❌ لغو شد.", "❌ Cancelled."))
        return True
    set_welcome_text(chat_id, new_text)
    await _return_to_welcome_panel(update, context, chat_id,
                                   L(lang, f"✔ متن خوش‌آمدگویی ذخیره شد:\n\n{new_text}",
                                     f"✔ Welcome text saved:\n\n{new_text}"))
    return True


async def ask_add_welcome_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _welcome_guard(update, context, chat_id):
        return
    await query.answer()
    context.user_data[WAITING_WELCOME_MEDIA_KEY] = chat_id
    context.user_data["welcome_prompt_chat_id"] = query.message.chat_id
    context.user_data["welcome_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ انصراف", "⬅️ Cancel"), callback_data=f"wc_panel:{chat_id}")]])
    await query.edit_message_text(
        L(lang, "🖼 یک گیف یا استیکر همینجا برام بفرست تا برای خوش‌آمدگویی ذخیره بشه.",
          "🖼 Send me a GIF or sticker here to save it for the welcome message."),
        reply_markup=kb)


async def receive_welcome_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = context.user_data.get(WAITING_WELCOME_MEDIA_KEY)
    if not chat_id:
        return False
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    message = update.effective_message
    lang = lang_of(update)

    if message.text == "/cancel":
        context.user_data[WAITING_WELCOME_MEDIA_KEY] = None
        await _return_to_welcome_panel(update, context, chat_id, L(lang, "❌ لغو شد.", "❌ Cancelled."))
        return True

    if message.sticker:
        set_welcome_media(chat_id, sticker_file_id=message.sticker.file_id)
        kind = L(lang, "استیکر", "Sticker")
    elif message.animation:
        set_welcome_media(chat_id, animation_file_id=message.animation.file_id)
        kind = L(lang, "گیف", "GIF")
    else:
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text(L(lang, "❗️ این گیف یا استیکر نبود. لطفاً یه گیف یا استیکر بفرست، یا /cancel رو بفرست.",
                                   "❗️ That wasn't a GIF or sticker. Please send a GIF or sticker, or send /cancel."))
        return True

    context.user_data[WAITING_WELCOME_MEDIA_KEY] = None
    await _return_to_welcome_panel(update, context, chat_id,
                                   L(lang, f"✔ {kind} برای خوش‌آمدگویی ذخیره شد.", f"✔ {kind} saved for the welcome message."))
    return True


async def clear_welcome_media_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    if not await _welcome_guard(update, context, chat_id):
        return
    clear_welcome_media(chat_id)
    await query.answer(L(lang_of(update), "حذف شد ✅", "Removed ✅"))
    await open_welcome_panel(update, context)


# ============================================================================
# گزارش / REPORTS
# ============================================================================
async def cmd_gozaresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or chat.type not in GROUP_TYPES:
        return
    lang = lang_of(update)

    if not message.reply_to_message:
        await message.reply_text(L(lang, "❗️ برای گزارش، روی پیام مورد نظر ریپلای کن و بنویس: گزارش",
                                   "❗️ To report, reply to the message and write: report"))
        return
    reported = message.reply_to_message.from_user
    if not reported:
        return

    reporter_name = f"@{user.username}" if user.username else user.full_name
    reported_name = f"@{reported.username}" if reported.username else reported.full_name
    snippet = (message.reply_to_message.text or message.reply_to_message.caption or NO_TEXT_SNIPPET)[:200]
    add_report(chat.id, user.id, reporter_name, reported.id, reported_name, snippet)
    try:
        await message.reply_text(L(lang, "✔ گزارش شما ثبت شد و به مالک گروه اطلاع داده می‌شود.",
                                   "✔ Your report was recorded and the group owner will be notified."))
    except Exception:
        pass


async def send_pending_reports_job(context: ContextTypes.DEFAULT_TYPE):
    """هر ۲ دقیقه: گزارش‌های جمع‌شده هر گروه رو یک‌جا برای مالک همون گروه می‌فرسته"""
    for chat_id in get_chats_with_pending_reports():
        reports = get_pending_reports(chat_id)
        if not reports:
            continue
        group = get_group(chat_id)
        owner_id = group["added_by_user_id"] if group else None
        if not owner_id:
            mark_reports_sent(chat_id)
            continue

        lang = get_lang(owner_id)  # گزارش تو پی‌وی مالک می‌ره، پس با زبان پی‌وی خودش
        title = group["title"] if group and group["title"] else str(chat_id)
        lines = [L(lang, f"🚨 گزارش‌های جدید گروه «{title}» ({len(reports)} مورد):\n",
                   f"🚨 New reports for the group \"{title}\" ({len(reports)}):\n")]
        for r in reports:
            snippet = reason_text(r["message_snippet"], lang)
            lines.append(L(lang,
                           f"• {r['reporter_username']} گزارش داد از {r['reported_username']}\n  متن: {snippet}",
                           f"• {r['reporter_username']} reported {r['reported_username']}\n  Text: {snippet}"))
        try:
            await context.bot.send_message(owner_id, "\n\n".join(lines)[:4000])
            mark_reports_sent(chat_id)
        except Exception:
            pass


# ============================================================================
# پنل مدیریت گروه در پی‌وی / GROUP ADMIN PANEL (DM)
# ============================================================================
def _fmt_time(ts, lang="fa"):
    if not ts:
        return "-"
    return tehran_from_ts(ts).strftime("%Y-%m-%d %H:%M")


def _back_kb(lang, cb):
    return InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=cb)]])


async def _user_can_see_group(bot, user_id, chat_id):
    return await can_access_dm_panel(bot, chat_id, user_id)


async def _panel_guard(update, context, chat_id):
    """اگه کاربر دسترسی نداشته باشه، پیام مناسب رو نشون می‌ده و False برمی‌گردونه"""
    if await _user_can_see_group(context.bot, update.effective_user.id, chat_id):
        return True
    await update.callback_query.answer(_deny(lang_of(update)), show_alert=True)
    return False


async def show_my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    lang = lang_of(update)
    if query:
        await query.answer()

    if await is_creator(user.id):
        groups = get_all_groups()
    else:
        groups = []
        for g in get_all_groups():
            if await is_telegram_group_creator(context.bot, g["chat_id"], user.id):
                groups.append(g)

    if not groups:
        text = L(lang, "شما مالک هیچ گروهی که ربات توشه نیستید.", "You don't own any group that the bot is in.")
        kb = _back_kb(lang, "start_menu")
        if query:
            await query.edit_message_text(text, reply_markup=kb)
        else:
            await update.effective_message.reply_text(text, reply_markup=kb)
        return

    rows = []
    for g in groups:
        status = "🟢" if g["is_active"] else "🔴"
        lock = "🔒" if g["is_locked"] else "🔓"
        toggle_cb = f"grp_active_off:{g['chat_id']}" if g["is_active"] else f"grp_active_on:{g['chat_id']}"
        rows.append([
            InlineKeyboardButton(f"{status}{lock} {g['title'] or g['chat_id']}", callback_data=f"grp_open:{g['chat_id']}"),
            InlineKeyboardButton(L(lang, "🔁 روشن/خاموش", "🔁 On/Off"), callback_data=toggle_cb),
        ])
    rows.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data="start_menu")])

    text = L(lang, "⚙️ پنل مدیریت گروه\n\nیکی از گروه‌های زیر رو انتخاب کن:",
             "⚙️ Group admin panel\n\nChoose one of the groups below:")
    markup = InlineKeyboardMarkup(rows)
    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def _group_panel_keyboard(chat_id, group, lang):
    B = lambda fa, en, cb: InlineKeyboardButton(L(lang, fa, en), callback_data=cb)
    lock_btn = (B("🔓 باز کردن گروه", "🔓 Unlock group", f"grp_unlock:{chat_id}") if group["is_locked"]
                else B("🔒 قفل کردن گروه", "🔒 Lock group", f"grp_lock:{chat_id}"))
    return InlineKeyboardMarkup([
        [lock_btn, B("⛔️ بن‌شده‌ها", "⛔️ Banned", f"grp_banned:{chat_id}")],
        [B("🔇 سکوت‌خورده‌ها", "🔇 Muted", f"grp_muted:{chat_id}"), B("⚠️ اخطارها", "⚠️ Warnings", f"grp_warned:{chat_id}")],
        [B("🧩 تغییر قابلیت‌ها", "🧩 Features", f"grp_features:{chat_id}"), B("👋 خوش‌آمدگویی", "👋 Welcome", f"wc_panel:{chat_id}")],
        [B("📩 گزارش‌ها", "📩 Reports", f"grp_reports:{chat_id}"), B("✏️ ویرایش اخطارها", "✏️ Edit warnings", f"warnedit_panel:{chat_id}")],
        [B("🌐 ترجمه", "🌐 Translation", f"tr_panel:{chat_id}"), B("🧹 پاک‌سازی خودکار", "🧹 Auto cleanup", f"cln_panel:{chat_id}")],
        [B("🗣 زبان عکس قیمت‌ها", "🗣 Price image language", f"imglang_panel:{chat_id}"),
         B("🚫 کلمات غیرمجاز", "🚫 Banned words", f"badwords_panel:{chat_id}")],
        [B("🔤 میان‌برهای دستورات", "🔤 Command shortcuts", f"cmdshortcuts_panel:{chat_id}"),
         B("🌐 زبان گروه", "🌐 Group language", f"lang_open:{chat_id}")],
        [B("⬅️ بازگشت به لیست گروه‌ها", "⬅️ Back to group list", "panel_my_groups")],
    ])


def _render_group_panel(group, lang):
    lock_info = ""
    if group["is_locked"] and group["lock_until"]:
        remain = int(group["lock_until"] - time.time())
        if remain > 0:
            lock_info = L(lang, f"\n⏳ باز شدن خودکار تا {remain // 60} دقیقه دیگر",
                          f"\n⏳ Unlocks automatically in {remain // 60} min")
    bot_state = L(lang, "🟢 فعال" if group["is_active"] else "🔴 خاموش", "🟢 Active" if group["is_active"] else "🔴 Off")
    grp_state = L(lang, "🔒 قفل" if group["is_locked"] else "🔓 باز", "🔒 Locked" if group["is_locked"] else "🔓 Open")
    text = L(lang,
             f"⚙️ پنل مدیریت گروه: {group['title']}\n\nوضعیت ربات: {bot_state}\nوضعیت گروه: {grp_state}{lock_info}",
             f"⚙️ Group admin panel: {group['title']}\n\nBot status: {bot_state}\nGroup status: {grp_state}{lock_info}")
    return text, _group_panel_keyboard(group["chat_id"], group, lang)


async def send_group_panel_message(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    lang = lang_of(update)
    group = get_group(chat_id)
    if not group:
        await update.effective_message.reply_text(L(lang, "این گروه پیدا نشد.", "Group not found."))
        return
    text, kb = _render_group_panel(group, lang)
    await update.effective_message.reply_text(text, reply_markup=kb)


async def open_group_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _user_can_see_group(context.bot, update.effective_user.id, chat_id):
        await query.answer(L(lang, "⛔️ این گروه مال شما نیست.", "⛔️ This group isn't yours."), show_alert=True)
        return
    group = get_group(chat_id)
    if not group:
        await query.edit_message_text(L(lang, "این گروه پیدا نشد.", "Group not found."))
        return
    text, kb = _render_group_panel(group, lang)
    await query.edit_message_text(text, reply_markup=kb)


async def toggle_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, chat_id = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return

    if action == "grp_lock":
        try:
            await context.bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=False))
        except Exception:
            pass
        set_group_lock(chat_id, True, None)
        await query.answer(L(lang, "گروه قفل شد ✔", "Group locked ✔"))
    else:
        try:
            await context.bot.set_chat_permissions(chat_id, _full_perms())
        except Exception:
            pass
        for job in context.job_queue.get_jobs_by_name(f"unlock_{chat_id}"):
            job.schedule_removal()
        set_group_lock(chat_id, False, None)
        await query.answer(L(lang, "گروه باز شد ✔", "Group unlocked ✔"))

    text, kb = _render_group_panel(get_group(chat_id), lang)
    await query.edit_message_text(text, reply_markup=kb)


async def toggle_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, chat_id = query.data.split(":")
    chat_id = int(chat_id)
    if not await _panel_guard(update, context, chat_id):
        return
    set_group_active(chat_id, action == "grp_active_on")
    await query.answer(L(lang_of(update), "ذخیره شد ✔", "Saved ✔"))
    await show_my_groups(update, context)


async def show_banned_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return

    rows = get_all_banned_users(chat_id)
    if not rows:
        text = L(lang, "⛔️ لیست بن‌شده‌ها\n\nهیچ کاربری بن نشده.", "⛔️ Banned users\n\nNo user is banned.")
    else:
        lines = [L(lang, "⛔️ لیست بن‌شده‌ها:\n", "⛔️ Banned users:\n")]
        for r in rows:
            reason = reason_text(r["reason"], lang) if r["has_reason"] and r["reason"] else L(lang, "بدون دلیل ثبت‌شده", "no reason given")
            lines.append(f"• {r['username']} — {reason} ({_fmt_time(r['banned_at'])})")
        text = "\n".join(lines)
    await query.edit_message_text(text[:4000], reply_markup=_back_kb(lang, f"grp_open:{chat_id}"))


def _muted_list_content(chat_id, lang):
    rows = get_active_mutes(chat_id)
    if not rows:
        return (L(lang, "🔇 لیست سکوت‌خورده‌ها\n\nهیچ کاربری سکوت نیست.", "🔇 Muted users\n\nNo user is muted."),
                _back_kb(lang, f"grp_open:{chat_id}"))
    text = L(lang, "🔇 لیست سکوت‌خورده‌ها:\n\nروی هرکس بزنید تا آزادش کنید یا مدت سکوتش رو تغییر بدید.",
             "🔇 Muted users:\n\nTap a user to free them or change the mute duration.")
    rows_kb = []
    for r in rows:
        until = _fmt_time(r["until_at"]) if r["until_at"] else L(lang, "نامحدود", "unlimited")
        rows_kb.append([InlineKeyboardButton(
            L(lang, f"{r['username']} — تا {until}", f"{r['username']} — until {until}"),
            callback_data=f"mute_user:{chat_id}:{r['user_id']}")])
    rows_kb.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")])
    return text, InlineKeyboardMarkup(rows_kb)


async def show_muted_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    if not await _panel_guard(update, context, chat_id):
        return
    text, kb = _muted_list_content(chat_id, lang_of(update))
    await query.edit_message_text(text, reply_markup=kb)


async def _render_mute_detail(query, chat_id, target_id, lang):
    record = get_mute_record(chat_id, target_id)
    if not record:
        await query.edit_message_text(L(lang, "این کاربر دیگه سکوت نیست.", "This user is no longer muted."),
                                      reply_markup=_back_kb(lang, f"grp_muted:{chat_id}"))
        return
    reason = reason_text(record["reason"], lang) if record["has_reason"] and record["reason"] else L(lang, "بدون دلیل ثبت‌شده", "no reason given")
    until = _fmt_time(record["until_at"]) if record["until_at"] else L(lang, "نامحدود", "unlimited")
    text = L(lang, f"🔇 {record['username']}\n\nدلیل: {reason}\nسکوت تا: {until}",
             f"🔇 {record['username']}\n\nReason: {reason}\nMuted until: {until}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "🔊 آزاد کردن فوری", "🔊 Free now"), callback_data=f"mute_release:{chat_id}:{target_id}")],
        [InlineKeyboardButton(L(lang, "✏️ تغییر مدت زمان", "✏️ Change duration"), callback_data=f"mute_edit:{chat_id}:{target_id}")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_muted:{chat_id}")],
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def show_mute_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, chat_id, target_id = query.data.split(":")
    chat_id, target_id = int(chat_id), int(target_id)
    if not await _panel_guard(update, context, chat_id):
        return
    await _render_mute_detail(query, chat_id, target_id, lang_of(update))


async def release_mute_from_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, target_id = query.data.split(":")
    chat_id, target_id = int(chat_id), int(target_id)
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    try:
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=_full_perms())
    except Exception:
        pass
    remove_mute(chat_id, target_id)
    await query.answer(L(lang, "آزاد شد ✔", "Freed ✔"))
    text, kb = _muted_list_content(chat_id, lang)
    await query.edit_message_text(text, reply_markup=kb)


DURATION_PRESETS = [
    ("5 دقیقه", "5 min", 5), ("10 دقیقه", "10 min", 10), ("30 دقیقه", "30 min", 30),
    ("1 ساعت", "1 hour", 60), ("3 ساعت", "3 hours", 180), ("12 ساعت", "12 hours", 720), ("24 ساعت", "24 hours", 1440),
]


async def ask_edit_mute_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, chat_id, target_id = query.data.split(":")
    chat_id, target_id = int(chat_id), int(target_id)
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return

    rows_kb, line = [], []
    for fa, en, minutes in DURATION_PRESETS:
        line.append(InlineKeyboardButton(L(lang, fa, en), callback_data=f"mute_setdur:{chat_id}:{target_id}:{minutes}"))
        if len(line) == 2:
            rows_kb.append(line)
            line = []
    if line:
        rows_kb.append(line)
    rows_kb.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"mute_user:{chat_id}:{target_id}")])
    await query.edit_message_text(L(lang, "⏱ مدت زمان جدید سکوت رو انتخاب کن:", "⏱ Choose the new mute duration:"),
                                  reply_markup=InlineKeyboardMarkup(rows_kb))


async def set_mute_duration_from_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, target_id, minutes = query.data.split(":")
    chat_id, target_id, minutes = int(chat_id), int(target_id), int(minutes)
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    until_ts = time.time() + minutes * 60
    try:
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False),
                                               until_date=utc_from_ts(until_ts))
    except Exception:
        pass
    update_mute_duration(chat_id, target_id, until_ts)
    await query.answer(L(lang, "مدت زمان ذخیره شد ✔", "Duration saved ✔"))
    await _render_mute_detail(query, chat_id, target_id, lang)


async def show_warned_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    rows = get_all_warned_users(chat_id)
    if not rows:
        text = L(lang, "⚠️ لیست اخطارگرفته‌ها\n\nهیچ کاربری اخطار نگرفته.", "⚠️ Warned users\n\nNo user has been warned.")
    else:
        lines = [L(lang, "⚠️ لیست اخطارگرفته‌ها:\n", "⚠️ Warned users:\n")]
        for r in rows:
            lines.append(L(lang, f"• {r['username']} — {r['cnt']} اخطار (آخرین: {_fmt_time(r['last_at'])})",
                           f"• {r['username']} — {r['cnt']} warning(s) (last: {_fmt_time(r['last_at'])})"))
        text = "\n".join(lines)
    await query.edit_message_text(text[:4000], reply_markup=_back_kb(lang, f"grp_open:{chat_id}"))


def _build_features_keyboard(chat_id, lang):
    def btn(key):
        icon = "✔" if is_feature_enabled(chat_id, key) else "✘"
        return InlineKeyboardButton(f"{icon} {feature_label(lang, key)}", callback_data=f"feat_toggle:{chat_id}:{key}")
    return InlineKeyboardMarkup([
        [btn("bad_words"), btn("games")], [btn("gifs"), btn("stickers")],
        [btn("photos"), btn("videos")], [btn("documents"), btn("date")],
        [btn("dollar"), btn("translate")], [btn("ai_chat"), btn("stats")],
        [btn("convert"), btn("chart")], [btn("instagram_dl")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")],
    ])


def _features_text(lang):
    return L(lang, "🧩 روشن/خاموش قابلیت‌ها\n\nروی هرکدوم بزن تا عوض بشه.",
             "🧩 Turn features on/off\n\nTap a feature to toggle it.")


async def show_features_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    await query.edit_message_text(_features_text(lang), reply_markup=_build_features_keyboard(chat_id, lang))


async def toggle_feature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, feature_key = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    new_state = not is_feature_enabled(chat_id, feature_key)
    set_feature_enabled(chat_id, feature_key, new_state)
    await query.answer(L(lang, "روشن شد ✔" if new_state else "خاموش شد ✘", "Turned on ✔" if new_state else "Turned off ✘"))
    await query.edit_message_text(_features_text(lang), reply_markup=_build_features_keyboard(chat_id, lang))


async def show_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    rows = get_all_reports(chat_id)
    if not rows:
        await query.edit_message_text(L(lang, "📩 گزارش‌ها\n\nهیچ گزارشی ثبت نشده.", "📩 Reports\n\nNo reports yet."),
                                      reply_markup=_back_kb(lang, f"grp_open:{chat_id}"))
        return
    rows_kb = [[InlineKeyboardButton(f"👤 {r['reported_username']} ({_fmt_time(r['created_at'])})",
                                     callback_data=f"report_open:{r['id']}")] for r in rows]
    rows_kb.append([InlineKeyboardButton(L(lang, "🗑 پاک کردن همه گزارش‌ها", "🗑 Clear all reports"),
                                         callback_data=f"reports_clear:{chat_id}")])
    rows_kb.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")])
    await query.edit_message_text(
        L(lang, "📩 گزارش‌های اخیر\n\nروی هرکدوم بزن برای جزئیات و اقدام:",
          "📩 Recent reports\n\nTap one for details and actions:"),
        reply_markup=InlineKeyboardMarkup(rows_kb))


async def open_report_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    report_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    report = get_report_by_id(report_id)
    if not report:
        await query.edit_message_text(L(lang, "این گزارش دیگه پیدا نشد.", "This report no longer exists."))
        return
    chat_id = report["chat_id"]
    if not await _panel_guard(update, context, chat_id):
        return
    snippet = reason_text(report["message_snippet"], lang)
    text = L(lang,
             f"📩 جزئیات گزارش\n\nگزارش‌دهنده: {report['reporter_username']}\nگزارش‌شده: {report['reported_username']}\n"
             f"متن: {snippet}\nزمان: {_fmt_time(report['created_at'])}",
             f"📩 Report details\n\nReporter: {report['reporter_username']}\nReported: {report['reported_username']}\n"
             f"Text: {snippet}\nTime: {_fmt_time(report['created_at'])}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "🔇 سکوت", "🔇 Mute"), callback_data=f"report_act:{report_id}:mute"),
         InlineKeyboardButton(L(lang, "⛔️ بن", "⛔️ Ban"), callback_data=f"report_act:{report_id}:ban"),
         InlineKeyboardButton(L(lang, "⚠️ اخطار", "⚠️ Warn"), callback_data=f"report_act:{report_id}:warn")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_reports:{chat_id}")],
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def handle_report_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, report_id, action = query.data.split(":")
    report_id = int(report_id)
    lang = lang_of(update)
    report = get_report_by_id(report_id)
    if not report:
        await query.answer(L(lang, "این گزارش دیگه پیدا نشد.", "This report no longer exists."), show_alert=True)
        return
    chat_id = report["chat_id"]
    target_id = report["reported_user_id"]
    target_name = report["reported_username"]
    if not await _panel_guard(update, context, chat_id):
        return

    if action == "mute":
        until_ts = time.time() + 10 * 60
        try:
            await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False),
                                                   until_date=utc_from_ts(until_ts))
        except Exception:
            pass
        add_mute(chat_id, target_id, target_name, REASON_REPORT, True, until_ts)
        await query.answer(L(lang, "سکوت ۱۰ دقیقه‌ای اعمال شد ✔", "10-minute mute applied ✔"))
    elif action == "ban":
        try:
            await context.bot.ban_chat_member(chat_id, target_id)
        except Exception:
            pass
        add_ban(chat_id, target_id, target_name, REASON_REPORT, True)
        await query.answer(L(lang, "بن شد ✔", "Banned ✔"))
    elif action == "warn":
        level = get_active_warning_count(chat_id, target_id) + 1
        add_warning(chat_id, target_id, target_name, REASON_REPORT, level)
        await query.answer(L(lang, f"اخطار سطح {level} ثبت شد ✔", f"Warning level {level} recorded ✔"))
    await open_report_detail(update, context)


async def clear_reports_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    if not await _panel_guard(update, context, chat_id):
        return
    clear_reports(chat_id)
    await query.answer(L(lang_of(update), "پاک شد ✔", "Cleared ✔"))
    await show_reports_list(update, context)


# ---- کلمات غیرمجاز ----
def _get_chat_specific_bad_words(chat_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT word FROM bad_words WHERE chat_id=?", (chat_id,))
        return [r["word"] for r in c.fetchall()]


def _bad_words_text(chat_id, lang):
    words = _get_chat_specific_bad_words(chat_id)
    head = L(lang, "🚫 کلمات غیرمجاز این گروه\n", "🚫 Banned words of this group\n")
    if not words:
        body = L(lang, "هنوز کلمه‌ای اختصاصی برای این گروه اضافه نشده.\n(کلمات پیش‌فرض مشترک بین همه گروه‌ها همچنان فعالن.)",
                 "No custom word has been added for this group yet.\n(The default words shared by all groups are still active.)")
    else:
        body = L(lang, "روی هرکدوم بزن تا حذفش کنی:", "Tap a word to delete it:")
    return head + "\n" + body


def _bad_words_keyboard(chat_id, lang):
    rows_kb = [[InlineKeyboardButton(f"🗑 {w}", callback_data=f"badwords_del:{chat_id}:{i}")]
               for i, w in enumerate(_get_chat_specific_bad_words(chat_id))]
    rows_kb.append([InlineKeyboardButton(L(lang, "➕ افزودن کلمه جدید", "➕ Add a new word"), callback_data=f"badwords_add:{chat_id}")])
    rows_kb.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")])
    return InlineKeyboardMarkup(rows_kb)


async def _render_bad_words_panel(query, chat_id, lang):
    await query.edit_message_text(_bad_words_text(chat_id, lang), reply_markup=_bad_words_keyboard(chat_id, lang))


async def open_bad_words_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    if not await _panel_guard(update, context, chat_id):
        return
    await _render_bad_words_panel(query, chat_id, lang_of(update))


async def ask_add_bad_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    context.user_data["waiting_for_bad_word_chat_id"] = chat_id
    context.user_data["bad_word_prompt_chat_id"] = query.message.chat_id
    context.user_data["bad_word_prompt_message_id"] = query.message.message_id
    await query.edit_message_text(
        L(lang, "➕ افزودن کلمه غیرمجاز\n\nلطفاً کلمه‌ای که می‌خوای فیلتر بشه رو تایپ و ارسال کن.\nبرای لغو، دستور /cancel رو بفرست.",
          "➕ Add a banned word\n\nPlease type and send the word you want to filter.\nTo cancel, send /cancel."),
        reply_markup=_back_kb(lang, f"badwords_panel:{chat_id}"))


async def receive_bad_word_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.user_data.get("waiting_for_bad_word_chat_id")
    if not chat_id:
        return False
    if not await _user_can_see_group(context.bot, update.effective_user.id, chat_id):
        return False
    lang = lang_of(update)
    prompt_chat_id = context.user_data.get("bad_word_prompt_chat_id")
    prompt_message_id = context.user_data.get("bad_word_prompt_message_id")
    text = (update.effective_message.text or "").strip()

    async def _finish(feedback_line):
        for k in ("waiting_for_bad_word_chat_id", "bad_word_prompt_chat_id", "bad_word_prompt_message_id"):
            context.user_data.pop(k, None)
        panel_text = f"{feedback_line}\n\n{_bad_words_text(chat_id, lang)}"
        panel_kb = _bad_words_keyboard(chat_id, lang)
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        if prompt_chat_id and prompt_message_id:
            try:
                await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id,
                                                    text=panel_text, reply_markup=panel_kb)
                return
            except Exception:
                pass
        await update.effective_message.reply_text(panel_text, reply_markup=panel_kb)

    if text == "/cancel":
        await _finish(L(lang, "❌ لغو شد.", "❌ Cancelled."))
        return True
    if not text:
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        return True
    add_bad_word(chat_id, text)
    await _finish(L(lang, f"✔ کلمه‌ی «{text}» اضافه شد.", f"✔ The word \"{text}\" was added."))
    return True


async def delete_bad_word_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, idx = query.data.split(":")
    chat_id, idx = int(chat_id), int(idx)
    lang = lang_of(update)
    if not await _panel_guard(update, context, chat_id):
        return
    words = _get_chat_specific_bad_words(chat_id)
    if 0 <= idx < len(words):
        with get_conn() as conn:
            conn.execute("DELETE FROM bad_words WHERE word=? AND chat_id=?", (words[idx], chat_id))
        await query.answer(L(lang, "✔ حذف شد", "✔ Deleted"))
    else:
        await query.answer(L(lang, "این کلمه دیگه پیدا نشد.", "This word no longer exists."))
    await _render_bad_words_panel(query, chat_id, lang)


# ============================================================================
# ردیابی سطح پنل / NAV
# ============================================================================
LEVEL0_PREFIXES = ("start_menu", "help_commands", "creator_panel_open", "restart_bot",
                   "ai_model_select", "ai_use_gemini", "ai_use_chatgpt")
LEVEL1_PREFIXES = ("panel_my_groups", "grp_active_on:", "grp_active_off:")
LEVEL2_PREFIXES = ("grp_open:",)
LEVEL3_NO_CHATID_PREFIXES = ("report_open:", "report_act:")
LEVEL3_PREFIXES = (
    "grp_banned:", "grp_muted:", "grp_warned:", "grp_features:", "feat_toggle:",
    "wc_", "grp_reports:", "reports_clear:", "warnedit_", "tr_panel:", "tr_set:",
    "cln_", "mute_", "report_open:", "report_act:", "imglang_",
)


def _clear_waiting_states(context: ContextTypes.DEFAULT_TYPE):
    """زدن هر دکمه‌ای یعنی کاربر دیگه تو حالت «منتظر متن بعدی» نیست؛ پرچم‌های waiting_* پاک می‌شن"""
    for key in list(context.user_data.keys()):
        if key.startswith("waiting_"):
            context.user_data.pop(key, None)


async def track_nav_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    _clear_waiting_states(context)

    parts = data.split(":")
    chat_id = None
    if len(parts) >= 2:
        try:
            chat_id = int(parts[1])
        except ValueError:
            chat_id = None

    if any(data.startswith(p) for p in LEVEL0_PREFIXES):
        context.user_data["nav_level"] = 0
        context.user_data["nav_chat_id"] = None
    elif any(data.startswith(p) for p in LEVEL1_PREFIXES):
        context.user_data["nav_level"] = 1
        context.user_data["nav_chat_id"] = None
    elif any(data.startswith(p) for p in LEVEL2_PREFIXES):
        context.user_data["nav_level"] = 2
        context.user_data["nav_chat_id"] = chat_id
    elif any(data.startswith(p) for p in LEVEL3_PREFIXES):
        context.user_data["nav_level"] = 3
        if not any(data.startswith(p) for p in LEVEL3_NO_CHATID_PREFIXES):
            context.user_data["nav_chat_id"] = chat_id


async def handle_back_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_waiting_states(context)
    await send_start_panel(update, context)
    context.user_data["nav_level"] = 0
    context.user_data["nav_chat_id"] = None


# ============================================================================
# پنل سازنده / CREATOR PANEL
# ============================================================================
async def open_creator_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    lang = lang_of(update)
    user = update.effective_user
    if user.id != CREATOR_ID:
        msg = L(lang, "⛔️ این پنل فقط برای سازنده ربات است.", "⛔️ This panel is only for the bot creator.")
        if query:
            await query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return

    context.user_data["waiting_for_shutdown_text"] = False
    context.user_data["waiting_for_update_msg"] = False

    is_active = is_global_active()
    shutdown_msg = shutdown_text(lang)
    update_msg = update_message_text(lang)
    cut = lambda s: escape(s[:40]) + ("..." if len(s) > 40 else "")
    state = L(lang, "🟢 فعال" if is_active else "🔴 خاموش", "🟢 Active" if is_active else "🔴 Off")
    text = L(lang,
             f"👑 <b>پنل ویژه سازنده</b>\n\nوضعیت ربات: {state}\nپیام خاموشی: {cut(shutdown_msg)}\n"
             f"پیام آپدیت: {cut(update_msg)}\n\nاز دکمه‌های زیر برای مدیریت استفاده کنید:",
             f"👑 <b>Creator panel</b>\n\nBot status: {state}\nShutdown message: {cut(shutdown_msg)}\n"
             f"Update message: {cut(update_msg)}\n\nUse the buttons below to manage:")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "🔴 خاموش", "🔴 Turn off") if is_active else L(lang, "🟢 روشن", "🟢 Turn on"),
                              callback_data="creator_global_off" if is_active else "creator_global_on"),
         InlineKeyboardButton(L(lang, "📝 خاموشی", "📝 Shutdown msg"), callback_data="creator_set_msg")],
        [InlineKeyboardButton(L(lang, "📝 آپدیت", "📝 Update msg"), callback_data="creator_set_update_msg"),
         InlineKeyboardButton(L(lang, "📋 نمایش آپدیت", "📋 Show update msg"), callback_data="creator_show_update_msg")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data="start_menu")],
    ])
    if query:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def toggle_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    lang = lang_of(update)
    if update.effective_user.id != CREATOR_ID:
        await query.answer(L(lang, "⛔️ فقط سازنده", "⛔️ Creator only"), show_alert=True)
        return
    set_global_active(query.data == "creator_global_on")
    await query.answer(L(lang, "✔ ذخیره شد", "✔ Saved"))
    await open_creator_panel(update, context)


async def ask_set_shutdown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    if update.effective_user.id != CREATOR_ID:
        await query.answer(L(lang, "⛔️ فقط سازنده", "⛔️ Creator only"), show_alert=True)
        return
    context.user_data["waiting_for_update_msg"] = False
    await query.edit_message_text(
        L(lang, "📝 <b>تغییر پیام خاموشی</b>\n\nلطفاً متن جدید پیام خاموشی را ارسال کنید.\n"
                "این پیام زمانی که ربات خاموش است به کاربران نمایش داده می‌شود.\n\nبرای لغو، دستور /cancel را بفرستید.",
          "📝 <b>Change shutdown message</b>\n\nPlease send the new shutdown message.\n"
          "It is shown to users while the bot is off.\n\nTo cancel, send /cancel."),
        reply_markup=_back_kb(lang, "creator_panel_open"), parse_mode="HTML")
    context.user_data["waiting_for_shutdown_text"] = True


async def receive_shutdown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_shutdown_text"):
        return False
    if update.effective_user.id != CREATOR_ID:
        return False
    lang = lang_of(update)
    text = update.effective_message.text
    context.user_data["waiting_for_shutdown_text"] = False
    if text == "/cancel":
        await update.effective_message.reply_text(L(lang, "❌ لغو شد.", "❌ Cancelled."))
        return True
    set_shutdown_message(text)
    await update.effective_message.reply_text(L(lang, "✔ پیام خاموشی با موفقیت تغییر کرد!", "✔ Shutdown message changed!"))
    await open_creator_panel(update, context)
    return True


async def ask_set_update_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    if update.effective_user.id != CREATOR_ID:
        await query.answer(L(lang, "⛔️ فقط سازنده", "⛔️ Creator only"), show_alert=True)
        return
    context.user_data["waiting_for_shutdown_text"] = False
    await query.edit_message_text(
        L(lang, "📝 <b>تغییر پیام آپدیت</b>\n\nلطفاً متن جدید پیام آپدیت را ارسال کنید.\n"
                "این پیام زمانی که ربات آپدیت می‌شود به مدیران گروه نمایش داده می‌شود.\n\nبرای لغو، دستور /cancel را بفرستید.",
          "📝 <b>Change update message</b>\n\nPlease send the new update message.\n"
          "It is shown to group admins when the bot is updated.\n\nTo cancel, send /cancel."),
        reply_markup=_back_kb(lang, "creator_panel_open"), parse_mode="HTML")
    context.user_data["waiting_for_update_msg"] = True


async def receive_update_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_update_msg"):
        return False
    if update.effective_user.id != CREATOR_ID:
        return False
    lang = lang_of(update)
    text = update.effective_message.text
    context.user_data["waiting_for_update_msg"] = False
    if text == "/cancel":
        await update.effective_message.reply_text(L(lang, "❌ لغو شد.", "❌ Cancelled."))
        return True
    set_setting("update_message", text)
    await update.effective_message.reply_text(L(lang, "✔ پیام آپدیت با موفقیت تغییر کرد!", "✔ Update message changed!"))
    await open_creator_panel(update, context)
    return True


async def show_update_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    if update.effective_user.id != CREATOR_ID:
        await query.answer(L(lang, "⛔️ فقط سازنده", "⛔️ Creator only"), show_alert=True)
        return
    msg = escape(update_message_text(lang))
    await query.edit_message_text(L(lang, f"📋 <b>پیام آپدیت فعلی:</b>\n\n{msg}", f"📋 <b>Current update message:</b>\n\n{msg}"),
                                  reply_markup=_back_kb(lang, "creator_panel_open"), parse_mode="HTML")


# ============================================================================
# /start و منوی اصلی / START
# ============================================================================
def start_text(lang, name=None):
    name = name or L(lang, "دوست", "friend")
    return L(lang,
             f"🤖 سلام {name} عزیز!\n\nبا بهترین ربات مدیریت گروه آشنا شوید\nدستیار قدرتمند برای نظم و امنیت گروه‌ها\n\n"
             "✨ ویژگی‌های کلیدی\n✔ پاسخ سریع  •  ✔ گروه‌های بزرگ\n✔ امنیت بالا  •  ✔ ضداسپم\n"
             "✔ فیلتر کلمات  •  ✔ کنترل دسترسی\n✔ قفل حرفه‌ای  •  ✔ پشتیبانی سریع\n\n"
             "🚀 نحوه نصب\n1️⃣ ربات رو به گروهتون اضافه کنید\n2️⃣ اون رو ادمین کامل کنید تا فعال بشه\n\n"
             "همین حالا ربات را به گروه خود اضافه کنید! 🎯",
             f"🤖 Hello {name}!\n\nMeet the best group management bot\nA powerful assistant for keeping your groups organized and safe\n\n"
             "✨ Key features\n✔ Fast replies  •  ✔ Large groups\n✔ High security  •  ✔ Anti-spam\n"
             "✔ Word filter  •  ✔ Access control\n✔ Pro locking  •  ✔ Quick support\n\n"
             "🚀 How to install\n1️⃣ Add the bot to your group\n2️⃣ Make it a full admin so it activates\n\n"
             "Add the bot to your group now! 🎯")


def start_welcome_text(lang):
    return L(lang,
             "🎉 **به ربات مدیریت گروه خوش آمدید!**\n\nمن اینجا هستم تا به شما در مدیریت گروه‌های تلگرام کمک کنم.\n\n"
             "✅ با استفاده از دکمه‌های زیر می‌توانید:\n• گروه‌های خود را مدیریت کنید\n• از هوش مصنوعی کمک بگیرید\n"
             "• با پشتیبانی تماس بگیرید\n\n🌟 **نکته:** ربات را در گروه خود ادمین کامل کنید تا همه قابلیت‌ها فعال شوند.",
             "🎉 **Welcome to the group management bot!**\n\nI'm here to help you manage your Telegram groups.\n\n"
             "✅ With the buttons below you can:\n• Manage your groups\n• Get help from AI\n• Contact support\n\n"
             "🌟 **Tip:** Make the bot a full admin in your group so all features work.")


def help_text(lang):
    return L(lang,
             "🤖 راهنمای ربات\n\n🔧 مدیریت گروه\nخاموشی، روشن، سکوت، آزاد کن، بن کن، اخطار، پاک، گیف بن، استیکر بن\n\n"
             "🎮 سرگرمی\nتاس، شیر یا خط، سنگ کاغذ قیچی، دوز\n\n🌐 ابزار\nترجمه، تاریخ، رمز ارز، گزارش، تگ، آمار\n\n"
             "🧠 هوش مصنوعی\nپاسخ به سوالات، فیلتر فحش، ترجمه، محاسبات ساده\n\n"
             "هر گروه می‌تونه کلمه‌ی هرکدوم از این دستورا رو از پنل مدیریت عوض کنه؛ برای دیدن کلمه‌های فعلیِ همین گروه، "
             "تو خودِ گروه بنویس «راهنما».\n\nدستورات انگلیسی (mute, ban, warn, ...) هم کار می‌کنن.",
             "🤖 Bot help\n\n🔧 Group management\nlock, unlock, mute, unmute, ban, warn, del, gifban, stickerban\n\n"
             "🎮 Fun\ndice, coin, rps, xo\n\n🌐 Tools\ntranslate, date, report, tag, stats\n\n"
             "🧠 AI\nAnswering questions, profanity filter, translation, simple calculations\n\n"
             "Each group can rename any of these commands from the admin panel; to see this group's current words, "
             "type \"help\" inside the group.\n\nThe Persian commands work too.")


def build_start_keyboard(user_id: int, bot_username: str, lang=None):
    lang = lang or get_lang(user_id)
    B = lambda fa, en, cb: InlineKeyboardButton(L(lang, fa, en), callback_data=cb)
    rows = [
        [InlineKeyboardButton(
            L(lang, "➕ افزودن به گروه", "➕ Add to group"),
            url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages+restrict_members+invite_users+pin_messages")],
        [B("⚙️ پنل مدیریت", "⚙️ Admin panel", "panel_my_groups"), B("📘 راهنما", "📘 Help", "help_commands")],
    ]
    if user_id == CREATOR_ID:
        rows.append([B("👑 پنل ویژه سازنده", "👑 Creator panel", "creator_panel_open")])
    rows.append([B("🧠 هوش مصنوعی", "🧠 AI", "ai_model_select"), B("📩 پشتیبانی", "📩 Support", "support_menu")])
    rows.append([B("📥 دانلودر", "📥 Downloader", "dl_panel_open"), B("🌐 زبان", "🌐 Language", f"lang_open:{user_id}")])
    if user_id == CREATOR_ID:  # ری‌استارت فقط برای سازنده
        rows.append([B("🔄 ری‌استارت ربات", "🔄 Restart bot", "restart_bot")])
    return InlineKeyboardMarkup(rows)


async def _delete_old_panel(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    old_msg_id = get_setting(f"panel_msg_{chat_id}")
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id, int(old_msg_id))
        except Exception:
            pass


def _remember_panel(chat_id: int, message_id: int):
    set_setting(f"panel_msg_{chat_id}", str(message_id))


async def send_start_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    lang = get_lang(chat.id)
    bot_username = (await context.bot.get_me()).username
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    await _delete_old_panel(context, chat.id)

    if get_setting(f"first_start_{user.id}", "true") == "true":
        set_setting(f"first_start_{user.id}", "false")
        sent = await update.effective_message.reply_text(
            start_welcome_text(lang), reply_markup=build_start_keyboard(user.id, bot_username, lang),
            parse_mode="Markdown")
    else:
        sent = await update.effective_message.reply_text(
            start_text(lang, user.first_name), reply_markup=build_start_keyboard(user.id, bot_username, lang))
    _remember_panel(chat.id, sent.message_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != "private":
        return
    lang = get_lang(chat.id)
    if not is_global_active() and user.id != CREATOR_ID:
        await update.effective_message.reply_text(shutdown_text(lang))
        return

    temp_msg = await update.effective_message.reply_text("🤖", reply_markup=ReplyKeyboardRemove())
    try:
        await temp_msg.delete()
    except Exception:
        pass

    # کاربر جدیدی که هنوز زبان انتخاب نکرده: اول انتخاب زبان
    if get_setting(f"first_start_{user.id}", "true") == "true" and not has_lang(chat.id):
        await send_language_chooser(update, context)
        return
    await send_start_panel(update, context)


async def on_help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    await query.edit_message_text(help_text(lang), reply_markup=_back_kb(lang, "start_menu"))


async def on_start_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["downloader_mode"] = False  # پنل اصلی نباید لینکی رو پردازش کنه
    lang = lang_of(update)
    bot_username = (await context.bot.get_me()).username
    await query.edit_message_text(
        start_text(lang, update.effective_user.first_name),
        reply_markup=build_start_keyboard(update.effective_user.id, bot_username, lang))


async def ai_model_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Gemini (Google)", callback_data="ai_use_gemini")],
        [InlineKeyboardButton("🤖 ChatGPT (OpenAI)", callback_data="ai_use_chatgpt")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data="start_menu")],
    ])
    await query.edit_message_text(
        L(lang, "🧠 **انتخاب هوش مصنوعی**\n\nلطفاً مدل مورد نظر را انتخاب کنید:\n\n"
                "• **Gemini** — رایگان، مناسب برای استفاده روزمره\n• **ChatGPT** — قدرتمندتر، نیاز به کلید API دارد",
          "🧠 **Choose an AI**\n\nPlease choose the model you want:\n\n"
          "• **Gemini** — free, good for everyday use\n• **ChatGPT** — more powerful, needs an API key"),
        reply_markup=kb, parse_mode="Markdown")


async def _ai_enabled(update, context, model, label):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    context.user_data["ai_model"] = model
    context.user_data["ai_chat_enabled"] = True
    await query.edit_message_text(
        L(lang, f"✔ **مدل {label} فعال شد**\n\nحالا می‌توانید سوال خود را بپرسید.\nبرای شروع، پیام خود را ارسال کنید.",
          f"✔ **{label} is now active**\n\nYou can ask your question now.\nJust send your message to start."),
        reply_markup=_back_kb(lang, "start_menu"), parse_mode="Markdown")


async def ai_use_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ai_enabled(update, context, "gemini", "Gemini")


async def ai_use_chatgpt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ai_enabled(update, context, "chatgpt", "ChatGPT")


async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ری‌استارت ربات (فقط سازنده)"""
    query = update.callback_query
    lang = lang_of(update)
    if update.effective_user.id != CREATOR_ID:
        await query.answer(L(lang, "⛔️ فقط سازنده ربات.", "⛔️ Bot creator only."), show_alert=True)
        return
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    temp_msg = await update.effective_message.reply_text(L(lang, "🤖 خوش آمدید!", "🤖 Welcome!"),
                                                         reply_markup=ReplyKeyboardRemove())
    try:
        await temp_msg.delete()
    except Exception:
        pass
    await send_start_panel(update, context)
    try:
        os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception as e:
        await update.effective_message.reply_text(L(lang, f"❌ خطا در ری‌استارت: {e}", f"❌ Restart failed: {e}"))


# ============================================================================
# انتخاب زبان / LANGUAGE
# ============================================================================
def _lang_markup(scope, current, ui_lang, back_cb=None):
    rows = []
    for code in SUPPORTED_LANGS:
        mark = "✅ " if code == current else ""
        rows.append([InlineKeyboardButton(f"{mark}{LANG_LABELS[code]}", callback_data=f"lang_set:{scope}:{code}")])
    if back_cb:
        rows.append([InlineKeyboardButton(L(ui_lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def _lang_text(ui_lang, scope, current):
    label = LANG_LABELS[current]
    if scope < 0:
        return L(ui_lang, f"🌐 زبان گروه\n\nزبان فعلی: {label}\n\nپیام‌های ربات تو گروه با این زبان نمایش داده می‌شن.",
                 f"🌐 Group language\n\nCurrent language: {label}\n\nThe bot's messages in this group will use this language.")
    return L(ui_lang, f"🌐 زبان ربات\n\nزبان فعلی: {label}\n\nیکی رو انتخاب کن:",
             f"🌐 Bot language\n\nCurrent language: {label}\n\nChoose one:")


async def _safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        pass  # مثلاً «message is not modified»


async def send_language_chooser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    sent = await update.effective_message.reply_text(
        "🌐 زبان / Language\n\nلطفاً زبان خودت رو انتخاب کن.\nPlease choose your language.",
        reply_markup=_lang_markup(chat.id, None, "fa"))
    _remember_panel(chat.id, sent.message_id)


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    ui = get_lang(chat.id)
    if chat.type == "private":
        back = "start_menu"
    else:
        if not await can_use_moderation_commands(context.bot, chat.id, user.id):
            await update.effective_message.reply_text(L(ui, "⛔️ فقط مدیران گروه می‌تونن زبان رو عوض کنن.",
                                                        "⛔️ Only group admins can change the language."))
            return
        back = None
    await update.effective_message.reply_text(_lang_text(ui, chat.id, ui), reply_markup=_lang_markup(chat.id, ui, ui, back))


async def lang_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    scope = int(query.data.split(":")[1])
    user = update.effective_user
    ui = get_lang(query.message.chat_id)
    allowed = (scope == user.id) if scope > 0 else await can_access_dm_panel(context.bot, scope, user.id)
    if not allowed:
        await query.answer(_deny(ui), show_alert=True)
        return
    back = "start_menu" if scope > 0 else f"grp_open:{scope}"
    current = get_lang(scope)
    await _safe_edit(query, _lang_text(ui, scope, current), _lang_markup(scope, current, ui, back))


async def lang_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, scope, code = query.data.split(":")
    scope = int(scope)
    user = update.effective_user
    ui = get_lang(query.message.chat_id)
    if code not in SUPPORTED_LANGS:
        await query.answer()
        return
    allowed = (scope == user.id) if scope > 0 else await can_use_moderation_commands(context.bot, scope, user.id)
    if not allowed:
        await query.answer(_deny(ui), show_alert=True)
        return

    set_lang(scope, code)
    in_private = query.message.chat.type == "private"
    if in_private and scope > 0:
        await query.answer(L(code, "✔ زبان به فارسی تغییر کرد", "✔ Language changed to English"))
        await send_start_panel(update, context)
    elif in_private:
        await query.answer(L(ui, "ذخیره شد ✔", "Saved ✔"))
        await _safe_edit(query, _lang_text(ui, scope, code), _lang_markup(scope, code, ui, f"grp_open:{scope}"))
    else:
        await query.answer()
        await _safe_edit(query, L(code, "✔ زبان گروه به فارسی تغییر کرد.", "✔ Group language changed to English."))


# ============================================================================
# پشتیبانی / SUPPORT
# ============================================================================
def _support_prompt_text(lang, extra_line=None):
    text = L(lang,
             "📩 <b>ارسال پیام به پشتیبانی</b>\n\nلطفاً پیام خود را بنویسید.\nمی‌توانید همراه با پیام، عکس هم ارسال کنید.\n\n"
             "⚠️ پیام شما پس از تأیید برای پشتیبانی ارسال می‌شود.\nبرای لغو، دکمه لغو را بزنید.",
             "📩 <b>Message to support</b>\n\nPlease write your message.\nYou can also send a photo with it.\n\n"
             "⚠️ Your message will be sent to support after you confirm.\nTo cancel, press the cancel button.")
    if extra_line:
        text = f"{extra_line}\n\n{text}"
    return text


def _support_cancel_kb(lang):
    return InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "✘ لغو", "✘ Cancel"), callback_data="start_menu")]])


async def support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    if update.effective_user.id == CREATOR_ID:
        await support_admin_panel(update, context)
        return
    await query.edit_message_text(_support_prompt_text(lang), reply_markup=_support_cancel_kb(lang), parse_mode="HTML")
    context.user_data["waiting_for_support"] = True
    context.user_data["support_photo"] = None
    context.user_data["support_text"] = None
    context.user_data["support_prompt_chat_id"] = query.message.chat_id
    context.user_data["support_prompt_message_id"] = query.message.message_id


async def _edit_support_prompt(context, text, kb, parse_mode="HTML"):
    prompt_chat_id = context.user_data.get("support_prompt_chat_id")
    prompt_message_id = context.user_data.get("support_prompt_message_id")
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id,
                                                text=text, reply_markup=kb, parse_mode=parse_mode)
            return
        except Exception:
            pass
    sent = await context.bot.send_message(prompt_chat_id, text, reply_markup=kb, parse_mode=parse_mode)
    context.user_data["support_prompt_chat_id"] = sent.chat_id
    context.user_data["support_prompt_message_id"] = sent.message_id


async def receive_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_support"):
        return False
    message = update.effective_message
    lang = lang_of(update)

    if message.photo:
        context.user_data["support_photo"] = message.photo[-1].file_id
        try:
            await message.delete()
        except Exception:
            pass
        await _edit_support_prompt(
            context, _support_prompt_text(lang, L(lang, "✔ عکس دریافت شد. حالا متن خود را بنویسید.",
                                                  "✔ Photo received. Now write your text.")),
            _support_cancel_kb(lang))
        return True

    if message.text:
        context.user_data["support_text"] = message.text
        try:
            await message.delete()
        except Exception:
            pass
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(L(lang, "✔ بله", "✔ Yes"), callback_data=f"support_confirm:{message.message_id}"),
            InlineKeyboardButton(L(lang, "✘ خیر", "✘ No"), callback_data="support_cancel"),
        ]])
        await _edit_support_prompt(
            context, L(lang, "📩 <b>تأیید ارسال</b>\n\nآیا می‌خواهید این پیام را برای پشتیبانی ارسال کنید؟",
                       "📩 <b>Confirm sending</b>\n\nDo you want to send this message to support?"), kb)
        return True
    return False


def _clear_support_state(context):
    context.user_data["waiting_for_support"] = False
    context.user_data["support_photo"] = None
    context.user_data["support_text"] = None
    context.user_data.pop("support_prompt_chat_id", None)
    context.user_data.pop("support_prompt_message_id", None)


async def confirm_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    lang = lang_of(update)
    photo = context.user_data.get("support_photo")
    text = context.user_data.get("support_text", "")
    if not text and not photo:
        await query.edit_message_text(L(lang, "❌ پیامی برای ارسال وجود ندارد.", "❌ There is no message to send."))
        return

    data = {"user_id": user.id, "username": f"@{user.username}" if user.username else user.full_name,
            "text": text, "photo": photo, "timestamp": time.time(), "status": "pending"}
    set_setting(f"support_msg_{int(time.time())}_{user.id}", json.dumps(data))

    clang = get_lang(CREATOR_ID)  # پیام برای سازنده‌ست، با زبان خودش
    admin_text = L(clang,
                   f"📩 <b>پیام جدید از پشتیبانی</b>\n\n👤 فرستنده: {escape(data['username'])}\n🆔 آیدی: {data['user_id']}\n"
                   f"🕐 زمان: {time.strftime('%Y-%m-%d %H:%M')}\n\n📝 متن:\n{escape(data['text'] or '')}",
                   f"📩 <b>New support message</b>\n\n👤 Sender: {escape(data['username'])}\n🆔 ID: {data['user_id']}\n"
                   f"🕐 Time: {time.strftime('%Y-%m-%d %H:%M')}\n\n📝 Text:\n{escape(data['text'] or '')}")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(L(clang, "📩 پاسخ", "📩 Reply"), callback_data=f"support_reply:{data['user_id']}:{data['timestamp']}"),
        InlineKeyboardButton(L(clang, "🗑 حذف", "🗑 Delete"), callback_data=f"support_delete:{data['user_id']}:{data['timestamp']}"),
    ]])
    try:
        if photo:
            await context.bot.send_photo(CREATOR_ID, photo=photo, caption=admin_text[:1024], reply_markup=kb, parse_mode="HTML")
        else:
            await context.bot.send_message(CREATOR_ID, admin_text, reply_markup=kb, parse_mode="HTML")
        await query.edit_message_text(
            L(lang, "✔ پیام شما با موفقیت به پشتیبانی ارسال شد.\n\nبه زودی پاسخ داده خواهد شد.",
              "✔ Your message was sent to support.\n\nYou will get a reply soon."),
            reply_markup=_back_kb(lang, "start_menu"))
    except Exception as e:
        await query.edit_message_text(L(lang, f"✘ خطا در ارسال: {e}", f"✘ Sending failed: {e}"))
        return
    _clear_support_state(context)


async def cancel_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _clear_support_state(context)
    await send_start_panel(update, context)


def _load_support_messages():
    messages = []
    for key in get_all_keys():
        if key.startswith("support_msg_"):
            data = get_setting(key, "")
            if data:
                try:
                    messages.append(json.loads(data))
                except Exception:
                    pass
    return messages


async def support_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    lang = lang_of(update)
    if update.effective_user.id != CREATOR_ID:
        await query.edit_message_text(L(lang, "⛔️ این بخش فقط برای سازنده است.", "⛔️ This section is for the creator only."))
        return

    messages = _load_support_messages()
    if not messages:
        text = L(lang, "📩 <b>پنل پشتیبانی</b>\n\nهیچ پیامی دریافت نشده است.",
                 "📩 <b>Support panel</b>\n\nNo messages received.")
        kb = _back_kb(lang, "start_menu")
        if query:
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return

    shown = messages[:20]
    rows = []
    for i in range(0, len(shown), 2):
        row = []
        for j in range(2):
            if i + j < len(shown):
                msg = shown[i + j]
                username = msg.get("username", L(lang, "ناشناس", "Unknown"))
                ts = time.strftime("%H:%M", time.localtime(msg.get("timestamp", 0)))
                row.append(InlineKeyboardButton(f"{i + j + 1}. {username} - {ts}", callback_data=f"support_show:{i + j}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(L(lang, "🔄 بروزرسانی", "🔄 Refresh"), callback_data="support_admin")])
    rows.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data="start_menu")])

    text = L(lang, f"📩 <b>پنل پشتیبانی</b>\n\n{len(messages)} پیام دریافت شده.\n\nروی هرکدام کلیک کنید تا متن را ببینید.",
             f"📩 <b>Support panel</b>\n\n{len(messages)} message(s) received.\n\nTap one to read it.")
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
    context.user_data["support_messages"] = messages


async def show_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    index = int(query.data.split(":")[1])
    messages = context.user_data.get("support_messages", [])
    if not messages or index >= len(messages):
        await query.edit_message_text(L(lang, "✘ پیام یافت نشد.", "✘ Message not found."))
        return
    msg = messages[index]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.get("timestamp", 0)))
    uname = escape(str(msg.get("username", L(lang, "ناشناس", "Unknown"))))
    body = escape(msg.get("text") or L(lang, "بدون متن", "no text"))
    text = L(lang,
             f"📩 <b>پیام پشتیبانی</b>\n\n👤 فرستنده: {uname}\n🆔 آیدی: {msg.get('user_id', 'نامشخص')}\n🕐 زمان: {when}\n\n📝 متن:\n{body}",
             f"📩 <b>Support message</b>\n\n👤 Sender: {uname}\n🆔 ID: {msg.get('user_id', 'unknown')}\n🕐 Time: {when}\n\n📝 Text:\n{body}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "📩 پاسخ", "📩 Reply"), callback_data=f"support_reply:{msg['user_id']}:{msg['timestamp']}"),
         InlineKeyboardButton(L(lang, "🗑 حذف", "🗑 Delete"), callback_data=f"support_delete:{msg['user_id']}:{msg['timestamp']}")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data="support_admin")],
    ])
    # پیام لیست متنیه؛ اگه عکس داشت، عکس جدا (با همین کپشن) فرستاده می‌شه
    if msg.get("photo"):
        try:
            await context.bot.send_photo(query.message.chat_id, msg["photo"], caption=text[:1024],
                                         reply_markup=kb, parse_mode="HTML")
            await query.message.delete()
            return
        except Exception:
            pass
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    _, user_id, timestamp = query.data.split(":")
    text = L(lang, "📩 <b>پاسخ به پیام</b>\n\nلطفاً متن پاسخ خود را بنویسید.\nپاسخ شما به کاربر ارسال خواهد شد.\n\nبرای لغو، /cancel را بفرستید.",
             "📩 <b>Reply to message</b>\n\nPlease write your reply.\nIt will be sent to the user.\n\nTo cancel, send /cancel.")
    if query.message.photo:
        await query.edit_message_caption(caption=text, parse_mode="HTML")
    else:
        await query.edit_message_text(text, parse_mode="HTML")
    context.user_data["waiting_support_reply_to"] = int(user_id)
    context.user_data["support_reply_timestamp"] = timestamp
    context.user_data["support_reply_prompt_chat_id"] = query.message.chat_id
    context.user_data["support_reply_prompt_message_id"] = query.message.message_id


async def send_support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_support_reply_to"):
        return False
    user_id = context.user_data["waiting_support_reply_to"]
    reply_text = update.effective_message.text
    lang = lang_of(update)
    prompt_chat_id = context.user_data.pop("support_reply_prompt_chat_id", None)
    prompt_message_id = context.user_data.pop("support_reply_prompt_message_id", None)
    context.user_data.pop("support_reply_timestamp", None)
    context.user_data["waiting_support_reply_to"] = None
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    if reply_text == "/cancel":
        result_text = L(lang, "✘ پاسخ لغو شد.", "✘ Reply cancelled.")
    else:
        ulang = get_lang(user_id)  # پاسخ با زبان خودِ کاربر
        try:
            await context.bot.send_message(
                user_id,
                L(ulang, f"📩 <b>پاسخ پشتیبانی</b>\n\n{escape(reply_text)}\n\n💡 برای ارسال پیام جدید، دکمه پشتیبانی را بزنید.",
                  f"📩 <b>Support reply</b>\n\n{escape(reply_text)}\n\n💡 To send a new message, press the Support button."),
                parse_mode="HTML")
            result_text = L(lang, "✔ پاسخ با موفقیت ارسال شد.", "✔ Reply sent.")
        except Exception as e:
            result_text = L(lang, f"✘ خطا در ارسال: {e}", f"✘ Sending failed: {e}")

    kb = _back_kb(lang, "support_admin")
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id,
                                                text=result_text, reply_markup=kb)
            return True
        except Exception:
            pass
    await context.bot.send_message(update.effective_chat.id, result_text, reply_markup=kb)
    return True


async def support_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    _, user_id, timestamp = query.data.split(":")
    user_id = int(user_id)
    for key in get_all_keys():
        if key.startswith("support_msg_"):
            data = get_setting(key, "")
            if data:
                try:
                    msg = json.loads(data)
                    if msg.get("user_id") == user_id and msg.get("timestamp") == float(timestamp):
                        set_setting(key, "")
                        text = L(lang, "🗑 پیام با موفقیت حذف شد.", "🗑 Message deleted.")
                        if query.message.photo:
                            await query.edit_message_caption(caption=text)
                        else:
                            await query.edit_message_text(text)
                        return
                except Exception:
                    pass
    await query.edit_message_text(L(lang, "✘ پیام یافت نشد.", "✘ Message not found."))


# ============================================================================
# قیمت دلار / طلا / تتر (عکس) / PRICES
# دلار و طلا از tgju و تتر از CoinGecko. درخواست‌ها synchronous ان، پس همه‌جا با
# asyncio.to_thread صدا زده می‌شن تا event loop ربات قفل نشه.
# ============================================================================
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
TGJU_URL = "https://call4.tgju.org/ajax.json"
USER_AGENT = "Mozilla/5.0 (compatible; TelegramBot/1.0; +https://core.telegram.org/bots)"

FIAT_GOLD_MAP = {
    "دلار": ("price_dollar_rl", "💵", "dollar"),
    "طلا": ("geram18", "🥇", "gold"),
}
SYMBOL_MAP = {"تتر": ("usdt", "💵")}
COINGECKO_IDS = {"usdt": "tether"}
# معادل انگلیسی اسم‌ها (با حروف کوچک)
PRICE_ALIASES = {"dollar": "دلار", "gold": "طلا", "tether": "تتر", "usdt": "تتر"}
PRICE_LOOKUP_NAMES = set(SYMBOL_MAP.keys()) | set(FIAT_GOLD_MAP.keys()) | set(PRICE_ALIASES.keys())

NAME_TRANSLATIONS = {
    "usdt": {"fa": "تتر", "en": "Tether", "ar": "تيثر"},
    "dollar": {"fa": "دلار", "en": "US Dollar", "ar": "الدولار الأمريكي"},
    "gold": {"fa": "طلا (۱۸ عیار)", "en": "Gold (18k)", "ar": "الذهب (18 قيراط)"},
}
UI_STRINGS = {
    "fa": {"greeting": "سلام جوان ایرانی", "price_label": "قیمت لحظه‌ای", "currency": "تومان",
           "change_suffix": "تغییر نسبت به دیروز", "updated": "بروزرسانی", "unknown": "نامشخص"},
    "en": {"greeting": "Hello Iranian Youth", "price_label": "Live Price", "currency": "Toman",
           "change_suffix": "change vs yesterday", "updated": "Updated", "unknown": "N/A"},
    "ar": {"greeting": "مرحباً أيها الشاب الإيراني", "price_label": "السعر اللحظي", "currency": "تومان",
           "change_suffix": "التغيير مقارنة بالأمس", "updated": "آخر تحديث", "unknown": "غير معروف"},
}
LANG_NAMES = {"fa": "فارسی", "en": "English", "ar": "العربية"}
AUTO_DELETE_DELAY = 60  # ثانیه؛ فقط تو پی‌وی


def _tr_name(symbol, lang):
    entry = NAME_TRANSLATIONS.get(symbol)
    if not entry:
        return symbol
    return entry.get(lang, entry.get("fa", symbol))


def _ui(lang, key):
    return UI_STRINGS.get(lang, UI_STRINGS["fa"]).get(key, UI_STRINGS["fa"][key])


FONT_PATH = os.path.join(BASE_DIR, "assets", "Vazirmatn-Bold.ttf")
FALLBACK_FONT_PATH = os.path.join(BASE_DIR, "assets", "DejaVuSans-Bold.ttf")
BG_IMAGE_PATH = os.path.join(BASE_DIR, "assets", "price_card_bg.jpg")
BG_VIDEO_PATH = os.path.join(BASE_DIR, "assets", "price_card_bg.mp4")
_cached_video_file_id = None


def _get_font(size):
    if os.path.exists(FONT_PATH):
        return ImageFont.truetype(FONT_PATH, size)
    if os.path.exists(FALLBACK_FONT_PATH):
        return ImageFont.truetype(FALLBACK_FONT_PATH, size)
    return ImageFont.load_default()


def _fa(text: str) -> str:
    """متن فارسی/عربی رو برای نمایش درست روی عکس آماده می‌کنه (لاتین بدون تغییر می‌مونه)"""
    return get_display(arabic_reshaper.reshape(text))


def fetch_coingecko_data():
    params = {"ids": ",".join(sorted(set(COINGECKO_IDS.values()))), "vs_currencies": "usd",
              "include_24hr_change": "true"}
    resp = requests.get(COINGECKO_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_price_toman(cg_data: dict, symbol: str, usd_to_toman):
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return None, None
    entry = cg_data.get(cg_id)
    if not entry:
        return None, None
    usd_price = entry.get("usd")
    change = entry.get("usd_24h_change")
    if usd_price is None or usd_to_toman is None:
        return None, change
    try:
        return int(float(usd_price) * float(usd_to_toman)), change
    except (TypeError, ValueError):
        return None, change


def fetch_tgju_data():
    resp = requests.get(TGJU_URL, timeout=10)
    resp.raise_for_status()
    return resp.json().get("current", {})


def get_fiat_gold_price(tgju_data: dict, key: str):
    entry = tgju_data.get(key)
    if not entry:
        return None, None
    try:
        price_toman = int(float(str(entry.get("p", "")).replace(",", "")) / 10)
    except (TypeError, ValueError):
        return None, None
    change = entry.get("dp") or entry.get("d")
    try:
        change = float(str(change).replace("%", "")) if change is not None else None
    except (TypeError, ValueError):
        change = None
    return price_toman, change


def get_usd_to_toman_rate(tgju_data: dict):
    rate, _ = get_fiat_gold_price(tgju_data, "price_dollar_rl")
    return rate


def _card_color(day_change):
    try:
        if day_change is not None and float(day_change) < 0:
            return (255, 90, 90)
    except (TypeError, ValueError):
        pass
    return (90, 200, 130)


COIN_COLORS = {"usdt": (38, 161, 123), "dollar": (90, 160, 230), "gold": (222, 180, 90)}


def _coin_color(symbol):
    return COIN_COLORS.get(symbol, (147, 51, 234))


def _vertical_gradient(width, height, top_color, bottom_color):
    img = Image.new("RGB", (width, height), top_color)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t_ = y / height
        draw.line([(0, y), (width, y)], fill=tuple(int(top_color[i] + (bottom_color[i] - top_color[i]) * t_) for i in range(3)))
    return img


def _load_background(width, height):
    if not os.path.exists(BG_IMAGE_PATH):
        return _vertical_gradient(width, height, (18, 10, 34), (6, 4, 14))
    bg = Image.open(BG_IMAGE_PATH).convert("RGB")
    src_w, src_h = bg.size
    target_ratio = width / height
    if src_w / src_h > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        bg = bg.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 3
        bg = bg.crop((0, top, src_w, top + new_h))
    bg = bg.resize((width, height), Image.LANCZOS)
    overlay = Image.new("RGBA", (width, height), (8, 4, 18, 110))
    return Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")


def _glass_panel(img, x0, y0, x1, y1, radius=32, blur=14, white_mix=0.06):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    region = img.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur))
    region = Image.blend(region, Image.new("RGB", region.size, (255, 255, 255)), white_mix)
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, region.size[0] - 1, region.size[1] - 1], radius=radius, fill=255)
    img = img.copy()
    img.paste(region, (x0, y0), mask)
    return img


def render_single_card(symbol: str, price, change, lang: str = "fa", extra_info=None) -> Image.Image:
    width, height = 1200, 675
    gold = (235, 180, 90)
    name = _tr_name(symbol, lang)
    img = _load_background(width, height)
    panel_margin = 70
    img = _glass_panel(img, panel_margin, panel_margin, width - panel_margin, height - panel_margin)
    draw = ImageDraw.Draw(img)

    greeting_font, name_font = _get_font(24), _get_font(54)
    price_label_font, price_font = _get_font(24), _get_font(72)
    change_font, footer_font, detail_font = _get_font(30), _get_font(20), _get_font(22)

    def centered(text, y, font, fill):
        w = draw.textlength(text, font=font)
        draw.text(((width - w) / 2, y), text, font=font, fill=fill)

    centered(_fa(_ui(lang, "greeting")), 118, greeting_font, (235, 225, 210))
    centered(_fa(name), 168, name_font, (255, 255, 255))
    centered(_fa(_ui(lang, "price_label")), 272, price_label_font, (225, 210, 190))
    price_text = _fa(f"{price:,} {_ui(lang, 'currency')}") if price is not None else _fa(_ui(lang, "unknown"))
    centered(price_text, 306, price_font, gold)

    y_cursor = 400
    if change is not None:
        try:
            change_val = float(change)
            sign = "+" if change_val >= 0 else ""
            centered(_fa(f"{sign}{change_val:.2f}٪ {_ui(lang, 'change_suffix')}"), y_cursor, change_font, _card_color(change))
            y_cursor += 48
        except (TypeError, ValueError):
            pass
    if extra_info:
        for line in extra_info:
            centered(_fa(line), y_cursor, detail_font, (210, 205, 220))
            y_cursor += 34

    footer = _fa(f"{_ui(lang, 'updated')}: {format_datetime(lang='en' if lang == 'en' else 'fa')}")
    centered(footer, height - panel_margin - 44, footer_font, (220, 205, 180))
    return img


def _image_to_bytes(img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "price.png"
    return buf


def _build_caption(symbol, price, change, lang="fa"):
    name = _tr_name(symbol, lang)
    price_str = _ui(lang, "unknown") if price is None else f"{price:,} {_ui(lang, 'currency')}"
    if lang == "en":
        text = f"{name} today: {price_str}"
    elif lang == "ar":
        text = f"{name} اليوم: {price_str}"
    else:
        text = f"{name} امروز {price_str}"
    if change is not None:
        try:
            change_val = float(change)
            text += f" ({'+' if change_val >= 0 else ''}{change_val:.2f}٪)"
        except (TypeError, ValueError):
            pass
    return text


async def _auto_delete_price_message(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    for message_id in job.data:
        try:
            await context.bot.delete_message(chat_id=job.chat_id, message_id=message_id)
        except Exception:
            pass


def _schedule_auto_delete(context, chat, message_ids, delay: int = AUTO_DELETE_DELAY):
    if not chat or chat.type != "private" or not context.job_queue:
        return
    context.job_queue.run_once(_auto_delete_price_message, delay, chat_id=chat.id, data=list(message_ids))


async def _send_price_result(update, context, chat, symbol, price, change, lang, caption):
    """اگه assets/price_card_bg.mp4 باشه همونو می‌فرسته (با کش file_id)، وگرنه عکس می‌سازه"""
    global _cached_video_file_id
    message = update.effective_message
    sent = None
    if os.path.exists(BG_VIDEO_PATH):
        try:
            if _cached_video_file_id:
                sent = await message.reply_video(video=_cached_video_file_id, caption=caption)
            else:
                with open(BG_VIDEO_PATH, "rb") as f:
                    sent = await message.reply_video(video=f, caption=caption)
                if sent and sent.video:
                    _cached_video_file_id = sent.video.file_id
        except Exception as e:
            logger.warning(f"video send failed, falling back to image: {e}")
            sent = None
    if sent is None:
        img = await asyncio.to_thread(render_single_card, symbol, price, change, lang)
        sent = await message.reply_photo(photo=_image_to_bytes(img), caption=caption)
    _schedule_auto_delete(context, chat, [sent.message_id, message.message_id])


def _price_image_lang(chat):
    """زبان عکس: تنظیم «زبان عکس قیمت‌ها»ی گروه؛ اگه تنظیم نشده بود، زبان ربات"""
    if not chat:
        return "fa"
    if chat.type in GROUP_TYPES:
        value = get_setting(f"img_lang_{chat.id}")
        if value in LANG_NAMES:
            return value
    return get_lang(chat.id)


async def cmd_crypto_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    raw = (message.text or "").strip()
    text = PRICE_ALIASES.get(raw.lower(), raw)
    chat = update.effective_chat
    lang = _price_image_lang(chat)
    ulang = lang_of(update)

    if text in FIAT_GOLD_MAP:
        key, emoji, trans_key = FIAT_GOLD_MAP[text]
        try:
            tgju_data = await asyncio.to_thread(fetch_tgju_data)
        except Exception as e:
            await message.reply_text(L(ulang, f"❌ نتونستم قیمت {text} رو بگیرم.\n{e}",
                                       f"❌ I couldn't get the price of {_tr_name(trans_key, 'en')}.\n{e}"))
            return
        price, change = get_fiat_gold_price(tgju_data, key)
        await _send_price_result(update, context, chat, trans_key, price, change, lang,
                                 _build_caption(trans_key, price, change, lang=lang))
        return

    match = SYMBOL_MAP.get(text)
    if not match:
        return
    symbol, emoji = match
    try:
        try:
            tgju_data = await asyncio.to_thread(fetch_tgju_data)
            usd_toman = get_usd_to_toman_rate(tgju_data)
        except Exception as e:
            await message.reply_text(L(ulang, f"❌ نتونستم نرخ دلار رو بگیرم (لازم برای تبدیل قیمت به تومان).\n{e}",
                                       f"❌ I couldn't get the dollar rate (needed to convert the price to Toman).\n{e}"))
            return
        try:
            cg_data = await asyncio.to_thread(fetch_coingecko_data)
        except Exception as e:
            await message.reply_text(L(ulang, f"❌ نتونستم به CoinGecko وصل بشم.\n{e}",
                                       f"❌ I couldn't connect to CoinGecko.\n{e}"))
            return
        price, change = get_price_toman(cg_data, symbol, usd_toman)
        await _send_price_result(update, context, chat, symbol, price, change, lang,
                                 _build_caption(symbol, price, change, lang=lang))
    except Exception as e:
        await message.reply_text(L(ulang, f"❌ خطای غیرمنتظره تو ساختن قیمت {text}.\n{e}",
                                   f"❌ Unexpected error while building the price.\n{e}"))


# ============================================================================
# آمار گروه، تبدیل ارز و نمودار / STATS, CONVERTER, CHARTS
# ============================================================================
STATS_DB_PATH = os.environ.get("STATS_DB_PATH", os.path.join(BASE_DIR, "stats.db"))
MAX_PEOPLE = 10
PRICE_SAMPLE_INTERVAL = 600
PRICE_KEEP_DAYS = 35
RATE_CACHE_SECONDS = 45

FIELDS = ("messages", "photos", "videos", "gifs", "stickers", "links")
STAT_ICONS = {"messages": "💬", "photos": "🖼", "videos": "🎬", "gifs": "🎞", "stickers": "🎭", "links": "🔗"}
STAT_LABELS = {
    "messages": ("پیام", "Messages"), "photos": ("عکس", "Photos"), "videos": ("فیلم", "Videos"),
    "gifs": ("گیف", "GIFs"), "stickers": ("استیکر", "Stickers"), "links": ("لینک", "Links"),
}


async def _auto_delete_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    for message_id in job.data:
        try:
            await context.bot.delete_message(chat_id=job.chat_id, message_id=message_id)
        except Exception:
            pass


def _schedule_delete(context, chat, message_ids, delay: int = AUTO_DELETE_DELAY):
    if not chat or chat.type != "private" or not context.job_queue:
        return
    context.job_queue.run_once(_auto_delete_job, delay, chat_id=chat.id, data=list(message_ids))


def _feature_ok(chat, key: str) -> bool:
    if not chat or chat.type not in GROUP_TYPES:
        return True
    try:
        return bool(is_feature_enabled(chat.id, key))
    except Exception:
        return True


_TO_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _digits(x, lang="fa") -> str:
    s = str(x)
    return s if lang == "en" else s.translate(_TO_FA)


def fmt_int(n, lang="fa") -> str:
    s = f"{int(round(n)):,}"
    return s if lang == "en" else s.translate(_TO_FA).replace(",", "٬")


def fmt_num(v: float, lang="fa", decimals: int = 2) -> str:
    if float(v).is_integer():
        return fmt_int(v, lang)
    s = f"{v:,.{decimals}f}"
    return s if lang == "en" else s.translate(_TO_FA).replace(",", "٬").replace(".", "٫")


def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1])
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def date_from_iso(iso_day: str, lang="fa") -> str:
    if lang == "en":
        return iso_day
    y, m, d = (int(x) for x in iso_day.split("-"))
    jy, jm, jd = gregorian_to_jalali(y, m, d)
    return _digits(f"{jy}/{jm:02d}/{jd:02d}")


def _norm(text: str) -> str:
    """یکدست‌سازی متن پیام برای تطبیق دستورها (ارقام، ی/ک عربی، نیم‌فاصله، حروف کوچک)"""
    s = text.translate(_TO_EN)
    s = s.replace("ي", "ی").replace("ك", "ک")
    s = s.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    s = re.sub(r"(?<=\d)[,،٬](?=\d)", "", s)
    s = s.replace("٫", ".")
    s = re.sub(r"[!؟?.]+$", "", s.strip())
    return re.sub(r"\s+", " ", s).strip().lower()


def _fa_mpl(text: str, lang="fa") -> str:
    """matplotlib خودش جهت راست‌به‌چپ رو می‌چرخونه؛ فقط حروف فارسی رو می‌چسبونیم"""
    return arabic_reshaper.reshape(text) if lang != "en" else text


_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_stats (
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0, photos INTEGER NOT NULL DEFAULT 0, videos INTEGER NOT NULL DEFAULT 0,
    gifs INTEGER NOT NULL DEFAULT 0, stickers INTEGER NOT NULL DEFAULT 0, links INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL, PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS price_history (symbol TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_price_history ON price_history (symbol, ts);
"""
_stats_conn = None


def _stats_db() -> sqlite3.Connection:
    global _stats_conn
    if _stats_conn is None:
        folder = os.path.dirname(STATS_DB_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)
        _stats_conn = sqlite3.connect(STATS_DB_PATH, check_same_thread=False)
        try:
            _stats_conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        _stats_conn.executescript(_STATS_SCHEMA)
        _stats_conn.commit()
    return _stats_conn


def _today_iso() -> str:
    return datetime.now(TEHRAN).strftime("%Y-%m-%d")


def add_counts(chat_id: int, user_id: int, name: str, counts: dict, day: str = None):
    day = day or _today_iso()
    conn = _stats_db()
    with conn:
        conn.execute(
            """INSERT INTO daily_stats (chat_id, user_id, day, messages, photos, videos, gifs, stickers, links)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
                 messages = messages + excluded.messages, photos = photos + excluded.photos,
                 videos = videos + excluded.videos, gifs = gifs + excluded.gifs,
                 stickers = stickers + excluded.stickers, links = links + excluded.links""",
            (chat_id, user_id, day, *[counts[f] for f in FIELDS]))
        conn.execute("INSERT INTO users (chat_id, user_id, name) VALUES (?, ?, ?) "
                     "ON CONFLICT(chat_id, user_id) DO UPDATE SET name = excluded.name", (chat_id, user_id, name))


def query_people(chat_id: int, day: str = None):
    sql = """SELECT d.user_id, COALESCE(u.name, '؟'), SUM(d.messages), SUM(d.photos), SUM(d.videos),
                    SUM(d.gifs), SUM(d.stickers), SUM(d.links)
             FROM daily_stats d LEFT JOIN users u ON u.chat_id = d.chat_id AND u.user_id = d.user_id
             WHERE d.chat_id = ?"""
    args = [chat_id]
    if day:
        sql += " AND d.day = ?"
        args.append(day)
    sql += " GROUP BY d.user_id ORDER BY SUM(d.messages) DESC, d.user_id"
    rows = []
    for r in _stats_db().execute(sql, args).fetchall():
        item = {"user_id": r[0], "name": r[1]}
        item.update(dict(zip(FIELDS, r[2:])))
        rows.append(item)
    return rows


def query_since(chat_id: int):
    r = _stats_db().execute("SELECT MIN(day) FROM daily_stats WHERE chat_id = ?", (chat_id,)).fetchone()
    return r[0] if r else None


def classify_message(m) -> dict:
    counts = dict.fromkeys(FIELDS, 0)
    counts["messages"] = 1
    if m.sticker:
        counts["stickers"] = 1
    elif m.animation:
        counts["gifs"] = 1
    elif m.video or m.video_note:
        counts["videos"] = 1
    elif m.photo:
        counts["photos"] = 1
    entities = list(m.entities or []) + list(m.caption_entities or [])
    counts["links"] = sum(1 for e in entities if e.type in ("url", "text_link"))
    return counts


async def count_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not m or not user or not chat or user.is_bot or chat.type not in GROUP_TYPES:
        return
    try:
        name = (user.full_name or user.username or "بدون‌نام").strip()[:30]
        add_counts(chat.id, user.id, name, classify_message(m))
    except Exception as e:
        logger.warning(f"stats recording failed: {e}")


_RANKS = ["🥇", "🥈", "🥉"]
_SEP = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def _compact_line(row: dict, lang="fa") -> str:
    parts = [f"💬 {fmt_int(row['messages'], lang)}"]
    for f in FIELDS[1:]:
        if row[f]:
            parts.append(f"{STAT_ICONS[f]} {fmt_int(row[f], lang)}")
    return "  ·  ".join(parts)


def build_group_text(rows, total_mode: bool, since_iso: str = None, lang="fa") -> str:
    if total_mode:
        title = L(lang, "آمار کل گروه", "Group stats (all time)")
        sub = L(lang, f"🗓 از {date_from_iso(since_iso, lang)} تا امروز",
                f"🗓 From {date_from_iso(since_iso, lang)} until today") if since_iso else ""
        empty = L(lang, "📭 هنوز آماری ثبت نشده.", "📭 No stats recorded yet.")
    else:
        title = L(lang, "آمار امروز گروه", "Group stats today")
        sub = f"📅 {date_from_iso(_today_iso(), lang)}"
        empty = L(lang, "📭 امروز هنوز پیامی ثبت نشده.", "📭 No messages recorded today yet.")

    if not rows:
        return f"📊 <b>{title}</b>\n\n{empty}"

    lines = [f"📊 <b>{title}</b>"]
    if sub:
        lines.append(sub)
    lines.append("")
    for i, row in enumerate(rows[:MAX_PEOPLE]):
        rank = _RANKS[i] if i < 3 else f"{_digits(i + 1, lang)}."
        lines.append(f"{rank} <b>{html.escape(row['name'])}</b>")
        lines.append(_compact_line(row, lang))
        lines.append("")
    hidden = len(rows) - MAX_PEOPLE
    if hidden > 0:
        lines.append(L(lang, f"… و {_digits(hidden, lang)} نفر دیگه", f"… and {hidden} more"))
        lines.append("")
    totals = {f: sum(r[f] for r in rows) for f in FIELDS}
    lines.append(_SEP)
    lines.append(L(lang, f"👥 مجموع {_digits(len(rows), lang)} نفر", f"👥 Total of {len(rows)} people"))
    lines.append(_compact_line(totals, lang))
    lines.append("")
    legend = "  ·  ".join(f"{STAT_ICONS[f]} {L(lang, *STAT_LABELS[f])}" for f in FIELDS)
    lines.append(f"<i>{legend}</i>")
    return "\n".join(lines)


def build_person_text(name: str, row, total_mode: bool, since_iso: str = None, lang="fa") -> str:
    if total_mode:
        label = L(lang, "آمار کل", "All-time stats")
        if since_iso:
            label += L(lang, f" · از {date_from_iso(since_iso, lang)}", f" · since {date_from_iso(since_iso, lang)}")
    else:
        label = L(lang, f"آمار امروز · {date_from_iso(_today_iso(), lang)}", f"Today's stats · {date_from_iso(_today_iso(), lang)}")
    lines = [f"👤 <b>{html.escape(name)}</b>", f"📅 {label}", ""]
    if not row:
        lines.append(L(lang, "📭 هنوز چیزی از این نفر ثبت نشده.", "📭 Nothing recorded for this person yet."))
        return "\n".join(lines)
    for f in FIELDS:
        lines.append(f"{STAT_ICONS[f]} {L(lang, *STAT_LABELS[f])}: <b>{fmt_int(row[f], lang)}</b>")
    return "\n".join(lines)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, total_mode: bool):
    msg = update.effective_message
    chat = update.effective_chat
    if not chat or chat.type not in GROUP_TYPES:
        return
    lang = lang_of(update)
    rows = query_people(chat.id, None if total_mode else _today_iso())
    since = query_since(chat.id) if total_mode else None
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if target and not target.is_bot:
        row = next((r for r in rows if r["user_id"] == target.id), None)
        name = (target.full_name or target.username or "بدون‌نام").strip()[:30]
        text = build_person_text(name, row, total_mode, since, lang)
    else:
        text = build_group_text(rows, total_mode, since, lang)
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


# ---- نرخ‌ها و تاریخچه ----
_rate_cache = {}


def _fetch_prices_blocking(want) -> dict:
    out = {}
    tgju = fetch_tgju_data()
    usd_toman = get_usd_to_toman_rate(tgju)
    if "dollar" in want:
        out["dollar"] = usd_toman
    if "gold" in want:
        out["gold"], _ = get_fiat_gold_price(tgju, "geram18")
    if "usdt" in want:
        try:
            out["usdt"], _ = get_price_toman(fetch_coingecko_data(), "usdt", usd_toman)
        except Exception as e:
            logger.warning(f"CoinGecko price fetch failed: {e}")
            out["usdt"] = None
    now = time.time()
    for k, v in out.items():
        if v:
            _rate_cache[k] = (now, v)
    return out


async def get_rate(symbol: str):
    hit = _rate_cache.get(symbol)
    if hit and time.time() - hit[0] < RATE_CACHE_SECONDS:
        return hit[1]
    try:
        prices = await asyncio.to_thread(_fetch_prices_blocking, {symbol})
    except Exception as e:
        logger.warning(f"rate fetch failed: {e}")
        return None
    return prices.get(symbol)


async def record_prices_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        prices = await asyncio.to_thread(_fetch_prices_blocking, {"dollar", "gold", "usdt"})
    except Exception as e:
        logger.warning(f"price recording failed: {e}")
        return
    now = int(time.time())
    conn = _stats_db()
    with conn:
        for symbol, price in prices.items():
            if price:
                conn.execute("INSERT INTO price_history (symbol, ts, price) VALUES (?, ?, ?)", (symbol, now, float(price)))
        conn.execute("DELETE FROM price_history WHERE ts < ?", (now - PRICE_KEEP_DAYS * 86400,))


# ---- تبدیل ارز ----
_AMOUNT = r"(\d+(?:\.\d+)?)(?: (هزار|میلیون|میلیارد|thousand|million|billion))?"
_CUR = r"(دلار|تتر|dollars?|usd|tether|usdt)"
_TO_TOMAN_RE = re.compile(rf"^{_AMOUNT} {_CUR}(?: (?:به|to|in) (?:تومان|toman))?$")
_FROM_TOMAN_RE = re.compile(rf"^{_AMOUNT} (?:تومان|toman)(?: (?:به|در|to|in))? {_CUR}$")
_MULT = {None: 1, "هزار": 1_000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000,
         "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_CUR_SYMBOL = {"دلار": "dollar", "dollar": "dollar", "dollars": "dollar", "usd": "dollar",
               "تتر": "usdt", "tether": "usdt", "usdt": "usdt"}
_CUR_INFO = {"dollar": ("💵", "دلار", "USD"), "usdt": ("🪙", "تتر", "USDT")}
_MAX_AMOUNT = 1e13


def parse_conversion(text: str):
    """(جهت، مقدار، symbol) یا None. جهت: 'to_toman' یا 'from_toman'"""
    m = _TO_TOMAN_RE.match(text)
    if m:
        return "to_toman", float(m.group(1)) * _MULT[m.group(2)], _CUR_SYMBOL[m.group(3)]
    m = _FROM_TOMAN_RE.match(text)
    if m:
        return "from_toman", float(m.group(1)) * _MULT[m.group(2)], _CUR_SYMBOL[m.group(3)]
    return None


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed):
    msg = update.effective_message
    chat = update.effective_chat
    lang = lang_of(update)
    direction, amount, symbol = parsed
    emoji, name_fa, name_en = _CUR_INFO[symbol]
    cur_name = L(lang, name_fa, name_en)
    if not _feature_ok(chat, "convert"):
        return False
    if symbol == "dollar" and not _feature_ok(chat, "dollar"):
        return False
    if amount <= 0 or amount > _MAX_AMOUNT:
        return

    rate = await get_rate(symbol)
    if not rate:
        sent = await msg.reply_text(L(lang, "❌ نتونستم نرخ رو بگیرم، چند لحظه‌ی دیگه دوباره امتحان کن.",
                                      "❌ I couldn't get the rate, please try again in a moment."))
        _schedule_delete(context, chat, [sent.message_id, msg.message_id])
        return

    toman = L(lang, "تومان", "Toman")
    if direction == "to_toman":
        head = f"{emoji} {fmt_num(amount, lang)} {cur_name}"
        result = f"{fmt_int(amount * rate, lang)} {toman}"
    else:
        head = f"💰 {fmt_int(amount, lang)} {toman}"
        result = f"{fmt_num(amount / rate, lang)} {cur_name}"
    text = (f"<b>{head}</b>\n\n↔️ {L(lang, 'حدوداً', 'About')} <b>{result}</b>\n\n"
            f"📌 {L(lang, f'نرخ هر {cur_name}', f'Rate per {cur_name}')}: {fmt_int(rate, lang)} {toman}")
    sent = await msg.reply_text(text, parse_mode=ParseMode.HTML)
    _schedule_delete(context, chat, [sent.message_id, msg.message_id])


# ---- نمودار ----
_CHART_RE = re.compile(r"^نمودار (دلار|طلا|تتر)(?: (امروز|روز|هفته|هفتگی|ماه|ماهانه))?$")
_CHART_RE_EN = re.compile(r"^chart (dollar|gold|tether|usdt)(?: (today|day|week|weekly|month|monthly))?$")
_CHART_SYMBOL = {"دلار": "dollar", "طلا": "gold", "تتر": "usdt", "dollar": "dollar", "gold": "gold",
                 "tether": "usdt", "usdt": "usdt"}
_P24 = (1, ("۲۴ ساعت اخیر", "Last 24 hours"))
_P7 = (7, ("۷ روز اخیر", "Last 7 days"))
_P30 = (30, ("۳۰ روز اخیر", "Last 30 days"))
_CHART_PERIOD = {None: _P24, "امروز": _P24, "روز": _P24, "today": _P24, "day": _P24,
                 "هفته": _P7, "هفتگی": _P7, "week": _P7, "weekly": _P7,
                 "ماه": _P30, "ماهانه": _P30, "month": _P30, "monthly": _P30}
_font_ready = False


def _setup_font():
    global _font_ready
    if _font_ready or not HAS_MPL:
        return
    _font_ready = True
    if os.path.exists(FONT_PATH):
        try:
            font_manager.fontManager.addfont(FONT_PATH)
            matplotlib.rcParams["font.family"] = font_manager.FontProperties(fname=FONT_PATH).get_name()
        except Exception as e:
            logger.warning(f"chart font load failed: {e}")


CHART_W, CHART_H = 1200, 675
PANEL_MARGIN = 40


def render_chart(symbol: str, rows, period_days: int, period_label: str, lang="fa") -> io.BytesIO:
    _setup_font()
    if len(rows) > 700:
        step = len(rows) // 700 + 1
        rows = rows[::step] + [rows[-1]]

    times = [datetime.fromtimestamp(ts, TEHRAN) for ts, _ in rows]
    prices = [pr for _, pr in rows]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.18 or hi * 0.01
    y_lo, y_hi = lo - pad, hi + pad

    color = tuple(c / 255 for c in _coin_color(symbol))
    gold = (235 / 255, 180 / 255, 90 / 255)
    soft = "#e6dccb"

    fig = Figure(figsize=(CHART_W / 100, CHART_H / 100), dpi=100, facecolor="none")
    ax = fig.add_axes([0.12, 0.12, 0.80, 0.50], facecolor="none")
    ax.plot(times, prices, color=color, linewidth=3, solid_capstyle="round")
    ax.fill_between(times, prices, y_lo, color=color, alpha=0.18)
    ax.scatter([times[-1]], [prices[-1]], color=color, s=80, zorder=5)
    ax.set_ylim(y_lo, y_hi)
    ax.margins(x=0.02)

    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    if period_days <= 1:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TEHRAN))
    else:
        def _fmt_day(v, _):
            d = mdates.num2date(v, tz=TEHRAN)
            if lang == "en":
                return f"{d.month:02d}/{d.day:02d}"
            _, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
            return f"{jm:02d}/{jd:02d}"
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(_fmt_day))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7, tz=TEHRAN))
    ax.tick_params(colors=soft, labelsize=12, length=0)
    ax.grid(True, color="white", alpha=0.10, linewidth=1)
    for spine in ax.spines.values():
        spine.set_visible(False)

    first, last = prices[0], prices[-1]
    change = (last - first) / first * 100 if first else 0.0
    change_color = "#5ac882" if change >= 0 else "#ff5a5a"
    sign = "+" if change >= 0 else ""

    name = _tr_name(symbol, "en" if lang == "en" else "fa")
    toman = L(lang, "تومان", "Toman")
    fig.text(0.5, 0.865, _fa_mpl(name, lang), ha="center", va="center", fontsize=30, color="white")
    fig.text(0.5, 0.808, _fa_mpl(period_label, lang), ha="center", va="center", fontsize=15, color=soft)
    fig.text(0.5, 0.748, _fa_mpl(f"{int(last):,} {toman}", lang), ha="center", va="center", fontsize=25, color=gold)
    fig.text(0.5, 0.690, _fa_mpl(f"{sign}{change:.2f}" + ("%" if lang == "en" else "٪"), lang), ha="center",
             va="center", fontsize=16, color=change_color)

    chart_buf = io.BytesIO()
    fig.savefig(chart_buf, format="png", transparent=True)
    chart_buf.seek(0)
    chart = Image.open(chart_buf).convert("RGBA")
    if chart.size != (CHART_W, CHART_H):
        chart = chart.resize((CHART_W, CHART_H), Image.LANCZOS)

    base = _load_background(CHART_W, CHART_H)
    base = _glass_panel(base, PANEL_MARGIN, PANEL_MARGIN, CHART_W - PANEL_MARGIN, CHART_H - PANEL_MARGIN)
    final = Image.alpha_composite(base.convert("RGBA"), chart).convert("RGB")
    buf = io.BytesIO()
    final.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "chart.png"
    return buf


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, period_key):
    msg = update.effective_message
    chat = update.effective_chat
    lang = lang_of(update)
    symbol = _CHART_SYMBOL[name]
    days, label_pair = _CHART_PERIOD[period_key]
    label = L(lang, *label_pair)
    if not _feature_ok(chat, "chart"):
        return False
    if symbol == "dollar" and not _feature_ok(chat, "dollar"):
        return False

    async def _reply_and_clean(text):
        sent = await msg.reply_text(text)
        _schedule_delete(context, chat, [sent.message_id, msg.message_id])

    if not HAS_MPL:
        await _reply_and_clean(L(lang, "❌ کتابخونه‌ی matplotlib نصب نیست، نمودار کار نمی‌کنه.",
                                 "❌ matplotlib is not installed, charts won't work."))
        return

    rows = _stats_db().execute("SELECT ts, price FROM price_history WHERE symbol = ? AND ts >= ? ORDER BY ts",
                               (symbol, int(time.time()) - days * 86400)).fetchall()
    if len(rows) < 3:
        await _reply_and_clean(L(lang, "⏳ هنوز داده‌ی کافی برای نمودار جمع نشده، چند دقیقه‌ی دیگه دوباره امتحان کن.",
                                 "⏳ Not enough data for the chart yet, please try again in a few minutes."))
        return

    try:
        buf = await asyncio.to_thread(render_chart, symbol, rows, days, label, lang)
    except Exception as e:
        logger.exception("chart rendering failed")
        await _reply_and_clean(L(lang, f"❌ نتونستم نمودار رو بسازم.\n{e}", f"❌ I couldn't build the chart.\n{e}"))
        return

    name_txt = _tr_name(symbol, "en" if lang == "en" else "fa")
    toman = L(lang, "تومان", "Toman")
    caption = L(lang, f"📈 نمودار {name_txt} · {label}\n💰 آخرین قیمت: {fmt_int(rows[-1][1], lang)} {toman}",
                f"📈 {name_txt} chart · {label}\n💰 Latest price: {fmt_int(rows[-1][1], lang)} {toman}")
    covered_hours = (rows[-1][0] - rows[0][0]) / 3600
    if covered_hours < days * 24 * 0.9:
        if covered_hours < 48:
            h = int(covered_hours) or 1
            caption += L(lang, f"\n\n📌 فعلاً فقط داده‌ی {_digits(h, lang)} ساعت اخیر ثبت شده.",
                         f"\n\n📌 Only the last {h} hour(s) of data have been recorded so far.")
        else:
            d = int(covered_hours // 24)
            caption += L(lang, f"\n\n📌 فعلاً فقط داده‌ی {_digits(d, lang)} روز اخیر ثبت شده.",
                         f"\n\n📌 Only the last {d} day(s) of data have been recorded so far.")
    sent = await msg.reply_photo(photo=buf, caption=caption)
    _schedule_delete(context, chat, [sent.message_id, msg.message_id])


_STATS_TODAY = {"آمار", "امار", "stats"}
_STATS_TOTAL = {"آمار کل", "امار کل", "stats total", "total stats"}


async def extras_handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگه پیام یکی از دستورهای آمار/تبدیل/نمودار بود انجامش می‌ده و True برمی‌گردونه"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not msg.text or len(msg.text) > 80:
        return False
    text = _norm(msg.text)
    in_group = bool(chat and chat.type in GROUP_TYPES)

    if text in _STATS_TODAY or text in _STATS_TOTAL:
        if not in_group:
            return False
        if not _feature_ok(chat, "stats"):
            return True
        await cmd_stats(update, context, total_mode=text in _STATS_TOTAL)
        return True

    m = _CHART_RE.match(text) or _CHART_RE_EN.match(text)
    if m:
        return await cmd_chart(update, context, m.group(1), m.group(2)) is not False

    parsed = parse_conversion(text)
    if parsed:
        return await cmd_convert(update, context, parsed) is not False
    return False


async def _group_text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await extras_handle_text(update, context)


def register_extras(application):
    _stats_db()
    # شمارش پیام‌ها تو گروه -5 و دستورات تو گروه 1 (تو PTB هر گروه فقط اولین handler مچ‌شده اجرا می‌شه)
    application.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, count_message), group=-5)
    application.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        _group_text_entry), group=1)
    if application.job_queue:
        application.job_queue.run_repeating(record_prices_job, interval=PRICE_SAMPLE_INTERVAL, first=15)
    else:
        logger.warning("job_queue is not installed; prices won't be recorded for charts. "
                       "Install python-telegram-bot[job-queue].")


# ============================================================================
# بازی‌ها / GAMES
# ============================================================================
async def cmd_tas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_dice(emoji="🎲")


async def cmd_shir_khat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = lang_of(update)
    result = random.choice([L(lang, "شیر 🦁", "Heads 🦁"), L(lang, "خط 〰️", "Tails 〰️")])
    await update.effective_message.reply_text(L(lang, f"🪙 نتیجه: {result}", f"🪙 Result: {result}"))


RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
_RPS_LEGACY = {"سنگ": "rock", "کاغذ": "paper", "قیچی": "scissors"}
RPS_TIMEOUT = 120


def _beats(a, b):
    return (a == "rock" and b == "scissors") or (a == "paper" and b == "rock") or (a == "scissors" and b == "paper")


def _rps_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🪨", callback_data="rps_pick:rock"),
        InlineKeyboardButton("📄", callback_data="rps_pick:paper"),
        InlineKeyboardButton("✂️", callback_data="rps_pick:scissors"),
    ]])


async def cmd_sang_kaghaz_gheychi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    lang = lang_of(update)

    existing = context.chat_data.get("rps_game")
    if existing and existing.get("status") == "playing":
        if time.time() - existing.get("created_at", 0) < RPS_TIMEOUT:
            await message.reply_text(L(lang, "⚠️ یه بازی سنگ‌کاغذقیچی از قبل تو این گروه در حال اجراست.",
                                       "⚠️ A rock-paper-scissors game is already running in this group."))
            return
        context.chat_data["rps_game"] = None

    reply_target = message.reply_to_message.from_user if message.reply_to_message else None
    if reply_target and not reply_target.is_bot:
        if reply_target.id == user.id:
            await message.reply_text(L(lang, "نمی‌تونی با خودت بازی کنی! 😄", "You can't play against yourself! 😄"))
            return
        context.chat_data["rps_game"] = {
            "mode": "vs_player", "player1_id": user.id, "player1_name": user.full_name,
            "player2_id": reply_target.id, "player2_name": reply_target.full_name,
            "choices": {}, "status": "playing", "created_at": time.time(),
        }
        await message.reply_text(
            L(lang, f"✂️📄🪨 {user.full_name} با {reply_target.full_name} به چالش سنگ‌کاغذقیچی افتاد!\n\nهردو نفر دکمه‌ی انتخابشون رو بزنن:",
              f"✂️📄🪨 {user.full_name} challenged {reply_target.full_name} to rock-paper-scissors!\n\nBoth players, press your choice:"),
            reply_markup=_rps_keyboard())
        return

    context.chat_data["rps_game"] = {
        "mode": "vs_bot", "player1_id": user.id, "player1_name": user.full_name,
        "choices": {}, "status": "playing", "created_at": time.time(),
    }
    await message.reply_text(L(lang, f"✂️📄🪨 {user.full_name}، با من بازی کن! انتخابتو بزن:",
                               f"✂️📄🪨 {user.full_name}, play with me! Press your choice:"),
                             reply_markup=_rps_keyboard())


async def rps_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    choice = query.data.split(":")[1]
    choice = _RPS_LEGACY.get(choice, choice)
    if choice not in RPS_EMOJI:
        await query.answer()
        return
    game = context.chat_data.get("rps_game")
    user = update.effective_user
    lang = get_lang(query.message.chat_id)
    E = RPS_EMOJI

    if not game or game.get("status") != "playing":
        await query.answer(L(lang, "این بازی دیگه فعال نیست.", "This game is no longer active."), show_alert=True)
        return
    if time.time() - game.get("created_at", 0) > RPS_TIMEOUT:
        context.chat_data["rps_game"] = None
        await query.answer(L(lang, "⌛ این بازی منقضی شده.", "⌛ This game has expired."), show_alert=True)
        await _safe_edit(query, L(lang, "⌛ بازی سنگ‌کاغذقیچی به‌خاطر بی‌جوابی بسته شد.",
                                  "⌛ The rock-paper-scissors game was closed due to no response."))
        return

    if game["mode"] == "vs_bot":
        if user.id != game["player1_id"]:
            await query.answer(L(lang, "⛔️ این بازی مال تو نیست.", "⛔️ This game isn't yours."), show_alert=True)
            return
        bot_choice = random.choice(list(E.keys()))
        await query.answer()
        if choice == bot_choice:
            result_line = L(lang, f"مساوی شدیم! هر دو {E[choice]} انتخاب کردیم. 🤝", f"It's a draw! We both chose {E[choice]}. 🤝")
        elif _beats(choice, bot_choice):
            result_line = L(lang, f"🎉 بردی! ({E[choice]} در برابر {E[bot_choice]})", f"🎉 You won! ({E[choice]} vs {E[bot_choice]})")
        else:
            result_line = L(lang, f"😅 باختی! ({E[bot_choice]} در برابر {E[choice]})", f"😅 You lost! ({E[bot_choice]} vs {E[choice]})")
        await query.edit_message_text(
            L(lang, f"✂️📄🪨 نتیجه\n\n{game['player1_name']}: {E[choice]}\nمن: {E[bot_choice]}\n\n{result_line}",
              f"✂️📄🪨 Result\n\n{game['player1_name']}: {E[choice]}\nMe: {E[bot_choice]}\n\n{result_line}"))
        context.chat_data["rps_game"] = None
        return

    if user.id not in (game["player1_id"], game["player2_id"]):
        await query.answer(L(lang, "⛔️ تو تو این بازی نیستی.", "⛔️ You're not in this game."), show_alert=True)
        return
    if user.id in game["choices"]:
        await query.answer(L(lang, "قبلاً انتخاب کردی، منتظر حریفت باش.", "You already chose, wait for your opponent."), show_alert=True)
        return

    game["choices"][user.id] = choice
    await query.answer(L(lang, "✅ انتخابت ثبت شد.", "✅ Your choice was recorded."))

    if len(game["choices"]) < 2:
        other_name = game["player2_name"] if user.id == game["player1_id"] else game["player1_name"]
        await _safe_edit(query, L(
            lang, f"✂️📄🪨 {game['player1_name']} 🆚 {game['player2_name']}\n\n✅ یک نفر انتخابش رو کرد.\n⏳ منتظر جواب {other_name}...",
            f"✂️📄🪨 {game['player1_name']} 🆚 {game['player2_name']}\n\n✅ One player has chosen.\n⏳ Waiting for {other_name}..."),
            _rps_keyboard())
        return

    c1, c2 = game["choices"][game["player1_id"]], game["choices"][game["player2_id"]]
    if c1 == c2:
        result_line = L(lang, f"مساوی شد! هر دو {E[c1]} انتخاب کردن. 🤝", f"It's a draw! Both chose {E[c1]}. 🤝")
    elif _beats(c1, c2):
        result_line = L(lang, f"🏆 {game['player1_name']} برد! ({E[c1]} در برابر {E[c2]})", f"🏆 {game['player1_name']} won! ({E[c1]} vs {E[c2]})")
    else:
        result_line = L(lang, f"🏆 {game['player2_name']} برد! ({E[c2]} در برابر {E[c1]})", f"🏆 {game['player2_name']} won! ({E[c2]} vs {E[c1]})")
    await query.edit_message_text(
        L(lang, f"✂️📄🪨 نتیجه\n\n{game['player1_name']}: {E[c1]}\n{game['player2_name']}: {E[c2]}\n\n{result_line}",
          f"✂️📄🪨 Result\n\n{game['player1_name']}: {E[c1]}\n{game['player2_name']}: {E[c2]}\n\n{result_line}"))
    context.chat_data["rps_game"] = None


# ---- دوز (XO) ----
DOOZ_EMPTY = "⬜"
DOOZ_SYMBOL = {"X": "❌", "O": "⭕"}
DOOZ_LEVELS = {"easy": ("آسان 😊", "Easy 😊"), "medium": ("متوسط 😐", "Medium 😐"), "hard": ("سخت 😈", "Hard 😈")}
DOOZ_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
DOOZ_IDLE_TIMEOUT = 600
DOOZ_MAX_GAMES_PER_CHAT = 12


def _dooz_winner(board):
    for a, b, c in DOOZ_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def _dooz_result(board):
    w = _dooz_winner(board)
    if w:
        return w
    if all(board):
        return "draw"
    return None


def _empty_cells(board):
    return [i for i, v in enumerate(board) if not v]


def _find_win_move(board, who):
    for i in _empty_cells(board):
        trial = list(board)
        trial[i] = who
        if _dooz_winner(trial) == who:
            return i
    return None


@lru_cache(maxsize=None)
def _minimax(board: tuple, turn: str, me: str) -> int:
    w = _dooz_winner(board)
    bonus = 1 + board.count("")
    if w == me:
        return bonus
    if w:
        return -bonus
    empties = [i for i, v in enumerate(board) if not v]
    if not empties:
        return 0
    other = "O" if turn == "X" else "X"
    scores = []
    for i in empties:
        trial = list(board)
        trial[i] = turn
        scores.append(_minimax(tuple(trial), other, me))
    return max(scores) if turn == me else min(scores)


def _dooz_bot_move(board, level, me="X", opp="O"):
    empties = _empty_cells(board)
    if level == "easy":
        return random.choice(empties)
    if level == "medium":
        move = _find_win_move(board, me)
        if move is None:
            move = _find_win_move(board, opp)
        if move is not None:
            return move
        if 4 in empties and random.random() < 0.6:
            return 4
        return random.choice(empties)
    best, moves = None, []
    for i in empties:
        trial = list(board)
        trial[i] = me
        score = _minimax(tuple(trial), opp, me)
        if best is None or score > best:
            best, moves = score, [i]
        elif score == best:
            moves.append(i)
    return random.choice(moves)


def _dooz_keyboard(board):
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            row.append(InlineKeyboardButton(DOOZ_SYMBOL[board[i]] if board[i] else DOOZ_EMPTY, callback_data=f"dooz:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _dooz_level_keyboard(lang):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(L(lang, *DOOZ_LEVELS[k]), callback_data=f"dooz_lvl:{k}") for k in ("easy", "medium", "hard")
    ]])


def _dooz_text(game, lang, result=None):
    players = game["players"]
    if game["mode"] == "bot":
        name = players["O"]["name"]
        level = L(lang, *DOOZ_LEVELS[game["level"]])
        lines = [L(lang, f"🎮 دوز با ربات · {level}", f"🎮 XO vs bot · {level}"), "",
                 L(lang, f"شما بازی می‌کنید به عنوان {DOOZ_SYMBOL['O']}", f"You are playing as {DOOZ_SYMBOL['O']}")]
        if result == "O":
            lines.append(L(lang, f"\n🎉 {name} برد!", f"\n🎉 {name} won!"))
        elif result == "X":
            lines.append(L(lang, "\n😈 من بردم!", "\n😈 I won!"))
        elif result == "draw":
            lines.append(L(lang, "\n🤝 مساوی شد!", "\n🤝 It's a draw!"))
        else:
            lines.append(L(lang, f"نوبت {name} 👈", f"{name}'s turn 👈"))
        return "\n".join(lines)

    lines = ["🎮 " + L(lang, "دوز", "XO"),
             f"{DOOZ_SYMBOL['X']} {players['X']['name']}  🆚  {DOOZ_SYMBOL['O']} {players['O']['name']}", ""]
    if result in ("X", "O"):
        lines.append(L(lang, f"🏆 {players[result]['name']} برد!", f"🏆 {players[result]['name']} won!"))
    elif result == "draw":
        lines.append(L(lang, "🤝 مساوی شد!", "🤝 It's a draw!"))
    else:
        turn = game["turn"]
        lines.append(L(lang, f"نوبت {DOOZ_SYMBOL[turn]} {players[turn]['name']} 👈",
                       f"{DOOZ_SYMBOL[turn]} {players[turn]['name']}'s turn 👈"))
    return "\n".join(lines)


def _dooz_store(context):
    return context.chat_data.setdefault("dooz_games", {})


def _dooz_prune(games):
    now = time.time()
    for mid in [m for m, g in games.items() if now - g["updated_at"] > DOOZ_IDLE_TIMEOUT * 3]:
        games.pop(mid, None)
    while len(games) >= DOOZ_MAX_GAMES_PER_CHAT:
        games.pop(min(games, key=lambda m: games[m]["updated_at"]), None)


async def _dooz_expire(query, games, lang):
    games.pop(query.message.message_id, None)
    await query.answer(L(lang, "⌛ این بازی به‌خاطر بی‌تحرکی بسته شده.", "⌛ This game was closed due to inactivity."), show_alert=True)
    await _safe_edit(query, L(lang, "⌛ بازی دوز به‌خاطر بی‌تحرکی بسته شد.", "⌛ The XO game was closed due to inactivity."))


async def cmd_dooz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    lang = lang_of(update)
    games = _dooz_store(context)
    _dooz_prune(games)
    now = time.time()
    target = message.reply_to_message.from_user if message.reply_to_message else None

    if target and not target.is_bot:
        if target.id == user.id:
            await message.reply_text(L(lang, "نمی‌تونی با خودت بازی کنی! 😄", "You can't play against yourself! 😄"))
            return
        game = {"mode": "player", "board": [""] * 9, "turn": "X", "status": "playing",
                "players": {"X": {"id": user.id, "name": user.full_name}, "O": {"id": target.id, "name": target.full_name}},
                "created_at": now, "updated_at": now}
        sent = await message.reply_text(_dooz_text(game, lang), reply_markup=_dooz_keyboard(game["board"]))
        games[sent.message_id] = game
        return

    game = {"mode": "bot", "status": "choose_level", "owner_id": user.id, "owner_name": user.full_name,
            "created_at": now, "updated_at": now}
    sent = await message.reply_text(
        L(lang, f"🎮 دوز با ربات\n\n{user.full_name}، سطح بازی رو انتخاب کن:",
          f"🎮 XO vs bot\n\n{user.full_name}, choose the difficulty:"),
        reply_markup=_dooz_level_keyboard(lang))
    games[sent.message_id] = game


async def dooz_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    level = query.data.split(":")[1]
    user = update.effective_user
    lang = get_lang(query.message.chat_id)
    games = _dooz_store(context)
    game = games.get(query.message.message_id)

    if not game or game.get("status") != "choose_level" or level not in DOOZ_LEVELS:
        await query.answer(L(lang, "این بازی دیگه فعال نیست.", "This game is no longer active."), show_alert=True)
        return
    if user.id != game["owner_id"]:
        await query.answer(L(lang, "⛔️ این بازی مال تو نیست.", "⛔️ This game isn't yours."), show_alert=True)
        return
    if time.time() - game["updated_at"] > DOOZ_IDLE_TIMEOUT:
        await _dooz_expire(query, games, lang)
        return

    board = [""] * 9
    game.update({"level": level, "board": board, "turn": "O", "status": "playing",
                 "players": {"X": {"id": None, "name": L(lang, "ربات", "Bot")}, "O": {"id": user.id, "name": user.full_name}},
                 "updated_at": time.time()})
    board[_dooz_bot_move(board, level)] = "X"
    await query.answer()
    await _safe_edit(query, _dooz_text(game, lang), _dooz_keyboard(board))


async def dooz_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    idx = int(query.data.split(":")[1])
    user = update.effective_user
    lang = get_lang(query.message.chat_id)
    games = _dooz_store(context)
    message_id = query.message.message_id
    game = games.get(message_id)

    if not game or game.get("status") != "playing":
        await query.answer(L(lang, "این بازی دیگه فعال نیست.", "This game is no longer active."), show_alert=True)
        return
    if time.time() - game["updated_at"] > DOOZ_IDLE_TIMEOUT:
        await _dooz_expire(query, games, lang)
        return

    board = game["board"]
    if game["mode"] == "bot":
        if user.id != game["players"]["O"]["id"]:
            await query.answer(L(lang, "⛔️ این بازی مال تو نیست.", "⛔️ This game isn't yours."), show_alert=True)
            return
        if board[idx]:
            await query.answer(L(lang, "این خونه پره!", "This cell is taken!"), show_alert=True)
            return
        board[idx] = "O"
        result = _dooz_result(board)
        if not result:
            board[_dooz_bot_move(board, game["level"])] = "X"
            result = _dooz_result(board)
    else:
        turn = game["turn"]
        if user.id != game["players"][turn]["id"]:
            in_game = user.id in (game["players"]["X"]["id"], game["players"]["O"]["id"])
            await query.answer(L(lang, "⏳ نوبت تو نیست." if in_game else "⛔️ تو تو این بازی نیستی.",
                                 "⏳ It's not your turn." if in_game else "⛔️ You're not in this game."), show_alert=True)
            return
        if board[idx]:
            await query.answer(L(lang, "این خونه پره!", "This cell is taken!"), show_alert=True)
            return
        board[idx] = turn
        result = _dooz_result(board)
        if not result:
            game["turn"] = "O" if turn == "X" else "X"

    game["updated_at"] = time.time()
    await query.answer()
    await _safe_edit(query, _dooz_text(game, lang, result), None if result else _dooz_keyboard(board))
    if result:
        games.pop(message_id, None)


# ============================================================================
# ترجمه / TRANSLATE
# زبان مقصد ترجمه (tr_lang_) کاملاً جدا از زبان ربات (bot_lang_) ه.
# ============================================================================
LANGUAGES = {
    "fa": ("فارسی", "Persian"), "en": ("انگلیسی", "English"), "ar": ("عربی", "Arabic"),
    "tr": ("ترکی", "Turkish"), "ru": ("روسی", "Russian"), "fr": ("فرانسه", "French"),
    "es": ("اسپانیایی", "Spanish"), "de": ("آلمانی", "German"), "zh-CN": ("چینی", "Chinese"),
}
DEFAULT_TRIGGER = "."


def get_translate_trigger(chat_id):
    return get_setting(f"translate_trigger_{chat_id}", DEFAULT_TRIGGER)


def set_translate_trigger(chat_id, trigger):
    set_setting(f"translate_trigger_{chat_id}", trigger)


def _lang_name(code, lang):
    pair = LANGUAGES.get(code)
    return L(lang, *pair) if pair else code


def translate_text(text: str, target_lang: str) -> str:
    params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": text}
    resp = requests.get("https://translate.googleapis.com/translate_a/single", params=params, timeout=10)
    resp.raise_for_status()
    return "".join(part[0] for part in resp.json()[0] if part[0])


async def cmd_tarjome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    message = update.effective_message
    if not chat or chat.type not in GROUP_TYPES:
        return
    if not is_feature_enabled(chat.id, "translate"):
        return
    lang = lang_of(update)
    if not message.reply_to_message:
        await message.reply_text(L(lang, "❗️ روی پیام مورد نظر ریپلای کن و بنویس: ترجمه",
                                   "❗️ Reply to the message and write: translate"))
        return
    source_text = message.reply_to_message.text or message.reply_to_message.caption
    if not source_text:
        await message.reply_text(L(lang, "❗️ این پیام متنی برای ترجمه نداره.", "❗️ This message has no text to translate."))
        return
    target = get_translate_lang(chat.id)
    try:
        translated = await asyncio.to_thread(translate_text, source_text, target)
    except Exception as e:
        await message.reply_text(L(lang, f"✘ ترجمه انجام نشد.\n{e}", f"✘ Translation failed.\n{e}"))
        return
    await message.reply_text(L(lang, f"🌐 ترجمه ({_lang_name(target, lang)}):\n{translated}",
                               f"🌐 Translation ({_lang_name(target, lang)}):\n{translated}"))


async def check_dot_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or chat.type not in GROUP_TYPES:
        return False
    if not is_feature_enabled(chat.id, "translate"):
        return False
    trigger = get_translate_trigger(chat.id)
    text = (message.text or "").strip()
    if not trigger or not text.startswith(trigger) or len(text) <= len(trigger):
        return False
    to_translate = text[len(trigger):].strip()
    if not to_translate:
        return False
    lang = lang_of(update)
    target = get_translate_lang(chat.id)
    try:
        translated = await asyncio.to_thread(translate_text, to_translate, target)
    except Exception as e:
        await message.reply_text(L(lang, f"✘ ترجمه انجام نشد.\n{e}", f"✘ Translation failed.\n{e}"))
        return True
    await message.reply_text(L(lang, f"🌐 ترجمه ({_lang_name(target, lang)}):\n{translated}",
                               f"🌐 Translation ({_lang_name(target, lang)}):\n{translated}"))
    return True


def _translate_panel_text(chat_id, lang, extra_line=None):
    current = get_translate_lang(chat_id)
    trigger = get_translate_trigger(chat_id)
    on = is_feature_enabled(chat_id, "translate")
    status = L(lang, "✔ فعال" if on else "✘ غیرفعال", "✔ On" if on else "✘ Off")
    text = L(lang,
             f"🌐 تنظیمات ترجمه\n\nوضعیت: {status}\nزبان مقصد فعلی: {_lang_name(current, lang)}\n"
             f"کلمه‌ی فعال‌ساز فعلی: «{trigger}»\n\nیه زبان انتخاب کن، یا کلمه‌ی فعال‌ساز رو تغییر بده:",
             f"🌐 Translation settings\n\nStatus: {status}\nCurrent target language: {_lang_name(current, lang)}\n"
             f"Current trigger: \"{trigger}\"\n\nChoose a language, or change the trigger:")
    if extra_line:
        text = f"{extra_line}\n\n{text}"
    return text


def _translate_panel_keyboard(chat_id, lang):
    current = get_translate_lang(chat_id)
    rows, line = [], []
    for code in LANGUAGES:
        mark = "✔ " if code == current else ""
        line.append(InlineKeyboardButton(f"{mark}{_lang_name(code, lang)}", callback_data=f"tr_set:{chat_id}:{code}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(L(lang, "✏️ تغییر کلمه‌ی فعال‌ساز", "✏️ Change trigger"), callback_data=f"tr_trigger:{chat_id}")])
    rows.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")])
    return InlineKeyboardMarkup(rows)


async def open_translate_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    await query.edit_message_text(_translate_panel_text(chat_id, lang), reply_markup=_translate_panel_keyboard(chat_id, lang))


async def set_translate_lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, code = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    set_translate_lang(chat_id, code)
    await query.answer(L(lang, "ذخیره شد ✔", "Saved ✔"))
    await _safe_edit(query, _translate_panel_text(chat_id, lang), _translate_panel_keyboard(chat_id, lang))


async def ask_set_translate_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    await query.answer()
    context.user_data["waiting_translate_trigger_chat_id"] = chat_id
    context.user_data["translate_prompt_chat_id"] = query.message.chat_id
    context.user_data["translate_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ انصراف", "⬅️ Cancel"), callback_data=f"tr_panel:{chat_id}")]])
    cur = get_translate_trigger(chat_id)
    await query.edit_message_text(
        L(lang, f"✏️ کلمه یا نشونه‌ی فعال‌ساز ترجمه رو بفرست.\n\nفعلی: «{cur}»\n"
                "مثلاً می‌تونی همین «.» رو نگه داری، یا چیزی مثل «:» یا «ترجمه:» بفرستی.\n\nبرای لغو، دستور /cancel را بفرستید.",
          f"✏️ Send the new translation trigger word or symbol.\n\nCurrent: \"{cur}\"\n"
          "For example keep \".\", or send something like \":\" or \"tr:\".\n\nTo cancel, send /cancel."),
        reply_markup=kb)


async def receive_translate_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = context.user_data.get("waiting_translate_trigger_chat_id")
    if not chat_id:
        return False
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    lang = lang_of(update)
    context.user_data["waiting_translate_trigger_chat_id"] = None
    prompt_chat_id = context.user_data.pop("translate_prompt_chat_id", None)
    prompt_message_id = context.user_data.pop("translate_prompt_message_id", None)
    new_trigger = (update.effective_message.text or "").strip()
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    if new_trigger == "/cancel":
        confirm = L(lang, "❌ لغو شد.", "❌ Cancelled.")
    elif not new_trigger:
        confirm = L(lang, "❗️ چیزی دریافت نشد، تغییری اعمال نشد.", "❗️ Nothing received, no change was made.")
    else:
        set_translate_trigger(chat_id, new_trigger)
        confirm = L(lang, f"✔ کلمه‌ی فعال‌ساز ترجمه به «{new_trigger}» تغییر کرد.",
                    f"✔ The translation trigger was changed to \"{new_trigger}\".")
    text = _translate_panel_text(chat_id, lang, extra_line=confirm)
    kb = _translate_panel_keyboard(chat_id, lang)
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id, text=text, reply_markup=kb)
            return True
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=kb)
    return True


# ============================================================================
# زبان عکس قیمت‌ها / PRICE IMAGE LANGUAGE
# ============================================================================
async def _imglang_can_manage(bot, user_id, chat_id):
    if await is_creator(user_id):
        return True
    return await is_group_owner(chat_id, user_id)


def _imglang_keyboard(chat_id, lang):
    current = get_image_lang(chat_id)
    rows = [[InlineKeyboardButton(f"{'✅ ' if code == current else ''}{name}", callback_data=f"imglang_set:{chat_id}:{code}")]
            for code, name in LANG_NAMES.items()]
    rows.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")])
    return InlineKeyboardMarkup(rows)


async def open_image_lang_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _imglang_can_manage(context.bot, update.effective_user.id, chat_id):
        await query.answer(_deny(lang), show_alert=True)
        return
    current = get_image_lang(chat_id)
    await query.edit_message_text(
        L(lang, f"🗣 زبان متن داخل عکس‌های قیمت\n\nفعلی: {LANG_NAMES.get(current, current)}\n\nیکی رو انتخاب کن:",
          f"🗣 Language of the text inside price images\n\nCurrent: {LANG_NAMES.get(current, current)}\n\nChoose one:"),
        reply_markup=_imglang_keyboard(chat_id, lang))


async def set_image_lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, code = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await _imglang_can_manage(context.bot, update.effective_user.id, chat_id):
        await query.answer(_deny(lang), show_alert=True)
        return
    set_image_lang(chat_id, code)
    await query.answer(L(lang, "ذخیره شد ✅", "Saved ✅"))
    await open_image_lang_panel(update, context)


# ============================================================================
# میان‌برهای دستورات / COMMAND SHORTCUTS
# کلید اصلی (فارسی) همیشه ثابته (تو دیتابیس گروه‌ها ذخیره شده)؛ فقط کلمه‌ی تایپ‌شده قابل‌تغییره.
# ============================================================================
DEFAULT_COMMANDS = {
    "خاموشی": ("قفل گروه", "Lock group"), "روشن": ("باز کردن گروه", "Unlock group"),
    "سکوت": ("سکوت دادن", "Mute"), "آزاد کن": ("آزاد کردن", "Unmute"),
    "بن کن": ("بن کردن", "Ban"), "اخطار": ("اخطار", "Warn"),
    "پاک": ("پاک کردن پیام", "Delete messages"), "گیف بن": ("بن کردن گیف", "Ban a GIF"),
    "استیکر بن": ("بن کردن استیکر", "Ban a sticker"), "تاس": ("بازی تاس", "Dice game"),
    "شیر یا خط": ("شیر یا خط", "Coin flip"), "سنگ کاغذ قیچی": ("سنگ‌کاغذقیچی", "Rock-paper-scissors"),
    "گزارش": ("گزارش کاربر", "Report a user"), "ترجمه": ("ترجمه", "Translate"), "تاریخ": ("تاریخ", "Date"),
}
COMMAND_ICONS = {
    "خاموشی": "🔒", "روشن": "🔓", "سکوت": "🔇", "آزاد کن": "🔊", "بن کن": "⛔️", "اخطار": "⚠️", "پاک": "🗑",
    "گیف بن": "🚫", "استیکر بن": "🚫", "تاس": "🎲", "شیر یا خط": "🪙", "سنگ کاغذ قیچی": "✊",
    "گزارش": "📩", "ترجمه": "🌐", "تاریخ": "📅",
}
COMMAND_KEYS_ORDER = list(DEFAULT_COMMANDS.keys())

# معادل انگلیسی هر دستور؛ همزمان با فارسی فعاله
ENGLISH_COMMANDS = {
    "lock": "خاموشی", "unlock": "روشن", "mute": "سکوت", "unmute": "آزاد کن", "ban": "بن کن",
    "warn": "اخطار", "del": "پاک", "gifban": "گیف بن", "stickerban": "استیکر بن", "dice": "تاس",
    "coin": "شیر یا خط", "rps": "سنگ کاغذ قیچی", "report": "گزارش", "translate": "ترجمه", "date": "تاریخ",
}
EN_FOR_KEY = {v: k for k, v in ENGLISH_COMMANDS.items()}


def get_command_aliases(chat_id):
    raw = get_setting(f"cmd_aliases_{chat_id}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_aliases(chat_id, aliases):
    set_setting(f"cmd_aliases_{chat_id}", json.dumps(aliases, ensure_ascii=False))


def set_command_alias(chat_id, key, new_word):
    aliases = get_command_aliases(chat_id)
    aliases[key] = new_word
    _save_aliases(chat_id, aliases)


def reset_command_alias(chat_id, key):
    aliases = get_command_aliases(chat_id)
    aliases.pop(key, None)
    _save_aliases(chat_id, aliases)


def reset_all_command_aliases(chat_id):
    _save_aliases(chat_id, {})


def get_active_keyword(chat_id, key):
    return get_command_aliases(chat_id).get(key, key)


def get_group_command_keywords(chat_id):
    """{کلمه‌ی فعلی (سفارشی یا پیش‌فرض): کلید اصلی}"""
    aliases = get_command_aliases(chat_id)
    return {aliases.get(key, key): key for key in DEFAULT_COMMANDS}


def _shortcuts_panel_text(chat_id, lang, extra_line=None):
    legend = "\n".join(f"{COMMAND_ICONS.get(k, '•')} {L(lang, *DEFAULT_COMMANDS[k])}" for k in COMMAND_KEYS_ORDER)
    text = L(lang, f"🔤 میان‌برهای دستورات گروه\n\n{legend}\n\nروی هرکدوم از دکمه‌های زیر بزن تا کلمه‌ی همون دستور رو عوض کنی:",
             f"🔤 Group command shortcuts\n\n{legend}\n\nTap a button below to change the word of that command:")
    if extra_line:
        text = f"{extra_line}\n\n{text}"
    return text


def _shortcuts_panel_keyboard(chat_id, lang):
    rows, line = [], []
    for i, key in enumerate(COMMAND_KEYS_ORDER):
        line.append(InlineKeyboardButton(f"{COMMAND_ICONS.get(key, '•')} «{get_active_keyword(chat_id, key)}»",
                                         callback_data=f"cmdalias_edit:{chat_id}:{i}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([InlineKeyboardButton(L(lang, "🧠 فعال‌ساز هوش مصنوعی", "🧠 AI trigger"), callback_data=f"ai_trigger_panel:{chat_id}")])
    rows.append([InlineKeyboardButton(L(lang, "🔄 بازنشانی همه به پیش‌فرض", "🔄 Reset all to default"), callback_data=f"cmdalias_resetall:{chat_id}")])
    rows.append([InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")])
    return InlineKeyboardMarkup(rows)


async def open_shortcuts_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    await query.edit_message_text(_shortcuts_panel_text(chat_id, lang), reply_markup=_shortcuts_panel_keyboard(chat_id, lang))


async def ask_edit_command_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, idx = query.data.split(":")
    chat_id, idx = int(chat_id), int(idx)
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    if idx < 0 or idx >= len(COMMAND_KEYS_ORDER):
        await query.answer(L(lang, "این مورد پیدا نشد.", "Item not found."), show_alert=True)
        return
    key = COMMAND_KEYS_ORDER[idx]
    await query.answer()
    context.user_data["waiting_cmdalias"] = (chat_id, key)
    context.user_data["cmdalias_prompt_chat_id"] = query.message.chat_id
    context.user_data["cmdalias_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "↩️ برگردوندن به پیش‌فرض", "↩️ Reset to default"), callback_data=f"cmdalias_reset:{chat_id}:{idx}")],
        [InlineKeyboardButton(L(lang, "⬅️ انصراف", "⬅️ Cancel"), callback_data=f"cmdshortcuts_panel:{chat_id}")],
    ])
    label = L(lang, *DEFAULT_COMMANDS[key])
    active = get_active_keyword(chat_id, key)
    await query.edit_message_text(
        L(lang, f"✏️ کلمه‌ی جدید برای «{label}» رو بفرست.\n\nپیش‌فرض: «{key}»\nفعلی: «{active}»\n\nبرای لغو، دستور /cancel را بفرستید.",
          f"✏️ Send the new word for \"{label}\".\n\nDefault: \"{key}\"\nCurrent: \"{active}\"\n\nTo cancel, send /cancel."),
        reply_markup=kb)


async def receive_command_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending = context.user_data.get("waiting_cmdalias")
    if not pending:
        return False
    chat_id, key = pending
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    lang = lang_of(update)
    context.user_data["waiting_cmdalias"] = None
    prompt_chat_id = context.user_data.pop("cmdalias_prompt_chat_id", None)
    prompt_message_id = context.user_data.pop("cmdalias_prompt_message_id", None)
    new_word = (update.effective_message.text or "").strip()
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    label = L(lang, *DEFAULT_COMMANDS[key])
    if new_word == "/cancel":
        confirm = L(lang, "❌ لغو شد.", "❌ Cancelled.")
    elif not new_word:
        confirm = L(lang, "❗️ چیزی دریافت نشد، تغییری اعمال نشد.", "❗️ Nothing received, no change was made.")
    else:
        set_command_alias(chat_id, key, new_word)
        confirm = L(lang, f"✔ کلمه‌ی «{label}» به «{new_word}» تغییر کرد.", f"✔ The word for \"{label}\" was changed to \"{new_word}\".")
    text = _shortcuts_panel_text(chat_id, lang, extra_line=confirm)
    kb = _shortcuts_panel_keyboard(chat_id, lang)
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id, text=text, reply_markup=kb)
            return True
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=kb)
    return True


async def reset_command_alias_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, idx = query.data.split(":")
    chat_id, idx = int(chat_id), int(idx)
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    if 0 <= idx < len(COMMAND_KEYS_ORDER):
        reset_command_alias(chat_id, COMMAND_KEYS_ORDER[idx])
    await query.answer(L(lang, "✔ به پیش‌فرض برگشت", "✔ Reset to default"))
    await query.edit_message_text(_shortcuts_panel_text(chat_id, lang), reply_markup=_shortcuts_panel_keyboard(chat_id, lang))


async def reset_all_command_aliases_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    reset_all_command_aliases(chat_id)
    await query.answer(L(lang, "✔ همه به پیش‌فرض برگشتن", "✔ All reset to default"))
    await query.edit_message_text(_shortcuts_panel_text(chat_id, lang), reply_markup=_shortcuts_panel_keyboard(chat_id, lang))


def build_group_help_text(chat_id, lang):
    pairs, line = [], []
    for key in COMMAND_KEYS_ORDER:
        icon = COMMAND_ICONS.get(key, "•")
        active = get_active_keyword(chat_id, key)
        if lang == "en":
            en_word = EN_FOR_KEY.get(key, key)
            item = f"{icon} {en_word}" + (f" ({active})" if active != key else "")
        else:
            item = f"{icon} {active}"
        line.append(item)
        if len(line) == 2:
            pairs.append("   ".join(line))
            line = []
    if line:
        pairs.append("   ".join(line))
    head = L(lang, "📖 راهنمای دستورات این گروه", "📖 Commands of this group")
    return f"{head}\n\n" + "\n".join(pairs)


async def cmd_group_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in GROUP_TYPES:
        return
    await update.effective_message.reply_text(build_group_help_text(chat.id, lang_of(update)))


# ============================================================================
# پاک‌سازی خودکار / AUTO CLEANUP
# ============================================================================
SECONDS_PER_DAY = 86400


def _get_cleanup_cursor(chat_id):
    val = get_setting(f"cleanup_cursor_{chat_id}")
    return int(val) if val else 1


def _set_cleanup_cursor(chat_id, value):
    set_setting(f"cleanup_cursor_{chat_id}", str(value))


async def _sweep_oldest_messages(bot, chat_id, count):
    """از نشانگر فعلی شروع می‌کنه و count پیام به سمت جلو پاک می‌کنه"""
    cursor = _get_cleanup_cursor(chat_id)
    last_msg_id = get_last_message_id(chat_id)
    end = cursor + count - 1
    if last_msg_id:
        end = min(end, last_msg_id)
    deleted = 0
    if end >= cursor:
        for msg_id in range(cursor, end + 1):
            try:
                await bot.delete_message(chat_id, msg_id)
                deleted += 1
            except Exception:
                pass
        _set_cleanup_cursor(chat_id, end + 1)
    return deleted


def _get_settings_in_days(chat_id):
    enabled, interval_seconds, count, last_ts = get_cleanup_settings(chat_id)
    return enabled, max(1, round(interval_seconds / SECONDS_PER_DAY)), count, last_ts


def _days_word(n, lang):
    return L(lang, f"{n} روز", f"{n} day{'s' if n != 1 else ''}")


def _cln_panel_text(chat_id, lang):
    enabled, days, count, _ = _get_settings_in_days(chat_id)
    status = L(lang, "✅ فعال" if enabled else "❌ غیرفعال", "✅ On" if enabled else "❌ Off")
    return L(lang,
             f"🧹 پاک‌سازی خودکار گروه\n\nوضعیت: {status}\nبازه: هر {days} روز\nتعداد پیام هر بار: {count} تا\n\nبا دکمه‌های پایین تنظیم کن.",
             f"🧹 Group auto cleanup\n\nStatus: {status}\nInterval: every {_days_word(days, 'en')}\nMessages per run: {count}\n\nAdjust with the buttons below.")


def _cln_panel_keyboard(chat_id, lang):
    enabled, days, count, _ = _get_settings_in_days(chat_id)
    toggle = (InlineKeyboardButton(L(lang, "❌ خاموش کردن", "❌ Turn off"), callback_data=f"cln_toggle:{chat_id}:off") if enabled
              else InlineKeyboardButton(L(lang, "✅ روشن کردن", "✅ Turn on"), callback_data=f"cln_toggle:{chat_id}:on"))
    return InlineKeyboardMarkup([
        [toggle],
        [InlineKeyboardButton(L(lang, f"⏱ تنظیم بازه (فعلی: هر {days} روز)", f"⏱ Set interval (current: every {_days_word(days, 'en')})"),
                              callback_data=f"cln_interval:{chat_id}")],
        [InlineKeyboardButton(L(lang, f"🔢 تنظیم تعداد (فعلی: {count})", f"🔢 Set count (current: {count})"),
                              callback_data=f"cln_count_adjust:{chat_id}:open")],
        [InlineKeyboardButton(L(lang, "🧹 پاک‌سازی همین الان", "🧹 Clean up now"), callback_data=f"cln_run:{chat_id}")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"grp_open:{chat_id}")],
    ])


async def _render_cln_panel(query, chat_id, lang):
    await query.edit_message_text(_cln_panel_text(chat_id, lang), reply_markup=_cln_panel_keyboard(chat_id, lang))


async def _cln_guard(update, context, chat_id):
    if await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return True
    await update.callback_query.answer(_deny(lang_of(update)), show_alert=True)
    return False


async def open_cleanup_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    if not await _cln_guard(update, context, chat_id):
        return
    await _render_cln_panel(query, chat_id, lang_of(update))


async def toggle_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, action = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await _cln_guard(update, context, chat_id):
        return
    set_cleanup_settings(chat_id, enabled=(action == "on"), last_ts=time.time())
    await query.answer(L(lang, "✔ ذخیره شد", "✔ Saved"))
    await _render_cln_panel(query, chat_id, lang)


async def run_cleanup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _cln_guard(update, context, chat_id):
        return
    await query.answer(L(lang, "🧹 در حال پاک‌سازی...", "🧹 Cleaning up..."))
    _, _, count, _ = get_cleanup_settings(chat_id)
    deleted = await _sweep_oldest_messages(context.bot, chat_id, count)
    set_cleanup_settings(chat_id, last_ts=time.time())
    await _render_cln_panel(query, chat_id, lang)
    try:
        glang = get_lang(chat_id)
        notice = await context.bot.send_message(chat_id, L(glang, f"🧹 پاک‌سازی دستی انجام شد ({deleted} پیام).",
                                                           f"🧹 Manual cleanup done ({deleted} messages)."))
        context.job_queue.run_once(_delete_message_later, when=10,
                                   data={"chat_id": chat_id, "message_id": notice.message_id},
                                   name=f"delcln_{chat_id}_{notice.message_id}")
    except Exception:
        pass


def _interval_draft_text(draft_days, lang):
    return L(lang, f"⏱ تنظیم بازه‌ی پاک‌سازی\n\nمقدار در حال تنظیم: هر {draft_days} روز\n(هنوز ذخیره نشده)\n\nبا ➖/➕ عدد رو تغییر بده، بعد «✅ ذخیره» رو بزن.",
             f"⏱ Set cleanup interval\n\nValue being set: every {_days_word(draft_days, 'en')}\n(not saved yet)\n\nChange the number with ➖/➕, then press \"✅ Save\".")


def _draft_keyboard(prefix, chat_id, label, lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➖", callback_data=f"{prefix}:{chat_id}:dec"),
         InlineKeyboardButton(label, callback_data=f"{prefix}:{chat_id}:noop"),
         InlineKeyboardButton("➕", callback_data=f"{prefix}:{chat_id}:inc")],
        [InlineKeyboardButton(L(lang, "✅ ذخیره", "✅ Save"), callback_data=f"{prefix}:{chat_id}:save"),
         InlineKeyboardButton(L(lang, "❌ لغو", "❌ Cancel"), callback_data=f"{prefix}:{chat_id}:cancel")],
    ])


async def ask_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await _cln_guard(update, context, chat_id):
        return
    _, current_days, _, _ = _get_settings_in_days(chat_id)
    context.user_data[f"cln_days_draft_{chat_id}"] = current_days
    await query.edit_message_text(_interval_draft_text(current_days, lang),
                                  reply_markup=_draft_keyboard("cln_adjust", chat_id, _days_word(current_days, lang), lang))


async def adjust_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, action = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await _cln_guard(update, context, chat_id):
        return
    draft_key = f"cln_days_draft_{chat_id}"
    if action == "noop":
        await query.answer()
        return
    draft = context.user_data.get(draft_key)
    if draft is None:
        _, draft, _, _ = _get_settings_in_days(chat_id)

    if action in ("inc", "dec"):
        draft = min(draft + 1, 365) if action == "inc" else max(draft - 1, 1)
        context.user_data[draft_key] = draft
        await query.answer()
        await _safe_edit(query, _interval_draft_text(draft, lang),
                         _draft_keyboard("cln_adjust", chat_id, _days_word(draft, lang), lang))
    elif action == "save":
        set_cleanup_settings(chat_id, interval_seconds=draft * SECONDS_PER_DAY)
        context.user_data.pop(draft_key, None)
        await query.answer(L(lang, "✔ ذخیره شد", "✔ Saved"))
        await _render_cln_panel(query, chat_id, lang)
    elif action == "cancel":
        context.user_data.pop(draft_key, None)
        await query.answer(L(lang, "لغو شد", "Cancelled"))
        await _render_cln_panel(query, chat_id, lang)


def _count_draft_text(draft_count, lang):
    return L(lang, f"🔢 تنظیم تعداد پیام هر پاک‌سازی\n\nمقدار در حال تنظیم: {draft_count} پیام\n(هنوز ذخیره نشده)\n\nبا ➖/➕ عدد رو تغییر بده، بعد «✅ ذخیره» رو بزن.",
             f"🔢 Set messages per cleanup\n\nValue being set: {draft_count} messages\n(not saved yet)\n\nChange the number with ➖/➕, then press \"✅ Save\".")


async def adjust_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, action = query.data.split(":")
    chat_id = int(chat_id)
    lang = lang_of(update)
    if not await _cln_guard(update, context, chat_id):
        return
    draft_key = f"cln_count_draft_{chat_id}"
    label = lambda n: L(lang, f"{n} پیام", f"{n} msgs")

    if action == "open":
        await query.answer()
        _, _, current, _ = get_cleanup_settings(chat_id)
        context.user_data[draft_key] = current
        await query.edit_message_text(_count_draft_text(current, lang),
                                      reply_markup=_draft_keyboard("cln_count_adjust", chat_id, label(current), lang))
        return
    if action == "noop":
        await query.answer()
        return
    draft = context.user_data.get(draft_key)
    if draft is None:
        _, _, draft, _ = get_cleanup_settings(chat_id)

    if action in ("inc", "dec"):
        draft = min(draft + 5, 200) if action == "inc" else max(draft - 5, 1)
        context.user_data[draft_key] = draft
        await query.answer()
        await _safe_edit(query, _count_draft_text(draft, lang),
                         _draft_keyboard("cln_count_adjust", chat_id, label(draft), lang))
    elif action == "save":
        set_cleanup_settings(chat_id, count=draft)
        context.user_data.pop(draft_key, None)
        await query.answer(L(lang, "✔ ذخیره شد", "✔ Saved"))
        await _render_cln_panel(query, chat_id, lang)
    elif action == "cancel":
        context.user_data.pop(draft_key, None)
        await query.answer(L(lang, "لغو شد", "Cancelled"))
        await _render_cln_panel(query, chat_id, lang)


async def set_cleanup_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id, count = query.data.split(":")
    chat_id, count = int(chat_id), int(count)
    if not await _cln_guard(update, context, chat_id):
        return
    set_cleanup_settings(chat_id, count=count)
    lang = lang_of(update)
    await query.answer(L(lang, "✔ ذخیره شد", "✔ Saved"))
    await _render_cln_panel(query, chat_id, lang)


async def track_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in GROUP_TYPES:
        return
    update_last_message_id(chat.id, message.message_id)


async def run_auto_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """هر ساعت: گروه‌هایی که وقتشون رسیده رو پاک‌سازی می‌کنه"""
    now = time.time()
    for chat_id in get_all_cleanup_enabled_chats():
        enabled, interval_seconds, count, last_ts = get_cleanup_settings(chat_id)
        if not enabled or now - last_ts < interval_seconds:
            continue
        if not get_last_message_id(chat_id):
            set_cleanup_settings(chat_id, last_ts=now)
            continue
        deleted = await _sweep_oldest_messages(context.bot, chat_id, count)
        set_cleanup_settings(chat_id, last_ts=now)
        try:
            glang = get_lang(chat_id)
            notice = await context.bot.send_message(chat_id, L(glang, f"🧹 پاک‌سازی خودکار انجام شد ({deleted} پیام).",
                                                               f"🧹 Automatic cleanup done ({deleted} messages)."))
            context.job_queue.run_once(_delete_message_later, when=10,
                                       data={"chat_id": chat_id, "message_id": notice.message_id},
                                       name=f"delcln_{chat_id}_{notice.message_id}")
        except Exception:
            pass


# ============================================================================
# تگ / TAG ALL
# ============================================================================
async def track_seen_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or chat.type not in GROUP_TYPES or user.is_bot:
        return
    save_seen_user(chat.id, user.id, user.username, user.full_name)


async def tag_all_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کلمه‌ی «تگ» / tag: اعضایی که قبلاً پیام داده‌ن رو منشن می‌کنه (فقط مدیران)"""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or chat.type not in GROUP_TYPES:
        return
    text = (message.text or "").strip()
    if text != "تگ" and text.lower() != "tag":
        return
    lang = lang_of(update)

    if not await is_admin(context.bot, chat.id, user.id):
        await message.reply_text(L(lang, "⛔️ فقط مدیران گروه می‌توانند از این دستور استفاده کنند.",
                                   "⛔️ Only group admins can use this command."))
        return
    last_use = context.chat_data.get("tag_all_last_use", 0)
    if time.time() - last_use < 300:
        remaining = int(300 - (time.time() - last_use))
        await message.reply_text(L(lang, f"⏳ لطفاً {remaining} ثانیه صبر کنید.", f"⏳ Please wait {remaining} seconds."))
        return
    context.chat_data["tag_all_last_use"] = time.time()

    status_msg = await message.reply_text(L(lang, "🔄 در حال تگ کردن اعضا...", "🔄 Tagging members..."))
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        admin_ids = [a.user.id for a in admins]
        members = [{"id": r["user_id"], "username": r["username"], "full_name": r["full_name"]} for r in get_seen_users(chat.id)]
        if not members:
            await status_msg.edit_text(L(lang, "❌ هنوز کسی رو تو این گروه ثبت نکردم. اول باید چند نفر پیام بدن.",
                                         "❌ I haven't recorded anyone in this group yet. Some people need to send messages first."))
            return

        mentions = []
        for member in members:
            if member["id"] in admin_ids:
                continue
            if member["username"]:
                mentions.append(f"@{escape(member['username'])}")
            else:
                mentions.append(f'<a href="tg://user?id={member["id"]}">{escape(member["full_name"] or L(lang, "کاربر", "User"))}</a>')
        if not mentions:
            await status_msg.edit_text(L(lang, "❌ هیچ کاربری برای تگ کردن وجود ندارد.", "❌ There is nobody to tag."))
            return

        chunks = [mentions[i:i + 50] for i in range(0, len(mentions), 50)]
        await status_msg.delete()
        for i, chunk in enumerate(chunks):
            text_msg = L(lang, f"🔔 <b>تگ</b> (بخش {i + 1}/{len(chunks)})\n\n", f"🔔 <b>Tag</b> (part {i + 1}/{len(chunks)})\n\n") + " ".join(chunk)
            if i == 0:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "❌ بستن", "❌ Close"), callback_data=f"tag_close:{chat.id}")]])
                await message.reply_text(text_msg, parse_mode="HTML", reply_markup=kb)
            else:
                await message.reply_text(text_msg, parse_mode="HTML")
            await asyncio.sleep(1)
    except Exception as e:
        await status_msg.edit_text(L(lang, f"❌ خطا: {str(e)}", f"❌ Error: {str(e)}"))


async def tag_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


# ============================================================================
# دانلودر لینک‌ها (yt-dlp) / DOWNLOADER
# ============================================================================
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
EXCLUDED_DOMAINS = ("t.me", "telegram.me", "telegram.org", "telegram.dog")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# سقف دانلود خام قبل از فشرده‌سازی - فایل‌های تا این حجم دانلود می‌شن، بعد اگه
# از MAX_UPLOAD_BYTES بزرگ‌تر بودن با ffmpeg فشرده می‌شن تا به حد مجاز برسن.
DOWNLOAD_SIZE_CAP = 300 * 1024 * 1024
MAX_MEDIA_GROUP = 10
DOWNLOADER_WAIT_TIMEOUT = 180
TG_UP = dict(read_timeout=300, write_timeout=300, connect_timeout=60, pool_timeout=60)
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "opus", "ogg", "wav"}
COOKIES_PATH = os.path.join(BASE_DIR, "assets", "instagram_cookies.txt")

DL_ERRORS = {
    "no_ytdlp": ("کتابخونه‌ی yt-dlp روی سرور نصب نیست.", "The yt-dlp library is not installed on the server."),
    "failed": ("دانلود ناموفق بود.", "Download failed."),
    "too_big": ("حجم فایل بیشتر از حد مجاز ارسال ربات (۵۰ مگابایت) است.", "The file is larger than the bot's upload limit (50 MB)."),
    "private": ("این لینک خصوصیه و قابل دانلود نیست.", "This link is private and can't be downloaded."),
    "login": ("سایت فعلاً درخواست رو رد کرد (احتمالاً نیاز به کوکی لاگین‌شده داره).",
              "The site rejected the request for now (it may need a logged-in cookie)."),
    "unsupported": ("این سایت یا لینک پشتیبانی نمی‌شه.", "This site or link is not supported."),
    "generic": ("دانلود این لینک ناموفق بود.", "Downloading this link failed."),
}


def _is_excluded_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return any(host == d or host.endswith("." + d) for d in EXCLUDED_DOMAINS)
    except Exception:
        return False


def find_link(text: str):
    if not text:
        return None
    for m in URL_RE.finditer(text):
        if not _is_excluded_url(m.group(0)):
            return m.group(0)
    return None


def _video_back_keyboard(link_message_id: int, lang):
    return InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ بازگشت به پنل اصلی", "⬅️ Back to main panel"),
                                                       callback_data=f"dl_back:{link_message_id}")]])


def _is_photo_path(path: str) -> bool:
    return (path.rsplit(".", 1)[-1].lower() if "." in path else "") in PHOTO_EXTENSIONS


def _is_audio_path(path: str) -> bool:
    return (path.rsplit(".", 1)[-1].lower() if "." in path else "") in AUDIO_EXTENSIONS


def _download_blocking(url: str, out_dir: str):
    out_tmpl = os.path.join(out_dir, "item_%(playlist_index,autonumber)02d.%(ext)s")
    opts = {
        "outtmpl": out_tmpl, "quiet": True, "no_warnings": True, "noplaylist": False,
        "playlistend": MAX_MEDIA_GROUP, "format": "bv*+ba/b/bv*+ba[ext=m4a]/best",
        # سقف دانلود خامه، نه سقف نهایی ارسال؛ فایل‌های بین ۵۰ تا ۳۰۰ مگ دانلود
        # می‌شن و بعداً با ffmpeg فشرده می‌شن (پایین‌تر تو download_media)
        "format_sort": ["ext:mp4:m4a"], "max_filesize": DOWNLOAD_SIZE_CAP,
        "socket_timeout": 30, "retries": 2, "ignoreerrors": True,
    }
    if ("instagram.com" in url.lower() or "instagr.am" in url.lower()) and os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH

    paths = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        entries = info.get("entries") if isinstance(info, dict) else None
        if entries:
            for entry in entries:
                if not entry:
                    continue
                try:
                    fp = ydl.prepare_filename(entry)
                except Exception:
                    continue
                if os.path.exists(fp):
                    paths.append(fp)
        else:
            fp = ydl.prepare_filename(info)
            if os.path.exists(fp):
                paths.append(fp)
    if not paths:
        for name in sorted(os.listdir(out_dir)):
            paths.append(os.path.join(out_dir, name))
    return paths


def _cleanup_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _cleanup_files(paths):
    if not paths:
        return
    folder = os.path.dirname(paths[0])
    for fp in paths:
        _cleanup_file(fp)
    try:
        if folder and os.path.exists(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except Exception:
        pass


VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "flv", "m4v"}


def _is_video_path(path: str) -> bool:
    return (path.rsplit(".", 1)[-1].lower() if "." in path else "") in VIDEO_EXTENSIONS


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _get_duration_seconds(path: str):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _compress_video_blocking(path: str, target_bytes: int):
    """
    ویدیوی path رو با ffmpeg فشرده می‌کنه تا حجمش زیر target_bytes بشه.
    خروجی: مسیر فایل فشرده‌شده‌ی جدید، یا None اگه نشد (ffmpeg نیست، مدت‌زمان
    پیدا نشد، یا بیت‌ریت لازم خیلی پایین افتاد).
    """
    if not _ffmpeg_available():
        return None

    duration = _get_duration_seconds(path)
    if not duration or duration <= 0:
        return None

    audio_kbps = 96
    # ۸٪ حاشیه‌ی اطمینان برای سربار کانتینر/هدرها
    target_kbit_total = (target_bytes * 8 / 1000) * 0.92 / duration
    video_kbps = int(target_kbit_total - audio_kbps)
    if video_kbps < 120:
        # بیت‌ریتِ لازم برای رسیدن به این حجم خیلی پایینه - کیفیت عملاً تماشا نشدنی می‌شه
        return None

    out_path = f"{os.path.splitext(path)[0]}_compressed.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.2)}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=900)
        if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            _cleanup_file(out_path)
            return None
        return out_path
    except Exception:
        _cleanup_file(out_path)
        return None


async def _compress_if_needed(paths, limit_bytes):
    """هر فایل ویدیوییِ بزرگ‌تر از limit_bytes رو فشرده می‌کنه (اگه ممکن بود)"""
    result = []
    for fp in paths:
        if os.path.exists(fp) and os.path.getsize(fp) > limit_bytes and _is_video_path(fp):
            compressed = await asyncio.to_thread(_compress_video_blocking, fp, limit_bytes)
            if compressed and os.path.exists(compressed) and os.path.getsize(compressed) <= limit_bytes:
                _cleanup_file(fp)
                result.append(compressed)
                continue
            if compressed:
                _cleanup_file(compressed)
        result.append(fp)
    return result


async def download_media(url: str):
    """خروجی: (paths, error_code). error_code یکی از کلیدهای DL_ERRORS یا None"""
    if not HAS_YTDLP:
        return None, "no_ytdlp"
    tmp_dir = tempfile.mkdtemp(prefix="igdl_")
    try:
        paths = await asyncio.to_thread(_download_blocking, url, tmp_dir)
        if not paths:
            return None, "failed"

        # فایل‌های ویدیوییِ بزرگ‌تر از حد مجاز رو قبل از رد کردن، فشرده می‌کنیم
        paths = await _compress_if_needed(paths, MAX_UPLOAD_BYTES)

        oversized = [fp for fp in paths if os.path.exists(fp) and os.path.getsize(fp) > MAX_UPLOAD_BYTES]
        if oversized:
            for fp in oversized:
                _cleanup_file(fp)
            paths = [fp for fp in paths if fp not in oversized]
            if not paths:
                return None, "too_big"
        if len(paths) > MAX_MEDIA_GROUP:
            for extra in paths[MAX_MEDIA_GROUP:]:
                _cleanup_file(extra)
            paths = paths[:MAX_MEDIA_GROUP]
        return paths, None
    except Exception as e:
        msg = str(e).lower()
        try:
            for f in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        except Exception:
            pass
        if "private" in msg:
            return None, "private"
        if "login" in msg or "rate-limit" in msg or "429" in msg:
            return None, "login"
        if "unsupported url" in msg:
            return None, "unsupported"
        logger.warning(f"download error: {e}")
        return None, "generic"


def _dl_feature_enabled(chat) -> bool:
    if not chat or chat.type not in GROUP_TYPES:
        return True
    try:
        return is_feature_enabled(chat.id, "instagram_dl")
    except Exception:
        return True


async def _send_media(message, paths, caption, reply_markup, lang, has_spoiler=False):
    """همه‌ی پیام‌های فرستاده‌شده رو برمی‌گردونه (برای پاک‌سازی بعدی)"""
    if len(paths) == 1:
        path = paths[0]
        with open(path, "rb") as f:
            if _is_photo_path(path):
                sent = await message.reply_photo(photo=f, caption=caption, reply_markup=reply_markup, has_spoiler=has_spoiler, **TG_UP)
            elif _is_audio_path(path):
                sent = await message.reply_audio(audio=f, caption=caption, reply_markup=reply_markup, **TG_UP)
            else:
                sent = await message.reply_video(video=f, caption=caption, reply_markup=reply_markup, has_spoiler=has_spoiler, supports_streaming=True, **TG_UP)
        return [sent]

    files = [open(fp, "rb") for fp in paths]
    try:
        media = []
        for i, (path, f) in enumerate(zip(paths, files)):
            kwargs = {"caption": caption} if i == 0 else {}
            if _is_photo_path(path):
                media.append(InputMediaPhoto(f, has_spoiler=has_spoiler, **kwargs))
            elif _is_audio_path(path):
                media.append(InputMediaAudio(f, **kwargs))
            else:
                media.append(InputMediaVideo(f, has_spoiler=has_spoiler, **kwargs))
        sent_list = await message.reply_media_group(media=media, **TG_UP)
        all_sent = list(sent_list) if sent_list else []
        if reply_markup:
            all_sent.append(await message.reply_text(L(lang, "مدیریت این پست:", "Manage this post:"), reply_markup=reply_markup))
        return all_sent
    finally:
        for f in files:
            try:
                f.close()
            except Exception:
                pass


async def try_handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """تو گروه همیشه فعاله (اگه قابلیتش خاموش نشده)؛ تو پی‌وی فقط تو حالت downloader_mode"""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not message.text:
        return False
    url = find_link(message.text)
    if not url:
        return False

    lang = lang_of(update)
    is_private = not (chat and chat.type in GROUP_TYPES)
    if not is_private:
        if not _dl_feature_enabled(chat):
            return False
    else:
        if not context.user_data.get("downloader_mode"):
            return False
        await _delete_old_panel(context, chat.id)
        for mid in context.user_data.pop("dl_last_result_msgs", None) or []:
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=mid)
            except Exception:
                pass

    back_kb = _video_back_keyboard(message.message_id, lang) if is_private else None
    status = await message.reply_text(L(lang, "⏳ در حال دانلود...", "⏳ Downloading..."))
    paths, error = await download_media(url)

    result_ids = []
    if error:
        try:
            await status.edit_text(f"❌ {L(lang, *DL_ERRORS[error])}", reply_markup=back_kb)
            result_ids.append(status.message_id)
        except Exception:
            pass
    else:
        try:
            await status.delete()
        except Exception:
            pass
        try:
            sent_msgs = await _send_media(message, paths, L(lang, "📥 دانلود شد", "📥 Downloaded"), back_kb, lang)
            result_ids.extend(m.message_id for m in sent_msgs if m)
        except Exception as e:
            logger.exception("sending downloaded media failed")
            try:
                err_msg = await message.reply_text(
                    L(lang, "❌ مدیا دانلود شد ولی ارسالش ناموفق بود (احتمالاً حجم بالاست).",
                      "❌ The media was downloaded but sending failed (probably too large)."), reply_markup=back_kb)
                result_ids.append(err_msg.message_id)
            except Exception:
                pass
        finally:
            _cleanup_files(paths)

    if is_private and result_ids:
        context.user_data["dl_last_result_msgs"] = result_ids
    await _finish_wait_cleanup(update, context)
    return True


def _wait_store(context):
    return context.chat_data.setdefault("dl_wait", {})


async def _dl_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id, user_id, trigger_id, prompt_id = context.job.data
    wait = context.application.chat_data.get(chat_id, {}).get("dl_wait", {})
    entry = wait.get(user_id)
    if entry and entry.get("trigger_id") == trigger_id:
        wait.pop(user_id, None)
        for mid in (trigger_id, prompt_id):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass


async def cmd_downloader_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not _dl_feature_enabled(chat):
        return
    lang = lang_of(update)
    prompt = await message.reply_text(L(
        lang, "📥 لینک مورد نظر رو بفرست (یوتیوب، اینستاگرام، توییتر، تیک‌تاک و خیلی سایت‌های دیگه).",
        "📥 Send the link you want (YouTube, Instagram, Twitter, TikTok and many other sites)."))
    _wait_store(context)[user.id] = {"trigger_id": message.message_id, "prompt_id": prompt.message_id, "created_at": time.time()}
    if context.job_queue:
        context.job_queue.run_once(_dl_cleanup_job, DOWNLOADER_WAIT_TIMEOUT, chat_id=chat.id,
                                   data=(chat.id, user.id, message.message_id, prompt.message_id))


async def _finish_wait_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in GROUP_TYPES or not user:
        return
    entry = _wait_store(context).pop(user.id, None)
    if not entry:
        return
    for mid in (entry["trigger_id"], entry["prompt_id"]):
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=mid)
        except Exception:
            pass


def _downloader_panel_text(lang):
    return L(lang,
             "📥 دانلودر\n\nلینک مورد نظرت رو همین‌جا بفرست (یوتیوب، اینستاگرام، توییتر/X، تیک‌تاک، پینترست، ساندکلاود و خیلی "
             "سایت‌های دیگه)، خودم دانلودش می‌کنم و برات می‌فرستم (اگه پست چند عکس/ویدیو داشت، همه‌شون با هم فرستاده می‌شن).\n\n"
             "می‌تونی چند تا لینک پشت‌سرهم بفرستی. برای خروج از این بخش، دکمه‌ی زیر رو بزن.",
             "📥 Downloader\n\nSend your link right here (YouTube, Instagram, Twitter/X, TikTok, Pinterest, SoundCloud and many "
             "other sites) and I'll download it and send it to you (if a post has several photos/videos, they are all sent together).\n\n"
             "You can send several links in a row. To leave this section, press the button below.")


def _downloader_keyboard(lang):
    return InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ بازگشت به پنل اصلی", "⬅️ Back to main panel"),
                                                       callback_data="start_menu")]])


async def open_downloader_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = lang_of(update)
    context.user_data["downloader_mode"] = True
    await query.edit_message_text(_downloader_panel_text(lang), reply_markup=_downloader_keyboard(lang))
    _remember_panel(query.message.chat_id, query.message.message_id)


async def private_downloader_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    lang = lang_of(update)
    context.user_data["downloader_mode"] = True
    await _delete_old_panel(context, chat.id)
    sent = await update.effective_message.reply_text(_downloader_panel_text(lang), reply_markup=_downloader_keyboard(lang))
    _remember_panel(chat.id, sent.message_id)


async def handle_downloader_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    lang = get_lang(chat_id)
    try:
        link_message_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        link_message_id = None
    try:
        await query.message.delete()
    except Exception:
        pass
    if link_message_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=link_message_id)
        except Exception:
            pass
    context.user_data["downloader_mode"] = False
    await _delete_old_panel(context, chat_id)
    bot_username = (await context.bot.get_me()).username
    sent = await context.bot.send_message(
        chat_id, start_text(lang, query.from_user.first_name),
        reply_markup=build_start_keyboard(query.from_user.id, bot_username, lang))
    _remember_panel(chat_id, sent.message_id)


# ============================================================================
# هوش مصنوعی / AI
# ============================================================================
DEFAULT_AI_TRIGGER = "/Bot"


def get_ai_trigger(chat_id):
    return get_setting(f"ai_trigger_{chat_id}", DEFAULT_AI_TRIGGER)


def set_ai_trigger(chat_id, trigger):
    set_setting(f"ai_trigger_{chat_id}", trigger)


GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
gemini_model = None
if genai and GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-3.6-flash")
else:
    print("⚠️ GOOGLE_API_KEY is not set / تنظیم نشده!")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


def _is_quota_error(e: Exception) -> bool:
    err_str = str(e)
    type_name = type(e).__name__
    return ("429" in err_str or "quota" in err_str.lower()
            or "ResourceExhausted" in type_name or "RateLimitError" in type_name)


def _ai_blocking(text, model_type):
    if model_type == "gemini" and gemini_model:
        return gemini_model.generate_content(text).text[:4000]
    if model_type == "chatgpt" and OPENAI_API_KEY:
        import openai
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": text}],
                                                  max_tokens=1000)
        return response.choices[0].message.content[:4000]
    return None


async def get_ai_response(text, model_type="gemini", lang="fa"):
    prompt = text + L(lang, "\n\n(لطفاً به فارسی پاسخ بده.)", "\n\n(Please answer in English.)")
    try:
        result = await asyncio.to_thread(_ai_blocking, prompt, model_type)
        if result is None:
            return L(lang, "❌ مدل انتخاب شده در دسترس نیست. لطفاً مدل دیگری را انتخاب کنید.",
                     "❌ The selected model is not available. Please choose another model.")
        return result
    except Exception as e:
        if _is_quota_error(e):
            return L(lang, "⚠️ شما پیام‌های چت امروزتون رو تموم کردین.\nفردا دوباره می‌تونید از هوش مصنوعی استفاده کنید.",
                     "⚠️ You've used up today's chat messages.\nYou can use the AI again tomorrow.")
        return L(lang, f"❌ خطا: {str(e)}", f"❌ Error: {str(e)}")


async def ai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پاسخ در گروه (با کلمه‌ی فعال‌ساز، پیش‌فرض /Bot)"""
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    if not is_feature_enabled(chat.id, "ai_chat"):
        return
    text = update.message.text
    bot_username = context.bot.username
    trigger = get_ai_trigger(chat.id)
    if trigger not in text and f"/{bot_username}" not in text:
        return

    lang = lang_of(update)
    question = re.sub(re.escape(trigger) + r"\s*", "", text)
    question = re.sub(f"/{re.escape(bot_username)}" + r"\s*", "", question).strip()
    if not question:
        await update.message.reply_text(L(lang, "🧐 چی بپرسم؟", "🧐 What should I ask?"))
        return

    model_type = context.user_data.get("ai_model", "gemini")
    thinking = await update.message.reply_text(L(lang, "🤔 دارم فکر می‌کنم...", "🤔 Thinking..."))
    reply = await get_ai_response(question, model_type, lang)
    await thinking.delete()
    await update.message.reply_text(reply)


async def ai_private_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پاسخ در پی‌وی (بدون تگ)"""
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    lang = lang_of(update)
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(L(lang, "🧐 چی بپرسم؟", "🧐 What should I ask?"))
        return

    model_type = context.user_data.get("ai_model", "gemini")
    thinking = await update.message.reply_text(L(lang, "🤔 دارم فکر می‌کنم...", "🤔 Thinking..."))
    reply = await get_ai_response(text, model_type, lang)
    await thinking.delete()

    await _delete_old_panel(context, chat.id)
    sent = await update.message.reply_text(reply, reply_markup=_back_kb(lang, "start_menu"))
    _remember_panel(chat.id, sent.message_id)
    try:
        await update.message.delete()
    except Exception:
        pass


def _ai_trigger_panel_text(chat_id, lang, extra_line=None):
    text = L(lang,
             f"🧠 کلمه‌ی فعال‌ساز هوش مصنوعی تو گروه\n\nفعلی: «{get_ai_trigger(chat_id)}»\n\n"
             "برای صحبت با هوش مصنوعی تو گروه، باید این کلمه (یا منشن مستقیم ربات) اول پیام باشه.",
             f"🧠 AI trigger word in the group\n\nCurrent: \"{get_ai_trigger(chat_id)}\"\n\n"
             "To talk to the AI in the group, this word (or a direct mention of the bot) must be in the message.")
    if extra_line:
        text = f"{extra_line}\n\n{text}"
    return text


def _ai_trigger_panel_keyboard(chat_id, lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(L(lang, "✏️ تغییر کلمه‌ی فعال‌ساز", "✏️ Change trigger"), callback_data=f"ai_trigger_set:{chat_id}")],
        [InlineKeyboardButton(L(lang, "⬅️ بازگشت", "⬅️ Back"), callback_data=f"cmdshortcuts_panel:{chat_id}")],
    ])


async def open_ai_trigger_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    await query.edit_message_text(_ai_trigger_panel_text(chat_id, lang), reply_markup=_ai_trigger_panel_keyboard(chat_id, lang))


async def ask_set_ai_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split(":")[1])
    lang = lang_of(update)
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        await query.answer(_deny(lang), show_alert=True)
        return
    await query.answer()
    context.user_data["waiting_ai_trigger_chat_id"] = chat_id
    context.user_data["ai_trigger_prompt_chat_id"] = query.message.chat_id
    context.user_data["ai_trigger_prompt_message_id"] = query.message.message_id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(L(lang, "⬅️ انصراف", "⬅️ Cancel"), callback_data=f"ai_trigger_panel:{chat_id}")]])
    cur = get_ai_trigger(chat_id)
    await query.edit_message_text(
        L(lang, f"✏️ کلمه‌ی جدید برای فعال‌سازی هوش مصنوعی تو گروه رو بفرست.\n\nفعلی: «{cur}»\n"
                "مثلاً: /Bot یا هر کلمه‌ی دیگه‌ای که دوست داری.\n\nبرای لغو، دستور /cancel را بفرستید.",
          f"✏️ Send the new trigger word for the AI in the group.\n\nCurrent: \"{cur}\"\n"
          "For example: /Bot or any other word you like.\n\nTo cancel, send /cancel."),
        reply_markup=kb)


async def receive_ai_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = context.user_data.get("waiting_ai_trigger_chat_id")
    if not chat_id:
        return False
    if not await can_access_dm_panel(context.bot, chat_id, update.effective_user.id):
        return False
    lang = lang_of(update)
    context.user_data["waiting_ai_trigger_chat_id"] = None
    prompt_chat_id = context.user_data.pop("ai_trigger_prompt_chat_id", None)
    prompt_message_id = context.user_data.pop("ai_trigger_prompt_message_id", None)
    new_trigger = (update.effective_message.text or "").strip()
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    if new_trigger == "/cancel":
        confirm = L(lang, "❌ لغو شد.", "❌ Cancelled.")
    elif not new_trigger:
        confirm = L(lang, "❗️ چیزی دریافت نشد، تغییری اعمال نشد.", "❗️ Nothing received, no change was made.")
    else:
        set_ai_trigger(chat_id, new_trigger)
        confirm = L(lang, f"✔ کلمه‌ی فعال‌ساز هوش مصنوعی به «{new_trigger}» تغییر کرد.",
                    f"✔ The AI trigger was changed to \"{new_trigger}\".")
    text = _ai_trigger_panel_text(chat_id, lang, extra_line=confirm)
    kb = _ai_trigger_panel_keyboard(chat_id, lang)
    if prompt_chat_id and prompt_message_id:
        try:
            await context.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id, text=text, reply_markup=kb)
            return True
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=kb)
    return True


# ============================================================================
# هندلر اصلی گروه و اجرای ربات / MAIN
# ============================================================================
# کلید اصلی (فارسی) -> تابع. کلیدها فارسی می‌مونن چون تو دیتابیس (میان‌برهای گروه‌ها) ذخیره شدن.
PERSIAN_COMMANDS = {
    "خاموشی": cmd_khamoshi, "روشن": cmd_roshan, "سکوت": cmd_sokoot, "آزاد کن": cmd_azad_kon,
    "بن کن": cmd_ban_kon, "اخطار": cmd_akhtar, "پاک": cmd_pak, "گیف بن": cmd_gif_ban,
    "استیکر بن": cmd_sticker_ban, "تاس": cmd_tas, "شیر یا خط": cmd_shir_khat,
    "سنگ کاغذ قیچی": cmd_sang_kaghaz_gheychi, "گزارش": cmd_gozaresh, "ترجمه": cmd_tarjome, "تاریخ": cmd_tarikh,
}
# فقط این دستورهای انگلیسی بعدشون آرگومان می‌گیرن؛ بقیه فقط وقتی کل پیام همون کلمه باشه
# (تا جمله‌های معمولی مثل «date night» یا «report this» اشتباهی دستور حساب نشن)
EN_ARG_COMMANDS = {"خاموشی", "سکوت", "بن کن", "اخطار", "پاک"}
GAME_COMMANDS = {"تاس", "شیر یا خط", "سنگ کاغذ قیچی"}


async def on_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in GROUP_TYPES:
        return

    text = (message.text or "").strip()
    low = text.lower()

    if text == "راهنما" or low == "help":
        await cmd_group_help(update, context)
        return
    if text == "دوز" or low == "xo":
        if is_feature_enabled(chat.id, "games"):
            await cmd_dooz(update, context)
        return
    if text == "دانلودر" or low == "downloader":
        await cmd_downloader_trigger(update, context)
        return
    if await try_handle_link(update, context):
        return

    # نگاشت کلمه‌ی فعلی (سفارشی یا پیش‌فرض) + معادل‌های انگلیسی -> کلید اصلی
    keyword_map = dict(get_group_command_keywords(chat.id))
    for en_kw, key in ENGLISH_COMMANDS.items():
        keyword_map.setdefault(en_kw, key)

    for kw in sorted(keyword_map.keys(), key=len, reverse=True):
        if not kw:
            continue
        original_key = keyword_map[kw]
        is_ascii = kw.isascii()
        cmp_text = low if is_ascii else text
        cmp_kw = kw.lower() if is_ascii else kw
        if not (cmp_text == cmp_kw or cmp_text.startswith(cmp_kw + " ")):
            continue

        rest = text[len(kw):].strip()
        if ENGLISH_COMMANDS.get(kw) == original_key and rest:
            if original_key not in EN_ARG_COMMANDS:
                continue
            if original_key != "خاموشی" and not message.reply_to_message:
                continue

        if original_key in GAME_COMMANDS and not is_feature_enabled(chat.id, "games"):
            return
        context.args = rest.split() if rest else []
        await PERSIAN_COMMANDS[original_key](update, context)
        return

    if low in PRICE_LOOKUP_NAMES:
        if PRICE_ALIASES.get(low, text) == "دلار" and not is_feature_enabled(chat.id, "dollar"):
            return
        await cmd_crypto_single(update, context)
        return

    if await check_dot_translate(update, context):
        return
    if is_feature_enabled(chat.id, "bad_words"):
        await check_message_for_bad_words(update, context)


async def _build_join_message(bot, chat, lang):
    """پیام هنگام نصب ربات تو گروه: مالک، ادمین‌ها، وضعیت ادمین بودن ربات و قابلیت‌ها"""
    try:
        admins = await bot.get_chat_administrators(chat.id)
    except Exception:
        admins = []
    me = await bot.get_me()

    owner = next((a.user for a in admins if a.status == "creator"), None)
    if owner:
        owner_name = f"@{owner.username}" if owner.username else owner.full_name
    else:
        owner_name = L(lang, "نامشخص", "Unknown")
    other_admins = [a.user for a in admins if a.status == "administrator" and not a.user.is_bot]
    admin_names = [f"• {f'@{u.username}' if u.username else u.full_name}" for u in other_admins]

    bot_member = next((a for a in admins if a.user.id == me.id), None)
    is_full_admin = bool(bot_member and bot_member.status == "administrator"
                         and getattr(bot_member, "can_delete_messages", False)
                         and getattr(bot_member, "can_restrict_members", False))

    lines = [L(lang, "📗 ربات با موفقیت در این گروه نصب شد.", "📗 The bot was installed in this group successfully."),
             "", L(lang, "➕ مالک گروه:", "➕ Group owner:"), f"▸ {owner_name}", ""]
    if admin_names:
        lines.append(L(lang, "👮 ادمین‌های گروه:", "👮 Group admins:"))
        lines.extend(admin_names)
        lines.append("")
    if is_full_admin:
        lines.append(L(lang, "✔ ربات ادمین کامل این گروه است.", "✔ The bot is a full admin of this group."))
    else:
        lines.append(L(lang,
                       "⚠️ ربات هنوز ادمین کامل نیست.\nبرای فعال شدن همه‌ی قابلیت‌ها، لطفاً از تنظیمات گروه، دسترسی «حذف پیام» و «محدود کردن اعضا» رو به ربات بده.",
                       "⚠️ The bot is not a full admin yet.\nTo enable all features, please give it the \"Delete messages\" and \"Restrict members\" permissions in the group settings."))
    lines.append("")
    lines.append(L(lang, "🛠 وضعیت پیش‌فرض قابلیت‌ها:", "🛠 Default feature status:"))
    lines.append("")
    for key in list(TOGGLEABLE_FEATURES.keys()) + ["welcome", "translate", "ai_chat"]:
        lines.append(f"{'✔' if is_feature_enabled(chat.id, key) else '✘'} {feature_label(lang, key)}")
    lines.append("")
    lines.append(L(lang,
                   "📚 برای مدیریت کامل این گروه (تنظیمات، اخطارها، پاک‌سازی خودکار و موارد دیگر)، به پی‌وی ربات مراجعه کن و از پنل مدیریت استفاده کن.\n🌐 برای تغییر زبان گروه: /language",
                   "📚 To fully manage this group (settings, warnings, auto cleanup and more), open the bot's private chat and use the admin panel.\n🌐 To change the group language: /language"))
    return "\n".join(lines)


def _adder_name(adder):
    if not adder:
        return None
    return f"@{adder.username}" if adder.username else adder.full_name


async def on_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.new_chat_members:
        return
    me = await context.bot.get_me()
    if me.id not in [m.id for m in message.new_chat_members]:
        return
    chat = update.effective_chat
    adder = message.from_user
    upsert_group(chat.id, chat.title, added_by_user_id=adder.id if adder else None,
                 added_by_username=_adder_name(adder))
    # پیام نصب فقط تو on_bot_added_to_group فرستاده می‌شه تا تکراری نشه


async def on_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result: ChatMemberUpdated = update.my_chat_member
    if not result:
        return
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    chat = result.chat
    if chat.type not in GROUP_TYPES:
        return
    just_joined = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    if new_status in ("member", "administrator"):
        adder = result.from_user
        upsert_group(chat.id, chat.title, added_by_user_id=adder.id if adder else None,
                     added_by_username=_adder_name(adder))
        if just_joined:
            # اگه گروه هنوز زبانی نداره و اضافه‌کننده تو پی‌وی زبان انتخاب کرده، همون رو برای گروه بذار
            if not has_lang(chat.id) and adder and has_lang(adder.id):
                set_lang(chat.id, get_lang(adder.id))
            try:
                await context.bot.send_message(chat.id, await _build_join_message(context.bot, chat, get_lang(chat.id)))
            except Exception:
                pass


async def global_shutdown_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_global_active():
        return
    user = update.effective_user
    if user and user.id == CREATOR_ID:
        return
    chat = update.effective_chat
    if chat and chat.type == "private":
        try:
            await update.effective_message.reply_text(shutdown_text(get_lang(chat.id)))
        except Exception:
            pass
    raise ApplicationHandlerStop


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("✘ لطفاً BOT_TOKEN رو توی فایل .env یا متغیر محیطی ست کنید. / Please set BOT_TOKEN in .env or the environment.")
    if not CREATOR_ID:
        logger.warning("CREATOR_ID is not set — the creator panel will not work.")

    init_db()
    app: Application = (ApplicationBuilder().token(BOT_TOKEN)
                        .connect_timeout(30).read_timeout(60).write_timeout(120).build())

    async def guarded_group_text(update, context):
        if not is_global_active() and update.effective_user.id != CREATOR_ID:
            return
        await on_group_text(update, context)

    async def guarded_private_text(update, context):
        chat = update.effective_chat
        user = update.effective_user

        if not is_global_active() and user.id != CREATOR_ID:
            if chat.type == "private":
                await update.effective_message.reply_text(shutdown_text(get_lang(chat.id)))
            return

        for receiver in (receive_shutdown_text, receive_update_msg, receive_welcome_text, receive_warn_text,
                         receive_bad_word_text, receive_translate_trigger, receive_ai_trigger,
                         receive_command_alias, receive_support_message, send_support_reply,
                         extras_handle_text):
            if await receiver(update, context):
                return

        text = (update.effective_message.text or "").strip()
        if text.lower() in PRICE_LOOKUP_NAMES:
            await cmd_crypto_single(update, context)
            return

        # دانلودر: فقط تو حالت downloader_mode (دکمه‌ی دانلودر یا تایپ «دانلودر» / downloader)
        if await try_handle_link(update, context):
            return
        if text == "دانلودر" or text.lower() == "downloader":
            await private_downloader_command(update, context)
            return

        # فقط اگه کاربر صراحتاً یه مدل هوش مصنوعی رو انتخاب کرده باشه
        if context.user_data.get("ai_chat_enabled"):
            await ai_private_chat(update, context)
            return

        # هیچ‌کدوم از قابلیت‌های ربات مرتبط نبود -> پیام رو پاک کن
        try:
            await update.effective_message.delete()
        except Exception:
            pass

    async def private_media_router(update, context):
        for receiver in (receive_welcome_media, receive_warn_media, receive_support_message):
            if await receiver(update, context):
                return
        try:
            await update.effective_message.delete()
        except Exception:
            pass

    # ===== گیت خاموشی و ردیابی منو =====
    app.add_handler(TypeHandler(Update, global_shutdown_gate), group=-1)
    app.add_handler(CallbackQueryHandler(track_nav_state), group=-1)

    # ===== هوش مصنوعی (گروه) =====
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, ai_handler), group=0)

    # ===== دستورات =====
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("language", cmd_language))

    # ===== پی‌وی =====
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Regex("^(◀️ صفحه قبل|◀️ Back)$"), handle_back_step))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, guarded_private_text))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.Sticker.ALL | filters.ANIMATION | filters.PHOTO), private_media_router))

    # ===== پیام‌های گروه =====
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, guarded_group_text), group=2)

    # ===== تگ (گروه‌های جدا، چون تو هر گروه فقط اولین handler مچ‌شده اجرا می‌شه) =====
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, track_seen_user), group=6)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, tag_all_members), group=7)
    app.add_handler(CallbackQueryHandler(tag_close, pattern="^tag_close:"))
    app.add_handler(CallbackQueryHandler(rps_pick, pattern="^rps_pick:"))
    app.add_handler(CallbackQueryHandler(dooz_level, pattern="^dooz_lvl:"))
    app.add_handler(CallbackQueryHandler(dooz_move, pattern="^dooz:"))
    app.add_handler(CallbackQueryHandler(open_downloader_panel, pattern="^dl_panel_open$"))
    app.add_handler(CallbackQueryHandler(handle_downloader_back, pattern=r"^dl_back:"))

    # ===== زبان =====
    app.add_handler(CallbackQueryHandler(lang_open, pattern=r"^lang_open:"))
    app.add_handler(CallbackQueryHandler(lang_set, pattern=r"^lang_set:"))

    # ===== لیست سیاه و مدیا =====
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.ANIMATION | filters.Sticker.ALL),
                                   check_blacklisted_media))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION | filters.Sticker.ALL),
        check_media_permissions), group=3)

    # ===== عضویت در گروه =====
    app.add_handler(ChatMemberHandler(on_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member_welcome), group=4)

    # ===== دکمه‌های شیشه‌ای =====
    cb = lambda fn, pattern: app.add_handler(CallbackQueryHandler(fn, pattern=pattern))
    cb(on_help_button, "^help_commands$")
    cb(on_start_menu_button, "^start_menu$")
    cb(show_my_groups, "^panel_my_groups$")
    cb(open_group_panel, r"^grp_open:")
    cb(toggle_lock, r"^grp_(lock|unlock):")
    cb(toggle_active, r"^grp_active_(on|off):")
    cb(show_banned_list, r"^grp_banned:")
    cb(show_muted_list, r"^grp_muted:")
    cb(show_mute_detail, r"^mute_user:")
    cb(release_mute_from_panel, r"^mute_release:")
    cb(ask_edit_mute_duration, r"^mute_edit:")
    cb(set_mute_duration_from_panel, r"^mute_setdur:")
    cb(show_features_panel, r"^grp_features:")
    cb(toggle_feature, r"^feat_toggle:")
    cb(open_welcome_panel, r"^wc_panel:")
    cb(toggle_welcome, r"^wc_(on|off):")
    cb(ask_edit_welcome, r"^wc_edit:")
    cb(preview_welcome, r"^wc_preview:")
    cb(reset_welcome, r"^wc_reset:")
    cb(show_reports_list, r"^grp_reports:")
    cb(clear_reports_cb, r"^reports_clear:")
    cb(open_report_detail, r"^report_open:")
    cb(handle_report_action, r"^report_act:")
    cb(open_warnedit_panel, r"^warnedit_panel:")
    cb(open_level_panel, r"^warnedit_lvl:")
    cb(ask_warn_text, r"^warnedit_text:")
    cb(ask_warn_media, r"^warnedit_media:")
    cb(reset_warn, r"^warnedit_reset:")
    cb(open_translate_panel, r"^tr_panel:")
    cb(set_translate_lang_cb, r"^tr_set:")
    cb(ask_set_translate_trigger, r"^tr_trigger:")
    cb(open_ai_trigger_panel, r"^ai_trigger_panel:")
    cb(ask_set_ai_trigger, r"^ai_trigger_set:")
    cb(open_shortcuts_panel, r"^cmdshortcuts_panel:")
    cb(ask_edit_command_alias, r"^cmdalias_edit:")
    cb(reset_command_alias_cb, r"^cmdalias_reset:")
    cb(reset_all_command_aliases_cb, r"^cmdalias_resetall:")

    # ===== پاک‌سازی خودکار =====
    cb(open_cleanup_panel, r"^cln_panel:")
    cb(toggle_cleanup, r"^cln_toggle:")
    cb(ask_interval, r"^cln_interval:")
    cb(adjust_interval, r"^cln_adjust:")
    cb(set_cleanup_count, r"^cln_count:")
    cb(adjust_count, r"^cln_count_adjust:")
    cb(run_cleanup_now, r"^cln_run:")

    cb(open_image_lang_panel, r"^imglang_panel:")
    cb(set_image_lang_cb, r"^imglang_set:")
    cb(ask_add_welcome_media, r"^wc_media:")
    cb(clear_welcome_media_cb, r"^wc_media_clear:")
    cb(show_warned_list, r"^grp_warned:")
    cb(open_bad_words_panel, r"^badwords_panel:")
    cb(ask_add_bad_word, r"^badwords_add:")
    cb(delete_bad_word_cb, r"^badwords_del:")

    # ===== پنل سازنده =====
    cb(open_creator_panel, "^creator_panel_open$")
    cb(toggle_global, "^creator_global_(on|off)$")
    cb(ask_set_shutdown_text, "^creator_set_msg$")
    cb(ask_set_update_msg, "^creator_set_update_msg$")
    cb(show_update_msg, "^creator_show_update_msg$")

    # ===== هوش مصنوعی و ری‌استارت =====
    cb(ai_model_select, "^ai_model_select$")
    cb(ai_use_gemini, "^ai_use_gemini$")
    cb(ai_use_chatgpt, "^ai_use_chatgpt$")
    cb(restart_bot, "^restart_bot$")

    # ===== پشتیبانی =====
    cb(support_menu, "^support_menu$")
    cb(confirm_support, "^support_confirm:")
    cb(cancel_support, "^support_cancel$")
    cb(support_admin_panel, "^support_admin$")
    cb(show_support_message, "^support_show:")
    cb(support_reply, "^support_reply:")
    cb(support_delete, "^support_delete:")

    # ===== Jobها =====
    app.job_queue.run_repeating(send_pending_reports_job, interval=120, first=120)
    app.job_queue.run_repeating(run_auto_cleanup_job, interval=3600, first=300)

    # ===== آمار گروه، تبدیل ارز و نمودار =====
    register_extras(app)

    # ===== ثبت آخرین آیدی پیام =====
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.ALL, track_last_message), group=5)

    logger.info("🤖 ربات در حال اجراست... / Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
