"""Shared helpers for modules: user rows, notifications, keyboards, parsing.

Modules may import this (it is part of the shared layer with db/security/
textfilter/config/i18n). Modules never import each other.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Iterable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

import db
from config import cfg
from i18n import SUPPORTED, t
from security import author_hash, decrypt_ref, encrypt_ref, is_private_chat, role_of, sign_cb  # noqa: F401 - re-exported

log = logging.getLogger(__name__)

PERMANENT_BAN = -1


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def ensure_user(telegram_id: int) -> str:
    """Upsert the anonymous user row; returns the author hash."""
    author = author_hash(telegram_id)
    ref = encrypt_ref(telegram_id) if cfg("notify_authors", True) else None
    db.execute(
        "INSERT INTO users(author, chat_ref, created) VALUES (?,?,?) "
        "ON CONFLICT(author) DO UPDATE SET chat_ref=COALESCE(excluded.chat_ref, users.chat_ref)",
        (author, ref, db.now()),
    )
    return author


def user_lang(author: str) -> str:
    return db.scalar("SELECT lang FROM users WHERE author=?", (author,), "en") or "en"


def lang_of(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "en"
    return user_lang(author_hash(user.id))


def is_banned(author: str) -> bool:
    until = db.scalar("SELECT banned_until FROM users WHERE author=?", (author,), 0) or 0
    return until == PERMANENT_BAN or until > db.now()


async def notify_author(context: ContextTypes.DEFAULT_TYPE, author: str, text: str, **kw) -> bool:
    """DM a user via the encrypted chat_ref. Returns False if impossible."""
    if not cfg("notify_authors", True):
        return False
    blob = db.scalar("SELECT chat_ref FROM users WHERE author=?", (author,))
    chat_id = decrypt_ref(blob)
    if chat_id is None:
        return False
    try:
        await context.bot.send_message(chat_id, text, **kw)
        return True
    except Forbidden:
        db.execute("UPDATE users SET chat_ref=NULL WHERE author=?", (author,))
        return False
    except TelegramError as exc:
        log.warning("notify failed: %s", type(exc).__name__)
        return False


TOPICS = ("flagged", "takedowns", "declined", "ads", "alerts")
TOPIC_NAMES = {"flagged": "⚠️ Flagged", "takedowns": "🛑 Takedowns",
               "declined": "🗂 Declined", "ads": "💰 Ads", "alerts": "🔔 Alerts"}


async def ensure_topics(context: ContextTypes.DEFAULT_TYPE, actor: int = 0, force: bool = False) -> dict:
    """Create any missing admin-group topics and remember their thread ids.

    Runs at startup and hourly (housekeeping), and from ⚙️ Admin → Group setup. If the group
    is not a forum (Topics off) or the bot lacks 'Manage topics', the owner is told once
    (flag in meta) and everything keeps flowing into the main chat.
    Returns {"created": [...], "missing": [...], "error": str|None}."""
    from config import invalidate

    env = context.bot_data.get("env")
    result = {"created": [], "missing": [k for k in TOPICS if not cfg(f"topic_{k}", 0)], "error": None}
    if not env or not env.admin_group_id or not result["missing"]:
        return result
    try:
        chat = await context.bot.get_chat(env.admin_group_id)
        if not getattr(chat, "is_forum", False):
            result["error"] = "topics_disabled"
    except TelegramError as exc:
        result["error"] = type(exc).__name__
    if not result["error"]:
        for k in list(result["missing"]):
            try:
                topic = await context.bot.create_forum_topic(env.admin_group_id, TOPIC_NAMES[k])
            except TelegramError as exc:
                result["error"] = type(exc).__name__
                break
            db.execute("UPDATE settings SET value=?, updated_by=?, updated_at=? WHERE key=?",
                       (str(topic.message_thread_id), actor, db.now(), f"topic_{k}"))
            invalidate(f"topic_{k}")
            result["created"].append(k)
            result["missing"].remove(k)
    if result["created"]:
        db.audit(actor, "topics_create", detail=",".join(result["created"]))
    if result["error"] and env.owner_id:
        warned = db.scalar("SELECT v FROM meta WHERE k='topics_warned'")
        if force or not warned:
            db.execute("INSERT INTO meta(k, v) VALUES ('topics_warned', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(db.now()),))
            hint = ("Topics are not enabled in the admin group. Open the group → Edit → Topics → on, and give the bot the "
                    "'Manage topics' admin right. The bot retries every hour; alerts go to the main chat until then."
                    if result["error"] == "topics_disabled" else
                    f"Could not create admin-group topics ({result['error']}). Check the bot's admin rights; it retries hourly.")
            try:
                await context.bot.send_message(env.owner_id, "⚠️ " + hint)
            except TelegramError:
                pass
    elif not result["missing"]:
        db.execute("DELETE FROM meta WHERE k='topics_warned'")
    return result


def topic_id(kind: str) -> int | None:
    """Forum thread id for an alert kind, or None → main chat."""
    tid = cfg(f"topic_{kind}", 0) if kind in TOPICS else 0
    return tid or None


async def alert_admins(context: ContextTypes.DEFAULT_TYPE, text: str, kind: str = "alerts", **kw):
    """Post to the admin group, routed into the topic for `kind` when configured.
    Returns the sent Message or None."""
    group = context.bot_data.get("env").admin_group_id
    if not group:
        return None
    tid = topic_id(kind)
    try:
        return await context.bot.send_message(group, text, message_thread_id=tid, **kw)
    except TelegramError as exc:
        log.warning("admin alert failed (%s): %s", kind, type(exc).__name__)
        if tid:  # topic may have been deleted → fall back to main chat and forget the id
            db.execute("UPDATE settings SET value='0' WHERE key=?", (f"topic_{kind}",))
            from config import invalidate
            invalidate(f"topic_{kind}")
            try:
                return await context.bot.send_message(group, text, **kw)
            except TelegramError:
                return None
        return None


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb(rows: Iterable[Sequence[tuple[str, str] | tuple[str, str, str]]]) -> InlineKeyboardMarkup:
    """rows of (label, action, payload) → signed callback buttons.
    A 2-tuple (label, url) with an https url becomes a URL button."""
    out = []
    for row in rows:
        btns = []
        for item in row:
            if len(item) == 2 and item[1].startswith("https://"):
                btns.append(InlineKeyboardButton(item[0], url=item[1]))
            else:
                label, action = item[0], item[1]
                payload = item[2] if len(item) > 2 else ""
                btns.append(InlineKeyboardButton(label, callback_data=sign_cb(action, payload)))
        out.append(btns)
    return InlineKeyboardMarkup(out)


# ---------------------------------------------------------------------------
# Navigation: persistent bottom keyboard + in-flow exits
# ---------------------------------------------------------------------------

# label key → action. Labels are resolved per language; routing is language-agnostic.
NAV_ACTIONS = {
    "menu.confess": "confess",

    "menu.mine": "mine",
    "menu.advertise": "advertise",
    "menu.rules": "rules",
    "menu.help": "help",
    "menu.lang": "lang",
    "menu.cancel": "cancel",
}


def nav_keyboard(lang: str, user_id: int | None = None) -> ReplyKeyboardMarkup:
    """Always-visible bottom keyboard. Staff get an extra row (Queue / Stats / Admin)."""
    rows = [[t(lang, "menu.confess"), t(lang, "menu.mine")]]
    second = [t(lang, "menu.rules"), t(lang, "menu.help")]
    if cfg("ads_enabled", True):
        second.insert(0, t(lang, "menu.advertise"))
    rows.append(second)
    rows.append([t(lang, "menu.lang")])
    role = role_of(user_id) if user_id else "user"
    if role in ("moderator", "admin", "owner"):
        staff_row = ["📋 Queue", "📊 Stats"] + (["⚙️ Admin"] if role != "moderator" else [])
        rows.append(staff_row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder=t(lang, "nav.placeholder"))


def nav_action(text: str | None) -> str | None:
    """Map a bottom-keyboard label (any language) to its action, else None."""
    if not text:
        return None
    text = text.strip()
    for key, action in NAV_ACTIONS.items():
        for lang in SUPPORTED:
            if text == t(lang, key):
                return action
    return None


CONVERSATION_ACTIONS = ("confess", "advertise")  # owned by their own modules' entry points


def nav_regex(only: tuple[str, ...] | None = None, exclude: tuple[str, ...] = ()) -> str:
    """Regex matching bottom-keyboard labels in every language."""
    keys = [k for k, a in NAV_ACTIONS.items() if (only is None or a in only) and a not in exclude]
    labels = {t(lang, key) for key in keys for lang in SUPPORTED}
    return "^(" + "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True)) + ")$"


def reasons_kb(reasons: tuple[str, ...], prefix: str, item_id: int, back: tuple[str, str] | None,
               label=lambda r: r) -> InlineKeyboardMarkup:
    """Two rows of reject-reason buttons (`prefix`:rejr:<id>:<reason>) plus an optional Back."""
    half = (len(reasons) + 1) // 2
    rows = [[(label(r), prefix, f"rejr:{item_id}:{r}") for r in reasons[:half]],
            [(label(r), prefix, f"rejr:{item_id}:{r}") for r in reasons[half:]]]
    if back:
        rows.append([("⬅️ Back", back[0], back[1])])
    return kb([row for row in rows if row])


def exit_row(lang: str, back: tuple[str, str] | None = None) -> InlineKeyboardMarkup:
    """[⬅️ Back] [✖️ Cancel] for prompts inside a flow. `back` = (action, payload)."""
    row = []
    if back:
        row.append((t(lang, "menu.back"), back[0], back[1]))
    row.append((t(lang, "menu.cancel"), "nav", "cancel"))
    return kb([row])


async def send_home(message, lang: str, text: str) -> None:
    """Terminal message of a flow: the text plus the persistent bottom keyboard."""
    await message.reply_text(text, reply_markup=nav_keyboard(lang, message.chat_id))


# ---------------------------------------------------------------------------
# Shared screens (hub). Used by core and by conversations when a user escapes.
# ---------------------------------------------------------------------------

def rules_markup(lang: str) -> InlineKeyboardMarkup:
    return kb([[(t(lang, "menu.takedown"), "takedown", "start")], [(t(lang, "menu.back"), "menu", "home")]])


def lang_markup() -> InlineKeyboardMarkup:
    labels = {"en": "English", "am": "አማርኛ"}
    return kb([[(labels[code], "lang", code) for code in SUPPORTED], [(t("en", "menu.back"), "menu", "home")]])


def render_setting_text(value: str):
    """Settings texts are admin-authored free text: rendered literally, with the
    first line shown bold as a title (a bot decision, not markup inside the value)."""
    from fmt import Rich

    value = normalize_newlines(value)
    first, _, rest = value.partition("\n")
    r = Rich().bold(first)
    if rest:
        r.nl().text(rest)
    return r.build()


def normalize_newlines(value: str) -> str:
    """Real newlines; also repair legacy values that contain the two characters '\\n'."""
    v = (value or "").replace("\r\n", "\n").replace("\\n", "\n")
    while "\n\n\n" in v:
        v = v.replace("\n\n\n", "\n\n")
    return v.strip()


async def show_home(message, lang: str) -> None:
    from fmt import Rich

    value = normalize_newlines(cfg("welcome_text", ""))
    first, _, rest = value.partition("\n")
    r = Rich().bold(first)
    if rest:
        r.nl().text(rest)
    role = role_of(message.chat_id)
    if role in ("moderator", "admin", "owner"):
        r.nl(2).italic(t(lang, f"nav.staff_hint_{'admin' if role != 'moderator' else 'moderator'}"))
    text, ents = r.build()
    await message.reply_text(text, entities=ents, reply_markup=nav_keyboard(lang, message.chat_id))


async def show_rules(message, lang: str) -> None:
    text, ents = render_setting_text(cfg("rules_text", ""))
    await message.reply_text(text, entities=ents, reply_markup=rules_markup(lang))


async def show_help(message, context, lang: str) -> None:
    from fmt import Rich

    value = t(lang, "help")
    first, _, rest = value.partition("\n")
    r = Rich().bold(first).nl().text(rest)
    info = context.bot_data.get("channel_info") or {}
    url = f"https://t.me/{info['username']}" if info.get("username") else (info.get("invite_link") or "")
    if url.startswith("https://"):
        r.nl(2).link(t(lang, "help.open_channel"), url)
    text, ents = r.build()
    await message.reply_text(text, entities=ents, reply_markup=nav_keyboard(lang, message.chat_id), disable_web_page_preview=True)


async def show_lang(message) -> None:
    await message.reply_text(t("en", "lang.pick"), reply_markup=lang_markup())


def my_confessions(author: str, limit: int = 5) -> list[dict]:
    """Status of the author's recent confessions across pending / live / declined (pure given DB)."""
    rows: list[dict] = []
    for r in db.q("SELECT id, created, text FROM submissions WHERE author=? ORDER BY id DESC LIMIT ?", (author, limit)):
        rows.append({"status": "pending", "ts": r["created"], "number": None, "snippet": r["text"]})
    for r in db.q("SELECT number, posted_at, comment_count, deleted FROM posts WHERE author=? AND kind='confession' ORDER BY number DESC LIMIT ?",
                  (author, limit)):
        rows.append({"status": "deleted" if r["deleted"] else "live", "ts": r["posted_at"], "number": r["number"], "count": r["comment_count"]})
    for r in db.q("SELECT created, reason, text FROM declined WHERE author=? ORDER BY id DESC LIMIT ?", (author, limit)):
        rows.append({"status": "declined", "ts": r["created"], "number": None, "reason": r["reason"] or "other", "snippet": r["text"]})
    rows.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return rows[:limit]


