"""/settings menu, /set, /export, /import, /post_rules."""
from __future__ import annotations

import json

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import config
import db
from common import is_private_chat, kb
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
        shown = r["value"] if r["type"] != "text" else "…"
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
        await q.message.edit_text("Settings — pick an area:", reply_markup=_groups_kb())
    elif payload.startswith("g:"):
        group = payload[2:]
        if group in GROUPS:
            await q.message.edit_text(f"Settings › {group}", reply_markup=_keys_kb(group))
    elif payload.startswith("k:"):
        key = payload[2:]
        row = db.one("SELECT * FROM settings WHERE key=?", (key,))
        if not row:
            return
        context.user_data["setting_key"] = key
        rng = f" [{row['min']}–{row['max']}]" if row["type"] == "int" and row["min"] is not None else ""
        await q.message.reply_text(
            f"{key} ({row['type']}{rng})\n{row['description']}\n\nCurrent:\n{row['value']}\n\nSend the new value (or /cancel).")


@require("admin")
async def settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = context.user_data.get("setting_key")
    if not key or not is_private_chat(update):
        return
    ok, result = set_setting(key, update.message.text or "", update.effective_user.id)
    context.user_data.pop("setting_key", None)
    await update.message.reply_text(f"✅ {key} = {result[:200]}" if ok else f"❌ {result}")


@require("admin")
async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    if len(context.args or []) < 2:
        await update.message.reply_text("Usage: /set <key> <value>")
        return
    key, value = context.args[0], " ".join(context.args[1:])
    ok, result = set_setting(key, value, update.effective_user.id)
    await update.message.reply_text(f"✅ {key} = {result[:200]}" if ok else f"❌ {result}")


@require("admin")
async def cancel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("setting_key", None):
        await update.message.reply_text("Cancelled.")


def export_config() -> dict:
    return {tbl: [dict(r) for r in db.q(f"SELECT * FROM {tbl}")] for tbl in CONFIG_TABLES}  # noqa: S608 - constant names


def import_config(data: dict, actor: int) -> int:
    """Replace config tables from an export. Only whitelisted tables/columns."""
    count = 0
    with db.tx():
        for tbl in CONFIG_TABLES:
            rows = data.get(tbl)
            if not isinstance(rows, list):
                continue
            cols = [r["name"] for r in db.q(f"PRAGMA table_info({tbl})")]  # noqa: S608
            db.execute(f"DELETE FROM {tbl}")  # noqa: S608
            for row in rows:
                if not isinstance(row, dict):
                    continue
                keys = [c for c in cols if c in row]
                placeholders = ",".join("?" for _ in keys)
                db.execute(f"INSERT INTO {tbl}({','.join(keys)}) VALUES ({placeholders})",  # noqa: S608
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
    msg = await context.bot.send_message(env.channel_id, cfg("rules_text", ""))
    try:
        await context.bot.pin_chat_message(env.channel_id, msg.message_id, disable_notification=True)
    except Exception:  # noqa: BLE001
        pass
    db.audit(update.effective_user.id, "post_rules", str(msg.message_id))
    await update.message.reply_text("Rules posted and pinned.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("set", set_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("post_rules", post_rules))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/import"), import_cmd))
    app.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^st:"))
    app.add_handler(CommandHandler("cancel", cancel_setting), group=3)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, settings_value), group=3)
