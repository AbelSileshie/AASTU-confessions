"""E2E: comment threads, aliases, reports, counter refresh, Connect (consent)."""
from __future__ import annotations

import pytest

import db
from jobs.housekeeping import counter_refresh_job, expire_connects
from jobs.poster import poster_job
from tests.e2e_harness import ADMIN_GROUP, CHANNEL

pytestmark = pytest.mark.asyncio

OP, A, B, C, MOD = 601, 602, 603, 604, 701


async def _post_one(h, uid=OP, text="A long enough confession to be posted into the channel for the comment tests."):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.submit_confession(uid, text)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    return db.scalar("SELECT max(number) FROM posts")


async def _comment(h, uid, number, text):
    await h.say(uid, f"/start c_{number}")
    await h.press(uid, "Write a comment")
    await h.say(uid, text)


async def test_thread_aliases_and_replies(h):
    n = await _post_one(h)
    await h.say(A, f"/start c_{n}")
    assert "No comments yet" in h.last(A).text
    await _comment(h, A, n, "First!")
    assert "Posted as Anon 1" in h.last(A).text
    h.set("comment_cooldown_sec", 0)
    await _comment(h, B, n, "Second here")
    assert "Posted as Anon 2" in h.last(B).text
    await _comment(h, OP, n, "Thanks both")
    assert "Posted as OP" in h.last(OP).text
    await _comment(h, A, n, "again")  # same alias in same thread
    assert "Posted as Anon 1" in h.last(A).text
    thread = h.last(A).text
    assert "Anon 1" in thread and "Anon 2" in thread and "OP" in thread and "(4)" in thread
    assert db.scalar("SELECT comment_count FROM posts WHERE number=?", (n,)) == 4


async def test_comment_filters_and_cooldown(h):
    n = await _post_one(h)
    await h.say(A, f"/start c_{n}")
    await h.press(A, "Write a comment")
    await h.say(A, "visit https://spam.example now")
    assert "Links" in h.last(A).text
    await h.say(A, "fine comment")
    assert "Posted as" in h.last(A).text
    await h.press(A, "Write a comment")
    await h.say(A, "too fast")
    assert "wait" in h.last(A).text.lower()


async def test_reports_hide_comment_and_alert_admins(h):
    n = await _post_one(h)
    h.set("comment_cooldown_sec", 0)
    await _comment(h, A, n, "something nasty")
    cid = db.scalar("SELECT id FROM comments")
    for uid in (B, C):
        await h.say(uid, f"/start c_{n}")
        await h.press(uid, "Report Anon 1")
    # same reporter twice does not count
    await h.say(B, f"/start c_{n}")
    await h.press(B, "Report Anon 1")
    assert db.scalar("SELECT hidden FROM comments WHERE id=?", (cid,)) == 0
    await h.say(OP, f"/start c_{n}")
    await h.press(OP, "Report Anon 1")
    assert db.scalar("SELECT hidden FROM comments WHERE id=?", (cid,)) == 1
    assert db.scalar("SELECT comment_count FROM posts WHERE number=?", (n,)) == 0
    alert = h.last(ADMIN_GROUP)
    assert "Comment hidden" in alert.text
    await h.press(MOD, "Unhide", message=alert, chat_id=ADMIN_GROUP)
    assert db.scalar("SELECT hidden FROM comments WHERE id=?", (cid,)) == 0


async def test_counter_refresh_edits_channel_button(h):
    n = await _post_one(h)
    await _comment(h, A, n, "hello there")
    h.set("counter_refresh_sec", 30)
    db.execute("UPDATE posts SET counter_updated=0")
    await h.run_job(counter_refresh_job)
    edits = [c for c in h.tg.calls if c.method == "editMessageReplyMarkup" and c.chat_id == CHANNEL]
    assert edits and edits[-1].button("Comments (1)")


def _mentions(h, chat_id):
    return [c for c in h.tg.sent_to(chat_id) if "TEXT_MENTION" in str(c.params.get("entities", "")).upper()]


async def _setup_request(h):
    n = await _post_one(h)
    await _comment(h, A, n, "I know exactly what you mean")
    cid = db.scalar("SELECT id FROM comments")
    await h.say(OP, f"/start c_{n}")
    assert h.last(OP).button("Ask Anon 1 for contact")
    await h.say(B, f"/start c_{n}")
    assert h.last(B).button("for contact") is None  # non-OP never sees Connect
    await h.press(OP, "Ask Anon 1 for contact")
    return n, cid


