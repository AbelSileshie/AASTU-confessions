-- 0004_review_stages: two-stage approval, admin-group topics, declined ledger

ALTER TABLE outbox ADD COLUMN stage TEXT NOT NULL DEFAULT 'ready';   -- final | flagged | ready
ALTER TABLE outbox ADD COLUMN final_by INT;
ALTER TABLE outbox ADD COLUMN final_at INT;
ALTER TABLE outbox ADD COLUMN group_msg_id INT;
ALTER TABLE outbox ADD COLUMN risk_hits TEXT DEFAULT '[]';
CREATE INDEX ix_outbox_stage ON outbox(stage, not_before, id);

CREATE TABLE declined (
  id INTEGER PRIMARY KEY,
  author TEXT NOT NULL,
  category_id INT,
  text TEXT NOT NULL,
  text_hash TEXT,
  risk_hits TEXT DEFAULT '[]',
  reason TEXT,
  declined_by INT,
  group_msg_id INT,
  created INT
);

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('final_review',        '1',  'bool', 'moderation', 'Moderator approvals need an admin Post in the group before publishing', NULL, NULL),
('declined_ttl_hours',  '72', 'int',  'moderation', 'Hours a declined confession stays in the Declined topic (then deleted)', 1, 720),
('topic_review',    '0', 'int', 'group', 'Thread id: 📋 Final review (0 = main chat)', 0, 2147483647),
('topic_flagged',   '0', 'int', 'group', 'Thread id: ⚠️ Flagged', 0, 2147483647),
('topic_takedowns', '0', 'int', 'group', 'Thread id: 🛑 Takedowns', 0, 2147483647),
('topic_declined',  '0', 'int', 'group', 'Thread id: 🗂 Declined', 0, 2147483647),
('topic_ads',       '0', 'int', 'group', 'Thread id: 💰 Ads', 0, 2147483647),
('topic_alerts',    '0', 'int', 'group', 'Thread id: 🔔 Alerts', 0, 2147483647);

-- bot banner: keep the code path, hide the product until it is actually sold
UPDATE packages SET enabled=0 WHERE slug='banner';
