"""Manual review.

Stage 1 (DM, moderators): /next → atomic claim → Approve / Edit / Reject / Ban / Skip / Escalate.
Stage 2 (admin group topics, admins): approved items become ONE card in 📋 Final review
(or ⚠️ Flagged when risk words hit) with Post / Edit / Reject. The first admin to act
wins atomically; the card is edited in place so nobody acts twice. Rejections land in
🗂 Declined with a Restore button and are purged after `declined_ttl_hours`.
"""
from __future__ import annotations

import json

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from common import alert_admins, category, fmt_ts, is_private_chat, kb, notify_author, user_lang
from config import cfg
from i18n import t
from security import has_role, require, role_of, verify_cb
from textfilter import check_text, highlight, strip_invisible

REJECT_REASONS = ("names", "explicit", "spam", "unclear", "other")
PERMANENT = -1
SNIPPET = 300


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


def stage_for(hits: list[str], approver_role: str) -> str:
    """Where an approved item goes next (pure)."""
    if hits:
        return "flagged"
    if cfg("final_review", True) and approver_role not in ("admin", "owner"):
        return "final"
    return "ready"


def approve(sub: dict, moderator_id: int) -> tuple[int, str]:
    """Move a submission to the outbox at the right stage. Returns (outbox id, stage)."""
    hits = json.loads(sub.get("risk_hits") or "[]")
    stage = stage_for(hits, role_of(moderator_id))
    now = db.now()
    with db.tx():
        out_id = db.execute(
            "INSERT INTO outbox(kind, author, category_id, text, approved_by, approved_at, stage, risk_hits) "
            "VALUES ('confession',?,?,?,?,?,?,?)",
            (sub["author"], sub["category_id"], sub["text"], moderator_id, now, stage, json.dumps(hits)),
        )
        db.execute("DELETE FROM submissions WHERE id=?", (sub["id"],))
    db.audit(moderator_id, "approve", f"sub:{sub['id']}", f"outbox:{out_id} stage:{stage}")
    return out_id, stage


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


def claim_final(outbox_id: int, admin_id: int) -> bool:
    """Atomic stage-2 approval: exactly one admin wins."""
    row = db.one("UPDATE outbox SET stage='ready', final_by=?, final_at=? WHERE id=? AND stage IN ('final','flagged') RETURNING id",
                 (admin_id, db.now(), outbox_id))
    if row:
        db.audit(admin_id, "final_post", f"outbox:{outbox_id}")
    return row is not None


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _strikes(author: str) -> int:
    return db.scalar("SELECT strikes FROM users WHERE author=?", (author,), 0) or 0


def _queue_size() -> int:
    return db.scalar("SELECT count(*) FROM submissions", default=0) or 0


def _name(user_id: int | None) -> str:
    row = db.one("SELECT display FROM staff WHERE user_id=?", (user_id,)) if user_id else None
    return (row["display"] if row and row["display"] else str(user_id or "?"))[:24]


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
    rows = [[(t("en", f"reject.{r}"), prefix, f"rejr:{item_id}:{r}") for r in REJECT_REASONS[:3]],
            [(t("en", f"reject.{r}"), prefix, f"rejr:{item_id}:{r}") for r in REJECT_REASONS[3:]],
            [("⬅️ Back", back[0], back[1])]]
    return kb(rows)


def ban_kb(sub_id: int):
    return kb([[("Ban + reject", "rv", f"banx:{sub_id}:ban"), ("Permanent", "rv", f"banx:{sub_id}:perm")],
               [("⬅️ Back", "rv", f"show:{sub_id}")]])


def render_final_card(item: dict) -> str:
    hits = json.loads(item.get("risk_hits") or "[]")
    cat = category(item.get("category_id"))
    text = item["text"]
    body = highlight(text if len(text) <= SNIPPET else text[:SNIPPET] + "…", hits)
    head = "⚠️ FLAGGED · " if hits else ""
    return (f"{head}{cat['label']} · by {_name(item['approved_by'])} · strikes {_strikes(item['author'])}\n"
            f"────────\n{body}")


