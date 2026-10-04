"""Outbox → channel. Runs every 15 s; posts at most one item per interval."""
from __future__ import annotations

import asyncio
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from fmt import CAPTION_LIMIT, Rich, card
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

import db
from common import alert_admins, bot_link, hashtags_of, kb, live_buttons, notify_author, post_link, staff_name, user_lang
from config import cfg
from i18n import t

log = logging.getLogger(__name__)
MAX_ATTEMPTS_BEFORE_ALERT = 5


# ---------------------------------------------------------------------------
# Selection (pure given DB state)
# ---------------------------------------------------------------------------

def _meta(k: str, default: int = 0) -> int:
    v = db.scalar("SELECT v FROM meta WHERE k=?", (k,))
    return int(v) if v is not None else default


def _set_meta(k: str, v: int) -> None:
    db.execute("INSERT INTO meta(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def ad_allowed(now: int) -> bool:
    today = now // 86400
    ads_today = _meta("ads_today") if _meta("ads_day") == today else 0
    return ads_today < cfg("max_ads_per_day", 3) and _meta("posts_since_ad") >= cfg("min_posts_between_ads", 6)


def pick_next(now: int) -> dict | None:
    if ad_allowed(now):
        ad = db.one("SELECT * FROM outbox WHERE kind='ad' AND stage='ready' AND not_before<=? ORDER BY id LIMIT 1", (now,))
        if ad:
            return dict(ad)
    row = db.one("SELECT * FROM outbox WHERE kind IN ('confession','system') AND stage='ready' AND not_before<=? ORDER BY id LIMIT 1", (now,))
    return dict(row) if row else None


def due(now: int) -> bool:
    return now - _meta("last_post_at") >= cfg("post_interval_sec", 120)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_confession(number: int, text: str, hashtags: str = "") -> tuple[str, list[MessageEntity]]:
    """Bold header, literal body, hashtags on their own line (omitted when none).
    Long posts fold into an expandable quote so the channel stays scannable."""
    r = Rich().bold(f"Confession #{number}").nl(2)
    if len(text) > 700:
        r.quote(text, expandable=True)
    else:
        r.text(text)
    if hashtags:
        r.nl(2).text(hashtags)
    return r.build()


def format_ad(text: str, bot_username: str = "") -> tuple[str, list[MessageEntity]]:
    """Sponsored label on top (disclosure), advertiser text literal, 'Advertise with us' footer."""
    r = Rich().bold(cfg("ad_footer", "📢 Sponsored")).nl(2).text(text).nl(2)
    url = f"https://t.me/{bot_username}?start=advertise" if bot_username else ""
    r.link("Advertise with us ↗", url) if url else r.italic("Advertise with us via the bot")
    return r.build(limit=CAPTION_LIMIT)


def comments_button(context: ContextTypes.DEFAULT_TYPE, number: int, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"💬 Comments ({count})", url=bot_link(context, f"c_{number}"))]])


def _ad_markup(buttons_json: str | None) -> InlineKeyboardMarkup | None:
    if not buttons_json:
        return None
    try:
        btns = json.loads(buttons_json)
        rows = [[InlineKeyboardButton(str(b["text"])[:64], url=b["url"])]
                for b in btns if isinstance(b, dict) and str(b.get("url", "")).startswith("https://")]
    except (ValueError, KeyError, TypeError):
        return None
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

async def poster_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = db.now()
    if not due(now):
        return
    item = pick_next(now)
    if not item:
        return
    try:
        if item["kind"] == "ad":
            await _post_ad(context, item, now)
        else:
            await _post_confession(context, item, now)
    except RetryAfter as exc:
        await asyncio.sleep(min(exc.retry_after, 60))
    except TelegramError as exc:
        attempts = (item["attempts"] or 0) + 1
        db.execute("UPDATE outbox SET attempts=?, last_error=? WHERE id=?", (attempts, type(exc).__name__, item["id"]))
        log.warning("post failed (%s) outbox=%s", type(exc).__name__, item["id"])
        if attempts == MAX_ATTEMPTS_BEFORE_ALERT:
            await alert_admins(context, f"❗ Outbox item {item['id']} failed {attempts} times: {type(exc).__name__}")


def _reserve_number() -> int:
    return (db.scalar("SELECT max(number) FROM posts", default=0) or 0) + 1


