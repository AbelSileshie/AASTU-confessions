"""Shared helpers for modules: user rows, notifications, keyboards, parsing.

Modules may import this (it is part of the shared layer with db/security/
textfilter/config/i18n). Modules never import each other.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

import db
from config import cfg
from i18n import t
from security import author_hash, decrypt_ref, encrypt_ref, is_private_chat, sign_cb  # noqa: F401 - re-exported

log = logging.getLogger(__name__)

PERMANENT_BAN = -1


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def ensure_user(telegram_id: int) -> str:
    """Upsert the anonymous user row; returns the author hash."""
    author = author_hash(telegram_id)
    ref = encrypt_ref(telegram_id) if cfg("notify_authors", True) else None
    db.execute(
        "INSERT INTO users(author, chat_ref, created) VALUES (?,?,?) "
        "ON CONFLICT(author) DO UPDATE SET chat_ref=COALESCE(excluded.chat_ref, users.chat_ref)",
        (author, ref, db.now()),
    )
    return author


def user_lang(author: str) -> str:
    return db.scalar("SELECT lang FROM users WHERE author=?", (author,), "en") or "en"


def lang_of(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "en"
    return user_lang(author_hash(user.id))


def is_banned(author: str) -> bool:
    until = db.scalar("SELECT banned_until FROM users WHERE author=?", (author,), 0) or 0
    return until == PERMANENT_BAN or until > db.now()


async def notify_author(context: ContextTypes.DEFAULT_TYPE, author: str, text: str, **kw) -> bool:
    """DM a user via the encrypted chat_ref. Returns False if impossible."""
    if not cfg("notify_authors", True):
        return False
    blob = db.scalar("SELECT chat_ref FROM users WHERE author=?", (author,))
    chat_id = decrypt_ref(blob)
    if chat_id is None:
        return False
    try:
        await context.bot.send_message(chat_id, text, **kw)
        return True
    except Forbidden:
        db.execute("UPDATE users SET chat_ref=NULL WHERE author=?", (author,))
        return False
    except TelegramError as exc:
        log.warning("notify failed: %s", type(exc).__name__)
        return False


async def alert_admins(context: ContextTypes.DEFAULT_TYPE, text: str, **kw) -> None:
    group = context.bot_data.get("env").admin_group_id
    if not group:
        return
    try:
        await context.bot.send_message(group, text, **kw)
    except TelegramError as exc:
        log.warning("admin alert failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb(rows: Iterable[Sequence[tuple[str, str] | tuple[str, str, str]]]) -> InlineKeyboardMarkup:
    """rows of (label, action, payload) → signed callback buttons.
    A 2-tuple (label, url) with an https url becomes a URL button."""
    out = []
    for row in rows:
        btns = []
        for item in row:
            if len(item) == 2 and item[1].startswith("https://"):
                btns.append(InlineKeyboardButton(item[0], url=item[1]))
            else:
                label, action = item[0], item[1]
                payload = item[2] if len(item) > 2 else ""
                btns.append(InlineKeyboardButton(label, callback_data=sign_cb(action, payload)))
        out.append(btns)
    return InlineKeyboardMarkup(out)


def main_menu(lang: str, show_ads: bool = True) -> InlineKeyboardMarkup:
    rows = [[(t(lang, "menu.confess"), "menu", "confess"), (t(lang, "menu.browse"), "menu", "browse")]]
    second = [(t(lang, "menu.rules"), "menu", "rules"), (t(lang, "menu.lang"), "menu", "lang")]
    if show_ads and cfg("ads_enabled", True):
        second.insert(0, (t(lang, "menu.advertise"), "menu", "advertise"))
    rows.append(second)
    return kb(rows)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def post_link(context: ContextTypes.DEFAULT_TYPE, channel_msg_id: int | None) -> str:
    env = context.bot_data.get("env")
    username = context.bot_data.get("channel_username")
    if username and channel_msg_id:
        return f"https://t.me/{username}/{channel_msg_id}"
    if env and channel_msg_id:
        internal = str(env.channel_id).replace("-100", "", 1)
        return f"https://t.me/c/{internal}/{channel_msg_id}"
    return ""


def bot_link(context: ContextTypes.DEFAULT_TYPE, start: str) -> str:
    username = context.bot_data.get("bot_username", "")
    return f"https://t.me/{username}?start={start}"


def parse_int(s: str | None) -> int | None:
    try:
        return int(str(s).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def category(cat_id: int | None) -> dict:
    row = db.one("SELECT * FROM categories WHERE id=?", (cat_id,)) if cat_id else None
    return dict(row) if row else {"id": None, "label": "Other", "hashtag": "#Other", "slug": "other"}


def active_banner() -> str | None:
    row = db.one("SELECT text FROM banners WHERE starts<=? AND ends>? ORDER BY id DESC LIMIT 1", (db.now(), db.now()))
    return row["text"] if row else None
