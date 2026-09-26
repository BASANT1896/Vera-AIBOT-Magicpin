# Vera — magicpin AI Challenge Submission

An AI-powered merchant engagement bot built for magicpin's Vera challenge. Vera decides *when* and *what* to proactively message a merchant, and carries on a grounded, multi-turn reply conversation — with an LLM-first composer that falls back to a fully deterministic engine when no LLM is configured or available.

**Live bot:** https://vera-bot-basant.onrender.com

## How it works

Vera composes every message from four pieces of context — **category** (tone/voice for the vertical), **merchant** (identity, performance, offers), **trigger** (why now), and optionally **customer** (for customer-facing sends). A single composer function dispatches by trigger-kind family, calls the configured LLM for a first pass, and falls back to a fact-only deterministic template on any LLM failure — so the bot is always operational, LLM key or not.

Conversations are stateful: a SQLite-backed engine tracks turn history per conversation and handles auto-reply detection, intent-transition (qualifying → action), hostile/off-topic de-escalation, and graceful exit.

## Project structure

| File | Responsibility |
|---|---|
| `bot.py` | Flask HTTP surface — the 5 required endpoints |
| `composer.py` | Builds the outgoing message from category/merchant/trigger/customer context |
| `conversation.py` | Multi-turn reply state machine |
| `llm_provider.py` | Stdlib-only client for Anthropic / OpenAI / DeepSeek / Gemini, temperature=0, strict JSON parsing, safe fallback on any failure |
| `storage.py` | SQLite-backed context store and conversation state |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/context` | Push category / merchant / customer / trigger context (idempotent by version) |
| `POST` | `/v1/tick` | Periodic wake-up — bot may proactively message merchants |
| `POST` | `/v1/reply` | Synchronous reply to a merchant/customer turn |
| `GET` | `/v1/healthz` | Liveness probe |
| `GET` | `/v1/metadata` | Bot identity and config |
| `POST` | `/v1/teardown` | Wipe state (testing only) |

## Running locally

```bash
pip install -r requirements.txt
python bot.py
```

## Running in production

```bash
gunicorn -w 1 --threads 8 --timeout 35 -b 0.0.0.0:$PORT bot:app
```

A single worker with multiple threads is required — see `storage.py` for why (one SQLite connection, one consistent view of in-flight ticks).

## Configuration

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic` \| `openai` \| `deepseek` \| `gemini` — omit for fully deterministic mode |
| `<PROVIDER>_API_KEY` | API key matching the chosen provider |
| `LLM_MODEL` | Override the provider's default model |
| `LLM_TIMEOUT_SECONDS` | Per-LLM-call timeout in seconds (default `18`) |
| `TEAM_NAME`, `TEAM_MEMBERS`, `TEAM_EMAIL` | Fields returned by `/v1/metadata` |

## Deployment

Deployed as a Docker container on Render. A scheduled health-check ping (via cron-job.org, every 10 minutes) keeps the free-tier instance warm during evaluation to avoid cold-start delays.

## Team

Team Vera Rebuild
