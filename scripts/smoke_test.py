#!/usr/bin/env python3
"""
smoke_test.py — exercises the whole bot end-to-end using Flask's in-process
test client (no network, no live server, no LLM key required — runs the
deterministic fallback path so it's fully reproducible in CI or offline).

Covers:
    - /v1/healthz, /v1/metadata
    - /v1/context idempotency (same version = no-op, higher version wins,
      malformed pushes rejected with 400)
    - /v1/tick composing a real message from pushed category+merchant+
      trigger context, respecting suppression on the second identical tick
    - /v1/reply state machine: auto-reply detection -> end after 2 hits,
      intent-transition -> action mode, hostile -> de-escalate (not end),
      off-topic -> redirect, "give me time" -> wait, engaged reply -> send
    - composer.compose() sanity across every trigger-kind family in the
      generated dataset (never empty body, valid cta, correct send_as)

Usage:
    python scripts/smoke_test.py
Exits 0 if all checks pass, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolate this run's SQLite file from any real deployment's state, and make
# sure we exercise the deterministic path deterministically (no live LLM
# calls during smoke testing, regardless of what's in a real .env).
_tmp_db = tempfile.NamedTemporaryFile(prefix="vera_smoke_", suffix=".db", delete=False)
os.environ["VERA_DB_PATH"] = _tmp_db.name
os.environ.pop("LLM_PROVIDER", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)

import bot  # noqa: E402
import composer  # noqa: E402

DATASET_DIR = ROOT / "dataset"

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        FAILURES.append(f"{label} :: {detail}")
        print(f"  [FAIL] {label} :: {detail}")


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def section(title: str):
    print(f"\n--- {title} ---")


def main():
    client = bot.app.test_client()

    section("Liveness")
    r = client.get("/v1/healthz")
    check("healthz returns 200", r.status_code == 200, str(r.status_code))
    body = r.get_json()
    check("healthz reports zeroed contexts before any push", body["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}, str(body))

    r = client.get("/v1/metadata")
    check("metadata returns 200", r.status_code == 200)
    meta = r.get_json()
    check("metadata has team_name/model/approach", all(k in meta for k in ("team_name", "model", "approach", "contact_email", "version")), str(meta))

    section("Context push idempotency")
    dentists = load_json(DATASET_DIR / "categories" / "dentists.json")
    r = client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": dentists, "delivered_at": "2026-04-26T10:00:00Z"})
    check("first push v1 accepted (200)", r.status_code == 200, str(r.get_json()))

    r = client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": dentists, "delivered_at": "2026-04-26T10:00:00Z"})
    check("re-push same version rejected (409, stale_version)", r.status_code == 409 and r.get_json().get("reason") == "stale_version", str(r.get_json()))

    r = client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 2, "payload": dentists, "delivered_at": "2026-04-26T11:00:00Z"})
    check("higher version accepted (200)", r.status_code == 200, str(r.get_json()))

    r = client.post("/v1/context", json={"scope": "bogus_scope", "context_id": "x", "version": 1, "payload": {}})
    check("invalid scope rejected (400)", r.status_code == 400, str(r.get_json()))

    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "m", "version": "not_an_int", "payload": {}})
    check("non-integer version rejected (400)", r.status_code == 400, str(r.get_json()))

    section("Loading full dataset")
    for f in (DATASET_DIR / "categories").glob("*.json"):
        data = load_json(f)
        client.post("/v1/context", json={"scope": "category", "context_id": data["slug"], "version": 5, "payload": data})
    n_m = n_c = n_t = 0
    for f in (DATASET_DIR / "merchants").glob("*.json"):
        data = load_json(f)
        client.post("/v1/context", json={"scope": "merchant", "context_id": data["merchant_id"], "version": 1, "payload": data})
        n_m += 1
    for f in (DATASET_DIR / "customers").glob("*.json"):
        data = load_json(f)
        client.post("/v1/context", json={"scope": "customer", "context_id": data["customer_id"], "version": 1, "payload": data})
        n_c += 1
    for f in (DATASET_DIR / "triggers").glob("*.json"):
        data = load_json(f)
        client.post("/v1/context", json={"scope": "trigger", "context_id": data["id"], "version": 1, "payload": data})
        n_t += 1

    r = client.get("/v1/healthz")
    counts = r.get_json()["contexts_loaded"]
    check("all merchants loaded", counts["merchant"] == n_m, f"{counts['merchant']} != {n_m}")
    check("all customers loaded", counts["customer"] == n_c, f"{counts['customer']} != {n_c}")
    check("all triggers loaded", counts["trigger"] == n_t, f"{counts['trigger']} != {n_t}")

    section("Tick — compose real messages + suppression dedup")
    sample_trigger_ids = ["trg_001_research_digest_dentists", "trg_010_ipl_match_delhi", "trg_018_supply_atorvastatin_recall"]
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": sample_trigger_ids})
    check("tick returns 200", r.status_code == 200, str(r.status_code))
    actions = r.get_json()["actions"]
    check("tick produced at least one action", len(actions) >= 1, str(actions))
    for a in actions:
        check(f"action[{a.get('trigger_id')}] has non-empty body", bool(a.get("body", "").strip()), str(a))
        check(f"action[{a.get('trigger_id')}] cta is valid", a.get("cta") in ("binary", "open_ended", "none", "multi_choice"), str(a.get("cta")))
        check(f"action[{a.get('trigger_id')}] send_as is valid", a.get("send_as") in ("vera", "merchant_on_behalf"), str(a.get("send_as")))

    # Second identical tick should suppress everything already sent.
    r2 = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": sample_trigger_ids})
    actions2 = r2.get_json()["actions"]
    check("repeat tick is suppressed (no duplicate sends)", len(actions2) == 0, str(actions2))

    section("Reply state machine — auto-reply detection")
    conv_id = actions[0]["conversation_id"] if actions else "conv_test_autoreply"
    merchant_id = actions[0]["merchant_id"] if actions else "m_001_drmeera_dentist_delhi"
    canned = "Thank you for contacting us! Our team will respond shortly."
    r = client.post("/v1/reply", json={"conversation_id": conv_id, "merchant_id": merchant_id, "customer_id": None, "from_role": "merchant", "message": canned, "received_at": "2026-04-26T10:40:00Z", "turn_number": 2})
    d1 = r.get_json()
    check("first canned reply -> one soft check-in (send), not immediate end", d1["action"] == "send", str(d1))

    r = client.post("/v1/reply", json={"conversation_id": conv_id, "merchant_id": merchant_id, "customer_id": None, "from_role": "merchant", "message": canned, "received_at": "2026-04-26T10:45:00Z", "turn_number": 3})
    d2 = r.get_json()
    check("second canned reply from same merchant -> end", d2["action"] == "end", str(d2))

    section("Reply state machine — intent transition (anti-pattern D)")
    conv_id2 = "conv_test_intent"
    client.post("/v1/reply", json={"conversation_id": conv_id2, "merchant_id": "m_006_southindiancafe_restaurant_bangalore", "customer_id": None, "from_role": "merchant", "message": "hi", "received_at": "2026-04-26T10:00:00Z", "turn_number": 1})
    r = client.post("/v1/reply", json={"conversation_id": conv_id2, "merchant_id": "m_006_southindiancafe_restaurant_bangalore", "customer_id": None, "from_role": "merchant", "message": "Ok lets do it. Whats next?", "received_at": "2026-04-26T10:05:00Z", "turn_number": 2})
    d = r.get_json()
    body_lower = d.get("body", "").lower()
    qualifying_phrases = ["would you", "do you have", "can you tell me", "how about we"]
    check("intent-commit switches to action, doesn't re-qualify", d["action"] == "send" and not any(p in body_lower for p in qualifying_phrases), str(d))

    section("Reply state machine — hostile handling")
    r = client.post("/v1/reply", json={"conversation_id": "conv_test_hostile", "merchant_id": "m_002_bharat_dentist_mumbai", "customer_id": None, "from_role": "merchant", "message": "Stop messaging me. This is useless spam.", "received_at": "2026-04-26T10:00:00Z", "turn_number": 1})
    d = r.get_json()
    check("hostile message de-escalates with an apology (send, not end)", d["action"] == "send" and ("sorry" in d.get("body", "").lower()), str(d))

    section("Reply state machine — off-topic redirect")
    r = client.post("/v1/reply", json={"conversation_id": "conv_test_offtopic", "merchant_id": "m_002_bharat_dentist_mumbai", "customer_id": None, "from_role": "merchant", "message": "can you also help me file my GST?", "received_at": "2026-04-26T10:00:00Z", "turn_number": 1})
    d = r.get_json()
    check("off-topic question redirects politely", d["action"] == "send" and "outside what i can help" in d.get("body", "").lower(), str(d))

    section("Reply state machine — wait request")
    r = client.post("/v1/reply", json={"conversation_id": "conv_test_wait", "merchant_id": "m_002_bharat_dentist_mumbai", "customer_id": None, "from_role": "merchant", "message": "let me think about it, call me later", "received_at": "2026-04-26T10:00:00Z", "turn_number": 1})
    d = r.get_json()
    check("'give me time' -> wait action", d["action"] == "wait" and d.get("wait_seconds", 0) > 0, str(d))

    section("Reply state machine — engaged, substantive reply")
    r = client.post("/v1/reply", json={"conversation_id": "conv_test_engaged", "merchant_id": "m_006_southindiancafe_restaurant_bangalore", "customer_id": None, "from_role": "merchant", "message": "Yes good idea, what would it look like", "received_at": "2026-04-26T10:00:00Z", "turn_number": 1})
    d = r.get_json()
    check("engaged reply -> send with non-empty body", d["action"] == "send" and bool(d.get("body", "").strip()), str(d))

    section("Composer sanity across every family (deterministic path)")
    families_seen = set()
    for f in (DATASET_DIR / "triggers").glob("*.json"):
        trg = load_json(f)
        family = composer.family_of(trg.get("kind", ""))
        families_seen.add(family)
        merchant = load_json(DATASET_DIR / "merchants" / f"{trg['merchant_id']}.json") if trg.get("merchant_id") else None
        if not merchant:
            continue
        category = load_json(DATASET_DIR / "categories" / f"{merchant['category_slug']}.json")
        customer = None
        if trg.get("scope") == "customer" and trg.get("customer_id"):
            cust_path = DATASET_DIR / "customers" / f"{trg['customer_id']}.json"
            if cust_path.exists():
                customer = load_json(cust_path)
            else:
                continue
        composed = composer.compose(category, merchant, trg, customer, recent_bodies=[])
        check(f"[{family}] {trg['id']}: non-empty body", bool(composed["body"].strip()), composed["body"])
        check(f"[{family}] {trg['id']}: valid cta", composed["cta"] in ("binary", "open_ended", "none", "multi_choice"), composed["cta"])
        expected_send_as = "merchant_on_behalf" if customer else "vera"
        check(f"[{family}] {trg['id']}: correct send_as", composed["send_as"] == expected_send_as, composed["send_as"])
    print(f"\n  Families exercised: {sorted(families_seen)}")

    section("Voice guard")
    import voice
    check("wants_hi_en(['en','hi']) is True", voice.wants_hi_en(["en", "hi"]) is True)
    check("wants_hi_en(['en']) is False", voice.wants_hi_en(["en"]) is False)
    cleaned, hits = voice.strip_taboo("This treatment is guaranteed and will completely cure your pain.", dentists)
    check("strip_taboo detects taboo words", len(hits) >= 1, str(hits))
    check("strip_taboo removes them from output", "guaranteed" not in cleaned.lower() and "completely cure" not in cleaned.lower(), cleaned)

    section("Submission file build")
    import subprocess
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_submission.py")], capture_output=True, text=True)
    check("build_submission.py exits 0", result.returncode == 0, result.stderr)
    sub_path = ROOT / "submission.jsonl"
    check("submission.jsonl was written", sub_path.exists())
    if sub_path.exists():
        lines = [json.loads(l) for l in open(sub_path, encoding="utf-8") if l.strip()]
        check("submission.jsonl has 30 lines", len(lines) == 30, str(len(lines)))
        required_keys = {"test_id", "body", "cta", "send_as", "suppression_key", "rationale"}
        check("every line has all required keys", all(required_keys.issubset(l.keys()) for l in lines))
        check("every body is non-empty", all(l["body"].strip() for l in lines))

    print(f"\n{'='*60}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*60}")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")

    try:
        os.unlink(_tmp_db.name)
    except OSError:
        pass

    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