async def show_mine(message, lang: str, author: str) -> None:
    items = my_confessions(author)
    if not items:
        await message.reply_text(t(lang, "mine.empty"), reply_markup=kb([[(t(lang, "menu.confess"), "menu", "confess")]]))
        return
    from fmt import Rich

    r, buttons = Rich().bold(t(lang, "mine.title")), []
    for it in items:
        r.nl()
        if it["status"] == "live":
            r.text(t(lang, "mine.live", number=it["number"], count=it["count"]))
            buttons.append([(t(lang, "mine.btn_thread", number=it["number"]), "cm", f"pg:{it['number']}:0"),
                            (t(lang, "mine.btn_delete", number=it["number"]), "mine", f"del:{it['number']}")])
        elif it["status"] == "declined":
            r.text(t(lang, "mine.declined", reason=t(lang, "reject." + it["reason"])))
        else:
            r.text(t(lang, f"mine.{it['status']}"))
        if it.get("snippet"):
            snippet = it["snippet"].replace("\n", " ")
            r.nl().italic("   “" + (snippet[:48] + "…" if len(snippet) > 48 else snippet) + "”")
    if db.one("SELECT 1 FROM connect_requests WHERE (requester=? OR target=?) AND status IN ('pending','accepted','revealed')", (author, author)):
        buttons.append([(t(lang, "connect.btn_list"), "cn", "list")])
    buttons.append([(t(lang, "menu.back"), "menu", "home")])
    text, ents = r.build()
    await message.reply_text(text, entities=ents, reply_markup=kb(buttons))


