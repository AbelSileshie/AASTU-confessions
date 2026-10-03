"""Periodic maintenance: claims, bans, retention, counters, alerts, order expiry."""
from __future__ import annotations

import logging

from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
from common import alert_admins
from config import cfg
from i18n import t

log = logging.getLogger(__name__)

RETENTION = {
    "hidden_comments_days": 90,
    "payments_raw_days": 365,
    "audit_days": 180,
    "webhook_events_days": 7,
}


def release_stale_claims(now: int) -> int:
    cutoff = now - cfg("claim_timeout_sec", 600)
    return db.execute("UPDATE submissions SET claimed_by=NULL, claimed_at=NULL WHERE claimed_at IS NOT NULL AND claimed_at<?",
                      (cutoff,))


def expire_submissions(now: int) -> list[str]:
    ttl = cfg("submission_ttl_hours", 0)
    if not ttl:
        return []
    rows = db.q("SELECT id, author FROM submissions WHERE created<? AND claimed_by IS NULL", (now - ttl * 3600,))
    if rows:
        placeholders = ",".join("?" * len(rows))
        db.execute(f"DELETE FROM submissions WHERE id IN ({placeholders})", [r["id"] for r in rows])  # nosec B608 - placeholders only
    return [r["author"] for r in rows]


def expire_invites(now: int) -> None:
    db.execute("DELETE FROM invites WHERE expires<? OR used_by IS NOT NULL", (now,))


def purge(now: int) -> None:
    day = 86400
    db.execute("DELETE FROM comments WHERE hidden=1 AND hidden_at IS NOT NULL AND hidden_at<?",
               (now - RETENTION["hidden_comments_days"] * day,))
    db.execute("UPDATE payments SET raw=NULL WHERE raw IS NOT NULL AND created<?",
               (now - RETENTION["payments_raw_days"] * day,))
    db.execute("DELETE FROM audit WHERE at<?", (now - RETENTION["audit_days"] * day,))
    db.execute("DELETE FROM webhook_events WHERE received_at<?", (now - RETENTION["webhook_events_days"] * day,))
    db.execute("DELETE FROM banners WHERE ends<?", (now - 30 * day,))


def expire_orders(now: int) -> list[dict]:
    hours = cfg("ad_payment_timeout_hours", 48)
    rows = db.q("SELECT a.id, adv.user_id FROM ads a JOIN advertisers adv ON adv.id=a.advertiser_id "
                "WHERE a.status IN ('draft','awaiting_payment') AND a.created<?", (now - hours * 3600,))
    for r in rows:
        db.execute("UPDATE ads SET status='cancelled', reject_reason='payment_timeout' WHERE id=?", (r["id"],))
    return [dict(r) for r in rows]


def expire_connects(now: int) -> None:
    ttl = cfg("connect_ttl_hours", 168)
    db.execute("UPDATE connect_requests SET status='expired', resolved=? WHERE status='pending' AND created<?",
               (now, now - ttl * 3600))


def expired_declined(now: int) -> list[dict]:
    ttl = cfg("declined_ttl_hours", 72)
    return [dict(r) for r in db.q("SELECT id, group_msg_id FROM declined WHERE created<?", (now - ttl * 3600,))]


async def purge_declined(context, now: int) -> int:
    """Delete expired Declined cards from the group and the rows (the only copy of that text)."""
    env = context.bot_data["env"]
    rows = expired_declined(now)
    for r in rows:
        if r["group_msg_id"] and env.admin_group_id:
            try:
                await context.bot.delete_message(env.admin_group_id, r["group_msg_id"])
            except TelegramError:
                pass
        db.execute("DELETE FROM declined WHERE id=?", (r["id"],))
    return len(rows)


async def housekeeping_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = db.now()
    try:
        release_stale_claims(now)
        await purge_declined(context, now)
        expire_submissions(now)
        expire_invites(now)
        expire_connects(now)
        for order in expire_orders(now):
            try:
                await context.bot.send_message(order["user_id"], t("en", "ads.expired", ad_id=order["id"]))
            except TelegramError:
                pass
        await _queue_alert(context)
    except Exception:  # noqa: BLE001
        log.exception("housekeeping failed")


async def daily_purge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    purge(db.now())
    db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def _queue_alert(context) -> None:
    size = db.scalar("SELECT count(*) FROM submissions", default=0) or 0
    threshold = cfg("queue_alert_threshold", 200)
    last = int(db.scalar("SELECT v FROM meta WHERE k='queue_alert_at'", default="0") or 0)
    now = db.now()
    if size >= threshold and now - last > 3600:
        on_shift = db.scalar("SELECT count(*) FROM staff WHERE on_shift=1 AND active=1", default=0) or 0
        await alert_admins(context, f"⚠️ Review queue is at {size} (threshold {threshold}). Moderators on shift: {on_shift}.")
        db.execute("INSERT INTO meta(k, v) VALUES ('queue_alert_at', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(now),))


# ---------------------------------------------------------------------------
# Comment counter refresh on channel buttons
# ---------------------------------------------------------------------------

async def counter_refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    from jobs.poster import comments_button

    env = context.bot_data["env"]
    now = db.now()
    min_gap = cfg("counter_refresh_sec", 300)
    rows = db.q(
        "SELECT number, channel_msg_id, comment_count FROM posts WHERE deleted=0 AND kind='confession' "
        "AND comment_count<>counter_shown AND counter_updated<? ORDER BY counter_updated LIMIT 10",
        (now - min_gap,),
    )
    for r in rows:
        try:
            await context.bot.edit_message_reply_markup(
                env.channel_id, r["channel_msg_id"], reply_markup=comments_button(context, r["number"], r["comment_count"]))
        except TelegramError as exc:
            log.debug("counter refresh skipped: %s", type(exc).__name__)
        db.execute("UPDATE posts SET counter_shown=?, counter_updated=? WHERE number=?", (r["comment_count"], now, r["number"]))
