"""/advertise: package → text → photo → button → preview → order → pay → reference."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from common import (
    advertiser_name,
    alert_admins,
    dispatch_nav,
    exit_row,
    is_private_chat,
    kb,
    lang_of,
    nav_action,
    nav_keyboard,
    nav_regex,
    send_home,
)
from config import cfg
from fmt import Rich, card
from i18n import t
from payments import (
    BANK_FIELDS,
    clean_extra,
    clean_reference,
    outcome_message,
    record_payment,
    required_extra,
    verify_payment,
)
from security import verify_cb
from textfilter import check_text, strip_invisible, valid_ad_url

PACKAGE, TEXT, PHOTO, BTN_TEXT, BTN_URL, PREVIEW, BANK, REFERENCE, EXTRA = range(9)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def ensure_advertiser(user_id: int, name: str) -> dict:
    db.execute("INSERT INTO advertisers(user_id, name, created) VALUES (?,?,?) ON CONFLICT(user_id) DO NOTHING",
               (user_id, name[:64], db.now()))
    return dict(db.one("SELECT * FROM advertisers WHERE user_id=?", (user_id,)))


def create_order(advertiser_id: int, pkg: dict, data: dict) -> int:
    return db.execute(
        "INSERT INTO ads(advertiser_id, package_id, text, media_file_id, button_text, button_url, price_etb, status, posts_left, created) "
        "VALUES (?,?,?,?,?,?,?,'awaiting_payment',?,?)",
        (advertiser_id, pkg["id"], data["text"], data.get("photo"), data.get("btn_text"), data.get("btn_url"),
         pkg["price_etb"], pkg["posts"], db.now()),
    )


def ad_preview_markup(btn_text: str | None, btn_url: str | None) -> InlineKeyboardMarkup | None:
    if btn_text and btn_url:
        return InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])
    return None


def pay_screen(ad_id: int, price: int, lang: str):
    """((text, entities), keyboard) or None when no payout account is configured."""
    accounts = db.q("SELECT * FROM payout_accounts WHERE enabled=1 ORDER BY sort, id")
    if not accounts:
        return None
    r = Rich().bold(t(lang, "ads.pay_title", ad_id=ad_id, price=price)).nl(2).text(t(lang, "ads.pay_to")).nl()
    for a in accounts:
        r.nl().text(f"• {BANK_FIELDS.get(a['bank'], {}).get('label', a['bank'])} ").code(a["account"]).text(f" — {a['holder']}")
    r.nl(2).text(t(lang, "ads.pay_then"))
    markup = kb([[(t(lang, "ads.btn_paid"), "ad", f"paid:{ad_id}")], [(t(lang, "ads.btn_cancel_order"), "ad", f"cancel:{ad_id}")]])
    return r.build(), markup


def _own_ad(ad_id: int, user_id: int, *statuses: str) -> dict | None:
    row = db.one("SELECT a.* FROM ads a JOIN advertisers adv ON adv.id=a.advertiser_id WHERE a.id=? AND adv.user_id=?",
                 (ad_id, user_id))
    if row and (not statuses or row["status"] in statuses):
        return dict(row)
    return None


# ---------------------------------------------------------------------------
# Creation flow
# ---------------------------------------------------------------------------

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_private_chat(update):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    lang = lang_of(update)
    msg = update.effective_message
    if not cfg("ads_enabled", True):
        await msg.reply_text(t(lang, "ads.disabled"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    adv = ensure_advertiser(update.effective_user.id, update.effective_user.full_name or "")
    if adv["blocked"]:
        await msg.reply_text(t(lang, "ads.blocked"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    open_orders = db.scalar("SELECT count(*) FROM ads WHERE advertiser_id=? AND status IN ('draft','awaiting_payment','verifying')", (adv["id"],), 0)
    if open_orders >= cfg("max_open_orders", 2):
        await msg.reply_text(t(lang, "ads.too_many_open"), reply_markup=kb([[("📢 /myads", "myad", "list")]]))
        return ConversationHandler.END
    pkgs = db.q("SELECT id, name, price_etb, description FROM packages WHERE enabled=1 ORDER BY sort, id")
    if not pkgs:
        await msg.reply_text(t(lang, "ads.disabled"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    context.user_data["ad"] = {}
    rows = [[(f"{p['name']} — {p['price_etb']} ETB", "adp", str(p["id"]))] for p in pkgs]
    rows.append([(t(lang, "menu.cancel"), "adp", "cancel")])
    body = t(lang, "ads.pick_package") + "\n\n" + "\n".join(f"• {p['name']}: {p['description']}" for p in pkgs)
    await msg.reply_text(body, reply_markup=kb(rows))
    return PACKAGE


async def pick_package(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    if not parsed or parsed[1] == "cancel":
        return await _cancel(update, context)
    pkg = db.one("SELECT * FROM packages WHERE id=? AND enabled=1", (parsed[1],))
    if not pkg:
        return PACKAGE
    context.user_data["ad"] = {"package_id": pkg["id"]}
    await q.message.reply_text(t(lang, "ads.send_text", max=pkg["max_chars"]), reply_markup=_exits(lang, "pkg"))
    return TEXT


def _exits(lang: str, back_to: str):
    return exit_row(lang, back=("ads", f"back_{back_to}"))


def _pkg(context) -> dict:
    return dict(db.one("SELECT * FROM packages WHERE id=?", (context.user_data["ad"]["package_id"],)))


async def ad_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    pkg = _pkg(context)
    text = strip_invisible(update.message.text or "").strip()
    result = check_text(text, min_len=5, max_len=pkg["max_chars"], blocklist=[], risklist=[], allow_links=True)
    if not result.ok:
        await update.message.reply_text(
            t(lang, "ads.too_long", max=pkg["max_chars"]) if result.reason == "too_long" else t(lang, "ads.text_only"),
            reply_markup=_exits(lang, "pkg"))
        return TEXT
    context.user_data["ad"]["text"] = text
    if pkg["allow_media"]:
        await update.message.reply_text(t(lang, "ads.send_photo"),
                                        reply_markup=kb([[(t(lang, "ads.btn_skip"), "ads", "skip_photo")],
                                                         [(t(lang, "menu.back"), "ads", "back_text"), (t(lang, "menu.cancel"), "ads", "cancel")]]))
        return PHOTO
    return await _ask_button(update.message, context, lang)


async def ad_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    if update.callback_query:
        await update.callback_query.answer()
        return await _ask_button(update.callback_query.message, context, lang)
    if not update.message.photo:
        await update.message.reply_text(t(lang, "ads.photo_only"),
                                        reply_markup=kb([[(t(lang, "ads.btn_skip"), "ads", "skip_photo")],
                                                         [(t(lang, "menu.cancel"), "ads", "cancel")]]))
        return PHOTO
    context.user_data["ad"]["photo"] = update.message.photo[-1].file_id
    return await _ask_button(update.message, context, lang)


async def _ask_button(message, context, lang: str) -> int:
    if not _pkg(context)["allow_button"]:
        return await _preview(message, context, lang)
    await message.reply_text(t(lang, "ads.send_button_text"),
                             reply_markup=kb([[(t(lang, "ads.btn_skip"), "ads", "skip_btn")],
                                              [(t(lang, "menu.cancel"), "ads", "cancel")]]))
    return BTN_TEXT


async def ad_btn_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    if update.callback_query:
        await update.callback_query.answer()
        return await _preview(update.callback_query.message, context, lang)
    label = strip_invisible(update.message.text or "").strip()[:40]
    if not label:
        return BTN_TEXT
    context.user_data["ad"]["btn_text"] = label
    await update.message.reply_text(t(lang, "ads.send_button_url"), reply_markup=_exits(lang, "btn"))
    return BTN_URL


async def ad_btn_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    url = (update.message.text or "").strip()
    if not valid_ad_url(url, cfg("ads_allow_shorteners", False)):
        await update.message.reply_text(t(lang, "ads.bad_url"), reply_markup=_exits(lang, "btn"))
        return BTN_URL
    context.user_data["ad"]["btn_url"] = url
    return await _preview(update.message, context, lang)


async def _preview(message, context, lang: str) -> int:
    data = context.user_data["ad"]
    pkg = _pkg(context)
    footer = cfg("ad_footer", "📢 Sponsored")
    body = f"{data['text']}\n\n{footer}"
    markup = ad_preview_markup(data.get("btn_text"), data.get("btn_url"))
    if data.get("photo"):
        await message.reply_photo(data["photo"], caption=body, reply_markup=markup)
    else:
        await message.reply_text(body, reply_markup=markup, disable_web_page_preview=True)
    await message.reply_text(
        t(lang, "ads.preview") + "\n\n" + cfg("ad_rules_text", ""),
        reply_markup=kb([[(t(lang, "ads.place_order", price=pkg["price_etb"]), "ads", "order")],
                         [(t(lang, "menu.cancel"), "ads", "cancel")]]),
    )
    return PREVIEW


async def preview_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    if not parsed or parsed[1] == "cancel":
        return await _cancel(update, context)
    if parsed[1] != "order":
        return await back_cb(update, context)
    data = context.user_data.get("ad") or {}
    if "text" not in data:
        return ConversationHandler.END
    adv = ensure_advertiser(update.effective_user.id, update.effective_user.full_name or "")
    pkg = _pkg(context)
    open_orders = db.scalar("SELECT count(*) FROM ads WHERE advertiser_id=? AND status IN ('draft','awaiting_payment','verifying')", (adv["id"],), 0)
    if open_orders >= cfg("max_open_orders", 2):  # re-check at order time (flow may have been idle)
        context.user_data.pop("ad", None)
        await q.message.reply_text(t(lang, "ads.too_many_open"), reply_markup=kb([[("📢 /myads", "myad", "list")]]))
        return ConversationHandler.END
    ad_id = create_order(adv["id"], pkg, data)
    db.audit(0, "ad_order", f"ad:{ad_id}", f"pkg:{pkg['slug']} price:{pkg['price_etb']}")
    context.user_data.pop("ad", None)
    screen = pay_screen(ad_id, pkg["price_etb"], lang)
    if not screen:
        await q.message.reply_text(t(lang, "ads.no_accounts"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    await q.message.reply_text(screen[0][0], entities=screen[0][1], reply_markup=screen[1])
    return ConversationHandler.END


async def back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ads:back_* — step back inside the creation flow."""
    q = update.callback_query
    parsed = verify_cb(q.data)
    lang = lang_of(update)
    target = parsed[1] if parsed else "cancel"
    data = context.user_data.get("ad")
    if data is None or "package_id" not in data:
        return await begin(update, context)
    if target == "back_pkg":
        await q.answer()
        return await begin(update, context)
    if target == "back_text":
        await q.answer()
        pkg = _pkg(context)
        await q.message.reply_text(t(lang, "ads.send_text", max=pkg["max_chars"]), reply_markup=_exits(lang, "pkg"))
        return TEXT
    if target == "back_btn":
        await q.answer()
        return await _ask_button(q.message, context, lang)
    return await _cancel(update, context)


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ad", None)
    context.user_data.pop("pay", None)
    lang = lang_of(update)
    if update.callback_query:
        await update.callback_query.answer()
    await send_home(update.effective_message, lang, t(lang, "ads.cancelled"))
    return ConversationHandler.END


