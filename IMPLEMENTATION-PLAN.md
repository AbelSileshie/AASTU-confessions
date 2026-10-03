# Campus Confessions Bot: Technical Implementation Plan

Version 1.0. Covers architecture, database, modules, admin and settings management, the ads system paid through Verify.et receipt verification, deployment on your own domain, how to scale while keeping approvals manual, and day-to-day operations.

Design rules:
1. **One process, one SQLite file, one domain.** Nothing else until a measured trigger says so (section 10).
2. **Everything an operator will touch lives in the database, not the code.** Packages, prices, categories, rules text, blocklist, rate limits, posting interval, admins and bank accounts are all edited from inside the bot.
3. **Approvals are always manual.** Scaling means more moderators and smarter queues, not auto-approval.
4. **Anonymous by construction.** Raw Telegram IDs are never written to disk.

---

## 1. System overview

```
                         ┌────────────────────────────────────────────────────────┐
  Students ──Telegram──▶ │  bot (python-telegram-bot v21, webhook mode)            │
  Moderators ──────────▶ │   ├─ handlers/   (submit, review, comments, admin, ads)  │
  Advertisers ─────────▶ │   ├─ services/   (moderation queue, poster, payments)    │
                         │   ├─ jobs/       (outbox, claims, backups, counters)     │
  Verify.et ──webhook──▶ │   └─ web/        (aiohttp: /tg, /webhooks/verify, /healthz) │
                         │              │                                           │
                         │          SQLite (WAL) /opt/confessbot/data/confess.db     │
                         └────────────────────────────────────────────────────────┘
                                  ▲                               │
            Caddy (TLS, your domain) ───────────────────── nightly encrypted backup
                                                          → private Telegram channel
Outputs: public channel · admin group · backup channel · advertiser DMs
```

Single VPS, single systemd service, Caddy in front for HTTPS. Telegram and Verify.et both deliver to the same aiohttp server on different paths.

---

## 2. Roles and permissions

| Role | Who | Can do |
|---|---|---|
| **owner** | You (set once in `.env` as `OWNER_ID`) | Everything, plus add or remove admins and change bank accounts and API keys |
| **admin** | Trusted team | Everything a moderator can, plus manage settings, packages, categories, blocklist, ads approval, payouts report |
| **moderator** | Review team | `/next`, approve, edit, reject, ban, takedown, view stats |
| **advertiser** | Anyone who starts `/advertise` | Create and pay for ads, view their own ad reports |
| **user** | Everyone | Submit, comment, report, delete own confession |

Roles are stored in the `staff` table, never in code. Assigning someone:

```
/staff add            → bot replies with a one-time invite link (t.me/Bot?start=inv_XXXX), valid 24h
                        the person taps it → they are added with the chosen role
/staff add @username  → works if that user has already started the bot (bot resolves the username from its own cache)
/staff list · /staff role <n> moderator|admin · /staff remove <n>
```
Invite links are the reliable path because bots can't look up arbitrary usernames. Each staff row stores the real Telegram ID (needed to DM them and check permissions); staff are not anonymous to each other, which is the right trade-off.

