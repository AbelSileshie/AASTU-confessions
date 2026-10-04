"""/settings menu, /set, /export, /import, /post_rules."""
from __future__ import annotations

import json

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import config
import db
from fmt import Rich
from common import is_private_chat, kb, normalize_newlines, pending_clear, pending_get, pending_set, render_setting_text, staff_reply
from textfilter import strip_invisible
from config import cfg
from security import require, verify_cb

GROUPS = ("submissions", "posting", "comments", "moderation", "ads", "texts")
CONFIG_TABLES = ("settings", "categories", "packages", "wordlist")


def validate(row: dict, raw: str) -> tuple[bool, str]:
    """Pure validator → (ok, normalized_value_or_error)."""
    raw = raw.strip()
    if row["type"] == "int":
        try:
            v = int(raw)
        except ValueError:
            return False, "must be an integer"
        if row["min"] is not None and v < row["min"]:
            return False, f"minimum is {row['min']}"
        if row["max"] is not None and v > row["max"]:
            return False, f"maximum is {row['max']}"
        return True, str(v)
    if row["type"] == "bool":
        if raw.lower() in ("1", "true", "yes", "on"):
            return True, "1"
        if raw.lower() in ("0", "false", "no", "off"):
            return True, "0"
        return False, "use 1 or 0"
    raw = normalize_newlines(strip_invisible(raw))
    if not raw:
        return False, "text cannot be empty"
    if len(raw) > 4000:
        return False, "text too long"
    return True, raw


def set_setting(key: str, raw: str, actor: int) -> tuple[bool, str]:
    row = db.one("SELECT * FROM settings WHERE key=?", (key,))
    if not row:
        return False, "unknown key"
    ok, value = validate(dict(row), raw)
    if not ok:
        return False, value
    db.execute("UPDATE settings SET value=?, updated_by=?, updated_at=? WHERE key=?", (value, actor, db.now(), key))
    config.invalidate(key)
    db.audit(actor, "set", key, value[:80])
    return True, value


def _groups_kb():
    return kb([[(g.title(), "st", f"g:{g}") for g in GROUPS[:3]], [(g.title(), "st", f"g:{g}") for g in GROUPS[3:]]])


def _keys_kb(group: str):
    rows = db.q("SELECT key, value, type FROM settings WHERE grp=? ORDER BY key", (group,))
    buttons = []
    for r in rows:
        shown = r["value"] if r["type"] != "text" else (r["value"] or "").split("\n")[0][:20] + "…"
        buttons.append([(f"{r['key']} = {shown[:20]}", "st", f"k:{r['key']}")])
    buttons.append([("⬅️ Groups", "st", "home")])
    return kb(buttons)


@require("admin")
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    await update.message.reply_text("Settings — pick an area:", reply_markup=_groups_kb())


@require("admin")
async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    if not parsed:
        return
    payload = parsed[1]
    if payload == "home":
        pending_clear(context, "setting_key")
        await q.message.edit_text("Settings — pick an area:", reply_markup=_groups_kb())
    elif payload.startswith("g:"):
        pending_clear(context, "setting_key")
        group = payload[2:]
        if group in GROUPS:
            await q.message.edit_text(f"Settings › {group}", reply_markup=_keys_kb(group))
    elif payload.startswith("k:"):
        key = payload[2:]
        row = db.one("SELECT * FROM settings WHERE key=?", (key,))
        if not row:
            return
        pending_set(context, "setting_key", key, q.message.chat_id)
        rng = f" [{row['min']}–{row['max']}]" if row["type"] == "int" and row["min"] is not None else ""
        r = (Rich().code(key).text(f" · {row['type']}{rng}").nl().italic(row["description"] or "").nl(2)
             .bold("Current:").nl().quote(normalize_newlines(row["value"] or "") or "—").nl(2)
             .text("Send the new value." + (" Line breaks are kept." if row["type"] == "text" else "")))
        text, ents = r.build()
        await q.message.reply_text(text, entities=ents,
                                   reply_markup=kb([[("⬅️ Back", "st", f"g:{row['grp']}"), ("✖️ Cancel", "st", "home")]]))


