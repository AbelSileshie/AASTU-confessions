-- 0008_connect_mutual: one-way reveal (commenter → vent owner), two-step with mutual change-of-mind

CREATE TABLE connect_requests_v2 (
  id INTEGER PRIMARY KEY,
  number INT NOT NULL,
  comment_id INT NOT NULL,
  requester TEXT NOT NULL,        -- vent owner (OP) author hash
  target TEXT NOT NULL,           -- commenter author hash
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','revealed','declined','withdrawn','revoked','refused','expired')),
  created INT,
  resolved INT,
  revealed_at INT,
  op_msg_id INT,                  -- OP's "request sent" DM (to edit buttons)
  target_msg_id INT               -- commenter's consent DM
);
INSERT INTO connect_requests_v2(id, number, comment_id, requester, target, status, created, resolved)
  SELECT id, number, comment_id, requester, target, status, created, resolved FROM connect_requests;
DROP TABLE connect_requests;
ALTER TABLE connect_requests_v2 RENAME TO connect_requests;
CREATE UNIQUE INDEX ux_connect_open ON connect_requests(number, target) WHERE status IN ('pending','accepted','revealed');
CREATE INDEX ix_connect_target ON connect_requests(target, status);
CREATE INDEX ix_connect_requester ON connect_requests(requester, status);

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('connect_revoke_hours', '72', 'int', 'comments', 'Hours after sharing during which a commenter can still revoke', 1, 720);

UPDATE settings SET description='Let confession authors ask commenters to share their contact (one-way, consent-based)' WHERE key='connect_enabled';
