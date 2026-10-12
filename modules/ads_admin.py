"""Admin ad tools: /ads queue, approve/reject/schedule, manual payment override, /ad_report, /revenue, /refunds."""
from __future__ import annotations

import json

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from common import (
    ad_card,
    kb,
    parse_int,
    pending_clear,
    pending_get,
    pending_set,
    post_link,
    reasons_kb,
    send_ad_cards,
    staff_reply,
    user_lang,
)
from i18n import t
from security import author_hash, require, verify_cb

AD_REJECT_REASONS = ("policy", "scam", "adult", "betting", "quality", "other")
PAYMENT_REJECT_REASONS = ("wrong_receiver", "underpaid", "duplicate_reference", "other")


def _review_chat_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    env = context.bot_data.get("env")
    return bool(chat and (chat.type == "private" or (env and chat.id == env.admin_group_id)))


def _payment_reject_label(reason: str) -> str:
    return t("en", "ads.payment_reject." + reason)


# ---------------------------------------------------------------------------
# Transitions (single choke point)
# ---------------------------------------------------------------------------

ALLOWED = {
    "paid": {"approved", "rejected"},
    "needs_review": {"paid", "rejected", "cancelled"},
    "approved": {"scheduled", "rejected"},
    "scheduled": {"live", "done", "rejected"},
    "live": {"done"},
    "rejected": {"refund_due"},
    "refund_due": {"cancelled"},
}


def transition(ad_id: int, to: str, actor: int, detail: str = "") -> bool:
    row = db.one("SELECT status FROM ads WHERE id=?", (ad_id,))
    if not row or to not in ALLOWED.get(row["status"], set()):
        return False
    db.execute("UPDATE ads SET status=? WHERE id=?", (to, ad_id))
    db.audit(actor, f"ad_{to}", f"ad:{ad_id}", detail)
    return True


def schedule(ad_id: int, actor: int, not_before: int = 0) -> bool:
    ad = db.one("SELECT * FROM ads WHERE id=? AND status='approved'", (ad_id,))
    if not ad:
        return False
    pkg = db.one("SELECT * FROM packages WHERE id=?", (ad["package_id"],))
    buttons = json.dumps([{"text": ad["button_text"], "url": ad["button_url"]}]) if ad["button_text"] and ad["button_url"] else None
    posts = max(1, ad["posts_left"] or pkg["posts"] or 1) if pkg["posts"] else 0
    with db.tx():
        gap = (7 * 86400) // (posts - 1) if posts > 1 else 0
        for i in range(posts):
            db.execute("INSERT INTO outbox(kind, text, media_file_id, buttons, ref_id, not_before, approved_by, approved_at) "
                       "VALUES ('ad',?,?,?,?,?,?,?)",
                       (ad["text"], ad["media_file_id"], buttons, ad_id, not_before + i * gap, actor, db.now()))
        if pkg["bot_banner_days"]:
            start = max(not_before, db.now())
            db.execute("INSERT INTO banners(ad_id, text, url, starts, ends) VALUES (?,?,?,?,?)",
                       (ad_id, ad["text"][:120], ad["button_url"], start, start + pkg["bot_banner_days"] * 86400))
        db.execute("UPDATE ads SET status=?, scheduled_at=?, approved_by=?, approved_at=? WHERE id=?",
                   ("scheduled" if posts else "done", not_before, actor, db.now(), ad_id))
    db.audit(actor, "ad_scheduled", f"ad:{ad_id}", f"posts={posts}")
    return True


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@require("admin")
async def ads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _review_chat_allowed(update, context):
        return
    if not await send_ad_cards(update.message):
        await update.message.reply_text("No ads waiting.", reply_markup=kb([[("⬅️ Admin", "hub", "home")]]))


def _ad_reason_label(reason: str, lang: str) -> str:
    namespace = "ads.payment_reject" if reason in PAYMENT_REJECT_REASONS else "ads.reject_reason"
    return t(lang, f"{namespace}.{reason}")