@require("admin")
async def settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    key = pending_get(context, "setting_key", update.effective_chat.id)
    if not key:
        return
    ok, result = set_setting(key, update.message.text or "", update.effective_user.id)
    pending_clear(context, "setting_key")
    await _confirm_setting(update.message, key, ok, result)


async def _confirm_setting(message, key: str, ok: bool, result: str) -> None:
    """Confirm and, for texts, show exactly what users will see."""
    markup = kb([[("⬅️ Settings", "st", "home"), ("⚙️ Admin", "hub", "home")]])
    if not ok:
        await message.reply_text(f"❌ {result}", reply_markup=markup)
        return
    row = db.one("SELECT type FROM settings WHERE key=?", (key,))
    if row and row["type"] == "text":
        await message.reply_text(f"✅ {key} saved. Preview:")
        text, ents = render_setting_text(result)
        await message.reply_text(text, entities=ents, reply_markup=markup)
    else:
        await message.reply_text(f"✅ {key} = {result}", reply_markup=markup)


@require("admin")
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    if len(context.args or []) < 2:
        await staff_reply(update, "Usage: /set <key> <value>  (multi-line text is kept as typed)")
        return
    key = context.args[0]
    # take the value from the raw message so newlines survive (context.args splits on them)
    raw = update.message.text or ""
    value = raw.split(None, 2)[2] if len(raw.split(None, 2)) > 2 else ""
    ok, result = set_setting(key, value, update.effective_user.id)
    await _confirm_setting(update.message, key, ok, result)


@require("admin")
async def cancel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if pending_clear(context, "setting_key"):
        await update.message.reply_text("Cancelled.")


def export_config() -> dict:
    return {tbl: [dict(r) for r in db.q(f"SELECT * FROM {tbl}")] for tbl in CONFIG_TABLES}  # nosec B608 - CONFIG_TABLES constant


def import_config(data: dict, actor: int) -> int:
    """Replace config tables from an export. Only whitelisted tables/columns."""
    count = 0
    with db.tx():
        for tbl in CONFIG_TABLES:
            rows = data.get(tbl)
            if not isinstance(rows, list):
                continue
            cols = [r["name"] for r in db.q(f"PRAGMA table_info({tbl})")]  # nosec B608 - constant
            db.execute(f"DELETE FROM {tbl}")  # nosec B608 - constant
            for row in rows:
                if not isinstance(row, dict):
                    continue
                keys = [c for c in cols if c in row]
                placeholders = ",".join("?" for _ in keys)
                db.execute(f"INSERT INTO {tbl}({','.join(keys)}) VALUES ({placeholders})",  # nosec B608 - keys ⊆ PRAGMA columns
                           [row[k] for k in keys])
                count += 1
    config.invalidate()
    db.audit(actor, "import_config", detail=str(count))
    return count


@require("owner")
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    payload = json.dumps(export_config(), ensure_ascii=False, indent=1).encode()
    await update.message.reply_document(document=payload, filename="confessbot-config.json")


@require("owner")
async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    doc = update.message.document
    if not doc or (doc.file_size or 0) > 2_000_000:
        await update.message.reply_text("Reply to /import with a JSON file (≤2 MB) attached, caption /import.")
        return
    f = await doc.get_file()
    raw = bytes(await f.download_as_bytearray())
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        await update.message.reply_text("Invalid JSON.")
        return
    n = import_config(data, update.effective_user.id)
    await update.message.reply_text(f"Imported {n} rows.")


@require("admin")
async def post_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = context.bot_data["env"]
    text, ents = render_setting_text(cfg("rules_text", ""))
    msg = await context.bot.send_message(env.channel_id, text, entities=ents)
    try:
        await context.bot.pin_chat_message(env.channel_id, msg.message_id, disable_notification=True)
    except TelegramError:  # nosec B110 - pinning is best-effort; the post itself succeeded
        pass
    db.audit(update.effective_user.id, "post_rules", str(msg.message_id))
    await staff_reply(update, "Rules posted and pinned.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("post_rules", post_rules))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/import"), import_cmd))
    app.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^st:"))
    app.add_handler(CommandHandler("cancel", cancel_setting), group=3)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, settings_value), group=3)
