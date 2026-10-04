"""Links, formatting (UTF-16 entities), names-not-ids, newline-safe settings."""
from __future__ import annotations

import json
import re

import pytest
from telegram import MessageEntity

import db
from common import channel_info, normalize_newlines, staff_name
from fmt import Rich, card, utf16_len
from jobs.poster import format_ad, format_confession, poster_job
from tests.e2e_harness import ADMIN_GROUP, CHANNEL, OWNER, h  # noqa: F401

pytestmark = pytest.mark.asyncio

S, S2, MOD, ADMIN, ADV = 501, 502, 701, 702, 801
TEXT = "A confession long enough to be posted for the link and formatting tests to run."


async def _post(h, text=TEXT):
    h.add_staff(MOD)
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.submit_confession(S, text)
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    return [c for c in h.tg.calls if c.chat_id == CHANNEL and c.method == "sendMessage"][-1]


# ---------------------------------------------------------------------------
# Rich builder (pure)
# ---------------------------------------------------------------------------

class TestRich:
    def test_utf16_offsets_with_emoji(self):
        text, ents = Rich().text("😳 hi ").bold("Title").build()
        # 😳 is 2 UTF-16 units but 1 Python char
        assert ents[0].offset == utf16_len("😳 hi ") == 6 and ents[0].length == 5

    def test_amharic_plus_emoji(self):
        body = "ሰላም 😳 world"
        text, ents = Rich().bold("H").nl().quote(body).build()
        assert ents[1].offset == 2 and ents[1].length == utf16_len(body) == len(body) + 1

    def test_truncation_never_leaves_dangling_entities(self):
        text, ents = Rich().bold("Header").nl().quote("x" * 5000).build(limit=100)
        assert utf16_len(text) <= 100
        for e in ents:
            assert e.offset + e.length <= utf16_len(text)

    def test_link_requires_https(self):
        _, ents = Rich().link("x", "javascript:alert(1)").build()
        assert ents == []
        _, ents = Rich().link("x", "https://ok.example").build()
        assert ents[0].type == MessageEntity.TEXT_LINK

    def test_card_shape(self):
        text, ents = card("T", [("A", "1"), ("B", "2")], "body", hint="hint").build()
        assert text.startswith("T\nA: 1   B: 2\n\nbody\n\nhint")
        kinds = [e.type for e in ents]
        assert kinds.count(MessageEntity.BOLD) == 3 and MessageEntity.BLOCKQUOTE in kinds and MessageEntity.ITALIC in kinds


def test_format_ad_footer_offset_correct_with_emoji(h):
    text, ents = format_ad("☕ Great coffee 😍 deal", "mybot")
    link = [e for e in ents if e.type == MessageEntity.TEXT_LINK][0]
    start = link.offset
    # decode via UTF-16 to check the entity covers exactly the footer label
    u16 = text.encode("utf-16-le")
    assert u16[start * 2:(start + link.length) * 2].decode("utf-16-le") == "Advertise with us ↗"
    assert ents[0].type == MessageEntity.BOLD and text.startswith("📢 Sponsored")


def test_format_confession_header_and_long_body(h):
    text, ents = format_confession(7, "x" * 800, "#Rant", "😤 Rant")
    assert text.startswith("😤 Confession #7\n\n") and ents[0].type == MessageEntity.BOLD
    assert any(e.type == MessageEntity.EXPANDABLE_BLOCKQUOTE for e in ents)
    text, ents = format_confession(8, "short", "#Rant", "😤 Rant")
    assert all(e.type != MessageEntity.EXPANDABLE_BLOCKQUOTE for e in ents)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

async def test_public_channel_links(h):
    posted = await _post(h)
    live = [c for c in h.tg.sent_to(S) if "live as" in c.text][-1]
    assert live.button("Open in channel")["url"] == f"https://t.me/aastu_confessions_test/{posted.message_id}"
    assert live.button("Comments")["url"] == f"https://t.me/aastu_confess_test_bot?start=c_1"
    assert live.button("Join") is None


async def test_private_channel_gets_join_button_and_switches_when_public_later(h):
    h.tg.channel_username = None
    await channel_info(h.context(), force=True)
    posted = await _post(h)
    live = [c for c in h.tg.sent_to(S) if "live as" in c.text][-1]
    assert live.button("Open in channel")["url"] == f"https://t.me/c/1/{posted.message_id}"
    assert live.button("Join")["url"] == "https://t.me/+InViTeLiNk123"
    # channel gets a username later: refresh picks it up without restart
    h.tg.channel_username = "now_public"
    db.execute("UPDATE meta SET v=json_set(v, '$.checked_at', 0) WHERE k='channel_info'")
    h.app.bot_data["channel_info"]["checked_at"] = 0
    await channel_info(h.context())
    await h.submit_confession(S2, "Second confession to check the refreshed public link rendering here.")
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    live2 = [c for c in h.tg.sent_to(S2) if "live as" in c.text][-1]
    assert live2.button("Open in channel")["url"].startswith("https://t.me/now_public/")
    assert live2.button("Join") is None


