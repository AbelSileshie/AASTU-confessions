"""⚙️ Admin hub: inline menus over ops.py for staff, catalog, payouts, wordlist,
group setup and health. Every screen has ⬅️ Back. Admin-only; payouts owner-only.

Callback grammar (signed):  hub:<screen>[:<id>[:<action>[:<arg>]]]
Text inputs (add/edit) use context.user_data["hub_input"] and a group-4 handler.
"""
from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
from common import TOPICS, TOPIC_NAMES, bot_link, ensure_topics, fmt_ts, is_private_chat, kb, pending_clear, pending_get, pending_set, send_ad_cards, staff_name
from fmt import Rich
from config import cfg, invalidate
from ops import (
    CATEGORY_FIELDS,
    PACKAGE_FIELDS,
    add_category,
    add_package,
    add_payout,
    add_word,
    can_assign,
    create_invite,
    health_text,
    move,
    remove_staff,
    remove_word_by_rowid,
    set_role,
    staff_list,
    toggle,
    update_field,
    words,
)
from payments import BANK_FIELDS
from security import has_role, require, verify_cb

BACK = ("⬅️ Back", "hub", "home")


# ---------------------------------------------------------------------------
# Screens → (text, keyboard)
# ---------------------------------------------------------------------------

def home_screen(uid: int):
    owner = has_role(uid, "owner")
    rows = [
        [("👥 Staff", "hub", "staff"), ("⚙️ Settings", "st", "home")],
        [("🏷 Categories", "hub", "cats"), ("📦 Packages", "hub", "pkgs")],
        [("🚫 Wordlist", "hub", "words"), ("💰 Ads", "hub", "ads")],
        [("💬 Group setup", "hub", "group"), ("🩺 Health", "hub", "health")],
        [("🧭 Setup checklist", "setup", "check")],
    ]
    if owner:
        rows.insert(2, [("🏦 Payout accounts", "hub", "pay")])
    return "⚙️ Admin — pick an area", kb(rows)


def staff_screen(uid: int):
    rows = [[(f"{'👑' if r['role']=='admin' else '🛡'} {staff_name(r['user_id'])}{' 🟢' if r['on_shift'] else ''}",
              "hub", f"staff:{r['user_id']}")] for r in staff_list()]
    invites = [("➕ Invite moderator", "hub", "staff:invite:moderator")]
    if can_assign(uid, "admin"):
        invites.append(("➕ Invite admin", "hub", "staff:invite:admin"))
    rows.append(invites)
    rows.append([BACK])
    return "👥 Staff — tap a person to manage", kb(rows)


def person_screen(uid: int, target: int):
    row = db.one("SELECT * FROM staff WHERE user_id=? AND active=1", (target,))
    if not row:
        return "That person is no longer staff.", kb([[("⬅️ Back", "hub", "staff")]])
    actions = []
    if row["role"] == "moderator" and can_assign(uid, "admin"):
        actions.append(("👑 Make admin", "hub", f"staff:{target}:role:admin"))
    if row["role"] == "admin" and can_assign(uid, "admin"):
        actions.append(("🛡 Make moderator", "hub", f"staff:{target}:role:moderator"))
    rows = [actions] if actions else []
    rows.append([("🗑️ Remove", "hub", f"staff:{target}:remove"), ("📜 Audit", "hub", f"staff:{target}:audit")])
    rows.append([("⬅️ Back", "hub", "staff")])
    decisions = db.scalar("SELECT count(*) FROM audit WHERE actor=? AND action IN ('approve','reject','final_post') AND at>?",
                          (target, db.now() - 7 * 86400), 0)
    text, ents = (Rich().bold(f"{'👑' if row['role']=='admin' else '🛡'} {staff_name(target)}").text(f" · {row['role']}").nl()
                  .label("Decisions (7d)", str(decisions)).text("   ").label("Shift", "🟢 on" if row["on_shift"] else "off").build())
    return (text, ents), kb(rows)


def cats_screen():
    rows = [[(f"{'✅ ' if r['enabled'] else '⬜ '}{r['label']} {r['hashtag']}", "hub", f"cat:{r['id']}")]
            for r in db.q("SELECT * FROM categories ORDER BY sort, id")]
    rows.append([("➕ Add category", "hub", "cat:add"), BACK])
    return "🏷 Categories — tap to edit", kb(rows)


def cat_screen(cid: int):
    r = db.one("SELECT * FROM categories WHERE id=?", (cid,))
    if not r:
        return "Gone.", kb([[("⬅️ Back", "hub", "cats")]])
    rows = [[("✏️ Label", "hub", f"cat:{cid}:set:label"), ("#️⃣ Hashtag", "hub", f"cat:{cid}:set:hashtag")],
            [("⬆️ Move up", "hub", f"cat:{cid}:up"), ("⬇️ Move down", "hub", f"cat:{cid}:down"),
             ("⬜ Disable" if r["enabled"] else "✅ Enable", "hub", f"cat:{cid}:toggle")],
            [("⬅️ Back", "hub", "cats")]]
    return f"{r['label']} {r['hashtag']} · {'enabled' if r['enabled'] else 'disabled'}", kb(rows)


