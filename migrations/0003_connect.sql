-- 0003_connect: OP → commenter consent-based contact exchange

CREATE TABLE connect_requests (
  id INTEGER PRIMARY KEY,
  number INT NOT NULL,            -- confession number
  comment_id INT NOT NULL,
  requester TEXT NOT NULL,        -- OP author hash
  target TEXT NOT NULL,           -- commenter author hash
  status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined','expired')),
  created INT,
  resolved INT
);
CREATE UNIQUE INDEX ux_connect_pending ON connect_requests(number, target) WHERE status='pending';
CREATE INDEX ix_connect_target ON connect_requests(target, status);

ALTER TABLE users ADD COLUMN last_connect INT DEFAULT 0;

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('connect_enabled',      '1',      'bool', 'comments', 'Let confession authors ask commenters to connect (consent-based)', NULL, NULL),
('connect_cooldown_sec', '300',    'int',  'comments', 'Seconds between connect requests per author', 0, 86400),
('connect_ttl_hours',    '168',    'int',  'comments', 'Pending connect requests expire after this many hours', 1, 720);
