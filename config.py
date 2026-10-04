"""Environment secrets (.env) and the cached runtime settings accessor.

Only secrets and wiring live in the environment. Everything an operator
touches is in the `settings` table and read through `cfg()`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()

SETTINGS_TTL_SEC = 30


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Env:
    bot_token: str
    owner_id: int
    channel_id: int
    admin_group_id: int
    backup_channel_id: int
    run_mode: str
    public_url: str
    webhook_path: str
    tg_webhook_secret: str
    listen_host: str
    listen_port: int
    hmac_secret: bytes
    fernet_key: str
    verify_api_key: str
    verify_webhook_secret: str
    verify_base_url: str
    age_recipient: str
    db_path: str


def load_env(strict: bool = True) -> Env:
    """Read and validate the environment. `strict=False` is for tests."""
    listen = _env("LISTEN", "127.0.0.1:8081")
    host, _, port = listen.rpartition(":")
    run_mode = _env("RUN_MODE", "polling").lower()
    if run_mode not in ("polling", "webhook"):
        raise RuntimeError("RUN_MODE must be 'polling' or 'webhook'")
    hmac_secret = _env("HMAC_SECRET", required=strict)
    if strict and len(hmac_secret) < 32:
        raise RuntimeError("HMAC_SECRET must be at least 32 characters")
    webhook_path = _env("WEBHOOK_PATH", "/tg/dev")
    if not webhook_path.startswith("/"):
        raise RuntimeError("WEBHOOK_PATH must start with '/'")
    tg_secret = _env("TG_WEBHOOK_SECRET", "")
    owner_id = _env_int("OWNER_ID")
    if strict and not owner_id:
        raise RuntimeError("OWNER_ID is required (the owner bootstraps staff and receives setup messages)")
    if strict and run_mode == "webhook":
        if len(tg_secret) < 32:
            raise RuntimeError("TG_WEBHOOK_SECRET (≥32 chars) is required in webhook mode")
        if webhook_path == "/tg/dev" or len(webhook_path) < 12:
            raise RuntimeError("WEBHOOK_PATH must be a random path like /tg/<32 hex> in webhook mode")
        if not _env("PUBLIC_URL", "").startswith("https://"):
            raise RuntimeError("PUBLIC_URL must be an https:// URL in webhook mode")
    return Env(
        bot_token=_env("BOT_TOKEN", required=strict),
        owner_id=owner_id,
        channel_id=_env_int("CHANNEL_ID"),
        admin_group_id=_env_int("ADMIN_GROUP_ID"),
        backup_channel_id=_env_int("BACKUP_CHANNEL_ID"),
        run_mode=run_mode,
        public_url=_env("PUBLIC_URL", "").rstrip("/"),
        webhook_path=webhook_path,
        tg_webhook_secret=tg_secret,
        listen_host=host or "127.0.0.1",
        listen_port=int(port or 8081),
        hmac_secret=hmac_secret.encode(),
        fernet_key=_env("FERNET_KEY", required=strict),
        verify_api_key=_env("VERIFY_API_KEY", ""),
        verify_webhook_secret=_env("VERIFY_WEBHOOK_SECRET", ""),
        verify_base_url=_env("VERIFY_BASE_URL", "https://verify.et").rstrip("/"),
        age_recipient=_env("AGE_RECIPIENT", ""),
        db_path=_env("DB_PATH", "./data/confess.db"),
    )


# ---------------------------------------------------------------------------
# Runtime settings (DB-backed, cached)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}


def _coerce(value: str, type_: str) -> Any:
    if type_ == "int":
        return int(value)
    if type_ == "bool":
        return value in ("1", "true", "True", "yes")
    return value


def cfg(key: str, default: Any = None) -> Any:
    """Return a runtime setting, cached for SETTINGS_TTL_SEC."""
    from db import one  # local import keeps config importable without a DB

    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    row = one("SELECT value, type FROM settings WHERE key=?", (key,))
    value = _coerce(row["value"], row["type"]) if row else default
    _cache[key] = (now + SETTINGS_TTL_SEC, value)
    return value


def invalidate(key: str | None = None) -> None:
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)
