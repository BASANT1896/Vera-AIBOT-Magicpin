"""
llm_provider.py — thin, dependency-free (stdlib only) client for whichever
LLM backend is configured via environment variables. Used exclusively by
composer.py's `llm_compose()` path.

Supported providers (set LLM_PROVIDER): anthropic | openai | deepseek | gemini
No API key configured -> is_configured() returns False and composer.py falls
back to the fully deterministic path. This means the bot is fully
operational (and passes the smoke test) with ZERO external network calls
and ZERO extra pip dependencies for this module.

Every call:
    - temperature = 0 (challenge-brief.md §7.1 requires deterministic
      output given the same inputs)
    - a hard client-side timeout well under the judge's 30s per-call budget
      (LLM_TIMEOUT_SECONDS, default 18s) so a slow LLM call never causes the
      /v1/tick or /v1/reply handler itself to blow the judge's budget
    - strict-JSON parsing with markdown-fence stripping; any parse failure
      returns None so the caller falls back to the deterministic composer
      rather than ever raising up into the HTTP layer
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional
from urllib import request as urlrequest, error as urlerror

TIMEOUT = float(os.environ.get("LLM_TIMEOUT_SECONDS", "18"))


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "").strip().lower()


def _api_key() -> str:
    p = _provider()
    key_env = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }.get(p)
    return os.environ.get(key_env, "") if key_env else ""


def is_configured() -> bool:
    return bool(_provider()) and bool(_api_key())


def _default_model(provider: str) -> str:
    return {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o-mini",
        "deepseek": "deepseek-chat",
        "gemini": "gemini-1.5-flash",
    }.get(provider, "")


def _model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or _default_model(_provider())


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


def _extract_json(text: str) -> Optional[dict]:
    text = _strip_fences(text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def _call_anthropic(system: str, user: str) -> Optional[str]:
    body = json.dumps({
        "model": _model(),
        "max_tokens": 700,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urlrequest.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": _api_key(),
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    resp = urlrequest.urlopen(req, timeout=TIMEOUT)
    data = json.loads(resp.read().decode("utf-8"))
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(parts) if parts else None


def _call_openai(system: str, user: str) -> Optional[str]:
    body = json.dumps({
        "model": _model(),
        "temperature": 0,
        "max_tokens": 700,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urlrequest.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
    )
    resp = urlrequest.urlopen(req, timeout=TIMEOUT)
    data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _call_deepseek(system: str, user: str) -> Optional[str]:
    body = json.dumps({
        "model": _model(),
        "temperature": 0,
        "max_tokens": 700,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urlrequest.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
    )
    resp = urlrequest.urlopen(req, timeout=TIMEOUT)
    data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _call_gemini(system: str, user: str) -> Optional[str]:
    full_prompt = f"{system}\n\n{user}"
    body = json.dumps({
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 700},
    }).encode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_model()}:generateContent?key={_api_key()}"
    req = urlrequest.Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urlrequest.urlopen(req, timeout=TIMEOUT)
    data = json.loads(resp.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"]


_CALLERS = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "deepseek": _call_deepseek,
    "gemini": _call_gemini,
}


def complete_json(system: str, user: str) -> Optional[dict]:
    """Returns a parsed dict, or None on ANY failure (network, timeout,
    malformed JSON, unconfigured provider). Callers must treat None as
    'fall back to the deterministic composer' — this never raises."""
    if not is_configured():
        return None
    caller = _CALLERS.get(_provider())
    if not caller:
        return None
    try:
        raw = caller(system, user)
        if not raw:
            return None
        return _extract_json(raw)
    except (urlerror.URLError, TimeoutError, OSError, KeyError, ValueError, IndexError):
        return None
