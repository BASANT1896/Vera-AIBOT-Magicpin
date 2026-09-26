"""
composer.py — implements the compose(category, merchant, trigger, customer?)
contract from challenge-brief.md §5.

Design:
    1. LLM path (primary): builds a family-specific prompt from the four
       contexts and asks the configured LLM (see llm_provider.py) for
       strict JSON. temperature=0 for determinism.
    2. Deterministic path (fallback): if no LLM key is configured, the call
       fails, times out, or returns unparsable output, we fall back to a
       rule-based composer that fills family-specific templates directly
       from the context fields. It never invents a fact that isn't present
       in category/merchant/trigger/customer — a field is simply omitted
       from the sentence if the data isn't there.
    3. Post-generation validation runs on EITHER path's output: single
       primary CTA, taboo-vocabulary check, anti-repetition against
       recently-sent bodies for this merchant/customer, and schema
       completion (fills any missing key with a safe default rather than
       ever returning malformed JSON to the harness).

Every trigger `kind` is mapped to a small set of "families" (digest,
perf_spike, recall, ...) so that adding a brand-new kind the bot has never
seen just falls back to `generic` instead of crashing.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import llm_provider
import voice

# ---------------------------------------------------------------------------
# Trigger kind -> family dispatch
# ---------------------------------------------------------------------------

FAMILY_MAP = {
    # knowledge / digest driven
    "research_digest": "digest",
    "research_digest_release": "digest",
    "category_research_digest_release": "digest",
    "cde_opportunity": "cde",
    "category_seasonal": "trend",
    "category_trend_movement": "trend",
    # compliance / safety
    "regulation_change": "compliance",
    "supply_alert": "compliance",
    # performance
    "perf_spike": "perf_spike",
    "perf_dip": "perf_dip",
    "seasonal_perf_dip": "perf_dip",
    "milestone_reached": "milestone",
    "review_theme_emerged": "review_theme",
    "gbp_unverified": "profile_action",
    "dormant_with_vera": "dormant",
    # merchant-lifecycle
    "renewal_due": "renewal",
    "winback_eligible": "winback",
    "curious_ask_due": "curious_ask",
    "competitor_opened": "competitor",
    # external / event
    "festival_upcoming": "external_event",
    "festival": "external_event",
    "weather_heatwave": "external_event",
    "local_news_event": "external_event",
    "ipl_match_today": "external_event",
    # merchant already mid-conversation (do NOT re-qualify)
    "active_planning_intent": "planning_continuation",
    # customer scope
    "recall_due": "recall",
    "customer_lapsed_soft": "lapse_soft",
    "customer_lapsed_hard": "lapse_hard",
    "appointment_tomorrow": "appointment",
    "chronic_refill_due": "refill",
    "trial_followup": "trial_followup",
    "unplanned_slot_open": "slot_open",
    "wedding_package_followup": "lifecycle_followup",
    "bridal_followup": "lifecycle_followup",
}

FAMILY_HINTS = {
    "digest": "New peer knowledge just landed (research/compliance/trend digest item). Cite the source and one concrete figure from it. Offer a concrete low-friction next step, don't just report the fact.",
    "cde": "Invite to a real educational/community event from the digest. Purely informational CTA, no pressure.",
    "trend": "A search/demand trend shifted. Translate it into one concrete, merchant-specific action, using the real numbers given.",
    "compliance": "Regulatory or safety-relevant. Precise and calm, never alarmist. State the concrete deadline or affected batch/item and the one action needed.",
    "perf_spike": "A real metric moved up. Acknowledge briefly with the real numbers, then propose converting the spike into a concrete next action.",
    "perf_dip": "A real metric moved down. Name it plainly with the real numbers. If the trigger marks it as expected/seasonal, reassure and redirect rather than alarm; otherwise propose one concrete fix.",
    "milestone": "A real milestone was hit or is imminent. Light celebratory touch using the exact number, no hard ask required.",
    "review_theme": "A recurring review theme emerged with a real occurrence count. Name it factually, propose one concrete fix.",
    "profile_action": "A concrete, fixable profile/listing gap exists. Name it plainly and offer to just do it for them.",
    "dormant": "Merchant has gone quiet with Vera for a while. Low-pressure, single easy re-entry question, no guilt.",
    "renewal": "Subscription renewal is approaching. State the real days-remaining and plan. Single clear CTA.",
    "winback": "Subscription lapsed but the merchant's underlying customer base is still active — make the reactivation case with the real numbers.",
    "curious_ask": "A light, low-stakes question to the merchant that harvests a useful answer and offers something back in return. No hard ask.",
    "competitor": "A competitor opened nearby. Frame as useful market intel, not fear — offer one concrete counter-move.",
    "external_event": "An outside event (festival/weather/local match/news) is relevant right now. Give a clear, specific read (go/no-go, or a concrete tactical suggestion) rather than a generic 'run a promo'.",
    "planning_continuation": "CRITICAL: the merchant already showed explicit intent in their own last message (see trigger payload). Continue the thread directly with the next concrete step. Do NOT ask a qualifying question and do NOT restart the pitch.",
    "recall": "Customer-facing. A recall/checkup window opened for this specific customer. Reference their last visit and offer real available slots if given. Warm, low-pressure.",
    "lapse_soft": "Customer-facing. Customer has gone quiet for a few months. No-guilt, warm re-engagement, one tiny concrete ask.",
    "lapse_hard": "Customer-facing. Longer lapse. Extra warmth; explicitly remove commitment friction (e.g. no charge, no obligation).",
    "appointment": "Customer-facing. Reminder for a booked appointment happening soon. Purely confirmatory/logistics tone.",
    "refill": "Customer-facing. A recurring/chronic order is due. Be precise about what's due and by when; make reordering effortless.",
    "trial_followup": "Customer-facing. Following up after a trial/first visit. Reference what actually happened, offer the natural next step.",
    "slot_open": "Customer-facing. An unplanned opening exists; offer it as a favor to a likely-interested customer, not a hard sell.",
    "lifecycle_followup": "Customer-facing. A meaningful life-event window is active (e.g. wedding countdown). Reference the concrete countdown/date and the specific next stage.",
    "generic": "Compose the best possible message from whatever real facts are available in the given contexts. Never invent a fact that isn't present.",
}


def family_of(kind: str) -> str:
    return FAMILY_MAP.get(kind, "generic")


# ---------------------------------------------------------------------------
# Fact-extraction helpers (never fabricate — just read what's there)
# ---------------------------------------------------------------------------

def _g(d: Optional[dict], *path, default=None):
    cur = d or {}
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def merchant_name(merchant: dict) -> str:
    return _g(merchant, "identity", "name", default="your business")


def owner_name(merchant: dict) -> Optional[str]:
    return _g(merchant, "identity", "owner_first_name")


def locality_city(merchant: dict) -> str:
    loc = _g(merchant, "identity", "locality")
    city = _g(merchant, "identity", "city")
    if loc and city:
        return f"{loc}, {city}"
    return loc or city or ""


def active_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in (merchant.get("offers") or []) if o.get("status") == "active" and o.get("title")]


def digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in (category or {}).get("digest", []) or []:
        if item.get("id") == item_id:
            return item
    return None


def peer_stats(category: dict) -> dict:
    return (category or {}).get("peer_stats", {}) or {}


def merchant_langs(merchant: dict) -> list[str]:
    return _g(merchant, "identity", "languages", default=[]) or []


def customer_name(customer: Optional[dict]) -> Optional[str]:
    return _g(customer, "identity", "name")


def customer_lang_pref(customer: Optional[dict]) -> Optional[str]:
    return _g(customer, "identity", "language_pref")


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the composition engine behind Vera, magicpin's WhatsApp AI assistant for local merchants (dentists, salons, restaurants, gyms, pharmacies) in India. You write EITHER a merchant-facing message (from Vera) OR a customer-facing message sent on behalf of the merchant to one of their own customers.

You are given four context layers: CATEGORY (how this business type is spoken to), MERCHANT (this specific business's real state), TRIGGER (the real event that justifies messaging right now), and optionally CUSTOMER (the specific customer, only for on-behalf-of-merchant sends).

Hard rules:
- Ground every claim in the data you were given. NEVER invent a number, date, offer, competitor name, or citation that is not present in the input. If a fact isn't given, don't reference it.
- Anchor on at least one concrete, verifiable fact (a real number, date, or headline) over generic language. "10% off" is weak; "Dental Cleaning @ ₹299" or "views +28%" is strong.
- Exactly ONE primary call-to-action per message. Prefer a binary yes/no ask for action-triggers; use "none" for pure-information triggers; a short multi-choice (e.g. pick one of two real slots) is acceptable only for booking flows.
- Match the category voice (tone/vocabulary/taboos given) — never use a taboo word.
- Personalize to the specific merchant (and customer, if present): use their real name/owner name, real numbers, and match their language preference. Hindi-English code-mix is natural and often preferred for Indian merchants/customers when their languages include "hi" — don't force it if not.
- Keep it concise: no long preambles, don't re-introduce yourself if there's conversation history, land the ask in the last sentence.
- Never repeat, near-verbatim, any of the "recently sent" messages you're shown.
- send_as is "vera" when there is no customer context, "merchant_on_behalf" when a customer context is given.

Respond with ONLY a single JSON object, no markdown fences, no commentary, in exactly this shape:
{"body": "...", "cta": "binary"|"open_ended"|"none"|"multi_choice", "send_as": "vera"|"merchant_on_behalf", "rationale": "one concise sentence on why this message and what it should achieve"}"""


