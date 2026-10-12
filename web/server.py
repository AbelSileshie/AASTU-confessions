"""aiohttp server: Telegram webhook, Verify.et webhook, health."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from aiohttp import web
from telegram import Update
from telegram.ext import Application

import db
from config import Env

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000
WEBHOOK_MAX_SKEW_SEC = 300
ENV_KEY: web.AppKey[Env] = web.AppKey("env")
APPLICATION_KEY: web.AppKey[Application] = web.AppKey("application")
VERIFY_HANDLER_KEY: web.AppKey[Any] = web.AppKey("verify_handler")
STATE_KEY: web.AppKey[dict[str, int]] = web.AppKey("state")


# ---------------------------------------------------------------------------
# Verify.et signature (pure)
# ---------------------------------------------------------------------------

def verify_signature(secret: str, timestamp: str | None, raw_body: bytes, header: str | None, now: float | None = None) -> bool:
    """HMAC-SHA256(secret, f"{timestamp}.{body}") compared constant-time against
    each `sha256=<hex>` value in the header (two values during rotation)."""
    if not secret:
        return False  # fail closed: unsigned webhooks are never trusted
    if not timestamp or not header:
        return False
    try:
        ts = float(timestamp)
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > WEBHOOK_MAX_SKEW_SEC:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    candidates = [part.strip().split("=", 1)[-1] for part in header.split(",") if part.strip()]
    return any(hmac.compare_digest(expected, c) for c in candidates)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def telegram_webhook(request: web.Request) -> web.Response:
    env = request.app[ENV_KEY]
    if not env.tg_webhook_secret or not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), env.tg_webhook_secret
    ):
        return web.Response(status=403)  # fail closed: no secret configured → nothing accepted
    if request.content_length and request.content_length > MAX_BODY:
        return web.Response(status=413)
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.Response(status=400)
    if not isinstance(data, dict) or not isinstance(data.get("update_id"), int):
        return web.Response(status=400)
    application = request.app[APPLICATION_KEY]
    try:
        update = Update.de_json(data, application.bot)
    except (KeyError, TypeError, ValueError):
        return web.Response(status=400)
    if update is None:
        return web.Response(status=400)
    request.app[STATE_KEY]["last_update_at"] = db.now()
    await application.update_queue.put(update)
    return web.Response(status=200)


async def verify_webhook(request: web.Request) -> web.Response:
    env = request.app[ENV_KEY]
    if request.content_length and request.content_length > MAX_BODY:
        return web.Response(status=413)
    raw = await request.read()
    if not verify_signature(env.verify_webhook_secret, request.headers.get("X-Webhook-Timestamp"), raw,
                            request.headers.get("X-Webhook-Signature")):
        return web.Response(status=401)
    if request.headers.get("X-Webhook-Test", "").lower() == "true":
        return web.Response(status=204)
    try:
        payload = json.loads(raw)
    except ValueError:
        return web.Response(status=400)
    if not isinstance(payload, dict):
        return web.Response(status=400)
    delivery_id = request.headers.get("X-Webhook-Delivery-Id") or f"{payload.get('requestId')}:{payload.get('event')}"
    delivery_id = delivery_id[:128]
    if db.one("SELECT 1 FROM webhook_events WHERE delivery_id=?", (delivery_id,)):
        return web.Response(status=204)
    handler = request.app.get(VERIFY_HANDLER_KEY)
    if handler:
        try:
            handled = await handler(payload, request.app[APPLICATION_KEY])
        except Exception:
            log.exception("verify webhook processing failed")
            return web.Response(status=500)
        if handled is False:
            return web.Response(status=503)
    try:
        db.execute("INSERT INTO webhook_events(delivery_id, received_at) VALUES (?,?)", (delivery_id, db.now()))
    except db.sqlite3.IntegrityError:
        pass
    return web.Response(status=204)


async def healthz(request: web.Request) -> web.Response:
    now = db.now()
    last_post = int(db.scalar("SELECT v FROM meta WHERE k='last_post_at'", default="0") or 0)
    last_update = request.app[STATE_KEY].get("last_update_at", 0)
    body = {
        "ok": True,
        "queue": db.scalar("SELECT count(*) FROM submissions", default=0),
        "outbox": db.scalar("SELECT count(*) FROM outbox", default=0),
        "last_post_age": now - last_post if last_post else None,
        "last_update_age": now - last_update if last_update else None,
    }
    return web.json_response(body)


def build_app(env, application, verify_handler=None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app[ENV_KEY] = env
    app[APPLICATION_KEY] = application
    app[VERIFY_HANDLER_KEY] = verify_handler
    app[STATE_KEY] = {}
    app.router.add_post(env.webhook_path, telegram_webhook)
    app.router.add_post("/webhooks/verify", verify_webhook)
    app.router.add_get("/healthz", healthz)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
