# AASTU Confessions Bot

Anonymous campus confessions on Telegram with manual moderation, anonymous comment
threads, a consent-based *Connect* feature, and a self-funded ads system paid by
bank transfer and verified through [Verify.et](https://verify.et).

The full design is in [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). This README covers
how to run, test and deploy it.

## Design rules

1. One process, one SQLite file, one domain.
2. Everything an operator touches lives in the database and is edited from inside the bot
   (`/settings`, `/packages`, `/categories`, `/payout_accounts`, `/block`, `/staff`).
3. Approvals are always manual.
4. Anonymous by construction: raw Telegram IDs are never written to disk
   (`author = HMAC(secret, id)`; the DM handle is Fernet-encrypted).

## Layout

```
app.py              entry point (RUN_MODE=polling|webhook), jobs, module registry
config.py           .env secrets + cfg() cached runtime settings
db.py               SQLite (WAL), migrations, helpers
security.py         author hash, chat_ref encryption, signed callbacks, @require(role)
textfilter.py       normalisation, link/phone/mention detection, block/risk words
payments.py         Verify.et client + acceptance checks (shared service)
fmt.py              UTF-16-aware rich text builder (entities, never parse_mode)
ops.py              staff/catalog/wordlist/stats operations shared by commands and /admin menus
common.py           shared helpers for modules
modules/            core submit review comments staff admin setup settings catalog wordlist ads ads_admin takedown stats
jobs/               poster housekeeping backup
web/server.py       aiohttp: Telegram webhook, /webhooks/verify, /healthz
migrations/         additive SQL migrations applied on startup
strings/            en.json, am.json
tests/              unit + end-to-end (fake Telegram API, fake Verify.et) + adversarial security tests
deploy/             systemd unit, Caddy snippet, backup/restore runbook
```

Modules never import each other; they share state through the database.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # fill BOT_TOKEN, OWNER_ID, CHANNEL_ID, ADMIN_GROUP_ID, HMAC_SECRET, FERNET_KEY
.venv/bin/python app.py  # RUN_MODE=polling by default
```

Generate secrets:

```bash
openssl rand -hex 32                                                        # HMAC_SECRET, TG_WEBHOOK_SECRET, WEBHOOK_PATH suffix
.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"  # FERNET_KEY
```

Telegram setup (no ids to copy): create the bot with @BotFather, then **add the bot as an admin**
to your public channel (post, edit, delete, pin), to a private admin group (with *Manage topics*)
and to a private backup channel. Each time, the bot DMs the owner *"I was added to … — what should
I use it for?"* with one-tap buttons. `CHANNEL_ID`/`ADMIN_GROUP_ID`/`BACKUP_CHANNEL_ID` in `.env`
are only needed if you prefer to set them by hand.

`⚙️ Admin → 🧭 Setup checklist` tells you exactly what is still missing before launch (bot rights,
topics, moderators, rules, backups, payout accounts, Verify.et keys); it is also DM'd to the
owner at startup while anything is red.

First run: the owner (`OWNER_ID`) has every permission. Add staff with `/staff add` and send
them the invite link. Add receiving accounts with `/payout_accounts add telebirr 09xxxxxxxx "Holder Name"`.

## Dependencies

`requirements.txt` holds the ranges; `requirements.lock` is the exact set verified by the test suite and
`pip-audit`. Deploy with `pip install -r requirements.lock`. Security model: [SECURITY.md](SECURITY.md).

## Tests

```bash
.venv/bin/python -m pytest -q            # 234 tests, ~30 s
.venv/bin/bandit -q -r . -x ./.venv,./tests
.venv/bin/pip-audit
```

The E2E suite drives the real `python-telegram-bot` Application through an in-process fake
Bot API and a fake Verify.et, so conversations, signed callbacks, jobs and the webhook server
all run unmodified. `tests/test_security_adversarial.py` covers forged/replayed callbacks,
IDOR, privilege escalation, SQL/markup injection, unicode filter evasion, oversized input and
anonymity guarantees.

## Deployment (own VPS + domain, Caddy)

```bash
adduser --system --group --home /opt/confessbot confessbot
apt install -y python3-venv sqlite3 age
sudo -u confessbot bash -c 'cd /opt/confessbot && git clone <repo> app && python3 -m venv venv \
  && venv/bin/pip install -r app/requirements.txt && mkdir -p data'
cp /opt/confessbot/app/.env.example /opt/confessbot/.env && chmod 600 /opt/confessbot/.env && chown confessbot: /opt/confessbot/.env
# edit .env: RUN_MODE=webhook, PUBLIC_URL, WEBHOOK_PATH=/tg/<random>, TG_WEBHOOK_SECRET, VERIFY_*, AGE_RECIPIENT
cat /opt/confessbot/app/deploy/Caddyfile.snippet >> /etc/caddy/Caddyfile && systemctl reload caddy
cp /opt/confessbot/app/deploy/confessbot.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now confessbot
curl -s https://confess.yourdomain.et/healthz
```

Verify.et: register your receiving accounts in the Verify.et dashboard (enables settlement
matching), create an API key with `verification: read, write`, and register the webhook
`https://confess.yourdomain.et/webhooks/verify` with a signing secret. Unsigned webhooks are
rejected (fail closed).

Backups run nightly at 03:00 UTC to the backup channel, encrypted with `age`; see
[deploy/backup-restore.md](deploy/backup-restore.md).

## Moderation flow

```
student → DM queue (moderators, 📋 Queue / /next, atomic claim)
        → Approve → outbox → channel          (one review is enough)
          risk-flagged items post too, but a notice lands in ⚠️ Flagged with [🛑 Take down] [✅ Fine]
        → Reject → 🗂 Declined topic: full text · reason · who · [↩️ Restore]; purged after `declined_ttl_hours`
takedown by someone else → the moderator who approved it is told why
```

Other topics: 🛑 Takedowns (requests, auto-hidden comments), 💰 Ads (payment review, advertiser
messages with [↩️ Reply]), 🔔 Alerts (queue, poster, backups). **Group setup:** enable *Topics* in the admin group and
give the bot *Manage topics* — the bot creates the topics itself at startup and re-checks hourly (`⚙️ Admin → Group
setup` shows the state and can force it).

## Student extras

Confessions can carry 0–3 categories (hashtags on the post). Students only ever see their own confessions (`📝 My confessions`); there is no public listing inside the bot.
Live notifications carry `📣 Open in channel` and `💬 Comments` buttons (plus `➕ Join the channel` when the channel is
private). `📝 My confessions` shows the status of recent submissions (pending / live #N / not approved /
deleted) with Delete and Thread buttons. Authors get one grouped DM for new comments at most every
`comment_notify_interval_sec`. Advertisers manage orders in `/myads` (re-open pay screen, cancel,
✉️ Contact admin → 💰 Ads topic).

## Admin hub

Staff see an extra keyboard row: `📋 Queue · 📊 Stats · ⚙️ Admin` (admins/owner only for the last).
`⚙️ Admin` opens inline menus for Staff (invite/promote/remove/audit), Settings, Categories,
Packages, Payout accounts (owner), Wordlist, Ads, Group setup and Health. The slash
commands below remain as a fast path and call the same functions (`ops.py`).

## Operator commands

| Who | Commands |
|---|---|
| everyone | `/start` `/rules` `/delete <n>` `/lang` `/advertise` `/myads` |
| moderator | `/next [1-5]` `/stats` `/top` `/block` `/risk` `/words` `/takedown <n> <reason>` `/shift on\|off` |
| admin | `/admin` `/settings` `/set k v` `/staff` `/unban` `/unblock` `/categories` `/packages` `/ads` `/ad_report` `/revenue` `/post_rules` `/mediakit` `/audit` |
| owner | `/payout_accounts` `/export` `/import` (attach JSON) `/refunds` |
