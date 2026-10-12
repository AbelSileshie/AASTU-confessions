"""Payments service (shared layer): Verify.et client, acceptance checks, reference entry, webhook processing.

Verify.et confirms a receipt is real, paid to OUR account, for enough money
and not reused. Money never flows through Verify.et.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

import db
from common import alert_admins, user_lang
from config import cfg
from i18n import t
from security import author_hash

log = logging.getLogger(__name__)

# Per-bank request fields (verify.et/docs/api → Bank Fields)
BANK_FIELDS: dict[str, dict[str, Any]] = {
    "telebirr": {"ref": "transactionNumber", "extra": None, "label": "Telebirr"},
    "mpesa": {"ref": "transactionNumber", "extra": None, "label": "M-Pesa"},
    "cbe": {"ref": "referenceNumber", "extra": ("accountSuffix", 8), "label": "CBE"},
    "boa": {"ref": "referenceNumber", "extra": ("accountSuffix", 5), "label": "Bank of Abyssinia"},
    "cbebirr": {"ref": "receiptNumber", "extra": ("phone", 10), "label": "CBE Birr"},
    "dashen": {"ref": "referenceNumber", "extra": None, "label": "Dashen"},
    "awash": {"ref": "referenceNumber", "extra": None, "label": "Awash"},
    "siinqee": {"ref": "referenceNumber", "extra": None, "label": "Siinqee"},
    "kaafiebirr": {"ref": "referenceNumber", "extra": None, "label": "Kaafi Ebirr"},
}
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{4,39}$")
RETRY_BACKOFF_SEC = (30, 120, 480)


def clean_reference(raw: str) -> str | None:
    """Extract a plausible reference token from typed text or pasted SMS."""
    raw = raw.strip()
    if _REF_RE.match(raw):
        return raw.upper()
    tokens = re.findall(r"\b[A-Z0-9][A-Z0-9\-_]{6,39}\b", raw.upper())
    tokens = [tk for tk in tokens if any(ch.isdigit() for ch in tk) and any(ch.isalpha() for ch in tk)] or tokens
    return tokens[0] if tokens else None


def clean_extra(bank: str, raw: str) -> str | None:
    spec = BANK_FIELDS[bank]["extra"]
    if not spec:
        return ""
    digits = re.sub(r"\D", "", raw)
    kind, n = spec
    if kind == "phone":
        if digits.startswith("251"):
            digits = "0" + digits[3:]
        return digits if len(digits) == 10 and digits.startswith("09") else None
    return digits if len(digits) == n else None


def required_extra(bank: str, reference: str) -> tuple[str, int] | None:
    spec = BANK_FIELDS[bank]["extra"]
    if bank == "cbe" and not reference.upper().startswith("FT"):
        return None
    return spec


def build_request(bank: str, reference: str, extra: str, settlement_account: str) -> dict:
    spec = BANK_FIELDS[bank]
    body = {"bank": bank, spec["ref"]: reference, "settlementAccount": settlement_account}
    extra_spec = required_extra(bank, reference)
    if extra_spec:
        body[extra_spec[0]] = extra
    return body


# ---------------------------------------------------------------------------
# Verify.et client
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifyOutcome:
    kind: str  # completed | queued | invalid | no_credits | retry | error
    data: dict | None = None
    request_id: str | None = None
    raw: str = ""


class VerifyClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def submit(self, body: dict, idem_key: str) -> VerifyOutcome:
        headers = {"x-api-key": self.api_key, "Idempotency-Key": idem_key, "Content-Type": "application/json"}
        try:
            r = await self._client.post(f"{self.base_url}/api/verify", params={"waitMs": 5000}, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return VerifyOutcome("retry", raw=type(exc).__name__)
        return parse_submit_response(r.status_code, r.text)

    async def status(self, request_id: str) -> VerifyOutcome:
        try:
            r = await self._client.get(f"{self.base_url}/api/verify/{request_id}", headers={"x-api-key": self.api_key})
        except httpx.HTTPError as exc:
            return VerifyOutcome("retry", raw=type(exc).__name__)
        if r.status_code != 200:
            return VerifyOutcome("retry" if r.status_code in (429, 503) else "error", raw=r.text[:500])
        try:
            data = r.json().get("data") or {}
        except ValueError:
            return VerifyOutcome("error", raw=r.text[:500])
        if data.get("processingStatus") == "completed":
            return VerifyOutcome("completed", data=data, request_id=request_id, raw=r.text[:4000])
        return VerifyOutcome("queued", request_id=request_id, raw=r.text[:500])


def parse_submit_response(status_code: int, text: str) -> VerifyOutcome:
    """Pure mapping of HTTP status/body → outcome."""
    raw = text[:4000]
    if status_code == 402:
        return VerifyOutcome("no_credits", raw=raw)
    if status_code in (400, 422):
        return VerifyOutcome("invalid", raw=raw)
    if status_code in (429, 503):
        return VerifyOutcome("retry", raw=raw)
    if status_code not in (200, 202):
        return VerifyOutcome("error", raw=raw)
    try:
        payload = json.loads(text)
    except ValueError:
        return VerifyOutcome("error", raw=raw)
    request_id = payload.get("requestId")
    if status_code == 202:
        return VerifyOutcome("queued", request_id=request_id, raw=raw)
    data = payload.get("data")
    first = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    return VerifyOutcome("completed", data=first, request_id=request_id, raw=raw)


# ---------------------------------------------------------------------------
# Acceptance checks (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Acceptance:
    status: str  # verified | failed | mismatch | duplicate | needs_review
    reason: str = ""
    amount: int | None = None
    sender: str = ""
    receiver_match: bool | None = None
    confirmed_before: bool | None = None


def _parse_amount(v: Any) -> int | None:
    try:
        return round(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> int | None:
    if not v:
        return None
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _norm_name(s: str) -> str:
    """Script-agnostic comparison key: NFKC, casefold, keep letters/digits only."""
    import unicodedata

    return "".join(ch for ch in unicodedata.normalize("NFKC", s or "").casefold() if ch.isalnum())


def evaluate(data: dict, price_etb: int, now: int, max_age_days: int, expected_holder: str,
             already_used: bool) -> Acceptance:
    """Apply section 6.4 checks. `data` is one Verify.et result object."""
    if not data or not data.get("verified"):
        return Acceptance("failed", "not_verified")
    amount = _parse_amount(data.get("amount"))
    sender = str(data.get("senderName") or "")
    sam = data.get("settlementAccountMatch") or {}
    ch = data.get("confirmationHistory") or {}
    matched = sam.get("matched") if sam else None
    ambiguous = bool(sam.get("ambiguous")) if sam else False
    confirmed_before = ch.get("confirmedBefore") if ch else None

    if amount is None:
        return Acceptance("needs_review", "no_amount", amount, sender, matched, confirmed_before)
    if amount < price_etb:
        return Acceptance("mismatch", "underpaid", amount, sender, matched, confirmed_before)

    if sam:
        if not matched or ambiguous:
            return Acceptance("needs_review", "receiver_mismatch", amount, sender, matched, confirmed_before)
    else:
        # Webhook / status payloads omit settlementAccountMatch → fall back to holder name
        receiver = _norm_name(str(data.get("receiverName") or ""))
        if not receiver or receiver != _norm_name(expected_holder):
            return Acceptance("needs_review", "receiver_unconfirmed", amount, sender, None, confirmed_before)
        matched = True

    if already_used or confirmed_before:
        return Acceptance("duplicate", "reference_reused", amount, sender, matched, confirmed_before)

    ts = _parse_ts(data.get("timestamp") or data.get("completedAt"))
    if ts is not None and now - ts > max_age_days * 86400:
        return Acceptance("needs_review", "too_old", amount, sender, matched, confirmed_before)
    if ts is not None and ts - now > 86400:
        return Acceptance("needs_review", "future_timestamp", amount, sender, matched, confirmed_before)
    return Acceptance("verified", "", amount, sender, matched, confirmed_before)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _client(context) -> VerifyClient:
    client = context.bot_data.get("verify_client")
    if client is None:
        env = context.bot_data["env"]
        client = VerifyClient(env.verify_base_url, env.verify_api_key)
        context.bot_data["verify_client"] = client
    return client


def record_payment(ad_id: int, bank: str, reference: str, extra: str) -> int:
    pid = db.execute(
        "INSERT INTO payments(ad_id, bank, reference, extra, status, created) VALUES (?,?,?,?,'submitted',?)",
        (ad_id, bank, reference, extra or None, db.now()),
    )
    db.execute("UPDATE payments SET idem_key=? WHERE id=?", (f"ad-{ad_id}-{pid}", pid))
    return pid


def reference_used(bank: str, reference: str, exclude_payment: int) -> bool:
    return db.one("SELECT 1 FROM payments WHERE bank=? AND reference=? AND status IN ('verified','manual_ok') AND id<>?",
                  (bank, reference, exclude_payment)) is not None


def apply_result(payment_id: int, data: dict, raw: str) -> Acceptance:
    """Evaluate a completed verification and persist ad + payment state."""
    p = db.one("SELECT p.*, a.price_etb, a.payout_account_id FROM payments p JOIN ads a ON a.id=p.ad_id WHERE p.id=?", (payment_id,))
    if not p:
        return Acceptance("failed", "unknown_payment")
    late_poll_result = p["status"] == "failed" and p["reason"] in ("poll_timeout", "verify_unavailable")
    if p["status"] not in ("submitted", "queued") and not late_poll_result:
        return Acceptance("failed", "already_applied")
    holder = db.scalar("SELECT holder FROM payout_accounts WHERE id=?", (p["payout_account_id"],), "") or ""
    acc = evaluate(data, p["price_etb"], db.now(), cfg("payment_max_age_days", 7), holder,
                   reference_used(p["bank"], p["reference"], payment_id))
    now = db.now()
    with db.tx():
        current = db.one("SELECT status, reason FROM payments WHERE id=?", (payment_id,))
        if not current:
            return Acceptance("failed", "unknown_payment")
        late_poll_result = current["status"] == "failed" and current["reason"] in ("poll_timeout", "verify_unavailable")
        if current["status"] not in ("submitted", "queued") and not late_poll_result:
            return Acceptance("failed", "already_applied")
        try:
            db.execute(
                "UPDATE payments SET status=?, amount=?, sender_name=?, receiver_match=?, confirmed_before=?, reason=?, raw=?, completed_at=? WHERE id=?",
                (acc.status if acc.status != "needs_review" else "failed", acc.amount, acc.sender[:80],
                 None if acc.receiver_match is None else int(acc.receiver_match),
                 None if acc.confirmed_before is None else int(acc.confirmed_before), acc.reason, raw, now, payment_id),
            )
        except db.sqlite3.IntegrityError:
            acc = Acceptance("duplicate", "reference_reused", acc.amount, acc.sender, acc.receiver_match, acc.confirmed_before)
            db.execute("UPDATE payments SET status='duplicate', reason='reference_reused', raw=?, completed_at=? WHERE id=?",
                       (raw, now, payment_id))
        ad_status = {"verified": "paid", "failed": "awaiting_payment", "mismatch": "needs_review",
                     "duplicate": "awaiting_payment", "needs_review": "needs_review"}[acc.status]
        db.execute("UPDATE ads SET status=?, paid_at=CASE WHEN ?='paid' THEN ? ELSE paid_at END WHERE id=?",
                   (ad_status, ad_status, now, p["ad_id"]))
        db.audit(0, f"payment_{acc.status}", f"ad:{p['ad_id']}", acc.reason)
    return acc


async def verify_payment(context, payment_id: int, schedule_retry: bool = True) -> tuple[str, Acceptance | None]:
    """Submit to Verify.et. Returns (outcome_kind, acceptance_if_completed).
    `schedule_retry=False` is used by retry_job itself so each round spawns at most one follow-up."""
    p = db.one("SELECT p.*, a.payout_account_id FROM payments p JOIN ads a ON a.id=p.ad_id WHERE p.id=?", (payment_id,))
    account = db.one("SELECT account FROM payout_accounts WHERE id=?", (p["payout_account_id"],))
    body = build_request(p["bank"], p["reference"], p["extra"] or "", account["account"] if account else "")
    outcome = await _client(context).submit(body, p["idem_key"])
    if outcome.kind == "completed":
        return "completed", apply_result(payment_id, outcome.data or {}, outcome.raw)
    if outcome.kind == "queued":
        db.execute("UPDATE payments SET status='queued', request_id=?, raw=? WHERE id=?", (outcome.request_id, outcome.raw, payment_id))
        db.execute("UPDATE ads SET status='verifying' WHERE id=?", (p["ad_id"],))
        context.job_queue.run_once(poll_job, 60, data={"payment_id": payment_id, "attempt": 0}, name=f"poll-{payment_id}")
        return "queued", None
    if outcome.kind == "no_credits":
        db.execute("UPDATE payments SET status='failed', reason='no_credits', raw=? WHERE id=?", (outcome.raw, payment_id))
        db.execute("UPDATE ads SET status='needs_review' WHERE id=?", (p["ad_id"],))
        await alert_admins(context, "❗ Verify.et credits exhausted. Ads are falling into manual review.", kind="ads")
        return "no_credits", None
    if outcome.kind == "retry":
        db.execute("UPDATE payments SET status='queued', raw=? WHERE id=?", (outcome.raw, payment_id))
        db.execute("UPDATE ads SET status='verifying' WHERE id=?", (p["ad_id"],))
        if schedule_retry:
            context.job_queue.run_once(retry_job, RETRY_BACKOFF_SEC[0], data={"payment_id": payment_id, "attempt": 0}, name=f"retry-{payment_id}")
        return "retry", None
    db.execute("UPDATE payments SET status='failed', reason=?, raw=? WHERE id=?", (outcome.kind, outcome.raw, payment_id))
    return outcome.kind, None


async def retry_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    p = db.one("SELECT status FROM payments WHERE id=?", (d["payment_id"],))
    if not p or p["status"] != "queued":
        return
    kind, acc = await verify_payment(context, d["payment_id"], schedule_retry=False)
    if kind == "retry":
        attempt = d["attempt"] + 1
        if attempt < len(RETRY_BACKOFF_SEC):
            context.job_queue.run_once(retry_job, RETRY_BACKOFF_SEC[attempt], data={**d, "attempt": attempt})
        else:
            await _give_up(context, d["payment_id"], "verify_unavailable")
    elif kind == "completed":
        await notify_outcome(context, d["payment_id"], acc)


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    p = db.one("SELECT status, request_id FROM payments WHERE id=?", (d["payment_id"],))
    if not p or p["status"] != "queued" or not p["request_id"]:
        return
    outcome = await _client(context).status(p["request_id"])
    if outcome.kind == "completed":
        data = outcome.data or {}
        if _has_poll_result(data):
            acc = apply_result(d["payment_id"], data, outcome.raw)
            await notify_outcome(context, d["payment_id"], acc)
            return
    if d["attempt"] < 5:
        context.job_queue.run_once(poll_job, 60, data={**d, "attempt": d["attempt"] + 1})
    else:
        await _give_up(context, d["payment_id"], "poll_timeout")


def _has_poll_result(data: dict) -> bool:
    if data.get("verified") is False:
        return True
    if data.get("verified") is not True or data.get("amount") is None:
        return False
    return bool(data.get("settlementAccountMatch")) or bool(str(data.get("receiverName") or "").strip())


async def _give_up(context, payment_id: int, reason: str) -> None:
    with db.tx():
        payment = db.one("SELECT ad_id FROM payments WHERE id=? AND status='queued'", (payment_id,))
        if not payment:
            return
        ad_id = payment["ad_id"]
        db.execute("UPDATE payments SET status='failed', reason=? WHERE id=? AND status='queued'", (reason, payment_id))
        db.execute("UPDATE ads SET status='needs_review' WHERE id=?", (ad_id,))
    await alert_admins(context, f"❗ Payment {payment_id} for ad {ad_id} could not be verified ({reason}); manual review.", kind="ads")


async def notify_outcome(context, payment_id: int, acc: Acceptance | None) -> None:
    row = db.one("SELECT a.id ad_id, a.price_etb, adv.user_id FROM payments p JOIN ads a ON a.id=p.ad_id "
                 "JOIN advertisers adv ON adv.id=a.advertiser_id WHERE p.id=?", (payment_id,))
    if not row or not acc or acc.reason == "already_applied":
        return
    msg = outcome_message(user_lang(author_hash(row["user_id"])), acc, row["price_etb"], attempts_left=None)
    try:
        await context.bot.send_message(row["user_id"], msg)
    except TelegramError:
        pass
    if acc.reason == "already_applied":
        return
    if acc.status in ("mismatch", "needs_review"):
        await alert_admins(context, f"🧾 Ad #{row['ad_id']} payment needs review ({acc.reason}). Use /ads.", kind="ads")


def outcome_message(lang: str, acc: Acceptance, price: int, attempts_left: int | None) -> str:
    if acc.status == "verified":
        return t(lang, "ads.verified")
    if acc.status == "mismatch":
        return t(lang, "ads.underpaid", amount=acc.amount, price=price)
    if acc.status == "duplicate":
        return t(lang, "ads.duplicate")
    if acc.status == "needs_review":
        return t(lang, "ads.needs_review")
    return t(lang, "ads.failed", left=attempts_left if attempts_left is not None else 0)


# ---------------------------------------------------------------------------
# Webhook entry (called from web/server.py)
# ---------------------------------------------------------------------------

async def handle_verify_webhook(payload: dict, application: Application) -> bool:
    request_id = payload.get("requestId")
    if not request_id:
        return True
    p = db.one("SELECT id, status, reason FROM payments WHERE request_id=?", (request_id,))
    if not p:
        return False
    late_poll_result = p["status"] == "failed" and p["reason"] in ("poll_timeout", "verify_unavailable")
    if p["status"] != "queued" and not late_poll_result:
        return True
    data = payload.get("data") or {}
    if data.get("processingStatus") not in ("completed", None):
        return True
    acc = apply_result(p["id"], data, json.dumps(payload)[:4000])

    class _Ctx:  # minimal context shim for notify helpers
        bot = application.bot
        bot_data = application.bot_data
        job_queue = application.job_queue

    try:
        await notify_outcome(_Ctx(), p["id"], acc)
    except Exception:
        log.exception("verify payment notification failed")
    return True


