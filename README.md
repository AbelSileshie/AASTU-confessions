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
common.py           shared helpers for modules
modules/            core submit review comments staff settings catalog wordlist ads ads_admin takedown stats
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

Telegram setup: create the bot with @BotFather, create a public channel and add the bot as
an admin (post, edit, delete, pin), create a private admin group and a private backup
channel and add the bot to both. Get the `-100…` ids by forwarding a message from each to
@userinfobot or similar.

First run: the owner (`OWNER_ID`) has every permission. Add staff with `/staff add` and send
them the invite link. Add receiving accounts with `/payout_accounts add telebirr 09xxxxxxxx "Holder Name"`.

## Tests

```bash
.venv/bin/python -m pytest -q            # 146 tests, ~10 s
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

## Operator commands

| Who | Commands |
|---|---|
| everyone | `/start` `/rules` `/delete <n>` `/lang` `/advertise` `/myads` |
| moderator | `/next [1-5]` `/stats` `/top` `/block` `/risk` `/words` `/takedown <n> <reason>` `/shift on\|off` |
| admin | `/settings` `/set k v` `/staff` `/unban` `/unblock` `/categories` `/packages` `/ads` `/ad_report` `/revenue` `/post_rules` `/mediakit` `/audit` |
| owner | `/payout_accounts` `/export` `/import` (attach JSON) `/refunds` |
