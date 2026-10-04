-- 0005_single_stage: moderator approval publishes; flagged notices; My confessions; comment digests; contact admin

-- single-stage review: drop the second stage and its topic
DELETE FROM settings WHERE key IN ('final_review', 'topic_review');

-- who approved a post (for moderator feedback on takedown) and flagged notice tracking
ALTER TABLE posts ADD COLUMN approved_by INT;
ALTER TABLE posts ADD COLUMN risk_hits TEXT DEFAULT '[]';

-- grouped OP comment notifications
ALTER TABLE posts ADD COLUMN unnotified_comments INT DEFAULT 0;
ALTER TABLE users ADD COLUMN last_comment_notify INT DEFAULT 0;
ALTER TABLE users ADD COLUMN last_contact INT DEFAULT 0;

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('comment_notify_interval_sec', '600', 'int', 'comments', 'Min seconds between "new comments" DMs to an author (0 = off)', 0, 86400),
('contact_cooldown_sec', '600', 'int', 'ads', 'Seconds between advertiser "Contact admin" messages', 0, 86400);
