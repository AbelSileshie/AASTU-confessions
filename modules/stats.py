"""/stats, /top, /mediakit."""
from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from common import is_private_chat, post_link
from security import require


def gather_stats(now: int) -> dict:
    day = now - 86400
    week = now - 7 * 86400
    month_start = now - 30 * 86400
    return {
        "queue": db.scalar("SELECT count(*) FROM submissions", default=0),
        "claimed": db.scalar("SELECT count(*) FROM submissions WHERE claimed_by IS NOT NULL", default=0),
        "outbox": db.scalar("SELECT count(*) FROM outbox", default=0),
        "posted_today": db.scalar("SELECT count(*) FROM posts WHERE posted_at>?", (day,), 0),
        "posted_7d": db.scalar("SELECT count(*) FROM posts WHERE posted_at>?", (week,), 0),
        "comments_today": db.scalar("SELECT count(*) FROM comments WHERE created>?", (day,), 0),
        "pending_ads": db.scalar("SELECT count(*) FROM ads WHERE status IN ('paid','needs_review')", default=0),
        "revenue_30d": db.scalar(
            "SELECT coalesce(sum(amount),0) FROM payments WHERE status IN ('verified','manual_ok') AND completed_at>?",
            (month_start,), 0),
        "on_shift": db.scalar("SELECT count(*) FROM staff WHERE on_shift=1 AND active=1", default=0),
        "mods_today": [dict(r) for r in db.q(
            "SELECT actor, count(*) n FROM audit WHERE action IN ('approve','reject') AND at>? GROUP BY actor ORDER BY n DESC",
            (day,))],
    }


@require("moderator")
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    s = gather_stats(db.now())
    mods = "\n".join(f"  {m['actor']}: {m['n']}" for m in s["mods_today"]) or "  –"
    await update.message.reply_text(
        f"Queue: {s['queue']} (claimed {s['claimed']}) · Outbox: {s['outbox']}\n"
        f"Posted: {s['posted_today']} today · {s['posted_7d']} / 7d\n"
        f"Comments today: {s['comments_today']}\n"
        f"Pending ads: {s['pending_ads']} · Revenue 30d: {s['revenue_30d']} ETB\n"
        f"On shift: {s['on_shift']}\nDecisions today:\n{mods}"
    )


def top_posts(days: int = 7, limit: int = 5) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT number, channel_msg_id, comment_count FROM posts WHERE kind='confession' AND deleted=0 AND posted_at>? "
        "ORDER BY comment_count DESC, number DESC LIMIT ?", (db.now() - days * 86400, limit))]


@require("moderator")
async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = top_posts()
    if not rows:
        await update.message.reply_text("Nothing this week.")
        return
    lines = [f"#{r['number']} · 💬 {r['comment_count']} · {post_link(context, r['channel_msg_id'])}" for r in rows]
    await update.message.reply_text("🏆 Top this week\n" + "\n".join(lines), disable_web_page_preview=True)


@require("admin")
async def mediakit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    env = context.bot_data["env"]
    try:
        subs = await context.bot.get_chat_member_count(env.channel_id)
    except TelegramError:
        subs = "n/a"
    s = gather_stats(db.now())
    pkgs = db.q("SELECT name, price_etb, description FROM packages WHERE enabled=1 ORDER BY sort")
    pk = "\n".join(f"• {p['name']} — {p['price_etb']} ETB: {p['description']}" for p in pkgs)
    await update.message.reply_text(
        f"📣 Media kit\nSubscribers: {subs}\nPosts/7d: {s['posted_7d']}\nComments/day: {s['comments_today']}\n"
        f"Audience: AASTU students\n\nPackages:\n{pk}\n\nOrder via the bot → 📢 Advertise."
    )


def register(app: Application) -> None:
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("mediakit", mediakit_cmd))