async def test_connect_is_one_way_and_two_step(h):
    await _setup_request(h)
    op_sent = h.last(OP)
    assert "Request sent" in op_sent.text and op_sent.button("Withdraw")
    ask = h.last(A)
    assert "Contact request" in ask.text and "Only the author receives it" in ask.text and str(OP) not in ask.text
    # commenter accepts → nothing revealed yet; OP must take the second step
    await h.press(A, "Share my contact", message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "accepted"
    assert _mentions(h, OP) == [] and _mentions(h, A) == []
    accepted = h.last(A)
    assert accepted.text.startswith("✅ You agreed to share") and accepted.button("Revoke")  # NEW message carries the action
    assert ask.buttons() == []  # old consent card lost its buttons
    offer = h.last(OP)
    assert "agreed" in offer.text and offer.button("Show contact") and offer.button("Not interested")
    assert sum("agreed" in c.text for c in h.tg.sent_to(OP)) == 1  # one message, not three
    await h.press(OP, "Show contact", message=offer)
    assert db.scalar("SELECT status FROM connect_requests") == "revealed"
    # the OP sees real contact details: name + @username (fake users have no username → name + profile button)
    contact = h.last(OP)
    assert "Name: U602" in contact.text and contact.button("Open profile")["url"] == f"tg://user?id={A}" and contact.button("Back to the thread")
    assert len(_mentions(h, OP)) == 1 and _mentions(h, A) == []
    # the commenter is told, with a visible Revoke
    told = h.last(A)
    assert "Your contact was opened" in told.text and told.button("Revoke")


async def test_contact_card_with_username(h):
    h.tg.user_usernames[A] = "anon_person"
    await _setup_request(h)
    await h.press(A, "Share my contact", message=h.last(A))
    await h.press(OP, "Show contact", message=h.last(OP))
    contact = h.last(OP)
    assert "Telegram: @anon_person" in contact.text and contact.button("Open @anon_person")["url"] == "https://t.me/anon_person"
    import json
    ents = contact.params["entities"]
    ents = json.loads(ents) if isinstance(ents, str) else ents
    assert any(str(e["type"]).upper().endswith("MENTION") and not str(e["type"]).upper().endswith("TEXT_MENTION") for e in ents)
    assert str(OP) not in str([dict(r) for r in db.q("SELECT * FROM connect_requests")])


async def test_commenter_declines(h):
    await _setup_request(h)
    await h.press(A, "Decline", message=h.last(A))
    assert db.scalar("SELECT status FROM connect_requests") == "declined"
    assert any("declined" in t and "share their contact" in t for t in h.texts(OP))
    assert _mentions(h, OP) == []


async def test_op_withdraws_before_answer(h):
    await _setup_request(h)
    ask = h.last(A)
    accept = ask.callback("Share my contact")
    await h.press(OP, "Withdraw", message=h.last(OP))
    assert db.scalar("SELECT status FROM connect_requests") == "withdrawn"
    assert any("Request withdrawn" in t for t in h.texts(A))
    # commenter's stale Accept is refused
    await h.press_raw(A, accept, message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "withdrawn" and _mentions(h, OP) == []


async def test_commenter_revokes_before_reveal_prevents_it(h):
    await _setup_request(h)
    ask = h.last(A)
    await h.press(A, "Share my contact", message=ask)
    offer = h.last(OP)
    show = offer.callback("Show contact")
    await h.press(A, "Revoke", message=h.last(A))
    assert db.scalar("SELECT status FROM connect_requests") == "revoked"
    assert any("changed their mind" in t for t in h.texts(OP))
    assert offer.buttons() == []  # OP's offer card was closed
    await h.press_raw(OP, show, message=offer)  # too late
    assert _mentions(h, OP) == []


async def test_op_not_interested_after_accept(h):
    await _setup_request(h)
    await h.press(A, "Share my contact", message=h.last(A))
    await h.press(OP, "Not interested", message=h.last(OP))
    assert db.scalar("SELECT status FROM connect_requests") == "refused"
    assert any("decided not to connect" in t for t in h.texts(A))
    assert _mentions(h, OP) == []


async def test_commenter_revokes_after_reveal_within_window_only(h):
    await _setup_request(h)
    ask = h.last(A)
    await h.press(A, "Share my contact", message=ask)
    await h.press(OP, "Show contact", message=h.last(OP))
    ask = h.last(A)  # the reveal notice carries Revoke
    revoke = ask.callback("Revoke")
    db.execute("UPDATE connect_requests SET revealed_at=revealed_at-1000000")
    await h.press_raw(A, revoke, message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "revealed"  # window passed
    db.execute("UPDATE connect_requests SET revealed_at=?", (db.now(),))
    await h.press_raw(A, revoke, message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "revoked"
    assert any("Please do not contact them" in t for t in h.texts(OP))


async def test_connect_forgery_and_wrong_actor(h):
    n, _ = await _setup_request(h)
    rid = db.scalar("SELECT id FROM connect_requests")
    from security import sign_cb
    # third party / wrong role presses every button
    for who, action in ((B, "yes"), (B, "no"), (B, "revoke"), (B, "withdraw"), (B, "show"), (B, "refuse"),
                        (OP, "yes"), (OP, "revoke"), (A, "withdraw"), (A, "show"), (A, "refuse")):
        await h.say(who, "/start")
        await h.press_raw(who, sign_cb("cn", f"{action}:{rid}"))
    assert db.scalar("SELECT status FROM connect_requests") == "pending"
    assert _mentions(h, OP) == [] and _mentions(h, A) == [] and _mentions(h, B) == []
    # unsigned
    await h.press_raw(A, f"cn:yes:{rid}:000000000000")
    assert db.scalar("SELECT status FROM connect_requests") == "pending"
    h.set("connect_enabled", 0)
    await h.say(OP, f"/start c_{n}")
    assert h.last(OP).button("for contact") is None


async def test_requests_list_for_both_sides(h):
    n, _ = await _setup_request(h)
    await h.say(OP, "📝 My confessions")
    assert h.last(OP).button("Contact requests")
    await h.press(OP, "Contact requests")
    lst = h.last(OP)
    assert f"#{n}" in lst.text and "waiting" in lst.text and lst.button("Withdraw")
    await h.say(A, f"/start c_{n}")
    assert h.last(A).button("Contact requests")
    await h.press(A, "Contact requests")
    assert h.last(A).button("Share my contact") and h.last(A).button("Decline")


async def test_connect_expiry(h):
    await _setup_request(h)
    ask = h.last(A)
    db.execute("UPDATE connect_requests SET created=created-10000000")
    expire_connects(db.now())
    assert db.scalar("SELECT status FROM connect_requests") == "expired"
    await h.press(A, "Share my contact", message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "expired"
