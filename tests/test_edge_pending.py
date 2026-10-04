"""Edge cases for pending-input states: expiry, chat binding, stale state never misroutes text."""
from __future__ import annotations

import pytest

import db
from tests.e2e_harness import ADMIN_GROUP, OWNER, h  # noqa: F401

pytestmark = pytest.mark.asyncio

S, MOD, ADMIN, ADV = 501, 701, 702, 801


async def _order_with_contact(h):
    h.add_payout()
    await h.say(ADV, "/advertise")
    await h.press(ADV, "Basic post")
    await h.say(ADV, "Coffee deal for students at the campus café this week only.")
    await h.press(ADV, "Skip")
    await h.press(ADV, "Skip")
    await h.press(ADV, "Place order")
    await h.say(ADV, "/myads")
    await h.press(ADV, "#1")
    await h.press(ADV, "Contact admin")
    await h.say(ADV, "help please")
    return [c for c in h.tg.calls if c.chat_id == ADMIN_GROUP and "Advertiser message" in c.text][-1]


async def test_admin_reply_state_expires_and_is_chat_bound(h):
    h.add_staff(ADMIN, "admin")
    card = await _order_with_contact(h)
    await h.press(ADMIN, "Reply", message=card, chat_id=ADMIN_GROUP)
    # typing in a *different* chat (DM) must not be forwarded
    await h.say(ADMIN, "private note to the bot")
    assert not any("private note" in t for t in h.texts(ADV))
    # state was dropped by the chat mismatch → later group text is not forwarded either
    await h.say(ADMIN, "late text", chat_id=ADMIN_GROUP)
    assert not any("late text" in t for t in h.texts(ADV))
    # fresh Reply + expiry
    await h.press(ADMIN, "Reply", message=card, chat_id=ADMIN_GROUP)
    h.app.user_data[ADMIN]["ad_reply"]["at"] -= 10_000
    await h.say(ADMIN, "too late", chat_id=ADMIN_GROUP)
    assert not any("too late" in t for t in h.texts(ADV))
    # happy path still works
    await h.press(ADMIN, "Reply", message=card, chat_id=ADMIN_GROUP)
    await h.say(ADMIN, "on time", chat_id=ADMIN_GROUP)
    assert any("on time" in t for t in h.texts(ADV))


async def test_setting_input_expires(h):
    await h.say(OWNER, "/settings")
    await h.press(OWNER, "Posting")
    await h.press(OWNER, "max_ads_per_day")
    h.app.user_data[OWNER]["setting_key"]["at"] -= 10_000
    await h.say(OWNER, "7")
    assert db.scalar("SELECT value FROM settings WHERE key='max_ads_per_day'") == "3"
    assert "didn't catch" in h.last(OWNER).text or "Did you mean" in h.last(OWNER).text or h.last(OWNER).reply_labels()


async def test_hub_input_expires_and_cancel_works(h):
    h.add_staff(ADMIN, "admin")
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Wordlist")
    await h.press(ADMIN, "Add")
    await h.say(ADMIN, "/cancel")
    assert "Cancelled" in h.last(ADMIN).text
    await h.say(ADMIN, "notaword")
    assert db.scalar("SELECT count(*) FROM wordlist WHERE word='notaword'") == 0


async def test_moderator_edit_state_expires(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    await h.submit_confession(S, "A confession long enough to be reviewed by the moderator in this test.")
    await h.say(MOD, "/next")
    await h.press(MOD, "Edit")
    h.app.user_data[MOD]["editing"]["at"] -= 10_000
    await h.say(MOD, "This would have replaced the text if the edit state were still alive here.")
    assert db.scalar("SELECT text FROM submissions").startswith("A confession long enough")


async def test_staff_label_row_does_not_leak_to_students(h):
    await h.say(S, "📋 Queue")
    await h.say(S, "⚙️ Admin")
    await h.say(S, "📊 Stats")
    assert all("Queue" not in t and "Admin —" not in t and "Status" not in t for t in h.texts(S))
    assert all(c.reply_labels() or c.markup for c in h.tg.sent_to(S))  # but they still get guidance, not silence
