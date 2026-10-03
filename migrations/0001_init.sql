-- 0001_init: schema per IMPLEMENTATION-PLAN.md section 3

-- 3.1 Identity and content ---------------------------------------------------
CREATE TABLE users (
  author        TEXT PRIMARY KEY,
  chat_ref      BLOB,
  lang          TEXT DEFAULT 'en',
  last_submit   INT DEFAULT 0,
  last_comment  INT DEFAULT 0,
  last_report   INT DEFAULT 0,
  submits_today INT DEFAULT 0,
  submits_day   INT DEFAULT 0,
  strikes       INT DEFAULT 0,
  banned_until  INT DEFAULT 0,
  created       INT
);

CREATE TABLE submissions (
  id INTEGER PRIMARY KEY,
  author TEXT NOT NULL,
  category_id INT,
  text TEXT NOT NULL,
  text_hash TEXT UNIQUE,
  risk_hits TEXT DEFAULT '[]',
  priority INT DEFAULT 0,
  created INT,
  claimed_by INT,
  claimed_at INT
);
CREATE INDEX ix_submissions_queue ON submissions(claimed_by, priority DESC, id);

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('confession','ad','system')),
  author TEXT,
  category_id INT,
  text TEXT,
  media_file_id TEXT,
  buttons TEXT,
  ref_id INT,
  not_before INT DEFAULT 0,
  approved_by INT,
  approved_at INT,
  attempts INT DEFAULT 0,
  last_error TEXT
);

CREATE TABLE posts (
  number INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  channel_msg_id INT,
  author TEXT,
  category_id INT,
  posted_at INT,
  comment_count INT DEFAULT 0,
  counter_shown INT DEFAULT 0,
  counter_updated INT DEFAULT 0,
  deleted INT DEFAULT 0,
  deleted_reason TEXT
);
CREATE INDEX ix_posts_author ON posts(author);

CREATE TABLE comments (
  id INTEGER PRIMARY KEY,
  number INT NOT NULL,
  author TEXT NOT NULL,
  alias TEXT,
  reply_to INT,
  text TEXT NOT NULL,
  created INT,
  reports INT DEFAULT 0,
  hidden INT DEFAULT 0,
  hidden_at INT
);
CREATE INDEX ix_comments ON comments(number, hidden, id);

CREATE TABLE comment_reports (
  comment_id INT NOT NULL,
  reporter TEXT NOT NULL,
  PRIMARY KEY(comment_id, reporter)
);

-- 3.2 Configuration ------------------------------------------------------------
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  type TEXT NOT NULL CHECK(type IN ('int','bool','text')),
  grp TEXT DEFAULT 'general',
  description TEXT,
  min INT,
  max INT,
  updated_by INT,
  updated_at INT
);

CREATE TABLE categories (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL,
  hashtag TEXT NOT NULL,
  enabled INT DEFAULT 1,
  sort INT DEFAULT 0
);

CREATE TABLE wordlist (
  word TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('block','risk')),
  added_by INT,
  added_at INT,
  PRIMARY KEY(word, kind)
);

CREATE TABLE staff (
  user_id INTEGER PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN ('moderator','admin','owner')),
  display TEXT,
  added_by INT,
  added_at INT,
  active INT DEFAULT 1,
  on_shift INT DEFAULT 0
);

CREATE TABLE invites (
  code TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  created_by INT,
  expires INT,
  used_by INT
);

CREATE TABLE audit (
  id INTEGER PRIMARY KEY,
  actor INT,
  action TEXT NOT NULL,
  target TEXT,
  detail TEXT,
  at INT
);
CREATE INDEX ix_audit_at ON audit(at);

CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);

-- 3.3 Ads and payments -----------------------------------------------------------
CREATE TABLE packages (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  price_etb INT NOT NULL,
  posts INT DEFAULT 1,
  pin_hours INT DEFAULT 0,
  bot_banner_days INT DEFAULT 0,
  max_chars INT DEFAULT 600,
  allow_media INT DEFAULT 1,
  allow_button INT DEFAULT 1,
  enabled INT DEFAULT 1,
  sort INT DEFAULT 0
);

CREATE TABLE payout_accounts (
  id INTEGER PRIMARY KEY,
  bank TEXT NOT NULL,
  account TEXT NOT NULL,
  holder TEXT NOT NULL,
  enabled INT DEFAULT 1,
  sort INT DEFAULT 0
);

CREATE TABLE advertisers (
  id INTEGER PRIMARY KEY,
  user_id INT UNIQUE NOT NULL,
  name TEXT,
  phone TEXT,
  created INT,
  blocked INT DEFAULT 0
);

CREATE TABLE ads (
  id INTEGER PRIMARY KEY,
  advertiser_id INT NOT NULL,
  package_id INT NOT NULL,
  text TEXT,
  media_file_id TEXT,
  button_text TEXT,
  button_url TEXT,
  price_etb INT NOT NULL,
  payout_account_id INT,
  status TEXT NOT NULL CHECK(status IN ('draft','awaiting_payment','verifying','paid','needs_review',
                                        'approved','scheduled','live','done','rejected','refund_due','cancelled')),
  reject_reason TEXT,
  posts_left INT,
  scheduled_at INT,
  pay_attempts INT DEFAULT 0,
  created INT,
  paid_at INT,
  approved_by INT,
  approved_at INT
);
CREATE INDEX ix_ads_status ON ads(status);

CREATE TABLE payments (
  id INTEGER PRIMARY KEY,
  ad_id INT NOT NULL,
  bank TEXT NOT NULL,
  reference TEXT NOT NULL,
  extra TEXT,
  idem_key TEXT UNIQUE,
  request_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('submitted','queued','verified','failed','mismatch','duplicate','manual_ok','manual_no')),
  amount INT,
  sender_name TEXT,
  receiver_match INT,
  confirmed_before INT,
  reason TEXT,
  raw TEXT,
  created INT,
  completed_at INT
);
CREATE UNIQUE INDEX ux_payments_ref ON payments(bank, reference) WHERE status IN ('verified','manual_ok');
CREATE INDEX ix_payments_request ON payments(request_id);
CREATE TABLE webhook_events (delivery_id TEXT PRIMARY KEY, received_at INT);

CREATE TABLE ad_posts (
  id INTEGER PRIMARY KEY,
  ad_id INT NOT NULL,
  post_number INT NOT NULL,
  pinned_until INT,
  views_24h INT
);

CREATE TABLE banners (
  id INTEGER PRIMARY KEY,
  ad_id INT,
  text TEXT NOT NULL,
  url TEXT,
  starts INT,
  ends INT
);
