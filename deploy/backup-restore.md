# Backup & restore

Backups are produced nightly (03:00 UTC) by the bot: `sqlite3 .backup` → `age` encrypt →
uploaded to the private backup channel. The last 30 are kept.

## Restore drill (monthly, on your laptop)
1. Download the latest `confess-YYYYMMDD-HHMM.db.age` from the backup channel.
2. `age -d -i ~/.age/confessbot.key -o confess.db confess-*.db.age`
3. `sqlite3 confess.db "PRAGMA integrity_check"`  → `ok`
4. `sqlite3 confess.db "SELECT count(*) FROM posts"`

## Disaster recovery to a new server
1. Follow README install steps (user, venv, Caddy, systemd).
2. Put the decrypted file at `/opt/confessbot/data/confess.db` (owner `confessbot`, mode 600).
3. Restore `.env` from your password manager (same HMAC_SECRET and FERNET_KEY or author links and DMs break).
4. `systemctl start confessbot` and check `/healthz`.