async def escape_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bottom-keyboard label mid-flow: leave the ad flow and show what they tapped."""
    context.user_data.pop("ad", None)
    context.user_data.pop("pay", None)
    lang = lang_of(update)
    action = nav_action(update.message.text)
    if action == "advertise":
        return await begin(update, context)
    if not await dispatch_nav(update.message, context, lang, action):
        await send_home(update.message, lang, t(lang, "ads.cancelled"))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Payment flow (separate conversation; entry from the pay screen)
# ---------------------------------------------------------------------------

async def pay_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    if not parsed:
        return ConversationHandler.END
    action, _, ad_id_s = parsed[1].partition(":")
    ad_id = int(ad_id_s) if ad_id_s.isdigit() else -1
    if action == "cancel":
        ad = _own_ad(ad_id, update.effective_user.id, "awaiting_payment", "draft")
        if ad:
            db.execute("UPDATE ads SET status='cancelled' WHERE id=?", (ad_id,))
            db.audit(0, "ad_cancel", f"ad:{ad_id}")
        await send_home(q.message, lang, t(lang, "ads.cancelled"))
        return ConversationHandler.END
    ad = _own_ad(ad_id, update.effective_user.id, "awaiting_payment")
    if not ad:
        return ConversationHandler.END
    accounts = db.q("SELECT id, bank, account FROM payout_accounts WHERE enabled=1 ORDER BY sort, id")
    if not accounts:
        await q.message.reply_text(t(lang, "ads.no_accounts"), reply_markup=nav_keyboard(lang, update.effective_user.id))
        return ConversationHandler.END
    context.user_data["pay"] = {"ad_id": ad_id}
    rows = [[(f"{BANK_FIELDS.get(a['bank'], {}).get('label', a['bank'])} …{a['account'][-4:]}", "adb", str(a["id"]))] for a in accounts]
    rows.append([(t(lang, "menu.back"), "adb", "back")])
    await q.message.reply_text(t(lang, "ads.pick_bank"), reply_markup=kb(rows))
    return BANK


async def pick_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    pay = context.user_data.get("pay")
    if not pay:
        return ConversationHandler.END
    if not parsed or parsed[1] == "back":
        ad = _own_ad(pay["ad_id"], update.effective_user.id, "awaiting_payment")
        context.user_data.pop("pay", None)
        screen = pay_screen(ad["id"], ad["price_etb"], lang) if ad else None
        if screen:
            await q.message.reply_text(screen[0][0], entities=screen[0][1], reply_markup=screen[1])
        return ConversationHandler.END
    acc = db.one("SELECT * FROM payout_accounts WHERE id=? AND enabled=1", (parsed[1],))
    if not acc:
        return BANK
    pay.update(account_id=acc["id"], bank=acc["bank"])
    db.execute("UPDATE ads SET payout_account_id=? WHERE id=?", (acc["id"], pay["ad_id"]))
    await q.message.reply_text(t(lang, "ads.ask_reference", bank=BANK_FIELDS[acc["bank"]]["label"]),
                               reply_markup=kb([[(t(lang, "menu.cancel"), "ads", "paycancel")]]))
    return REFERENCE


async def got_reference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    pay = context.user_data.get("pay")
    if not pay:
        return ConversationHandler.END
    ref = clean_reference(update.message.text or "")
    if not ref:
        await update.message.reply_text(t(lang, "ads.bad_reference"), reply_markup=kb([[(t(lang, "menu.cancel"), "ads", "paycancel")]]))
        return REFERENCE
    pay["reference"] = ref
    spec = required_extra(pay["bank"], ref)
    if spec:
        key = "ads.ask_phone" if spec[0] == "phone" else "ads.ask_suffix"
        await update.message.reply_text(t(lang, key, n=spec[1]), reply_markup=kb([[(t(lang, "menu.cancel"), "ads", "paycancel")]]))
        return EXTRA
    return await _submit_payment(update, context, lang, "")


async def got_extra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = lang_of(update)
    pay = context.user_data.get("pay")
    if not pay:
        return ConversationHandler.END
    extra = clean_extra(pay["bank"], update.message.text or "")
    if extra is None:
        spec = BANK_FIELDS[pay["bank"]]["extra"]
        await update.message.reply_text(t(lang, "ads.ask_phone" if spec[0] == "phone" else "ads.ask_suffix", n=spec[1]),
                                        reply_markup=kb([[(t(lang, "menu.cancel"), "ads", "paycancel")]]))
        return EXTRA
    return await _submit_payment(update, context, lang, extra)


async def _submit_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, extra: str) -> int:
    pay = context.user_data.pop("pay")
    ad = _own_ad(pay["ad_id"], update.effective_user.id, "awaiting_payment")
    if not ad:
        return ConversationHandler.END
    max_attempts = cfg("payment_max_attempts", 3)
    attempts = (ad["pay_attempts"] or 0) + 1
    if attempts > max_attempts:  # cap applies to queued/invalid paths too, before spending a credit
        db.execute("UPDATE ads SET status='needs_review' WHERE id=?", (ad["id"],))
        await alert_admins(context, f"🧾 Ad #{ad['id']}: too many payment attempts; manual review.", kind="ads")
        await update.message.reply_text(t(lang, "ads.needs_review"), reply_markup=kb([[(t(lang, "ads.btn_contact"), "adc", str(ad["id"]))]]))
        return ConversationHandler.END
    db.execute("UPDATE ads SET pay_attempts=? WHERE id=?", (attempts, ad["id"]))
    await update.message.reply_text(t(lang, "ads.verifying"))
    pid = record_payment(ad["id"], pay["bank"], pay["reference"], extra)
    kind, acc = await verify_payment(context, pid)
    if kind == "completed" and acc:
        left = max_attempts - attempts
        if acc.status in ("failed", "duplicate") and left <= 0:
            db.execute("UPDATE ads SET status='needs_review' WHERE id=?", (ad["id"],))
            await alert_admins(context, f"🧾 Ad #{ad['id']}: {attempts} failed payment attempts; manual review.", kind="ads")
            await update.message.reply_text(t(lang, "ads.needs_review"), reply_markup=kb([[(t(lang, "ads.btn_contact"), "adc", str(ad["id"]))]]))
        else:
            msg_text = outcome_message(lang, acc, ad["price_etb"], attempts_left=left)
            if acc.status in ("mismatch", "needs_review"):
                await alert_admins(context, f"🧾 Ad #{ad['id']} payment needs review ({acc.reason}). Use /ads.", kind="ads")
                await update.message.reply_text(msg_text, reply_markup=kb([[(t(lang, "ads.btn_contact"), "adc", str(ad["id"]))]]))
            elif acc.status == "verified":
                await send_home(update.message, lang, msg_text)
            else:  # failed / duplicate: show the pay screen again
                await update.message.reply_text(msg_text)
                screen = pay_screen(ad["id"], ad["price_etb"], lang)
                if screen:
                    await update.message.reply_text(screen[0][0], entities=screen[0][1], reply_markup=screen[1])
    elif kind in ("queued", "retry"):
        await send_home(update.message, lang, t(lang, "ads.queued"))
    elif kind == "invalid":
        db.execute("UPDATE ads SET status='awaiting_payment' WHERE id=?", (ad["id"],))
        await update.message.reply_text(t(lang, "ads.bad_reference"))
        screen = pay_screen(ad["id"], ad["price_etb"], lang)
        if screen:
            await update.message.reply_text(screen[0][0], entities=screen[0][1], reply_markup=screen[1])
    else:
        await send_home(update.message, lang, t(lang, "ads.needs_review"))
    return ConversationHandler.END


async def pay_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Leave the reference-entry step without cancelling the order; re-show the pay screen."""
    pay = context.user_data.pop("pay", None)
    lang = lang_of(update)
    msg = update.effective_message
    if update.callback_query:
        await update.callback_query.answer()
    ad = _own_ad(pay["ad_id"], update.effective_user.id, "awaiting_payment") if pay else None
    screen = pay_screen(ad["id"], ad["price_etb"], lang) if ad else None
    if screen:
        await msg.reply_text(screen[0][0], entities=screen[0][1], reply_markup=screen[1])
    else:
        await send_home(msg, lang, t(lang, "nav.cancelled"))
    if update.message and nav_action(update.message.text):
        await dispatch_nav(update.message, context, lang, nav_action(update.message.text))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /myads