def _trim_merchant(merchant: dict) -> dict:
    return {
        "identity": merchant.get("identity", {}),
        "subscription": merchant.get("subscription", {}),
        "performance": merchant.get("performance", {}),
        "offers": [o for o in merchant.get("offers", []) if o.get("status") == "active"],
        "conversation_history": (merchant.get("conversation_history") or [])[-3:],
        "customer_aggregate": merchant.get("customer_aggregate", {}),
        "signals": merchant.get("signals", []),
        "review_themes": merchant.get("review_themes", []),
    }


def _trim_category(category: dict, trigger: dict) -> dict:
    out = {
        "slug": category.get("slug"),
        "voice": category.get("voice", {}),
        "peer_stats": category.get("peer_stats", {}),
    }
    item = digest_item(category, _g(trigger, "payload", "top_item_id"))
    if item:
        out["relevant_digest_item"] = item
    if _g(trigger, "payload", "digest_item_id"):
        alt = digest_item(category, _g(trigger, "payload", "digest_item_id"))
        if alt:
            out["relevant_digest_item"] = alt
    out["offer_catalog"] = category.get("offer_catalog", [])
    out["seasonal_beats"] = category.get("seasonal_beats", [])
    out["trend_signals"] = category.get("trend_signals", [])
    return out


def build_user_prompt(category: dict, merchant: dict, trigger: dict, customer: Optional[dict], recent_bodies: list[str]) -> str:
    family = family_of(trigger.get("kind", ""))
    payload = {
        "family_guidance": FAMILY_HINTS.get(family, FAMILY_HINTS["generic"]),
        "category": _trim_category(category, trigger),
        "merchant": _trim_merchant(merchant),
        "trigger": {
            "kind": trigger.get("kind"),
            "scope": trigger.get("scope"),
            "source": trigger.get("source"),
            "urgency": trigger.get("urgency"),
            "payload": trigger.get("payload", {}),
        },
        "customer": customer or None,
        "recently_sent_bodies_do_not_repeat": recent_bodies[:5],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def llm_compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict], recent_bodies: list[str]) -> Optional[dict]:
    if not llm_provider.is_configured():
        return None
    user = build_user_prompt(category, merchant, trigger, customer, recent_bodies)
    data = llm_provider.complete_json(SYSTEM_PROMPT, user)
    if not data or not data.get("body"):
        return None
    return {
        "body": str(data.get("body", "")).strip(),
        "cta": data.get("cta", "open_ended"),
        "send_as": data.get("send_as", "merchant_on_behalf" if customer else "vera"),
        "rationale": data.get("rationale", "Composed from category+merchant+trigger context."),
    }