def final_kb(outbox_id: int):
    s = str(outbox_id)
    return kb([[("✅ Post", "fr", f"post:{s}"), ("✏️ Edit", "fr", f"edit:{s}"), ("❌ Reject", "fr", f"rej:{s}")]])


def render_declined_card(row: dict) -> str:
    cat = category(row["category_id"])
    return (f"❌ {cat['label']} · {t('en', 'reject.' + (row['reason'] or 'other'))} · by {_name(row['declined_by'])}\n"
            f"────────\n{row['text']}")


def declined_kb(declined_id: int):
    return kb([[("↩️ Restore to queue", "dc", f"restore:{declined_id}")]])


async def _send_card(message, sub: dict) -> None:
    await message.reply_text(render_card(sub, _queue_size()), reply_markup=card_kb(sub["id"]))


async def post_final_card(context, outbox_id: int) -> None:
    item = db.one("SELECT * FROM outbox WHERE id=?", (outbox_id,))
    if not item:
        return
    kind = "flagged" if item["stage"] == "flagged" else "review"
    msg = await alert_admins(context, render_final_card(dict(item)), kind=kind, reply_markup=final_kb(outbox_id))
    if msg:
        db.execute("UPDATE outbox SET group_msg_id=? WHERE id=?", (msg.message_id, outbox_id))


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
        await q.answer()
        await q.message.edit_text(render_card(sub, _queue_size()), reply_markup=card_kb(sub["id"]))
    elif action == "ok":
        await _approve(q, sub, mod, context)
    elif action == "edit":
        context.user_data["editing"] = ("sub", sub["id"])
        await q.answer()
        await q.message.reply_text(f"Send the corrected text for #{sub['id']} (or /cancel).")
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
        await alert_admins(context, f"🤔 Escalation from {_name(mod)} for submission #{sub['id']}:\n\n{sub['text']}",
                           kind="flagged", reply_markup=kb([[("Take over", "rv", f"take:{sub['id']}")]]))


async def _approve(q, sub, mod, context) -> None:
    hits = json.loads(sub.get("risk_hits") or "[]")
    if hits and cfg("flagged_requires_admin", False) and not has_role(mod, "admin"):
        await q.answer("Flagged items need an admin. Use Escalate.", show_alert=True)
        return
    out_id, stage = approve(sub, mod)
    if stage != "ready":
        await post_final_card(context, out_id)
    await q.answer("Approved")
    where = {"ready": "queued for posting", "final": "sent to admins for final review", "flagged": "sent to admins (flagged)"}[stage]
    await q.message.edit_text(f"✅ Approved #{sub['id']} — {where}", reply_markup=kb([[("➡️ Next", "rv", "next:0")]]))


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


# ---------------------------------------------------------------------------
# Stage 2 handlers (admin group cards)
# ---------------------------------------------------------------------------

@require("admin")
async def final_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    parts = parsed[1].split(":")
    action, out_id = parts[0], int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    extra = parts[2] if len(parts) > 2 else ""
    admin = update.effective_user.id
    item = db.one("SELECT * FROM outbox WHERE id=?", (out_id,))
    if not item or item["stage"] == "ready":
        await q.answer("Already handled.", show_alert=True)
        done_by = _name(item["final_by"]) if item else "?"
        await _finish_card(q, f"✅ posted by {done_by}")
        return

    if action == "post":
        if claim_final(out_id, admin):
            await q.answer("Posting")
            await _finish_card(q, f"✅ approved for posting by {_name(admin)}")
        else:
            await q.answer("Someone else just handled this.", show_alert=True)
    elif action == "show":
        await q.answer()
        try:
            await q.message.edit_text(render_final_card(dict(item)), reply_markup=final_kb(out_id))
        except TelegramError:
            pass
    elif action == "edit":
        context.user_data["editing"] = ("out", out_id)
        await q.answer()
        await q.message.reply_text(f"{_name(admin)}: reply here with the corrected text (or /cancel).\n\nCurrent:\n{item['text']}")
    elif action == "rej":
        await q.answer()
        await q.message.edit_reply_markup(reply_markup=_reasons("fr", out_id, ("fr", f"show:{out_id}")))
    elif action == "rejr":
        reason = extra if extra in REJECT_REASONS else "other"
        did = decline(dict(item), admin, reason, "out")
        await post_declined_card(context, did)
        lang = user_lang(item["author"])
        await notify_author(context, item["author"], t(lang, "notify.rejected", reason=t(lang, f"reject.{reason}")))
        await q.answer("Rejected")
        await _finish_card(q, f"❌ rejected ({reason}) by {_name(admin)}")


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
        await _finish_card(q, f"↩️ restored to queue by {_name(update.effective_user.id)}")
    else:
        await q.answer("Gone.", show_alert=True)
        await _finish_card(q, "⌛ no longer available")


