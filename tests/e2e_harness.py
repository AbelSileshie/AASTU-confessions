"""E2E harness: real PTB Application + fake Telegram API + fake Verify.et."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest_asyncio
from telegram.ext import ApplicationBuilder, CallbackContext, ExtBot

import config
import db
import security
from payments import VerifyClient
from tests.fake_telegram import FakeRequest, callback_update, document_update, photo_update, text_update

OWNER = 1000
CHANNEL = -1001
ADMIN_GROUP = -1002
BACKUP = -1003


class FakeVerify:
    """Programmable Verify.et. Set `responder` to a function (body, headers) -> (status, json)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responder = self.success
        self.status_payload: dict | None = None

    def transport(self) -> httpx.MockTransport:
        async def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == "/api/verify":
                body = json.loads(req.content or b"{}")
                self.requests.append({"body": body, "headers": dict(req.headers), "params": dict(req.url.params)})
                status, payload = self.responder(body, dict(req.headers))
                return httpx.Response(status, json=payload)
            if req.method == "GET" and req.url.path.startswith("/api/verify/"):
                return httpx.Response(200, json=self.status_payload or {"success": True, "data": {"processingStatus": "running"}})
            return httpx.Response(404, json={"success": False})

        return httpx.MockTransport(handler)

    # ---- canned responders ----
    @staticmethod
    def result(verified=True, amount=800, matched=True, ambiguous=False, confirmed_before=False, ts="2026-10-03T10:00:00.000Z",
               sender="Abebe Bikila", receiver="Kuru Labs", bank="telebirr", ref="ABC123XYZ"):
        return {
            "bank": bank, "status": "success" if verified else "failed", "verified": verified, "amount": amount, "currency": "ETB",
            "senderName": sender, "receiverName": receiver, "receiverAccount": "09****0897", "referenceNumber": ref, "timestamp": ts,
            "confirmationHistory": {"confirmedBefore": confirmed_before, "confirmationCount": 2 if confirmed_before else 1},
            "settlementAccountMatch": {"matched": matched, "ambiguous": ambiguous, "reason": "receiver_account_exact_match" if matched else "candidate_account_mismatch"},
        }

    @classmethod
    def success(cls, body, headers):
        return 200, {"success": True, "data": [cls.result()], "requestId": "req-1",
                     "verification": {"processingStatus": "completed", "verified": True}}

    @classmethod
    def with_result(cls, **kw):
        def r(body, headers):
            return 200, {"success": True, "data": [cls.result(**kw)], "requestId": "req-x",
                         "verification": {"processingStatus": "completed"}}
        return r

    @staticmethod
    def queued(body, headers):
        return 202, {"success": True, "data": [], "requestId": "req-q", "statusUrl": "/api/verify/req-q",
                     "verification": {"processingStatus": "queued"}}

    @staticmethod
    def no_credits(body, headers):
        return 402, {"success": False, "code": "INSUFFICIENT_CREDITS"}

    @staticmethod
    def invalid(body, headers):
        return 422, {"success": False, "message": "Invalid bank-specific payload"}

    @staticmethod
    def rate_limited(body, headers):
        return 429, {"success": False}


class Harness:
    def __init__(self, app, req: FakeRequest, verify: FakeVerify, env) -> None:
        self.app = app
        self.tg = req
        self.verify = verify
        self.env = env

    @property
    def bot(self):
        return self.app.bot

    async def say(self, uid: int, text: str, chat_id: int | None = None, username: str | None = None):
        await self.app.process_update(text_update(self.bot, uid, text, chat_id, username))
        await self._settle()

    async def _settle(self):
        import asyncio
        for _ in range(3):
            await asyncio.sleep(0)

    async def photo(self, uid: int, file_id: str = "PHOTO1"):
        await self.app.process_update(photo_update(self.bot, uid, file_id))

    async def document(self, uid: int, caption: str, size: int = 100):
        await self.app.process_update(document_update(self.bot, uid, caption, size=size))

    async def press(self, uid: int, label: str, message=None, chat_id: int | None = None):
        """Press the button whose label contains `label` on `message` (default: last message to uid)."""
        message = message or self.tg.last(chat_id if chat_id is not None else uid)
        await self.app.process_update(callback_update(self.bot, uid, message.callback(label), message, chat_id))

    async def press_raw(self, uid: int, data: str, message=None):
        message = message or self.tg.last(uid)
        await self.app.process_update(callback_update(self.bot, uid, data, message))

    def last(self, chat_id: int):
        return self.tg.last(chat_id)

    def texts(self, chat_id: int) -> list[str]:
        return self.tg.texts(chat_id)

    def context(self, job_data: dict | None = None) -> CallbackContext:
        ctx = CallbackContext(self.app)
        if job_data is not None:
            ctx.job = SimpleNamespace(data=job_data, name='test')
        return ctx

    async def run_job(self, job_fn, data: dict | None = None):
        await job_fn(self.context(data))

    # ---- common setup ----
    def add_staff(self, uid: int, role: str = "moderator") -> None:
        db.execute("INSERT INTO staff(user_id, role, added_at) VALUES (?,?,?)", (uid, role, db.now()))

    def add_payout(self, bank="telebirr", account="0939080897", holder="Kuru Labs") -> int:
        return db.execute("INSERT INTO payout_accounts(bank, account, holder) VALUES (?,?,?)", (bank, account, holder))

    def set(self, key: str, value) -> None:
        db.execute("UPDATE settings SET value=? WHERE key=?", (str(value), key))
        config.invalidate(key)

    async def submit_confession(self, uid: int, text: str, category_label: str = "Campus") -> None:
        await self.say(uid, "/start")
        await self.press(uid, "Confess")
        await self.press(uid, category_label)
        await self.say(uid, text)
        await self.press(uid, "Submit")


@pytest_asyncio.fixture
async def h(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setenv("PUBLIC_URL", "https://confess.test")
    monkeypatch.setenv("VERIFY_API_KEY", "VERIFY_BANK_ET_test")
    monkeypatch.setenv("VERIFY_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("TG_WEBHOOK_SECRET", "tgsecret")
    env = config.load_env()
    db.reset_for_tests(env.db_path)
    config.invalidate()
    security.configure(env.hmac_secret, env.fernet_key, env.owner_id)

    import app as appmod

    req = FakeRequest()
    bot = ExtBot(token=env.bot_token, request=req, get_updates_request=FakeRequest())
    application = ApplicationBuilder().bot(bot).post_init(appmod.post_init).concurrent_updates(False).build()
    application.bot_data["env"] = env
    application.add_error_handler(appmod.on_error)
    appmod.register_modules(application)

    verify = FakeVerify()
    application.bot_data["verify_client"] = VerifyClient(env.verify_base_url, env.verify_api_key,
                                                         client=httpx.AsyncClient(transport=verify.transport()))
    await application.initialize()
    await application.start()
    req.clear()
    harness = Harness(application, req, verify, env)
    yield harness
    await application.stop()
    await application.shutdown()
