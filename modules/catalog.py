"""CRUD for categories, packages and payout accounts via commands."""
from __future__ import annotations

import shlex

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from common import is_private_chat
from payments import BANK_FIELDS
from security import require

PACKAGE_FIELDS = {
    "name": "text", "description": "text", "price_etb": "int", "posts": "int", "pin_hours": "int",
    "bot_banner_days": "int", "max_chars": "int", "allow_media": "bool", "allow_button": "bool",
    "enabled": "bool", "sort": "int",
}
CATEGORY_FIELDS = {"label": "text", "hashtag": "text", "enabled": "bool", "sort": "int"}


def _coerce(kind: str, raw: str):
    if kind == "int":
        v = int(raw)
        if v < 0 or v > 10_000_000:
            raise ValueError
        return v
    if kind == "bool":
        return 1 if raw.lower() in ("1", "true", "yes", "on") else 0
    if not raw or len(raw) > 500:
        raise ValueError
    return raw


def update_field(table: str, fields: dict, row_id: int, field: str, raw: str) -> bool:
    if field not in fields:
        return False
    try:
        value = _coerce(fields[field], raw)
    except ValueError:
        return False
    # table/field are whitelisted constants, value is parameterised
    return db.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (value, row_id)) > 0  # noqa: S608


def _args(update: Update) -> list[str]:
    text = update.message.text or ""
    try:
        return shlex.split(text)[1:]
    except ValueError:
        return text.split()[1:]


# ---------------------------------------------------------------------------
# /categories
# ---------------------------------------------------------------------------

@require("admin")
async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    a = _args(update)
    actor = update.effective_user.id
    msg = update.message
    if not a or a[0] == "list":
        rows = db.q("SELECT * FROM categories ORDER BY sort, id")
        await msg.reply_text("\n".join(f"{r['id']} · {r['label']} {r['hashtag']} · {'on' if r['enabled'] else 'off'} · sort {r['sort']}" for r in rows)
                             + "\n\n/categories add <slug> \"<label>\" <#hashtag> · set <id> <field> <value> · toggle <id>")
    elif a[0] == "add" and len(a) >= 4:
        slug = a[1].lower()[:32]
        if not slug.isidentifier() or not a[3].startswith("#"):
            await msg.reply_text("slug must be a word; hashtag must start with #")
            return
        db.execute("INSERT OR IGNORE INTO categories(slug, label, hashtag, sort) VALUES (?,?,?,?)",
                   (slug, a[2][:40], a[3][:40], db.scalar("SELECT coalesce(max(sort),0)+1 FROM categories")))
        db.audit(actor, "category_add", slug)
        await msg.reply_text("Added.")
    elif a[0] == "set" and len(a) >= 4:
        ok = update_field("categories", CATEGORY_FIELDS, int(a[1]) if a[1].isdigit() else -1, a[2], " ".join(a[3:]))
        db.audit(actor, "category_set", a[1], f"{a[2]}={' '.join(a[3:])[:40]}")
        await msg.reply_text("Updated." if ok else "Invalid id/field/value.")
    elif a[0] == "toggle" and len(a) >= 2 and a[1].isdigit():
        db.execute("UPDATE categories SET enabled=1-enabled WHERE id=?", (int(a[1]),))
        db.audit(actor, "category_toggle", a[1])
        await msg.reply_text("Toggled.")
    else:
        await msg.reply_text("Unknown subcommand. /categories")


# ---------------------------------------------------------------------------
# /packages
# ---------------------------------------------------------------------------