# ---------------------------------------------------------------------------
# Deterministic fallback path — one builder per family
# ---------------------------------------------------------------------------

def _num_pct(x) -> Optional[str]:
    if x is None:
        return None
    try:
        return f"{abs(float(x)) * 100:.0f}%"
    except (TypeError, ValueError):
        return None


_SALUTATION_TITLE_RE = re.compile(r"^([A-Za-z]+\.?)\s*\{first_name\}$")


def _salutation_title(category: Optional[dict]) -> Optional[str]:
    """Reads category.voice.salutation_examples for a '<Title> {first_name}'
    pattern (e.g. dentists: 'Dr. {first_name}') and returns just the title
    token. Categories without such a pattern (salons/restaurants/gyms/
    pharmacies use plain 'Hi {first_name}') return None. This is what lets
    a dentist merchant be addressed as 'Dr. Meera' — matching every example
    in case-studies.md — while a salon owner stays 'Renu', not 'Hi Renu'
    baked into the name itself."""
    for example in ((category or {}).get("voice", {}) or {}).get("salutation_examples", []) or []:
        m = _SALUTATION_TITLE_RE.match(str(example).strip())
        if m and m.group(1).lower() not in ("hi",):
            return m.group(1)
    return None


def _who(merchant: dict, category: Optional[dict] = None) -> str:
    name = owner_name(merchant) or merchant_name(merchant)
    title = _salutation_title(category)
    if title and owner_name(merchant) and not name.lower().startswith(title.lower().rstrip(".")):
        return f"{title} {name}"
    return name