def pkgs_screen():
    rows = [[(f"{'✅' if r['enabled'] else '⬜'} {r['name']} · {r['price_etb']} ETB", "hub", f"pkg:{r['id']}")]
            for r in db.q("SELECT * FROM packages ORDER BY sort, id")]
    rows.append([("➕ Add package", "hub", "pkg:add"), BACK])
    return "📦 Packages — tap to edit (paid orders keep their price)", kb(rows)


def pkg_screen(pid: int):
    r = db.one("SELECT * FROM packages WHERE id=?", (pid,))
    if not r:
        return "Gone.", kb([[("⬅️ Back", "hub", "pkgs")]])
    rows = [[("💵 Price", "hub", f"pkg:{pid}:set:price_etb"), ("🔁 Posts", "hub", f"pkg:{pid}:set:posts"), ("📌 Pin hours", "hub", f"pkg:{pid}:set:pin_hours")],
            [("✏️ Name", "hub", f"pkg:{pid}:set:name"), ("📝 Description", "hub", f"pkg:{pid}:set:description"), ("🔤 Max chars", "hub", f"pkg:{pid}:set:max_chars")],
            [("⬆️ Move up", "hub", f"pkg:{pid}:up"), ("⬇️ Move down", "hub", f"pkg:{pid}:down"),
             ("⬜ Disable" if r["enabled"] else "✅ Enable", "hub", f"pkg:{pid}:toggle")],
            [("⬅️ Back", "hub", "pkgs")]]
    text = (f"{r['name']} · {r['price_etb']} ETB · {'enabled' if r['enabled'] else 'disabled'}\n"
            f"posts {r['posts']} · pin {r['pin_hours']}h · banner {r['bot_banner_days']}d · max {r['max_chars']} chars\n{r['description'] or ''}")
    return text, kb(rows)


def payouts_screen():
    rows = [[(f"{'✅' if r['enabled'] else '⬜'} {BANK_FIELDS.get(r['bank'], {}).get('label', r['bank'])} …{r['account'][-4:]} · {r['holder']}",
              "hub", f"pay:{r['id']}:toggle")] for r in db.q("SELECT * FROM payout_accounts ORDER BY sort, id")]
    rows.append([("➕ Add account", "hub", "pay:add"), BACK])
    return "🏦 Payout accounts — tap to enable/disable. Register the same accounts in Verify.et.", kb(rows)


