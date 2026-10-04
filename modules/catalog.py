"""Catalog slash commands (fast path): /categories /packages /payout_accounts. Logic in ops.py."""
from __future__ import annotations

import shlex

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from common import is_private_chat, staff_reply
from ops import CATEGORY_FIELDS, PACKAGE_FIELDS, add_category, add_package, add_payout, toggle, update_field
from payments import BANK_FIELDS
from security import require


def _args(update: Update) -> list[str]:
    text = update.message.text or ""
    try:
        return shlex.split(text)[1:]
    except ValueError:
        return text.split()[1:]


def _id(s: str) -> int:
    return int(s) if s.isdigit() else -1


@require("admin")
async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    a, actor = _args(update), update.effective_user.id
    if not a or a[0] == "list":
        rows = db.q("SELECT * FROM categories ORDER BY sort, id")
        await staff_reply(update, "\n".join(f"{r['id']} · {r['label']} {r['hashtag']} · {'on' if r['enabled'] else 'off'}" for r in rows)
                             + "\n\n/categories add <slug> \"<label>\" <#hashtag> · set <id> <field> <value> · toggle <id>\nOr ⚙️ Admin → Categories.")
    elif a[0] == "add" and len(a) >= 4:
        await staff_reply(update, "Added." if add_category(a[1], a[2], a[3], actor) else "slug must be a word; hashtag must start with #")
    elif a[0] == "set" and len(a) >= 4:
        await staff_reply(update, "Updated." if update_field("categories", CATEGORY_FIELDS, _id(a[1]), a[2], " ".join(a[3:]), actor) else "Invalid id/field/value.")
    elif a[0] == "toggle" and len(a) >= 2:
        await staff_reply(update, "Toggled." if toggle("categories", _id(a[1]), actor) else "Not found.")
    else:
        await staff_reply(update, "Unknown subcommand. /categories")


@require("admin")
async def packages_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    a, actor = _args(update), update.effective_user.id
    if not a or a[0] == "list":
        rows = db.q("SELECT * FROM packages ORDER BY sort, id")
        lines = [f"{r['id']} · {r['name']} · {r['price_etb']} ETB · posts {r['posts']} · pin {r['pin_hours']}h · {'on' if r['enabled'] else 'off'}" for r in rows]
        await staff_reply(update, "\n".join(lines) + "\n\n/packages add <slug> \"<name>\" <price> · set <id> <field> <value> · toggle <id>\nfields: "
                             + ", ".join(PACKAGE_FIELDS) + "\nOr ⚙️ Admin → Packages.")
    elif a[0] == "add" and len(a) >= 4 and a[3].isdigit():
        await staff_reply(update, "Added. Set description/posts/pin_hours with /packages set." if add_package(a[1], a[2], int(a[3]), actor) else "slug must be a word")
    elif a[0] == "set" and len(a) >= 4:
        ok = update_field("packages", PACKAGE_FIELDS, _id(a[1]), a[2], " ".join(a[3:]), actor)
        await staff_reply(update, "Updated (existing orders keep their snapshot price)." if ok else "Invalid id/field/value.")
    elif a[0] == "toggle" and len(a) >= 2:
        await staff_reply(update, "Toggled." if toggle("packages", _id(a[1]), actor) else "Not found.")
    else:
        await staff_reply(update, "Unknown subcommand. /packages")


@require("owner")
async def payout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    a, actor = _args(update), update.effective_user.id
    if not a or a[0] == "list":
        rows = db.q("SELECT * FROM payout_accounts ORDER BY sort, id")
        body = "\n".join(f"{r['id']} · {r['bank']} · {r['account']} · {r['holder']} · {'on' if r['enabled'] else 'off'}" for r in rows) or "none"
        await staff_reply(update, body + "\n\n/payout_accounts add <bank> <account> \"<holder>\" · toggle <id>\nbanks: " + ", ".join(BANK_FIELDS)
                             + "\nOr ⚙️ Admin → Payout accounts.")
    elif a[0] == "add" and len(a) >= 4:
        ok = add_payout(a[1], a[2], a[3], actor, tuple(BANK_FIELDS))
        await staff_reply(update, "Added. Make sure the same account is registered in your Verify.et dashboard." if ok else "Invalid bank, account or holder.")
    elif a[0] in ("toggle", "remove") and len(a) >= 2:
        await staff_reply(update, "Toggled." if toggle("payout_accounts", _id(a[1]), actor) else "Not found.")
    else:
        await staff_reply(update, "Unknown subcommand. /payout_accounts")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("categories", categories_cmd))
    app.add_handler(CommandHandler("packages", packages_cmd))
    app.add_handler(CommandHandler("payout_accounts", payout_cmd))