Permission check is a decorator: `@require("moderator")` on every handler. Unknown users calling staff commands get silence, not an error (don't reveal command existence).

---

## 3. Data model

SQLite, WAL mode, `busy_timeout=5000`, migrations numbered in `migrations/` and applied on startup.

### 3.1 Identity and content
```sql
CREATE TABLE users (
  author        TEXT PRIMARY KEY,   -- HMAC-SHA256(HMAC_SECRET, telegram_id)
  chat_ref      BLOB,               -- Fernet(telegram_id); used only to DM the user (notifications/receipts); NULL if NOTIFY=0
  lang          TEXT DEFAULT 'en',
  last_submit   INT DEFAULT 0, last_comment INT DEFAULT 0,
  strikes       INT DEFAULT 0, banned_until INT DEFAULT 0,   -- 0 = not banned, -1 = permanent
  created       INT
);

CREATE TABLE submissions (                 -- pending review
  id INTEGER PRIMARY KEY, author TEXT, category_id INT, text TEXT, text_hash TEXT UNIQUE,
  risk_hits TEXT,                           -- JSON list of risk words matched (precomputed)
  priority INT DEFAULT 0,                   -- 0 normal, 1 flagged-first
  created INT, claimed_by INT, claimed_at INT
);

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY, kind TEXT CHECK(kind IN ('confession','ad','system')),
  author TEXT, category_id INT, text TEXT, media_file_id TEXT, buttons TEXT,  -- buttons = JSON
  ref_id INT,                               -- ad id for ads
  not_before INT DEFAULT 0, approved_by INT, approved_at INT, attempts INT DEFAULT 0
);

CREATE TABLE posts (
  number INTEGER PRIMARY KEY, kind TEXT, channel_msg_id INT, author TEXT, category_id INT,
  posted_at INT, comment_count INT DEFAULT 0, deleted INT DEFAULT 0, deleted_reason TEXT
);

CREATE TABLE comments (
  id INTEGER PRIMARY KEY, number INT, author TEXT, alias TEXT, reply_to INT,
  text TEXT, created INT, reports INT DEFAULT 0, hidden INT DEFAULT 0
);
CREATE INDEX ix_comments ON comments(number, hidden, id);
CREATE TABLE comment_reports (comment_id INT, reporter TEXT, PRIMARY KEY(comment_id, reporter));
```

### 3.2 Configuration (the "no code edits" layer)
```sql
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, type TEXT, description TEXT, updated_by INT, updated_at INT);
-- seeded on first run, e.g.:
-- submit_cooldown_sec=600 int · min_len=20 · max_len=2000 · post_interval_sec=120
-- max_ads_per_day=3 · min_posts_between_ads=6 · comments_enabled=1 · notify_authors=1
-- claim_timeout_sec=600 · ban_strikes=3 · queue_alert_threshold=200 · submissions_open=1
-- rules_text (text) · welcome_text · received_text · ad_rules_text · ad_footer="📢 Sponsored"

CREATE TABLE categories (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, label TEXT, hashtag TEXT,
                         enabled INT DEFAULT 1, sort INT DEFAULT 0);

CREATE TABLE wordlist (word TEXT, kind TEXT CHECK(kind IN ('block','risk')), added_by INT, added_at INT,
                       PRIMARY KEY(word, kind));

CREATE TABLE staff (user_id INTEGER PRIMARY KEY, role TEXT, display TEXT, added_by INT, added_at INT, active INT DEFAULT 1);
CREATE TABLE invites (code TEXT PRIMARY KEY, role TEXT, created_by INT, expires INT, used_by INT);

CREATE TABLE audit (id INTEGER PRIMARY KEY, actor INT, action TEXT, target TEXT, detail TEXT, at INT);
```
`settings` is read through a tiny cached accessor (`cfg("post_interval_sec")`) with a 30-second TTL, so changes apply without a restart and without hammering the DB.

### 3.3 Ads and payments
```sql
CREATE TABLE packages (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, description TEXT,
  price_etb INT, posts INT DEFAULT 1, pin_hours INT DEFAULT 0, bot_banner_days INT DEFAULT 0,
  max_chars INT DEFAULT 600, allow_media INT DEFAULT 1, allow_button INT DEFAULT 1,
  enabled INT DEFAULT 1, sort INT DEFAULT 0
);

CREATE TABLE payout_accounts (            -- where advertisers pay; shown in the pay screen
  id INTEGER PRIMARY KEY, bank TEXT,       -- verify.et bank code: telebirr|cbe|cbebirr|boa|awash|dashen|...
  account TEXT, holder TEXT, enabled INT DEFAULT 1, sort INT DEFAULT 0
);

CREATE TABLE advertisers (id INTEGER PRIMARY KEY, user_id INT, name TEXT, phone TEXT, created INT, blocked INT DEFAULT 0);

CREATE TABLE ads (
  id INTEGER PRIMARY KEY, advertiser_id INT, package_id INT,
  text TEXT, media_file_id TEXT, button_text TEXT, button_url TEXT,
  price_etb INT,                           -- snapshot of package price at order time
  status TEXT CHECK(status IN ('draft','awaiting_payment','verifying','paid','needs_review','approved',
                                'scheduled','live','done','rejected','refund_due','cancelled')),
  reject_reason TEXT, posts_left INT, scheduled_at INT,
  created INT, paid_at INT, approved_by INT, approved_at INT
);

CREATE TABLE payments (
  id INTEGER PRIMARY KEY, ad_id INT, bank TEXT, reference TEXT, phone TEXT,
  idem_key TEXT UNIQUE, request_id TEXT,  -- verify.et ids
  status TEXT CHECK(status IN ('submitted','queued','verified','failed','mismatch','duplicate','manual_ok','manual_no')),
  amount INT, sender_name TEXT, receiver_match INT, confirmed_before INT,
  raw TEXT, created INT, completed_at INT
);
CREATE UNIQUE INDEX ux_payments_ref ON payments(bank, reference) WHERE status IN ('verified','manual_ok');

CREATE TABLE ad_posts (id INTEGER PRIMARY KEY, ad_id INT, post_number INT, pinned_until INT, views_24h INT);
CREATE TABLE banners (id INTEGER PRIMARY KEY, ad_id INT, text TEXT, url TEXT, starts INT, ends INT);
```

### 3.4 Retention
| Table | Rule |
|---|---|
| `submissions.text` | row deleted on approve/reject |
| `outbox.text` | row deleted after successful post |
| `comments` | kept; hidden ones purged after 90 days |
| `payments.raw` | kept 1 year (dispute evidence), then nulled |
| `audit` | 180 days |
| `invites` | deleted on use or expiry |

---

## 4. Code layout

```
confessbot/
├── app.py                 # builds Application, registers modules, starts aiohttp + webhook
├── config.py              # .env loader (secrets only) + cfg() settings accessor
├── db.py                  # connection, migrations runner, helpers
├── security.py            # author_hash(), encrypt/decrypt chat_ref, require(role) decorator
├── textfilter.py          # normalize(), link/phone/mention detection, block + risk matching
├── i18n.py                # strings in en/am from strings/*.json (fallback to en)
├── modules/               # each exposes register(app) and is listed in MODULES
│   ├── core.py            # /start, /help, /rules, language picker
│   ├── submit.py          # category → text → preview → submit
│   ├── review.py          # /next, claim, approve/edit/reject/ban callbacks, checklist
│   ├── comments.py        # deep-link threads, aliases, replies, reports
│   ├── staff.py           # /staff add|list|role|remove, invites
│   ├── settings.py        # /settings menu (inline buttons) + /set key value
│   ├── catalog.py         # /categories, /packages, /payout_accounts CRUD menus
│   ├── wordlist.py        # /block /risk /unblock /words
│   ├── ads.py             # /advertise flow for advertisers
│   ├── ads_admin.py       # /ads queue, approve/reject, schedule, reports
│   ├── payments.py        # pay screen, receipt submission, verify.et client, webhook handler
│   └── stats.py           # /stats, /top, /mediakit
├── jobs/
│   ├── poster.py          # outbox → channel, ad spacing, pin/unpin
│   ├── housekeeping.py    # release stale claims, expire bans, purge, counters
│   └── backup.py          # nightly .backup → age → backup channel
├── web/
│   └── server.py          # aiohttp routes: POST /tg (Telegram), POST /webhooks/verify, GET /healthz
├── migrations/0001_init.sql …
├── strings/en.json, am.json
├── requirements.txt       # python-telegram-bot[webhooks,job-queue]>=21, aiohttp, httpx, cryptography
├── .env.example
└── deploy/ confessbot.service, Caddyfile.snippet, backup-restore.md
```

**Modularity rule:** a module may import `db`, `security`, `textfilter`, `config`, `i18n`; modules never import each other. Cross-module needs go through the DB (e.g. ads module writes to `outbox`; poster reads it). Adding a feature = adding a file and one line in `MODULES`.

Feature flags live in `settings` (`comments_enabled`, `ads_enabled`, `submissions_open`). Turning a module off is a setting, not a deploy.

---

## 5. Core flows

### 5.1 Submit
1. `/start` → welcome (from settings) + buttons: **✍️ Confess · 💬 Browse recent · 📢 Advertise · 📜 Rules**
2. Category keyboard (from `categories` where enabled, ordered by `sort`).
3. User sends text. Checks in order, each with its own message: banned → cooldown → submissions_open → length → link/mention/phone → blocklist → duplicate (`text_hash` over normalized text).
4. Preview with the hashtag → **Submit / Edit / Cancel**.
5. Insert into `submissions` with `risk_hits` precomputed. Reply with `received_text`.

### 5.2 Review (manual, always)
1. Moderator sends `/next` (or taps **Next** on the previous card).
2. Atomic claim: `UPDATE submissions SET claimed_by=?, claimed_at=? WHERE id = (SELECT id FROM submissions WHERE claimed_by IS NULL ORDER BY priority DESC, id LIMIT 1) RETURNING *`. SQLite handles this safely in one statement.
3. Card shows: category, text, **risk words highlighted** (wrapped in ⚠️), author strike count, queue size, and the checklist line: *"Names a person? Threat/blackmail? Sexual about a real person? → Reject"*.
4. Buttons: **✅ Approve · ✏️ Edit · ❌ Reject · 🚫 Ban (reason) · ⏭ Skip**
   - Edit: moderator replies with the new text; card refreshes; approve again.
   - Reject: optional reason keyboard (names / explicit / spam / unclear / other). Author is notified with the reason if notify is on.
   - Ban: adds a strike; `ban_strikes` strikes → ban for `ban_days` (settings); admins can `/unban`.
5. Approve → move to `outbox`, delete from `submissions`, write `audit`.
6. Housekeeping job releases claims older than `claim_timeout_sec`.

Two moderators can never get the same item; nothing is lost if one disappears.

### 5.3 Poster
Runs every 15 seconds; posts at most one item if `now - last_post >= post_interval_sec`.
- Choose next: an ad if (ads_today < max_ads_per_day AND posts_since_last_ad >= min_posts_between_ads AND an ad is `scheduled` with `not_before <= now`); otherwise the oldest confession.
- Confession format (plain text, no parse_mode on user text; the header uses an entity offset so the number is bold without parsing user text):
  ```
  Confession #1234
  <text>
  #Relationship
  [💬 Comments (0)]   ← url button t.me/Bot?start=c_1234
  ```
- Ad format: `📢 Sponsored` footer, optional photo, optional URL button; pin if `pin_hours > 0`, record `pinned_until`.
- On `RetryAfter` sleep that long; on other errors increment `attempts`, alert admin group after 5.
- After posting: insert `posts`, delete outbox row, notify author ("Live as #1234") via `chat_ref`.

### 5.4 Comments
- Deep link `start=c_1234` opens the thread: last 10 comments (paged), **Write comment**, **Report** per comment.
- Alias: `"Anon " + (1 + count of distinct authors in thread before this one)`; the confession's author shows as **OP**. Alias mapping is `HMAC(number, author)` → stable within thread, different across threads.
- Comments pass the same text filters, go live immediately, and are rate-limited (`comment_cooldown_sec`).
- `report_hide_threshold` (default 3) distinct reporters hides the comment and pings the admin group.
- Comment counter on the channel button refreshed at most every 5 minutes per post, only for posts with new activity (`editMessageReplyMarkup`, cheap and rate-safe).

### 5.5 Takedown and deletion
- `/takedown 1234 <reason>` (moderator): deletes the channel message, marks `posts.deleted`, hides the thread, audits.
- `/delete 1234` (user): works only if `author_hash` matches; same effect.
- Public takedown request: **📜 Rules → Request takedown** asks for the post number and a short reason, forwards to the admin group with a **Take down** button.

---

## 6. Ads and payments with Verify.et

Verify.et does not move money. Advertisers pay **directly into your Telebirr/CBE/other account**, send the receipt reference, and the bot asks Verify.et to confirm the receipt is real, the right amount, paid to **your** account, and not already used. That keeps costs near zero: no gateway fees, only Verify.et verification credits.

### 6.1 Setup (once)
1. Create a Verify.et account, add your receiving accounts under **Bank accounts** (this enables settlement matching), buy verification credits.
2. Create an API key with `verification: read, write`. Store as `VERIFY_API_KEY` in `.env`.
3. Register a webhook endpoint `https://yourdomain/webhooks/verify`, save the `whsec_` secret as `VERIFY_WEBHOOK_SECRET`. The bot's endpoint replies `204` to the test ping (`X-Webhook-Test: true`).
4. Add the same accounts in the bot: `/payout_accounts add telebirr 09xxxxxxxx "Kuru Labs"`.

### 6.2 Advertiser flow
```
📢 Advertise
 → package list (from `packages` enabled, with price, posts, pin hours)
 → send ad text (≤ max_chars) → optional photo → optional button text + URL
 → preview + ad_rules_text → [Place order]
 → ad.status = awaiting_payment; show pay screen:
      "Pay 1,500 ETB to one of:
       • Telebirr 09xx xxx xxx (Kuru Labs)
       • CBE 1000xxxxxxx (Kuru Labs)
       Then send me the transaction/reference number (or paste the SMS)."
      [I have paid ▸ enter reference] [Cancel order]
 → user sends reference (and phone if CBE Birr; bot asks only what that bank needs)
 → bot calls Verify.et (6.3)
 → verified ✓ → status paid → "Thanks! Your ad is in review; usually within 24h."
 → admin approves content → scheduled → poster publishes → done → 24h report to advertiser
```
Orders in `awaiting_payment` expire after `ad_payment_timeout_hours` (default 48).

### 6.3 Verification call
```python
POST https://verify.et/api/verify?waitMs=5000
x-api-key: VERIFY_API_KEY
Idempotency-Key: f"ad-{ad_id}-{payment_id}"
{
  "bank": "telebirr",                       # from the payout account the user picked, or omitted to use smart routing
  "transactionNumber": "<reference>",       # field name per bank table (reference / referenceNumber / receiptNumber+phone / referenceNumber+accountSuffix)
  "settlementAccount": "<payout account>"   # forces receiver matching
}
```
Handling:
| Result | Action |
|---|---|
| 200, `verified: true` | Run checks in 6.4 |
| 202 queued | store `request_id`, status `queued`; wait for webhook; poll `GET /api/verify/{id}` after 60 s as fallback |
| 402 `INSUFFICIENT_CREDITS` | status `needs_review`, alert owner "Verify.et credits exhausted"; advertiser told "we'll confirm manually" |
| 422 invalid payload | ask user to re-enter the reference (max 3 tries, then `needs_review`) |
| 429 / 503 | retry with backoff (30 s, 2 m, 8 m) using the same Idempotency-Key |
| `verified: false` / not found | status `failed`; tell user; allow 3 attempts |

Receipt images are **not** sent to Verify.et at first (image credits cost extra); users type the reference. Add image support later as a package option if advertisers struggle.

### 6.4 Acceptance checks (all must pass → `paid`)
1. `verified == true`
2. `amount >= ad.price_etb` (overpayment allowed; underpayment → `mismatch`, tell the user the difference)
3. `settlementAccountMatch.matched == true` and `ambiguous == false` (money reached **your** account)
4. `confirmationHistory.confirmedBefore == false` **and** no existing `verified` payment with the same `(bank, reference)` (unique index) → otherwise `duplicate`
5. `timestamp` within the last `payment_max_age_days` (default 7)

Anything failing 2–5 → status `needs_review`, admin sees it in `/ads` with the Verify.et data and buttons **Accept manually / Decline**. Admin override writes `manual_ok|manual_no` and an audit row.

### 6.5 Webhook endpoint
`POST /webhooks/verify`
1. Read raw body. Compute `HMAC_SHA256(secret, f"{timestamp}.{raw_body}")`, compare constant-time against each `sha256=` value in `X-Webhook-Signature` (two values possible during rotation). Reject on mismatch or if timestamp older than 5 minutes.
2. If `X-Webhook-Test: true` → `204`.
3. Look up `payments.request_id`; ignore unknown or already terminal (idempotent on event `id`).
4. Return `204` immediately, then process (same acceptance checks) in a background task.

### 6.6 Admin ad tools
- `/ads` → queue of `paid` + `needs_review` ads: preview, payment summary (amount, sender name, bank, matched ✓), **Approve / Reject (reason) / Schedule at…**
- `/packages` menu: list · add · edit field · enable/disable · reorder. Prices change without touching paid orders (price is snapshotted).
- `/payout_accounts` menu: add/remove/toggle receiving accounts (owner only).
- `/ad_report <id>` and automatic 24-hour report to the advertiser: views (from `getChat` message view count via channel post forward trick is unreliable; use the number Telegram shows when the bot fetches the post via `copyMessage` to a private log channel, or simply report channel subscribers + the post link). Keep expectations honest: "views" are best-effort.
- `/revenue [month]` → totals by package and bank from `payments`.
- Refunds are manual (bank transfer); `refund_due` status keeps the list visible until the owner marks it done.

### 6.7 Costs
| Item | Cost |
|---|---|
| Verify.et credits | pay-as-you-go per verification; check `/api/credits/packages` for the current price list. Budget: roughly one credit per ad sold plus ~30 % for retries. |
| Gateway fees | none, since money goes straight to your account |
| Domain | already owned |

---

## 7. Settings management (nothing hard-coded)

`/settings` opens a paginated inline menu grouped by area: **Submissions · Posting · Comments · Moderation · Ads · Texts**. Tap a key → the bot shows its description, current value and type → send the new value → validated (int ranges, 0/1, non-empty text) → saved with `updated_by` → audit → confirmation. `/set key value` is the fast path for admins who know the key.

Texts (`rules_text`, `welcome_text`, `received_text`, `ad_rules_text`) accept Amharic and English; `/settings` → **Texts** → pick → send the new text. Pinned rules post can be regenerated with `/post_rules`.

Everything in `categories`, `packages`, `payout_accounts`, `wordlist`, `staff` has its own CRUD menu. The only things that require a server login are secrets in `.env`.

`/export settings` dumps all config tables to JSON; `/import` restores them (handy for a second campus later).

---

## 8. Security

| Area | Measure |
|---|---|
| Identity | `author = HMAC(HMAC_SECRET, id)`; `chat_ref` Fernet-encrypted with a separate `FERNET_KEY`; both keys only in `.env` (600, owner `confessbot`) |
| Staff | Role check on every command and callback; callback data includes a short HMAC so forged button payloads are rejected |
| Telegram webhook | URL contains a random path token; `secret_token` set in `setWebhook` and checked on each request (`X-Telegram-Bot-Api-Secret-Token`) |
| Verify.et webhook | Signature + timestamp window + idempotent event ids |
| Output | User text always sent as plain text (entities only for the header); ad URLs validated (`https://` only, no `t.me/joinchat` spam, no shorteners unless admin allows) |
| Process | Non-root user, systemd hardening, `MemoryMax`, no shell access from the bot |
| Logging | Errors and audit only; never text, IDs or references in logs |
| Backups | `age`-encrypted; private key stored offline (your laptop + password manager) |
| Abuse | Cooldowns on submit, comment, report, advertise; strike-based bans; `submissions_open=0` kill switch |
| Payments | Amount, receiver, duplicate and age checks; unique index on verified references; admin override always audited |

---

## 9. Deployment on your domain

Assumes the domain already points to your VPS and Caddy is running (it is, for Hermes).

### 9.1 Files
```
/opt/confessbot/
├── app/        # git checkout
├── venv/
├── data/       # confess.db, confess.db-wal
└── .env        # secrets, chmod 600
```

### 9.2 `.env`
```
BOT_TOKEN=
OWNER_ID=
CHANNEL_ID=-100…
ADMIN_GROUP_ID=-100…
BACKUP_CHANNEL_ID=-100…
PUBLIC_URL=https://confess.yourdomain.et
WEBHOOK_PATH=/tg/<random 32 hex>
TG_WEBHOOK_SECRET=<random 32 hex>
LISTEN=127.0.0.1:8081
HMAC_SECRET=<openssl rand -hex 32>
FERNET_KEY=<Fernet.generate_key()>
VERIFY_API_KEY=VERIFY_BANK_ET_…
VERIFY_WEBHOOK_SECRET=whsec_…
AGE_RECIPIENT=age1…
DB_PATH=/opt/confessbot/data/confess.db
```

### 9.3 Caddy
```
confess.yourdomain.et {
    encode gzip
    @allowed path /tg/* /webhooks/* /healthz
    handle @allowed {
        reverse_proxy 127.0.0.1:8081
    }
    handle {
        respond "ok" 200
    }
}
```
Caddy handles TLS automatically. Only the three paths reach the app.

### 9.4 systemd
```ini
[Unit]
Description=Confession bot
After=network-online.target caddy.service
Wants=network-online.target

[Service]
User=confessbot
Group=confessbot
WorkingDirectory=/opt/confessbot/app
EnvironmentFile=/opt/confessbot/.env
ExecStart=/opt/confessbot/venv/bin/python app.py
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ReadWritePaths=/opt/confessbot/data
MemoryMax=400M
CPUQuota=80%

[Install]
WantedBy=multi-user.target
```

### 9.5 Install steps
```bash
adduser --system --group --home /opt/confessbot confessbot
apt install -y python3-venv sqlite3 age
sudo -u confessbot bash -c 'cd /opt/confessbot && git clone <repo> app && python3 -m venv venv && venv/bin/pip install -r app/requirements.txt && mkdir -p data'
cp app/.env.example /opt/confessbot/.env && chmod 600 /opt/confessbot/.env && chown confessbot: /opt/confessbot/.env
# fill .env
cat app/deploy/Caddyfile.snippet >> /etc/caddy/Caddyfile && systemctl reload caddy
cp app/deploy/confessbot.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now confessbot
curl -s https://confess.yourdomain.et/healthz   # {"ok":true,"queue":0}
```
On startup the app runs migrations, seeds defaults, calls `setWebhook(PUBLIC_URL+WEBHOOK_PATH, secret_token=…, allowed_updates=[message,callback_query,my_chat_member])`, and DMs the owner "started, version X".

### 9.6 Updates and rollback
```bash
sudo -u confessbot git -C /opt/confessbot/app pull
sudo -u confessbot /opt/confessbot/venv/bin/pip install -q -r /opt/confessbot/app/requirements.txt
systemctl restart confessbot
# rollback: git checkout <previous tag> && systemctl restart confessbot  (migrations are forward-only; keep them additive)
```
Tag releases (`v1.0.0`). Migrations must be additive (new tables/columns), never destructive, so rollback is always safe.

### 9.7 Backups and restore
- Job at 03:00: `sqlite3 .backup` → `age -r $AGE_RECIPIENT` → upload to backup channel → delete temp files; keep the last 30 in the channel (older messages deleted by the job).
- Restore drill (monthly, on your laptop): download → `age -d` → `sqlite3 confess.db "PRAGMA integrity_check"` → `SELECT count(*) FROM posts`.
- Disaster recovery to a new server: install steps + restore the file into `data/` + same `.env` (keep a copy of `.env` in your password manager). Expected downtime: under 15 minutes.

### 9.8 Monitoring
- `/healthz` returns `{ok, queue, last_post_age, last_update_age}`; a free **healthchecks.io** or **UptimeRobot** check hits it every 5 minutes.
- Bot posts to admin group: crash loops, queue > `queue_alert_threshold`, poster errors, Verify.et credits low, backup failure.
- `/stats`: queue, claimed, posted today/7d, comments today, pending ads, revenue this month, moderator counts (approvals per person today).

---

## 10. Growth plan while keeping approvals manual

The bottleneck is **human review time**, not servers. A moderator reviews about 2–4 items per minute; a 4-person team doing 30 minutes each per day clears roughly 300–500 confessions. Plan around that.

### 10.1 Throughput levers (all settings, no code)
| Lever | Setting | Effect |
|---|---|---|
| Raise the cooldown | `submit_cooldown_sec` | Fewer, better submissions when the queue is long |
| Daily cap per user | `submit_daily_cap` | Stops a few people flooding |
| Queue cap | `queue_soft_cap` → reply "queue is full, try tomorrow" | Protects moderators from an unbounded backlog |
| Priority | `risk_hits` non-empty → `priority=1` | Dangerous items are reviewed first |
| Shorter content | `max_len` | Faster reviews |
| Office hours | `submissions_open` schedule (`open_hours=08-23`) | Queue builds only when reviewers are around |
| Auto-expire | `submission_ttl_hours` (e.g. 72) | Stale items drop with a polite notice; the queue can't grow forever |

### 10.2 Moderator tooling as volume grows
- **Batch mode** `/next 5`: five compact cards at once with one-tap approve/reject each.
- **Reviewer shifts**: `/shift on|off`; `/stats` shows who is on; the bot nudges the admin group when nobody is on and the queue is above threshold.
- **Two-step for flagged items**: anything with `risk_hits` needs an **admin** approve, not just a moderator (`flagged_requires_admin=1`).
- **Second opinion**: **🤔 Escalate** button sends the item to the admin group thread.
- **Reviewer quality**: `/audit <moderator>` shows the last 20 decisions; spot-checks by admins.

### 10.3 Infrastructure triggers
| Signal | Change | Cost |
|---|---|---|
| VPS RAM/CPU > 70 % sustained or Hermes and the bot compete | Own VPS (Hetzner CX22 / Contabo), copy folder, restore backup, move DNS | ~$4–6/mo |
| > ~30 updates/s sustained (unlikely before 100k+ users) | Already on webhooks; add `concurrent_updates=True`, then a second worker only for comments | $0 |
| DB > 5 GB, or you want a web dashboard, or multiple campuses with shared admin | PostgreSQL on the same VPS; schema ports with minimal changes (replace `INTEGER PRIMARY KEY` autoincrement, JSON columns) | $0 |
| Multiple campuses | Add `campus_id` to content tables; one bot per campus running the same code with a different `.env`; `/export`/`/import` seeds the config | ~$5/campus |
| Verify.et credits > a few thousand ETB/month | Negotiate a plan or add a second provider behind the same `payments` interface | depends |

Telegram limits do not change with your server: ~30 msg/s overall, ~20 msg/min into one channel/group, 1 msg/s per user chat. The outbox and per-user notification throttle keep you inside them.

---

## 11. Build order and acceptance criteria

| Milestone | Scope | Done when |
|---|---|---|
| **M1 Core** (week 1) | db, security, textfilter, core, submit, review, poster, staff, settings | A confession goes submit → `/next` → approve → channel; `/staff add` invite works; `/set post_interval_sec 60` takes effect without restart |
| **M2 Deploy** (week 2) | Caddy, systemd, webhook, backups, healthz, wordlist, takedown, stats | Running on the domain; restore drill passed; healthcheck alerting works; 5 testers for 7 days |
| **M3 Comments** (week 3–4) | comments module, aliases, reports, counter refresh | Threads work; 3 reports hide a comment; counters update |
| **M4 Ads** (week 5–6) | catalog, ads, payments (Verify.et), ads_admin, revenue | A real 10 ETB test payment verifies end-to-end; a reused reference is rejected; wrong-account receipt goes to `needs_review`; package edits reflected immediately |
| **M5 Scale tooling** (week 7–8) | batch review, shifts, escalation, caps, open hours, `/export` | Queue of 500 cleared by 4 moderators in one evening |

Testing: `pytest` for `textfilter`, `security`, acceptance checks and the claim query; a `TEST_MODE=1` flag that points the bot at a test channel and a Verify.et sandbox (use their `POST /api/verify/test-webhook` to simulate deliveries).

---

## 12. Operations runbook

| Situation | What to do |
|---|---|
| Bot not responding | `systemctl status confessbot`, `journalctl -u confessbot -n 100`; check `/healthz`; `systemctl restart confessbot` |
| Queue exploding | `/set submit_cooldown_sec 1800`, `/set queue_soft_cap 300`, ping moderators; worst case `/set submissions_open 0` |
| Harmful post got through | `/takedown <n> <reason>` (removes in seconds); `/ban` the author hash; add words to `/risk` |
| Verify.et down or out of credits | Ads fall into `needs_review` automatically; verify manually in the bank app; `Accept manually` |
| Advertiser says they paid but verification failed | `/ads` shows attempts and raw responses; check reference typos; manual override |
| Add a moderator | `/staff add` → send them the link |
| Change prices/packages | `/packages` menu |
| Change rules text | `/settings` → Texts → `rules_text` → `/post_rules` to re-pin |
| Move servers | section 9.7 |
| Rotate a secret | Edit `.env`, `systemctl restart`; for `HMAC_SECRET` note that bans and author links reset (plan it; announce it); Verify.et secret rotation keeps the old one valid for 24 h |

---

## 13. What is deliberately left out (for now)
Web dashboard, PostgreSQL, Docker, Redis, AI moderation, auto-approval, media confessions, multiple languages beyond en/am strings, click tracking via redirect domain. Each has a trigger in section 10 or is a one-module addition thanks to the layout in section 4.
