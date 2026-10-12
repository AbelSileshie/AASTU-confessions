"""Adversarial / security tests: forgery, IDOR, privilege escalation, injection, evasion, DoS inputs."""
from __future__ import annotations

import json

import db
from jobs.poster import poster_job
from security import author_hash, sign_cb
from tests.e2e_harness import ADMIN_GROUP, CHANNEL, OWNER
from tests.fake_telegram import callback_update

STUDENT, ATTACKER, MOD, ADMIN = 501, 666, 701, 702
CONFESSION = "A perfectly ordinary confession that is long enough to pass the length check."


async def _queue_one(h, uid=STUDENT, text=CONFESSION):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    await h.submit_confession(uid, text)


# ---------------------------------------------------------------------------
# Callback forgery / replay
# ---------------------------------------------------------------------------

async def test_forged_unsigned_callbacks_are_ignored_everywhere(h):
    await _queue_one(h)
    sub_id = db.scalar("SELECT id FROM submissions")
    await h.say(MOD, "/next")
    card = h.last(MOD)
    for data in (f"rv:ok:{sub_id}", f"rv:ok:{sub_id}:badsig1234", "st:k:min_len:0000000000", "ada:approve:1:x",
                 "menu:confess:", "cn:yes:1:0000000000", "td:go:1:0000000000"):
        await h.app.process_update(callback_update(h.bot, MOD, data, card))
    assert db.scalar("SELECT count(*) FROM submissions") == 1


async def test_valid_signature_but_wrong_actor_is_rejected(h):
    """Attacker learns a real signed payload (e.g. screenshot) and replays it."""
    await _queue_one(h)
    await h.say(MOD, "/next")
    card = h.last(MOD)
    approve = card.callback("Approve")
    await h.app.process_update(callback_update(h.bot, ATTACKER, approve, card, chat_id=ATTACKER))
    assert db.scalar("SELECT count(*) FROM submissions") == 1
    assert h.tg.sent_to(ATTACKER) == []


async def test_signed_payload_cannot_be_retargeted(h):
    await _queue_one(h)
    await h.submit_confession(ATTACKER, "Attacker confession that should not get approved by payload tampering.")
    await h.say(MOD, "/next")
    card = h.last(MOD)
    mine = db.scalar("SELECT id FROM submissions WHERE claimed_by=?", (MOD,))
    other = db.scalar("SELECT id FROM submissions WHERE claimed_by IS NULL")
    tampered = card.callback("Approve").replace(f"ok:{mine}:", f"ok:{other}:")
    await h.app.process_update(callback_update(h.bot, MOD, tampered, card))
    assert db.scalar("SELECT count(*) FROM submissions") == 2


async def test_approve_replay_after_consumed(h):
    await _queue_one(h)
    await h.say(MOD, "/next")
    card = h.last(MOD)
    approve = card.callback("Approve")
    await h.app.process_update(callback_update(h.bot, MOD, approve, card))
    await h.app.process_update(callback_update(h.bot, MOD, approve, card))
    assert db.scalar("SELECT count(*) FROM outbox") == 1


# ---------------------------------------------------------------------------
# Privilege boundaries
# ---------------------------------------------------------------------------

async def test_moderator_cannot_use_admin_or_owner_commands(h):
    h.add_staff(MOD)
    for cmd in ("/settings", "/set min_len 1", "/staff add", "/packages", "/payout_accounts", "/export",
                "/ads", "/revenue", "/refunds", "/unblock x", "/unban abc", "/mediakit", "/audit", "/post_rules", "/categories"):
        await h.say(MOD, cmd)
    assert h.tg.sent_to(MOD) == []
    assert db.scalar("SELECT value FROM settings WHERE key='min_len'") == "20"


async def test_admin_cannot_use_owner_commands(h):
    h.add_staff(ADMIN, "admin")
    for cmd in ("/payout_accounts add telebirr 0911111111 X", "/export", "/refunds"):
        await h.say(ADMIN, cmd)
    assert h.tg.sent_to(ADMIN) == []
    assert db.scalar("SELECT count(*) FROM payout_accounts") == 0


async def test_moderator_cannot_permanently_ban(h):
    await _queue_one(h)
    await h.say(MOD, "/next")
    await h.press(MOD, "Ban")
    await h.press(MOD, "Permanent")
    assert db.scalar("SELECT banned_until FROM users WHERE author=?", (author_hash(STUDENT),)) == 0


