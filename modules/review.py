"""Manual review (single stage).

Moderators: /next → atomic claim → Approve / Edit / Reject / Ban / Skip / Escalate.
Approve publishes. Risk-flagged items still publish, but a notice card lands in the
⚠️ Flagged topic with a one-tap Take down (see jobs/poster.py). Rejections land in
🗂 Declined (full text, Restore button) and are purged after `declined_ttl_hours`.
"""
from __future__ import annotations

import json

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from common import alert_admins, category, pending_clear, pending_get, pending_set, staff_reply, fmt_ts, is_private_chat, kb, notify_author, reasons_kb, staff_name, user_lang
from config import cfg
from i18n import t
from security import has_role, require, verify_cb
from textfilter import check_text, highlight, strip_invisible

REJECT_REASONS = ("names", "explicit", "spam", "unclear", "other")
PERMANENT = -1


# ---------------------------------------------------------------------------
# Queue operations (stage 1)
# ---------------------------------------------------------------------------

def claim_next(moderator_id: int, now: int) -> dict | None:
    row = db.one(
        "UPDATE submissions SET claimed_by=?, claimed_at=? WHERE id = ("
        "  SELECT id FROM submissions WHERE claimed_by IS NULL ORDER BY priority DESC, id LIMIT 1"
        ") RETURNING *",
        (moderator_id, now),
    )
    return dict(row) if row else None


def my_claims(moderator_id: int) -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM submissions WHERE claimed_by=? ORDER BY id", (moderator_id,))]


def release(submission_id: int) -> None:
    db.execute("UPDATE submissions SET claimed_by=NULL, claimed_at=NULL WHERE id=?", (submission_id,))


def approve(sub: dict, moderator_id: int) -> int:
    """Move a submission to the outbox, ready to post. Returns the outbox id."""
    hits = sub.get("risk_hits") or "[]"
    now = db.now()
    with db.tx():
        out_id = db.execute(
            "INSERT INTO outbox(kind, author, category_id, text, approved_by, approved_at, stage, risk_hits) "
            "VALUES ('confession',?,?,?,?,?,'ready',?)",
            (sub["author"], sub["category_id"], sub["text"], moderator_id, now, hits),
        )
        db.execute("DELETE FROM submissions WHERE id=?", (sub["id"],))
    db.audit(moderator_id, "approve", f"sub:{sub['id']}", f"outbox:{out_id}")
    return out_id


def decline(item: dict, actor: int, reason: str, source: str) -> int:
    """Record a rejection in `declined` (full text, TTL-purged) and drop the source row."""
    now = db.now()
    with db.tx():
        did = db.execute(
            "INSERT INTO declined(author, category_id, text, text_hash, risk_hits, reason, declined_by, created) VALUES (?,?,?,?,?,?,?,?)",
            (item["author"], item["category_id"], item["text"], item.get("text_hash"), item.get("risk_hits") or "[]",
             reason, actor, now),
        )
        db.execute("DELETE FROM submissions WHERE id=?" if source == "sub" else "DELETE FROM outbox WHERE id=?", (item["id"],))
    db.audit(actor, "reject", f"{source}:{item['id']}", f"{reason} declined:{did}")
    return did


def restore(declined_id: int, actor: int) -> bool:
    row = db.one("SELECT * FROM declined WHERE id=?", (declined_id,))
    if not row:
        return False
    with db.tx():
        db.execute(
            "INSERT OR IGNORE INTO submissions(author, category_id, text, text_hash, risk_hits, priority, created) VALUES (?,?,?,?,?,?,?)",
            (row["author"], row["category_id"], row["text"], row["text_hash"], row["risk_hits"],
             1 if json.loads(row["risk_hits"] or "[]") else 0, db.now()),
        )
        db.execute("DELETE FROM declined WHERE id=?", (declined_id,))
    db.audit(actor, "restore", f"declined:{declined_id}")
    return True


