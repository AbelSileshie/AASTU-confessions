"""/block /risk /unblock /words — wordlist management."""
from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from common import is_private_chat
from security import require
from textfilter import normalize

MAX_WORD_LEN = 64


def add_word(word: str, kind: str, actor: int) -> bool:
    w = word.strip()[:MAX_WORD_LEN]
    if not normalize(w):
        return False
    db.execute("INSERT OR IGNORE INTO wordlist(word, kind, added_by, added_at) VALUES (?,?,?,?)", (w, kind, actor, db.now()))
    db.audit(actor, f"word_add_{kind}", w)
    return True


def remove_word(word: str, actor: int) -> int:
    n = db.execute("DELETE FROM wordlist WHERE word=?", (word.strip(),))
    db.audit(actor, "word_remove", word.strip()[:MAX_WORD_LEN])
    return n


async def _add(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    if not is_private_chat(update):
        return
    phrase = " ".join(context.args or [])
    if not phrase:
        await update.message.reply_text(f"Usage: /{kind} <word or phrase>")
        return
    ok = add_word(phrase, kind, update.effective_user.id)
    await update.message.reply_text("Added." if ok else "Invalid word.")


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
async def words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    rows = db.q("SELECT word, kind FROM wordlist ORDER BY kind, word")
    blocks = ", ".join(r["word"] for r in rows if r["kind"] == "block") or "-"
    risks = ", ".join(r["word"] for r in rows if r["kind"] == "risk") or "-"
    await update.message.reply_text(f"Block ({blocks.count(',') + 1 if blocks != '-' else 0}):\n{blocks}\n\nRisk:\n{risks}"[:4000])


def register(app: Application) -> None:
    app.add_handler(CommandHandler("block", block))
    app.add_handler(CommandHandler("risk", risk))
    app.add_handler(CommandHandler("unblock", unblock))
    app.add_handler(CommandHandler("words", words))
