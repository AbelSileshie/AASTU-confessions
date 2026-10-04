"""Non-developer setup: detect chats the bot is added to, assign roles with one tap,
and a readiness checklist (⚙️ Admin → 🧭 Setup, and DM'd to the owner at startup when red).

Chat ids chosen here are stored in `settings` (chat_channel / chat_admin_group / chat_backup)
and override the .env values at runtime, so an owner never has to find a -100… id by hand.
"""
from __future__ import annotations

import dataclasses

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, ContextTypes

import db
from common import TOPICS, kb
from config import cfg, invalidate
from fmt import Rich
from security import require, verify_cb

ROLE_KEYS = {"channel": "chat_channel", "admin": "chat_admin_group", "backup": "chat_backup"}
ROLE_LABELS = {"channel": "📣 Confessions channel", "admin": "🛡 Admin group", "backup": "💾 Backup channel"}


# ---------------------------------------------------------------------------
# Env overrides
# ---------------------------------------------------------------------------

def apply_overrides(env):
    """Return env with chat ids replaced by owner-chosen ones from settings (if any)."""
    changes = {}
    for role, key in ROLE_KEYS.items():
        v = cfg(key, 0)
        if v:
            changes[{"channel": "channel_id", "admin": "admin_group_id", "backup": "backup_channel_id"}[role]] = int(v)
    return dataclasses.replace(env, **changes) if changes else env


def assign(context: ContextTypes.DEFAULT_TYPE, role: str, chat_id: int, actor: int) -> None:
    db.execute("UPDATE settings SET value=?, updated_by=?, updated_at=? WHERE key=?", (str(chat_id), actor, db.now(), ROLE_KEYS[role]))
    invalidate(ROLE_KEYS[role])
    context.bot_data["env"] = apply_overrides(context.bot_data["env"])
    if role == "admin":  # topics belong to the old group
        for k in TOPICS:
            db.execute("UPDATE settings SET value='0' WHERE key=?", (f"topic_{k}",))
            invalidate(f"topic_{k}")
    if role == "channel":
        context.bot_data.pop("channel_info", None)
        db.execute("DELETE FROM meta WHERE k='channel_info'")
    db.audit(actor, "chat_assign", role, str(chat_id)[-6:])


# ---------------------------------------------------------------------------
# Detection: the bot was added to / promoted in a chat
# ---------------------------------------------------------------------------

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cm = update.my_chat_member
    if not cm or cm.chat.type == "private":
        return
    status = cm.new_chat_member.status
    if status not in ("administrator", "member"):
        return
    env = context.bot_data["env"]
    if not env.owner_id:
        return
    chat = cm.chat
    kind = "channel" if chat.type == "channel" else "group"
    text = (f"➕ I was added to the {kind} “{chat.title}” as {status}.\n"
            f"What should I use it for?")
    options = [("channel", "backup")] if kind == "channel" else [("admin",)]
    rows = [[(ROLE_LABELS[r], "setup", f"assign:{r}:{chat.id}") for r in group] for group in options]
    rows.append([("Nothing", "setup", "ignore")])
    try:
        await context.bot.send_message(env.owner_id, text, reply_markup=kb(rows))
    except TelegramError:
        pass


@require("owner")
async def setup_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    if not parsed:
        return
    parts = parsed[1].split(":")
    if parts[0] == "assign" and len(parts) == 3 and parts[1] in ROLE_KEYS:
        try:
            chat_id = int(parts[2])
        except ValueError:
            return
        assign(context, parts[1], chat_id, update.effective_user.id)
        await q.message.edit_text(f"✅ Using this chat as {ROLE_LABELS[parts[1]]}.", reply_markup=kb([[("🧭 Setup checklist", "setup", "check")]]))
    elif parts[0] == "ignore":
        await q.message.edit_text("Okay, ignored.", reply_markup=None)
    elif parts[0] == "check":
        text, ents, ok = await checklist(context)
        try:
            await q.message.edit_text(text, entities=ents, reply_markup=kb([[("🔄 Re-check", "setup", "check"), ("⬅️ Admin", "hub", "home")]]))
        except TelegramError:
            await q.message.reply_text(text, entities=ents, reply_markup=kb([[("🔄 Re-check", "setup", "check"), ("⬅️ Admin", "hub", "home")]]))