async def dispatch_nav(message, context: ContextTypes.DEFAULT_TYPE, lang: str, action: str | None) -> bool:
    """Render the hub screen for a bottom-keyboard action. Returns False for
    actions owned by conversations (confess/advertise)."""
    if action in ("browse", "mine"):  # legacy 'browse' buttons → own confessions only
        await show_mine(message, lang, author_hash(message.chat_id))
    elif action == "rules":
        await show_rules(message, lang)
    elif action == "help":
        await show_help(message, context, lang)
    elif action == "lang":
        await show_lang(message)
    elif action == "cancel":
        await send_home(message, lang, t(lang, "nav.cancelled"))
    elif action in CONVERSATION_ACTIONS:
        # left one flow by tapping the other's label: exit cleanly, ask for one more tap
        await send_home(message, lang, t(lang, "nav.left_flow", label=t(lang, f"menu.{action}")))
    else:
        return False
    return True


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

CHANNEL_INFO_TTL = 600


async def channel_info(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> dict:
    """{username, invite_link, checked_at}. Re-fetched every CHANNEL_INFO_TTL so a channel
    made public (or private) later is picked up without a restart. Persisted in meta."""
    env = context.bot_data.get("env")
    info = context.bot_data.get("channel_info")
    if info is None:
        raw = db.scalar("SELECT v FROM meta WHERE k='channel_info'")
        info = json.loads(raw) if raw else {"username": None, "invite_link": None, "checked_at": 0}
    if not force and db.now() - info.get("checked_at", 0) < CHANNEL_INFO_TTL:
        context.bot_data["channel_info"] = info
        return info
    if env and env.channel_id:
        try:
            chat = await context.bot.get_chat(env.channel_id)
            info["username"] = chat.username
            if not chat.username and not info.get("invite_link"):
                try:
                    info["invite_link"] = await context.bot.export_chat_invite_link(env.channel_id)
                except TelegramError:
                    info["invite_link"] = chat.invite_link
            info["checked_at"] = db.now()
            db.execute("INSERT INTO meta(k, v) VALUES ('channel_info', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(info),))
        except TelegramError as exc:
            log.warning("channel info refresh failed: %s", type(exc).__name__)
    context.bot_data["channel_info"] = info
    return info


def post_link(context: ContextTypes.DEFAULT_TYPE, channel_msg_id: int | None) -> str:
    """Public link when the channel has a username, else the members-only t.me/c form."""
    env = context.bot_data.get("env")
    info = context.bot_data.get("channel_info") or {}
    username = info.get("username")
    if username and channel_msg_id:
        return f"https://t.me/{username}/{channel_msg_id}"
    if env and channel_msg_id:
        internal = str(env.channel_id).replace("-100", "", 1)
        return f"https://t.me/c/{internal}/{channel_msg_id}"
    return ""


def bot_link(context: ContextTypes.DEFAULT_TYPE, start: str) -> str:
    username = context.bot_data.get("bot_username") or getattr(context.bot, "username", None) or ""
    return f"https://t.me/{username}?start={start}"


def live_buttons(context: ContextTypes.DEFAULT_TYPE, lang: str, number: int, channel_msg_id: int) -> InlineKeyboardMarkup:
    """[📣 Open in channel] [💬 Comments] (+ [➕ Join channel] when the channel is private)."""
    info = context.bot_data.get("channel_info") or {}
    rows = [[(t(lang, "live.btn_open"), post_link(context, channel_msg_id)), (t(lang, "live.btn_comments"), bot_link(context, f"c_{number}"))]]
    if not info.get("username") and (info.get("invite_link") or "").startswith("https://"):
        rows.append([(t(lang, "live.btn_join"), info["invite_link"])])
    return kb(rows)


# ---------------------------------------------------------------------------
# Pending "type your next message" states: expire + bind to the originating chat
# ---------------------------------------------------------------------------

PENDING_TTL_SEC = 600


def pending_set(context: ContextTypes.DEFAULT_TYPE, key: str, value, chat_id: int) -> None:
    context.user_data[key] = {"v": value, "chat": chat_id, "at": db.now()}


def pending_get(context: ContextTypes.DEFAULT_TYPE, key: str, chat_id: int):
    """Return the stored value if fresh and from the same chat, else drop it and return None."""
    item = context.user_data.get(key)
    if not isinstance(item, dict) or "v" not in item:
        context.user_data.pop(key, None)
        return None
    if item["chat"] != chat_id or db.now() - item["at"] > PENDING_TTL_SEC:
        context.user_data.pop(key, None)
        return None
    return item["v"]


def pending_clear(context: ContextTypes.DEFAULT_TYPE, key: str) -> bool:
    return context.user_data.pop(key, None) is not None


async def staff_reply(update: Update, text: str, **kw) -> None:
    """Reply to a staff slash command with the role-aware bottom keyboard (never a dead end)."""
    uid = update.effective_user.id
    await update.effective_message.reply_text(text, reply_markup=nav_keyboard(lang_of(update), uid), **kw)


def parse_int(s: str | None) -> int | None:
    try:
        return int(str(s).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ad_card(ad: dict) -> tuple[tuple[str, list], InlineKeyboardMarkup]:
    """Admin card ((text, entities), keyboard) for an ad awaiting payment review or approval."""
    from fmt import Rich

    pkg = db.one("SELECT name FROM packages WHERE id=?", (ad["package_id"],))
    p = db.one("SELECT * FROM payments WHERE ad_id=? ORDER BY id DESC LIMIT 1", (ad["id"],))
    r = (Rich().bold(f"🧾 Order #{ad['id']} · {pkg['name'] if pkg else '?'} · {ad['price_etb']} ETB").nl()
         .label("Status", ad["status"].replace("_", " ")).text("   ").label("Advertiser", advertiser_name(ad["advertiser_id"])))
    if p:
        match = {None: "unverified", 1: "✓ ours", 0: "✗ not ours"}[p["receiver_match"]]
        r.nl().label("Payment", f"{p['status']} · {p['bank']} ").code(p["reference"]).text(
            f" · {p['amount'] or '?'} ETB · from {p['sender_name'] or '?'} · receiver {match}"
            f"{' · reused' if p['confirmed_before'] else ''}{(' · ' + p['reason']) if p['reason'] else ''}")
    else:
        r.nl().label("Payment", "none yet")
    r.nl(2).quote(ad["text"] or "")
    if ad["button_url"]:
        r.nl().label("Button", f"{ad['button_text']} → {ad['button_url']}")
    if ad["media_file_id"]:
        r.nl().italic("📷 includes a photo")
    text = r.build(limit=1000 if ad["media_file_id"] else 4096)
    s = str(ad["id"])
    if ad["status"] == "needs_review":
        markup = kb([[("✅ Accept payment", "ada", f"manual_ok:{s}"), ("❌ Decline", "ada", f"manual_no:{s}")]])
    else:
        markup = kb([[("✅ Approve & schedule", "ada", f"approve:{s}"), ("❌ Reject", "ada", f"rej:{s}")], [("🕐 Schedule +24h", "ada", f"later:{s}")]])
    return text, markup


async def send_ad_cards(message, limit: int = 5) -> int:
    """Send the pending ad cards (needs_review first). Returns how many were sent."""
    rows = db.q("SELECT * FROM ads WHERE status IN ('paid','needs_review') "
                "ORDER BY CASE status WHEN 'needs_review' THEN 0 ELSE 1 END, id LIMIT ?", (limit,))
    for r in rows:
        ad = dict(r)
        (text, ents), markup = ad_card(ad)
        if ad["media_file_id"]:
            try:
                await message.reply_photo(ad["media_file_id"], caption=text, caption_entities=ents, reply_markup=markup)
                continue
            except TelegramError:
                pass
        await message.reply_text(text, entities=ents, reply_markup=markup, disable_web_page_preview=True)
    return len(rows)


def staff_name(user_id: int | None) -> str:
    """Human label for a staff member. Never the raw Telegram id: falls back to
    role + a short stable tag so people can still be told apart."""
    if not user_id:
        return "System"
    row = db.one("SELECT display, role FROM staff WHERE user_id=?", (user_id,))
    if row and row["display"]:
        return row["display"][:24]
    role = (row["role"] if row else role_of(user_id)).capitalize()
    import hashlib

    tag = hashlib.sha256(str(user_id).encode()).hexdigest()[:3]  # display tag only
    return f"{role} ·#{tag}"


def advertiser_name(advertiser_id: int | None) -> str:
    row = db.one("SELECT name FROM advertisers WHERE id=?", (advertiser_id,)) if advertiser_id else None
    return (row["name"][:32] if row and row["name"] else f"Advertiser #{advertiser_id or '?'}")


def categories_of(item: dict) -> list[dict]:
    """Ordered categories for a submission/outbox/post row (JSON `category_ids`, legacy `category_id`)."""
    try:
        ids = [int(x) for x in json.loads(item.get("category_ids") or "[]")]
    except (ValueError, TypeError):
        ids = []
    if not ids and item.get("category_id"):
        ids = [item["category_id"]]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.q(f"SELECT * FROM categories WHERE id IN ({marks}) ORDER BY sort, id", ids)  # nosec B608 - placeholders
    return [dict(r) for r in rows]


def category_labels(item: dict) -> str:
    cats = categories_of(item)
    return " · ".join(c["label"] for c in cats) if cats else "No category"


def hashtags_of(item: dict) -> str:
    return " ".join(c["hashtag"] for c in categories_of(item))


def active_banner() -> str | None:
    row = db.one("SELECT text FROM banners WHERE starts<=? AND ends>? ORDER BY id DESC LIMIT 1", (db.now(), db.now()))
    return row["text"] if row else None
