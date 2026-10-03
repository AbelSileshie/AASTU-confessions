"""/start, /help, /rules, language picker, browse recent."""
from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import db
from common import bot_link, ensure_user, is_private_chat, kb, lang_of, main_menu, post_link
from config import cfg
from i18n import SUPPORTED, t
from security import author_hash, verify_cb

DEEP_LINK_PREFIXES = ("inv_", "c_")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    payload = context.args[0] if context.args else ""
    if payload.startswith(DEEP_LINK_PREFIXES):
        return  # handled by staff / comments modules
    ensure_user(update.effective_user.id)
    lang = lang_of(update)
    await update.message.reply_text(cfg("welcome_text", ""), reply_markup=main_menu(lang))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    await update.message.reply_text(t(lang_of(update), "help"))


def rules_markup(lang: str):
    return kb([[(t(lang, "menu.takedown"), "takedown", "start")], [(t(lang, "menu.back"), "menu", "home")]])


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    lang = lang_of(update)
    await update.message.reply_text(cfg("rules_text", ""), reply_markup=rules_markup(lang))


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    await update.message.reply_text(t("en", "lang.pick"), reply_markup=_lang_kb())


def _lang_kb():
    labels = {"en": "English", "am": "አማርኛ"}
    return kb([[(labels[code], "lang", code) for code in SUPPORTED]])


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    if not parsed:
        return
    _, what = parsed
    lang = lang_of(update)
    if what == "home":
        await q.message.reply_text(cfg("welcome_text", ""), reply_markup=main_menu(lang))
    elif what == "rules":
        await q.message.reply_text(cfg("rules_text", ""), reply_markup=rules_markup(lang))
    elif what == "lang":
        await q.message.reply_text(t("en", "lang.pick"), reply_markup=_lang_kb())
    elif what == "browse":
        await q.message.reply_text(_recent_text(context) or "Nothing posted yet.", disable_web_page_preview=True)


def _recent_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    rows = db.q(
        "SELECT number, channel_msg_id, comment_count FROM posts "
        "WHERE deleted=0 AND kind='confession' ORDER BY number DESC LIMIT 10"
    )
    lines = []
    for r in rows:
        link = post_link(context, r["channel_msg_id"])
        thread = bot_link(context, f"c_{r['number']}")
        lines.append(f"#{r['number']} · 💬 {r['comment_count']} · {link or thread}")
    return "\n".join(lines)


async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    if not parsed or parsed[1] not in SUPPORTED:
        return
    code = parsed[1]
    author = ensure_user(update.effective_user.id)
    db.execute("UPDATE users SET lang=? WHERE author=?", (code, author))
    await q.message.reply_text(t(code, "lang.set"), reply_markup=main_menu(code))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu:(home|rules|lang|browse):"))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern=r"^lang:"))
