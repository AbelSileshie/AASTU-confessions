-- 0006_abuse_limits: caps on open ad orders and takedown-request frequency

ALTER TABLE users ADD COLUMN last_takedown_req INT DEFAULT 0;

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('max_open_orders',        '2',    'int', 'ads',        'Max unpaid ad orders per advertiser at a time', 1, 20),
('takedown_cooldown_sec',  '600',  'int', 'moderation', 'Seconds between takedown requests per user', 0, 86400);
