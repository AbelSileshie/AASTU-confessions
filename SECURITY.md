# Security model

Last audited: see git log for the latest `security:` commit. Automated gates: `pytest` (196),
`bandit`, `ruff`, `vulture`, `pip-audit` — all clean.

## Assets and who may touch them

| Asset | Protection |
|---|---|
| Student identity | Never stored. `users.author = HMAC-SHA256(HMAC_SECRET, telegram_id)`. The DM handle is `Fernet(telegram_id)` with a separate key, used only to notify; set `notify_authors=0` to not store it at all. Tests assert no raw id appears in any content table. |
| Staff identity | `staff.user_id` is a raw id **by design** (permission checks, DMs, audit). Staff are not anonymous to each other. |
| Advertiser identity | `advertisers.user_id` raw **by design** (commercial relationship, payment follow-up). Not echoed into group messages. |
| Confession text | Deleted from `submissions` on approve/reject; outbox row deleted after posting. Rejected text lives in `declined` + the 🗂 Declined topic for `declined_ttl_hours` (default 72) then both are deleted. Channel is the archive. |
| Secrets | `.env` only (`chmod 600`, owner `confessbot`), never in git. Startup fails if `HMAC_SECRET` < 32 chars. Logs pass through a redacting formatter (bot tokens, long numeric ids). |
| Payments | Verify.et verifies: real receipt, `amount ≥ price`, paid to **our** registered account, not reused (Verify.et history **and** our unique index), ≤ `payment_max_age_days` old. Anything else → `needs_review`; manual override is audited. Raw Verify.et responses kept 1 year for disputes, then nulled. |

## Trust boundaries

| Boundary | Check |
|---|---|
| Telegram → `POST {WEBHOOK_PATH}` | Random path + `X-Telegram-Bot-Api-Secret-Token` (constant-time), 1 MB cap, malformed → 400. |
| Verify.et → `POST /webhooks/verify` | `HMAC(secret, "{ts}.{body}")` vs `X-Webhook-Signature` (all values, constant-time), ±5 min window, **fails closed when no secret is configured**, idempotent on `X-Webhook-Delivery-Id`, only acts on a `payments.request_id` we created and that is still `queued`. |
| Bot → Verify.et | `x-api-key`, `Idempotency-Key = ad-{ad}-{payment}`, per-bank request validation, retry with backoff on 429/503, 402 → manual review + owner alert. |
| Reverse proxy | Caddy forwards only `/tg/*`, `/webhooks/*`, `/healthz`; everything else 200 "ok". App binds `127.0.0.1`. |
| Inline buttons | `callback_data = "{action}:{payload}:{hmac12}"`; forged/retargeted payloads are rejected before any handler logic. |
| Roles | `@require(role)` on every staff handler; unknown callers get silence. Owner from `OWNER_ID`; admins may create moderators; only the owner creates admins or touches payout accounts. Invite links: 24 h, single use, never demote. |
| Object ownership | `/delete`, 📝 My confessions, Connect accept/decline, pay/cancel/contact buttons and review cards all re-check ownership server-side (IDOR tests). |

## Abuse limits (all settings, live within 30 s)

submit cooldown + daily cap + queue soft cap + open hours · comment cooldown · report cooldown + distinct reporters
· connect cooldown + one pending per thread · `max_open_orders` · payment attempts (3) · contact-admin cooldown
· takedown-request cooldown · strike-based bans · `submissions_open` kill switch.

## Access separation

- Students never see a listing of other people's confessions inside the bot: `💬 Browse recent` was removed; the only
  list is `📝 My confessions` (own author hash). The `menu:browse` action renders the staff monitor for moderators/admins
  and the user's own list for everyone else. Comment threads remain reachable through the channel button (public feature).
- `📰 Recent` (moderators + admins) shows the latest posts with approver, flags and a one-tap take down.

## Connect (contact sharing)

One-way: only the confession author (OP) can ever receive a contact; the commenter never learns who the OP is.
Two-step: commenter accepts → OP must explicitly tap **Show contact**; until then the commenter can revoke and
nothing is revealed. After reveal the commenter can still revoke for `connect_revoke_hours` (the OP is told not to
contact them). OP can withdraw a pending request or decline an accepted one. Every transition is an atomic,
ownership-checked `UPDATE … WHERE status IN (…) RETURNING`, so replays and races cannot double-reveal. The only place a
raw id travels is inside the `text_mention` entity sent to the OP; nothing is persisted.

## Admin group topics

Created automatically at startup and re-checked hourly (`ensure_topics`); the owner is warned once if Topics are off or
the bot lacks *Manage topics*, and alerts fall back to the main chat. A deleted topic is detected on the next failed
send and recreated on the next hourly run.

## Links

- Channel links use `t.me/<username>/<msg>` when the channel is public. If it is private the bot falls back to the
  members-only `t.me/c/…` form **and** attaches a `➕ Join the channel` button using one exported invite link (cached in
  `meta`, refreshed when the channel info is re-fetched every 10 min). The owner is told at startup when the channel is private.
- Bot deep links always use the bot's username (resolved in `post_init`, which `app.py` calls explicitly — PTB's
  `initialize()` alone does not).

## Input handling

- Text: NFKC + zero-width stripped before filters; links/@mentions/phones/blocklist; length caps everywhere.
- Styling is applied only through `MessageEntity` offsets built by `fmt.Rich` (UTF-16 aware). User/advertiser/setting text
  only ever goes through `.text()` or as a blockquote *container*; `parse_mode` is never used, so nothing a user types can
  be interpreted as markup. Entities are truncated with the text so none can point past the end.
- Humans are shown display names, never raw Telegram ids (`staff_name` falls back to role + short tag; advertisers by name).
- URLs in ads: `https://` only, hostname required, no Telegram invite links, no shorteners unless allowed, ≤ 500 chars.
- SQL: parameterised throughout; the only interpolated identifiers are whitelisted constants (annotated `nosec B608`).
- `/import`: owner only, ≤ 2 MB JSON, whitelisted tables and columns only.
- "Type your next message" states (edit, setting value, hub input, admin reply): expire after 10 min, bound to the chat
  where they started, cleared by any navigation.

## Process hardening

Non-root `confessbot` user, systemd `ProtectSystem=strict`, `NoNewPrivileges`, empty capability set,
`ReadWritePaths=/opt/confessbot/data` only, `MemoryMax`/`CPUQuota`. Backups `age`-encrypted to a public key whose
private half never touches the server.

## Known accepted risks

- `/healthz` is unauthenticated and reveals queue/outbox counts (behind Caddy; low sensitivity).
- Callback signatures are 48 bits; brute force requires one Telegram callback per attempt and is rate-limited by Telegram.
- Rotating `HMAC_SECRET` unlinks every author from their posts/bans (announce before doing it).
- Declined confessions are readable by every staff member in the group for up to `declined_ttl_hours`.

## Re-audit

```bash
.venv/bin/python -m pytest -q
.venv/bin/bandit -q -r . -x ./.venv,./tests
.venv/bin/ruff check --select F,E9,B006,B008,B904 --exclude .venv --exclude tests .
.venv/bin/pip-audit -r requirements.lock
```
