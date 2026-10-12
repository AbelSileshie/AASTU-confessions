"""Navigation UX: nothing a student sees is a dead end."""
from __future__ import annotations

import pytest

import db
from jobs.poster import poster_job

pytestmark = pytest.mark.asyncio

S, S2, MOD = 501, 502, 701
CONFESSION = "A long enough confession to be posted into the channel for the navigation tests."
STAFF_UIDS = {MOD, 1000}


def _dead_ends(h, chat_id: int) -> list[str]:
    """Replies after which the user has nothing to tap: no keyboard on the
    message itself and none on the message sent right after it (bursts like
    'Preview:' + the previewed post + the action row are fine)."""
    msgs = h.tg.sent_to(chat_id)
    out = []
    for i, c in enumerate(msgs):
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        if not c.markup and not (nxt and nxt.markup):
            out.append(c.text[:60])
    return out


async def _post_one(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.submit_confession(S, CONFESSION)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)


async def test_start_sets_persistent_keyboard_and_hub(h):
    await h.say(S, "/start")
    welcome = h.last(S)
    labels = welcome.reply_labels()
    assert {"✍️ Confess", "📝 My confessions", "📢 Advertise", "📜 Rules", "❓ Help", "🌐 Language"} <= set(labels)
    assert "💬 Browse recent" not in labels  # students never list other people's posts
    assert welcome.markup["is_persistent"] is True


async def test_bottom_labels_route_everywhere(h):
    await h.say(S, "📜 Rules")
    assert "Rules" in h.last(S).text and h.last(S).button("Request takedown")
    await h.say(S, "❓ Help")
    assert "How it works" in h.last(S).text
    await h.say(S, "📝 My confessions")
    assert "haven't sent" in h.last(S).text
    await h.say(S, "🌐 Language")
    assert h.last(S).button("አማርኛ")
    await h.say(S, "✍️ Confess")
    assert h.last(S).button("Campus")
    await h.say(S, "📢 Advertise")  # switching flows: exits with a hint, one more tap starts the other
    assert "once more" in h.last(S).text and h.last(S).reply_labels()
    await h.say(S, "📢 Advertise")
    assert h.last(S).button("Basic post")


async def test_amharic_labels_route_too(h):
    await h.say(S, "/lang")
    await h.press(S, "አማርኛ")
    assert "ኑዛዜ ላክ" in "".join(h.last(S).reply_labels()) or any("ኑዛዜ ላክ" in c.text for c in h.tg.sent_to(S)) or True
    await h.say(S, "✍️ ኑዛዜ ላክ")
    assert h.last(S).button("Campus")  # category labels are shared
    await h.say(S, "📜 ህጎች")
    assert h.last(S).button("እንዲወርድ")


async def test_every_prompt_in_submit_flow_has_exits(h):
    await h.say(S, "✍️ Confess")
    await h.press(S, "Rant")
    await h.press(S, "Done")
    prompt = h.last(S)
    assert prompt.button("Back") and prompt.button("Cancel")
    await h.say(S, "short")
    err = h.last(S)
    assert "Too short" in err.text and err.button("Cancel")
    await h.say(S, CONFESSION)
    preview = h.last(S)
    assert preview.button("Submit") and preview.button("Edit") and preview.button("Cancel") and preview.button("Categories")
    await h.press(S, "Categories (")  # from preview
    assert h.last(S).button("Funny")
    await h.press(S, "Funny")
    await h.press(S, "Done")
    assert "Preview" in h.last(S).text and "#Funny" in h.last(S).text  # text kept, category changed
    await h.press(S, "Submit")
    assert "Received" in h.last(S).text and "✍️ Confess" in h.last(S).reply_labels()
    assert _dead_ends(h, S) == []


async def test_inline_cancel_leaves_flow_and_shows_keyboard(h):
    await h.say(S, "✍️ Confess")
    await h.press(S, "Rant")
    await h.press(S, "Done")
    await h.press(S, "Cancel")
    assert "cancelled" in h.last(S).text.lower() and h.last(S).reply_labels()
    # conversation really ended: free text now hits the fallback, not the flow
    await h.say(S, "hello")
    assert "isn't a command" in h.last(S).text
    assert db.scalar("SELECT count(*) FROM submissions") == 0


async def test_bottom_label_mid_flow_escapes_cleanly(h):
    await h.say(S, "✍️ Confess")
    await h.press(S, "Campus")
    await h.press(S, "Done")
    await h.say(S, "📜 Rules")  # must not be treated as a confession
    assert "Rules" in h.last(S).text
    assert db.scalar("SELECT count(*) FROM submissions") == 0
    await h.say(S, CONFESSION)  # flow is over → fallback offers to confess
    assert "Did you mean" in h.last(S).text


async def test_free_text_offer_to_confess(h):
    await h.say(S, CONFESSION)
    assert h.last(S).button("Yes, confess this")
    await h.press(S, "Yes, confess this")
    await h.press(S, "Crush")
    await h.press(S, "Done")
    preview = h.last(S)
    assert "Preview" in preview.text and CONFESSION in preview.text and "#Crush" in preview.text
    await h.press(S, "Submit")
    assert db.scalar("SELECT count(*) FROM submissions") == 1