@require("admin")
async def packages_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    a = _args(update)
    actor = update.effective_user.id
    msg = update.message
    if not a or a[0] == "list":
        rows = db.q("SELECT * FROM packages ORDER BY sort, id")
        lines = [f"{r['id']} · {r['name']} · {r['price_etb']} ETB · posts {r['posts']} · pin {r['pin_hours']}h · banner {r['bot_banner_days']}d · {'on' if r['enabled'] else 'off'}"
                 for r in rows]
        await msg.reply_text("\n".join(lines) + "\n\n/packages add <slug> \"<name>\" <price> · set <id> <field> <value> · toggle <id>\nfields: "
                             + ", ".join(PACKAGE_FIELDS))
    elif a[0] == "add" and len(a) >= 4 and a[3].isdigit():
        slug = a[1].lower()[:32]
        if not slug.isidentifier():
            await msg.reply_text("slug must be a word")
            return
        db.execute("INSERT OR IGNORE INTO packages(slug, name, price_etb, sort) VALUES (?,?,?,?)",
                   (slug, a[2][:60], int(a[3]), db.scalar("SELECT coalesce(max(sort),0)+1 FROM packages")))
        db.audit(actor, "package_add", slug, a[3])
        await msg.reply_text("Added. Set description/posts/pin_hours with /packages set.")
    elif a[0] == "set" and len(a) >= 4:
        ok = update_field("packages", PACKAGE_FIELDS, int(a[1]) if a[1].isdigit() else -1, a[2], " ".join(a[3:]))
        db.audit(actor, "package_set", a[1], f"{a[2]}={' '.join(a[3:])[:40]}")
        await msg.reply_text("Updated (existing orders keep their snapshot price)." if ok else "Invalid id/field/value.")
    elif a[0] == "toggle" and len(a) >= 2 and a[1].isdigit():
        db.execute("UPDATE packages SET enabled=1-enabled WHERE id=?", (int(a[1]),))
        db.audit(actor, "package_toggle", a[1])
        await msg.reply_text("Toggled.")
    else:
        await msg.reply_text("Unknown subcommand. /packages")


# ---------------------------------------------------------------------------
# /payout_accounts  (owner)
# ---------------------------------------------------------------------------

@require("owner")
async def payout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    a = _args(update)
    actor = update.effective_user.id
    msg = update.message
    if not a or a[0] == "list":
        rows = db.q("SELECT * FROM payout_accounts ORDER BY sort, id")
        body = "\n".join(f"{r['id']} · {r['bank']} · {r['account']} · {r['holder']} · {'on' if r['enabled'] else 'off'}" for r in rows) or "none"
        await msg.reply_text(body + "\n\n/payout_accounts add <bank> <account> \"<holder>\" · toggle <id> · remove <id>\nbanks: "
                             + ", ".join(BANK_FIELDS))
    elif a[0] == "add" and len(a) >= 4:
        bank = a[1].lower()
        account = "".join(ch for ch in a[2] if ch.isalnum())
        if bank not in BANK_FIELDS or not (6 <= len(account) <= 20) or not a[3].strip():
            await msg.reply_text("Invalid bank, account or holder.")
            return
        db.execute("INSERT INTO payout_accounts(bank, account, holder, sort) VALUES (?,?,?,?)",
                   (bank, account, a[3][:80], db.scalar("SELECT coalesce(max(sort),0)+1 FROM payout_accounts")))
        db.audit(actor, "payout_add", bank, account[-4:])
        await msg.reply_text("Added. Make sure the same account is registered in your Verify.et dashboard.")
    elif a[0] == "toggle" and len(a) >= 2 and a[1].isdigit():
        db.execute("UPDATE payout_accounts SET enabled=1-enabled WHERE id=?", (int(a[1]),))
        db.audit(actor, "payout_toggle", a[1])
        await msg.reply_text("Toggled.")
    elif a[0] == "remove" and len(a) >= 2 and a[1].isdigit():
        db.execute("UPDATE payout_accounts SET enabled=0 WHERE id=?", (int(a[1]),))
        db.audit(actor, "payout_remove", a[1])
        await msg.reply_text("Disabled (kept for payment history).")
    else:
        await msg.reply_text("Unknown subcommand. /payout_accounts")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("categories", categories_cmd))
    app.add_handler(CommandHandler("packages", packages_cmd))
    app.add_handler(CommandHandler("payout_accounts", payout_cmd))
