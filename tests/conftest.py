"""Shared fixtures: fresh file-backed SQLite per test, configured security."""
from __future__ import annotations

pytest_plugins = ("tests.e2e_harness",)

import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("HMAC_SECRET", "x" * 40)
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("OWNER_ID", "1000")
os.environ.setdefault("CHANNEL_ID", "-1001")
os.environ.setdefault("ADMIN_GROUP_ID", "-1002")
os.environ.setdefault("BACKUP_CHANNEL_ID", "-1003")

import config
import db
import security

OWNER_ID = 1000


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.reset_for_tests(str(tmp_path / "test.db"))
    config.invalidate()
    security.configure(os.environ["HMAC_SECRET"].encode(), os.environ["FERNET_KEY"], OWNER_ID)
    yield
    config.invalidate()


@pytest.fixture
def env():
    return config.load_env(strict=True)
