"""Staff-side dead-end audit: moderator and admin journeys leave something to tap at every step."""
from __future__ import annotations

import pytest

import db
from jobs.poster import poster_job
from tests.e2e_harness import ADMIN_GROUP, OWNER, h  # noqa: F401

pytestmark = pytest.mark.asyncio

S, MOD, ADMIN = 501, 701, 702


def _dead_ends(h, chat_id: int) -> list[str]:
    msgs = h.tg.sent_to(chat_id)
    out = []
    for i, c in enumerate(msgs):
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        if not c.markup and not (nxt and nxt.markup):
            out.append(c.text[:60])
    return out


async def test_moderator_journey_has_no_dead_ends(h):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.say(MOD, "/start")
    await h.say(MOD, "📋 Queue")                    # empty
    for i in range(3):
        await h.submit_confession(S, f"Confession number {i} long enough to pass validation for the audit.")
    await h.say(MOD, "📋 Queue")
    await h.press(MOD, "Edit")
    await h.press(MOD, "Cancel edit")
    await h.press(MOD, "Approve")
    await h.press(MOD, "Next")
    await h.press(MOD, "Reject")
    await h.press(MOD, "Back")
    await h.press(MOD, "Skip")
    await h.press(MOD, "Next")
    await h.press(MOD, "Ban")
    await h.press(MOD, "Back")
    await h.press(MOD, "Escalate")
    await h.say(MOD, "📊 Stats")
    await h.run_job(poster_job)
    await h.say(MOD, "/takedown 1 test")
    await h.say(MOD, "/words")
    await h.say(MOD, "/block badword")
    await h.say(MOD, "/top")
    assert _dead_ends(h, MOD) == [], _dead_ends(h, MOD)


async def test_admin_journey_has_no_dead_ends(h):
    h.add_staff(ADMIN, "admin")
    await h.say(ADMIN, "/start")
    await h.say(ADMIN, "⚙️ Admin")
    for path in (("Staff", "Invite moderator"), ("Back to staff",), ("Back",), ("Categories", "Add category"), ("Back",), ("Back",),
                 ("Packages", "Basic post", "Price")):
        for label in path:
            await h.press(ADMIN, label)
    await h.say(ADMIN, "abc")  # invalid price
    await h.press(ADMIN, "Back")
    await h.say(ADMIN, "/settings")
    await h.press(ADMIN, "Moderation")
    await h.press(ADMIN, "ban_days")
    await h.press(ADMIN, "Cancel")
    await h.say(ADMIN, "/ads")
    await h.say(ADMIN, "/revenue")
    await h.say(ADMIN, "/staff")
    await h.say(ADMIN, "/audit")
    await h.say(ADMIN, "/categories")
    assert _dead_ends(h, ADMIN) == [], _dead_ends(h, ADMIN)


async def test_owner_journey_has_no_dead_ends(h):
    await h.say(OWNER, "/start")
    await h.say(OWNER, "⚙️ Admin")
    await h.press(OWNER, "Payout accounts")
    await h.press(OWNER, "Add account")
    await h.say(OWNER, "nonsense")
    await h.press(OWNER, "Back")
    await h.press(OWNER, "Health")
    await h.say(OWNER, "/refunds")
    await h.say(OWNER, "/payout_accounts")
    await h.say(OWNER, "/mediakit")
    assert _dead_ends(h, OWNER) == [], _dead_ends(h, OWNER)