def words_screen(kind: str):
    items = words(kind)
    rows, row = [], []
    for w in items[:40]:
        row.append((f"Remove “{w['word'][:18]}”", "hub", f"word:{w['id']}:del:{kind}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    other = "risk" if kind == "block" else "block"
    rows.append([("➕ Add", "hub", f"word:add:{kind}"), (f"→ {other} list", "hub", f"words:{other}"), BACK])
    title = "🚫 Block list (rejected automatically)" if kind == "block" else "⚠️ Risk list (highlighted for review)"
    return f"{title} · {len(items)} words", kb(rows)


def group_screen():
    lines = ["💬 Admin group setup\n"]
    for k in TOPICS:
        tid = cfg(f"topic_{k}", 0)
        lines.append(f"{TOPIC_NAMES[k]}: {'thread ' + str(tid) if tid else 'main chat'}")
    missing = [k for k in TOPICS if not cfg(f"topic_{k}", 0)]
    lines.append("\n" + ("✅ All topics are set up. The bot re-checks hourly." if not missing else
                         "The bot creates missing topics automatically at startup and every hour. "
                         "Requirements: Topics enabled in the group and the bot has the 'Manage topics' right."))
    return "\n".join(lines), kb([[("🧵 Create missing now", "hub", "group:create"), ("♻️ Reset to main chat", "hub", "group:reset")], [BACK]])


def health_screen():
    return health_text(db.now(), staff_name), kb([[("🔄 Refresh", "hub", "health"), BACK]])


def _pretty_target(target: str | None) -> str:
    """Audit targets that are bare staff ids → names."""
    if target and target.isdigit():
        return staff_name(int(target))
    return target or ""


def ads_screen():
    n = db.scalar("SELECT count(*) FROM ads WHERE status IN ('paid','needs_review')", default=0)
    r = db.scalar("SELECT count(*) FROM ads WHERE status='refund_due'", default=0)
    return (f"💰 Ads — {n} waiting · {r} refunds due\n/revenue for totals · /refunds for the refund list"), kb([[("🔄 Refresh", "hub", "ads"), BACK]])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _render(q, screen) -> None:
    text, markup = screen
    ents = None
    if isinstance(text, tuple):
        text, ents = text
    try:
        await q.message.edit_text(text, entities=ents, reply_markup=markup)
    except TelegramError:
        await q.message.reply_text(text, entities=ents, reply_markup=markup)


@require("admin")
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    text, markup = home_screen(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=markup)  # title only, no entities needed


@require("admin")
async def hub_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parsed = verify_cb(q.data)
    if not parsed:
        await q.answer()
        return
    parts = parsed[1].split(":")
    uid = update.effective_user.id
    screen, rest = parts[0], parts[1:]
    await q.answer()
    pending_clear(context, "hub_input")

    if screen == "home":
        await _render(q, home_screen(uid))
    elif screen == "staff":
        await _staff(q, context, uid, rest)
    elif screen == "cats":
        await _render(q, cats_screen())
    elif screen == "cat":
        await _catalog(q, context, uid, "categories", CATEGORY_FIELDS, rest, cats_screen, cat_screen, "cat")
    elif screen == "pkgs":
        await _render(q, pkgs_screen())
    elif screen == "pkg":
        await _catalog(q, context, uid, "packages", PACKAGE_FIELDS, rest, pkgs_screen, pkg_screen, "pkg")
    elif screen == "pay":
        await _pay(q, context, uid, rest)
    elif screen == "words":
        await _render(q, words_screen(rest[0] if rest and rest[0] in ("block", "risk") else "block"))
    elif screen == "word":
        await _word(q, context, uid, rest)
    elif screen == "group":
        await _group(q, context, uid, rest)
    elif screen == "health":
        await _render(q, health_screen())
    elif screen == "ads":
        await _render(q, ads_screen())
        await send_ad_cards(q.message)


async def _staff(q, context, uid: int, rest: list[str]) -> None:
    if not rest:
        await _render(q, staff_screen(uid))
        return
    if rest[0] == "invite":
        role = rest[1] if len(rest) > 1 else "moderator"
        if not can_assign(uid, role):
            await q.answer("You cannot assign that role.", show_alert=True)
            return
        code = create_invite(role, uid)
        await q.message.reply_text(f"Invite for a new {role} (valid 24h, single use). Forward this to them:\n{bot_link(context, 'inv_' + code)}",
                                   disable_web_page_preview=True, reply_markup=kb([[("⬅️ Back to staff", "hub", "staff")]]))
        return
    target = int(rest[0]) if rest[0].isdigit() else None
    if target is None:
        return
    action = rest[1] if len(rest) > 1 else ""
    if action == "role" and len(rest) > 2:
        if not set_role(uid, target, rest[2]):
            await q.answer("Not allowed.", show_alert=True)
    elif action == "remove":
        if not remove_staff(uid, target):
            await q.answer("Not allowed.", show_alert=True)
        await _render(q, staff_screen(uid))
        return
    elif action == "audit":
        rows = db.q("SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT 15", (target,))
        r = Rich().bold(f"📜 Audit · {staff_name(target)}").text(" ").code(f"(id {target})")
        for a in rows:
            r.nl().text(f"{fmt_ts(a['at'])} · {a['action']} {_pretty_target(a['target'])}")
        if not rows:
            r.nl().text("no actions yet")
        text, ents = r.build()
        await q.message.reply_text(text, entities=ents, reply_markup=kb([[("⬅️ Back", "hub", f"staff:{target}")]]))
        return
    await _render(q, person_screen(uid, target))


async def _catalog(q, context, uid, table, fields, rest, list_screen, item_screen, prefix) -> None:
    if not rest:
        await _render(q, list_screen())
        return
    if rest[0] == "add":
        pending_set(context, "hub_input", {"kind": f"{prefix}_add"}, q.message.chat_id)
        hint = ("Send: slug | Label | #Hashtag   (e.g. sports | ⚽ Sports | #Sports)" if prefix == "cat"
                else "Send: slug | Name | price   (e.g. story | Story post | 1200)")
        await q.message.reply_text(hint, reply_markup=kb([[("⬅️ Back", "hub", f"{prefix}s")]]))
        return
    item_id = int(rest[0]) if rest[0].isdigit() else -1
    action = rest[1] if len(rest) > 1 else ""
    if action == "toggle":
        toggle(table, item_id, uid)
    elif action in ("up", "down"):
        move(table, item_id, -1 if action == "up" else 1, uid)
        await _render(q, list_screen())
        return
    elif action == "set" and len(rest) > 2 and rest[2] in fields:
        pending_set(context, "hub_input", {"kind": f"{prefix}_set", "id": item_id, "field": rest[2]}, q.message.chat_id)
        await q.message.reply_text(f"Send the new {rest[2].replace('_', ' ')}:", reply_markup=kb([[("⬅️ Back", "hub", f"{prefix}:{item_id}")]]))
        return
    await _render(q, item_screen(item_id))


async def _pay(q, context, uid, rest) -> None:
    if not has_role(uid, "owner"):
        await q.answer("Owner only.", show_alert=True)
        return
    if rest and rest[0] == "add":
        pending_set(context, "hub_input", {"kind": "pay_add"}, q.message.chat_id)
        await q.message.reply_text("Send: bank | account | Holder name\nbanks: " + ", ".join(BANK_FIELDS),
                                   reply_markup=kb([[("⬅️ Back", "hub", "pay")]]))
        return
    if len(rest) >= 2 and rest[0].isdigit() and rest[1] == "toggle":
        toggle("payout_accounts", int(rest[0]), uid)
    await _render(q, payouts_screen())


async def _word(q, context, uid, rest) -> None:
    if rest and rest[0] == "add":
        kind = rest[1] if len(rest) > 1 and rest[1] in ("block", "risk") else "block"
        pending_set(context, "hub_input", {"kind": "word_add", "list": kind}, q.message.chat_id)
        await q.message.reply_text(f"Send words for the {kind} list (one per line or comma-separated):",
                                   reply_markup=kb([[("⬅️ Back", "hub", f"words:{kind}")]]))
        return
    if len(rest) >= 2 and rest[0].isdigit() and rest[1] == "del":
        remove_word_by_rowid(int(rest[0]), uid)
        await _render(q, words_screen(rest[2] if len(rest) > 2 else "block"))


async def _group(q, context, uid, rest) -> None:
    env = context.bot_data["env"]
    if rest and rest[0] == "create":
        if not env.admin_group_id:
            await q.answer("ADMIN_GROUP_ID is not set.", show_alert=True)
            return
        res = await ensure_topics(context, actor=uid, force=True)
        if res["error"] == "topics_disabled":
            await q.answer("Topics are off in the group. Group → Edit → Topics → on, then try again.", show_alert=True)
        elif res["error"]:
            await q.answer(f"Could not create topics ({res['error']}). Check the bot's 'Manage topics' right.", show_alert=True)
        elif res["created"]:
            await q.answer(f"Created {len(res['created'])} topic(s).")
        else:
            await q.answer("All topics already exist.")
    elif rest and rest[0] == "reset":
        for k in TOPICS:
            db.execute("UPDATE settings SET value='0' WHERE key=?", (f"topic_{k}",))
            invalidate(f"topic_{k}")
        db.audit(uid, "topics_reset")
    await _render(q, group_screen())


# ---------------------------------------------------------------------------
# Text input for add/edit steps
# ---------------------------------------------------------------------------

def _split(text: str, n: int) -> list[str] | None:
    parts = [p.strip() for p in text.split("|")]
    return parts if len(parts) == n and all(parts) else None


@require("admin")
async def hub_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    spec = pending_get(context, "hub_input", update.effective_chat.id)
    if not spec:
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    pending_clear(context, "hub_input")
    kind = spec["kind"]
    ok, back = False, "home"
    if kind == "cat_add" and (p := _split(text, 3)):
        ok, back = add_category(p[0], p[1], p[2], uid), "cats"
    elif kind == "pkg_add" and (p := _split(text, 3)) and p[2].isdigit():
        ok, back = add_package(p[0], p[1], int(p[2]), uid), "pkgs"
    elif kind == "cat_set":
        ok, back = update_field("categories", CATEGORY_FIELDS, spec["id"], spec["field"], text, uid), f"cat:{spec['id']}"
    elif kind == "pkg_set":
        ok, back = update_field("packages", PACKAGE_FIELDS, spec["id"], spec["field"], text, uid), f"pkg:{spec['id']}"
    elif kind == "pay_add" and has_role(uid, "owner") and (p := _split(text, 3)):
        ok, back = add_payout(p[0], p[1], p[2], uid, tuple(BANK_FIELDS)), "pay"
    elif kind == "word_add":
        items = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        ok = bool(items) and all(add_word(w, spec["list"], uid) for w in items)
        back = f"words:{spec['list']}"
    await update.message.reply_text("✅ Saved." if ok else "❌ Invalid input — nothing changed.",
                                    reply_markup=kb([[("⬅️ Back", "hub", back)]]))


@require("admin")
async def cancel_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if pending_clear(context, "hub_input"):
        await update.message.reply_text("Cancelled.", reply_markup=kb([[("⚙️ Admin", "hub", "home")]]))


def register(app: Application) -> None:
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(hub_cb, pattern=r"^hub:"))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^⚙️ Admin$") & filters.ChatType.PRIVATE, admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_input), group=4)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, hub_input), group=4)
