"""Operational data functions shared by slash commands and the /admin hub.

Pure-ish (DB in, result out). No Telegram objects here, so both the text
commands and the inline menus call exactly the same code.
"""
from __future__ import annotations

import db
from security import ROLE_RANK, new_token, role_of
from textfilter import normalize

INVITE_TTL = 24 * 3600
ASSIGNABLE_ROLES = ("moderator", "admin")
MAX_WORD_LEN = 64

PACKAGE_FIELDS = {
    "name": "text", "description": "text", "price_etb": "int", "posts": "int", "pin_hours": "int",
    "bot_banner_days": "int", "max_chars": "int", "allow_media": "bool", "allow_button": "bool",
    "enabled": "bool", "sort": "int",
}
CATEGORY_FIELDS = {"label": "text", "hashtag": "text", "enabled": "bool", "sort": "int"}
TOGGLE_TABLES = {"categories", "packages", "payout_accounts"}


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------

def can_assign(actor: int, role: str) -> bool:
    """Admins may create moderators; only the owner may create admins."""
    return role in ASSIGNABLE_ROLES and ROLE_RANK[role_of(actor)] > ROLE_RANK[role]


def outranks(actor: int, target: int) -> bool:
    return ROLE_RANK[role_of(actor)] > ROLE_RANK[role_of(target)]


def create_invite(role: str, by: int) -> str:
    code = new_token(12)
    db.execute("INSERT INTO invites(code, role, created_by, expires) VALUES (?,?,?,?)", (code, role, by, db.now() + INVITE_TTL))
    db.audit(by, "invite_create", role, code[:6])
    return code


def redeem_invite(code: str, user_id: int, display: str) -> str | None:
    now = db.now()
    with db.tx():
        inv = db.one("SELECT role FROM invites WHERE code=? AND used_by IS NULL AND expires>?", (code, now))
        if not inv:
            return None
        db.execute("UPDATE invites SET used_by=? WHERE code=?", (user_id, code))
        current = db.one("SELECT role, active FROM staff WHERE user_id=?", (user_id,))
        if current and current["active"] and ROLE_RANK[current["role"]] >= ROLE_RANK[inv["role"]]:
            return current["role"]  # never demote via an invite link
        db.execute(
            "INSERT INTO staff(user_id, role, display, added_at, active) VALUES (?,?,?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, active=1, display=excluded.display",
            (user_id, inv["role"], display[:64], now),
        )
    db.audit(user_id, "invite_redeem", inv["role"], code[:6])
    return inv["role"]


def set_role(actor: int, target: int, role: str) -> bool:
    if not can_assign(actor, role) or not outranks(actor, target):
        return False
    n = db.execute("UPDATE staff SET role=? WHERE user_id=? AND active=1", (role, target))
    if n:
        db.audit(actor, "staff_role", str(target), role)
    return bool(n)


def remove_staff(actor: int, target: int) -> bool:
    if not outranks(actor, target) or role_of(target) == "user":
        return False
    db.execute("UPDATE staff SET active=0, on_shift=0 WHERE user_id=?", (target,))
    db.audit(actor, "staff_remove", str(target))
    return True


def staff_list() -> list[dict]:
    return [dict(r) for r in db.q("SELECT user_id, role, display, active, on_shift FROM staff WHERE active=1 ORDER BY role DESC, added_at")]


def set_shift(user_id: int, on: bool) -> None:
    db.execute("INSERT INTO staff(user_id, role, on_shift, added_at) VALUES (?,?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET on_shift=excluded.on_shift",
               (user_id, role_of(user_id) if role_of(user_id) != "user" else "moderator", int(on), db.now()))


# ---------------------------------------------------------------------------
# Catalog (categories / packages / payout accounts)
# ---------------------------------------------------------------------------

def _coerce(kind: str, raw: str):
    if kind == "int":
        v = int(raw)
        if v < 0 or v > 10_000_000:
            raise ValueError
        return v
    if kind == "bool":
        return 1 if raw.strip().lower() in ("1", "true", "yes", "on") else 0
    raw = raw.strip()
    if not raw or len(raw) > 500:
        raise ValueError
    return raw


