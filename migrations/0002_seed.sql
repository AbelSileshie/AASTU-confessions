-- 0002_seed: default settings, categories, packages. All editable from the bot.

INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
-- Submissions
('submissions_open',      '1',    'bool', 'submissions', 'Accept new confessions', NULL, NULL),
('submit_cooldown_sec',   '600',  'int',  'submissions', 'Seconds between confessions per user', 0, 86400),
('submit_daily_cap',      '5',    'int',  'submissions', 'Max confessions per user per day (0 = unlimited)', 0, 100),
('min_len',               '20',   'int',  'submissions', 'Minimum confession length', 1, 1000),
('max_len',               '2000', 'int',  'submissions', 'Maximum confession length', 50, 4000),
('queue_soft_cap',        '0',    'int',  'submissions', 'Refuse new confessions when queue exceeds this (0 = off)', 0, 100000),
('open_hours',            '',     'text', 'submissions', 'Accept submissions only in HH-HH (e.g. 08-23), blank = always', NULL, NULL),
('submission_ttl_hours',  '0',    'int',  'submissions', 'Drop unreviewed confessions older than this (0 = never)', 0, 720),
-- Posting
('post_interval_sec',     '120',  'int',  'posting', 'Seconds between channel posts', 5, 86400),
('max_ads_per_day',       '3',    'int',  'posting', 'Max sponsored posts per day', 0, 50),
('min_posts_between_ads', '6',    'int',  'posting', 'Confessions required between two ads', 0, 100),
('ad_footer',             '📢 Sponsored', 'text', 'posting', 'Footer line on sponsored posts', NULL, NULL),
-- Comments
('comments_enabled',      '1',    'bool', 'comments', 'Allow comments on confessions', NULL, NULL),
('comment_cooldown_sec',  '30',   'int',  'comments', 'Seconds between comments per user', 0, 3600),
('comment_max_len',       '500',  'int',  'comments', 'Maximum comment length', 1, 2000),
('report_hide_threshold', '3',    'int',  'comments', 'Distinct reports that hide a comment', 1, 100),
('report_cooldown_sec',   '10',   'int',  'comments', 'Seconds between reports per user', 0, 3600),
('counter_refresh_sec',   '300',  'int',  'comments', 'Min seconds between comment-counter edits per post', 30, 86400),
-- Moderation
('claim_timeout_sec',     '600',  'int',  'moderation', 'Release a claimed item after this many seconds', 60, 86400),
('ban_strikes',           '3',    'int',  'moderation', 'Strikes before an automatic ban', 1, 100),
('ban_days',              '7',    'int',  'moderation', 'Ban length in days', 1, 3650),
('queue_alert_threshold', '200',  'int',  'moderation', 'Alert admins when queue exceeds this', 1, 100000),
('flagged_requires_admin','0',    'bool', 'moderation', 'Risk-flagged items need an admin to approve', NULL, NULL),
('notify_authors',        '1',    'bool', 'moderation', 'DM authors when posted/rejected', NULL, NULL),
-- Ads
('ads_enabled',           '1',    'bool', 'ads', 'Allow /advertise', NULL, NULL),
('ad_payment_timeout_hours','48', 'int',  'ads', 'Cancel unpaid orders after this many hours', 1, 720),
('payment_max_age_days',  '7',    'int',  'ads', 'Reject receipts older than this', 1, 365),
('payment_max_attempts',  '3',    'int',  'ads', 'Reference entry attempts before manual review', 1, 10),
('ads_allow_shorteners',  '0',    'bool', 'ads', 'Allow URL shorteners in ad buttons', NULL, NULL),
-- Texts
('welcome_text', 'Welcome to AASTU Confessions. Everything you send here is anonymous: we never store your Telegram ID.', 'text', 'texts', 'Shown on /start', NULL, NULL),
('rules_text', 'Rules:\n1. No real names, phone numbers or usernames.\n2. No threats, blackmail or sexual content about real people.\n3. No links or ads (use /advertise).\n4. Be kind. Moderators may edit or reject anything.\n\nTo request a takedown, open the bot → Rules → Request takedown.', 'text', 'texts', 'Rules text (/rules and pinned post)', NULL, NULL),
('received_text', 'Received ✅ A moderator will review it. You will be notified when it is live.', 'text', 'texts', 'Reply after a confession is submitted', NULL, NULL),
('ad_rules_text', 'Ad rules: no betting, crypto schemes, adult content, political campaigns or fake products. Content is reviewed before posting; refused ads are refunded in full.', 'text', 'texts', 'Shown before an advertiser places an order', NULL, NULL);

INSERT INTO categories(slug, label, hashtag, sort) VALUES
('relationship', '❤️ Relationship', '#Relationship', 1),
('crush',        '😳 Crush',        '#Crush',        2),
('campus',       '🏫 Campus',       '#Campus',       3),
('rant',         '😤 Rant',         '#Rant',         4),
('funny',        '😂 Funny',        '#Funny',        5),
('academic',     '📚 Academic',     '#Academic',     6),
('advice',       '🤔 Advice',       '#Advice',       7),
('other',        '💬 Other',        '#Other',        8);

INSERT INTO packages(slug, name, description, price_etb, posts, pin_hours, bot_banner_days, max_chars, sort) VALUES
('basic',  'Basic post',   '1 sponsored post in the channel',            800,  1, 0,  0, 600, 1),
('pinned', 'Pinned post',  '1 sponsored post, pinned for 24 hours',      2000, 1, 24, 0, 600, 2),
('bundle', 'Bundle ×3',    '3 sponsored posts spread across a week',     3000, 3, 0,  0, 600, 3),
('banner', 'Bot banner',   'One line under every "Received" reply, 7d', 1500, 0, 0,  7, 120, 4);

INSERT INTO wordlist(word, kind, added_at) VALUES
('video', 'risk', 0), ('photo', 'risk', 0), ('pic', 'risk', 0), ('nude', 'risk', 0),
('kill', 'risk', 0), ('suicide', 'risk', 0), ('rape', 'risk', 0), ('blackmail', 'risk', 0),
('ቪዲዮ', 'risk', 0), ('ፎቶ', 'risk', 0);

INSERT INTO meta(k, v) VALUES ('last_post_at', '0'), ('posts_since_ad', '0'), ('ads_today', '0'), ('ads_day', '0');
