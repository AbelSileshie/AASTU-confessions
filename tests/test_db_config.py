import config
import db


def test_migrations_applied_once():
    assert db.migrate() == []
    names = [r["name"] for r in db.q("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == ["0001_init.sql", "0002_seed.sql", "0003_connect.sql", "0004_review_stages.sql", "0005_single_stage.sql", "0006_abuse_limits.sql", "0007_text_format.sql", "0008_connect_mutual.sql", "0009_setup.sql"]


def test_seed_present():
    assert db.scalar("SELECT count(*) FROM categories") == 8
    assert db.scalar("SELECT count(*) FROM packages") == 4
    assert db.scalar("SELECT value FROM settings WHERE key='post_interval_sec'") == "120"


def test_cfg_reads_and_caches():
    assert config.cfg("post_interval_sec") == 120
    db.execute("UPDATE settings SET value='60' WHERE key='post_interval_sec'")
    assert config.cfg("post_interval_sec") == 120  # cached
    config.invalidate("post_interval_sec")
    assert config.cfg("post_interval_sec") == 60


def test_cfg_types():
    assert config.cfg("comments_enabled") is True
    assert isinstance(config.cfg("ad_footer"), str)
    assert config.cfg("does_not_exist", "dflt") == "dflt"


def test_tx_rolls_back():
    try:
        with db.tx():
            db.execute("INSERT INTO meta(k, v) VALUES ('t', '1')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.scalar("SELECT v FROM meta WHERE k='t'") is None


def test_unique_verified_reference():
    db.execute("INSERT INTO ads(id, advertiser_id, package_id, price_etb, status) VALUES (1,1,1,100,'paid')")
    db.execute("INSERT INTO payments(ad_id, bank, reference, status) VALUES (1,'telebirr','ABC','verified')")
    db.execute("INSERT INTO payments(ad_id, bank, reference, status) VALUES (1,'telebirr','ABC','failed')")  # ok
    import sqlite3

    try:
        db.execute("INSERT INTO payments(ad_id, bank, reference, status) VALUES (1,'telebirr','ABC','verified')")
        assert False, "duplicate verified reference must fail"
    except sqlite3.IntegrityError:
        pass


def test_env_loads(env):
    assert env.owner_id == 1000 and env.run_mode == "polling"
