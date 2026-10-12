"""E2E: My confessions, OP comment digests, /myads + Contact admin + admin reply, hub ad cards, Stats = Health."""
from __future__ import annotations

import pytest

import db
from jobs.housekeeping import comment_digest_job
from jobs.poster import poster_job
from security import author_hash, sign_cb
from tests.e2e_harness import ADMIN_GROUP
from tests.fake_telegram import callback_update

pytestmark = pytest.mark.asyncio

S, S2, S3, MOD, ADMIN, ADV = 501, 502, 503, 701, 702, 801
TEXT = "A confession long enough to be reviewed and posted for the My confessions tests."


async def _setup(h):
    h.add_staff(MOD)
    h.add_staff(ADMIN, "admin")
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)


async def _post(h, uid=S, text=TEXT):
    await h.submit_confession(uid, text)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    return db.scalar("SELECT max(number) FROM posts")


# ---------------------------------------------------------------------------
# 📝 My confessions
# ---------------------------------------------------------------------------

async def test_my_confessions_shows_every_status(h):
    await _setup(h)
    await h.say(S, "📝 My confessions")
    assert "haven't sent" in h.last(S).text and h.last(S).button("Confess")
    n = await _post(h)
    await h.submit_confession(S, "Second confession still waiting in the moderation queue right now.")
    await h.submit_confession(S, "Third confession that the moderator is going to reject for spam.")
    await h.say(MOD, "/next")
    await h.press(MOD, "Reject")
    await h.press(MOD, "spam")
    await h.say(S, "📝 My confessions")
    mine = h.last(S)
    assert f"Live as #{n}" in mine.text and "Waiting for review" in mine.text and "Not approved — spam" in mine.text
    assert mine.button(f"Comments on #{n}") and mine.button(f"Delete #{n}")
    await h.press(S, f"Delete #{n}")
    assert "Delete #" in h.last(S).text and h.last(S).button("Yes, delete")
    await h.press(S, "Yes, delete")
    assert db.scalar("SELECT deleted FROM posts WHERE number=?", (n,)) == 1
    await h.say(S, "📝 My confessions")
    assert "Deleted" in h.last(S).text


async def test_my_confessions_delete_is_author_bound(h):
    await _setup(h)
    n = await _post(h)
    # S2 forges S's delete callbacks (signature is valid: same bot secret) — must be refused
    await h.say(S2, "/start")
    for data in (sign_cb("mine", f"del:{n}"), sign_cb("mine", f"delok:{n}")):
        await h.press_raw(S2, data)
    assert db.scalar("SELECT deleted FROM posts WHERE number=?", (n,)) == 0
    assert "not yours" in h.last(S2).text
    # S2's own list is empty; no leak of S's items
    await h.say(S2, "📝 My confessions")
    assert "haven't sent" in h.last(S2).text


async def test_my_confessions_in_amharic(h):
    await _setup(h)
    await h.say(S, "/lang")
    await h.press(S, "አማርኛ")
    await h.say(S, "📝 የእኔ ኑዛዜዎች")
    assert "ኑዛዜ" in h.last(S).text


# ---------------------------------------------------------------------------
# OP comment digests
# ---------------------------------------------------------------------------

async def _comment(h, uid, n, text):
    await h.say(uid, f"/start c_{n}")
    await h.press(uid, "Write a comment")
    await h.say(uid, text)


async def test_op_gets_grouped_digest_not_per_comment(h):
    await _setup(h)
    h.set("comment_cooldown_sec", 0)
    n = await _post(h)
    before = len(h.texts(S))
    await _comment(h, S2, n, "first")
    await _comment(h, S3, n, "second")
    await _comment(h, S, n, "op replying")  # OP's own comment never counts
    assert len(h.texts(S)) - before == 0 or all("new comment" not in t for t in h.texts(S)[before:])  # nothing yet
    await h.run_job(comment_digest_job)
    digest = [c for c in h.tg.sent_to(S) if "new comment" in c.text]
    assert len(digest) == 1 and "2 new comment" in digest[0].text and digest[0].button("Open thread")
    assert db.scalar("SELECT unnotified_comments FROM posts WHERE number=?", (n,)) == 0
    # rate limit: a new comment right away does not produce a second DM
    await _comment(h, S2, n, "third")
    await h.run_job(comment_digest_job)
    assert len([c for c in h.tg.sent_to(S) if "new comment" in c.text]) == 1
    db.execute("UPDATE users SET last_comment_notify=0 WHERE author=?", (author_hash(S),))
    await h.run_job(comment_digest_job)
    assert len([c for c in h.tg.sent_to(S) if "new comment" in c.text]) == 2


async def test_digest_across_multiple_posts_and_disabled(h):
    await _setup(h)
    h.set("comment_cooldown_sec", 0)
    n1 = await _post(h)
    n2 = await _post(h, text="Another confession by the same author so the digest spans two posts.")
    await _comment(h, S2, n1, "a")
    await _comment(h, S2, n2, "b")
    await h.run_job(comment_digest_job)
    digest = [c for c in h.tg.sent_to(S) if "new comment" in c.text][-1]
    assert "across 2" in digest.text and digest.button("My confessions")
    h.set("comment_notify_interval_sec", 0)
    await _comment(h, S3, n1, "c")
    db.execute("UPDATE users SET last_comment_notify=0")
    await h.run_job(comment_digest_job)
    assert len([c for c in h.tg.sent_to(S) if "new comment" in c.text]) == 1


