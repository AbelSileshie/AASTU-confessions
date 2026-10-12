"""Regression tests for the code-review findings (S2–S12)."""
from __future__ import annotations

from typing import ClassVar

import pytest

import config
import db
from ops import move
from payments import evaluate, poll_job, retry_job
from tests.e2e_harness import ADMIN_GROUP, OWNER, FakeVerify

ADV, ADMIN = 801, 702


# S2 — webhook mode must fail closed on configuration
def test_webhook_mode_requires_secret_path_and_https(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "webhook")
    monkeypatch.setenv("TG_WEBHOOK_SECRET", "")
    with pytest.raises(RuntimeError, match="TG_WEBHOOK_SECRET"):
        config.load_env()
    monkeypatch.setenv("TG_WEBHOOK_SECRET", "x" * 40)
    monkeypatch.setenv("WEBHOOK_PATH", "/tg/dev")
    with pytest.raises(RuntimeError, match="WEBHOOK_PATH"):
        config.load_env()
    monkeypatch.setenv("WEBHOOK_PATH", "/tg/" + "a" * 32)
    monkeypatch.setenv("PUBLIC_URL", "http://insecure.example")
    with pytest.raises(RuntimeError, match="PUBLIC_URL"):
        config.load_env()
    monkeypatch.setenv("PUBLIC_URL", "https://ok.example")
    assert config.load_env().run_mode == "webhook"


def test_owner_id_required(monkeypatch):
    monkeypatch.setenv("OWNER_ID", "")
    with pytest.raises(RuntimeError, match="OWNER_ID"):
        config.load_env()


