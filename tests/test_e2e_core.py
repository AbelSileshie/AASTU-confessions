"""E2E: submit → review → post → notify → delete (M1/M2 acceptance)."""
from __future__ import annotations

import pytest

import db
from jobs.housekeeping import housekeeping_job, release_stale_claims
from jobs.poster import poster_job
from security import author_hash
from tests.e2e_harness import ADMIN_GROUP, CHANNEL, OWNER, h  # noqa: F401

pytestmark = pytest.mark.asyncio

STUDENT = 501
STUDENT2 = 502
MOD = 701
MOD2 = 702
CONFESSION = "I have been secretly leaving snacks in the library for whoever studies late."


async def test_start_shows_menu_and_no_raw_id_stored(h):
    await h.say(STUDENT, "/start")
    welcome, hub = h.tg.sent_to(STUDENT)[-2:]
    assert "anonymous" in welcome.text.lower()
    assert "✍️ Confess" in welcome.reply_labels() and "📜 Rules" in welcome.reply_labels()  # persistent keyboard
    assert hub.button("Confess") and hub.button("Advertise") and hub.button("Rules")
    rows = db.q("SELECT * FROM users")
    assert len(rows) == 1 and rows[0]["author"] == author_hash(STUDENT)
    dump = " ".join(str(dict(r)) for r in rows)
    assert str(STUDENT) not in dump.replace(rows[0]["author"], "")


async def test_full_confession_lifecycle(h):
    h.add_staff(MOD)
    await h.submit_confession(STUDENT, CONFESSION)
    assert "Received" in h.last(STUDENT).text
    assert db.scalar("SELECT count(*) FROM submissions") == 1

    # moderator reviews
    await h.say(MOD, "/next")
    card = h.last(MOD)
    assert CONFESSION in card.text and "Queue: 1" in card.text
    await h.press(MOD, "Approve")
    assert db.scalar("SELECT count(*) FROM submissions") == 0
    assert db.scalar("SELECT count(*) FROM outbox") == 1

    # poster publishes to the channel
    h.set("post_interval_sec", 5)
    await h.run_job(poster_job)
    posted = [c for c in h.tg.calls if c.chat_id == CHANNEL and c.method == "sendMessage"]
    assert len(posted) == 1
    assert posted[0].text.startswith("🏫 Confession #1\n") and CONFESSION in posted[0].text and "#Campus" in posted[0].text
    assert "parse_mode" not in posted[0].params or not posted[0].params["parse_mode"]
    assert posted[0].button("Comments (0)")["url"].endswith("?start=c_1")
    assert db.scalar("SELECT count(*) FROM outbox") == 0
    assert db.scalar("SELECT number FROM posts") == 1

    # author notified via encrypted chat_ref
    live = [c for c in h.tg.sent_to(STUDENT) if "live as #1" in c.text][-1]
    assert live.button("Open in channel")["url"] == f"https://t.me/{h.tg.channel_username}/{posted[0].message_id}"
    assert live.button("Comments")["url"].endswith("?start=c_1")

    # author deletes their own post; other user cannot
    await h.say(STUDENT2, "/delete 1")
    assert "not yours" in h.last(STUDENT2).text
    assert db.scalar("SELECT deleted FROM posts WHERE number=1") == 0
    await h.say(STUDENT, "/delete 1")
    assert "deleted" in h.last(STUDENT).text.lower()
    assert db.scalar("SELECT deleted FROM posts WHERE number=1") == 1
    assert (CHANNEL, posted[0].message_id) in h.tg.deleted