def strike(author: str, moderator_id: int, reason: str) -> int:
    now = db.now()
    strikes = (db.scalar("SELECT strikes FROM users WHERE author=?", (author,), 0) or 0) + 1
    until = 0
    if strikes >= cfg("ban_strikes", 3):
        until = now + cfg("ban_days", 7) * 86400
    db.execute("UPDATE users SET strikes=?, banned_until=CASE WHEN ?>0 THEN ? ELSE banned_until END WHERE author=?",
               (strikes, until, until, author))
    db.audit(moderator_id, "strike", f"author:{author[:12]}", f"{reason} strikes={strikes} until={until}")
    return until


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _strikes(author: str) -> int:
    return db.scalar("SELECT strikes FROM users WHERE author=?", (author,), 0) or 0


def _queue_size() -> int:
    return db.scalar("SELECT count(*) FROM submissions", default=0) or 0


def render_card(sub: dict, queue_size: int) -> str:
    hits = json.loads(sub.get("risk_hits") or "[]")
    cat = category(sub.get("category_id"))
    flag = " ⚠️ FLAGGED" if hits else ""
    return (
        f"#{sub['id']} · {cat['label']}{flag}\n"
        f"author strikes: {_strikes(sub['author'])} · queue: {queue_size}\n"
        f"────────\n{highlight(sub['text'], hits)}\n────────\n"
        "Names a person? Threat/blackmail? Sexual about a real person? → Reject"
    )


def card_kb(sub_id: int):
    s = str(sub_id)
    return kb([
        [("✅ Approve", "rv", f"ok:{s}"), ("✏️ Edit", "rv", f"edit:{s}")],
        [("❌ Reject", "rv", f"rej:{s}"), ("🚫 Ban", "rv", f"ban:{s}")],
        [("⏭ Skip", "rv", f"skip:{s}"), ("🤔 Escalate", "rv", f"esc:{s}"), ("➡️ Next", "rv", "next:0")],
    ])


def _reasons(prefix: str, item_id: int, back: tuple[str, str]):
    return reasons_kb(REJECT_REASONS, prefix, item_id, back, label=lambda r: t("en", f"reject.{r}"))


def ban_kb(sub_id: int):
    return kb([[("Ban + reject", "rv", f"banx:{sub_id}:ban"), ("Permanent", "rv", f"banx:{sub_id}:perm")],
               [("⬅️ Back", "rv", f"show:{sub_id}")]])


def render_declined_card(row: dict) -> str:
    cat = category(row["category_id"])
    return (f"❌ {cat['label']} · {t('en', 'reject.' + (row['reason'] or 'other'))} · by {staff_name(row['declined_by'])}\n"
            f"────────\n{row['text']}")


def declined_kb(declined_id: int):
    return kb([[("↩️ Restore to queue", "dc", f"restore:{declined_id}")]])


async def _send_card(message, sub: dict) -> None:
    await message.reply_text(render_card(sub, _queue_size()), reply_markup=card_kb(sub["id"]))


async def post_declined_card(context, declined_id: int) -> None:
    row = db.one("SELECT * FROM declined WHERE id=?", (declined_id,))
    if not row:
        return
    msg = await alert_admins(context, render_declined_card(dict(row)), kind="declined", reply_markup=declined_kb(declined_id))
    if msg:
        db.execute("UPDATE declined SET group_msg_id=? WHERE id=?", (msg.message_id, declined_id))


async def _finish_card(q, suffix: str) -> None:
    """Edit a group card in place: strip buttons, append the outcome."""
    try:
        await q.message.edit_text((q.message.text or "")[:3800] + f"\n\n{suffix}", reply_markup=None)
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Stage 1 handlers (DM)
# ---------------------------------------------------------------------------

@require("moderator")
async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    count = 1
    if context.args:
        try:
            count = max(1, min(5, int(context.args[0])))
        except ValueError:
            count = 1
    await _serve(update.effective_message, update.effective_user.id, count)