async def test_hidden_or_deleted_posts_do_not_notify(h):
    await _setup(h)
    n = await _post(h)
    await _comment(h, S2, n, "hello")
    await h.say(S, f"/delete {n}")
    await h.run_job(comment_digest_job)
    assert not any("new comment" in t for t in h.texts(S))


# ---------------------------------------------------------------------------
# /myads, Contact admin, admin reply
# ---------------------------------------------------------------------------

async def _order(h):
    h.add_payout()
    await h.say(ADV, "/advertise")
    await h.press(ADV, "Basic post")
    await h.say(ADV, "Coffee deal for students at the campus café this week only.")
    await h.press(ADV, "Skip")
    await h.press(ADV, "Skip")
    await h.press(ADV, "Place order")
    return db.scalar("SELECT max(id) FROM ads")


async def test_myads_reopens_pay_screen(h):
    await _setup(h)
    ad_id = await _order(h)
    await h.say(ADV, "/myads")
    assert h.last(ADV).button(f"#{ad_id}")
    await h.press(ADV, f"#{ad_id}")
    status = h.last(ADV)
    assert "Awaiting payment" in status.text and "0/1 published" in status.text
    assert status.button("Pay / enter reference") and status.button("Contact admin")
    await h.press(ADV, "Pay / enter reference")
    assert h.last(ADV).button("Telebirr")  # bank picker = payment flow re-entered
    await h.press(ADV, "Back")
    assert h.last(ADV).button("I have paid")  # pay screen re-shown


async def test_contact_admin_reaches_ads_topic_and_admin_can_reply(h):
    await _setup(h)
    h.set("topic_ads", 42)
    ad_id = await _order(h)
    db.execute("UPDATE advertisers SET name='Campus Café' WHERE user_id=?", (ADV,))
    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    await h.press(ADV, "Contact admin")
    assert f"order #{ad_id}" in h.last(ADV).text and h.last(ADV).button("Cancel")
    await h.say(ADV, "I paid but typed the reference wrong, please help")
    assert "Sent" in h.last(ADV).text and h.last(ADV).reply_labels()
    card = [c for c in h.tg.calls if c.chat_id == ADMIN_GROUP and c.method == "sendMessage"][-1]
    assert f"Order #{ad_id}" in card.text and "typed the reference wrong" in card.text and card.params.get("message_thread_id") == 42
    assert str(ADV) not in card.text and "Campus Café" in card.text  # name, never the raw id
    # admin replies from the topic
    await h.press(ADMIN, "Reply", message=card, chat_id=ADMIN_GROUP)
    await h.say(ADMIN, "No problem, send the correct reference via My ads.", chat_id=ADMIN_GROUP)
    assert any("Admin reply about your ad" in t and "correct reference" in t for t in h.texts(ADV))
    # moderator cannot use Reply
    await h.app.process_update(callback_update(h.bot, MOD, card.callback("Reply"), card, chat_id=ADMIN_GROUP))
    await h.say(MOD, "leaked?", chat_id=ADMIN_GROUP)
    assert not any("leaked?" in t for t in h.texts(ADV))


async def test_contact_admin_rate_limited_and_owner_bound(h):
    await _setup(h)
    ad_id = await _order(h)
    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    await h.press(ADV, "Contact admin")
    await h.say(ADV, "first message")
    await h.say(ADV, "/myads")
    await h.press(ADV, f"#{ad_id}")
    await h.press(ADV, "Contact admin")
    assert "wait" in h.last(ADV).text.lower()
    assert len([c for c in h.tg.calls if c.chat_id == ADMIN_GROUP and "Advertiser message" in c.text]) == 1
    # another user cannot contact about this ad
    await h.say(S2, "/start")
    await h.press_raw(S2, sign_cb("adc", str(ad_id)))
    await h.say(S2, "spoofed")
    assert not any("spoofed" in c.text for c in h.tg.calls if c.chat_id == ADMIN_GROUP)


async def test_contact_button_after_failed_payment(h):
    await _setup(h)
    from tests.e2e_harness import FakeVerify
    h.verify.responder = FakeVerify.with_result(matched=False)
    await _order(h)
    await h.press(ADV, "I have paid")
    await h.press(ADV, "Telebirr")
    await h.say(ADV, "ABC123XYZ")
    assert "admin will check" in h.last(ADV).text.lower() and h.last(ADV).button("Contact admin")


# ---------------------------------------------------------------------------
# Hub shows ad cards; Stats == Health
# ---------------------------------------------------------------------------

async def test_hub_ads_screen_shows_actionable_cards(h):
    await _setup(h)
    await _order(h)
    await h.press(ADV, "I have paid")
    await h.press(ADV, "Telebirr")
    await h.say(ADV, "ABC123XYZ")
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Ads")
    card = h.last(ADMIN)
    assert "Order #1" in card.text and card.button("Approve & schedule")
    await h.press(ADMIN, "Approve & schedule")
    assert db.scalar("SELECT status FROM ads") == "scheduled"
    await h.say(ADMIN, "/ads")
    assert "No ads waiting" in h.last(ADMIN).text and h.last(ADMIN).button("Admin")


async def test_stats_and_health_share_renderer(h):
    await _setup(h)
    await h.say(MOD, "📊 Stats")
    stats = h.last(MOD).text
    assert "Status" in stats and h.last(MOD).button("Queue")
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Health")
    health = [c for c in h.tg.calls if c.method == "editMessageText" and c.chat_id == ADMIN][-1].text
    assert health.split("\n")[1] == stats.split("\n")[1]
