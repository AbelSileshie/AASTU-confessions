-- 0007_text_format: real newlines in seeded texts (SQLite keeps '\n' literally), owner's rules wording,
-- legacy repair for deployed databases. Settings texts render with the first line as a bold title.

UPDATE settings SET value = replace(value, '\n', char(10)) WHERE grp = 'texts';

UPDATE settings SET value =
  '📜 Rules' || char(10) || char(10) ||
  '1. No real names, phone numbers or usernames.' || char(10) ||
  '2. No threats, blackmail or sexual content about real people.' || char(10) ||
  '3. No links or ads — use 📢 Advertise.' || char(10) ||
  '4. Be kind. Moderators may edit or reject anything.' || char(10) || char(10) ||
  '🛑 Need something removed? Open the bot → 📜 Rules → Request takedown.'
WHERE key = 'rules_text' AND (updated_by IS NULL OR value LIKE 'Rules:%');

UPDATE settings SET value =
  '👋 Welcome to AASTU Confessions' || char(10) || char(10) ||
  'Everything you send here is anonymous — we never store your Telegram ID.' || char(10) ||
  'Tap ✍️ Confess to begin.'
WHERE key = 'welcome_text' AND updated_by IS NULL;

UPDATE settings SET value = '✅ Received. A moderator will review it; you will get a message when it is live.'
WHERE key = 'received_text' AND updated_by IS NULL;

UPDATE settings SET value =
  '📢 Ad rules' || char(10) || char(10) ||
  'No betting, crypto schemes, adult content, political campaigns or fake products.' || char(10) ||
  'Content is reviewed before posting; refused ads are refunded in full.'
WHERE key = 'ad_rules_text' AND updated_by IS NULL;
