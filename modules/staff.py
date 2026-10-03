"""Staff management: invites, roles, shifts."""
from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import db
from common import bot_link, fmt_ts, is_private_chat
from security import ROLE_RANK, has_role, new_token, require, role_of

ASSIGNABLE = ("moderator", "admin")
INVITE_TTL = 24 * 3600


def create_invite(role: str, by: int) -> str:
    code = new_token(12)
    db.execute("INSERT INTO invites(code, role, created_by, expires) VALUES (?,?,?,?)",
               (code, role, by, db.now() + INVITE_TTL))
    db.audit(by, "invite_create", role, code[:6])
    return code


def redeem_invite(code: str, user_id: int, display: str) -> str | None:
    """Consume an invite. Returns the granted role or None."""
    now = db.now()
    with db.tx():
        inv = db.one("SELECT role FROM invites WHERE code=? AND used_by IS NULL AND expires>?", (code, now))
        if not inv:
            return None
        db.execute("UPDATE invites SET used_by=? WHERE code=?", (user_id, code))
        db.execute(
            "INSERT INTO staff(user_id, role, display, added_at, active) VALUES (?,?,?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, active=1, display=excluded.display",
            (user_id, inv["role"], display[:64], now),
        )
    db.audit(user_id, "invite_redeem", inv["role"], code[:6])
    return inv["role"]


def _can_assign(actor: int, role: str) -> bool:
    # admins may add moderators; only the owner may add admins
    return ROLE_RANK[role_of(actor)] > ROLE_RANK[role]


@require("admin")
async def staff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    args = context.args or []
    sub = args[0] if args else "list"
    actor = update.effective_user.id
    msg = update.message

    if sub == "add":
        role = args[1] if len(args) > 1 and args[1] in ASSIGNABLE else "moderator"
        if not _can_assign(actor, role):
            await msg.reply_text("You cannot assign that role.")
            return
        code = create_invite(role, actor)
        await msg.reply_text(
            f"Invite for a new {role} (valid 24h, single use):\n{bot_link(context, 'inv_' + code)}",
            disable_web_page_preview=True,
        )
    elif sub == "list":
        rows = db.q("SELECT user_id, role, display, active, on_shift FROM staff ORDER BY role DESC, added_at")
        if not rows:
            await msg.reply_text("No staff yet. /staff add")
            return
        lines = [f"{r['user_id']} · {r['role']} · {r['display'] or '-'}"
                 f"{'' if r['active'] else ' (inactive)'}{' 🟢' if r['on_shift'] else ''}" for r in rows]
        await msg.reply_text("\n".join(lines))
    elif sub == "role" and len(args) >= 3:
        target, role = _int(args[1]), args[2]
        if target is None or role not in ASSIGNABLE or not _can_assign(actor, role):
            await msg.reply_text("Usage: /staff role <user_id> moderator|admin")
            return
        if ROLE_RANK[role_of(target)] >= ROLE_RANK[role_of(actor)]:
            await msg.reply_text("Cannot change a peer or superior.")
            return
        db.execute("UPDATE staff SET role=? WHERE user_id=?", (role, target))
        db.audit(actor, "staff_role", str(target), role)
        await msg.reply_text("Updated.")
    elif sub == "remove" and len(args) >= 2:
        target = _int(args[1])
        if target is None or ROLE_RANK[role_of(target)] >= ROLE_RANK[role_of(actor)]:
            await msg.reply_text("Cannot remove that user.")
            return
        db.execute("UPDATE staff SET active=0, on_shift=0 WHERE user_id=?", (target,))
        db.audit(actor, "staff_remove", str(target))
        await msg.reply_text("Removed.")
    else:
        await msg.reply_text("/staff add [moderator|admin] · /staff list · /staff role <id> <role> · /staff remove <id>")


def _int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


async def start_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not context.args:
        return
    code = context.args[0][4:]
    user = update.effective_user
    if has_role(user.id, "owner"):
        await update.message.reply_text("You are the owner already.")
        return
    role = redeem_invite(code, user.id, user.full_name or "")
    if role:
        await update.message.reply_text(f"Welcome aboard — you are now a {role}. Try /next.")
    else:
        await update.message.reply_text("This invite is invalid or expired.")


@require("moderator")
async def shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    on = (context.args or ["on"])[0].lower() == "on"
    db.execute("INSERT INTO staff(user_id, role, on_shift, added_at) VALUES (?,?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET on_shift=excluded.on_shift",
               (update.effective_user.id, role_of(update.effective_user.id), int(on), db.now()))
    await update.message.reply_text("Shift on 🟢" if on else "Shift off ⚪")


@require("admin")
async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/audit [user_id] – last 20 actions."""
    if context.args and (uid := _int(context.args[0])) is not None:
        rows = db.q("SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT 20", (uid,))
    else:
        rows = db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 20")
    if not rows:
        await update.message.reply_text("No audit entries.")
        return
    await update.message.reply_text("\n".join(
        f"{fmt_ts(r['at'])} · {r['actor']} · {r['action']} {r['target'] or ''} {r['detail'] or ''}" for r in rows))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("staff", staff_cmd))
    app.add_handler(CommandHandler("shift", shift))
    app.add_handler(CommandHandler("audit", audit_cmd))
    app.add_handler(CommandHandler("start", start_invite, filters=filters.Regex(r"^/start\s+inv_")), group=1)
