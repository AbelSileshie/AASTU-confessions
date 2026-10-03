"""/start, /help, /rules, language picker, browse recent, bottom-keyboard routing, fallbacks."""
from __future__ import annotations

from telegram import MenuButtonCommands, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from common import dispatch_nav, ensure_user, is_private_chat, kb, lang_of, main_menu, nav_action, nav_keyboard, nav_regex, send_home, show_browse, show_help, show_home, show_lang, show_rules
import db
from config import cfg
from i18n import SUPPORTED, t
from security import verify_cb
from textfilter import strip_invisible

DEEP_LINK_PREFIXES = ("inv_", "c_")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    payload = context.args[0] if context.args else ""
    if payload.startswith(DEEP_LINK_PREFIXES):
        return  # handled by staff / comments modules
    ensure_user(update.effective_user.id)
    await show_home(update.message, lang_of(update))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update):
        await show_help(update.message, lang_of(update))


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update):
        await show_rules(update.message, lang_of(update))


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update):
        await show_lang(update.message)


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    if not parsed:
        return
    lang = lang_of(update)
    what = parsed[1]
    if what == "home":
        await show_home(q.message, lang)
    elif what == "rules":
        await show_rules(q.message, lang)
    elif what == "lang":
        await show_lang(q.message)
    elif what == "browse":
        await show_browse(q.message, context, lang)
    elif what == "help":
        await show_help(q.message, lang)


async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    if not parsed or parsed[1] not in SUPPORTED:
        return
    code = parsed[1]
    author = ensure_user(update.effective_user.id)
    db.execute("UPDATE users SET lang=? WHERE author=?", (code, author))
    await q.message.reply_text(t(code, "lang.set"), reply_markup=nav_keyboard(code))
    await q.message.reply_text(t(code, "nav.hub"), reply_markup=main_menu(code))


async def nav_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bottom-keyboard labels that this module owns. Confess/Advertise are
    conversation entry points registered in their own modules."""
    if not is_private_chat(update):
        return
    ensure_user(update.effective_user.id)
    await dispatch_nav(update.message, context, lang_of(update), nav_action(update.message.text))


async def nav_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline ✖️ Cancel pressed outside any conversation (conversations handle their own)."""
    q = update.callback_query
    await q.answer()
    await send_home(q.message, lang_of(update), t(lang_of(update), "nav.cancelled"))


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free text outside every flow: guide the user, and offer to confess if it looks like one."""
    if not is_private_chat(update):
        return
    lang = lang_of(update)
    ensure_user(update.effective_user.id)
    text = strip_invisible(update.message.text or "").strip()
    if len(text) >= cfg("min_len", 20) and cfg("submissions_open", True):
        context.user_data["offered_text"] = text[: cfg("max_len", 2000)]
        await update.message.reply_text(
            t(lang, "nav.offer_submit"),
            reply_markup=kb([[(t(lang, "nav.btn_offer_yes"), "menu", "confess_offer"), (t(lang, "nav.btn_offer_no"), "nav", "cancel")]]),
        )
        return
    await update.message.reply_text(t(lang, "nav.unknown"), reply_markup=nav_keyboard(lang))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update):
        await show_help(update.message, lang_of(update))


async def set_menu_button(app: Application) -> None:
    try:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError:
        pass


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu:(home|rules|lang|browse|help):"))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern=r"^lang:"))


# keys other modules keep in user_data while a stateful (non-conversation) step is pending
_PENDING_KEYS = ("editing", "setting_key")


def _idle(context) -> bool:
    return not any(k in context.user_data for k in _PENDING_KEYS)


def register_late(app: Application) -> None:
    """Called after every module registered: these only fire when nothing else matched."""

    async def _text(update, context):
        if _idle(context):
            await unknown_text(update, context)

    async def _cmd(update, context):
        await unknown_command(update, context)

    # hub labels after every conversation, so an active flow can escape first
    hub_labels = nav_regex(exclude=("confess", "advertise"))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(hub_labels) & filters.ChatType.PRIVATE, nav_label))
    app.add_handler(CallbackQueryHandler(nav_cancel_cb, pattern=r"^nav:cancel:"))
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, _cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, _text))
