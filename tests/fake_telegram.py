"""In-process fake Telegram Bot API.

Plugs into PTB as a `BaseRequest`, so the real Application, handlers,
ConversationHandlers and callback signing all run unchanged. Every API
call is recorded so tests can assert on what the bot sent.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import Update
from telegram.request import BaseRequest, RequestData

BOT_ID = 999_000
BOT_USERNAME = "aastu_confess_test_bot"


@dataclass
class Sent:
    method: str
    chat_id: int | None
    message_id: int | None
    text: str
    markup: dict | None
    params: dict = field(default_factory=dict)

    def buttons(self) -> list[dict]:
        if not self.markup:
            return []
        return [b for row in self.markup.get("inline_keyboard", []) for b in row]

    def reply_labels(self) -> list[str]:
        if not self.markup:
            return []
        return [b["text"] for row in self.markup.get("keyboard", []) for b in row]

    def button(self, label_substr: str) -> dict | None:
        for b in self.buttons():
            if label_substr in b.get("text", ""):
                return b
        return None

    def callback(self, label_substr: str) -> str:
        b = self.button(label_substr)
        assert b and "callback_data" in b, f"no callback button containing {label_substr!r} in {[x['text'] for x in self.buttons()]}"
        return b["callback_data"]


class FakeRequest(BaseRequest):
    """Answers Bot API calls locally and records them."""

    def __init__(self) -> None:
        self.calls: list[Sent] = []
        self.raw_calls: list[tuple[str, dict]] = []
        self._next_msg_id = 100
        self.channel_username = "aastu_confessions_test"
        self.member_count = 1234
        self.fail_chats: set[int] = set()  # chats where sendMessage raises Forbidden
        self.deleted: list[tuple[int, int]] = []
        self.pinned: list[tuple[int, int]] = []
        self.topics: list[str] = []

    async def initialize(self) -> None:  # noqa: D401
        return None

    async def shutdown(self) -> None:
        return None

    # -- helpers --------------------------------------------------------------
    def _msg(self, chat_id: int, text: str = "", **extra) -> dict:
        self._next_msg_id += 1
        return {
            "message_id": self._next_msg_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "channel"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": BOT_USERNAME},
            "text": text,
            **extra,
        }

    def _ok(self, result: Any) -> tuple[int, bytes]:
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def _err(self, code: int, desc: str) -> tuple[int, bytes]:
        return code, json.dumps({"ok": False, "error_code": code, "description": desc}).encode()

    def sent_to(self, chat_id: int) -> list[Sent]:
        return [c for c in self.calls if c.chat_id == chat_id and c.method in ("sendMessage", "sendPhoto", "sendDocument")]

    def last(self, chat_id: int) -> Sent:
        items = self.sent_to(chat_id)
        assert items, f"nothing sent to {chat_id}"
        return items[-1]

    def texts(self, chat_id: int) -> list[str]:
        return [c.text for c in self.sent_to(chat_id)]

    def clear(self) -> None:
        self.calls.clear()
        self.raw_calls.clear()

    # -- main dispatch --------------------------------------------------------
    async def do_request(self, url: str, method: str, request_data: RequestData | None = None,
                         read_timeout=None, write_timeout=None, connect_timeout=None, pool_timeout=None) -> tuple[int, bytes]:
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data and request_data.multipart_data:
            for field, item in request_data.multipart_data.items():
                params.setdefault("filename", getattr(item, "filename", None) or (item[0] if isinstance(item, tuple) else None))
        self.raw_calls.append((api, params))
        chat_id = _int(params.get("chat_id"))
        markup = params.get("reply_markup")
        if isinstance(markup, str):
            markup = json.loads(markup)

        if api == "getMe":
            return self._ok({"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": BOT_USERNAME,
                             "can_join_groups": True, "can_read_all_group_messages": False, "supports_inline_queries": False})
        if api in ("setMyCommands", "deleteWebhook", "setWebhook", "pinChatMessage", "unpinChatMessage", "answerCallbackQuery"):
            if api == "pinChatMessage":
                self.pinned.append((chat_id, _int(params.get("message_id"))))
            self.calls.append(Sent(api, chat_id, _int(params.get("message_id")), str(params.get("text", "")), None, params))
            return self._ok(True)
        if api == "getChat":
            return self._ok({"id": chat_id, "type": "channel", "title": "Test", "username": self.channel_username})
        if api == "getChatMemberCount":
            return self._ok(self.member_count)
        if api == "createForumTopic":
            self.topics.append(str(params.get("name")))
            return self._ok({"message_thread_id": 100 + len(self.topics), "name": params.get("name"), "icon_color": 7322096})
        if api == "deleteMessage":
            self.deleted.append((chat_id, _int(params.get("message_id"))))
            self.calls.append(Sent(api, chat_id, _int(params.get("message_id")), "", None, params))
            return self._ok(True)
        if api in ("sendMessage", "sendPhoto", "sendDocument"):
            if chat_id in self.fail_chats:
                return self._err(403, "Forbidden: bot was blocked by the user")
            text = str(params.get("text") or params.get("caption") or "")
            # Telegram echoes only inline keyboards back on the Message object
            echo = markup if markup and "inline_keyboard" in markup else None
            msg = self._msg(chat_id, text, reply_markup=echo) if echo else self._msg(chat_id, text)
            if api == "sendPhoto":
                msg["photo"] = [{"file_id": str(params.get("photo")), "file_unique_id": "u", "width": 1, "height": 1}]
                msg["caption"] = text
            if api == "sendDocument":
                msg["document"] = {"file_id": "doc", "file_unique_id": "d", "file_name": params.get("filename", "f")}
            self.calls.append(Sent(api, chat_id, msg["message_id"], text, markup, params))
            return self._ok(msg)
        if api in ("editMessageText", "editMessageReplyMarkup", "editMessageCaption"):
            text = str(params.get("text") or params.get("caption") or "")
            mid = _int(params.get("message_id"))
            # mutate the original so tests see the current state of that message
            for c in self.calls:
                if c.chat_id == chat_id and c.message_id == mid and c.method in ("sendMessage", "sendPhoto"):
                    c.markup = markup
                    if api != "editMessageReplyMarkup":
                        c.text = text
            self.calls.append(Sent(api, chat_id, mid, text, markup, params))
            return self._ok(self._msg(chat_id or 0, text))
        if api == "getFile":
            return self._ok({"file_id": params.get("file_id"), "file_unique_id": "x", "file_path": "x"})
        return self._err(400, f"fake: unsupported method {api}")

    async def retrieve(self, url: str, read_timeout=None, write_timeout=None, connect_timeout=None, pool_timeout=None) -> bytes:
        return b""


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Update factories
# ---------------------------------------------------------------------------

_update_id = 0
_user_msg_id = 5000


def _next_ids() -> tuple[int, int]:
    global _update_id, _user_msg_id
    _update_id += 1
    _user_msg_id += 1
    return _update_id, _user_msg_id


def _user(uid: int, username: str | None = None) -> dict:
    return {"id": uid, "is_bot": False, "first_name": f"U{uid}", **({"username": username} if username else {})}


def text_update(bot, uid: int, text: str, chat_id: int | None = None, username: str | None = None) -> Update:
    upd_id, msg_id = _next_ids()
    chat_id = chat_id or uid
    chat = {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}
    entities = []
    if text.startswith("/"):
        cmd = text.split()[0]
        entities = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
    data = {"update_id": upd_id, "message": {"message_id": msg_id, "date": int(time.time()), "chat": chat,
                                              "from": _user(uid, username), "text": text, "entities": entities}}
    return Update.de_json(data, bot)


def photo_update(bot, uid: int, file_id: str = "PHOTO1") -> Update:
    upd_id, msg_id = _next_ids()
    data = {"update_id": upd_id, "message": {"message_id": msg_id, "date": int(time.time()),
                                              "chat": {"id": uid, "type": "private"}, "from": _user(uid),
                                              "photo": [{"file_id": file_id, "file_unique_id": "u1", "width": 10, "height": 10}]}}
    return Update.de_json(data, bot)


def document_update(bot, uid: int, caption: str, file_id: str = "DOC1", size: int = 100) -> Update:
    upd_id, msg_id = _next_ids()
    data = {"update_id": upd_id, "message": {"message_id": msg_id, "date": int(time.time()),
                                              "chat": {"id": uid, "type": "private"}, "from": _user(uid),
                                              "caption": caption, "document": {"file_id": file_id, "file_unique_id": "d1", "file_size": size}}}
    return Update.de_json(data, bot)


def callback_update(bot, uid: int, data: str, message: Sent, chat_id: int | None = None) -> Update:
    upd_id, _ = _next_ids()
    chat_id = chat_id if chat_id is not None else (message.chat_id or uid)
    msg = {"message_id": message.message_id, "date": int(time.time()),
           "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
           "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}, "text": message.text or "x"}
    if message.markup:
        msg["reply_markup"] = message.markup
    if message.method == "sendPhoto":
        msg["caption"] = message.text
        msg["photo"] = [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}]
    payload = {"update_id": upd_id, "callback_query": {"id": str(upd_id), "from": _user(uid), "chat_instance": "ci",
                                                      "data": data, "message": msg}}
    return Update.de_json(payload, bot)
