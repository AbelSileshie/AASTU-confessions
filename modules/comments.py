"""Anonymous comment threads + consent-based Connect (OP ↔ commenter)."""
from __future__ import annotations

from telegram import MessageEntity, Update, User
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from common import alert_admins, dispatch_nav, ensure_user, exit_row, is_banned, is_private_chat, kb, lang_of, nav_action, nav_keyboard, nav_regex, notify_author, parse_int, send_home, user_lang
from config import cfg
from i18n import t
from security import author_hash, decrypt_ref, verify_cb
from textfilter import check_text, strip_invisible

PAGE = 10
WRITE = 0


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

def alias_for(number: int, author: str) -> str:
    """OP for the confession author; otherwise 'Anon N' by first-appearance order."""
    post = db.one("SELECT author FROM posts WHERE number=?", (number,))
    if post and post["author"] == author:
        return "OP"
    existing = db.one("SELECT alias FROM comments WHERE number=? AND author=? LIMIT 1", (number, author))
    if existing:
        return existing["alias"]
    n = db.scalar("SELECT count(DISTINCT author) FROM comments WHERE number=? AND alias<>'OP'", (number,), 0) or 0
    return f"Anon {n + 1}"


# ---------------------------------------------------------------------------
# Thread rendering
# ---------------------------------------------------------------------------

def _thread_rows(number: int, before_id: int | None) -> list[dict]:
    if before_id:
        rows = db.q("SELECT * FROM comments WHERE number=? AND hidden=0 AND id<? ORDER BY id DESC LIMIT ?", (number, before_id, PAGE))
    else:
        rows = db.q("SELECT * FROM comments WHERE number=? AND hidden=0 ORDER BY id DESC LIMIT ?", (number, PAGE))
    return [dict(r) for r in reversed(rows)]


def render_thread(number: int, rows: list[dict], lang: str, viewer_is_op: bool) -> tuple[str, list]:
    count = db.scalar("SELECT count(*) FROM comments WHERE number=? AND hidden=0", (number,), 0)
    lines = [t(lang, "comments.title", number=number, count=count)]
    if not rows:
        lines.append(t(lang, "comments.empty"))
    for r in rows:
        reply = f" ↩︎{r['reply_to']}" if r["reply_to"] else ""
        lines.append(f"\n[{r['id']}] {r['alias']}{reply}:\n{r['text']}")
    buttons = [[(t(lang, "comments.write"), "cm", f"w:{number}")]]
    per_comment = []
    for r in rows[-5:]:
        row = [(f"↩️ {r['id']}", "cm", f"w:{number}:{r['id']}"), (f"🚩 {r['id']}", "cm", f"rep:{r['id']}")]
        if viewer_is_op and r["alias"] != "OP" and cfg("connect_enabled", True):
            row.append((f"🤝 {r['id']}", "cn", f"ask:{r['id']}"))
        per_comment.append(row)
    nav = []
    if rows and db.one("SELECT 1 FROM comments WHERE number=? AND hidden=0 AND id<?", (number, rows[0]["id"])):
        nav.append((t(lang, "comments.older"), "cm", f"pg:{number}:{rows[0]['id']}"))
    nav.append((t(lang, "menu.back"), "menu", "home"))
    return "\n".join(lines)[:4000], buttons + per_comment + [nav]