async def _send_advertiser(context, user_id: int, text: str) -> None:
    try:
        await context.bot.send_message(user_id, text, disable_web_page_preview=True)
    except TelegramError:
        pass


async def _notify_advertiser(context, order_id: int, key: str, **values) -> None:
    uid = db.scalar("SELECT adv.user_id FROM ads a JOIN advertisers adv ON adv.id=a.advertiser_id WHERE a.id=?", (order_id,))
    if uid:
        lang = user_lang(author_hash(uid))
        if key in ("ads.rejected", "ads.payment_declined"):
            values["reason"] = _ad_reason_label(values["reason"], lang)
        await _send_advertiser(context, uid, t(lang, key, **values))


@require("admin")
async def ads_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _review_chat_allowed(update, context):
        await q.answer()
        return
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    parts = parsed[1].split(":")
    action, ad_id = parts[0], parse_int(parts[1]) if len(parts) > 1 else None
    extra = parts[2] if len(parts) > 2 else ""
    actor = update.effective_user.id
    if ad_id is None:
        await q.answer()
        return

    if action == "approve":
        if transition(ad_id, "approved", actor) and schedule(ad_id, actor):
            await q.answer("Scheduled")
            await _notify_advertiser(context, ad_id, "ads.approved", ad_id=ad_id)
            await _edit(q, "✅ approved & scheduled")
        else:
            await q.answer("Not in a state that can be approved.", show_alert=True)
    elif action == "later":
        if transition(ad_id, "approved", actor) and schedule(ad_id, actor, not_before=db.now() + 86400):
            await q.answer("Scheduled for +24h")
            await _notify_advertiser(context, ad_id, "ads.approved", ad_id=ad_id)
            await _edit(q, "🕐 scheduled +24h")
        else:
            await q.answer("Cannot schedule.", show_alert=True)
    elif action == "rej":
        await q.answer()
        await q.message.edit_reply_markup(reply_markup=reasons_kb(AD_REJECT_REASONS, "ada", ad_id, None))
    elif action == "rejr":
        reason = extra if extra in AD_REJECT_REASONS else "other"
        paid = db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) in ("paid", "approved", "scheduled")
        if transition(ad_id, "rejected", actor, reason):
            db.execute("UPDATE ads SET reject_reason=? WHERE id=?", (reason, ad_id))
            if paid:
                transition(ad_id, "refund_due", actor, "auto")
            db.execute("DELETE FROM outbox WHERE kind='ad' AND ref_id=?", (ad_id,))
            db.execute("DELETE FROM banners WHERE ad_id=?", (ad_id,))
            await q.answer("Rejected")
            reason_label = _ad_reason_label(reason, "en")
            await _notify_advertiser(context, ad_id, "ads.rejected", ad_id=ad_id, reason=reason)
            await _edit(q, f"❌ rejected ({reason_label}){' · refund due' if paid else ''}")
        else:
            await q.answer("Cannot reject.", show_alert=True)
    elif action == "manual_ok":
        p = db.one("SELECT id FROM payments WHERE ad_id=? ORDER BY id DESC LIMIT 1", (ad_id,))
        if p and transition(ad_id, "paid", actor, "manual_ok"):
            try:
                db.execute("UPDATE payments SET status='manual_ok', completed_at=? WHERE id=?", (db.now(), p["id"]))
            except db.sqlite3.IntegrityError:
                db.execute("UPDATE ads SET status='needs_review' WHERE id=?", (ad_id,))
                await q.answer("That reference is already used by a verified payment.", show_alert=True)
                return
            db.execute("UPDATE ads SET paid_at=? WHERE id=?", (db.now(), ad_id))
            await q.answer("Marked paid")
            await _notify_advertiser(context, ad_id, "ads.verified")
            ad = dict(db.one("SELECT * FROM ads WHERE id=?", (ad_id,)))
            await _edit(q, "💰 payment accepted manually", markup=ad_card(ad)[1])  # keyboard only
        else:
            await q.answer("Cannot accept.", show_alert=True)
    elif action == "manual_no":
        await q.answer()
        half = (len(PAYMENT_REJECT_REASONS) + 1) // 2
        reason_rows = [[(_payment_reject_label(reason), "ada", f"manual_nor:{ad_id}:{reason}")
                        for reason in PAYMENT_REJECT_REASONS[:half]],
                       [(_payment_reject_label(reason), "ada", f"manual_nor:{ad_id}:{reason}")
                        for reason in PAYMENT_REJECT_REASONS[half:]]]
        await q.message.edit_reply_markup(reply_markup=kb(reason_rows))
    elif action == "manual_nor":
        reason = extra if extra in PAYMENT_REJECT_REASONS else "other"
        p = db.one("SELECT id FROM payments WHERE ad_id=? ORDER BY id DESC LIMIT 1", (ad_id,))
        if transition(ad_id, "cancelled", actor, f"manual_no:{reason}"):
            db.execute("UPDATE ads SET reject_reason=? WHERE id=?", (reason, ad_id))
            if p:
                db.execute("UPDATE payments SET status='manual_no', completed_at=? WHERE id=?", (db.now(), p["id"]))
            await q.answer("Declined")
            reason_text = _payment_reject_label(reason)
            await _notify_advertiser(context, ad_id, "ads.payment_declined", ad_id=ad_id, reason=reason)
            await _edit(q, f"🚫 payment declined ({reason_text}); order cancelled")
        else:
            await q.answer("Cannot decline.", show_alert=True)