async def test_invite_cannot_demote_and_cannot_be_redeemed_twice(h):
    h.add_staff(ADMIN, "admin")
    await h.say(OWNER, "/staff add moderator")
    code = h.last(OWNER).text.split("start=")[1].strip()
    await h.say(ADMIN, f"/start {code}")
    assert db.scalar("SELECT role FROM staff WHERE user_id=?", (ADMIN,)) == "admin"
    await h.say(ATTACKER, f"/start {code}")
    assert db.scalar("SELECT count(*) FROM staff WHERE user_id=?", (ATTACKER,)) == 0


async def test_expired_invite_rejected(h):
    await h.say(OWNER, "/staff add")
    code = h.last(OWNER).text.split("start=")[1].strip()
    db.execute("UPDATE invites SET expires=0")
    await h.say(ATTACKER, f"/start {code}")
    assert "invalid" in h.last(ATTACKER).text
    assert db.scalar("SELECT count(*) FROM staff") == 0


async def test_removed_staff_lose_access_immediately(h):
    h.add_staff(MOD)
    await h.say(OWNER, f"/staff remove {MOD}")
    await h.say(MOD, "/next")
    assert h.tg.sent_to(MOD) == []


# ---------------------------------------------------------------------------
# IDOR
# ---------------------------------------------------------------------------

async def test_cannot_delete_or_takedown_others_posts(h):
    await _queue_one(h)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    for n in ("1", "#1", "01", "1 ", "9999", "-1", "1; DROP TABLE posts", "1 OR 1=1"):
        await h.say(ATTACKER, f"/delete {n}")
    assert db.scalar("SELECT deleted FROM posts WHERE number=1") == 0
    assert db.scalar("SELECT count(*) FROM posts") == 1


async def test_comment_report_requires_distinct_users(h):
    await _queue_one(h)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    await h.say(ATTACKER, "/start c_1")
    await h.press(ATTACKER, "Write a comment")
    await h.say(ATTACKER, "victim comment")
    cid = db.scalar("SELECT id FROM comments")
    await h.say(STUDENT, "/start c_1")
    rep = h.last(STUDENT).callback("Report Anon 1")
    h.set("report_cooldown_sec", 0)
    for _ in range(10):
        await h.app.process_update(callback_update(h.bot, STUDENT, rep, h.last(STUDENT)))
    assert db.scalar("SELECT reports FROM comments WHERE id=?", (cid,)) == 1
    assert db.scalar("SELECT hidden FROM comments WHERE id=?", (cid,)) == 0


# ---------------------------------------------------------------------------
# Injection / evasion / oversize
# ---------------------------------------------------------------------------

async def test_sql_and_markup_injection_is_stored_and_rendered_literally(h):
    payload = "Robert'); DROP TABLE submissions;-- <b>bold</b> *md* `code` {format} %s"
    await _queue_one(h, text=payload + " padding to reach the minimum length easily.")
    assert db.scalar("SELECT count(*) FROM submissions") == 1
    await h.say(MOD, "/next")
    assert payload in h.last(MOD).text
    await h.press(MOD, "Approve")
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    posted = [c for c in h.tg.calls if c.chat_id == CHANNEL and c.method == "sendMessage"][-1]
    assert payload in posted.text and not posted.params.get("parse_mode")
    assert db.scalar("SELECT count(*) FROM sqlite_master WHERE name='submissions'") == 1


async def test_unicode_evasion_of_filters(h):
    await h.say(STUDENT, "✍️ Confess")
    await h.press(STUDENT, "Rant")
    await h.press(STUDENT, "Done")
    for evasive in (
        "call me on 09\u200b11\u200b22\u200b33\u200b44 tonight please ok",          # zero-width in phone
        "my handle is @\u200bsome_user_name find me there anytime",                # zero-width in mention
        "ｈｔｔｐｓ://evil.example/path fullwidth link attempt here",                # fullwidth URL
        "see HTTPS://EVIL.EXAMPLE for details about everything else",              # uppercase scheme
    ):
        await h.say(STUDENT, evasive)
        assert "not allowed" in h.last(STUDENT).text
    assert db.scalar("SELECT count(*) FROM submissions") == 0


async def test_oversized_inputs_rejected_not_crash(h):
    await h.say(STUDENT, "✍️ Confess")
    await h.press(STUDENT, "Rant")
    await h.press(STUDENT, "Done")
    await h.say(STUDENT, "x" * 4096)
    assert "Too long" in h.last(STUDENT).text
    await h.say(OWNER, "/set welcome_text " + "y" * 4096)
    assert "too long" in h.last(OWNER).text
    await h.say(OWNER, "/block " + "z" * 500)
    assert db.scalar("SELECT max(length(word)) FROM wordlist") <= 64


async def test_settings_type_validation(h):
    for bad in ("/set min_len abc", "/set min_len -5", "/set min_len 99999999", "/set comments_enabled maybe",
                "/set rules_text", "/set post_interval_sec 1e3"):
        await h.say(OWNER, bad)
        assert "❌" in h.last(OWNER).text or "Usage" in h.last(OWNER).text
    assert db.scalar("SELECT value FROM settings WHERE key='min_len'") == "20"