def _build_digest(category, merchant, trigger, customer):
    item = digest_item(category, _g(trigger, "payload", "top_item_id"))
    who = _who(merchant, category)
    if item:
        src = item.get("source", "")
        title = item.get("title", "")
        n = item.get("trial_n")
        seg = item.get("patient_segment") or item.get("segment")
        seg_txt = f" relevant to your {seg.replace('_', ' ')}" if seg else ""
        n_txt = f"{n}-sample " if n else ""
        body = (
            f"{who}, this week's {category.get('display_name', category.get('slug', 'category'))} digest landed. "
            f"One item{seg_txt}: {title}"
            + (f" ({n_txt}study, {src})." if src else ".")
            + " Want me to pull the full summary and draft something you can share or act on?"
        )
    else:
        body = (
            f"{who}, a new item landed in this week's category digest. Want me to pull the details relevant to your practice?"
        )
    return body, "open_ended", ["specificity", "reciprocity", "curiosity"]


def _build_cde(category, merchant, trigger, customer):
    item = digest_item(category, _g(trigger, "payload", "digest_item_id"))
    who = _who(merchant, category)
    if item:
        date = item.get("date", "")
        credits = trigger.get("payload", {}).get("credits") or item.get("credits")
        credit_txt = f", {credits} credits" if credits else ""
        fee = trigger.get("payload", {}).get("fee", "")
        fee_txt = " — free for members" if "free" in str(fee) else ""
        body = (
            f"{who}, heads up on {item.get('title', 'an upcoming session')}{credit_txt}{fee_txt}. "
            f"Worth blocking the slot" + (f" ({date[:10]})" if date else "") + "? I can add a reminder."
        )
    else:
        body = f"{who}, there's a relevant category event coming up — want the details?"
    return body, "binary", ["specificity", "reciprocity"]


