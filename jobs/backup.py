"""Nightly encrypted SQLite backup → private Telegram channel."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
from common import alert_admins

log = logging.getLogger(__name__)
KEEP_LAST = 30


def _sqlite_backup(src_path: str, dest_path: str) -> None:
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def _age_encrypt(src: str, dest: str, recipient: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "age", "-r", recipient, "-o", dest, src,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"age failed: {err.decode(errors='replace')[:200]}")


async def make_backup(db_path: str, recipient: str, workdir: str) -> str:
    """Create an encrypted backup file; returns its path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    plain = os.path.join(workdir, f"confess-{stamp}.db")
    enc = plain + ".age"
    await asyncio.to_thread(_sqlite_backup, db_path, plain)
    try:
        await _age_encrypt(plain, enc, recipient)
    finally:
        Path(plain).unlink(missing_ok=True)
    return enc


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    env = context.bot_data["env"]
    if not env.backup_channel_id or not env.age_recipient:
        log.info("backup skipped: not configured")
        return
    with tempfile.TemporaryDirectory(prefix="confessbak-") as tmp:
        try:
            path = await make_backup(env.db_path, env.age_recipient, tmp)
            with open(path, "rb") as fh:
                msg = await context.bot.send_document(env.backup_channel_id, fh, filename=os.path.basename(path),
                                                      caption=f"backup {os.path.basename(path)} · {os.path.getsize(path)//1024} KB")
            db.execute("INSERT INTO meta(k, v) VALUES ('last_backup_at', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                       (str(db.now()),))
            await _prune(context, env.backup_channel_id, msg.message_id)
        except (RuntimeError, OSError, TelegramError) as exc:
            log.error("backup failed: %s", type(exc).__name__)
            await alert_admins(context, f"❗ Backup failed: {type(exc).__name__}")


async def _prune(context, channel_id: int, latest_msg_id: int) -> None:
    """Delete backup messages older than the last KEEP_LAST (tracked in meta)."""
    raw = db.scalar("SELECT v FROM meta WHERE k='backup_msgs'", default="") or ""
    ids = [int(x) for x in raw.split(",") if x] + [latest_msg_id]
    while len(ids) > KEEP_LAST:
        old = ids.pop(0)
        try:
            await context.bot.delete_message(channel_id, old)
        except TelegramError:
            pass
    db.execute("INSERT INTO meta(k, v) VALUES ('backup_msgs', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (",".join(map(str, ids)),))
