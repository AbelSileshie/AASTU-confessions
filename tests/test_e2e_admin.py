"""E2E: two-stage approval in the admin group, topics routing, declined ledger, /admin hub."""
from __future__ import annotations

import pytest

import db
from jobs.housekeeping import purge_declined
from jobs.poster import poster_job
from tests.e2e_harness import ADMIN_GROUP, CHANNEL, OWNER, h  # noqa: F401
from tests.fake_telegram import callback_update

pytestmark = pytest.mark.asyncio

S, S2, MOD, ADMIN, ADMIN2 = 501, 502, 701, 702, 703
TEXT = "A confession long enough to go through the two-stage review pipeline properly."
FLAGGED = "Someone has a video of me and is threatening to share it with the whole class."


def _group_cards(h):
    return [c for c in h.tg.calls if c.chat_id == ADMIN_GROUP and c.method == "sendMessage"]


async def _two_stage(h):  # name kept for brevity: sets up staff + fast posting
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    h.add_staff(MOD)
    h.add_staff(ADMIN, "admin")
    h.add_staff(ADMIN2, "admin")
    db.execute("UPDATE staff SET display=CASE user_id WHEN ? THEN 'Mod Mary' WHEN ? THEN 'Admin Abel' WHEN ? THEN 'Admin Bini' END",
               (MOD, ADMIN, ADMIN2))


# ---------------------------------------------------------------------------
# Single-stage approval + flagged notice + moderator feedback
# ---------------------------------------------------------------------------

async def test_moderator_approval_publishes_directly(h):
    await _two_stage(h)
    await h.submit_confession(S, TEXT)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    assert "queued for posting" in h.last(MOD).text
    assert db.scalar("SELECT stage FROM outbox") == "ready"
    await h.run_job(poster_job)
    assert db.scalar("SELECT approved_by FROM posts WHERE number=1") == MOD
    assert _group_cards(h) == []  # nothing for admins to do


async def test_flagged_publishes_with_notice_and_one_tap_takedown(h):
    await _two_stage(h)
    h.set("topic_flagged", 77)
    await h.submit_confession(S, FLAGGED)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    assert "flagged" in h.last(MOD).text
    await h.run_job(poster_job)
    assert db.scalar("SELECT count(*) FROM posts") == 1
    notice = _group_cards(h)[-1]
    assert notice.params.get("message_thread_id") == 77 and "Flagged post is live · #1" in notice.text and "Mod Mary" in notice.text
    assert notice.button("Take down") and notice.button("Leave it up")
    await h.press(ADMIN, "Take down", message=notice, chat_id=ADMIN_GROUP)
    assert db.scalar("SELECT deleted FROM posts WHERE number=1") == 1
    # moderator who approved is told
    assert any("taken down by Admin Abel" in t for t in h.texts(MOD))
    # author told too
    assert any("deleted" in t for t in h.texts(S))


async def test_flagged_notice_fine_button_closes_card(h):
    await _two_stage(h)
    await h.submit_confession(S, FLAGGED)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    notice = _group_cards(h)[-1]
    await h.press(ADMIN, "Leave it up", message=notice, chat_id=ADMIN_GROUP)
    edited = [c for c in h.tg.calls if c.method == "editMessageText" and c.message_id == notice.message_id][-1]
    assert "left up" in edited.text and edited.markup is None
    assert db.scalar("SELECT deleted FROM posts WHERE number=1") == 0


async def test_moderator_not_notified_for_own_takedown(h):
    await _two_stage(h)
    await h.submit_confession(S, TEXT)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    await h.say(MOD, "/takedown 1 changed my mind")
    assert not any("taken down by" in t for t in h.texts(MOD))


# ---------------------------------------------------------------------------
# Declined ledger
# ---------------------------------------------------------------------------

async def test_moderator_reject_creates_declined_card_and_restore_round_trip(h):
    await _two_stage(h)
    await h.submit_confession(S, TEXT)
    await h.say(MOD, "/next")
    await h.press(MOD, "Reject")
    await h.press(MOD, "spam")
    assert db.scalar("SELECT count(*) FROM submissions") == 0
    card = _group_cards(h)[-1]
    assert "spam" in card.text and "Mod Mary" in card.text and TEXT in card.text
    restore_data = card.callback("Restore")
    # moderator cannot restore
    await h.app.process_update(callback_update(h.bot, MOD, restore_data, card, chat_id=ADMIN_GROUP))
    assert db.scalar("SELECT count(*) FROM submissions") == 0
    await h.press(ADMIN, "Restore", message=card, chat_id=ADMIN_GROUP)
    assert db.scalar("SELECT count(*) FROM submissions") == 1 and db.scalar("SELECT count(*) FROM declined") == 0
    # second press is a no-op
    await h.press_raw(ADMIN, restore_data, message=card)
    assert db.scalar("SELECT count(*) FROM submissions") == 1


