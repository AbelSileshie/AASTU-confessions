"""Manual review queue: /next, atomic claim, approve/edit/reject/ban/skip/escalate."""
from __future__ import annotations

import json

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from common import alert_admins, category, fmt_ts, is_private_chat, kb, notify_author, user_lang
from config import cfg
from i18n import t
from security import has_role, require, verify_cb
from textfilter import check_text, highlight, strip_invisible

REJECT_REASONS = ("names", "explicit", "spam", "unclear", "other")
PERMANENT = -1


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def claim_next(moderator_id: int, now: int) -> dict | None:
    """Atomically claim the highest-priority unclaimed submission."""
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
    """Move a submission to the outbox. Returns outbox id."""
    now = db.now()
    with db.tx():
        out_id = db.execute(
            "INSERT INTO outbox(kind, author, category_id, text, approved_by, approved_at) VALUES ('confession',?,?,?,?,?)",
            (sub["author"], sub["category_id"], sub["text"], moderator_id, now),
        )
        db.execute("DELETE FROM submissions WHERE id=?", (sub["id"],))
    db.audit(moderator_id, "approve", f"sub:{sub['id']}", f"outbox:{out_id}")
    return out_id


def reject(sub: dict, moderator_id: int, reason: str) -> None:
    with db.tx():
        db.execute("DELETE FROM submissions WHERE id=?", (sub["id"],))
    db.audit(moderator_id, "reject", f"sub:{sub['id']}", reason)


def strike(author: str, moderator_id: int, reason: str) -> int:
    """Add a strike; ban when the threshold is reached. Returns banned_until."""
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
# Card rendering
# ---------------------------------------------------------------------------

def render_card(sub: dict, queue_size: int) -> str:
    hits = json.loads(sub.get("risk_hits") or "[]")
    cat = category(sub.get("category_id"))
    strikes = db.scalar("SELECT strikes FROM users WHERE author=?", (sub["author"],), 0) or 0
    flag = " ⚠️ FLAGGED" if hits else ""
    body = highlight(sub["text"], hits)
    return (
        f"#{sub['id']} · {cat['label']}{flag}\n"
        f"author strikes: {strikes} · queue: {queue_size}\n"
        f"────────\n{body}\n────────\n"
        "Names a person? Threat/blackmail? Sexual about a real person? → Reject"
    )


def card_kb(sub_id: int):
    s = str(sub_id)
    return kb([
        [("✅ Approve", "rv", f"ok:{s}"), ("✏️ Edit", "rv", f"edit:{s}")],
        [("❌ Reject", "rv", f"rej:{s}"), ("🚫 Ban", "rv", f"ban:{s}")],
        [("⏭ Skip", "rv", f"skip:{s}"), ("🤔 Escalate", "rv", f"esc:{s}"), ("➡️ Next", "rv", "next:0")],
    ])


def reasons_kb(sub_id: int):
    rows = [[(t("en", f"reject.{r}"), "rv", f"rejr:{sub_id}:{r}") for r in REJECT_REASONS[:3]],
            [(t("en", f"reject.{r}"), "rv", f"rejr:{sub_id}:{r}") for r in REJECT_REASONS[3:]],
            [("⬅️ Back", "rv", f"show:{sub_id}")]]
    return kb(rows)


def ban_kb(sub_id: int):
    return kb([[("Ban + reject", "rv", f"banx:{sub_id}:ban"), ("Permanent", "rv", f"banx:{sub_id}:perm")],
               [("⬅️ Back", "rv", f"show:{sub_id}")]])


def _queue_size() -> int:
    return db.scalar("SELECT count(*) FROM submissions", default=0) or 0


async def _send_card(message, sub: dict) -> None:
    await message.reply_text(render_card(sub, _queue_size()), reply_markup=card_kb(sub["id"]))


