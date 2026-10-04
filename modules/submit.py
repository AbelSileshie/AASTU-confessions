"""Confession submission: category → text → preview → submit.

Navigation contract: every prompt carries [⬅️ Back] [✖️ Cancel]; every exit
re-sends the bottom keyboard; bottom-keyboard labels abort the flow cleanly.
"""
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
from common import (
    active_banner,
    category,
    dispatch_nav,
    ensure_user,
    exit_row,
    is_banned,
    is_private_chat,
    kb,
    lang_of,
    nav_action,
    nav_keyboard,
    nav_regex,
    normalize_newlines,
    send_home,
)
from config import cfg
from fmt import Rich
from i18n import t
from security import verify_cb
from textfilter import check_text, strip_invisible, text_hash

CATEGORY, TEXT, PREVIEW = range(3)


# ---------------------------------------------------------------------------
# Gate checks (pure given DB state) → (i18n key, kwargs) or None
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
# Screens
# ---------------------------------------------------------------------------

def _category_kb(lang: str):
    cats = db.q("SELECT id, label FROM categories WHERE enabled=1 ORDER BY sort, id")
    rows, row = [], []
    for c in cats:
        row.append((c["label"], "cat", str(c["id"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([(t(lang, "menu.cancel"), "cat", "cancel")])
    return kb(rows)


async def _ask_category(message, lang: str) -> int:
    await message.reply_text(t(lang, "submit.pick_category"), reply_markup=_category_kb(lang))
    return CATEGORY


async def _ask_text(message, lang: str) -> int:
    await message.reply_text(
        t(lang, "submit.send_text", min=cfg("min_len", 20), max=cfg("max_len", 2000)),
        reply_markup=exit_row(lang, back=("sub", "back_cat")),
    )
    return TEXT


async def _show_preview(message, context, lang: str) -> int:
    cat = category(context.user_data.get("category_id"))
    text, ents = (Rich().bold(t(lang, "submit.preview_title")).nl(2).quote(context.user_data["text"]).nl(2)
                  .text(cat["hashtag"]).nl(2).italic(t(lang, "submit.preview_hint")).build())
    await message.reply_text(
        text, entities=ents,
        reply_markup=kb([
            [(t(lang, "submit.btn_submit"), "sub", "go")],
            [(t(lang, "submit.btn_edit"), "sub", "edit"), (t(lang, "submit.btn_category", label=cat["label"]), "sub", "back_cat")],
            [(t(lang, "menu.cancel"), "sub", "cancel")],
        ]),
    )
    return PREVIEW


async def _leave(message, context, lang: str, text_key: str = "nav.cancelled") -> int:
    context.user_data.pop("category_id", None)
    context.user_data.pop("text", None)
    context.user_data.pop("text_hash", None)
    context.user_data.pop("risk_hits", None)
    context.user_data.pop("offered_text", None)
    await send_home(message, lang, t(lang, text_key))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_private_chat(update):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    author = ensure_user(update.effective_user.id)
    lang = lang_of(update)
    msg = update.effective_message
    blocked = gate(author, db.now())
    if blocked:
        await msg.reply_text(t(lang, blocked[0], **blocked[1]), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    offered = context.user_data.pop("offered_text", None)
    for k in ("category_id", "text", "text_hash", "risk_hits"):
        context.user_data.pop(k, None)
    if offered and update.callback_query and update.callback_query.data.startswith("menu:confess_offer:"):
        context.user_data["pending_offer"] = offered
    return await _ask_category(msg, lang)


async def pick_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    if not parsed or parsed[1] == "cancel":
        return await _leave(q.message, context, lang)
    cat = db.one("SELECT id FROM categories WHERE id=? AND enabled=1", (parsed[1],))
    if not cat:
        return CATEGORY
    context.user_data["category_id"] = cat["id"]
    offered = context.user_data.pop("pending_offer", None)
    if offered:
        state = await _accept_text(q.message, context, lang, offered)
        if state == PREVIEW:
            return state
    if "text" in context.user_data:  # came back from the preview to change category
        return await _show_preview(q.message, context, lang)
    return await _ask_text(q.message, lang)


async def _accept_text(message, context, lang: str, text: str) -> int:
    """Validate text; on success store it and show the preview. On failure
    explain and stay in TEXT (with exits)."""
    result = check_text(
        text,
        min_len=cfg("min_len", 20),
        max_len=cfg("max_len", 2000),
        blocklist=wordlist("block"),
        risklist=wordlist("risk"),
    )
    exits = exit_row(lang, back=("sub", "back_cat"))
    if not result.ok:
        await message.reply_text(
            t(lang, f"submit.{result.reason}", min=cfg("min_len", 20), max=cfg("max_len", 2000)), reply_markup=exits)
        return TEXT
    h = text_hash(text)
    if db.one("SELECT 1 FROM submissions WHERE text_hash=?", (h,)):
        await message.reply_text(t(lang, "submit.duplicate"), reply_markup=exits)
        return TEXT
    context.user_data.update(text=text, text_hash=h, risk_hits=result.risk_hits)
    return await _show_preview(message, context, lang)


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    text = strip_invisible(update.message.text or "").strip()
    return await _accept_text(update.message, context, lang, text)


async def not_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    await update.message.reply_text(t(lang, "submit.text_only"), reply_markup=exit_row(lang, back=("sub", "back_cat")))
    return TEXT


async def flow_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """sub:* buttons: go / edit / back_cat / cancel — valid from TEXT and PREVIEW."""
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    action = parsed[1] if parsed else "cancel"
    if action == "cancel":
        return await _leave(q.message, context, lang)
    if action == "back_cat":
        return await _ask_category(q.message, lang)
    if action == "edit":
        return await _ask_text(q.message, lang)
    if action == "go" and "text" in context.user_data:
        return await _finalize(update, context, lang)
    return await _leave(q.message, context, lang)


async def nav_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    return await _leave(update.callback_query.message, context, lang_of(update))


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> int:
    author = ensure_user(update.effective_user.id)
    now = db.now()
    msg = update.effective_message
    blocked = gate(author, now)
    if blocked:
        await msg.reply_text(t(lang, blocked[0], **blocked[1]), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    data = context.user_data
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
        await msg.reply_text(t(lang, "submit.duplicate"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    r = Rich().text(normalize_newlines(cfg("received_text", "")))
    banner = active_banner()
    if banner:
        r.nl(2).italic(banner)
    for k in ("category_id", "text", "text_hash", "risk_hits"):
        context.user_data.pop(k, None)
    text, ents = r.build()
    await msg.reply_text(text, entities=ents, reply_markup=nav_keyboard(lang, update.effective_user.id))
    return ConversationHandler.END


async def escape_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A bottom-keyboard label typed mid-flow: leave, then let core/others handle it next time.
    Confess restarts the flow; Cancel/others exit."""
    lang = lang_of(update)
    action = nav_action(update.message.text)
    for k in ("category_id", "text", "text_hash", "risk_hits", "pending_offer"):
        context.user_data.pop(k, None)
    if action == "confess":
        return await _ask_category(update.message, lang)
    if not await dispatch_nav(update.message, context, lang, action):
        await send_home(update.message, lang, t(lang, "nav.cancelled"))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _leave(update.effective_message, context, lang_of(update))


def register(app: Application) -> None:
    label_filter = filters.TEXT & filters.Regex(nav_regex())
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("confess", begin),
            CallbackQueryHandler(begin, pattern=r"^menu:(confess|confess_offer):"),
            MessageHandler(filters.TEXT & filters.Regex(_confess_label_regex()) & filters.ChatType.PRIVATE, begin),
        ],
        states={
            CATEGORY: [CallbackQueryHandler(pick_category, pattern=r"^cat:")],
            TEXT: [
                MessageHandler(label_filter, escape_label),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
                MessageHandler(~filters.TEXT & ~filters.COMMAND, not_text),
            ],
            PREVIEW: [MessageHandler(label_filter, escape_label)],
        },
        fallbacks=[
            CallbackQueryHandler(flow_cb, pattern=r"^sub:"),
            CallbackQueryHandler(nav_cancel_cb, pattern=r"^nav:cancel:"),
            MessageHandler(label_filter, escape_label),
            CommandHandler("cancel", cancel),
            CommandHandler("start", cancel),
        ],
        conversation_timeout=1800,
        name="submit",
    )
    app.add_handler(conv)


def _confess_label_regex() -> str:
    return nav_regex(only=("confess",))
