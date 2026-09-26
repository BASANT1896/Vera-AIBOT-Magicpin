"""
voice.py — small, dependency-free helpers for category voice/tone matching:
taboo-vocabulary guarding and Hindi-English code-mix detection.

Kept deliberately tiny and easy to audit: the judge rubric
(challenge-brief.md §8, "Category fit") penalizes any taboo word appearing
in a merchant-facing OR customer-facing message, so this is the one safety
net every composed body passes through regardless of whether it came from
the LLM path or the deterministic fallback (see composer.compose()).
"""

from __future__ import annotations

import re
from typing import Optional


def wants_hi_en(languages: list[str]) -> bool:
    """True only when the merchant/customer explicitly lists Hindi among
    their languages — never assumed, never forced onto an English-only
    profile (challenge-brief.md §11 anti-pattern: 'ignoring the language
    preference')."""
    langs = {str(l).strip().lower() for l in (languages or [])}
    return "hi" in langs


def _taboo_list(category: Optional[dict]) -> list[str]:
    return [t for t in ((category or {}).get("voice", {}) or {}).get("vocab_taboo", []) if t]


def strip_taboo(body: str, category: Optional[dict]) -> tuple[str, list[str]]:
    """Scan `body` for any taboo word/phrase declared in the category's
    voice profile. Returns (body_with_hits_removed, list_of_hits_found).

    composer.py's normal path is to discard the whole body and regenerate
    deterministically the moment `hits` is non-empty (a single word being
    surgically deleted can leave a broken sentence) — the cleaned string
    returned here is a defensive fallback for any caller that ships the
    body anyway.
    """
    hits: list[str] = []
    cleaned = body or ""
    for taboo in _taboo_list(category):
        # Taboo entries occasionally carry a parenthetical qualifier, e.g.
        # "FDA-approved (use only when actually applicable)" — only match
        # the literal phrase before any "(".
        phrase = taboo.split("(")[0].strip()
        if not phrase:
            continue
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        if pattern.search(cleaned):
            hits.append(phrase)
            cleaned = pattern.sub("", cleaned)
    if hits:
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, hits


def language_pref_hint(language_pref: Optional[str]) -> str:
    """Normalizes free-form language_pref strings ('hi-en mix', 'english',
    'hi', ...) into one of: 'hi_en', 'hi', 'en'. Used only for logging /
    prompt hints, never to gate whether we personalize."""
    if not language_pref:
        return "en"
    lp = language_pref.strip().lower()
    if "hi" in lp and "en" in lp:
        return "hi_en"
    if lp.startswith("hi"):
        return "hi"
    return "en"