async def _serve(message, moderator_id: int, count: int = 1) -> None:
    served = 0
    for sub in my_claims(moderator_id)[:count]:
        await _send_card(message, sub)
        served += 1
    while served < count:
        sub = claim_next(moderator_id, db.now())
        if not sub:
            if served == 0:
                await message.reply_text("Queue is empty 🎉",
                                         reply_markup=kb([[("🔄 Check again", "rv", "next:0"), ("📊 Stats", "rv", "stats:0")]]))
            return
        await _send_card(message, sub)
        served += 1


def _owned(sub_id: str, moderator_id: int) -> dict | None:
    row = db.one("SELECT * FROM submissions WHERE id=? AND claimed_by=?", (sub_id, moderator_id))
    return dict(row) if row else None


@require("moderator")
async def review_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    parts = parsed[1].split(":")
    action, sub_id = parts[0], parts[1] if len(parts) > 1 else "0"
    extra = parts[2] if len(parts) > 2 else ""
    mod = update.effective_user.id

    if action == "next":
        await q.answer()
        await _serve(q.message, mod)
        return
    if action == "stats":
        await q.answer()
        claimed = db.scalar("SELECT count(*) FROM submissions WHERE claimed_by IS NOT NULL", default=0)
        await q.message.reply_text(f"Queue: {_queue_size()} (claimed {claimed}). Use /stats for the full picture.",
                                   reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))
        return
    if action == "take":
        if not has_role(mod, "admin"):
            await q.answer()
            return
        n = db.execute("UPDATE submissions SET claimed_by=?, claimed_at=? WHERE id=?", (mod, db.now(), sub_id))
        db.audit(mod, "take_over", f"sub:{sub_id}")
        await q.answer("Claimed" if n else "Already handled")
        if n:
            await _serve(q.message, mod)
        return

    sub = _owned(sub_id, mod)
    if not sub:
        await q.answer("Not yours or already handled.", show_alert=True)
        return

    if action == "show":
        pending_clear(context, "editing")
        await q.answer()
        try:
            await q.message.edit_text(render_card(sub, _queue_size()), reply_markup=card_kb(sub["id"]))
        except TelegramError:
            await _send_card(q.message, sub)
    elif action == "ok":
        await _approve(q, sub, mod, context)
    elif action == "edit":
        pending_set(context, "editing", ("sub", sub["id"]), q.message.chat_id)
        await q.answer()
        await q.message.reply_text(f"Send the corrected text for #{sub['id']}.", reply_markup=kb([[("✖️ Cancel edit", "rv", f"show:{sub['id']}")]]))
    elif action == "rej":
        await q.answer()
        await q.message.edit_reply_markup(reply_markup=_reasons("rv", sub["id"], ("rv", f"show:{sub['id']}")))
    elif action == "rejr":
        await _reject(q, sub, mod, context, extra if extra in REJECT_REASONS else "other")
    elif action == "ban":
        await q.answer()
        await q.message.edit_reply_markup(reply_markup=ban_kb(sub["id"]))
    elif action == "banx":
        await _ban(q, sub, mod, context, permanent=(extra == "perm"))
    elif action == "skip":
        release(sub["id"])
        db.audit(mod, "skip", f"sub:{sub['id']}")
        await q.answer("Released")
        await q.message.edit_reply_markup(reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))
    elif action == "esc":
        await q.answer("Escalated")
        await alert_admins(context, f"🤔 Escalation from {staff_name(mod)} for submission #{sub['id']}:\n\n{sub['text']}",
                           kind="flagged", reply_markup=kb([[("Take over", "rv", f"take:{sub['id']}")]]))


