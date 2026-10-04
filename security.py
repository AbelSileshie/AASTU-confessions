"""Anonymity primitives and permission checks.

- author_hash(): HMAC-SHA256 of the Telegram ID. Raw IDs are never stored.
- encrypt_ref()/decrypt_ref(): Fernet-wrapped chat id, used only to DM users.
- sign_cb()/verify_cb(): authenticated callback payloads so forged button
  data is rejected.
- require(role): decorator for handlers; unknown users get silence.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import secrets
from typing import Awaitable, Callable, Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

ROLE_RANK = {"user": 0, "moderator": 1, "admin": 2, "owner": 3}

_hmac_secret: bytes = b""
_fernet: Optional[Fernet] = None
_owner_id: int = 0


def configure(hmac_secret: bytes, fernet_key: str, owner_id: int) -> None:
    global _hmac_secret, _fernet, _owner_id
    if len(hmac_secret) < 16:
        raise ValueError("hmac secret too short")
    _hmac_secret = hmac_secret
    _fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
    _owner_id = owner_id


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def author_hash(telegram_id: int) -> str:
    return hmac.new(_hmac_secret, str(int(telegram_id)).encode(), hashlib.sha256).hexdigest()


def encrypt_ref(telegram_id: int) -> bytes:
    if _fernet is None:
        raise RuntimeError("security.configure() not called")
    return _fernet.encrypt(str(int(telegram_id)).encode())


def decrypt_ref(blob: bytes | None) -> Optional[int]:
    if not blob or _fernet is None:
        return None
    try:
        return int(_fernet.decrypt(bytes(blob)))
    except (InvalidToken, ValueError):
        return None


def new_token(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


# ---------------------------------------------------------------------------
# Callback data signing  ("action:payload:sig")
# ---------------------------------------------------------------------------

_SIG_LEN = 12


def _cb_sig(action: str, payload: str) -> str:
    return hmac.new(_hmac_secret, f"cb:{action}:{payload}".encode(), hashlib.sha256).hexdigest()[:_SIG_LEN]


def sign_cb(action: str, payload: str = "") -> str:
    data = f"{action}:{payload}:{_cb_sig(action, payload)}"
    if len(data.encode()) > 64:
        raise ValueError("callback data too long")
    return data


def verify_cb(data: str | None) -> Optional[tuple[str, str]]:
    """Return (action, payload) if the signature is valid, else None."""
    if not data or data.count(":") < 2:
        return None
    # action never contains ':'; payload may.
    action, rest = data.split(":", 1)
    payload, _, sig = rest.rpartition(":")
    if not hmac.compare_digest(sig, _cb_sig(action, payload)):
        return None
    return action, payload


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def role_of(user_id: int | None) -> str:
    from db import one

    if user_id is None:
        return "user"
    if _owner_id and user_id == _owner_id:
        return "owner"
    row = one("SELECT role FROM staff WHERE user_id=? AND active=1", (user_id,))
    return row["role"] if row else "user"


def has_role(user_id: int | None, required: str) -> bool:
    return ROLE_RANK[role_of(user_id)] >= ROLE_RANK[required]


def require(role: str) -> Callable:
    """Handler decorator. Silently ignores callers below `role`."""

    def deco(fn: Callable[..., Awaitable]):
        @functools.wraps(fn)
        async def wrapper(update, context, *a, **kw):
            user = update.effective_user
            if user is None or not has_role(user.id, role):
                if update.callback_query:
                    try:
                        await update.callback_query.answer()
                    except Exception:  # nosec B110 - swallow on purpose: never reveal command existence
                        pass
                return None
            return await fn(update, context, *a, **kw)

        return wrapper

    return deco


def is_private_chat(update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def redact(text: str) -> str:
    """Last-resort log scrubber: strip anything that looks like a token or id."""
    import re

    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", "[bot-token]", text)
    text = re.sub(r"\b\d{7,}\b", "[id]", text)
    return text
