"""Text normalisation and content checks (pure functions).

Normalisation targets Amharic written in Latin letters: lowercase, collapse
repeated letters, map common swaps so one blocklist entry catches many
spellings.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|\b[\w-]+\.(?:com|net|org|et|io|me|ly|co|app|info|xyz)\b)", re.I)
_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,}")
_PHONE_RE = re.compile(r"(?:\+?251|0)\s?9\s?\d(?:[\s\-.]?\d){7}")
_REPEAT_RE = re.compile(r"(.)\1+")
_WS_RE = re.compile(r"\s+")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

_SWAPS = (("ph", "f"), ("q", "k"), ("ck", "k"), ("sh", "x"))


def strip_invisible(text: str) -> str:
    return _INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", text))


def normalize(text: str) -> str:
    """Canonical lowercase form used for matching and dedupe."""
    t = strip_invisible(text).lower()
    t = _REPEAT_RE.sub(r"\1", t)
    for src, dst in _SWAPS:
        t = t.replace(src, dst)
    t = re.sub(r"[^\w\s\u1200-\u137f]", " ", t)
    return _WS_RE.sub(" ", t).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def has_link(text: str) -> bool:
    return bool(_URL_RE.search(strip_invisible(text)))


def has_mention(text: str) -> bool:
    return bool(_MENTION_RE.search(strip_invisible(text)))


def has_phone(text: str) -> bool:
    return bool(_PHONE_RE.search(strip_invisible(text)))


def _word_hits(normalized: str, words: list[str]) -> list[str]:
    tokens = set(normalized.split())
    hits: list[str] = []
    for w in words:
        nw = normalize(w)
        if not nw:
            continue
        if " " in nw:
            if nw in normalized:
                hits.append(w)
        elif nw in tokens:
            hits.append(w)
    return hits


def blocked_words(text: str, blocklist: list[str]) -> list[str]:
    return _word_hits(normalize(text), blocklist)


def risk_words(text: str, risklist: list[str]) -> list[str]:
    return _word_hits(normalize(text), risklist)


def highlight(text: str, words: list[str], marker: str = "⚠️") -> str:
    """Wrap matched words in the original text with a marker (for moderator
    cards only; never sent to the channel)."""
    if not words:
        return text
    out = text
    for w in sorted(set(words), key=len, reverse=True):
        out = re.sub(rf"(?i)\b({re.escape(w)})\b", rf"{marker}\1{marker}", out)
    return out


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""
    risk_hits: list[str] = field(default_factory=list)


def check_text(
    text: str,
    *,
    min_len: int,
    max_len: int,
    blocklist: list[str],
    risklist: list[str],
    allow_links: bool = False,
) -> CheckResult:
    """Run the content checks in the documented order. Returns the first failure."""
    clean = strip_invisible(text).strip()
    n = len(clean)
    if n < min_len:
        return CheckResult(False, "too_short")
    if n > max_len:
        return CheckResult(False, "too_long")
    if not allow_links and has_link(clean):
        return CheckResult(False, "link")
    if has_mention(clean):
        return CheckResult(False, "mention")
    if has_phone(clean):
        return CheckResult(False, "phone")
    if blocked_words(clean, blocklist):
        return CheckResult(False, "blocked")
    return CheckResult(True, "", risk_words(clean, risklist))


_SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "cutt.ly", "rb.gy", "shorturl.at", "is.gd"}


def valid_ad_url(url: str, allow_shorteners: bool = False) -> bool:
    """https only, hostname present, no telegram invite spam, no shorteners."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    if p.scheme != "https" or not p.netloc or len(url) > 500:
        return False
    host = p.hostname or ""
    if host in ("t.me", "telegram.me") and (p.path.startswith("/joinchat") or p.path.startswith("/+")):
        return False
    if not allow_shorteners and host in _SHORTENERS:
        return False
    return True
