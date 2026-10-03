"""Tiny i18n: strings/<lang>.json with English fallback."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

STRINGS_DIR = Path(__file__).parent / "strings"
DEFAULT_LANG = "en"
SUPPORTED = ("en", "am")


@lru_cache(maxsize=None)
def _load(lang: str) -> dict[str, str]:
    path = STRINGS_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def t(lang: str | None, key: str, **kw) -> str:
    lang = lang if lang in SUPPORTED else DEFAULT_LANG
    template = _load(lang).get(key) or _load(DEFAULT_LANG).get(key) or key
    try:
        return template.format(**kw)
    except (KeyError, IndexError):
        return template