async def test_submission_checks_in_order(h):
    await h.say(STUDENT, "/start")
    await h.press(STUDENT, "Confess")
    await h.press(STUDENT, "Rant")
    await h.say(STUDENT, "too short")
    assert "Too short" in h.last(STUDENT).text
    await h.say(STUDENT, "please check my channel at https://t.me/somewhere for more details")
    assert "Links" in h.last(STUDENT).text
    await h.say(STUDENT, "ask @someone_famous about what happened yesterday night")
    assert "Usernames" in h.last(STUDENT).text
    await h.say(STUDENT, "my number is 0911223344 call me if you want to talk")
    assert "Phone" in h.last(STUDENT).text
    db.execute("INSERT INTO wordlist(word, kind) VALUES ('forbiddenword', 'block')")
    await h.say(STUDENT, "this contains forbiddenword somewhere inside the sentence")
    assert "not allowed" in h.last(STUDENT).text
    await h.say(STUDENT, CONFESSION)
    assert "Preview" in h.last(STUDENT).text


async def test_cooldown_and_duplicate(h):
    await h.submit_confession(STUDENT, CONFESSION)
    await h.say(STUDENT, "/confess")
    assert "wait" in h.last(STUDENT).text.lower()
    h.set("submit_cooldown_sec", 0)
    await h.say(STUDENT2, "/confess")
    await h.press(STUDENT2, "Campus")
    await h.say(STUDENT2, CONFESSION.upper() + "!!!")
    assert "already submitted" in h.last(STUDENT2).text


async def test_kill_switch_and_queue_cap(h):
    h.set("submissions_open", 0)
    await h.say(STUDENT, "/confess")
    assert "closed" in h.last(STUDENT).text.lower()
    h.set("submissions_open", 1)
    h.set("queue_soft_cap", 1)
    h.set("submit_cooldown_sec", 0)
    await h.submit_confession(STUDENT, CONFESSION)
    await h.say(STUDENT2, "/confess")
    assert "full" in h.last(STUDENT2).text.lower()


async def test_two_moderators_never_share_an_item_and_stale_claims_release(h):
    h.add_staff(MOD)
    h.add_staff(MOD2)
    h.set("submit_cooldown_sec", 0)
    await h.submit_confession(STUDENT, CONFESSION)
    await h.submit_confession(STUDENT2, "Another totally different confession about the cafeteria food quality.")
    await h.say(MOD, "/next")
    await h.say(MOD2, "/next")
    ids = {r["claimed_by"] for r in db.q("SELECT claimed_by FROM submissions")}
    assert ids == {MOD, MOD2}
    # MOD2 cannot act on MOD's card
    card = h.last(MOD)
    await h.app.process_update(__import__("tests.fake_telegram", fromlist=["callback_update"]).callback_update(
        h.bot, MOD2, card.callback("Approve"), card))
    assert db.scalar("SELECT count(*) FROM submissions") == 2
    # stale claims released
    db.execute("UPDATE submissions SET claimed_at=claimed_at-100000")
    assert release_stale_claims(db.now()) == 2
    assert db.scalar("SELECT count(*) FROM submissions WHERE claimed_by IS NULL") == 2