async def test_import_only_touches_whitelisted_tables(h, monkeypatch):
    from modules.settings import import_config

    evil = {"staff": [{"user_id": ATTACKER, "role": "owner"}], "users": [], "sqlite_master": [],
            "settings": [{"key": "min_len", "value": "1", "type": "int", "grp": "submissions", "evil_col": "x"}]}
    import_config(evil, OWNER)
    assert db.scalar("SELECT count(*) FROM staff") == 0
    assert db.scalar("SELECT value FROM settings WHERE key='min_len'") == "1"


async def test_payout_account_input_validation(h):
    for bad in ("/payout_accounts add notabank 0911 X", "/payout_accounts add telebirr 12 X",
                "/payout_accounts add telebirr 0911223344' OR 1=1-- X"):
        await h.say(OWNER, bad)
    assert db.scalar("SELECT count(*) FROM payout_accounts") == 0
    await h.say(OWNER, '/payout_accounts add telebirr 0911223344 "Kuru Labs"')
    assert db.scalar("SELECT account FROM payout_accounts") == "0911223344"


async def test_ad_text_cannot_smuggle_unsafe_button(h):
    """Buttons JSON in outbox is the only path to channel buttons; non-https is dropped."""
    from jobs.poster import _ad_markup

    assert _ad_markup(json.dumps([{"text": "x", "url": "javascript:alert(1)"}])) is None
    assert _ad_markup(json.dumps([{"text": "x", "url": "http://plain.example"}])) is None
    assert _ad_markup("not json") is None
    assert _ad_markup(json.dumps([{"text": "ok", "url": "https://ok.example"}])) is not None


# ---------------------------------------------------------------------------
# Anonymity guarantees
# ---------------------------------------------------------------------------

async def test_database_never_contains_raw_telegram_ids_of_users(h):
    h.add_payout()
    await _queue_one(h)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    await h.say(ATTACKER, "/start c_1")
    await h.press(ATTACKER, "Write a comment")
    await h.say(ATTACKER, "hello")
    for table in ("users", "submissions", "outbox", "posts", "comments", "comment_reports", "connect_requests"):
        rows = [dict(r) for r in db.q(f"SELECT * FROM {table}")]  # nosec B608 - test constant
        blob = json.dumps(rows, default=str)
        for uid in (STUDENT, ATTACKER):
            assert str(uid) not in blob.replace(author_hash(uid), ""), f"raw id {uid} leaked in {table}"


async def test_notifications_survive_blocked_user_without_crash(h):
    await _queue_one(h)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    h.tg.fail_chats.add(STUDENT)
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    assert db.scalar("SELECT count(*) FROM posts") == 1
    assert db.scalar("SELECT chat_ref FROM users WHERE author=?", (author_hash(STUDENT),)) is None


async def test_open_order_cap(h):
    h.add_payout()
    for _ in range(2):
        await h.say(ATTACKER, "/advertise")
        await h.press(ATTACKER, "Basic post")
        await h.say(ATTACKER, "Coffee deal for students at the campus café this week only.")
        await h.press(ATTACKER, "Skip")
        await h.press(ATTACKER, "Skip")
        await h.press(ATTACKER, "Place order")
    assert db.scalar("SELECT count(*) FROM ads") == 2
    await h.say(ATTACKER, "/advertise")
    assert "unpaid orders" in h.last(ATTACKER).text
    assert db.scalar("SELECT count(*) FROM ads") == 2


async def test_takedown_request_cooldown(h):
    await _queue_one(h)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    for _ in range(3):
        await h.say(ATTACKER, "/rules")
        await h.press(ATTACKER, "Request takedown")
        await h.say(ATTACKER, "1")
        await h.say(ATTACKER, "spam spam")
    assert len([c for c in h.tg.calls if c.chat_id == ADMIN_GROUP and "Takedown request" in c.text]) == 1
    assert any("wait" in t.lower() for t in h.texts(ATTACKER))


def test_longest_callback_payloads_fit_telegram_limit():

    samples = [("hub", "staff:9999999999:role:moderator"), ("rv", "rejr:99999999:explicit"), ("ada", "manual_ok:99999999"),
               ("cn", "ask:99999999"), ("word", "99999999:del:block"), ("pkg", "99999999:set:bot_banner_days"),
               ("cm", "pg:99999999:99999999"), ("mine", "delok:99999999"), ("fr", "post:99999999")]
    for a, p in samples:
        assert len(sign_cb(a, p).encode()) <= 64, (a, p)