async def _post_confession(context, item: dict, now: int) -> None:
    env = context.bot_data["env"]
    number = _reserve_number()
    text, entities = format_confession(number, item["text"], hashtags_of(item))
    msg = await context.bot.send_message(
        env.channel_id, text, entities=entities, reply_markup=comments_button(context, number, 0),
        disable_web_page_preview=True,
    )
    hits = json.loads(item.get("risk_hits") or "[]")
    with db.tx():
        db.execute(
            "INSERT INTO posts(number, kind, channel_msg_id, author, category_id, category_ids, posted_at, approved_by, risk_hits) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (number, item["kind"], msg.message_id, item["author"], item["category_id"], item.get("category_ids") or "[]", now,
             item.get("approved_by"), json.dumps(hits)),
        )
        db.execute("DELETE FROM outbox WHERE id=?", (item["id"],))
        _set_meta("last_post_at", now)
        _set_meta("posts_since_ad", _meta("posts_since_ad") + 1)
    if item["author"]:
        lang = user_lang(item["author"])
        await notify_author(context, item["author"], t(lang, "notify.live", number=number),
                            reply_markup=live_buttons(context, lang, number, msg.message_id), disable_web_page_preview=True)
    if hits:
        ftext, fents = (card(f"⚠️ Flagged post is live · #{number}",
                             [("Words", ", ".join(hits)), ("Approved by", staff_name(item.get("approved_by")))],
                             item["text"], body_limit=400)
                        .nl(2).link("Open post ↗", post_link(context, msg.message_id)).build())
        await alert_admins(context, ftext, kind="flagged", entities=fents, disable_web_page_preview=True,
                           reply_markup=kb([[("🛑 Take down", "td", f"go:{number}"), ("✅ Leave it up", "td", f"ignore:{number}")]]))


async def _post_ad(context, item: dict, now: int) -> None:
    env = context.bot_data["env"]
    number = _reserve_number()
    text, entities = format_ad(item["text"] or "", context.bot_data.get("bot_username", ""))
    markup = _ad_markup(item["buttons"])
    if item["media_file_id"]:
        msg = await context.bot.send_photo(env.channel_id, item["media_file_id"], caption=text,
                                           caption_entities=entities, reply_markup=markup)
    else:
        msg = await context.bot.send_message(env.channel_id, text, entities=entities, reply_markup=markup,
                                             disable_web_page_preview=True)
    ad = db.one("SELECT * FROM ads WHERE id=?", (item["ref_id"],))
    pkg = db.one("SELECT pin_hours FROM packages WHERE id=?", (ad["package_id"],)) if ad else None
    pinned_until = None
    if pkg and pkg["pin_hours"]:
        try:
            await context.bot.pin_chat_message(env.channel_id, msg.message_id, disable_notification=True)
            pinned_until = now + pkg["pin_hours"] * 3600
        except TelegramError:
            pinned_until = None
    today = now // 86400
    with db.tx():
        db.execute("INSERT INTO posts(number, kind, channel_msg_id, posted_at) VALUES (?,?,?,?)",
                   (number, "ad", msg.message_id, now))
        db.execute("INSERT INTO ad_posts(ad_id, post_number, pinned_until) VALUES (?,?,?)",
                   (item["ref_id"], number, pinned_until))
        db.execute("DELETE FROM outbox WHERE id=?", (item["id"],))
        _set_meta("last_post_at", now)
        _set_meta("posts_since_ad", 0)
        _set_meta("ads_today", (_meta("ads_today") if _meta("ads_day") == today else 0) + 1)
        _set_meta("ads_day", today)
        if ad:
            left = (ad["posts_left"] or 1) - 1
            status = "done" if left <= 0 else "live"
            db.execute("UPDATE ads SET posts_left=?, status=? WHERE id=?", (left, status, ad["id"]))
    if ad:
        adv = db.one("SELECT user_id FROM advertisers WHERE id=?", (ad["advertiser_id"],))
        if adv:
            try:
                await context.bot.send_message(adv["user_id"], t("en", "ads.live"),
                                               reply_markup=kb([[("📣 Open in channel", post_link(context, msg.message_id))]]))
            except TelegramError:
                pass


async def unpin_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    env = context.bot_data["env"]
    now = db.now()
    rows = db.q("SELECT ap.id, p.channel_msg_id FROM ad_posts ap JOIN posts p ON p.number=ap.post_number "
                "WHERE ap.pinned_until IS NOT NULL AND ap.pinned_until<=?", (now,))
    for r in rows:
        try:
            await context.bot.unpin_chat_message(env.channel_id, r["channel_msg_id"])
        except TelegramError:
            pass
        db.execute("UPDATE ad_posts SET pinned_until=NULL WHERE id=?", (r["id"],))