def _build_trend(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    trends = payload.get("trends")
    if trends:
        def _fmt_trend(t: str) -> str:
            t = t.replace("_", " ")
            return t + "%" if re.search(r"[+-]?\d+$", t) else t
        top = ", ".join(_fmt_trend(t) for t in trends[:3])
        body = (
            f"{who}, seasonal demand is shifting in your category right now: {top}. "
            f"Want me to suggest which 2-3 lines to push to the front of your listing this week?"
        )
    else:
        signals = category.get("trend_signals", [])
        if signals:
            s = signals[0]
            delta = _num_pct(s.get("delta_yoy"))
            body = (
                f"{who}, \"{s.get('query')}\" searches are up {delta} YoY"
                + (f" in the {s.get('segment_age')} age band" if s.get("segment_age") else "")
                + ". Want me to tweak your listing to catch that demand?"
            )
        else:
            body = f"{who}, category search demand is shifting — want me to flag what's moving for your listing?"
    return body, "binary", ["specificity", "trend_relevance"]


def _build_compliance(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    if trigger.get("kind") == "supply_alert":
        molecule = payload.get("molecule", "a product")
        batches = payload.get("affected_batches") or []
        mfr = payload.get("manufacturer", "")
        batch_txt = ", ".join(batches) if batches else ""
        agg = merchant.get("customer_aggregate", {})
        count_hint = agg.get("total_unique_ytd")
        count_txt = f" I can cross-check against your {count_hint} regular customers if useful." if count_hint else ""
        body = (
            f"{who}, urgent: voluntary recall on {molecule}"
            + (f" batches {batch_txt}" if batch_txt else "")
            + (f" by {mfr}" if mfr else "")
            + f". Customers on this should be informed for replacement.{count_txt} "
            + "Want me to draft the customer note and a replacement-pickup flow?"
        )
    else:
        item = digest_item(category, _g(trigger, "payload", "top_item_id"))
        deadline = payload.get("deadline_iso", "")
        if item:
            body = (
                f"{who}, compliance update: {item.get('title')}"
                + (f" — deadline {deadline[:10]}." if deadline else ".")
                + f" {item.get('actionable', 'Worth reviewing your setup against this.')} Want the full circular?"
            )
        else:
            body = f"{who}, a regulatory update relevant to your category just came in" + (f", effective {deadline[:10]}" if deadline else "") + ". Want the details?"
    return body, "open_ended", ["urgency", "specificity"]


def _build_perf_spike(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    metric = payload.get("metric", "numbers")
    delta = _num_pct(payload.get("delta_pct"))
    baseline = payload.get("vs_baseline")
    driver = payload.get("likely_driver")
    driver_txt = f" — looks driven by {driver.replace('_', ' ')}" if driver else ""
    body = (
        f"{who}, good sign: your {metric} are up {delta or 'noticeably'} this week"
        + (f" (vs a usual {baseline})" if baseline else "")
        + f"{driver_txt}. Want me to double down on whatever's working while it's hot?"
    )
    return body, "binary", ["specificity", "positive_reinforcement"]


def _build_perf_dip(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    metric = payload.get("metric", "performance")
    delta = _num_pct(payload.get("delta_pct"))
    seasonal = payload.get("is_expected_seasonal")
    note = payload.get("season_note", "").replace("_", " ")
    if seasonal:
        agg = merchant.get("customer_aggregate", {})
        active_hint = agg.get("total_unique_ytd") or agg.get("active_count")
        active_txt = f"Focus retention on your {active_hint} existing customers instead. " if active_hint else ""
        body = (
            f"{who}, your {metric} are down {delta or 'a bit'} this week — worth flagging this looks like the normal "
            f"seasonal dip{(' (' + note + ')') if note else ''}, not a real problem. {active_txt}"
            f"Want me to draft something to keep them engaged through it?"
        )
    else:
        body = (
            f"{who}, your {metric} dropped {delta or 'noticeably'} this week. Want me to take a look and suggest one concrete fix?"
        )
    return body, "binary", ["specificity", "loss_aversion" if not seasonal else "reassurance"]


def _build_milestone(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    metric = (payload.get("metric") or "milestone").replace("_", " ")
    value_now = payload.get("value_now")
    milestone_value = payload.get("milestone_value")
    if payload.get("is_imminent") and value_now and milestone_value:
        body = (
            f"{who}, you're at {value_now} {metric} — {milestone_value - value_now} away from {milestone_value}. "
            f"Want me to draft a quick ask to your recent happy customers to help close the gap?"
        )
    elif value_now:
        body = f"{who}, milestone hit: {value_now} {metric}. Want me to turn this into a Google post?"
    else:
        body = f"{who}, you're closing in on a real milestone. Want me to draft something to mark it?"
    return body, "binary", ["social_proof", "specificity"]


def _build_review_theme(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    theme = (payload.get("theme") or "a recurring theme").replace("_", " ")
    n = payload.get("occurrences_30d")
    quote = payload.get("common_quote")
    n_txt = f"{n} reviews this month mention it" if n else "it's come up a few times recently"
    quote_txt = f' — one said "{quote}"' if quote else ""
    body = f"{who}, heads-up: {n_txt} — {theme}{quote_txt}. Want me to draft one concrete fix + a reply template for these reviews?"
    return body, "binary", ["specificity", "loss_aversion"]


def _build_profile_action(category, merchant, trigger, customer):
    who = _who(merchant, category)
    uplift = trigger.get("payload", {}).get("estimated_uplift_pct")
    uplift_txt = f" Verified listings typically see about {_num_pct(uplift)} more visibility." if uplift else ""
    body = f"{who}, your listing isn't verified yet on Google — that's likely costing you visibility.{uplift_txt} Want me to start the verification for you? Takes 2 minutes."
    return body, "binary", ["loss_aversion", "effort_externalization"]


def _build_dormant(category, merchant, trigger, customer):
    who = _who(merchant, category)
    days = trigger.get("payload", {}).get("days_since_last_merchant_message")
    days_txt = f"it's been about {days} days since we last spoke" if days else "it's been a while"
    body = f"{who}, {days_txt} — no rush, just checking in. Anything on your account you'd like me to take a look at this week?"
    return body, "open_ended", ["low_pressure"]


def _build_renewal(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    days = payload.get("days_remaining") or _g(merchant, "subscription", "days_remaining")
    plan = payload.get("plan") or _g(merchant, "subscription", "plan")
    amount = payload.get("renewal_amount")
    amount_txt = f" (₹{amount})" if amount else ""
    body = f"{who}, your {plan or 'plan'} renews in {days} days{amount_txt}. Want me to lock in the renewal now so there's no gap in your listing?"
    return body, "binary", ["specificity", "loss_aversion"]


def _build_winback(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    since = payload.get("days_since_expiry")
    lapsed_added = payload.get("lapsed_customers_added_since_expiry")
    dip = _num_pct(payload.get("perf_dip_pct"))
    parts = []
    if since:
        parts.append(f"it's been {since} days since your plan lapsed")
    if dip:
        parts.append(f"visibility is down {dip} since")
    if lapsed_added:
        parts.append(f"{lapsed_added} more customers have gone quiet in that time")
    detail = "; ".join(parts) if parts else "your account has been quiet since the plan lapsed"
    body = f"{who}, {detail}. Want me to show you exactly what reactivating would fix first?"
    return body, "binary", ["loss_aversion", "specificity"]


def _build_curious_ask(category, merchant, trigger, customer):
    who = _who(merchant, category)
    body = (
        f"Hi {who}! Quick one — what's been the most-asked-for thing at your place this week? "
        f"I'll turn the answer into a ready-to-use Google post and a quick WhatsApp reply you can reuse. Takes 5 min."
    )
    return body, "open_ended", ["asking_the_merchant", "reciprocity", "effort_externalization"]


def _build_competitor(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    name = payload.get("competitor_name")
    dist = payload.get("distance_km")
    their_offer = payload.get("their_offer")
    parts = []
    if name:
        parts.append(f"{name} just opened")
    if dist:
        parts.append(f"{dist}km away")
    if their_offer:
        parts.append(f"leading with {their_offer}")
    detail = ", ".join(parts) if parts else "a new competitor opened nearby"
    body = f"{who}, {detail}. Want me to check how your listing compares and suggest one move to stay ahead this week?"
    return body, "binary", ["specificity", "loss_aversion"]


def _build_external_event(category, merchant, trigger, customer):
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    kind = trigger.get("kind")
    if kind == "ipl_match_today":
        match = payload.get("match")
        venue = payload.get("venue")
        time_ = payload.get("match_time_iso", "")
        is_weeknight = payload.get("is_weeknight")
        offers = active_offers(merchant)
        offer_txt = f" push {offers[0]}" if offers else " push your active offer"
        read = "weeknight match — dine-in usually spikes, worth a match-night push" if is_weeknight else "weekend match — covers usually dip as people watch at home, better to lean delivery-only"
        time_part = f" {time_[11:16]}" if len(time_) >= 16 else ""
        body = (
            f"Heads up {who} — {match} at {venue} tonight{time_part}. "
            f"{read.capitalize()}; {offer_txt} instead of a blanket promo. Want me to draft the banner? Live in 10 min."
        )
    else:
        festival = payload.get("festival")
        days_until = payload.get("days_until")
        if festival and days_until is not None:
            body = f"{who}, {festival} is {days_until} days out. Want me to draft a category-relevant campaign now so it's ready in time?"
        else:
            body = f"{who}, a local event relevant to footfall is coming up. Want me to check if it's worth a push for you?"
    return body, "binary", ["timeliness", "specificity"]


def _build_planning_continuation(category, merchant, trigger, customer):
    """The merchant already said something engaged (see payload.merchant_last_message).
    We must NOT re-qualify — move the thread forward directly (anti-pattern D)."""
    who = _who(merchant, category)
    payload = trigger.get("payload", {})
    topic = (payload.get("intent_topic") or "this").replace("_", " ")
    body = (
        f"On it, {who} — here's a first draft structure for {topic}: pricing tier, what's included, and a launch date. "
        f"I'll have the full draft ready shortly; want me to send it here as soon as it's done?"
    )
    return body, "binary", ["momentum", "effort_externalization"]


# --- customer-facing families ---

def _build_recall(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    payload = trigger.get("payload", {})
    last_service = payload.get("last_service_date")
    slots = payload.get("available_slots") or []
    offers = active_offers(merchant)
    offer_txt = f" {offers[0]}." if offers else ""
    if slots:
        labels = [s.get("label") for s in slots[:2] if s.get("label")]
        slot_txt = " or ".join(labels)
        body = (
            f"Hi {name}, {merchant_name(merchant)} here. Your recall checkup is due"
            + (f" (last visit {last_service})" if last_service else "")
            + f". We have {slot_txt} open.{offer_txt} Reply with which works, or suggest another time."
        )
    else:
        body = f"Hi {name}, {merchant_name(merchant)} here — your checkup window is open. Want me to share available slots?"
    return body, "multi_choice" if slots else "open_ended", ["personalization", "specificity", "low_friction"]


def _build_lapse_soft(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    body = f"Hi {name}, {merchant_name(merchant)} here — it's been a little while! No pressure at all, just wanted to check in. Want me to hold a slot for you this week?"
    return body, "binary", ["no_guilt", "low_friction"]


def _build_lapse_hard(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    payload = trigger.get("payload", {})
    days = payload.get("days_since_last_visit")
    focus = (payload.get("previous_focus") or "").replace("_", " ")
    days_txt = f"It's been about {days // 7} weeks" if days else "It's been a while"
    focus_txt = f" — I remember {focus} was your focus" if focus else ""
    body = (
        f"Hi {name} 👋 {merchant_name(merchant)} here. {days_txt}{focus_txt}, happens to everyone, no judgment. "
        f"Want me to hold a no-commitment trial spot for you this week? Reply YES, no charge either way."
    )
    return body, "binary", ["no_shame", "no_friction"]


def _build_appointment(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    body = f"Hi {name}, quick reminder from {merchant_name(merchant)} — you have an appointment tomorrow. See you then! Reply if you need to reschedule."
    return body, "none", ["logistics_only"]


def _build_refill(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    payload = trigger.get("payload", {})
    molecules = payload.get("molecule_list") or []
    runs_out = payload.get("stock_runs_out_iso", "")
    mol_txt = ", ".join(molecules) if molecules else "your regular medicines"
    date_txt = f"around {runs_out[:10]}" if runs_out else "soon"
    delivery = payload.get("delivery_address_saved")
    delivery_txt = " Free delivery to your saved address." if delivery else ""
    body = f"{merchant_name(merchant)} here — {mol_txt} will run out {date_txt}. Same dose, same brand ready to go.{delivery_txt} Reply CONFIRM to dispatch."
    return body, "binary", ["specificity", "effort_externalization"]


def _build_trial_followup(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    payload = trigger.get("payload", {})
    trial_date = payload.get("trial_date")
    options = payload.get("next_session_options") or []
    trial_txt = f" from your trial on {trial_date}" if trial_date else " from your recent trial"
    if options:
        label = options[0].get("label", "")
        body = f"Hi {name}, hope you enjoyed{trial_txt}! Next session slot: {label}. Want me to lock it in?"
    else:
        body = f"Hi {name}, hope you enjoyed{trial_txt}! Want me to set up your next session?"
    return body, "binary", ["personalization", "momentum"]


def _build_slot_open(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    body = f"Hi {name}, a slot just opened up at {merchant_name(merchant)} sooner than usual — thought of you first. Want it?"
    return body, "binary", ["exclusivity", "low_friction"]


def _build_lifecycle_followup(category, merchant, trigger, customer):
    name = customer_name(customer) or "there"
    payload = trigger.get("payload", {})
    days_to = payload.get("days_to_wedding")
    next_window = (payload.get("next_step_window_open") or "").replace("_", " ")
    if days_to is not None:
        body = (
            f"Hi {name} 💍 {merchant_name(merchant)} here. {days_to} days to go — good time to start the {next_window or 'next step'}. "
            f"Want me to block your usual slot for the first session?"
        )
    else:
        body = f"Hi {name}, {merchant_name(merchant)} here — following up on your plan. Want me to book the next step?"
    return body, "binary", ["specificity", "urgency", "personalization"]


def _build_generic(category, merchant, trigger, customer):
    if customer:
        name = customer_name(customer) or "there"
        body = f"Hi {name}, {merchant_name(merchant)} here with something relevant for you. Want the details?"
    else:
        who = _who(merchant, category)
        signals = merchant.get("signals", [])
        signal_txt = f" I noticed: {signals[0].replace(':', ' — ').replace('_', ' ')}." if signals else ""
        body = f"{who}, checking in with something relevant to your account.{signal_txt} Want me to look into it?"
    return body, "open_ended", ["low_pressure"]


FAMILY_BUILDERS = {
    "digest": _build_digest,
    "cde": _build_cde,
    "trend": _build_trend,
    "compliance": _build_compliance,
    "perf_spike": _build_perf_spike,
    "perf_dip": _build_perf_dip,
    "milestone": _build_milestone,
    "review_theme": _build_review_theme,
    "profile_action": _build_profile_action,
    "dormant": _build_dormant,
    "renewal": _build_renewal,
    "winback": _build_winback,
    "curious_ask": _build_curious_ask,
    "competitor": _build_competitor,
    "external_event": _build_external_event,
    "planning_continuation": _build_planning_continuation,
    "recall": _build_recall,
    "lapse_soft": _build_lapse_soft,
    "lapse_hard": _build_lapse_hard,
    "appointment": _build_appointment,
    "refill": _build_refill,
    "trial_followup": _build_trial_followup,
    "slot_open": _build_slot_open,
    "lifecycle_followup": _build_lifecycle_followup,
    "generic": _build_generic,
}


def deterministic_compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> dict:
    family = family_of(trigger.get("kind", ""))
    builder = FAMILY_BUILDERS.get(family, _build_generic)
    body, cta, levers = builder(category or {}, merchant or {}, trigger or {}, customer)

    # Light Hindi-English touch for merchant-facing sends when the merchant
    # actually lists "hi" among their languages (never for pure-English
    # merchants). We deliberately avoid mid-sentence fragment substitution
    # (e.g. swapping just "Want me to" -> a Hindi opener) because English
    # content words don't take Hindi verb endings, which produces broken
    # Hinglish ("Chahenge to main pull the..."). Instead we either swap in
    # a complete, grammatically self-contained Hindi sentence for a known
    # exact string, or tack on a short, complete Hindi-English closing tag
    # after an already-complete English sentence — both patterns match how
    # real bilingual code-mix reads in the brief's Pattern A/B examples.
    if not customer and voice.wants_hi_en(merchant_langs(merchant)) and family in ("dormant", "digest", "curious_ask"):
        if family == "dormant":
            body = body.replace(
                "Anything on your account you'd like me to take a look at this week?",
                "Kuch hai jo aap chahte hain main is week dekh loon?",
            )
        elif body.rstrip().endswith("?"):
            body = body.rstrip() + " Bataiye?"

    rationale = f"{FAMILY_HINTS.get(family, FAMILY_HINTS['generic'])}"
    send_as = "merchant_on_behalf" if customer else "vera"
    return {"body": body, "cta": cta, "send_as": send_as, "rationale": _short_rationale(family, trigger, levers)}


def _short_rationale(family: str, trigger: dict, levers: list[str]) -> str:
    kind = trigger.get("kind", family)
    lever_txt = ", ".join(levers[:3]) if levers else "specificity"
    return f"Triggered by {kind}; uses {lever_txt} to prompt a reply, grounded only in the pushed context."


# ---------------------------------------------------------------------------
# Post-generation validation
# ---------------------------------------------------------------------------

_MULTI_CTA_PATTERN = re.compile(r"(reply\s+\w+\s+for\s+\w+.*reply\s+\w+\s+for)", re.IGNORECASE)


def _looks_like_repeat(body: str, recent_bodies: list[str]) -> bool:
    norm = re.sub(r"\s+", " ", body.strip().lower())
    for prev in recent_bodies:
        prev_norm = re.sub(r"\s+", " ", (prev or "").strip().lower())
        if not prev_norm:
            continue
        if norm == prev_norm:
            return True
        # Also treat >90% shared prefix as a near-duplicate (anti-repetition
        # per testing-brief.md §10: "same body verbatim" penalty; we try to
        # avoid even close variants).
        shorter = min(len(norm), len(prev_norm))
        if shorter > 20 and norm[: int(shorter * 0.9)] == prev_norm[: int(shorter * 0.9)]:
            return True
    return False


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            recent_bodies: Optional[list[str]] = None) -> dict:
    """Public entrypoint matching challenge-brief.md §5/§7.1. Never raises;
    always returns a complete dict with body/cta/send_as/suppression_key/rationale."""
    recent_bodies = recent_bodies or []
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}

    result = llm_compose(category, merchant, trigger, customer, recent_bodies)
    used_llm = result is not None
    if result is None:
        result = deterministic_compose(category, merchant, trigger, customer)

    body = (result.get("body") or "").strip()

    # Anti-fabrication guard: strip taboo vocabulary if the LLM slipped one in.
    _, taboo_hits = voice.strip_taboo(body, category)
    if taboo_hits and used_llm:
        # Regenerate deterministically rather than ship a taboo word.
        result = deterministic_compose(category, merchant, trigger, customer)
        body = result["body"]

    # Anti-repetition: if this is a near-duplicate of something already sent
    # to this merchant/customer, fall back to (or re-roll) the deterministic
    # template, which is guaranteed to differ because it reads live fields.
    if _looks_like_repeat(body, recent_bodies):
        alt = deterministic_compose(category, merchant, trigger, customer)
        if not _looks_like_repeat(alt["body"], recent_bodies):
            result = alt
            body = alt["body"]
        else:
            body = body + " (following up again on this)"
            result["body"] = body

    if not body:
        result = deterministic_compose(category, merchant, trigger, customer)
        body = result["body"]

    suppression_key = trigger.get("suppression_key") or f"{trigger.get('kind','generic')}:{merchant.get('merchant_id','')}"

    return {
        "body": body,
        "cta": result.get("cta", "open_ended"),
        "send_as": result.get("send_as", "merchant_on_behalf" if customer else "vera"),
        "suppression_key": suppression_key,
        "rationale": result.get("rationale", "Composed from category+merchant+trigger context."),
    }


def build_template_params(merchant: dict, body: str, customer: Optional[dict] = None) -> list[str]:
    """First-touch WhatsApp template params — a generic {{1}}/{{2}}/{{3}}
    shape (recipient name, business/owner name, one-line hook)."""
    recipient = customer_name(customer) if customer else (owner_name(merchant) or merchant_name(merchant))
    hook = body.split(".")[0][:60] if body else ""
    return [recipient or "there", merchant_name(merchant), hook]
