"""Display contract: every state-changing action produces a visible message for every affected party,
with the right buttons; nothing lives only in the database."""
from __future__ import annotations

import pytest

import db
from jobs.poster import poster_job

pytestmark = pytest.mark.asyncio

OP, A, MOD = 601, 602, 701
TEXT = "A confession long enough to be posted into the channel for the display contract tests."


async def _post(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    h.set("comment_cooldown_sec", 0)
    await h.submit_confession(OP, TEXT)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)


async def _comment(h, uid, text):
    await h.say(uid, "/start c_1")
    await h.press(uid, "Write a comment")
    await h.say(uid, text)


def _new_since(h, chat, mark):
    return h.tg.sent_to(chat)[mark:]


def _has_action(msgs, label):
    return any(m.button(label) for m in msgs)


async def test_every_connect_transition_is_visible_to_both_sides(h):
    await _post(h)
    await _comment(h, A, "a comment")
    await h.say(OP, "/start c_1")
    m_op, m_a = len(h.tg.sent_to(OP)), len(h.tg.sent_to(A))

    await h.press(OP, "Ask Anon 1 for contact")
    op_new, a_new = _new_since(h, OP, m_op), _new_since(h, A, m_a)
    assert op_new and _has_action(op_new, "Withdraw request"), "OP must see the request with Withdraw"
    assert a_new and _has_action(a_new, "Share my contact") and _has_action(a_new, "Decline"), "commenter must see the ask"
    m_op, m_a = len(h.tg.sent_to(OP)), len(h.tg.sent_to(A))

    await h.press(A, "Share my contact", message=h.last(A))
    op_new, a_new = _new_since(h, OP, m_op), _new_since(h, A, m_a)
    assert len(op_new) == 1 and op_new[0].button("Show contact") and op_new[0].button("Not interested"), "OP gets ONE offer message"
    assert len(a_new) == 1 and a_new[0].button("Revoke"), "commenter gets a NEW message with Revoke"
    m_op, m_a = len(h.tg.sent_to(OP)), len(h.tg.sent_to(A))

    await h.press(OP, "Show contact", message=h.last(OP))
    op_new, a_new = _new_since(h, OP, m_op), _new_since(h, A, m_a)
    assert len(op_new) == 1 and "Name:" in op_new[0].text and (op_new[0].button("Open profile") or op_new[0].button("Open @")), "contact card actually shows details"
    assert op_new[0].button("Back to the thread")
    assert len(a_new) == 1 and a_new[0].button("Revoke") and "opened" in a_new[0].text
    m_op, m_a = len(h.tg.sent_to(OP)), len(h.tg.sent_to(A))

    await h.press(A, "Revoke", message=h.last(A))
    op_new, a_new = _new_since(h, OP, m_op), _new_since(h, A, m_a)
    assert op_new and "do not contact" in op_new[0].text
    assert a_new and "Revoked" in a_new[0].text
    # every earlier card lost its buttons: no stale actions anywhere
    stale = [m for m in h.tg.sent_to(OP) + h.tg.sent_to(A) if m.buttons() and any(
        b["text"] in ("✖️ Withdraw request", "👤 Show contact", "🙈 Not interested", "🚫 Revoke sharing", "✅ Share my contact") for b in m.buttons())]
    assert stale == [], [m.text[:40] for m in stale]


async def test_requests_list_offers_only_valid_actions(h):
    await _post(h)
    await _comment(h, A, "a comment")
    await h.say(OP, "/start c_1")
    await h.press(OP, "Ask Anon 1 for contact")
    await h.press(A, "Share my contact", message=h.last(A))
    await h.press(OP, "Show contact", message=h.last(OP))
    # commenter list inside the window: Revoke offered
    await h.say(A, "/start c_1")
    await h.press(A, "Contact requests")
    assert h.last(A).button("Revoke")
    # past the window: no Revoke offered
    db.execute("UPDATE connect_requests SET revealed_at=revealed_at-1000000")
    await h.say(A, "/start c_1")
    await h.press(A, "Contact requests")
    assert h.last(A).button("Revoke") is None and "contact opened" in h.last(A).text
    # OP list: opened → no actions, just status
    await h.say(OP, "📝 My confessions")
    await h.press(OP, "Contact requests")
    assert h.last(OP).button("Show contact") is None


async def test_multi_category_display_everywhere(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.say(OP, "✍️ Confess")
    picker = h.last(OP)
    assert "Categories (0/3)" in picker.text and picker.button("No category") and picker.button("Done")
    await h.press(OP, "Crush")
    picker = h.last(OP)
    assert "(1/3)" in picker.text and picker.button("✅ Crush")
    await h.press(OP, "Campus")
    await h.press(OP, "Funny")
    await h.press(OP, "Rant")  # 4th → refused with a toast
    assert "(3/3)" in h.last(OP).text and h.last(OP).button("✅ Rant") is None
    await h.press(OP, "✅ Crush")  # unselect
    assert "(2/3)" in h.last(OP).text
    await h.press(OP, "Done")
    await h.say(OP, TEXT)
    assert "#Campus #Funny" in h.last(OP).text and h.last(OP).button("Categories (2)")
    await h.press(OP, "Submit")
    await h.say(MOD, "/next")
    assert "Campus · Funny" in h.last(MOD).text  # review card
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    post = [c for c in h.tg.calls if c.chat_id == -1001 and c.method == "sendMessage"][-1]
    assert post.text.startswith("Confession #1\n") and post.text.endswith("\n\n#Campus #Funny")
    assert "😳" not in post.text and "🏫" not in post.text  # no category emoji anywhere


async def test_no_category_is_allowed_and_renders_clean(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.say(OP, "✍️ Confess")
    await h.press(OP, "No category")
    await h.say(OP, TEXT)
    assert "#" not in h.last(OP).text.replace("#1", "") or "Preview" in h.last(OP).text
    await h.press(OP, "Submit")
    await h.say(MOD, "/next")
    assert "No category" in h.last(MOD).text
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    post = [c for c in h.tg.calls if c.chat_id == -1001 and c.method == "sendMessage"][-1]
    assert post.text == f"Confession #1\n\n{TEXT}"
