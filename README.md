# Vera Rebuild — magicpin AI Challenge submission

A stateful HTTP bot implementing the 4-context composition contract
(`challenge-brief.md` §4–5) and the 5-endpoint judge harness
(`challenge-testing-brief.md` §2). Fully functional with **zero LLM key
configured** (deterministic fallback), and upgrades in place the moment an
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / etc. is set.

## Repo layout

```
bot.py                  Flask app — the 5 required endpoints + /v1/teardown
composer.py              compose(category, merchant, trigger, customer?) — the core engine
conversation.py          /v1/reply state machine (auto-reply, intent, hostile, off-topic, wait, end)
conversation_handlers.py Thin re-export of conversation.respond(state, message) — brief §7.4 signature
storage.py               SQLite persistence (idempotent contexts, conversations, suppression, counters)
voice.py                 Taboo-vocabulary guard + Hindi-English detection helpers
llm_provider.py          Stdlib-only client for Anthropic/OpenAI/DeepSeek/Gemini (optional)

requirements.txt, Procfile, Dockerfile, .env.example   Deployment
scripts/build_submission.py   Regenerates submission.jsonl from the dataset via composer.compose()
scripts/smoke_test.py         340-assertion in-process test of the whole HTTP surface (no network needed)

dataset/                 Full expanded dataset (5 categories / 50 merchants / 200 customers / 100 triggers / test_pairs.json)
submission.jsonl         The 30 required test-pair outputs (§7.2)
brief/                   Original challenge materials (both briefs, case studies, dataset seeds + generator, judge_simulator.py)
```

## Quickstart

```bash
pip install -r requirements.txt
python scripts/smoke_test.py        # 340 checks, no network/LLM key needed, ~3s
python bot.py                       # dev server on :8080
# or in production:
gunicorn -w 1 --threads 8 --timeout 35 -b 0.0.0.0:$PORT bot:app
```

To regenerate the dataset and submission file from scratch:
```bash
python brief/dataset/generate_dataset.py --seed-dir brief/dataset --out dataset
python scripts/build_submission.py
```

## Deploying to get a public URL

Any host that runs a Dockerfile or a `Procfile` works — Render, Railway, Fly.io,
or an ngrok tunnel over `python bot.py` for a quick local demo. Copy
`.env.example` to `.env` (or set the equivalent host env vars) first; the
bot runs correctly with **no** LLM key set at all.

**Run as a single worker process.** `storage.py` is a single SQLite
connection guarded by an in-process lock; two worker processes would each
hold a divergent view of pushed contexts and in-flight conversations.
`--threads 8` gives real concurrency for the judge's ≤10 req/s load without
that risk. This is already set correctly in both `Procfile` and `Dockerfile`.

## Approach

**Composer (`composer.py`).** Every trigger `kind` maps to one of ~24
"families" (digest, perf_spike, recall, lapse_hard, ...), each with its own
family-specific guidance string. The LLM path builds one strict-JSON prompt
per family from the four trimmed contexts; the deterministic fallback has a
hand-written template builder per family that reads only real fields off
the context objects — it cannot fabricate a number, date, or offer that
wasn't pushed, because there's no code path that lets it write anything
except what it read. Every output — LLM or deterministic — passes back
through the same two guards: a taboo-vocabulary scan against the category's
`voice.vocab_taboo` list (regenerates deterministically if the LLM slipped
one in), and an anti-repetition check against the last 20 bodies sent to
that merchant/customer pair.

**Conversation state machine (`conversation.py`).** Ordered checks per
reply, highest-priority first: (1) auto-reply detection, tracked
*per-merchant-number* rather than per-conversation (a canned WhatsApp
Business responder is a property of the phone line, not one thread) — one
soft check-in, then a clean `end`; (2) hostile-message de-escalation with
an explicit opt-out, without hard-closing so a hostile-then-off-topic
sequence in the same replay can still be handled; (3) intent-commit
detection ("let's do it" / "go ahead" / "what's next"), checked *before*
the generic question-mark heuristic below it, since "ok let's do it, what's
next?" must not be caught by the off-topic filter — this directly targets
`challenge-brief.md`'s anti-pattern D (re-qualifying after explicit yes);
(4) off-topic redirection that stays polite and on-mission; (5) explicit
"give me time" → `wait`; (6) explicit disinterest → graceful `end`;
(7) default: a real composed next step, not a canned acknowledgment.

**Storage.** SQLite over a bare dict specifically because the testing
brief's own warning — "just don't restart between calls" — is exactly the
failure mode a single dropped/restarted process would hit during a live
60-minute test. WAL mode, one connection, one lock; costs nothing in
latency, survives a process bounce on the same host for free.

## Tradeoffs

- **Deterministic-first, not LLM-first, as the thing we optimized.** The
  LLM path is one prompt call and will usually out-write the templates on
  novel phrasing, but every family builder was hand-tuned against the
  brief's own case studies (`brief/examples/case-studies.md`) so the bot's
  *floor* — what a judge sees with zero API cost or on an LLM timeout — is
  still specific, on-voice, and trigger-grounded rather than generic.
- **One action per merchant per tick.** The testing brief allows multiple
  triggers "available" for the same merchant in one tick; we cap proactive
  sends to one per merchant per tick and let the next tick pick up the
  rest, on the FAQ's own steer ("restraint is rewarded, spam is
  penalized") rather than maximizing message volume.
- **No customer fabrication.** If a customer-scoped trigger references a
  `customer_id` whose context hasn't been pushed yet, the bot skips that
  trigger entirely rather than composing without personalization data.
  This trades a small amount of Phase-3 responsiveness for zero
  hallucination risk.
- **Hindi-English code-mix kept intentionally shallow.** An earlier pass
  did naive mid-sentence word substitution ("Want me to" → "Chahenge to
  main ...") which produced broken Hinglish, since English content words
  don't take Hindi verb endings. The current version only swaps in
  complete, pre-written Hindi sentences for exact known strings, or tacks
  on a short, grammatically self-contained Hindi closing tag ("Bataiye?")
  after an already-complete English sentence — safe rather than
  comprehensive.

## What additional context would have helped most

1. **A real customer-list source-of-truth per merchant** (flagged as an
   open question in `engagement-design.md` §"Open questions") — right now
   `customer_aggregate` gives us counts but not which specific lapsed
   customers to re-engage first; a ranked list would sharpen `recall_due` /
   `customer_lapsed_*` composition a lot.
2. **A real per-merchant reply-latency / engagement-quality history**
   beyond the last 3 conversation turns, to calibrate how much "priming"
   (vs. straight to the ask) a specific merchant responds to.
3. **An actual offer/catalog write-back path** — several family builders
   (bridal followup, corporate-bulk pricing) currently can only reference
   offers already in `MerchantContext.offers`; they can't originate a new
   tiered offer the merchant hasn't set up yet without risking fabrication.
