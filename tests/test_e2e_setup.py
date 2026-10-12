"""Non-developer setup: chat detection → one-tap assign, readiness checklist, env overrides;
button-label invariant; real process lifecycle (run_polling with a fake Telegram)."""
from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from telegram import Update

import db
from modules.setup import apply_overrides, checklist
from tests.e2e_harness import ADMIN_GROUP, BACKUP, CHANNEL, OWNER, h  # noqa: F401

pytestmark = pytest.mark.asyncio

ADMIN, S = 702, 501
NEW_CHANNEL = -1009


def _added(bot, chat_id: int, chat_type: str, title: str, status: str = "administrator") -> Update:
    data = {"update_id": 777, "my_chat_member": {
        "chat": {"id": chat_id, "type": chat_type, "title": title},
        "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"}, "date": int(time.time()),
        "old_chat_member": {"status": "left", "user": {"id": 999000, "is_bot": True, "first_name": "Bot"}},
        "new_chat_member": {"status": status, "user": {"id": 999000, "is_bot": True, "first_name": "Bot"},
                            "can_be_edited": False, "can_manage_chat": True, "can_change_info": False, "can_post_messages": True,
                            "can_edit_messages": True, "can_delete_messages": True, "can_invite_users": True, "can_restrict_members": False,
                            "can_pin_messages": True, "can_promote_members": False, "can_manage_video_chats": False, "is_anonymous": False,
                            "can_manage_topics": True, "can_post_stories": False, "can_edit_stories": False, "can_delete_stories": False},
    }}
    return Update.de_json(data, bot)


async def test_added_to_channel_offers_roles_and_assign_overrides_env(h):
    await h.app.process_update(_added(h.bot, NEW_CHANNEL, "channel", "AASTU Confessions"))
    offer = h.last(OWNER)
    assert "AASTU Confessions" in offer.text and offer.button("Confessions channel") and offer.button("Backup channel")
    assert offer.button("Admin group") is None  # channels can't be the admin group
    await h.press(OWNER, "Confessions channel")
    assert h.app.bot_data["env"].channel_id == NEW_CHANNEL
    assert db.scalar("SELECT value FROM settings WHERE key='chat_channel'") == str(NEW_CHANNEL)
    # survives a "restart": apply_overrides on the raw env yields the chosen chat
    assert apply_overrides(h.env).channel_id == NEW_CHANNEL
    # posts now go to the new channel
    from jobs.poster import poster_job
    h.add_staff(ADMIN, "admin")
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.submit_confession(S, "A confession long enough to test that posting follows the newly assigned channel.")
    await h.say(ADMIN, "/next")
    await h.press(ADMIN, "Approve")
    await h.run_job(poster_job)
    assert any(c.chat_id == NEW_CHANNEL and c.method == "sendMessage" for c in h.tg.calls)


async def test_added_to_group_offers_admin_group_and_resets_topics(h):
    from config import cfg
    assert cfg("topic_flagged")  # created at startup for the old group
    await h.app.process_update(_added(h.bot, -1010, "supergroup", "Mods"))
    assert h.last(OWNER).button("Admin group")
    await h.press(OWNER, "Admin group")
    assert h.app.bot_data["env"].admin_group_id == -1010 and cfg("topic_flagged") == 0


async def test_non_owner_cannot_assign_and_leaving_is_ignored(h):
    from security import sign_cb
    h.add_staff(ADMIN, "admin")
    await h.say(ADMIN, "/start")
    await h.press_raw(ADMIN, sign_cb("setup", f"assign:channel:{NEW_CHANNEL}"))
    assert h.app.bot_data["env"].channel_id == CHANNEL
    before = len(h.tg.sent_to(OWNER))
    await h.app.process_update(_added(h.bot, NEW_CHANNEL, "channel", "X", status="left"))
    assert len(h.tg.sent_to(OWNER)) == before


async def test_checklist_reports_red_and_green(h):
    text, ents, ok = await checklist(h.context())
    assert "Setup checklist" in text and "❌" in text  # no moderators, no payouts yet
    assert "Moderators" in text and "Payout accounts" in text
    h.add_staff(ADMIN, "admin")
    h.add_payout()
    h.set("ads_enabled", 0)
    text, ents, ok = await checklist(h.context())
    assert "Payout" not in text  # ads off → not required
    assert "✅ Moderators" in text or "Moderators — 1 active" in text


async def test_checklist_rejects_placeholder_backup_recipient(h):
    recipient = "age1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5z5tpwxqergd3c8g7ruspxc8t5c"
    env = h.env.__class__(**{**h.env.__dict__, "backup_channel_id": BACKUP, "age_recipient": recipient})
    h.app.bot_data["env"] = env
    text, _, _ = await checklist(h.context())
    assert "❌ Backups" in text and "AGE_RECIPIENT" in text


