"""/stats, /top, /mediakit."""
from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
from common import is_private_chat, kb, post_link, staff_name, staff_reply
from ops import gather_stats, health_text
from security import require


@require("moderator")
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update):
        text, ents = health_text(db.now(), staff_name)
        await update.message.reply_text(text, entities=ents, reply_markup=kb([[("📋 Queue", "rv", "next:0")]]))


def top_posts(days: int = 7, limit: int = 5) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT number, channel_msg_id, comment_count FROM posts WHERE kind='confession' AND deleted=0 AND posted_at>? "
        "ORDER BY comment_count DESC, number DESC LIMIT ?", (db.now() - days * 86400, limit))]


@require("moderator")
async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = top_posts()
    if not rows:
        await staff_reply(update, "Nothing this week.")
        return
    lines = [f"#{r['number']} · 💬 {r['comment_count']} · {post_link(context, r['channel_msg_id'])}" for r in rows]
    await staff_reply(update, "🏆 Top this week\n" + "\n".join(lines), disable_web_page_preview=True)


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
    await staff_reply(
        update,
        f"📣 Media kit\nSubscribers: {subs}\nPosts/7d: {s['posted_7d']}\nComments/day: {s['comments_today']}\n"
        f"Audience: AASTU students\n\nPackages:\n{pk}\n\nOrder via the bot → 📢 Advertise."
    )


def register(app: Application) -> None:
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^📊 Stats$") & filters.ChatType.PRIVATE, stats_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("mediakit", mediakit_cmd))