async def test_deep_link_case_insensitive(h):
    await _post(h)
    await h.say(S2, "/start C_1")
    assert "Comments on #1" in h.last(S2).text


async def test_channel_button_in_channel_post_is_bot_deep_link(h):
    posted = await _post(h)
    assert posted.button("Comments")["url"] == "https://t.me/aastu_confess_test_bot?start=c_1"


# ---------------------------------------------------------------------------
# Names, never ids
# ---------------------------------------------------------------------------

def test_staff_name_never_returns_raw_id(h):
    db.execute("INSERT INTO staff(user_id, role, display, added_at) VALUES (7001, 'moderator', NULL, 0)")
    assert "7001" not in staff_name(7001) and staff_name(7001).startswith("Moderator")
    assert staff_name(0) == "System" and staff_name(None) == "System"
    db.execute("UPDATE staff SET display='Sara' WHERE user_id=7001")
    assert staff_name(7001) == "Sara"


async def test_no_raw_staff_ids_in_human_screens(h):
    h.add_staff(MOD)
    h.add_staff(ADMIN, "admin")
    h.set("submit_cooldown_sec", 0)
    h.set("post_interval_sec", 0)
    await h.submit_confession(S, "Someone has a video of me and is threatening to share it with the class.")
    await h.say(MOD, "/next")
    await h.press(MOD, "Approve")
    await h.run_job(poster_job)
    await h.say(MOD, "📊 Stats")
    await h.say(ADMIN, "/admin")
    await h.press(ADMIN, "Staff")
    await h.press(ADMIN, "Moderator")
    await h.press(ADMIN, "Audit")
    await h.say(ADMIN, "/staff list")
    texts = [c.text for c in h.tg.calls if c.chat_id in (MOD, ADMIN, ADMIN_GROUP) and c.method in ("sendMessage", "editMessageText")]
    offenders = [t for t in texts if re.search(rf"\b({MOD}|{ADMIN})\b", t) and "(id " not in t]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Settings texts: newlines, preview, rendering
# ---------------------------------------------------------------------------

def test_normalize_newlines_repairs_legacy():
    assert normalize_newlines("Rules:\\n1. a\\n\\n\\n\\n2. b") == "Rules:\n1. a\n\n2. b"
    assert normalize_newlines("a\r\nb") == "a\nb"


def test_seeded_rules_have_real_newlines_and_owner_wording(h):
    rules = db.scalar("SELECT value FROM settings WHERE key='rules_text'")
    assert "\\n" not in rules and rules.count("\n") >= 5
    assert "No real names, phone numbers or usernames." in rules and "Request takedown" in rules


async def test_rules_render_with_bold_title(h):
    await h.say(S, "📜 Rules")
    msg = h.last(S)
    assert msg.text.startswith("📜 Rules\n\n1. No real names")
    ents = json.loads(msg.params["entities"]) if isinstance(msg.params["entities"], str) else msg.params["entities"]
    assert ents[0]["type"] == "bold" and ents[0]["offset"] == 0


async def test_set_keeps_newlines_and_shows_preview(h):
    await h.say(OWNER, "/set welcome_text 👋 Hello\n\nLine two\nLine three")
    assert db.scalar("SELECT value FROM settings WHERE key='welcome_text'") == "👋 Hello\n\nLine two\nLine three"
    msgs = h.tg.sent_to(OWNER)
    assert "saved. Preview" in msgs[-2].text and msgs[-1].text == "👋 Hello\n\nLine two\nLine three"
    await h.say(S, "/start")
    assert h.last(S).text.startswith("👋 Hello\n\nLine two")


async def test_settings_menu_shows_current_text_as_quote(h):
    await h.say(OWNER, "/settings")
    await h.press(OWNER, "Texts")
    menu = h.last(OWNER)
    btn = [b for b in menu.buttons() if b["text"].startswith("rules_text")][0]
    await h.press_raw(OWNER, btn["callback_data"], message=menu)
    msg = h.last(OWNER)
    assert "Line breaks are kept" in msg.text and "1. No real names" in msg.text
    await h.say(OWNER, "Short\\nwith literal escape")
    assert db.scalar("SELECT value FROM settings WHERE key='rules_text'") == "Short\nwith literal escape"


async def test_post_rules_matches_rules_screen(h):
    await h.say(OWNER, "/post_rules")
    posted = [c for c in h.tg.calls if c.chat_id == CHANNEL and c.method == "sendMessage"][-1]
    assert posted.text.startswith("📜 Rules\n\n") and posted.params.get("entities")
