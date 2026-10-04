"""Staff slash commands (fast path). Menus live in modules/admin.py; logic in ops.py."""
from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import db
from common import bot_link, fmt_ts, is_private_chat, staff_reply
from ops import ASSIGNABLE_ROLES, can_assign, create_invite, redeem_invite, remove_staff, set_role, set_shift, staff_list
from security import has_role, require


def _int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


@require("admin")
async def staff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    args = context.args or []
    sub = args[0] if args else "list"
    actor = update.effective_user.id

    if sub == "add":
        role = args[1] if len(args) > 1 and args[1] in ASSIGNABLE_ROLES else "moderator"
        if not can_assign(actor, role):
            await staff_reply(update, "You cannot assign that role.")
            return
        code = create_invite(role, actor)
        await staff_reply(update, f"Invite for a new {role} (valid 24h, single use):\n{bot_link(context, 'inv_' + code)}",
                             disable_web_page_preview=True)
    elif sub == "list":
        rows = staff_list()
        if not rows:
            await staff_reply(update, "No staff yet. /staff add")
            return
        await staff_reply(update, "\n".join(f"{r['user_id']} · {r['role']} · {r['display'] or '-'}{' 🟢' if r['on_shift'] else ''}" for r in rows))
    elif sub == "role" and len(args) >= 3 and (target := _int(args[1])) is not None:
        ok = set_role(actor, target, args[2])
        await staff_reply(update, "Updated." if ok else "Cannot change that user's role.")
    elif sub == "remove" and len(args) >= 2 and (target := _int(args[1])) is not None:
        ok = remove_staff(actor, target)
        await staff_reply(update, "Removed." if ok else "Cannot remove that user.")
    else:
        await staff_reply(update, "/staff add [moderator|admin] · /staff list · /staff role <id> <role> · /staff remove <id>\nOr use ⚙️ Admin → Staff.")


async def start_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not context.args:
        return
    code = context.args[0][4:]
    user = update.effective_user
    if has_role(user.id, "owner"):
        await staff_reply(update, "You are the owner already.")
        return
    role = redeem_invite(code, user.id, user.full_name or "")
    if role:
        await staff_reply(update, f"Welcome aboard — you are now a {role}. Tap 📋 Queue to start reviewing.")
    else:
        await staff_reply(update, "This invite is invalid or expired.")


@require("moderator")
async def shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    on = (context.args or ["on"])[0].lower() == "on"
    set_shift(update.effective_user.id, on)
    await staff_reply(update, "Shift on 🟢" if on else "Shift off ⚪")


@require("admin")
async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and (uid := _int(context.args[0])) is not None:
        rows = db.q("SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT 20", (uid,))
    else:
        rows = db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 20")
    if not rows:
        await staff_reply(update, "No audit entries.")
        return
    await staff_reply(update, "\n".join(
        f"{fmt_ts(r['at'])} · {r['actor']} · {r['action']} {r['target'] or ''} {r['detail'] or ''}" for r in rows))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("staff", staff_cmd))
    app.add_handler(CommandHandler("shift", shift))
    app.add_handler(CommandHandler("audit", audit_cmd))
    app.add_handler(CommandHandler("start", start_invite, filters=filters.Regex(r"^/start\s+inv_")), group=1)
