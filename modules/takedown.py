"""Takedown: moderator /takedown, author /delete, public takedown request."""
from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
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
from common import alert_admins, ensure_user, is_private_chat, kb, lang_of, main_menu, notify_author, parse_int, user_lang
from i18n import t
from security import author_hash, require, verify_cb
from textfilter import strip_invisible

ASK_NUMBER, ASK_REASON = range(2)


async def take_down(context: ContextTypes.DEFAULT_TYPE, number: int, actor: int, reason: str, by_author: bool = False) -> bool:
    """Delete the channel message and mark the post deleted. Returns success."""
    post = db.one("SELECT * FROM posts WHERE number=? AND deleted=0", (number,))
    if not post:
        return False
    env = context.bot_data["env"]
    try:
        await context.bot.delete_message(env.channel_id, post["channel_msg_id"])
    except TelegramError:
        pass  # already gone or too old; still mark deleted
    db.execute("UPDATE posts SET deleted=1, deleted_reason=? WHERE number=?", (reason[:200], number))
    db.execute("UPDATE comments SET hidden=1, hidden_at=? WHERE number=? AND hidden=0", (db.now(), number))
    db.audit(actor if not by_author else 0, "takedown" if not by_author else "author_delete", f"post:{number}", reason[:200])
    if not by_author and post["author"]:
        await notify_author(context, post["author"], t(user_lang(post["author"]), "notify.deleted", number=number))
    return True


@require("moderator")
async def takedown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    number = parse_int(context.args[0]) if context.args else None
    if number is None:
        await update.message.reply_text("Usage: /takedown <number> <reason>")
        return
    reason = " ".join(context.args[1:]) or "moderator"
    ok = await take_down(context, number, update.effective_user.id, reason)
    await update.message.reply_text(f"Taken down #{number}." if ok else "Not found or already deleted.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    lang = lang_of(update)
    number = parse_int(context.args[0]) if context.args else None
    if number is None:
        await update.message.reply_text(t(lang, "delete.usage"))
        return
    author = author_hash(update.effective_user.id)
    post = db.one("SELECT 1 FROM posts WHERE number=? AND author=? AND deleted=0", (number, author))
    if not post:
        await update.message.reply_text(t(lang, "delete.not_yours"))
        return
    await take_down(context, number, 0, "author", by_author=True)
    await update.message.reply_text(t(lang, "delete.done", number=number))


# ---------------------------------------------------------------------------
# Public takedown request (conversation)
# ---------------------------------------------------------------------------

async def request_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ensure_user(update.effective_user.id)
    await q.message.reply_text(t(lang_of(update), "takedown.ask_number"))
    return ASK_NUMBER


async def request_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    number = parse_int(update.message.text)
    if number is None or not db.one("SELECT 1 FROM posts WHERE number=? AND deleted=0", (number,)):
        await update.message.reply_text(t(lang, "takedown.invalid"))
        return ASK_NUMBER
    context.user_data["td_number"] = number
    await update.message.reply_text(t(lang, "takedown.ask_reason"))
    return ASK_REASON


async def request_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    number = context.user_data.pop("td_number", None)
    reason = strip_invisible(update.message.text or "")[:300]
    if number is None:
        return ConversationHandler.END
    requester = author_hash(update.effective_user.id)[:12]
    db.audit(0, "takedown_request", f"post:{number}", f"{requester} {reason}")
    await alert_admins(context, f"🛑 Takedown request for #{number}\nReason: {reason}",
                       reply_markup=kb([[("Take down", "td", f"go:{number}"), ("Ignore", "td", f"ignore:{number}")]]))
    await update.message.reply_text(t(lang, "takedown.sent"), reply_markup=main_menu(lang))
    return ConversationHandler.END


async def request_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("td_number", None)
    return ConversationHandler.END


@require("moderator")
async def takedown_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    action, _, num = parsed[1].partition(":")
    number = parse_int(num)
    if action == "go" and number is not None:
        ok = await take_down(context, number, update.effective_user.id, "takedown request")
        await q.answer("Done" if ok else "Already gone")
        await q.message.edit_text(q.message.text + f"\n\n✅ taken down by {update.effective_user.id}")
    else:
        await q.answer("Ignored")
        await q.message.edit_text(q.message.text + f"\n\n⏭ ignored by {update.effective_user.id}")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("takedown", takedown_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CallbackQueryHandler(takedown_cb, pattern=r"^td:(go|ignore):"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(request_start, pattern=r"^takedown:start:")],
        states={
            ASK_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_number)],
            ASK_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_reason)],
        },
        fallbacks=[CommandHandler("cancel", request_cancel), CommandHandler("start", request_cancel)],
        conversation_timeout=600,
        name="takedown_request",
    ))