@require("admin")
async def reply_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """↩️ Reply on an advertiser message → next text from this admin is DM'd to the advertiser."""
    q = update.callback_query
    if not _review_chat_allowed(update, context):
        await q.answer()
        return
    parsed = verify_cb(q.data)
    await q.answer()
    if parsed and parsed[1] == "cancel":
        pending_clear(context, "ad_reply")
        try:
            await q.message.edit_text((q.message.text or "") + "\n\n✖️ cancelled", reply_markup=None)
        except TelegramError:
            pass
        return
    if not parsed or not parsed[1].isdigit():
        return
    pending_set(context, "ad_reply", int(parsed[1]), q.message.chat_id)
    await q.message.reply_text(f"Reply to the advertiser of ad #{parsed[1]} — send the text here.",
                               reply_markup=kb([[("✖️ Cancel", "adr", "cancel")]]))


@require("admin")
async def reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ad_id = pending_get(context, "ad_reply", update.effective_chat.id)
    if ad_id is None:
        return
    pending_clear(context, "ad_reply")
    text = (update.message.text or "").strip()[:1500]
    uid = db.scalar("SELECT adv.user_id FROM ads a JOIN advertisers adv ON adv.id=a.advertiser_id WHERE a.id=?", (ad_id,))
    if uid:
        await _send_advertiser(context, uid, f"💬 Admin reply about your ad #{ad_id}:\n\n{text}")
    db.audit(update.effective_user.id, "ad_reply", f"ad:{ad_id}")
    await update.message.reply_text("Sent to the advertiser.")


@require("admin")
async def cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if pending_clear(context, "ad_reply"):
        await update.message.reply_text("Reply cancelled.")


async def _edit(q, suffix: str, markup=None) -> None:
    """Append an italic outcome line, keeping the card's existing styling."""
    from fmt import Rich

    try:
        if q.message.caption is not None:
            base = q.message.caption or ""
            text, ents = Rich().text(base).nl(2).italic(suffix).build(limit=1024)
            await q.message.edit_caption(caption=text, caption_entities=list(q.message.caption_entities or []) + ents, reply_markup=markup)
        else:
            base = q.message.text or ""
            text, ents = Rich().text(base).nl(2).italic(suffix).build()
            await q.message.edit_text(text, entities=list(q.message.entities or []) + ents, reply_markup=markup)
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def ad_report(context, ad_id: int) -> str:
    posts = db.q("SELECT p.channel_msg_id FROM ad_posts ap JOIN posts p ON p.number=ap.post_number WHERE ap.ad_id=?", (ad_id,))
    links = "\n".join(post_link(context, p["channel_msg_id"]) for p in posts) or "-"
    subs = context.bot_data.get("channel_members", "n/a")
    return t("en", "ads.report", ad_id=ad_id, posts=len(posts), subs=subs, links=links)


