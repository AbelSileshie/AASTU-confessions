"""Rich text without parse_mode.

Telegram styling is applied through MessageEntity offsets, so user-supplied
text is never parsed. Offsets/lengths are computed in UTF-16 code units, which
is what Telegram expects (emoji and other astral characters are 2 units).

Rule: `.text()` / `.quote()` for anything a user, advertiser or setting wrote;
`.bold()/.italic()/.code()/.link()` only for strings the bot authored.
"""
from __future__ import annotations

from telegram import MessageEntity

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class Rich:
    __slots__ = ("_ents", "_len", "_parts")

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._ents: list[MessageEntity] = []
        self._len = 0

    # -- literal ----------------------------------------------------------------
    def text(self, s: str) -> Rich:
        if s:
            self._parts.append(s)
            self._len += utf16_len(s)
        return self

    def nl(self, n: int = 1) -> Rich:
        return self.text("\n" * n)

    # -- styled (bot-authored) ------------------------------------------------------
    def styled(self, s: str, type_: str, **kw) -> Rich:
        if not s:
            return self
        self._ents.append(MessageEntity(type_, self._len, utf16_len(s), **kw))
        return self.text(s)

    def bold(self, s: str) -> Rich:
        return self.styled(s, MessageEntity.BOLD)

    def italic(self, s: str) -> Rich:
        return self.styled(s, MessageEntity.ITALIC)

    def code(self, s: str) -> Rich:
        return self.styled(s, MessageEntity.CODE)

    def link(self, s: str, url: str) -> Rich:
        if not url.startswith("https://") and not url.startswith("tg://"):
            return self.text(s)
        return self.styled(s, MessageEntity.TEXT_LINK, url=url)

    def quote(self, s: str, expandable: bool = False) -> Rich:
        """Blockquote *container* around (possibly user) text; the text stays literal."""
        kind = MessageEntity.EXPANDABLE_BLOCKQUOTE if expandable else MessageEntity.BLOCKQUOTE
        return self.styled(s, kind)

    def label(self, name: str, value: str) -> Rich:
        """`Name: value` with a bold name; value literal."""
        return self.bold(name + ":").text(" " + value)

    # -- output ---------------------------------------------------------------------
    @property
    def length(self) -> int:
        return self._len

    def build(self, limit: int = TEXT_LIMIT) -> tuple[str, list[MessageEntity]]:
        """Return (text, entities), truncated safely at `limit` UTF-16 units so no
        entity points past the end."""
        text = "".join(self._parts)
        if self._len <= limit:
            return text, list(self._ents)
        cut = _cut_at_utf16(text, limit - 1) + "…"
        cut_len = utf16_len(cut)
        ents = []
        for e in self._ents:
            if e.offset >= cut_len:
                continue
            length = min(e.length, cut_len - e.offset)
            if length > 0:
                ents.append(MessageEntity(e.type, e.offset, length, url=e.url, user=e.user, language=e.language))
        return cut, ents


def _cut_at_utf16(text: str, units: int) -> str:
    out, used = [], 0
    for ch in text:
        n = utf16_len(ch)
        if used + n > units:
            break
        out.append(ch)
        used += n
    return "".join(out)


def truncate(s: str, n: int) -> str:
    """Character-based truncation with an ellipsis (for snippets)."""
    return s if len(s) <= n else s[: n - 1] + "…"


def card(title: str, meta: list[tuple[str, str]] | None = None, body: str | None = None,
         hint: str | None = None, body_limit: int | None = None, expandable: bool = False) -> Rich:
    """Common card skeleton: bold title · bold-labelled meta line · blockquote body · italic hint."""
    r = Rich().bold(title)
    if meta:
        r.nl()
        for i, (k, v) in enumerate(meta):
            if i:
                r.text("   ")
            r.label(k, v)
    if body:
        r.nl(2).quote(truncate(body, body_limit) if body_limit else body, expandable=expandable)
    if hint:
        r.nl(2).italic(hint)
    return r