async def test_risk_words_flag_and_prioritise(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    await h.submit_confession(STUDENT, "Nothing special here just a calm and long enough confession text.")
    await h.submit_confession(STUDENT2, "Someone has a video of me and threatens to share it with everyone.")
    assert db.scalar("SELECT priority FROM submissions WHERE author=?", (author_hash(STUDENT2),)) == 1
    await h.say(MOD, "/next")
    card = h.last(MOD)
    assert "FLAGGED" in card.text and "⚠️video⚠️" in card.text  # flagged item served first
    h.set("flagged_requires_admin", 1)
    await h.press(MOD, "Approve")
    assert db.scalar("SELECT count(*) FROM submissions") == 2  # moderator blocked
    h.add_staff(MOD2, "admin")
    await h.press(MOD, "Escalate")
    assert "Escalation" in h.last(ADMIN_GROUP).text


async def test_edit_reject_ban_flow(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("ban_strikes", 1)
    await h.submit_confession(STUDENT, CONFESSION)
    await h.say(MOD, "/next")
    await h.press(MOD, "Edit")
    await h.say(MOD, "Edited version of the confession that is long enough to pass.")
    assert db.scalar("SELECT text FROM submissions").startswith("Edited version")
    await h.press(MOD, "Reject")
    await h.press(MOD, "names a person")
    assert db.scalar("SELECT count(*) FROM submissions") == 0
    assert any("not approved" in t and "names a person" in t for t in h.texts(STUDENT))
    # ban on next submission
    await h.submit_confession(STUDENT, "A second confession that will get its author banned by a strike.")
    await h.say(MOD, "/next")
    await h.press(MOD, "Ban")
    await h.press(MOD, "Ban + reject")
    assert db.scalar("SELECT banned_until FROM users WHERE author=?", (author_hash(STUDENT),)) > db.now()
    await h.say(STUDENT, "/confess")
    assert "cannot submit" in h.last(STUDENT).text.lower()
    # permanent ban requires admin
    assert db.scalar("SELECT count(*) FROM audit WHERE action='strike'") == 1


async def test_unknown_user_staff_commands_are_silent(h):
    for cmd in ("/next", "/stats", "/settings", "/staff list", "/set min_len 5", "/takedown 1 x", "/ads", "/payout_accounts"):
        await h.say(STUDENT, cmd)
    assert h.tg.sent_to(STUDENT) == []


async def test_staff_invite_and_role_boundaries(h):
    await h.say(OWNER, "/staff add admin")
    link = h.last(OWNER).text.split("start=")[1].strip()
    assert link.startswith("inv_")
    await h.say(MOD, f"/start {link}")
    assert "admin" in h.last(MOD).text
    # reuse fails
    await h.say(MOD2, f"/start {link}")
    assert "invalid" in h.last(MOD2).text
    # admin may invite moderators but not admins
    await h.say(MOD, "/staff add admin")
    assert "cannot" in h.last(MOD).text.lower()
    await h.say(MOD, "/staff add")
    assert "inv_" in h.last(MOD).text
    # admin cannot remove the owner or a peer
    await h.say(MOD, f"/staff remove {OWNER}")
    assert "Cannot" in h.last(MOD).text


async def test_settings_change_applies_without_restart(h):
    from config import cfg

    assert cfg("post_interval_sec") == 120
    await h.say(OWNER, "/set post_interval_sec 60")
    assert "✅" in h.last(OWNER).text and cfg("post_interval_sec") == 60
    await h.say(OWNER, "/set post_interval_sec 1")
    assert "minimum" in h.last(OWNER).text
    await h.say(OWNER, "/set no_such_key 1")
    assert "unknown" in h.last(OWNER).text
    # menu path
    await h.say(OWNER, "/settings")
    await h.press(OWNER, "Posting")
    await h.press(OWNER, "max_ads_per_day")
    await h.say(OWNER, "5")
    assert cfg("max_ads_per_day") == 5


async def test_takedown_request_and_moderator_action(h):
    h.add_staff(MOD)
    await h.submit_confession(STUDENT, CONFESSION)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    h.set("post_interval_sec", 0)
    await h.run_job(poster_job)
    await h.say(STUDENT2, "/rules")
    await h.press(STUDENT2, "Request takedown")
    await h.say(STUDENT2, "1")
    await h.say(STUDENT2, "it identifies me")
    alert = h.last(ADMIN_GROUP)
    assert "Takedown request · #1" in alert.text
    await h.press(MOD, "Take down", message=alert, chat_id=ADMIN_GROUP)
    assert db.scalar("SELECT deleted FROM posts WHERE number=1") == 1


async def test_queue_alert_and_stats(h):
    h.add_staff(MOD)
    h.set("queue_alert_threshold", 1)
    await h.submit_confession(STUDENT, CONFESSION)
    await h.run_job(housekeeping_job)
    assert "Review queue is at 1" in h.last(ADMIN_GROUP).text
    await h.say(MOD, "/stats")
    assert "Queue: 1" in h.last(MOD).text


async def test_group_chat_commands_ignored(h):
    await h.say(STUDENT, "/start", chat_id=-5000)
    await h.say(STUDENT, "/confess", chat_id=-5000)
    assert h.tg.sent_to(-5000) == []
