"""E2E for the aiohttp layer: Telegram webhook auth, Verify.et signature/idempotency, /healthz."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

import db
from tests.e2e_harness import h  # noqa: F401
from web.server import build_app, verify_signature

pytestmark = pytest.mark.asyncio


def _sig(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


class TestSignaturePure:
    def test_valid(self):
        body = b'{"a":1}'
        ts = str(time.time())
        assert verify_signature("s", ts, body, _sig("s", ts, body))

    def test_rotation_two_values(self):
        body = b"x"
        ts = str(time.time())
        header = _sig("old", ts, body) + "," + _sig("new", ts, body)
        assert verify_signature("new", ts, body, header)

    def test_bad_secret(self):
        body = b"x"
        ts = str(time.time())
        assert not verify_signature("s", ts, body, _sig("other", ts, body))

    def test_stale_timestamp(self):
        body = b"x"
        ts = str(time.time() - 1000)
        assert not verify_signature("s", ts, body, _sig("s", ts, body))

    def test_missing_headers(self):
        assert not verify_signature("s", None, b"x", None)
        assert not verify_signature("s", "abc", b"x", "sha256=00")

    def test_no_secret_configured_denies(self):
        assert not verify_signature("", None, b"x", None)
        assert not verify_signature("", str(time.time()), b"x", "sha256=abc")


@pytest.fixture
async def client(h):
    received = []

    async def handler(payload, application):
        received.append(payload)

    app = build_app(h.env, h.app, verify_handler=handler)
    app["received"] = received
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True and body["queue"] == 0


async def test_unknown_paths_404(client):
    assert (await client.get("/")).status == 404
    assert (await client.get("/admin")).status == 404
    assert (await client.post("/tg/wrongpath", json={})).status == 404


async def test_telegram_webhook_requires_secret(client, h):
    update = {"update_id": 1, "message": {"message_id": 1, "date": 1, "chat": {"id": 55, "type": "private"},
                                          "from": {"id": 55, "is_bot": False, "first_name": "x"}, "text": "/start",
                                          "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}
    r = await client.post(h.env.webhook_path, json=update)
    assert r.status == 403
    r = await client.post(h.env.webhook_path, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert r.status == 403
    r = await client.post(h.env.webhook_path, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"})
    assert r.status == 200
    import asyncio
    await asyncio.sleep(0.1)  # running app consumes the queue and handles /start
    assert h.tg.sent_to(55)


async def test_telegram_webhook_rejects_garbage(client, h):
    hdr = {"X-Telegram-Bot-Api-Secret-Token": "tgsecret"}
    assert (await client.post(h.env.webhook_path, data=b"not json", headers=hdr)).status == 400
    assert (await client.post(h.env.webhook_path, json={"foo": "bar"}, headers=hdr)).status == 400
    assert (await client.post(h.env.webhook_path, json=[1, 2], headers=hdr)).status == 400
    big = b"x" * 1_100_000
    assert (await client.post(h.env.webhook_path, data=big, headers=hdr)).status == 413


async def test_verify_webhook_signature_enforced(client):
    body = json.dumps({"event": "verification.completed", "requestId": "r1", "data": {}}).encode()
    ts = str(time.time())
    assert (await client.post("/webhooks/verify", data=body)).status == 401
    bad = {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": "sha256=deadbeef"}
    assert (await client.post("/webhooks/verify", data=body, headers=bad)).status == 401
    good = {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": _sig("whsec_test", ts, body), "X-Webhook-Delivery-Id": "d1"}
    assert (await client.post("/webhooks/verify", data=body, headers=good)).status == 204
    # tampered body with the same signature
    tampered = body.replace(b'"r1"', b'"r2"')
    assert (await client.post("/webhooks/verify", data=tampered, headers=good)).status == 401


async def test_verify_webhook_test_ping_and_idempotency(client):
    body = b'{"event":"test"}'
    ts = str(time.time())
    hdr = {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": _sig("whsec_test", ts, body), "X-Webhook-Test": "true"}
    assert (await client.post("/webhooks/verify", data=body, headers=hdr)).status == 204
    assert db.scalar("SELECT count(*) FROM webhook_events") == 0
    body = json.dumps({"event": "verification.completed", "requestId": "r9", "data": {}}).encode()
    hdr = {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": _sig("whsec_test", ts, body), "X-Webhook-Delivery-Id": "dup"}
    assert (await client.post("/webhooks/verify", data=body, headers=hdr)).status == 204
    assert (await client.post("/webhooks/verify", data=body, headers=hdr)).status == 204
    assert db.scalar("SELECT count(*) FROM webhook_events") == 1
    import asyncio
    await asyncio.sleep(0.05)
    assert len(client.server.app["received"]) == 1