# ---------------------------------------------------------------------------
# Shared text editing (DM for stage 1, group reply for stage 2)
# ---------------------------------------------------------------------------

@require("moderator")
async def edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    editing = context.user_data.get("editing")
    if not editing:
        return
    kind, item_id = editing
    actor = update.effective_user.id
    new_text = strip_invisible(update.message.text or "").strip()
    result = check_text(new_text, min_len=cfg("min_len", 20), max_len=cfg("max_len", 2000), blocklist=[], risklist=[])
    if not result.ok:
        await update.message.reply_text(f"Edited text rejected: {result.reason}")
        return
    if kind == "sub":
        sub = _owned(str(item_id), actor)
        if not sub:
            context.user_data.pop("editing", None)
            return
        db.execute("UPDATE submissions SET text=? WHERE id=?", (new_text, item_id))
        sub["text"] = new_text
        context.user_data.pop("editing", None)
        db.audit(actor, "edit", f"sub:{item_id}")
        await _send_card(update.message, sub)
        return
    # outbox edit from the group: admins only, item must still be awaiting final review
    if not has_role(actor, "admin"):
        return
    item = db.one("SELECT * FROM outbox WHERE id=? AND stage IN ('final','flagged')", (item_id,))
    context.user_data.pop("editing", None)
    if not item:
        await update.message.reply_text("That item was already handled.")
        return
    db.execute("UPDATE outbox SET text=? WHERE id=?", (new_text, item_id))
    db.audit(actor, "edit", f"outbox:{item_id}")
    env = context.bot_data["env"]
    if item["group_msg_id"]:
        try:
            await context.bot.edit_message_text(render_final_card({**dict(item), "text": new_text}), env.admin_group_id,
                                                item["group_msg_id"], reply_markup=final_kb(item_id))
        except TelegramError:
            pass
    await update.message.reply_text("Updated. Use the card's buttons to Post or Reject.")


@require("moderator")
async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("editing", None):
        await update.message.reply_text("Edit cancelled.")


@require("admin")
async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /unban <author prefix>")
        return
    prefix = context.args[0][:64]
    rows = db.q("SELECT author FROM users WHERE author LIKE ? || '%' AND (banned_until>? OR banned_until=-1)", (prefix, db.now()))
    if len(rows) != 1:
        await update.message.reply_text(f"{len(rows)} matches; be more specific.")
        return
    db.execute("UPDATE users SET banned_until=0, strikes=0 WHERE author=?", (rows[0]["author"],))
    db.audit(update.effective_user.id, "unban", f"author:{prefix}")
    await update.message.reply_text("Unbanned.")


def register(app: Application) -> None:
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^📋 Queue$") & filters.ChatType.PRIVATE, next_cmd))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CallbackQueryHandler(review_cb, pattern=r"^rv:"))
    app.add_handler(CallbackQueryHandler(final_cb, pattern=r"^fr:"))
    app.add_handler(CallbackQueryHandler(declined_cb, pattern=r"^dc:"))
    app.add_handler(CommandHandler("cancel", cancel_edit), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text), group=2)
