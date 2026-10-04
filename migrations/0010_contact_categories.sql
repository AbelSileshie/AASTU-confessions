-- 0010: contact reveal bookkeeping; categories without emoji; multi-select categories (0–3)

ALTER TABLE connect_requests ADD COLUMN op_offer_msg_id INT;
ALTER TABLE connect_requests ADD COLUMN target_accept_msg_id INT;
ALTER TABLE connect_requests ADD COLUMN target_reveal_msg_id INT;

ALTER TABLE categories ADD COLUMN emoji TEXT DEFAULT '';
UPDATE categories SET emoji = substr(label, 1, instr(label, ' ') - 1), label = substr(label, instr(label, ' ') + 1)
 WHERE instr(label, ' ') > 0 AND unicode(label) > 8000;

ALTER TABLE submissions ADD COLUMN category_ids TEXT DEFAULT '[]';
ALTER TABLE outbox ADD COLUMN category_ids TEXT DEFAULT '[]';
ALTER TABLE posts ADD COLUMN category_ids TEXT DEFAULT '[]';
ALTER TABLE declined ADD COLUMN category_ids TEXT DEFAULT '[]';
UPDATE declined SET category_ids = '[' || category_id || ']' WHERE category_id IS NOT NULL;
UPDATE submissions SET category_ids = '[' || category_id || ']' WHERE category_id IS NOT NULL;
UPDATE outbox SET category_ids = '[' || category_id || ']' WHERE category_id IS NOT NULL;
UPDATE posts SET category_ids = '[' || category_id || ']' WHERE category_id IS NOT NULL;

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('max_categories', '3', 'int', 'submissions', 'Maximum categories a confession can have (0 allowed by default)', 1, 5);
