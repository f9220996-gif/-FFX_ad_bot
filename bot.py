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
import sqlite3
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