# ---------------------------------------------------------------------------

def _my_orders(user_id: int) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT a.id, a.status, a.price_etb, p.name FROM ads a JOIN advertisers adv ON adv.id=a.advertiser_id "
        "JOIN packages p ON p.id=a.package_id WHERE adv.user_id=? ORDER BY a.id DESC LIMIT 8", (user_id,))]


async def show_myads(message, user_id: int, lang: str) -> None:
    rows = _my_orders(user_id)
    if not rows:
        await message.reply_text(t(lang, "ads.none_yet"), reply_markup=nav_keyboard(lang, user_id))
        return
    buttons = [[(f"#{r['id']} · {r['name']} · {r['status'].replace('_', ' ')}", "myad", f"show:{r['id']}")] for r in rows]
    buttons.append([(t(lang, "menu.back"), "menu", "home")])
    await message.reply_text(t(lang, "ads.myads_title"), reply_markup=kb(buttons))


async def myads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private_chat(update):
        await show_myads(update.message, update.effective_user.id, lang_of(update))


async def myad_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """myad:show:<id> → order status with the right actions; myad:list → back."""
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    uid = update.effective_user.id
    if not parsed or parsed[1] == "list":
        await show_myads(q.message, uid, lang)
        return
    _, _, ad_id_s = parsed[1].partition(":")
    ad = _own_ad(int(ad_id_s), uid) if ad_id_s.isdigit() else None
    if not ad:
        await show_myads(q.message, uid, lang)
        return
    pkg = db.one("SELECT name FROM packages WHERE id=?", (ad["package_id"],))
    rows = []
    if ad["status"] == "awaiting_payment":
        rows.append([(t(lang, "ads.btn_pay_again"), "ad", f"paid:{ad['id']}"), (t(lang, "ads.btn_cancel_order"), "ad", f"cancel:{ad['id']}")])
    if ad["status"] in ("awaiting_payment", "verifying", "needs_review", "paid", "refund_due"):
        rows.append([(t(lang, "ads.btn_contact"), "adc", str(ad["id"]))])
    rows.append([(t(lang, "menu.back"), "myad", "list")])
    await q.message.reply_text(t(lang, "ads.order_status", ad_id=ad["id"], name=pkg["name"] if pkg else "?", price=ad["price_etb"],
                                 status=ad["status"].replace("_", " ")), reply_markup=kb(rows))