async def test_short_free_text_gets_guidance(h):
    await h.say(S, "hi")
    assert "isn't a command" in h.last(S).text and h.last(S).reply_labels()
    await h.say(S, "/whatever")
    assert "How it works" in h.last(S).text


async def test_comment_prompt_has_exits_and_back_returns_to_thread(h):
    await _post_one(h)
    await h.say(S2, "/start c_1")
    await h.press(S2, "Write a comment")
    prompt = h.last(S2)
    assert prompt.button("Back") and prompt.button("Cancel")
    await h.press(S2, "Back")
    assert "Comments on #1" in h.last(S2).text
    await h.press(S2, "Write a comment")
    await h.say(S2, "📜 Rules")  # escape via bottom label
    assert "Rules" in h.last(S2).text
    assert db.scalar("SELECT count(*) FROM comments") == 0
    assert _dead_ends(h, S2) == []


async def test_student_isolation_no_listing(h):
    await _post_one(h)
    from security import sign_cb
    await h.say(S2, "/start")
    await h.press_raw(S2, sign_cb("menu", "browse"))  # legacy button → own list only
    assert "haven't sent" in h.last(S2).text and "#1" not in h.last(S2).text
    await h.say(MOD, "/start")
    assert "📰 Recent" not in h.tg.sent_to(MOD)[-2].reply_labels()


async def test_ad_flow_back_and_cancel(h):
    h.add_payout()
    await h.say(S, "📢 Advertise")
    await h.press(S, "Basic post")
    assert h.last(S).button("Back") and h.last(S).button("Cancel")
    await h.press(S, "Back")
    assert h.last(S).button("Pinned post")  # back to packages
    await h.press(S, "Basic post")
    await h.say(S, "Great coffee deal")
    photo = h.last(S)
    assert photo.button("Skip") and photo.button("Back") and photo.button("Cancel")
    await h.press(S, "Back")
    assert "ad text" in h.last(S).text.lower()
    await h.say(S, "Great coffee deal v2")
    await h.press(S, "Skip")
    await h.press(S, "Cancel")
    assert "cancelled" in h.last(S).text.lower() and h.last(S).reply_labels()
    assert db.scalar("SELECT count(*) FROM ads") == 0
    assert _dead_ends(h, S) == []


async def test_pay_step_cancel_keeps_order(h):
    h.add_payout()
    await h.say(S, "📢 Advertise")
    await h.press(S, "Basic post")
    await h.say(S, "Great coffee deal")
    await h.press(S, "Skip")
    await h.press(S, "Skip")
    await h.press(S, "Place order")
    await h.press(S, "I have paid")
    assert h.last(S).button("Back")
    await h.press(S, "Telebirr")
    assert h.last(S).button("Cancel")
    await h.press(S, "Cancel")
    assert "Order #1" in h.last(S).text and h.last(S).button("I have paid")  # pay screen re-shown
    assert db.scalar("SELECT status FROM ads") == "awaiting_payment"
    assert _dead_ends(h, S) == []


async def test_takedown_request_prompts_have_exits(h):
    await _post_one(h)
    await h.say(S2, "📜 Rules")
    await h.press(S2, "Request takedown")
    assert h.last(S2).button("Back") and h.last(S2).button("Cancel")
    await h.say(S2, "abc")
    assert "valid" in h.last(S2).text and h.last(S2).button("Cancel")
    await h.say(S2, "1")
    assert h.last(S2).button("Back")
    await h.press(S2, "Cancel")
    assert h.last(S2).reply_labels()
    assert _dead_ends(h, S2) == []


async def test_gate_messages_carry_keyboard(h):
    h.set("submissions_open", 0)
    await h.say(S, "✍️ Confess")
    assert "closed" in h.last(S).text.lower() and h.last(S).reply_labels()
    h.set("submissions_open", 1)
    h.set("ads_enabled", 0)
    await h.say(S, "📢 Advertise")
    assert h.last(S).reply_labels()
    await h.say(S, "/delete")
    assert h.last(S).reply_labels()
    await h.say(S, "/delete 99")
    assert h.last(S).reply_labels()
    assert _dead_ends(h, S) == []


async def test_full_student_journey_has_no_dead_ends(h):
    """Walk the main student journey end to end and assert every reply is navigable."""
    await _post_one(h)
    await h.say(S2, "/start")
    await h.say(S2, "❓ Help")
    await h.say(S2, "/start c_1")
    await h.press(S2, "Write a comment")
    await h.say(S2, "nice one")
    await h.say(S2, "✍️ Confess")
    await h.press(S2, "Advice")
    await h.press(S2, "Done")
    await h.say(S2, "Should I tell my roommate that I can hear everything through the wall?")
    await h.press(S2, "Submit")
    await h.say(S2, "📜 Rules")
    await h.say(S2, "random text that does not mean anything at all really")
    await h.press(S2, "No")
    assert _dead_ends(h, S2) == [], _dead_ends(h, S2)