async def _show_thread(message, update: Update, number: int, before_id: int | None = None) -> None:
    lang = lang_of(update)
    post = db.one("SELECT author, deleted FROM posts WHERE number=? AND kind='confession'", (number,))
    if not post or post["deleted"]:
        await message.reply_text(t(lang, "comments.not_found"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return
    viewer = author_hash(update.effective_user.id)
    text, rows = render_thread(number, _thread_rows(number, before_id), lang, viewer == post["author"])
    await message.reply_text(text, reply_markup=kb(rows))


async def start_thread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not context.args:
        return
    number = parse_int(context.args[0][2:])
    if number is None:
        return
    ensure_user(update.effective_user.id)
    await _show_thread(update.message, update, number)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

async def comments_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return ConversationHandler.END
    parts = parsed[1].split(":")
    action = parts[0]
    lang = lang_of(update)
    ensure_user(update.effective_user.id)

    if action == "pg":
        await q.answer()
        await _show_thread(q.message, update, int(parts[1]), int(parts[2]))
        return ConversationHandler.END
    if action == "rep":
        await _report(q, update, context, int(parts[1]), lang)
        return ConversationHandler.END
    if action == "w":
        if not cfg("comments_enabled", True):
            await q.answer(t(lang, "comments.disabled"), show_alert=True)
            return ConversationHandler.END
        author = author_hash(update.effective_user.id)
        if is_banned(author):
            await q.answer(t(lang, "submit.banned"), show_alert=True)
            return ConversationHandler.END
        await q.answer()
        context.user_data["cm_number"] = int(parts[1])
        context.user_data["cm_reply"] = int(parts[2]) if len(parts) > 2 else None
        await q.message.reply_text(t(lang, "comments.send", max=cfg("comment_max_len", 500)),
                                   reply_markup=exit_row(lang, back=("cm", f"pg:{parts[1]}:0")))
        return WRITE
    await q.answer()
    return ConversationHandler.END


def _blocklist() -> list[str]:
    return [r["word"] for r in db.q("SELECT word FROM wordlist WHERE kind='block'")]


async def write_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    number = context.user_data.get("cm_number")
    if number is None:
        return ConversationHandler.END
    author = ensure_user(update.effective_user.id)
    now = db.now()
    last = db.scalar("SELECT last_comment FROM users WHERE author=?", (author,), 0) or 0
    cooldown = cfg("comment_cooldown_sec", 30)
    exits = exit_row(lang, back=("cm", f"pg:{number}:0"))
    if now - last < cooldown:
        await update.message.reply_text(t(lang, "comments.cooldown", seconds=cooldown - (now - last)), reply_markup=exits)
        return WRITE
    text = strip_invisible(update.message.text or "").strip()
    result = check_text(text, min_len=1, max_len=cfg("comment_max_len", 500), blocklist=_blocklist(), risklist=[])
    if not result.ok:
        await update.message.reply_text(t(lang, f"submit.{result.reason}", min=1, max=cfg("comment_max_len", 500)), reply_markup=exits)
        return WRITE
    post = db.one("SELECT 1 FROM posts WHERE number=? AND deleted=0", (number,))
    if not post:
        await update.message.reply_text(t(lang, "comments.not_found"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    reply_to = context.user_data.get("cm_reply")
    if reply_to and not db.one("SELECT 1 FROM comments WHERE id=? AND number=?", (reply_to, number)):
        reply_to = None
    alias = alias_for(number, author)
    with db.tx():
        db.execute("INSERT INTO comments(number, author, alias, reply_to, text, created) VALUES (?,?,?,?,?,?)",
                   (number, author, alias, reply_to, text, now))
        db.execute("UPDATE posts SET comment_count=comment_count+1, unnotified_comments=unnotified_comments+CASE WHEN ?='OP' THEN 0 ELSE 1 END WHERE number=?",
                   (alias, number))
        db.execute("UPDATE users SET last_comment=? WHERE author=?", (now, author))
    context.user_data.pop("cm_number", None)
    context.user_data.pop("cm_reply", None)
    await update.message.reply_text(t(lang, "comments.posted", alias=alias))
    await _show_thread(update.message, update, number)
    return ConversationHandler.END


async def cancel_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("cm_number", None)
    context.user_data.pop("cm_reply", None)
    lang = lang_of(update)
    if update.callback_query:
        await update.callback_query.answer()
        parsed = verify_cb(update.callback_query.data)
        if parsed and parsed[0] == "cm" and parsed[1].startswith("pg:"):
            _, num, before = parsed[1].split(":")
            await _show_thread(update.callback_query.message, update, int(num), int(before) or None)
            return ConversationHandler.END
        await send_home(update.callback_query.message, lang, t(lang, "nav.cancelled"))
        return ConversationHandler.END
    if update.message:
        if not await dispatch_nav(update.message, context, lang, nav_action(update.message.text)):
            await send_home(update.message, lang, t(lang, "nav.cancelled"))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def report_comment(comment_id: int, reporter: str, now: int) -> str:
    """Returns 'reported' | 'already' | 'hidden' | 'missing'."""
    c = db.one("SELECT id, number, hidden FROM comments WHERE id=?", (comment_id,))
    if not c or c["hidden"]:
        return "missing"
    try:
        db.execute("INSERT INTO comment_reports(comment_id, reporter) VALUES (?,?)", (comment_id, reporter))
    except db.sqlite3.IntegrityError:
        return "already"
    db.execute("UPDATE comments SET reports=reports+1 WHERE id=?", (comment_id,))
    reports = db.scalar("SELECT reports FROM comments WHERE id=?", (comment_id,), 0)
    if reports >= cfg("report_hide_threshold", 3):
        with db.tx():
            db.execute("UPDATE comments SET hidden=1, hidden_at=? WHERE id=?", (now, comment_id))
            db.execute("UPDATE posts SET comment_count=max(comment_count-1,0) WHERE number=?", (c["number"],))
        return "hidden"
    return "reported"


async def _report(q, update, context, comment_id: int, lang: str) -> None:
    author = author_hash(update.effective_user.id)
    now = db.now()
    last = db.scalar("SELECT last_report FROM users WHERE author=?", (author,), 0) or 0
    if now - last < cfg("report_cooldown_sec", 10):
        await q.answer(t(lang, "comments.report_cooldown"), show_alert=True)
        return
    db.execute("UPDATE users SET last_report=? WHERE author=?", (now, author))
    outcome = report_comment(comment_id, author, now)
    if outcome == "already":
        await q.answer(t(lang, "comments.already_reported"), show_alert=True)
        return
    await q.answer(t(lang, "comments.reported"))
    if outcome == "hidden":
        c = db.one("SELECT number, text FROM comments WHERE id=?", (comment_id,))
        await alert_admins(context, f"🙈 Comment {comment_id} on #{c['number']} auto-hidden after reports:\n\n{c['text']}", kind="takedowns",
                           reply_markup=kb([[("Unhide", "cma", f"unhide:{comment_id}"), ("Keep hidden", "cma", f"keep:{comment_id}")]]))


async def comment_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from security import has_role

    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed or not has_role(update.effective_user.id, "moderator"):
        await q.answer()
        return
    action, _, cid = parsed[1].partition(":")
    c = db.one("SELECT number FROM comments WHERE id=?", (cid,))
    if action == "unhide" and c:
        with db.tx():
            db.execute("UPDATE comments SET hidden=0, hidden_at=NULL, reports=0 WHERE id=?", (cid,))
            db.execute("DELETE FROM comment_reports WHERE comment_id=?", (cid,))
            db.execute("UPDATE posts SET comment_count=comment_count+1 WHERE number=?", (c["number"],))
        db.audit(update.effective_user.id, "comment_unhide", cid)
        await q.answer("Unhidden")
    else:
        db.audit(update.effective_user.id, "comment_keep_hidden", cid)
        await q.answer("Kept hidden")
    await q.message.edit_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# Connect (OP → commenter, consent-based)
# ---------------------------------------------------------------------------

async def _send_mention(bot, chat_id: int, template: str, target_user_id: int, **kw) -> None:
    label = "👤 Open profile"
    prefix = template.format(mention="", **kw)
    text = prefix + label
    entities = [MessageEntity(MessageEntity.TEXT_MENTION, len(prefix), len(label),
                              user=User(id=target_user_id, first_name=label, is_bot=False))]
    await bot.send_message(chat_id, text, entities=entities)


def create_connect(number: int, comment_id: int, requester: str, now: int) -> tuple[str, dict]:
    """Returns (status_key, info). Pure given DB state."""
    if not cfg("connect_enabled", True):
        return "connect.disabled", {}
    post = db.one("SELECT author FROM posts WHERE number=? AND deleted=0", (number,))
    if not post or post["author"] != requester:
        return "connect.not_op", {}
    c = db.one("SELECT author, alias, text FROM comments WHERE id=? AND number=? AND hidden=0", (comment_id, number))
    if not c:
        return "connect.gone", {}
    if c["author"] == requester:
        return "connect.self", {}
    last = db.scalar("SELECT last_connect FROM users WHERE author=?", (requester,), 0) or 0
    cooldown = cfg("connect_cooldown_sec", 300)
    if now - last < cooldown:
        return "connect.cooldown", {"minutes": max(1, (cooldown - (now - last)) // 60)}
    if db.one("SELECT 1 FROM connect_requests WHERE number=? AND target=? AND status='pending'", (number, c["author"])):
        return "connect.already", {}
    with db.tx():
        rid = db.execute("INSERT INTO connect_requests(number, comment_id, requester, target, status, created) VALUES (?,?,?,?,'pending',?)",
                         (number, comment_id, requester, c["author"], now))
        db.execute("UPDATE users SET last_connect=? WHERE author=?", (now, requester))
    db.audit(0, "connect_request", f"post:{number}", f"req:{rid}")
    return "connect.sent", {"id": rid, "target": c["author"], "comment": c["text"]}


async def connect_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    action, _, arg = parsed[1].partition(":")
    lang = lang_of(update)
    me = ensure_user(update.effective_user.id)
    if action == "ask":
        c = db.one("SELECT number FROM comments WHERE id=?", (arg,))
        if not c:
            await q.answer(t(lang, "connect.gone"), show_alert=True)
            return
        key, info = create_connect(c["number"], int(arg), me, db.now())
        if key != "connect.sent":
            await q.answer(t(lang, key, **info), show_alert=True)
            return
        await q.answer()
        tl = user_lang(info["target"])
        sent = await notify_author(context, info["target"],
                                   t(tl, "connect.ask", number=c["number"], comment=info["comment"][:200]),
                                   reply_markup=kb([[(t(tl, "connect.btn_accept"), "cn", f"yes:{info['id']}"),
                                                     (t(tl, "connect.btn_decline"), "cn", f"no:{info['id']}")]]))
        if not sent:
            db.execute("UPDATE connect_requests SET status='expired', resolved=? WHERE id=?", (db.now(), info["id"]))
            await q.message.reply_text(t(lang, "connect.gone"))
            return
        await q.message.reply_text(t(lang, "connect.sent"))
    elif action in ("yes", "no"):
        await _resolve(q, update, context, int(arg), me, accept=(action == "yes"), lang=lang)
    else:
        await q.answer()


async def _resolve(q, update, context, rid: int, me: str, accept: bool, lang: str) -> None:
    req = db.one("SELECT * FROM connect_requests WHERE id=? AND target=? AND status='pending'", (rid, me))
    if not req:
        await q.answer(t(lang, "connect.gone"), show_alert=True)
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    now = db.now()
    status = "accepted" if accept else "declined"
    db.execute("UPDATE connect_requests SET status=?, resolved=? WHERE id=?", (status, now, rid))
    db.audit(0, f"connect_{status}", f"post:{req['number']}", f"req:{rid}")
    await q.answer()
    try:
        await q.message.edit_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    alias = db.scalar("SELECT alias FROM comments WHERE id=?", (req["comment_id"],), "Anon")
    rl = user_lang(req["requester"])
    if not accept:
        await q.message.reply_text(t(lang, "connect.declined_target"))
        await notify_author(context, req["requester"], t(rl, "connect.declined_requester", alias=alias, number=req["number"]))
        return
    requester_id = decrypt_ref(db.scalar("SELECT chat_ref FROM users WHERE author=?", (req["requester"],)))
    target_id = update.effective_user.id
    if requester_id is None:
        await q.message.reply_text(t(lang, "connect.gone"))
        return
    try:
        await _send_mention(context.bot, target_id, t(lang, "connect.accepted_target"), requester_id, number=req["number"])
        await _send_mention(context.bot, requester_id, t(rl, "connect.accepted_requester"), target_id,
                            alias=alias, number=req["number"])
    except TelegramError:
        await q.message.reply_text(t(lang, "connect.gone"))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_thread, filters=filters.Regex(r"^/start\s+c_\d+")), group=1)
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(comments_cb, pattern=r"^cm:")],
        states={WRITE: [
            MessageHandler(filters.TEXT & filters.Regex(nav_regex()), cancel_write),
            MessageHandler(filters.TEXT & ~filters.COMMAND, write_comment),
        ]},
        fallbacks=[
            CallbackQueryHandler(cancel_write, pattern=r"^(nav:cancel|cm:pg):"),
            CommandHandler("cancel", cancel_write), CommandHandler("start", cancel_write),
        ],
        conversation_timeout=600,
        name="comments",
    ))
    app.add_handler(CallbackQueryHandler(comment_admin_cb, pattern=r"^cma:"))
    app.add_handler(CallbackQueryHandler(connect_cb, pattern=r"^cn:"))