# ---------------------------------------------------------------------------
# Contact admin (advertiser → 💰 Ads topic), rate-limited
# ---------------------------------------------------------------------------

CONTACT = 0


async def contact_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    parsed = verify_cb(q.data)
    await q.answer()
    lang = lang_of(update)
    uid = update.effective_user.id
    ad = _own_ad(int(parsed[1]), uid) if parsed and parsed[1].isdigit() else None
    if not ad:
        return ConversationHandler.END
    from security import author_hash
    last = db.scalar("SELECT last_contact FROM users WHERE author=?", (author_hash(uid),), 0) or 0
    if db.now() - last < cfg("contact_cooldown_sec", 600):
        await q.message.reply_text(t(lang, "ads.contact_cooldown"), reply_markup=nav_keyboard(lang, uid))
        return ConversationHandler.END
    context.user_data["contact_ad"] = ad["id"]
    await q.message.reply_text(t(lang, "ads.contact_ask", ad_id=ad["id"]), reply_markup=kb([[(t(lang, "menu.cancel"), "nav", "cancel")]]))
    return CONTACT


async def contact_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from common import ensure_user
    lang = lang_of(update)
    uid = update.effective_user.id
    ad_id = context.user_data.pop("contact_ad", None)
    text = strip_invisible(update.message.text or "").strip()[:800]
    if ad_id is None or not text:
        return ConversationHandler.END
    author = ensure_user(uid)
    db.execute("UPDATE users SET last_contact=? WHERE author=?", (db.now(), author))
    summary = db.one("SELECT a.status, a.price_etb, p.reference, p.status pstatus FROM ads a LEFT JOIN payments p ON p.ad_id=a.id "
                     "WHERE a.id=? ORDER BY p.id DESC LIMIT 1", (ad_id,))
    adv_id = db.scalar("SELECT advertiser_id FROM ads WHERE id=?", (ad_id,))
    atext, aents = card(f"✉️ Advertiser message · Order #{ad_id}",
                        [("From", advertiser_name(adv_id)), ("Status", f"{summary['status'].replace('_', ' ')} · {summary['price_etb']} ETB"),
                         ("Last payment", f"{summary['pstatus'] or '—'} {summary['reference'] or ''}".strip())], text).build()
    await alert_admins(context, atext, kind="ads", entities=aents, reply_markup=kb([[("↩️ Reply", "adr", str(ad_id))]]))
    db.audit(0, "ad_contact", f"ad:{ad_id}")
    await send_home(update.message, lang, t(lang, "ads.contact_sent"))
    return ConversationHandler.END