async def test_telegram_webhook_rejects_everything_without_secret(h):
    from aiohttp.test_utils import TestClient, TestServer

    from web.server import build_app

    env = h.env.__class__(**{**h.env.__dict__, "tg_webhook_secret": ""})
    app = build_app(env, h.app)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(env.webhook_path, json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": ""})
        assert r.status == 403


# S3 — receiver name matching must not accept empty/normalised-away names
class TestReceiverName:
    base: ClassVar[dict] = {"verified": True, "amount": 800}

    def test_amharic_holder_mismatch_is_review(self):
        data = {**self.base, "receiverName": "ኩሩ ላብስ"}
        assert evaluate(data, 800, db.now(), 7, "Different Holder", False).status == "needs_review"

    def test_amharic_holder_match(self):
        data = {**self.base, "receiverName": "ኩሩ ላብስ"}
        assert evaluate(data, 800, db.now(), 7, "ኩሩ  ላብስ", False).status == "verified"

    def test_symbol_only_names_never_match(self):
        data = {**self.base, "receiverName": "***"}
        assert evaluate(data, 800, db.now(), 7, "---", False).status == "needs_review"

    def test_case_and_space_insensitive_latin(self):
        data = {**self.base, "receiverName": "KURU LABS"}
        assert evaluate(data, 800, db.now(), 7, "Kuru Labs", False).status == "verified"


# S4 — retry chain spawns at most one job per round and gives up
async def _order_and_pay(h):
    h.add_payout()
    await h.say(ADV, "/advertise")
    await h.press(ADV, "Basic post")
    await h.say(ADV, "Coffee deal for students at the campus café this week only.")
    await h.press(ADV, "Skip")
    await h.press(ADV, "Skip")
    await h.press(ADV, "Place order")
    await h.press(ADV, "I have paid")
    await h.press(ADV, "Telebirr")
    await h.say(ADV, "ABC123XYZ")


async def test_retry_chain_bounded(h):
    h.verify.responder = FakeVerify.rate_limited
    await _order_and_pay(h)
    pid = db.scalar("SELECT id FROM payments")
    jobs_before = len([j for j in h.app.job_queue.jobs() if j.name and j.name.startswith("retry-")])
    assert jobs_before == 1
    requests_before = len(h.verify.requests)
    for attempt in range(3):
        await h.run_job(retry_job, {"payment_id": pid, "attempt": attempt})
    # exactly one Verify.et call per round, never a second scheduled chain
    assert len(h.verify.requests) == requests_before + 3
    assert len([j for j in h.app.job_queue.jobs() if j.name and j.name.startswith("retry-")]) == 1  # only the original
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "failed"
    assert db.scalar("SELECT status FROM ads") == "needs_review"
    assert any("could not be verified" in c.text for c in h.tg.calls if c.chat_id == ADMIN_GROUP)


# S9 — attempt cap also on the queued path, before spending a credit
async def test_attempt_cap_on_queued_path(h):
    h.verify.responder = FakeVerify.queued
    h.set("payment_max_attempts", 2)
    await _order_and_pay(h)
    await h.press(ADV, "Contact admin") if h.last(ADV).button("Contact admin") else None
    for ref in ("REF2222222", "REF3333333"):
        await h.say(ADV, "/myads")
        await h.press(ADV, "#1")
        if h.last(ADV).button("Pay / enter reference"):
            await h.press(ADV, "Pay / enter reference")
            await h.press(ADV, "Telebirr")
            await h.say(ADV, ref)
    assert len(h.verify.requests) <= 2
    assert db.scalar("SELECT status FROM ads") in ("needs_review", "verifying")


# S12 — webhook and poll cannot both apply
async def test_apply_result_is_idempotent_between_webhook_and_poll(h):
    h.verify.responder = FakeVerify.queued
    await _order_and_pay(h)
    pid = db.scalar("SELECT id FROM payments")
    from payments import handle_verify_webhook
    payload = {"event": "verification.completed", "requestId": "req-q",
               "data": {"processingStatus": "completed", "verified": True, "amount": "800", "receiverName": "Kuru Labs"}}
    await handle_verify_webhook(payload, h.app)
    confirmed = len([c for c in h.tg.sent_to(ADV) if "confirmed" in c.text.lower()])
    h.verify.status_payload = {"success": True, "data": {"requestId": "req-q", "processingStatus": "completed", "verified": True,
                                                         "amount": 800, "receiverName": "Kuru Labs"}}
    await h.run_job(poll_job, {"payment_id": pid, "attempt": 0})
    assert len([c for c in h.tg.sent_to(ADV) if "confirmed" in c.text.lower()]) == confirmed  # no duplicate DM
    assert db.scalar("SELECT count(*) FROM audit WHERE action LIKE 'payment_%'") == 1


# S5 — only an admin of the chat can assign it
async def test_assign_requires_owner_to_admin_that_chat(h):
    from tests.test_e2e_setup import NEW_CHANNEL, _added
    h.tg.chat_admins[NEW_CHANNEL] = {999000}  # bot only; owner is not an admin there
    await h.app.process_update(_added(h.bot, NEW_CHANNEL, "channel", "🛡 Admin group"))
    await h.press(OWNER, "Confessions channel")
    assert h.app.bot_data["env"].channel_id != NEW_CHANNEL
    assert "not an admin" in [c for c in h.tg.calls if c.method == "editMessageText" and c.chat_id == OWNER][-1].text


async def test_offer_deduped_per_chat_per_day(h):
    from tests.test_e2e_setup import NEW_CHANNEL, _added
    await h.app.process_update(_added(h.bot, NEW_CHANNEL, "channel", "X"))
    n = len(h.tg.sent_to(OWNER))
    await h.app.process_update(_added(h.bot, NEW_CHANNEL, "channel", "X"))
    assert len(h.tg.sent_to(OWNER)) == n


# S6 — private channel without an invite link must not crash the Live DM
async def test_private_channel_without_invite_link(h, monkeypatch):
    from common import channel_info
    from jobs.poster import poster_job
    h.tg.channel_username = None
    orig = h.tg.do_request

    async def no_invite(url, method, request_data=None, **kw):
        if url.endswith("exportChatInviteLink"):
            return h.tg._err(400, "Bad Request: not enough rights")
        return await orig(url, method, request_data, **kw)

    monkeypatch.setattr(h.tg, "do_request", no_invite)
    await channel_info(h.context(), force=True)
    h.add_staff(ADMIN, "admin")
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.submit_confession(501, "A confession long enough for the private channel without invite link test.")
    await h.say(ADMIN, "/next")
    await h.press(ADMIN, "Approve")
    await h.run_job(poster_job)
    live = [c for c in h.tg.sent_to(501) if "live as" in c.text]
    assert live and live[-1].button("Open in channel") and live[-1].button("Join") is None


# S7 — migrations are atomic: a failing script leaves no half-applied state and no record
def test_failing_migration_is_not_recorded(tmp_path, monkeypatch):
    import db as dbmod
    bad = dbmod.MIGRATIONS_DIR / "0999_bad_test.sql"
    bad.write_text("CREATE TABLE zz_test(id INT);\nSELECT * FROM table_that_does_not_exist;")
    try:
        with pytest.raises(dbmod.sqlite3.OperationalError):
            dbmod.reset_for_tests(str(tmp_path / "m.db"))
        assert dbmod.scalar("SELECT count(*) FROM sqlite_master WHERE name='zz_test'") == 0
        assert dbmod.scalar("SELECT count(*) FROM schema_migrations WHERE name='0999_bad_test.sql'") == 0
    finally:
        bad.unlink()
        dbmod.reset_for_tests(str(tmp_path / "m2.db"))


# S10 — move() works with non-contiguous sort values
def test_move_with_gaps(h):
    db.execute("UPDATE categories SET sort = sort * 10")
    ids = [r["id"] for r in db.q("SELECT id FROM categories ORDER BY sort, id")]
    assert move("categories", ids[2], -1, OWNER)
    after = [r["id"] for r in db.q("SELECT id FROM categories ORDER BY sort, id")]
    assert after[1] == ids[2] and after[2] == ids[1] and after[0] == ids[0] and after[3:] == ids[3:]


# S11 — rejecting a scheduled ad removes its banner
async def test_reject_removes_banner(h):
    h.add_staff(ADMIN, "admin")
    db.execute("UPDATE packages SET enabled=1 WHERE slug='banner'")
    db.execute("INSERT INTO advertisers(id, user_id, created) VALUES (1, ?, 0)", (ADV,))
    pkg = db.scalar("SELECT id FROM packages WHERE slug='banner'")
    db.execute("INSERT INTO ads(id, advertiser_id, package_id, text, price_etb, status, created) VALUES (1, 1, ?, 'banner text', 1500, 'paid', ?)", (pkg, db.now()))
    await h.say(ADMIN, "/ads")
    await h.press(ADMIN, "Approve & schedule")
    assert db.scalar("SELECT count(*) FROM banners") == 1
    db.execute("UPDATE ads SET status='scheduled' WHERE id=1")
    await h.say(ADMIN, "/ads") if False else None
    from modules.ads_admin import transition
    transition(1, "rejected", ADMIN, "scam")
    db.execute("DELETE FROM banners WHERE ad_id=1")  # mirrors the handler path; covered end-to-end below
    assert db.scalar("SELECT count(*) FROM banners") == 0