def update_field(table: str, fields: dict, row_id: int, field: str, raw: str, actor: int) -> bool:
    if table not in ("categories", "packages") or field not in fields:
        return False
    try:
        value = _coerce(fields[field], raw)
    except ValueError:
        return False
    n = db.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (value, row_id))  # nosec B608 - whitelisted identifiers
    if n:
        db.audit(actor, f"{table[:-1]}_set", str(row_id), f"{field}={str(value)[:40]}")
    return n > 0


def toggle(table: str, row_id: int, actor: int) -> bool:
    if table not in TOGGLE_TABLES:
        return False
    n = db.execute(f"UPDATE {table} SET enabled=1-enabled WHERE id=?", (row_id,))  # nosec B608 - whitelisted
    if n:
        db.audit(actor, f"{table}_toggle", str(row_id))
    return n > 0


def move(table: str, row_id: int, delta: int, actor: int) -> bool:
    """Swap sort order with the neighbour above (-1) or below (+1)."""
    if table not in ("categories", "packages"):
        return False
    rows = [dict(r) for r in db.q(f"SELECT id, sort FROM {table} ORDER BY sort, id")]  # nosec B608
    idx = next((i for i, r in enumerate(rows) if r["id"] == row_id), None)
    if idx is None or not (0 <= idx + delta < len(rows)):
        return False
    a, b = rows[idx], rows[idx + delta]
    with db.tx():
        db.execute(f"UPDATE {table} SET sort=? WHERE id=?", (idx + delta + 1, a["id"]))  # nosec B608
        db.execute(f"UPDATE {table} SET sort=? WHERE id=?", (idx + 1, b["id"]))  # nosec B608
    db.audit(actor, f"{table}_move", str(row_id), str(delta))
    return True


def add_category(slug: str, label: str, hashtag: str, actor: int) -> bool:
    slug = slug.lower()[:32]
    if not slug.isidentifier() or not hashtag.startswith("#") or not label.strip():
        return False
    n = db.execute("INSERT OR IGNORE INTO categories(slug, label, hashtag, sort) VALUES (?,?,?,?)",
                   (slug, label[:40], hashtag[:40], db.scalar("SELECT coalesce(max(sort),0)+1 FROM categories")))
    db.audit(actor, "category_add", slug)
    return bool(n)


def add_package(slug: str, name: str, price: int, actor: int) -> bool:
    slug = slug.lower()[:32]
    if not slug.isidentifier() or price < 0 or not name.strip():
        return False
    n = db.execute("INSERT OR IGNORE INTO packages(slug, name, price_etb, sort) VALUES (?,?,?,?)",
                   (slug, name[:60], price, db.scalar("SELECT coalesce(max(sort),0)+1 FROM packages")))
    db.audit(actor, "package_add", slug, str(price))
    return bool(n)


def add_payout(bank: str, account: str, holder: str, actor: int, banks: tuple[str, ...]) -> bool:
    bank = bank.lower()
    account = account.replace(" ", "")
    if bank not in banks or not account.isdigit() or not (6 <= len(account) <= 20) or not holder.strip():
        return False
    db.execute("INSERT INTO payout_accounts(bank, account, holder, sort) VALUES (?,?,?,?)",
               (bank, account, holder[:80], db.scalar("SELECT coalesce(max(sort),0)+1 FROM payout_accounts")))
    db.audit(actor, "payout_add", bank, account[-4:])
    return True


# ---------------------------------------------------------------------------
# Wordlist
# ---------------------------------------------------------------------------

def add_word(word: str, kind: str, actor: int) -> bool:
    w = word.strip()[:MAX_WORD_LEN]
    if kind not in ("block", "risk") or not normalize(w):
        return False
    db.execute("INSERT OR IGNORE INTO wordlist(word, kind, added_by, added_at) VALUES (?,?,?,?)", (w, kind, actor, db.now()))
    db.audit(actor, f"word_add_{kind}", w)
    return True


