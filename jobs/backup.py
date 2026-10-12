"""Nightly encrypted SQLite backup → private Telegram channel."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
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
_AGE_RECIPIENT_RE = re.compile(r"age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}\Z")
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATORS = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _bech32_polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top_bits = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(_BECH32_GENERATORS):
            if (top_bits >> index) & 1:
                checksum ^= generator
    return checksum


def _valid_age_recipient(recipient: str) -> bool:
    if not _AGE_RECIPIENT_RE.fullmatch(recipient):
        return False
    hrp = [ord(char) >> 5 for char in "age"] + [0] + [ord(char) & 31 for char in "age"]
    data = [_BECH32_CHARSET.index(char) for char in recipient[4:]]
    key_data = data[:-6]
    if len(key_data) != 52 or key_data[-1] & 0x0F:
        return False
    return _bech32_polymod(hrp + data) == 1


def backup_configuration_issue(recipient: str) -> str | None:
    if not recipient:
        return "AGE_RECIPIENT is missing"
    if not _valid_age_recipient(recipient):
        return "AGE_RECIPIENT is not a valid age public recipient"
    if shutil.which("age") is None:
        return "age executable is not installed"
    return None


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
    if not env.backup_channel_id:
        log.info("backup skipped: not configured")
        return
    issue = backup_configuration_issue(env.age_recipient)
    if issue:
        log.error("backup unavailable: %s", issue)
        await alert_admins(context, f"❗ Backup failed: {issue}")
        return
    with tempfile.TemporaryDirectory(prefix="confessbak-") as tmp:
        try:
            path = await make_backup(env.db_path, env.age_recipient, tmp)
            fh = await asyncio.to_thread(open, path, "rb")
            try:
                msg = await context.bot.send_document(env.backup_channel_id, fh, filename=os.path.basename(path),
                                                      caption=f"backup {os.path.basename(path)} · {os.path.getsize(path)//1024} KB")
            finally:
                await asyncio.to_thread(fh.close)
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