async def _approve(q, sub, mod, context) -> None:
    hits = json.loads(sub.get("risk_hits") or "[]")
    if hits and cfg("flagged_requires_admin", False) and not has_role(mod, "admin"):
        await q.answer("Flagged items need an admin. Use Escalate.", show_alert=True)
        return
    approve(sub, mod)
    await q.answer("Approved")
    note = " (flagged — admins get a notice when it posts)" if hits else ""
    await q.message.edit_text(f"✅ Approved #{sub['id']} — queued for posting{note}", reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


async def _reject(q, sub, mod, context, reason: str) -> None:
    did = decline(sub, mod, reason, "sub")
    await post_declined_card(context, did)
    await q.answer("Rejected")
    lang = user_lang(sub["author"])
    await notify_author(context, sub["author"], t(lang, "notify.rejected", reason=t(lang, f"reject.{reason}")))
    await q.message.edit_text(f"❌ Rejected #{sub['id']} ({reason})", reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


async def _ban(q, sub, mod, context, permanent: bool) -> None:
    if permanent and not has_role(mod, "admin"):
        await q.answer("Only admins can ban permanently.", show_alert=True)
        return
    did = decline(sub, mod, "ban", "sub")
    await post_declined_card(context, did)
    if permanent:
        db.execute("UPDATE users SET banned_until=? WHERE author=?", (PERMANENT, sub["author"]))
        db.audit(mod, "ban_permanent", f"author:{sub['author'][:12]}")
        until_txt = "forever"
    else:
        until = strike(sub["author"], mod, "ban")
        until_txt = fmt_ts(until) if until else "strike only"
    await q.answer("Done")
    if until_txt != "strike only":
        await notify_author(context, sub["author"], t(user_lang(sub["author"]), "notify.banned", until=until_txt))
    await q.message.edit_text(f"🚫 #{sub['id']} rejected; author: {until_txt}", reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


@require("admin")
async def declined_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    action, _, did = parsed[1].partition(":")
    if action == "restore" and did.isdigit() and restore(int(did), update.effective_user.id):
        await q.answer("Back in the queue")
        await _finish_card(q, f"↩️ restored to queue by {staff_name(update.effective_user.id)}")
    else:
        await q.answer("Gone.", show_alert=True)
        await _finish_card(q, "⌛ no longer available")


# ---------------------------------------------------------------------------
# Shared text editing (DM for stage 1, group reply for stage 2)
# ---------------------------------------------------------------------------

@require("moderator")
async def edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    editing = pending_get(context, "editing", update.effective_chat.id)
    if not editing:
        return
    kind, item_id = editing
    if kind != "sub":
        pending_clear(context, "editing")
        return
    actor = update.effective_user.id
    new_text = strip_invisible(update.message.text or "").strip()
    result = check_text(new_text, min_len=cfg("min_len", 20), max_len=cfg("max_len", 2000), blocklist=[], risklist=[])
    if not result.ok:
        await update.message.reply_text(f"Edited text rejected: {result.reason}")
        return
    if kind == "sub":
        sub = _owned(str(item_id), actor)
        if not sub:
            pending_clear(context, "editing")
            return
        db.execute("UPDATE submissions SET text=? WHERE id=?", (new_text, item_id))
        sub["text"] = new_text
        pending_clear(context, "editing")
        db.audit(actor, "edit", f"sub:{item_id}")
        await _send_card(update.message, sub)
@require("moderator")
async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if pending_clear(context, "editing"):
        await update.message.reply_text("Edit cancelled.")


@require("admin")
async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await staff_reply(update, "Usage: /unban <author prefix>")
        return
    prefix = context.args[0][:64]
    rows = db.q("SELECT author FROM users WHERE author LIKE ? || '%' AND (banned_until>? OR banned_until=-1)", (prefix, db.now()))
    if len(rows) != 1:
        await staff_reply(update, f"{len(rows)} matches; be more specific.")
        return
    db.execute("UPDATE users SET banned_until=0, strikes=0 WHERE author=?", (rows[0]["author"],))
    db.audit(update.effective_user.id, "unban", f"author:{prefix}")
    await staff_reply(update, "Unbanned.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^📋 Queue$") & filters.ChatType.PRIVATE, next_cmd))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CallbackQueryHandler(review_cb, pattern=r"^rv:"))
    app.add_handler(CallbackQueryHandler(declined_cb, pattern=r"^dc:"))
    app.add_handler(CommandHandler("cancel", cancel_edit), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text), group=2)
