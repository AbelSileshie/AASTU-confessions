"""E2E: comment threads, aliases, reports, counter refresh, Connect (consent)."""
from __future__ import annotations

import pytest

import db
from jobs.housekeeping import counter_refresh_job, expire_connects
from jobs.poster import poster_job
from security import author_hash
from tests.e2e_harness import ADMIN_GROUP, CHANNEL, h  # noqa: F401

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
    assert "Posted as Anon 1" in h.texts(A)[-2]
    h.set("comment_cooldown_sec", 0)
    await _comment(h, B, n, "Second here")
    assert "Posted as Anon 2" in h.texts(B)[-2]
    await _comment(h, OP, n, "Thanks both")
    assert "Posted as OP" in h.texts(OP)[-2]
    await _comment(h, A, n, "again")  # same alias in same thread
    assert "Posted as Anon 1" in h.texts(A)[-2]
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
    assert "Posted as" in h.texts(A)[-2]
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
        await h.press(uid, f"🚩 {cid}")
    # same reporter twice does not count
    await h.say(B, f"/start c_{n}")
    await h.press(B, f"🚩 {cid}")
    assert db.scalar("SELECT hidden FROM comments WHERE id=?", (cid,)) == 0
    await h.say(OP, f"/start c_{n}")
    await h.press(OP, f"🚩 {cid}")
    assert db.scalar("SELECT hidden FROM comments WHERE id=?", (cid,)) == 1
    assert db.scalar("SELECT comment_count FROM posts WHERE number=?", (n,)) == 0
    alert = h.last(ADMIN_GROUP)
    assert "auto-hidden" in alert.text
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


async def test_connect_requires_consent_and_exchanges_contacts(h):
    n = await _post_one(h)
    await _comment(h, A, n, "I know exactly what you mean")
    cid = db.scalar("SELECT id FROM comments")
    # non-OP sees no connect button
    await h.say(B, f"/start c_{n}")
    assert h.last(B).button("🤝") is None
    # OP sees it and asks
    await h.say(OP, f"/start c_{n}")
    assert h.last(OP).button(f"🤝 {cid}")
    await h.press(OP, f"🤝 {cid}")
    assert "Request sent" in h.last(OP).text
    ask = h.last(A)
    assert "would like to connect" in ask.text and "I know exactly" in ask.text
    assert str(OP) not in ask.text  # nothing revealed yet
    # second request blocked while pending
    await h.say(OP, f"/start c_{n}")
    h.set("connect_cooldown_sec", 0)
    await h.press(OP, f"🤝 {cid}")
    assert db.scalar("SELECT count(*) FROM connect_requests") == 1
    # A accepts → both receive text_mention entities pointing at each other
    await h.press(A, "Share my contact", message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "accepted"
    to_a = h.tg.last(A)
    to_op = h.tg.last(OP)
    assert "accepted" in to_a.text.lower() or "Here is" in to_a.text
    ent_a = to_a.params["entities"]
    ent_op = to_op.params["entities"]
    import json
    ea = json.loads(ent_a) if isinstance(ent_a, str) else ent_a
    eo = json.loads(ent_op) if isinstance(ent_op, str) else ent_op
    assert ea[0]["type"] == "text_mention" and ea[0]["user"]["id"] == OP
    assert eo[0]["type"] == "text_mention" and eo[0]["user"]["id"] == A
    # nothing persisted about raw ids
    assert str(OP) not in str([dict(r) for r in db.q("SELECT * FROM connect_requests")])


async def test_connect_decline_reveals_nothing(h):
    n = await _post_one(h)
    await _comment(h, A, n, "a comment")
    cid = db.scalar("SELECT id FROM comments")
    await h.say(OP, f"/start c_{n}")
    await h.press(OP, f"🤝 {cid}")
    ask = h.last(A)
    accept_data = ask.callback("Share my contact")
    await h.press(A, "Decline", message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "declined"
    assert "declined" in h.last(OP).text.lower()
    assert not any("entities" in c.params and c.params["entities"] for c in h.tg.sent_to(OP))
    # replaying the accept button after decline is rejected
    await h.press_raw(A, accept_data, message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "declined"


async def test_connect_cannot_be_initiated_by_commenter_or_forged(h):
    n = await _post_one(h)
    await _comment(h, A, n, "a comment")
    cid = db.scalar("SELECT id FROM comments")
    from security import sign_cb
    await h.say(B, f"/start c_{n}")
    # B forges the OP's connect button
    await h.press_raw(B, sign_cb("cn", f"ask:{cid}"))
    assert db.scalar("SELECT count(*) FROM connect_requests") == 0
    # unsigned garbage
    await h.press_raw(B, f"cn:ask:{cid}:0000000000")
    assert db.scalar("SELECT count(*) FROM connect_requests") == 0
    # disabled setting
    h.set("connect_enabled", 0)
    await h.say(OP, f"/start c_{n}")
    assert h.last(OP).button("🤝") is None


async def test_connect_expiry(h):
    n = await _post_one(h)
    await _comment(h, A, n, "a comment")
    cid = db.scalar("SELECT id FROM comments")
    await h.say(OP, f"/start c_{n}")
    await h.press(OP, f"🤝 {cid}")
    db.execute("UPDATE connect_requests SET created=created-10000000")
    expire_connects(db.now())
    assert db.scalar("SELECT status FROM connect_requests") == "expired"
    ask = [c for c in h.tg.sent_to(A) if "would like to connect" in c.text][-1]
    await h.press(A, "Share my contact", message=ask)
    assert db.scalar("SELECT status FROM connect_requests") == "expired"
