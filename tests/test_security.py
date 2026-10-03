import asyncio
from types import SimpleNamespace

import pytest

import db
import security
from security import (
    author_hash,
    decrypt_ref,
    encrypt_ref,
    has_role,
    redact,
    require,
    role_of,
    sign_cb,
    verify_cb,
)
from tests.conftest import OWNER_ID


class TestIdentity:
    def test_hash_is_deterministic_and_hex(self):
        assert author_hash(42) == author_hash(42)
        assert len(author_hash(42)) == 64

    def test_hash_hides_id(self):
        assert author_hash(42) != "42"

    def test_hash_depends_on_secret(self):
        import os

        a = author_hash(42)
        security.configure(b"y" * 40, os.environ["FERNET_KEY"], OWNER_ID)
        assert author_hash(42) != a

    def test_chat_ref_roundtrip(self):
        assert decrypt_ref(encrypt_ref(123456789)) == 123456789

    def test_chat_ref_tamper_returns_none(self):
        blob = bytearray(encrypt_ref(1))
        blob[-1] ^= 0xFF
        assert decrypt_ref(bytes(blob)) is None
        assert decrypt_ref(None) is None



class TestCallbackSigning:
    def test_roundtrip(self):
        assert verify_cb(sign_cb("approve", "17")) == ("approve", "17")

    def test_payload_with_colon(self):
        assert verify_cb(sign_cb("set", "a:b")) == ("set", "a:b")

    def test_forged_rejected(self):
        assert verify_cb("approve:17:0000000000") is None
        assert verify_cb("approve:18:" + sign_cb("approve", "17").rsplit(":", 1)[1]) is None

    def test_garbage_rejected(self):
        assert verify_cb(None) is None
        assert verify_cb("x") is None

    def test_too_long_raises(self):
        with pytest.raises(ValueError):
            sign_cb("a", "p" * 70)


class TestRoles:
    def test_owner_from_env(self):
        assert role_of(OWNER_ID) == "owner"

    def test_unknown_is_user(self):
        assert role_of(555) == "user"
        assert not has_role(555, "moderator")

    def test_staff_roles_rank(self):
        db.execute("INSERT INTO staff(user_id, role) VALUES (1, 'moderator'), (2, 'admin')")
        assert has_role(1, "moderator") and not has_role(1, "admin")
        assert has_role(2, "moderator") and has_role(2, "admin") and not has_role(2, "owner")

    def test_inactive_staff_ignored(self):
        db.execute("INSERT INTO staff(user_id, role, active) VALUES (3, 'admin', 0)")
        assert role_of(3) == "user"


class TestRequire:
    def _update(self, uid, cb=False):
        q = SimpleNamespace(answer=_async_noop) if cb else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid), callback_query=q)

    def test_blocks_silently(self):
        called = []

        @require("moderator")
        async def h(update, ctx):
            called.append(1)
            return "ran"

        assert asyncio.run(h(self._update(999), None)) is None
        assert called == []

    def test_allows_owner(self):
        @require("admin")
        async def h(update, ctx):
            return "ran"

        assert asyncio.run(h(self._update(OWNER_ID), None)) == "ran"

    def test_callback_answered_when_blocked(self):
        @require("admin")
        async def h(update, ctx):
            return "ran"

        assert asyncio.run(h(self._update(5, cb=True), None)) is None


async def _async_noop(*a, **k):
    return None


def test_redact_scrubs_tokens_and_ids():
    s = redact("token 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234 user 987654321")
    assert "ABCDEF" not in s and "987654321" not in s