@require("admin")
async def ad_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ad_id = parse_int(context.args[0]) if context.args else None
    if ad_id is None or not db.one("SELECT 1 FROM ads WHERE id=?", (ad_id,)):
        await staff_reply(update, "Usage: /ad_report <ad id>")
        return
    await staff_reply(update, ad_report(context, ad_id), disable_web_page_preview=True)


@require("admin")
async def revenue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = parse_int(context.args[0]) if context.args else 30
    since = db.now() - (days or 30) * 86400
    rows = db.q("SELECT pk.name, p.bank, count(*) n, sum(p.amount) total FROM payments p JOIN ads a ON a.id=p.ad_id "
                "JOIN packages pk ON pk.id=a.package_id WHERE p.status IN ('verified','manual_ok') AND p.completed_at>? "
                "GROUP BY pk.name, p.bank ORDER BY total DESC", (since,))
    total = sum(r["total"] or 0 for r in rows)
    body = "\n".join(f"{r['name']} via {r['bank']}: {r['n']} × → {r['total']} ETB" for r in rows) or "no revenue"
    await staff_reply(update, f"Revenue last {days or 30} days: {total} ETB\n{body}")


@require("owner")
async def refunds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0] == "done" and len(context.args) > 1:
        ad_id = parse_int(context.args[1])
        if ad_id is not None and transition(ad_id, "cancelled", update.effective_user.id, "refunded"):
            await staff_reply(update, f"Ad #{ad_id} marked refunded.")
        else:
            await staff_reply(update, "Not in refund_due.")
        return
    rows = db.q("SELECT a.id, a.price_etb, p.bank, p.reference, p.sender_name FROM ads a LEFT JOIN payments p ON p.ad_id=a.id "
                "AND p.status IN ('verified','manual_ok') WHERE a.status='refund_due' ORDER BY a.id")
    body = "\n".join(f"#{r['id']} · {r['price_etb']} ETB · {r['bank']} {r['reference']} · {r['sender_name'] or '?'}" for r in rows) or "none"
    await staff_reply(update, f"Refunds due:\n{body}\n\n/refunds done <ad id> when paid back.")


async def daily_ad_reports_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """24h after an ad's last post: send the advertiser a report once."""
    env = context.bot_data["env"]
    try:
        context.bot_data["channel_members"] = await context.bot.get_chat_member_count(env.channel_id)
    except TelegramError:
        pass
    rows = db.q("SELECT a.id, adv.user_id FROM ads a JOIN advertisers adv ON adv.id=a.advertiser_id "
                "WHERE a.status='done' AND a.id NOT IN (SELECT CAST(target AS INT) FROM audit WHERE action='ad_report_sent') "
                "AND EXISTS (SELECT 1 FROM ad_posts ap JOIN posts p ON p.number=ap.post_number WHERE ap.ad_id=a.id AND p.posted_at<?)",
                (db.now() - 86400,))
    for r in rows:
        try:
            await context.bot.send_message(r["user_id"], ad_report(context, r["id"]), disable_web_page_preview=True)
        except TelegramError:
            pass
        db.audit(0, "ad_report_sent", str(r["id"]))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("ads", ads_cmd))
    app.add_handler(CommandHandler("ad_report", ad_report_cmd))
    app.add_handler(CommandHandler("revenue", revenue_cmd))
    app.add_handler(CommandHandler("refunds", refunds_cmd))
    app.add_handler(CallbackQueryHandler(ads_cb, pattern=r"^ada:"))
    app.add_handler(CallbackQueryHandler(reply_cb, pattern=r"^adr:"))
    app.add_handler(CommandHandler("cancel", cancel_reply), group=5)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_text), group=5)
