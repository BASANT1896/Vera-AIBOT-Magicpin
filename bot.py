"""
bot.py — the HTTP surface for the magicpin AI Challenge, per
challenge-testing-brief.md §2. Wires storage.py (persistence),
composer.py (compose()) and conversation.py (multi-turn state machine)
into the 5 required endpoints:

    POST /v1/context   — idempotent context push
    POST /v1/tick      — periodic wake-up; bot may initiate conversations
    POST /v1/reply      — synchronous reply to a merchant/customer turn
    GET  /v1/healthz    — liveness probe
    GET  /v1/metadata   — bot identity

Plus the optional POST /v1/teardown (testing-brief.md §11: wipe state at
end of test).

Run locally:
    pip install -r requirements.txt
    python bot.py                      # Flask dev server on :8080
Run in production:
    gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT bot:app

IMPORTANT: run as a SINGLE worker process (see storage.py docstring and
Procfile) — multiple processes would each keep their own SQLite connection
handle and, more importantly, their own view of in-flight ticks; --threads
gives concurrency within the one process, guarded by storage.py's lock.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify, request

import composer
import conversation
import storage

# Load a local .env for development convenience if python-dotenv is
# installed; harmless no-op in production where real env vars are set.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
storage.init_db()

START_TIME = time.time()
MAX_ACTIONS_PER_TICK = 20


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bad_request(reason: str, details: str = "") -> tuple:
    body = {"accepted": False, "reason": reason}
    if details:
        body["details"] = details
    return jsonify(body), 400


def _load_bundle_for_trigger(trigger_id: str):
    """Returns (category, merchant, trigger, customer) or None if a
    required piece (merchant/category) hasn't been pushed yet. customer is
    None unless the trigger is customer-scoped AND that customer's context
    has been pushed — we never fabricate a customer."""
    trigger = storage.get_context("trigger", trigger_id)
    if not trigger:
        return None
    merchant_id = trigger.get("merchant_id")
    merchant = storage.find_merchant_by_id(merchant_id) if merchant_id else None
    if not merchant:
        return None
    category = storage.find_category_for_merchant(merchant)
    if not category:
        return None
    customer = None
    if trigger.get("scope") == "customer":
        customer_id = trigger.get("customer_id")
        if not customer_id:
            return None
        customer = storage.get_context("customer", customer_id)
        if not customer:
            # Customer context not pushed yet — don't fabricate a
            # customer-facing send; skip this trigger for now.
            return None
    return category, merchant, trigger, customer


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

@app.route("/v1/context", methods=["POST"])
def push_context():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request("invalid_body", "Expected a JSON object")

    scope = data.get("scope")
    context_id = data.get("context_id")
    version = data.get("version")
    payload = data.get("payload")
    delivered_at = data.get("delivered_at", _now_iso())

    if scope not in ("category", "merchant", "customer", "trigger"):
        return _bad_request("invalid_scope", f"scope must be one of category|merchant|customer|trigger, got {scope!r}")
    if not context_id or not isinstance(context_id, str):
        return _bad_request("invalid_context_id", "context_id is required")
    if not isinstance(version, int):
        return _bad_request("invalid_version", "version must be an integer")
    if not isinstance(payload, dict):
        return _bad_request("invalid_payload", "payload must be a JSON object")

    result = storage.push_context(scope, context_id, version, payload, delivered_at)
    status = 200 if result.get("accepted") else 409
    return jsonify(result), status


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------
@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(silent=True) or {}
    now_str = data.get("now") or _now_iso()
    now_dt = _parse_iso(now_str) or datetime.now(timezone.utc)
    available_triggers = data.get("available_triggers") or []
    if not isinstance(available_triggers, list):
        available_triggers = []

    tick_start = time.time()
    TICK_BUDGET_SECONDS = 10  # stay well under the judge's 15s client timeout

    actions = []
    merchants_acted_this_tick: set[str] = set()

    for trigger_id in available_triggers:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break

        elapsed = time.time() - tick_start
        if elapsed > TICK_BUDGET_SECONDS:
            # Running low on time — return what's already composed rather
            # than risk the whole request timing out on the judge's side.
            print(f"[tick] budget exhausted at {elapsed:.1f}s with "
                  f"{len(actions)} action(s) composed; stopping early", flush=True)
            break

        try:
            trigger_id = str(trigger_id)
            trigger_peek = storage.get_context("trigger", trigger_id)
            if not trigger_peek:
                continue

            # Respect expiry.
            expires_dt = _parse_iso(trigger_peek.get("expires_at"))
            if expires_dt and now_dt > expires_dt:
                continue

            # Respect suppression (already sent for this trigger's key).
            suppression_key = trigger_peek.get("suppression_key") or f"{trigger_peek.get('kind','')}:{trigger_peek.get('merchant_id','')}"
            if storage.is_suppressed(suppression_key):
                continue

            merchant_id = trigger_peek.get("merchant_id")
            # Restraint over spam: at most one proactive send per merchant
            # per tick, even if several of their triggers are "available"
            # this cycle — the rest get picked up on a later tick
            # (testing-brief.md FAQ: "restraint is rewarded; spam is
            # penalized").
            if merchant_id and merchant_id in merchants_acted_this_tick:
                continue

            bundle = _load_bundle_for_trigger(trigger_id)
            if not bundle:
                continue
            category, merchant, trigger, customer = bundle

            recent_bodies = storage.recent_bodies_for(
                merchant.get("merchant_id", ""),
                customer.get("customer_id") if customer else None,
            )

            call_start = time.time()
            composed = composer.compose(category, merchant, trigger, customer, recent_bodies=recent_bodies)
            print(f"[tick] {trigger_id} composed in {time.time() - call_start:.1f}s "
                  f"(cumulative {time.time() - tick_start:.1f}s)", flush=True)

            conversation_id = f"conv_{merchant.get('merchant_id','m')}_{trigger_id}"
            customer_id = customer.get("customer_id") if customer else None
            storage.create_conversation(conversation_id, merchant.get("merchant_id", ""), customer_id, trigger_id)
            send_role = composed.get("send_as", "vera")
            storage.record_turn(conversation_id, send_role, composed["body"])
            storage.mark_suppressed(composed["suppression_key"])

            template_params = composer.build_template_params(merchant, composed["body"], customer)

            actions.append({
                "conversation_id": conversation_id,
                "merchant_id": merchant.get("merchant_id"),
                "customer_id": customer_id,
                "send_as": send_role,
                "trigger_id": trigger_id,
                "template_name": f"vera_{composer.family_of(trigger.get('kind',''))}_v1",
                "template_params": template_params,
                "body": composed["body"],
                "cta": composed["cta"],
                "suppression_key": composed["suppression_key"],
                "rationale": composed["rationale"],
            })
            if merchant_id:
                merchants_acted_this_tick.add(merchant_id)
        except Exception:
            # Never let one bad trigger take down the whole tick — skip it.
            continue

    return jsonify({"actions": actions}), 200

# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

@app.route("/v1/reply", methods=["POST"])
def reply():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _bad_request("invalid_body", "Expected a JSON object")

    conversation_id = data.get("conversation_id")
    merchant_id = data.get("merchant_id")
    customer_id = data.get("customer_id")
    from_role = data.get("from_role", "merchant")
    message = data.get("message", "")

    if not conversation_id:
        return _bad_request("invalid_conversation_id", "conversation_id is required")

    conv = storage.get_conversation(conversation_id)
    if conv is None:
        # First we've heard of this conversation_id — register it so
        # subsequent turns (and anti-repetition lookups) have a home.
        trigger_id = None
        storage.create_conversation(conversation_id, merchant_id or "", customer_id, trigger_id)
        conv = storage.get_conversation(conversation_id)

    merchant_id = merchant_id or conv.get("merchant_id")
    customer_id = customer_id if customer_id is not None else conv.get("customer_id")
    trigger_id = conv.get("trigger_id")

    merchant = storage.find_merchant_by_id(merchant_id) if merchant_id else None
    category = storage.find_category_for_merchant(merchant) if merchant else None
    trigger = storage.get_context("trigger", trigger_id) if trigger_id else {}
    customer = storage.get_context("customer", customer_id) if customer_id else None

    storage.record_turn(conversation_id, from_role, message)

    result = conversation.handle_reply(conversation_id, message, category, merchant, trigger, customer)

    action = result.get("action")
    if action == "send":
        send_role = "merchant_on_behalf" if customer else "vera"
        storage.record_turn(conversation_id, send_role, result.get("body", ""))
        storage.reset_unanswered(merchant_id) if merchant_id else None
        return jsonify({
            "action": "send",
            "body": result.get("body", ""),
            "cta": result.get("cta", "open_ended"),
            "rationale": result.get("rationale", ""),
        }), 200
    if action == "wait":
        return jsonify({
            "action": "wait",
            "wait_seconds": result.get("wait_seconds", 1800),
            "rationale": result.get("rationale", ""),
        }), 200
    # action == "end"
    storage.end_conversation(conversation_id)
    return jsonify({"action": "end", "rationale": result.get("rationale", "")}), 200


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": storage.contexts_loaded_counts(),
    }), 200


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.route("/v1/metadata", methods=["GET"])
def metadata():
    import llm_provider

    model = os.environ.get("LLM_MODEL") or (
        llm_provider._default_model(llm_provider._provider()) if llm_provider.is_configured() else "deterministic-fallback (no LLM key configured)"
    )
    team_members = [m.strip() for m in os.environ.get("TEAM_MEMBERS", "Solo Builder").split(",") if m.strip()]
    return jsonify({
        "team_name": os.environ.get("TEAM_NAME", "Team Vera Rebuild"),
        "team_members": team_members,
        "model": model,
        "approach": (
            "4-context composer (category/merchant/trigger/customer) dispatched by trigger-kind "
            "family, LLM-first with a fully-deterministic fact-only fallback; SQLite-backed "
            "stateful conversation engine handling auto-reply detection, intent-transition, "
            "hostile/off-topic de-escalation, and graceful exit."
        ),
        "contact_email": os.environ.get("TEAM_EMAIL", "team@example.com"),
        "version": "1.0.0",
        "submitted_at": os.environ.get("SUBMITTED_AT", _now_iso()),
    }), 200


# ---------------------------------------------------------------------------
# POST /v1/teardown (optional, per testing-brief.md §11)
# ---------------------------------------------------------------------------

@app.route("/v1/teardown", methods=["POST"])
def teardown():
    storage.wipe()
    return jsonify({"status": "wiped"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
