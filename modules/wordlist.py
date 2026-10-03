"""/block /risk /unblock /words — wordlist fast path. Logic in ops.py."""
from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from common import is_private_chat
from ops import add_word, remove_word, words
from security import require


async def _add(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    if not is_private_chat(update):
        return
    phrase = " ".join(context.args or [])
    if not phrase:
        await update.message.reply_text(f"Usage: /{kind} <word or phrase>")
        return
    await update.message.reply_text("Added." if add_word(phrase, kind, update.effective_user.id) else "Invalid word.")


@require("moderator")
async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _add(update, context, "block")


@require("moderator")
async def risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _add(update, context, "risk")


@require("admin")
async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    phrase = " ".join(context.args or [])
    n = remove_word(phrase, update.effective_user.id) if phrase else 0
    await update.message.reply_text(f"Removed {n}.")


@require("moderator")
async def words_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    blocks = ", ".join(w["word"] for w in words("block")) or "-"
    risks = ", ".join(w["word"] for w in words("risk")) or "-"
    await update.message.reply_text(f"Block:\n{blocks}\n\nRisk:\n{risks}"[:4000])


def register(app: Application) -> None:
    app.add_handler(CommandHandler("block", block))
    app.add_handler(CommandHandler("risk", risk))
    app.add_handler(CommandHandler("unblock", unblock))
    app.add_handler(CommandHandler("words", words_cmd))