# ---------------------------------------------------------------------------
# Readiness checklist
# ---------------------------------------------------------------------------

async def _bot_rights(context, chat_id: int) -> tuple[bool, str]:
    if not chat_id:
        return False, "not set"
    try:
        me = await context.bot.get_chat_member(chat_id, context.bot.id)
    except TelegramError as exc:
        return False, f"cannot access ({type(exc).__name__})"
    if me.status == "creator":
        return True, "owner"
    if me.status != "administrator":
        return False, f"bot is {me.status}, needs admin"
    missing = [n for n, ok in (("post", getattr(me, "can_post_messages", True)), ("edit", getattr(me, "can_edit_messages", True)),
                               ("delete", getattr(me, "can_delete_messages", True)), ("pin", getattr(me, "can_pin_messages", True)))
               if ok is False]
    return (not missing), ("admin ✓" if not missing else "admin, missing: " + ", ".join(missing))


async def checklist(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, list, bool]:
    env = context.bot_data["env"]
    items: list[tuple[bool, str, str]] = []
    ok, note = await _bot_rights(context, env.channel_id)
    items.append((ok, "Confessions channel", note))
    ok, note = await _bot_rights(context, env.admin_group_id)
    items.append((ok, "Admin group", note))
    topics_ok = all(cfg(f"topic_{k}", 0) for k in TOPICS)
    items.append((topics_ok, "Group topics", "created" if topics_ok else "pending (Topics on + Manage topics right)"))
    staff = db.scalar("SELECT count(*) FROM staff WHERE active=1", default=0)
    items.append((staff > 0, "Moderators", f"{staff} active" if staff else "none — ⚙️ Admin → Staff → Invite"))
    items.append((bool(cfg("rules_text", "")), "Rules text", "set" if cfg("rules_text", "") else "empty"))
    ok, note = await _bot_rights(context, env.backup_channel_id)
    items.append((ok and bool(env.age_recipient), "Backups", note if not ok else ("ready" if env.age_recipient else "AGE_RECIPIENT missing in .env")))
    if cfg("ads_enabled", True):
        payouts = db.scalar("SELECT count(*) FROM payout_accounts WHERE enabled=1", default=0)
        items.append((payouts > 0, "Payout accounts", f"{payouts}" if payouts else "none — ⚙️ Admin → Payout accounts"))
        items.append((bool(env.verify_api_key), "Verify.et API key", "set" if env.verify_api_key else "VERIFY_API_KEY missing in .env"))
        items.append((bool(env.verify_webhook_secret), "Verify.et webhook secret", "set" if env.verify_webhook_secret else "missing — webhooks rejected"))
    all_ok = all(i[0] for i in items)
    r = Rich().bold("🧭 Setup checklist").nl().italic("Everything green = ready to launch." if all_ok else "Fix the red items before launch.")
    for good, name, note in items:
        r.nl().text("✅ " if good else "❌ ").bold(name).text(f" — {note}")
    text, ents = r.build()
    return text, ents, all_ok


async def startup_check(app: Application) -> None:
    """DM the owner the checklist at startup when something is not ready."""
    env = app.bot_data["env"]
    if not env.owner_id:
        return
    ctx = ContextTypes.DEFAULT_TYPE(app)
    try:
        text, ents, ok = await checklist(ctx)
    except Exception:  # noqa: BLE001
        return
    if not ok:
        try:
            await app.bot.send_message(env.owner_id, text, entities=ents, reply_markup=kb([[("🔄 Re-check", "setup", "check")]]))
        except TelegramError:
            pass


def register(app: Application) -> None:
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(setup_cb, pattern=r"^setup:"))