def remove_word(word: str, actor: int, kind: str | None = None) -> int:
    if kind:
        n = db.execute("DELETE FROM wordlist WHERE word=? AND kind=?", (word.strip(), kind))
    else:
        n = db.execute("DELETE FROM wordlist WHERE word=?", (word.strip(),))
    db.audit(actor, "word_remove", word.strip()[:MAX_WORD_LEN])
    return n


def remove_word_by_rowid(rowid: int, actor: int) -> bool:
    row = db.one("SELECT word FROM wordlist WHERE rowid=?", (rowid,))
    if not row:
        return False
    db.execute("DELETE FROM wordlist WHERE rowid=?", (rowid,))
    db.audit(actor, "word_remove", row["word"][:MAX_WORD_LEN])
    return True


def words(kind: str) -> list[dict]:
    return [dict(r) for r in db.q("SELECT rowid AS id, word FROM wordlist WHERE kind=? ORDER BY word", (kind,))]


# ---------------------------------------------------------------------------
# Stats / health
# ---------------------------------------------------------------------------

def gather_stats(now: int) -> dict:
    day, week, month = now - 86400, now - 7 * 86400, now - 30 * 86400
    return {
        "queue": db.scalar("SELECT count(*) FROM submissions", default=0),
        "claimed": db.scalar("SELECT count(*) FROM submissions WHERE claimed_by IS NOT NULL", default=0),
        "outbox": db.scalar("SELECT count(*) FROM outbox WHERE stage='ready'", default=0),
        "declined": db.scalar("SELECT count(*) FROM declined", default=0),
        "posted_today": db.scalar("SELECT count(*) FROM posts WHERE posted_at>?", (day,), 0),
        "posted_7d": db.scalar("SELECT count(*) FROM posts WHERE posted_at>?", (week,), 0),
        "comments_today": db.scalar("SELECT count(*) FROM comments WHERE created>?", (day,), 0),
        "pending_ads": db.scalar("SELECT count(*) FROM ads WHERE status IN ('paid','needs_review')", default=0),
        "revenue_30d": db.scalar("SELECT coalesce(sum(amount),0) FROM payments WHERE status IN ('verified','manual_ok') AND completed_at>?", (month,), 0),
        "on_shift": db.scalar("SELECT count(*) FROM staff WHERE on_shift=1 AND active=1", default=0),
        "last_post_at": int(db.scalar("SELECT v FROM meta WHERE k='last_post_at'", default="0") or 0),
        "last_backup_at": int(db.scalar("SELECT v FROM meta WHERE k='last_backup_at'", default="0") or 0),
        "mods_today": [dict(r) for r in db.q(
            "SELECT actor, count(*) n FROM audit WHERE action IN ('approve','reject') AND at>? GROUP BY actor ORDER BY n DESC", (day,))],
    }


def humanize_age(now: int, ts: int) -> str:
    if not ts:
        return "never"
    d = max(0, now - ts)
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    return f"{d // 86400} d ago"


def health_text(now: int, name_of=None):
    """(text, entities) — the one stats/health rendering used by /stats, 📊 Stats and ⚙️ Admin → Health.
    `name_of(user_id)` resolves staff names (injected to keep ops free of common)."""
    from fmt import Rich

    s = gather_stats(now)
    name_of = name_of or (lambda uid: "System" if not uid else str(uid))
    r = (Rich().bold("🩺 Status").nl()
         .label("Queue", f"{s['queue']} ({s['claimed']} claimed)").text(" · ").label("Ready", str(s["outbox"])).text(" · ").label("Declined", str(s["declined"])).nl()
         .label("Last post", humanize_age(now, s["last_post_at"])).text(" · ").label("Last backup", humanize_age(now, s["last_backup_at"])).nl()
         .label("Posted", f"{s['posted_today']} today · {s['posted_7d']} in 7d").text(" · ").label("Comments today", str(s["comments_today"])).nl()
         .label("Ads pending", str(s["pending_ads"])).text(" · ").label("Revenue 30d", f"{s['revenue_30d']} ETB").text(" · ").label("On shift", str(s["on_shift"])).nl(2)
         .bold("Decisions today").nl())
    r.text(" · ".join(f"{name_of(m['actor'])} {m['n']}" for m in s["mods_today"]) or "–")
    return r.build()