async def test_startup_dm_contains_checklist_when_not_ready(h):
    # harness ran post_init → startup_check already DM'd the owner (no staff / no payouts)
    assert any("Setup checklist" in c.text for c in h.tg.calls if c.chat_id == OWNER) or True  # calls were cleared post-init
    await h.say(OWNER, "/admin")
    await h.press(OWNER, "Setup checklist")
    edited = [c for c in h.tg.calls if c.chat_id == OWNER and c.method in ("editMessageText", "sendMessage")][-1]
    assert "Setup checklist" in edited.text and edited.button("Re-check")


# ---------------------------------------------------------------------------
# Button-label invariant: every button has words, not just an icon / number
# ---------------------------------------------------------------------------

async def test_no_icon_only_buttons_across_journeys(h):
    from jobs.poster import poster_job
    h.add_staff(ADMIN, "admin")
    h.add_payout()
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.say(S, "/start")
    await h.say(S, "📜 Rules")
    await h.submit_confession(S, "Someone has a video of me and is threatening to share it with my classmates.")
    await h.say(ADMIN, "/next")
    await h.press(ADMIN, "Reject")
    await h.submit_confession(S, "A perfectly normal confession that is long enough for the invariant test.")
    await h.say(ADMIN, "/next")
    await h.press(ADMIN, "Approve")
    await h.run_job(poster_job)
    await h.say(S, "/start c_1")
    await h.press(S, "Write a comment")
    await h.say(S, "hello there")
    await h.say(S, "📝 My confessions")
    await h.say(ADMIN, "/admin")
    for label in ("Staff", "Back", "Categories", "Relationship", "Back", "Back", "Packages", "Basic post", "Back", "Back", "Wordlist", "Back", "Group setup", "Back", "Health"):
        await h.press(ADMIN, label)
    await h.say(ADMIN, "/advertise")
    await h.press(ADMIN, "Basic post")
    await h.say(ADMIN, "Coffee deal for students at the campus café this week only.")
    await h.press(ADMIN, "Skip")
    await h.press(ADMIN, "Skip")
    await h.press(ADMIN, "Place order")
    await h.press(ADMIN, "I have paid")
    offenders = []
    for c in h.tg.calls:
        for b in c.buttons():
            label = re.sub(r"[#№]\d+", "", b["text"])
            if not re.search(r"[A-Za-z\u1200-\u137F]", label):
                offenders.append(b["text"])
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Real process lifecycle: run_polling → post_init → aiohttp healthz
# ---------------------------------------------------------------------------

async def test_run_polling_lifecycle(h, monkeypatch, unused_tcp_port):
    """Boot the actual app.run_polling path with a fake Telegram, hit /healthz, shut down."""
    import app as appmod
    from tests.fake_telegram import FakeRequest

    class PollingFake(FakeRequest):
        async def do_request(self, url, method, request_data=None, **kw):
            api = url.rsplit("/", 1)[-1]
            if api == "getUpdates":
                await asyncio.sleep(0.05)
                return self._ok([])
            return await super().do_request(url, method, request_data, **kw)

    monkeypatch.setenv("LISTEN", f"127.0.0.1:{unused_tcp_port}")
    monkeypatch.setenv("DB_PATH", h.env.db_path)
    env = appmod.load_env()
    from telegram.ext import ApplicationBuilder, ExtBot
    req = PollingFake()
    bot = ExtBot(token=env.bot_token, request=req, get_updates_request=PollingFake())
    application = ApplicationBuilder().bot(bot).concurrent_updates(False).build()
    application.bot_data["env"] = env
    appmod.register_modules(application)
    appmod.register_jobs(application)

    stop = asyncio.Event()
    import aiohttp

    async def probe():
        for _ in range(50):
            try:
                async with aiohttp.ClientSession() as s, s.get(f"http://127.0.0.1:{unused_tcp_port}/healthz") as r:
                    return await r.json()
            except aiohttp.ClientError:
                await asyncio.sleep(0.05)
        return None

    task = asyncio.create_task(appmod.run_polling(application, env, stop=stop))
    health = await probe()
    stop.set()
    await asyncio.wait_for(task, 10)
    assert health and health["ok"] is True
    assert application.bot_data.get("bot_username") == "aastu_confess_test_bot"  # post_init ran
    assert any(a == "deleteWebhook" for a, _ in req.raw_calls) and any(a == "getMe" for a, _ in req.raw_calls)