async def test_declined_purged_after_ttl(h):
    await _two_stage(h)
    await h.submit_confession(S, TEXT)
    await h.say(MOD, "/next")
    await h.press(MOD, "Reject")
    await h.press(MOD, "other")
    card = _group_cards(h)[-1]
    h.set("declined_ttl_hours", 1)
    assert await purge_declined(h.context(), db.now()) == 0
    db.execute("UPDATE declined SET created=created-7200")
    assert await purge_declined(h.context(), db.now()) == 1
    assert (ADMIN_GROUP, card.message_id) in h.tg.deleted
    assert db.scalar("SELECT count(*) FROM declined") == 0


# ---------------------------------------------------------------------------
# Topic routing
# ---------------------------------------------------------------------------

async def test_alerts_route_to_topics_and_fall_back_to_main(h):
    await _two_stage(h)
    h.set("topic_takedowns", 31)
    await h.submit_confession(S, TEXT)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    await h.say(S2, "/rules")
    await h.press(S2, "Request takedown")
    await h.say(S2, "1")
    await h.say(S2, "identifies me")
    td = _group_cards(h)[-1]
    assert "Takedown request · #1" in td.text and td.params.get("message_thread_id") == 31
    # unconfigured kind → main chat
    from jobs.housekeeping import housekeeping_job
    h.set("topic_alerts", 0)
    h.set("queue_alert_threshold", 1)
    await h.submit_confession(S2, "Another confession long enough to sit in the queue for the alert.")
    await h.run_job(housekeeping_job)
    alert = _group_cards(h)[-1]
    assert "Review queue" in alert.text and alert.params.get("message_thread_id") is None


async def test_topics_created_automatically_at_startup(h):
    from config import cfg
    assert len(h.tg.topics) == 5
    assert cfg("topic_flagged") == 101 and cfg("topic_alerts") == 105
    assert db.scalar("SELECT count(*) FROM audit WHERE action='topics_create'") == 1


async def test_topics_retry_hourly_and_setup_button(h):
    from common import ensure_topics
    from config import cfg

    await _two_stage(h)
    # one topic lost → hourly check recreates only that one
    h.set("topic_declined", 0)
    res = await ensure_topics(h.context())
    assert res["created"] == ["declined"] and cfg("topic_declined") == 106
    # already complete → no-op
    assert (await ensure_topics(h.context()))["created"] == []
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Group setup")
    assert "All topics are set up" in h.last(ADMIN).text or any("All topics" in c.text for c in h.tg.calls if c.method == "editMessageText")
    await h.press(ADMIN, "Reset to main chat")
    assert cfg("topic_flagged") == 0
    await h.press(ADMIN, "Create missing now")
    assert cfg("topic_flagged") == 107


async def test_topics_disabled_warns_owner_once_and_falls_back(h):
    from common import ensure_topics

    await _two_stage(h)
    for k in ("flagged", "takedowns", "declined", "ads", "alerts"):
        h.set(f"topic_{k}", 0)
    h.tg.group_is_forum = False
    before = len(h.tg.sent_to(OWNER))
    res = await ensure_topics(h.context())
    assert res["error"] == "topics_disabled" and res["created"] == []
    assert len(h.tg.sent_to(OWNER)) == before + 1 and "Topics are not enabled" in h.last(OWNER).text
    await ensure_topics(h.context())
    assert len(h.tg.sent_to(OWNER)) == before + 1  # warned once
    # alerts still flow into the main chat
    h.set("queue_alert_threshold", 1)
    await h.submit_confession(S, TEXT)
    from jobs.housekeeping import housekeeping_job
    await h.run_job(housekeeping_job)
    assert _group_cards(h)[-1].params.get("message_thread_id") is None


# ---------------------------------------------------------------------------
# /admin hub
# ---------------------------------------------------------------------------

async def test_staff_keyboard_row_by_role(h):
    await _two_stage(h)
    await h.say(MOD, "/start")
    labels = h.tg.sent_to(MOD)[-1].reply_labels()
    assert "📋 Queue" in labels and "📊 Stats" in labels and "⚙️ Admin" not in labels
    await h.say(ADMIN, "/start")
    assert "⚙️ Admin" in h.tg.sent_to(ADMIN)[-1].reply_labels()
    await h.say(S, "/start")
    assert "📋 Queue" not in h.tg.sent_to(S)[-1].reply_labels()
    await h.say(MOD, "📋 Queue")
    assert "Queue is empty" in h.last(MOD).text
    await h.say(MOD, "⚙️ Admin")
    assert "Admin — pick" not in h.last(MOD).text  # moderators get the free-text fallback, not the hub


