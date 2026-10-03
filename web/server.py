"""aiohttp server: Telegram webhook, Verify.et webhook, health."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

from aiohttp import web
from telegram import Update

import db

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000
WEBHOOK_MAX_SKEW_SEC = 300


# ---------------------------------------------------------------------------
# Verify.et signature (pure)
# ---------------------------------------------------------------------------

def verify_signature(secret: str, timestamp: str | None, raw_body: bytes, header: str | None, now: float | None = None) -> bool:
    """HMAC-SHA256(secret, f"{timestamp}.{body}") compared constant-time against
    each `sha256=<hex>` value in the header (two values during rotation)."""
    if not secret:
        return True  # signatures not configured
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
    env = request.app["env"]
    if env.tg_webhook_secret and not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), env.tg_webhook_secret
    ):
        return web.Response(status=403)
    if request.content_length and request.content_length > MAX_BODY:
        return web.Response(status=413)
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.Response(status=400)
    application = request.app["application"]
    update = Update.de_json(data, application.bot)
    if update is None:
        return web.Response(status=400)
    await application.update_queue.put(update)
    return web.Response(status=200)


async def verify_webhook(request: web.Request) -> web.Response:
    env = request.app["env"]
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
    delivery_id = request.headers.get("X-Webhook-Delivery-Id") or f"{payload.get('requestId')}:{payload.get('event')}"
    try:
        db.execute("INSERT INTO webhook_events(delivery_id, received_at) VALUES (?,?)", (delivery_id[:128], db.now()))
    except db.sqlite3.IntegrityError:
        return web.Response(status=204)  # duplicate delivery
    handler = request.app.get("verify_handler")
    if handler:
        asyncio.get_running_loop().create_task(_safe(handler, payload, request.app["application"]))
    return web.Response(status=204)


async def _safe(handler, payload, application) -> None:
    try:
        await handler(payload, application)
    except Exception:  # noqa: BLE001
        log.exception("verify webhook processing failed")


async def healthz(request: web.Request) -> web.Response:
    now = db.now()
    last_post = int(db.scalar("SELECT v FROM meta WHERE k='last_post_at'", default="0") or 0)
    last_update = request.app.get("last_update_at", 0)
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
    app["env"] = env
    app["application"] = application
    app["verify_handler"] = verify_handler
    app.router.add_post(env.webhook_path, telegram_webhook)
    app.router.add_post("/webhooks/verify", verify_webhook)
    app.router.add_get("/healthz", healthz)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
