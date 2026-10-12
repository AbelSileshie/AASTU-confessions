"""E2E: advertiser flow, Verify.et verification, acceptance checks, admin ad tools, poster spacing."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import db
import payments
from jobs.housekeeping import expire_orders
from jobs.poster import poster_job, unpin_job
from payments import VerifyOutcome, apply_result, handle_verify_webhook, poll_job
from tests.e2e_harness import ADMIN_GROUP, CHANNEL, OWNER, FakeVerify

pytestmark = pytest.mark.asyncio

ADV = 801
ADV2 = 802
MOD = 701
AD_TEXT = "Fresh coffee at Campus Café, 20% off for students this week!"


async def _order(h, uid=ADV, package="Basic post", with_button=True, photo=False) -> int:
    await h.say(uid, "/advertise")
    await h.press(uid, package)
    await h.say(uid, AD_TEXT)
    if photo:
        await h.photo(uid)
    else:
        await h.press(uid, "Skip")
    if with_button:
        await h.say(uid, "Order now")
        await h.say(uid, "https://campuscafe.example.com/deal")
    else:
        await h.press(uid, "Skip")
    await h.press(uid, "Place order")
    return db.scalar("SELECT max(id) FROM ads")


async def _pay(h, uid=ADV, reference="ABC123XYZ", extra=None):
    await h.press(uid, "I have paid")
    await h.press(uid, "Telebirr" if extra is None else "CBE")
    await h.say(uid, reference)
    if extra is not None:
        await h.say(uid, extra)


async def test_order_shows_pay_screen_with_configured_accounts(h):
    h.add_payout()
    ad_id = await _order(h)
    screen = h.last(ADV)
    assert f"Order #{ad_id}" in screen.text and "800 ETB" in screen.text and "0939080897" in screen.text and "Kuru Labs" in screen.text
    assert screen.button("I have paid") and screen.button("Cancel order")
    ad = dict(db.one("SELECT * FROM ads WHERE id=?", (ad_id,)))
    assert ad["status"] == "awaiting_payment" and ad["price_etb"] == 800 and ad["button_url"].startswith("https://")


async def test_no_payout_accounts_blocks_order(h):
    await _order(h)
    assert "not available" in h.last(ADV).text


async def test_ad_url_validation(h):
    h.add_payout()
    await h.say(ADV, "/advertise")
    await h.press(ADV, "Basic post")
    await h.say(ADV, AD_TEXT)
    await h.press(ADV, "Skip")
    await h.say(ADV, "Go")
    for bad in ("http://x.com", "javascript:alert(1)", "https://bit.ly/abc", "https://t.me/+abc", "not a url"):
        await h.say(ADV, bad)
        assert "not allowed" in h.last(ADV).text
    await h.say(ADV, "https://ok.example.com")
    assert "Place order" in h.last(ADV).button("Place order")["text"]


async def test_successful_telebirr_payment_end_to_end(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    ad_id = await _order(h)
    await _pay(h)
    # request shape sent to Verify.et
    req = h.verify.requests[-1]
    assert req["body"] == {"bank": "telebirr", "transactionNumber": "ABC123XYZ", "settlementAccount": "0939080897"}
    assert req["headers"]["idempotency-key"].startswith(f"ad-{ad_id}-")
    assert req["headers"]["x-api-key"] == "VERIFY_BANK_ET_test"
    assert "x-webhook-url" not in req["headers"]
    assert req["params"]["waitMs"] == "5000"
    assert "confirmed" in h.last(ADV).text.lower()
    p = dict(db.one("SELECT * FROM payments"))
    assert p["status"] == "verified" and p["amount"] == 800 and p["receiver_match"] == 1 and p["confirmed_before"] == 0
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"

    # admin approves → outbox → poster publishes with footer & button
    await h.say(MOD, "/ads")
    card = h.last(MOD)
    assert "ABC123XYZ" in card.text and "receiver ✓" in card.text
    await h.press(MOD, "Approve & schedule")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "scheduled"
    assert any("approved" in t for t in h.texts(ADV))
    h.set("post_interval_sec", 0)
    h.set("min_posts_between_ads", 0)
    await h.run_job(poster_job)
    posted = [c for c in h.tg.calls if c.chat_id == CHANNEL and c.method == "sendMessage"]
    assert posted and "📢 Sponsored" in posted[-1].text and posted[-1].button("Order now")["url"].startswith("https://campuscafe")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "done"
    assert any("live" in t for t in h.texts(ADV))
    await h.say(MOD, "/revenue")
    assert "800 ETB" in h.last(MOD).text


async def test_reused_reference_is_rejected(h):
    h.add_payout()
    await _order(h)
    await _pay(h)
    assert db.scalar("SELECT count(*) FROM payments WHERE status='verified'") == 1
    await _order(h, uid=ADV2)
    await _pay(h, uid=ADV2)  # same reference, Verify.et still says verified
    assert "already used" in h.last(ADV2).text or "already used" in h.texts(ADV2)[-2]
    assert db.scalar("SELECT count(*) FROM payments WHERE status='verified'") == 1
    assert db.scalar("SELECT status FROM ads WHERE advertiser_id=(SELECT id FROM advertisers WHERE user_id=?)", (ADV2,)) == "awaiting_payment"


async def test_verify_et_says_confirmed_before_is_duplicate(h):
    h.add_payout()
    h.verify.responder = FakeVerify.with_result(confirmed_before=True)
    await _order(h)
    await _pay(h)
    assert db.scalar("SELECT status FROM payments") == "duplicate"


async def test_wrong_receiver_goes_to_manual_review(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(matched=False)
    ad_id = await _order(h)
    await _pay(h)
    from common import ensure_user
    author = ensure_user(ADV)
    db.execute("UPDATE users SET lang='am' WHERE author=?", (author,))
    assert "admin will check" in h.last(ADV).text.lower()
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "needs_review"
    assert "needs review" in h.last(ADMIN_GROUP).text
    await h.say(MOD, "/ads")
    assert h.last(MOD).button("Accept payment")
    await h.press(MOD, "Decline")
    await h.press(MOD, "Wrong receiver")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "cancelled"
    assert db.scalar("SELECT status FROM payments") == "manual_no"
    assert db.scalar("SELECT reject_reason FROM ads WHERE id=?", (ad_id,)) == "wrong_receiver"
    assert "ተቀባይ" in h.last(ADV).text


async def test_underpayment_mismatch_then_manual_accept(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(amount=500)
    ad_id = await _order(h)
    await _pay(h)
    assert "500 ETB" in h.last(ADV).text and "800 ETB" in h.last(ADV).text
    assert db.scalar("SELECT status FROM payments") == "mismatch"
    await h.say(MOD, "/ads")
    await h.press(MOD, "Accept payment")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"
    assert db.scalar("SELECT status FROM payments") == "manual_ok"
    assert db.scalar("SELECT count(*) FROM audit WHERE action='ad_paid' AND detail='manual_ok'") == 1


async def test_overpayment_allowed_and_old_receipt_flagged(h):
    h.add_payout()
    h.verify.responder = FakeVerify.with_result(amount=1000)
    await _order(h)
    await _pay(h)
    assert db.scalar("SELECT status FROM payments") == "verified"
    h.verify.responder = FakeVerify.with_result(ts="2020-01-01T00:00:00Z", ref="OLD1")
    await _order(h, uid=ADV2)
    await _pay(h, uid=ADV2, reference="OLDREF9")
    assert db.scalar("SELECT reason FROM payments WHERE reference='OLDREF9'") == "too_old"


async def test_failed_attempts_then_manual_review(h):
    h.add_payout()
    h.verify.responder = FakeVerify.with_result(verified=False)
    ad_id = await _order(h)
    await _pay(h, reference="WRONG001")
    assert "2 attempt" in h.last(ADV).text or "2 attempt" in h.texts(ADV)[-2]
    await _pay(h, reference="WRONG002")
    await _pay(h, reference="WRONG003")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "needs_review"
    assert "failed payment attempts" in h.last(ADMIN_GROUP).text


async def test_cbe_requires_suffix_and_sends_it(h):
    h.add_payout(bank="cbe", account="1000303997441", holder="Kuru Labs")
    await _order(h)
    await h.press(ADV, "I have paid")
    await h.press(ADV, "CBE")
    await h.say(ADV, "FT1234567890")
    assert "last 8 digits" in h.last(ADV).text
    await h.say(ADV, "1234")
    assert "last 8 digits" in h.last(ADV).text  # rejected, asks again
    await h.say(ADV, "1234 5678")
    assert h.verify.requests[-1]["body"] == {"bank": "cbe", "referenceNumber": "FT1234567890", "accountSuffix": "12345678",
                                             "settlementAccount": "1000303997441"}


async def test_cbe_receipt_id_does_not_prompt_for_suffix(h):
    h.add_payout(bank="cbe", account="1000303997441", holder="Kuru Labs")
    await _order(h)
    await h.press(ADV, "I have paid")
    await h.press(ADV, "CBE")
    await h.say(ADV, "CBE26RCPT99")
    assert h.verify.requests[-1]["body"] == {"bank": "cbe", "referenceNumber": "CBE26RCPT99",
                                             "settlementAccount": "1000303997441"}


async def test_reference_extracted_from_pasted_sms(h):
    h.add_payout()
    await _order(h)
    await h.press(ADV, "I have paid")
    await h.press(ADV, "Telebirr")
    await h.say(ADV, "Dear customer you have transferred ETB 800.00 to Kuru Labs on 03/10/2026. Transaction number: CDJ7HK9L2M. Thank you.")
    assert h.verify.requests[-1]["body"]["transactionNumber"] == "CDJ7HK9L2M"


async def test_queued_then_webhook_completes(h):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    assert "being checked" in h.last(ADV).text
    assert db.scalar("SELECT status FROM payments") == "queued"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "verifying"
    # webhook (no settlementAccountMatch in webhook payload → receiverName fallback)
    current_timestamp = datetime.fromtimestamp(db.now(), timezone.utc).isoformat()
    payload = {"event": "verification.completed", "requestId": "req-q", "timestamp": current_timestamp,
               "data": {"processingStatus": "completed", "status": "success", "verified": True, "bank": "telebirr",
                        "amount": "800", "currency": "ETB", "senderName": "Abebe", "receiverName": "KURU LABS",
                        "referenceNumber": "ABC123XYZ", "completedAt": current_timestamp}}
    await handle_verify_webhook(payload, h.app)
    assert db.scalar("SELECT status FROM payments") == "verified"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"
    assert "confirmed" in h.last(ADV).text.lower()
    # replay is ignored
    await handle_verify_webhook(payload, h.app)
    assert db.scalar("SELECT count(*) FROM payments") == 1


async def test_webhook_with_unknown_receiver_needs_review(h):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    payload = {"event": "verification.completed", "requestId": "req-q",
               "data": {"processingStatus": "completed", "verified": True, "amount": "800", "receiverName": "Someone Else"}}
    await handle_verify_webhook(payload, h.app)
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "needs_review"
    assert db.scalar("SELECT reason FROM payments") == "receiver_unconfirmed"


async def test_queued_then_poll_completes(h):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    h.verify.status_payload = {"success": True, "data": {"requestId": "req-q", "processingStatus": "completed", "verified": True,
                                                         "amount": 800, "receiverName": "Kuru Labs"}}
    pid = db.scalar("SELECT id FROM payments")
    await h.run_job(poll_job, {"payment_id": pid, "attempt": 0})
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"


@pytest.mark.parametrize("status_data", [
    {"status": "success", "verified": True},
    {"status": "success", "verified": True, "amount": 800, "settlementAccountMatch": {}},
    {"status": "success", "amount": 800},
])
async def test_sparse_completed_poll_waits_for_full_webhook(h, status_data):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    pid = db.scalar("SELECT id FROM payments")
    current_timestamp = datetime.fromtimestamp(db.now(), timezone.utc).isoformat()
    h.verify.status_payload = {"success": True, "data": {"requestId": "req-q", "processingStatus": "completed",
                                                         **status_data, "completedAt": current_timestamp}}
    await h.run_job(poll_job, {"payment_id": pid, "attempt": 0})
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "queued"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "verifying"

    payload = {"event": "verification.completed", "requestId": "req-q", "data": FakeVerify.result()}
    await handle_verify_webhook(payload, h.app)
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "verified"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"


async def test_webhook_can_complete_after_poll_timeout(h):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    pid = db.scalar("SELECT id FROM payments")
    current_timestamp = datetime.fromtimestamp(db.now(), timezone.utc).isoformat()
    h.verify.status_payload = {"success": True, "data": {"requestId": "req-q", "processingStatus": "completed",
                                                         "status": "success", "verified": True,
                                                         "completedAt": current_timestamp}}
    await h.run_job(poll_job, {"payment_id": pid, "attempt": 5})
    assert db.scalar("SELECT reason FROM payments WHERE id=?", (pid,)) == "poll_timeout"
    payload = {"event": "verification.completed", "requestId": "req-q", "data": FakeVerify.result()}
    await handle_verify_webhook(payload, h.app)
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "verified"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"


async def test_poll_timeout_does_not_overwrite_concurrent_webhook(h, monkeypatch):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    pid = db.scalar("SELECT id FROM payments")
    payload = {"event": "verification.completed", "requestId": "req-q", "data": FakeVerify.result()}

    class RacingClient:
        async def status(self, request_id):
            await handle_verify_webhook(payload, h.app)
            return VerifyOutcome("queued", request_id=request_id)

    monkeypatch.setattr("payments._client", lambda context: RacingClient())
    await h.run_job(poll_job, {"payment_id": pid, "attempt": 5})
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "verified"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"


async def test_payment_and_audit_roll_back_together(h, monkeypatch):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    pid = db.scalar("SELECT id FROM payments")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(db, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        apply_result(pid, FakeVerify.result(), "result")
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "queued"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "verifying"


async def test_payment_result_rechecks_state_inside_transaction(h, monkeypatch):
    h.add_payout()
    h.verify.responder = FakeVerify.queued
    ad_id = await _order(h)
    await _pay(h)
    pid = db.scalar("SELECT id FROM payments")
    original_evaluate = payments.evaluate
    interleaved = False

    def interleaving_evaluate(data, *args, **kwargs):
        nonlocal interleaved
        result = original_evaluate(data, *args, **kwargs)
        if not interleaved:
            interleaved = True
            payments.apply_result(pid, FakeVerify.result(matched=False), "interleaved")
        return result

    monkeypatch.setattr(payments, "evaluate", interleaving_evaluate)
    result = payments.apply_result(pid, FakeVerify.result(), "outer")
    assert result.reason == "already_applied"
    assert db.scalar("SELECT status FROM payments WHERE id=?", (pid,)) == "failed"
    assert db.scalar("SELECT reason FROM payments WHERE id=?", (pid,)) == "receiver_mismatch"
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "needs_review"


async def test_no_credits_falls_back_to_manual(h):
    h.add_payout()
    h.verify.responder = FakeVerify.no_credits
    ad_id = await _order(h)
    await _pay(h)
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "needs_review"
    assert "credits exhausted" in h.last(ADMIN_GROUP).text


async def test_invalid_payload_asks_again(h):
    h.add_payout()
    h.verify.responder = FakeVerify.invalid
    ad_id = await _order(h)
    await _pay(h)
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "awaiting_payment"
    assert any("valid reference" in t for t in h.texts(ADV))


async def test_rate_limited_schedules_retry(h):
    h.add_payout()
    h.verify.responder = FakeVerify.rate_limited
    ad_id = await _order(h)
    await _pay(h)
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "verifying"
    assert any(j.name and j.name.startswith("retry-") for j in h.app.job_queue.jobs())


async def test_cancel_order_and_expiry(h):
    h.add_payout()
    ad_id = await _order(h)
    await h.press(ADV, "Cancel order")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "cancelled"
    ad2 = await _order(h, uid=ADV2)
    db.execute("UPDATE ads SET created=created-1000000 WHERE id=?", (ad2,))
    expired = expire_orders(db.now())
    assert [e["id"] for e in expired] == [ad2]
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad2,)) == "cancelled"


async def test_user_cannot_pay_for_someone_elses_order(h):
    h.add_payout()
    ad_id = await _order(h)
    screen = h.last(ADV)
    from tests.fake_telegram import callback_update
    await h.app.process_update(callback_update(h.bot, ADV2, screen.callback("I have paid"), screen, chat_id=ADV2))
    assert h.tg.sent_to(ADV2) == []
    await h.app.process_update(callback_update(h.bot, ADV2, screen.callback("Cancel order"), screen, chat_id=ADV2))
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "awaiting_payment"


async def test_reject_paid_ad_marks_refund_due(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    ad_id = await _order(h)
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Reject")
    await h.press(MOD, "scam")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "refund_due"
    assert any("refund" in t for t in h.texts(ADV))
    await h.say(OWNER, "/refunds")
    assert f"#{ad_id}" in h.last(OWNER).text
    await h.say(OWNER, f"/refunds done {ad_id}")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "cancelled"


async def test_admin_group_can_review_ads_and_advertiser_sees_decline_reason(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    ad_id = await _order(h)
    await _pay(h)

    await h.say(MOD, "/ads", chat_id=-9090)
    assert h.tg.sent_to(-9090) == []
    await h.say(ADV, "/ads", chat_id=ADMIN_GROUP)
    assert h.tg.sent_to(ADMIN_GROUP) == []
    await h.say(MOD, "/ads", chat_id=ADMIN_GROUP)
    card = h.last(ADMIN_GROUP)
    assert card.button("Approve & schedule") and card.button("Reject")
    await h.press(ADV, "Reject", message=card, chat_id=ADMIN_GROUP)
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "paid"
    await h.press(MOD, "Reject", message=card, chat_id=ADMIN_GROUP)
    await h.press(MOD, "scam", message=card, chat_id=ADMIN_GROUP)
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "refund_due"
    assert db.scalar("SELECT reject_reason FROM ads WHERE id=?", (ad_id,)) == "scam"
    assert any("Scam or misleading" in text for text in h.texts(ADV))

    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    assert "Scam or misleading" in h.last(ADV).text


async def test_pinned_package_pins_and_unpins(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(amount=2000)
    ad_id = await _order(h, package="Pinned post", photo=True)
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Approve & schedule")
    h.set("post_interval_sec", 0)
    h.set("min_posts_between_ads", 0)
    await h.run_job(poster_job)
    photos = [c for c in h.tg.calls if c.chat_id == CHANNEL and c.method == "sendPhoto"]
    assert photos and "Sponsored" in photos[-1].text
    assert h.tg.pinned == [(CHANNEL, photos[-1].message_id)]
    assert db.scalar("SELECT pinned_until FROM ad_posts WHERE ad_id=?", (ad_id,)) > db.now()
    db.execute("UPDATE ad_posts SET pinned_until=1")
    await h.run_job(unpin_job)
    assert any(c.method == "unpinChatMessage" for c in h.tg.calls)


async def test_bundle_posts_are_spread_across_the_advertised_week(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(amount=3000)
    ad_id = await _order(h, package="Bundle ×3")
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Approve & schedule")

    rows = db.q("SELECT not_before FROM outbox WHERE kind='ad' AND ref_id=? ORDER BY not_before", (ad_id,))
    assert len(rows) == 3
    assert [rows[i + 1]["not_before"] - rows[i]["not_before"] for i in range(2)] == [302400, 302400]


async def test_bot_banner_package_adds_clickable_receipt_banner(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(amount=1500)
    ad_id = await _order(h, package="Bot banner")
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Approve & schedule")
    assert db.scalar("SELECT status FROM ads WHERE id=?", (ad_id,)) == "done"

    await h.submit_confession(901, "A confession long enough to check the banner placement after submission.")
    receipt = h.last(901)
    assert "Fresh coffee" in receipt.text
    entities = receipt.params.get("entities") or []
    if isinstance(entities, str):
        entities = json.loads(entities)
    assert any(entity.get("type") == "text_link" and entity.get("url") == "https://campuscafe.example.com/deal"
               for entity in entities)
    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    assert "Status: Live" in h.last(ADV).text


async def test_advertiser_myads_shows_published_link_and_pin_expiry(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(amount=2000)
    ad_id = await _order(h, package="Pinned post", photo=True)
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Approve & schedule")
    h.set("post_interval_sec", 0)
    h.set("min_posts_between_ads", 0)
    await h.run_job(poster_job)
    post = db.one("SELECT p.channel_msg_id FROM ad_posts ap JOIN posts p ON p.number=ap.post_number WHERE ap.ad_id=?", (ad_id,))

    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    detail = h.last(ADV)
    assert "Published" in detail.text and "1/1" in detail.text and "pinned until" in detail.text.lower()
    assert detail.button("Open post") and detail.button("Open post")["url"].endswith(f"/{post['channel_msg_id']}")


async def test_myads_details_are_private_even_for_copied_callbacks(h):
    ad_id = await _order(h)
    await h.say(ADV, "/myads")
    card = h.last(ADV)
    group_messages = len(h.tg.sent_to(ADMIN_GROUP))
    await h.press(ADV, f"#{ad_id}", message=card, chat_id=ADMIN_GROUP)
    assert len(h.tg.sent_to(ADMIN_GROUP)) == group_messages


async def test_future_banner_is_not_reported_as_active(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    h.verify.responder = FakeVerify.with_result(amount=1500)
    ad_id = await _order(h, package="Bot banner")
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Schedule +24h")

    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    assert "banner scheduled" in h.last(ADV).text.lower()
    assert "Status: Scheduled" in h.last(ADV).text
    assert "Banner active" not in h.last(ADV).text


async def test_ad_spacing_rules(h):
    h.add_payout()
    h.add_staff(MOD, "admin")
    await _order(h)
    await _pay(h)
    await h.say(MOD, "/ads")
    await h.press(MOD, "Approve & schedule")
    # 2 confessions in outbox, ad needs 1 confession before it
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    h.set("min_posts_between_ads", 1)
    for i, uid in enumerate((901, 902)):
        await h.submit_confession(uid, f"Confession number {i} with enough length to pass the minimum check.")
        await h.say(MOD, "/next")
        await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    await h.run_job(poster_job)
    await h.run_job(poster_job)
    kinds = [r["kind"] for r in db.q("SELECT kind FROM posts ORDER BY number")]
    assert kinds == ["confession", "ad", "confession"]
    # daily cap
    h.set("max_ads_per_day", 1)
    await _order(h, uid=ADV2)
    await _pay(h, uid=ADV2, reference="SECOND99")
    await h.say(MOD, "/ads")
    await h.press(MOD, "Approve & schedule")
    await h.run_job(poster_job)
    assert db.scalar("SELECT count(*) FROM outbox WHERE kind='ad'") == 1  # held back


async def test_package_edit_does_not_change_paid_order(h):
    h.add_payout()
    ad_id = await _order(h)
    await h.say(OWNER, "/packages set 1 price_etb 999")
    assert "Updated" in h.last(OWNER).text
    assert db.scalar("SELECT price_etb FROM ads WHERE id=?", (ad_id,)) == 800
    await h.say(ADV2, "/advertise")
    assert "999 ETB" in h.last(ADV2).button("Basic")["text"]


async def test_ads_disabled_flag(h):
    h.set("ads_enabled", 0)
    await h.say(ADV, "/advertise")
    assert "not available" in h.last(ADV).text
    await h.say(ADV, "/start")
    assert h.last(ADV).button("Advertise") is None
