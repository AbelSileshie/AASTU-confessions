-- 0009_setup: owner-assigned chats (override .env) for non-developer setup
INSERT INTO settings(key, value, type, grp, description, min, max) VALUES
('chat_channel',     '0', 'int', 'group', 'Confessions channel id chosen from the bot (0 = use .env CHANNEL_ID)', -9999999999999, 0),
('chat_admin_group', '0', 'int', 'group', 'Admin group id chosen from the bot (0 = use .env ADMIN_GROUP_ID)', -9999999999999, 0),
('chat_backup',      '0', 'int', 'group', 'Backup channel id chosen from the bot (0 = use .env BACKUP_CHANNEL_ID)', -9999999999999, 0);