async def contact_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("contact_ad", None)
    lang = lang_of(update)
    if update.callback_query:
        await update.callback_query.answer()
    await send_home(update.effective_message, lang, t(lang, "nav.cancelled"))
    return ConversationHandler.END


def _advertise_label_regex() -> str:
    return nav_regex(only=("advertise",))


def register(app: Application) -> None:
    label_filter = filters.TEXT & filters.Regex(nav_regex())
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("advertise", begin), CallbackQueryHandler(begin, pattern=r"^menu:advertise:"),
                      MessageHandler(filters.TEXT & filters.Regex(_advertise_label_regex()) & filters.ChatType.PRIVATE, begin)],
        states={
            PACKAGE: [CallbackQueryHandler(pick_package, pattern=r"^adp:")],
            TEXT: [MessageHandler(label_filter, escape_label), MessageHandler(filters.TEXT & ~filters.COMMAND, ad_text)],
            PHOTO: [MessageHandler(label_filter, escape_label), MessageHandler(filters.PHOTO, ad_photo),
                    CallbackQueryHandler(ad_photo, pattern=r"^ads:skip_photo:"), MessageHandler(~filters.COMMAND, ad_photo)],
            BTN_TEXT: [MessageHandler(label_filter, escape_label), MessageHandler(filters.TEXT & ~filters.COMMAND, ad_btn_text),
                       CallbackQueryHandler(ad_btn_text, pattern=r"^ads:skip_btn:")],
            BTN_URL: [MessageHandler(label_filter, escape_label), MessageHandler(filters.TEXT & ~filters.COMMAND, ad_btn_url)],
            PREVIEW: [MessageHandler(label_filter, escape_label), CallbackQueryHandler(preview_action, pattern=r"^ads:(order|cancel):")],
        },
        fallbacks=[CallbackQueryHandler(back_cb, pattern=r"^ads:back_"), CallbackQueryHandler(_cancel, pattern=r"^(ads:cancel|nav:cancel):"),
                   MessageHandler(label_filter, escape_label), CommandHandler("cancel", _cancel), CommandHandler("start", _cancel)],
        conversation_timeout=1800,
        name="advertise",
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(pay_entry, pattern=r"^ad:(paid|cancel):")],
        states={
            BANK: [CallbackQueryHandler(pick_bank, pattern=r"^adb:")],
            REFERENCE: [MessageHandler(label_filter, pay_cancel), MessageHandler(filters.TEXT & ~filters.COMMAND, got_reference)],
            EXTRA: [MessageHandler(label_filter, pay_cancel), MessageHandler(filters.TEXT & ~filters.COMMAND, got_extra)],
        },
        fallbacks=[CallbackQueryHandler(pay_cancel, pattern=r"^(ads:paycancel|nav:cancel):"), MessageHandler(label_filter, pay_cancel),
                   CommandHandler("cancel", pay_cancel), CommandHandler("start", pay_cancel)],
        conversation_timeout=1800,
        name="pay",
    ))
    app.add_handler(CommandHandler("myads", myads))
    app.add_handler(CallbackQueryHandler(myad_cb, pattern=r"^myad:"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(contact_start, pattern=r"^adc:")],
        states={CONTACT: [MessageHandler(label_filter, contact_cancel), MessageHandler(filters.TEXT & ~filters.COMMAND, contact_text)]},
        fallbacks=[CallbackQueryHandler(contact_cancel, pattern=r"^nav:cancel:"), CommandHandler("cancel", contact_cancel), CommandHandler("start", contact_cancel)],
        conversation_timeout=600, name="ad_contact",
    ))