# ---------------------------------------------------------------------------
# Handlers
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
    existing = my_claims(moderator_id)
    served = 0
    for sub in existing[:count]:
        await _send_card(message, sub)
        served += 1
    while served < count:
        sub = claim_next(moderator_id, db.now())
        if not sub:
            if served == 0:
                await message.reply_text("Queue is empty 🎉", reply_markup=kb([[("🔄 Check again", "rv", "next:0"), ("📊 Stats", "rv", "stats:0")]]))
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
        size = _queue_size()
        claimed = db.scalar("SELECT count(*) FROM submissions WHERE claimed_by IS NOT NULL", default=0)
        await q.message.reply_text(f"Queue: {size} (claimed {claimed}). Use /stats for the full picture.",
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
        await q.answer()
        await q.message.edit_text(render_card(sub, _queue_size()), reply_markup=card_kb(sub["id"]))
    elif action == "ok":
        await _approve(q, sub, mod, context)
    elif action == "edit":
        context.user_data["editing"] = sub["id"]
        await q.answer()
        await q.message.reply_text(f"Send the corrected text for #{sub['id']} (or /cancel).")
    elif action == "rej":
        await q.answer()
        await q.message.edit_reply_markup(reply_markup=reasons_kb(sub["id"]))
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
        await alert_admins(context, f"🤔 Escalation from moderator for submission #{sub['id']}:\n\n{sub['text']}",
                           reply_markup=kb([[("Take over", "rv", f"take:{sub['id']}")]]))


async def _approve(q, sub, mod, context) -> None:
    hits = json.loads(sub.get("risk_hits") or "[]")
    if hits and cfg("flagged_requires_admin", False) and not has_role(mod, "admin"):
        await q.answer("Flagged items need an admin. Use Escalate.", show_alert=True)
        return
    approve(sub, mod)
    await q.answer("Approved")
    await q.message.edit_text(f"✅ Approved #{sub['id']} (queued for posting)",
                              reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


async def _reject(q, sub, mod, context, reason: str) -> None:
    reject(sub, mod, reason)
    await q.answer("Rejected")
    lang = user_lang(sub["author"])
    await notify_author(context, sub["author"], t(lang, "notify.rejected", reason=t(lang, f"reject.{reason}")))
    await q.message.edit_text(f"❌ Rejected #{sub['id']} ({reason})", reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


async def _ban(q, sub, mod, context, permanent: bool) -> None:
    reject(sub, mod, "ban")
    if permanent:
        if not has_role(mod, "admin"):
            await q.answer("Only admins can ban permanently.", show_alert=True)
            return
        db.execute("UPDATE users SET banned_until=? WHERE author=?", (PERMANENT, sub["author"]))
        db.audit(mod, "ban_permanent", f"author:{sub['author'][:12]}")
        until_txt = "forever"
    else:
        until = strike(sub["author"], mod, "ban")
        until_txt = fmt_ts(until) if until else "strike only"
    await q.answer("Done")
    lang = user_lang(sub["author"])
    if until_txt != "strike only":
        await notify_author(context, sub["author"], t(lang, "notify.banned", until=until_txt))
    await q.message.edit_text(f"🚫 #{sub['id']} rejected; author: {until_txt}",
                              reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


@require("moderator")
async def edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub_id = context.user_data.get("editing")
    if not sub_id or not is_private_chat(update):
        return
    sub = _owned(str(sub_id), update.effective_user.id)
    if not sub:
        context.user_data.pop("editing", None)
        return
    new_text = strip_invisible(update.message.text or "").strip()
    result = check_text(new_text, min_len=cfg("min_len", 20), max_len=cfg("max_len", 2000), blocklist=[], risklist=[])
    if not result.ok:
        await update.message.reply_text(f"Edited text rejected: {result.reason}")
        return
    db.execute("UPDATE submissions SET text=? WHERE id=?", (new_text, sub["id"]))
    db.audit(update.effective_user.id, "edit", f"sub:{sub['id']}")
    context.user_data.pop("editing", None)
    sub["text"] = new_text
    await _send_card(update.message, sub)


@require("moderator")
async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("editing", None):
        await update.message.reply_text("Edit cancelled.")


@require("admin")
async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unban <author-hash-prefix>  (prefix shown on cards/audit)."""
    if not context.args:
        await update.message.reply_text("Usage: /unban <author prefix>")
        return
    prefix = context.args[0][:64]
    rows = db.q("SELECT author FROM users WHERE author LIKE ? || '%' AND (banned_until>? OR banned_until=-1)",
                (prefix, db.now()))
    if len(rows) != 1:
        await update.message.reply_text(f"{len(rows)} matches; be more specific.")
        return
    db.execute("UPDATE users SET banned_until=0, strikes=0 WHERE author=?", (rows[0]["author"],))
    db.audit(update.effective_user.id, "unban", f"author:{prefix}")
    await update.message.reply_text("Unbanned.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CallbackQueryHandler(review_cb, pattern=r"^rv:"))
    app.add_handler(CommandHandler("cancel", cancel_edit), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, edit_text), group=2)
