"""Confession submission: category → text → preview → submit."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from common import active_banner, category, ensure_user, is_banned, is_private_chat, kb, lang_of, main_menu
from config import cfg
from i18n import t
from security import verify_cb
from textfilter import check_text, strip_invisible, text_hash

CATEGORY, TEXT, PREVIEW = range(3)


# ---------------------------------------------------------------------------
# Pure-ish gate checks (return i18n key + kwargs, or None when allowed)
# ---------------------------------------------------------------------------

def _hours_open(spec: str, hour: int) -> bool:
    if not spec:
        return True
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        return True
    return start <= hour < end if start <= end else hour >= start or hour < end


def gate(author: str, now: int) -> tuple[str, dict] | None:
    """Pre-text checks in the documented order. Pure given DB state."""
    if is_banned(author):
        return "submit.banned", {}
    user = db.one("SELECT last_submit, submits_today, submits_day FROM users WHERE author=?", (author,))
    cooldown = cfg("submit_cooldown_sec", 600)
    if user and now - (user["last_submit"] or 0) < cooldown:
        left = cooldown - (now - user["last_submit"])
        return "submit.cooldown", {"minutes": max(1, left // 60)}
    cap = cfg("submit_daily_cap", 0)
    today = now // 86400
    if cap and user and user["submits_day"] == today and (user["submits_today"] or 0) >= cap:
        return "submit.daily_cap", {"cap": cap}
    if not cfg("submissions_open", True):
        return "submit.closed", {}
    hours = cfg("open_hours", "")
    if not _hours_open(hours, datetime.fromtimestamp(now, tz=timezone.utc).hour):
        return "submit.closed_hours", {"hours": hours}
    soft_cap = cfg("queue_soft_cap", 0)
    if soft_cap and db.scalar("SELECT count(*) FROM submissions") >= soft_cap:
        return "submit.queue_full", {}
    return None


def wordlist(kind: str) -> list[str]:
    return [r["word"] for r in db.q("SELECT word FROM wordlist WHERE kind=?", (kind,))]


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

def _category_kb(lang: str):
    cats = db.q("SELECT id, label FROM categories WHERE enabled=1 ORDER BY sort, id")
    rows = [[(c["label"], "cat", str(c["id"]))] for c in cats]
    rows.append([(t(lang, "menu.cancel"), "cat", "cancel")])
    return kb(rows)


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_private_chat(update):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    author = ensure_user(update.effective_user.id)
    lang = lang_of(update)
    blocked = gate(author, db.now())
    msg = update.effective_message
    if blocked:
        await msg.reply_text(t(lang, blocked[0], **blocked[1]))
        return ConversationHandler.END
    context.user_data.clear()
    await msg.reply_text(t(lang, "submit.pick_category"), reply_markup=_category_kb(lang))
    return CATEGORY


async def pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    if not parsed or parsed[1] == "cancel":
        await q.message.reply_text(t(lang, "submit.cancelled"), reply_markup=main_menu(lang))
        return ConversationHandler.END
    cat = db.one("SELECT id FROM categories WHERE id=? AND enabled=1", (parsed[1],))
    if not cat:
        return CATEGORY
    context.user_data["category_id"] = cat["id"]
    await q.message.reply_text(t(lang, "submit.send_text", min=cfg("min_len", 20), max=cfg("max_len", 2000)))
    return TEXT


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    text = strip_invisible(update.message.text or "").strip()
    result = check_text(
        text,
        min_len=cfg("min_len", 20),
        max_len=cfg("max_len", 2000),
        blocklist=wordlist("block"),
        risklist=wordlist("risk"),
    )
    if not result.ok:
        await update.message.reply_text(
            t(lang, f"submit.{result.reason}", min=cfg("min_len", 20), max=cfg("max_len", 2000))
        )
        return TEXT
    h = text_hash(text)
    if db.one("SELECT 1 FROM submissions WHERE text_hash=?", (h,)):
        await update.message.reply_text(t(lang, "submit.duplicate"))
        return TEXT
    context.user_data.update(text=text, text_hash=h, risk_hits=result.risk_hits)
    cat = category(context.user_data.get("category_id"))
    await update.message.reply_text(
        t(lang, "submit.preview", text=text, hashtag=cat["hashtag"]),
        reply_markup=kb([
            [(t(lang, "submit.btn_submit"), "sub", "go"), (t(lang, "submit.btn_edit"), "sub", "edit")],
            [(t(lang, "menu.cancel"), "sub", "cancel")],
        ]),
    )
    return PREVIEW


async def not_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(t(lang_of(update), "submit.text_only"))
    return TEXT


async def preview_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    if not parsed or parsed[1] == "cancel":
        context.user_data.clear()
        await q.message.reply_text(t(lang, "submit.cancelled"), reply_markup=main_menu(lang))
        return ConversationHandler.END
    if parsed[1] == "edit":
        await q.message.reply_text(t(lang, "submit.send_text", min=cfg("min_len", 20), max=cfg("max_len", 2000)))
        return TEXT
    return await _finalize(update, context, lang)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    author = ensure_user(update.effective_user.id)
    now = db.now()
    blocked = gate(author, now)
    msg = update.effective_message
    if blocked:
        await msg.reply_text(t(lang, blocked[0], **blocked[1]))
        return ConversationHandler.END
    data = context.user_data
    if "text" not in data:
        return ConversationHandler.END
    hits = data.get("risk_hits") or []
    try:
        with db.tx():
            db.execute(
                "INSERT INTO submissions(author, category_id, text, text_hash, risk_hits, priority, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (author, data.get("category_id"), data["text"], data["text_hash"], json.dumps(hits), 1 if hits else 0, now),
            )
            today = now // 86400
            db.execute(
                "UPDATE users SET last_submit=?, submits_today=CASE WHEN submits_day=? THEN submits_today+1 ELSE 1 END, "
                "submits_day=? WHERE author=?",
                (now, today, today, author),
            )
    except db.sqlite3.IntegrityError:
        await msg.reply_text(t(lang, "submit.duplicate"))
        return ConversationHandler.END
    context.user_data.clear()
    reply = cfg("received_text", "")
    banner = active_banner()
    if banner:
        reply += f"\n\n{banner}"
    await msg.reply_text(reply, reply_markup=main_menu(lang))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    lang = lang_of(update)
    await update.effective_message.reply_text(t(lang, "submit.cancelled"), reply_markup=main_menu(lang))
    return ConversationHandler.END


def register(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("confess", begin),
            CallbackQueryHandler(begin, pattern=r"^menu:confess:"),
        ],
        states={
            CATEGORY: [CallbackQueryHandler(pick_category, pattern=r"^cat:")],
            TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
                MessageHandler(~filters.TEXT & ~filters.COMMAND, not_text),
            ],
            PREVIEW: [CallbackQueryHandler(preview_action, pattern=r"^sub:")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", cancel)],
        conversation_timeout=1800,
        name="submit",
    )
    app.add_handler(conv)
