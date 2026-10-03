"""Jobs: backup (with a fake `age`), purge/retention, submission TTL, poster failure handling."""
from __future__ import annotations

import os
import stat

import pytest

import db
from jobs import backup as backup_mod
from jobs.housekeeping import expire_submissions, purge
from jobs.poster import poster_job
from tests.e2e_harness import ADMIN_GROUP, BACKUP, CHANNEL, h  # noqa: F401



@pytest.fixture
def fake_age(tmp_path, monkeypatch):
    """A stand-in `age` that just copies input → output (so no real key needed)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "age"
    script.write_text("#!/bin/sh\n# args: -r RECIPIENT -o OUT IN\ncp \"$5\" \"$4\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return script


async def test_backup_uploads_encrypted_file_and_prunes(h, fake_age, monkeypatch):
    monkeypatch.setattr(backup_mod, "KEEP_LAST", 2)
    env = h.env.__class__(**{**h.env.__dict__, "age_recipient": "age1test"})
    h.app.bot_data["env"] = env
    for _ in range(3):
        await h.run_job(backup_mod.backup_job)
    docs = [c for c in h.tg.calls if c.method == "sendDocument" and c.chat_id == BACKUP]
    assert len(docs) == 3 and all(d.params["filename"].endswith(".db.age") for d in docs)
    assert len(h.tg.deleted) == 1  # pruned down to KEEP_LAST
    assert db.scalar("SELECT v FROM meta WHERE k='last_backup_at'") is not None


async def test_backup_failure_alerts_admins(h, monkeypatch):
    env = h.env.__class__(**{**h.env.__dict__, "age_recipient": "age1test"})
    h.app.bot_data["env"] = env
    monkeypatch.setenv("PATH", "/nonexistent")
    await h.run_job(backup_mod.backup_job)
    assert "Backup failed" in h.last(ADMIN_GROUP).text


async def test_backup_skipped_when_unconfigured(h):
    await h.run_job(backup_mod.backup_job)
    assert h.tg.sent_to(BACKUP) == []


def test_purge_retention_rules(h):
    now = db.now()
    old = now - 400 * 86400
    db.execute("INSERT INTO comments(number, author, alias, text, created, hidden, hidden_at) VALUES (1,'a','x','old',?,1,?)", (old, old))
    db.execute("INSERT INTO comments(number, author, alias, text, created, hidden, hidden_at) VALUES (1,'a','x','new',?,1,?)", (now, now))
    db.execute("INSERT INTO ads(id, advertiser_id, package_id, price_etb, status) VALUES (1,1,1,1,'paid')")
    db.execute("INSERT INTO payments(ad_id, bank, reference, status, raw, created) VALUES (1,'telebirr','A','verified','RAW',?)", (old,))
    db.execute("INSERT INTO audit(actor, action, at) VALUES (1,'x',?)", (old,))
    db.execute("INSERT INTO webhook_events(delivery_id, received_at) VALUES ('d',?)", (old,))
    purge(now)
    assert db.scalar("SELECT count(*) FROM comments") == 1
    assert db.scalar("SELECT raw FROM payments") is None
    assert db.scalar("SELECT count(*) FROM audit WHERE action='x'") == 0
    assert db.scalar("SELECT count(*) FROM webhook_events") == 0


def test_submission_ttl(h):
    h.set("submission_ttl_hours", 1)
    db.execute("INSERT INTO submissions(author, text, text_hash, created) VALUES ('a','t','h1',?)", (db.now() - 7200,))
    db.execute("INSERT INTO submissions(author, text, text_hash, created) VALUES ('b','t','h2',?)", (db.now(),))
    assert expire_submissions(db.now()) == ["a"]
    assert db.scalar("SELECT count(*) FROM submissions") == 1


async def test_poster_retries_and_alerts_after_repeated_failure(h):
    h.set("post_interval_sec", 0)
    db.execute("INSERT INTO outbox(kind, author, text) VALUES ('confession','a','hello')")
    h.tg.fail_chats.add(CHANNEL)
    for _ in range(5):
        await h.run_job(poster_job)
    assert db.scalar("SELECT attempts FROM outbox") == 5
    assert "failed 5 times" in h.last(ADMIN_GROUP).text
    h.tg.fail_chats.clear()
    await h.run_job(poster_job)
    assert db.scalar("SELECT count(*) FROM outbox") == 0
    assert db.scalar("SELECT count(*) FROM posts") == 1
