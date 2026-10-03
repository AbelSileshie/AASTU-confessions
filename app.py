"""Entry point: builds the Telegram Application, registers modules and jobs,
runs in polling (dev) or webhook (production) mode alongside the aiohttp server."""
from __future__ import annotations

import asyncio
import importlib
import logging
import signal
import sys
import warnings
from datetime import time as dtime, timezone

from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes
from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", category=PTBUserWarning, message=".*per_message=False.*")

import db
import security
from config import load_env
from jobs.backup import backup_job
from jobs.housekeeping import counter_refresh_job, daily_purge_job, housekeeping_job
from jobs.poster import poster_job, unpin_job
from modules import MODULES
from payments import handle_verify_webhook
from security import redact
from web.server import build_app, start_server

VERSION = "1.0.0"


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        return redact(super().format(record))


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log = logging.getLogger("app")
    log.error("handler error: %s", type(context.error).__name__)
    log.debug("handler traceback", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer()
        except TelegramError:
            pass


def register_modules(app: Application) -> None:
    for name in MODULES:
        importlib.import_module(name).register(app)


def register_jobs(app: Application) -> None:
    jq = app.job_queue
    jq.run_repeating(poster_job, interval=15, first=10, name="poster")
    jq.run_repeating(housekeeping_job, interval=60, first=20, name="housekeeping")
    jq.run_repeating(counter_refresh_job, interval=60, first=30, name="counters")
    jq.run_repeating(unpin_job, interval=300, first=60, name="unpin")
    jq.run_daily(daily_purge_job, time=dtime(hour=2, minute=30, tzinfo=timezone.utc), name="purge")
    jq.run_daily(backup_job, time=dtime(hour=3, minute=0, tzinfo=timezone.utc), name="backup")
    from modules.ads_admin import daily_ad_reports_job

    jq.run_repeating(daily_ad_reports_job, interval=3600, first=120, name="ad_reports")


COMMANDS = [
    BotCommand("start", "Main menu"), BotCommand("rules", "Rules"), BotCommand("delete", "Delete your confession"),
    BotCommand("advertise", "Advertise"), BotCommand("myads", "Your ads"), BotCommand("lang", "Language"),
]


async def post_init(app: Application) -> None:
    env = app.bot_data["env"]
    me = await app.bot.get_me()
    app.bot_data["bot_username"] = me.username
    try:
        chat = await app.bot.get_chat(env.channel_id)
        app.bot_data["channel_username"] = chat.username
    except TelegramError:
        app.bot_data["channel_username"] = None
    await app.bot.set_my_commands(COMMANDS)
    if env.owner_id:
        try:
            await app.bot.send_message(env.owner_id, f"started · v{VERSION} · {env.run_mode}")
        except TelegramError:
            pass


def build() -> tuple[Application, object]:
    env = load_env()
    db.init(env.db_path)
    security.configure(env.hmac_secret, env.fernet_key, env.owner_id)
    app = (ApplicationBuilder().token(env.bot_token).post_init(post_init)
           .concurrent_updates(False).build())
    app.bot_data["env"] = env
    app.add_error_handler(on_error)
    register_modules(app)
    register_jobs(app)
    return app, env


async def run_webhook(app: Application, env) -> None:
    web_app = build_app(env, app, verify_handler=handle_verify_webhook)
    runner = await start_server(web_app, env.listen_host, env.listen_port)
    async with app:
        await app.bot.set_webhook(
            url=env.public_url + env.webhook_path, secret_token=env.tg_webhook_secret or None,
            allowed_updates=["message", "callback_query", "my_chat_member"], drop_pending_updates=False,
        )
        await app.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await app.stop()
    await runner.cleanup()


async def run_polling(app: Application, env) -> None:
    """Polling for Telegram; aiohttp still serves /healthz and /webhooks/verify."""
    web_app = build_app(env, app, verify_handler=handle_verify_webhook)
    runner = await start_server(web_app, env.listen_host, env.listen_port)
    async with app:
        await app.bot.delete_webhook(drop_pending_updates=False)
        await app.start()
        await app.updater.start_polling(allowed_updates=["message", "callback_query", "my_chat_member"])
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await app.updater.stop()
        await app.stop()
    await runner.cleanup()


def main() -> None:
    setup_logging()
    app, env = build()
    logging.getLogger("app").info("confessbot v%s starting in %s mode", VERSION, env.run_mode)
    asyncio.run(run_webhook(app, env) if env.run_mode == "webhook" else run_polling(app, env))


if __name__ == "__main__":
    main()