async def test_admin_hub_staff_management(h):
    await _two_stage(h)
    await h.say(OWNER, "⚙️ Admin")
    hub = h.last(OWNER)
    assert hub.button("Staff") and hub.button("Payout accounts") and hub.button("Group setup")
    await h.press(OWNER, "Staff")
    assert h.last(OWNER).button("Mod Mary") and h.last(OWNER).button("Invite admin")
    await h.press(OWNER, "Mod Mary")
    assert h.last(OWNER).button("Make admin")
    await h.press(OWNER, "Make admin")
    assert db.scalar("SELECT role FROM staff WHERE user_id=?", (MOD,)) == "admin"
    await h.press(OWNER, "Remove")
    assert db.scalar("SELECT active FROM staff WHERE user_id=?", (MOD,)) == 0
    await h.press(OWNER, "Invite moderator")
    assert "inv_" in h.last(OWNER).text
    # admin (not owner) sees no 'Invite admin' and no payouts
    await h.say(ADMIN, "/admin")
    assert h.last(ADMIN).button("Payout accounts") is None
    await h.press(ADMIN, "Staff")
    assert h.last(ADMIN).button("Invite admin") is None
    await h.press(ADMIN, "Admin Bini")
    assert h.last(ADMIN).button("Make moderator") is None  # peer: cannot demote
    await h.press(ADMIN, "Remove")
    assert db.scalar("SELECT active FROM staff WHERE user_id=?", (ADMIN2,)) == 1


async def test_admin_hub_catalog_and_wordlist(h):
    await _two_stage(h)
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Packages")
    await h.press(ADMIN, "Basic post")
    await h.press(ADMIN, "Price")
    await h.say(ADMIN, "950")
    assert "Saved" in h.last(ADMIN).text and db.scalar("SELECT price_etb FROM packages WHERE slug='basic'") == 950
    await h.press(ADMIN, "Back")
    await h.press(ADMIN, "Disable")
    assert db.scalar("SELECT enabled FROM packages WHERE slug='basic'") == 0
    await h.press(ADMIN, "Back")
    await h.press(ADMIN, "Add package")
    await h.say(ADMIN, "story | Story post | 1200")
    assert db.scalar("SELECT price_etb FROM packages WHERE slug='story'") == 1200
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Categories")
    await h.press(ADMIN, "Add category")
    await h.say(ADMIN, "bad input without pipes")
    assert "Invalid" in h.last(ADMIN).text
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Wordlist")
    await h.press(ADMIN, "Add")
    await h.say(ADMIN, "foo, bar\nbaz")
    assert db.scalar("SELECT count(*) FROM wordlist WHERE kind='block' AND word IN ('foo','bar','baz')") == 3
    await h.press(ADMIN, "Back")
    await h.press(ADMIN, "Remove “foo”")
    assert db.scalar("SELECT count(*) FROM wordlist WHERE word='foo'") == 0


async def test_owner_payout_menu_and_admin_denied(h):
    await _two_stage(h)
    await h.say(OWNER, "/admin")
    await h.press(OWNER, "Payout accounts")
    await h.press(OWNER, "Add account")
    await h.say(OWNER, "telebirr | 0911 223 344 | Kuru Labs")
    assert db.scalar("SELECT account FROM payout_accounts") == "0911223344"
    await h.press(OWNER, "Back")
    await h.press(OWNER, "…3344")
    assert db.scalar("SELECT enabled FROM payout_accounts") == 0
    from security import sign_cb
    await h.say(ADMIN, "/admin")
    await h.press_raw(ADMIN, sign_cb("hub", "pay:add"))
    assert "hub_input" not in h.app.user_data.get(ADMIN, {})


async def test_admin_hub_has_no_dead_ends(h):
    await _two_stage(h)
    await h.say(OWNER, "/admin")
    for label in ("Staff", "Back", "Categories", "Back", "Packages", "Back", "Wordlist", "Back", "Health", "Back", "Ads", "Back", "Group setup", "Back"):
        await h.press(OWNER, label)
    for c in h.tg.sent_to(OWNER) + [c for c in h.tg.calls if c.method == "editMessageText" and c.chat_id == OWNER]:
        assert c.markup, f"dead end: {c.text[:50]}"
