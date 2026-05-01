import asyncio
import json
import os
import re
from typing import Optional

from openai import AsyncOpenAI

from prompts import RUBRIC, HARD_RULES, WINNING_PATTERNS, REPLY_SYSTEM, CUSTOMER_FACING_RULES, get_voice, get_playbook, get_few_shot_compose_example

_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Model picks tuned for quality + 30s tick budget:
# - gpt-4.1: ~3-5s per call, 100% success rate at scale, supports function calling reliably
# - Two passes (compose + critique) ≈ 6-10s total, comfortable margin under 30s tick budget
# - o3/gpt-5 produce marginally better text but timeouts at scale lose far more points than they gain
COMPOSE_MODEL = "gpt-4.1"
CRITIQUE_MODEL = "gpt-4.1"
REPLY_MODEL = "gpt-4.1"

_sem: Optional[asyncio.Semaphore] = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(12)
    return _sem


# ─── Function schemas ─────────────────────────────────────────────────────────

_COMPOSE_FUNCTION = {
    "name": "compose_message",
    "description": "Output the composed WhatsApp message after first reasoning about the strongest signal.",
    "parameters": {
        "type": "object",
        "properties": {
            "decision_reasoning": {
                "type": "string",
                "description": (
                    "FILL THIS FIRST — the judge reads this field directly for Decision Quality scoring. "
                    "1-2 sentences: (1) name the SINGLE strongest signal from trigger+merchant+category, "
                    "(2) state WHY THIS MERCHANT, WHY TODAY specifically, "
                    "(3) state the exact action you are prescribing. "
                    "WEAK (scores 4/10): 'Merchant has low orders so I will suggest improvements.' "
                    "STRONG (scores 10/10): 'Orders -32% MoM + 3 new competitors opened this week + IPL season = "
                    "merchant risks losing lunch crowd permanently before competitors lock in habits. "
                    "Prescribing a priced Weekday Thali targeting office lunch crowd + delivery-only BOGO for match nights, "
                    "this week before the weekend fixtures.' "
                    "Always derive a non-obvious, time-specific insight — not just a restatement of the trigger."
                ),
            },
            "key_facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "3-5 specific verifiable facts FROM THE INPUT CONTEXT that appear in the body. "
                    "Each must contain the exact number/date/name as it appears in context. "
                    "Example: '38% lower caries recurrence (JIDA Oct 2026, p.14, n=2100 trial)' "
                    "or 'Orders down 32% MoM (98 vs 145 last month)' or 'Dental Cleaning @ ₹299 (active offer)'. "
                    "Do NOT invent numbers — if you cannot find it in context, omit that fact."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "The WhatsApp message body. Rules: no URLs, no preambles ('Hope you are well'), "
                    "specific and personal (use owner name, exact numbers), ONE CTA in the LAST sentence only. "
                    "Length: 3-5 lines maximum."
                ),
            },
            "cta": {"type": "string", "enum": ["binary_yes_no", "open_ended", "multi_choice", "none"]},
            "send_as": {"type": "string", "enum": ["vera", "merchant_on_behalf"]},
            "template_name": {"type": "string"},
            "template_params": {"type": "array", "items": {"type": "string"}},
            "suppression_key": {"type": "string"},
            "rationale": {"type": "string", "description": "1 sentence — what signal drove this message."},
        },
        "required": ["decision_reasoning", "key_facts", "body", "cta", "send_as",
                     "template_name", "template_params", "suppression_key", "rationale"],
    },
}

_CRITIQUE_FUNCTION = {
    "name": "critique_and_revise",
    "description": "Critique the message against scoring rubric and produce an improved version.",
    "parameters": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific issues that reduce any of the 5 scoring dimensions. Check: "
                    "(1) Decision Quality: is the reasoning generic/restated-trigger, or genuinely contrarian+time-specific? "
                    "(2) Specificity: any number/date/name NOT verifiable in context? "
                    "(3) Category Fit: wrong salutation? taboo word? wrong tone register? "
                    "(4) Merchant Fit: owner name missing? no specific metrics cited? "
                    "(5) Engagement Compulsion: more than 1 question? CTA not in last sentence? no reason to reply NOW? "
                    "Empty list only if ALL 5 are genuinely strong."
                ),
            },
            "improvements_needed": {
                "type": "boolean",
                "description": "True if ANY of the 5 scoring dimensions has an issue that materially hurts the score.",
            },
            "revised_body": {
                "type": "string",
                "description": (
                    "Revised message body fixing every issue identified. "
                    "Keep all correct numbers verbatim — do NOT change any figure that was verified in context. "
                    "If improvements_needed=false, copy original body exactly."
                ),
            },
            "revised_cta": {"type": "string", "enum": ["binary_yes_no", "open_ended", "multi_choice", "none"]},
            "revised_rationale": {"type": "string"},
        },
        "required": ["issues", "improvements_needed", "revised_body", "revised_cta", "revised_rationale"],
    },
}

_REPLY_FUNCTION = {
    "name": "reply_action",
    "description": "Decide the next action in the ongoing conversation.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["send", "wait", "end"]},
            "body": {"type": "string"},
            "cta": {"type": "string", "enum": ["binary_yes_no", "open_ended", "multi_choice", "none"]},
            "wait_seconds": {"type": "integer"},
            "rationale": {"type": "string"},
        },
        "required": ["action", "rationale"],
    },
}

# ─── Digest pre-ranking ───────────────────────────────────────────────────────

def _rank_digest(digest: list[dict], merchant_signals: list[str], merchant_agg: dict) -> list[dict]:
    if not digest:
        return []
    signal_text = " ".join(merchant_signals).lower()
    has_high_risk = "high_risk_adult" in signal_text or merchant_agg.get("high_risk_adult_count", 0) > 50
    has_lapsed = "lapsed" in signal_text or merchant_agg.get("lapsed_180d_plus", 0) > 30
    is_dormant = "dormant" in signal_text
    is_below_peer = "below_peer" in signal_text

    def score(item: dict) -> int:
        s = 0
        kind = item.get("kind", "").lower()
        if kind == "compliance": s += 10
        if kind == "research":
            seg = item.get("patient_segment", "").lower()
            if has_high_risk and "high_risk" in seg: s += 8
            elif has_lapsed and ("retention" in seg or "lapsed" in seg): s += 8
            else: s += 3
        if kind == "cde" and not is_dormant: s += 4
        if kind == "trend" and is_below_peer: s += 6
        elif kind == "trend": s += 2
        if kind == "tech": s += 2
        if item.get("trial_n", 0) > 1000: s += 2
        return s

    return sorted(digest, key=score, reverse=True)


# ─── Context formatters ────────────────────────────────────────────────────────

def _fmt_merchant(m: dict) -> str:
    identity = m.get("identity", {})
    perf = m.get("performance", {})
    delta = perf.get("delta_7d", {})
    offers = m.get("offers", [])
    signals = m.get("signals", [])
    history = m.get("conversation_history", [])
    agg = m.get("customer_aggregate", {})
    sub = m.get("subscription", {})
    review_themes = m.get("review_themes", [])

    owner = identity.get("owner_first_name") or identity.get("name", "Owner").split()[0]
    active_offers = [o["title"] for o in offers if o.get("status") == "active"]
    expired_offers = [o["title"] for o in offers if o.get("status") == "expired"]

    views_delta = delta.get("views_pct", 0)
    calls_delta = delta.get("calls_pct", 0)

    est_year = identity.get("established_year")
    identity_line = (
        "MERCHANT: " + identity.get("name", "Unknown") + " | " + identity.get("locality", "") + ", " + identity.get("city", "")
        + (" | Est. " + str(est_year) if est_year else "")
    )

    parts = [
        identity_line,
        "Owner first name (USE THIS in salutation): " + owner,
        "Verified: " + str(identity.get("verified", False)) + " | Languages: " + ", ".join(identity.get("languages", ["en"])),
        "Plan: " + sub.get("plan", "N/A") + " (" + str(sub.get("days_remaining", "?")) + " days remaining) | Status: " + sub.get("status", "unknown"),
        "",
        "PERFORMANCE (30d): Views " + str(perf.get("views", 0)) + " (7d: " + (("+" if views_delta >= 0 else "") + str(round(views_delta * 100)) + "%") + ") | "
        "Calls " + str(perf.get("calls", 0)) + " (7d: " + (("+" if calls_delta >= 0 else "") + str(round(calls_delta * 100)) + "%") + ") | "
        "CTR " + str(round(perf.get("ctr", 0) * 100, 1)) + "% | Leads " + str(perf.get("leads", 0)) + " | Directions " + str(perf.get("directions", 0)),
        "SIGNALS (use these to ground WHY NOW): " + (", ".join(signals) if signals else "none"),
        "",
        "ACTIVE OFFERS (use service+price from here EXACTLY): " + (" | ".join(active_offers) if active_offers else "none"),
    ]
    if expired_offers:
        parts.append("EXPIRED OFFERS (can reactivate): " + " | ".join(expired_offers))
    if agg:
        agg_parts = [
            "Total unique YTD: " + str(agg.get("total_unique_ytd", 0)),
            "Lapsed 180d+: " + str(agg.get("lapsed_180d_plus", 0)),
            "6mo retention: " + str(round(agg.get("retention_6mo_pct", 0) * 100)) + "%",
        ]
        for k, v in agg.items():
            if k.endswith("_count") and isinstance(v, int) and v > 0:
                label = k.replace("_count", "").replace("_", " ")
                agg_parts.append(label + ": " + str(v))
        parts.append("CUSTOMER COHORTS: " + " | ".join(agg_parts))
    if review_themes:
        parts.append("REVIEW THEMES (cite these when composing — they're from real customer reviews):")
        for rt in review_themes:
            sentiment = "POSITIVE" if rt.get("sentiment") == "pos" else "NEGATIVE"
            parts.append(
                "  [" + sentiment + "] " + rt.get("theme", "?") + " — " + str(rt.get("occurrences_30d", 0)) + " reviews/30d"
                + (' | Example: "' + rt.get("common_quote", "") + '"' if rt.get("common_quote") else "")
            )
    if history:
        parts.append("CONVERSATION HISTORY (last " + str(min(3, len(history))) + " turns):")
        for h in history[-3:]:
            engagement = h.get("engagement", "")
            eng_note = " ← " + engagement if engagement else ""
            parts.append("  [" + h.get("from", "?") + "]: " + str(h.get("body", ""))[:120] + eng_note)
    return "\n".join(parts)


def _fmt_category_minimal(cat: dict, merchant_signals: list[str], merchant_agg: dict) -> str:
    voice = cat.get("voice", {})
    peer = cat.get("peer_stats", {})
    digest = cat.get("digest", [])
    seasonal = cat.get("seasonal_beats", [])
    catalog = cat.get("offer_catalog", [])
    trend_signals = cat.get("trend_signals", [])
    patient_content = cat.get("patient_content_library", [])
    reg_auths = cat.get("regulatory_authorities", [])
    journals = cat.get("professional_journals", [])

    ranked_digest = _rank_digest(digest, merchant_signals, merchant_agg)[:3]

    tone_examples = voice.get("tone_examples", [])
    salutation_examples = voice.get("salutation_examples", [])
    parts = [
        "CATEGORY: " + cat.get("display_name", cat.get("slug", "unknown")),
        "Voice tone: " + str(voice.get("tone", "neutral")) + " | Register: " + str(voice.get("register", "")) + " | Code-mix: " + str(voice.get("code_mix", "english")),
        "Vocab USE (naturally where relevant): " + ", ".join(voice.get("vocab_allowed", [])[:10]),
        "Vocab taboo (NEVER use): " + ", ".join(voice.get("vocab_taboo", [])[:6]),
    ]
    if salutation_examples:
        parts.append("Salutation (USE EXACTLY — e.g. 'Dr. Meera' not 'Hi' or 'Doctor'): " + " | ".join(salutation_examples))
    if tone_examples:
        parts.append("Tone examples (write like this): " + " | ".join(tone_examples[:3]))
    parts += [
        "",
        "PEER BENCHMARKS: Rating " + str(peer.get("avg_rating", 0)) + " | Reviews " + str(peer.get("avg_review_count", 0)) + " | "
        "CTR " + str(round(peer.get("avg_ctr", 0) * 100, 1)) + "% | "
        "Views/30d " + str(peer.get("avg_views_30d", 0)) + " | Calls/30d " + str(peer.get("avg_calls_30d", 0)) + " | "
        "6mo retention " + str(round(peer.get("retention_6mo_pct", 0) * 100)) + "% | "
        "Avg post every " + str(peer.get("avg_post_freq_days", 14)) + " days",
        "",
        "OFFER CATALOG (use these as-is for service+price): " + " | ".join(o["title"] for o in catalog[:6]),
        "",
        "TOP-RANKED DIGEST ITEMS (cite source+title EXACTLY — each has verifiable data you MUST quote):",
    ]
    for item in ranked_digest:
        trial_note = " (n=" + str(item["trial_n"]) + ")" if item.get("trial_n") else ""
        actionable = " | Actionable: " + item["actionable"] if item.get("actionable") else ""
        seg_note = " | Segment: " + item["patient_segment"] if item.get("patient_segment") else ""
        summary_note = "\n    Key finding (quote specific numbers): " + item["summary"] if item.get("summary") else ""
        parts.append(
            "  [" + item["id"] + "] " + item["kind"].upper() + ": \"" + item["title"] + "\"" + trial_note
            + " | Source: " + item.get("source", "unknown") + seg_note + actionable + summary_note
        )
    if reg_auths:
        parts.append("\nREGULATORY AUTHORITIES (valid citation bodies — cite these for compliance/research): " + " | ".join(reg_auths))
    if journals:
        parts.append("PROFESSIONAL JOURNALS (valid citation sources): " + " | ".join(journals))
    if seasonal:
        seasonal_str = " | ".join(s["month_range"] + ": " + s["note"] for s in seasonal)
        parts.append("\nSEASONAL BEATS: " + seasonal_str)
    if trend_signals:
        trend_str = " | ".join(
            t.get("query", "") + " +" + str(round(t.get("delta_yoy", 0) * 100)) + "% YoY"
            + (" (" + t.get("segment_age", "") + ")" if t.get("segment_age") else "")
            for t in trend_signals[:3]
        )
        parts.append("TREND SIGNALS (for specificity — cite as search/market data): " + trend_str)
    if patient_content:
        content_str = " | ".join('"' + c["title"] + '"' for c in patient_content[:3])
        parts.append("PATIENT CONTENT LIBRARY (merchant can reshare these with customers): " + content_str)
    return "\n".join(parts)


def _fmt_trigger(trg: dict) -> str:
    payload = trg.get("payload", {})
    lines = [
        "TRIGGER: kind=" + trg.get("kind", "unknown") + " | scope=" + trg.get("scope", "merchant")
        + " | source=" + trg.get("source", "?") + " | urgency=" + str(trg.get("urgency", 1)) + "/5",
        "Suppression key: " + trg.get("suppression_key", "") + " | Expires: " + trg.get("expires_at", "?"),
        "Payload: " + json.dumps(payload, ensure_ascii=False),
    ]
    # Format available_slots explicitly for slot-picking CTAs (recall_due, appointment, winback)
    slots = payload.get("available_slots", [])
    if slots:
        slot_str = " | ".join(
            "Reply " + str(i + 1) + " for " + s.get("label", s.get("iso", "?"))
            for i, s in enumerate(slots[:4])
        )
        lines.append("AVAILABLE SLOTS (use multi_choice CTA with these): " + slot_str)
    return "\n".join(lines)


def _fmt_customer(cust: dict) -> str:
    identity = cust.get("identity", {})
    rel = cust.get("relationship", {})
    prefs = cust.get("preferences", {})
    consent = cust.get("consent", {})
    state = cust.get("state", "unknown")
    state_labels = {
        "lapsed_soft": "lapsed (soft — visited before, not recently)",
        "lapsed_hard": "lapsed (hard — gone 180d+)",
        "active": "active regular",
        "new": "new customer",
        "at_risk": "at-risk of churning",
    }
    ltv = rel.get("lifetime_value")
    return "\n".join([
        "CUSTOMER: " + identity.get("name", "Unknown") + " | Language pref (MATCH THIS EXACTLY): " + identity.get("language_pref", "en"),
        "State: " + state_labels.get(state, state) + " | Age band: " + str(identity.get("age_band", "unknown")) + " | Senior: " + str(identity.get("is_senior", False)),
        "First visit: " + rel.get("first_visit", "?") + " | Last visit: " + rel.get("last_visit", "?") + " | Total visits: " + str(rel.get("visits_total", 0)) + (" | LTV: ₹" + str(ltv) if ltv else ""),
        "Services received: " + ", ".join(rel.get("services_received", [])),
        "Preferred slots: " + prefs.get("preferred_slots", "any") + " | Reminder opt-in: " + str(prefs.get("reminder_opt_in", True)),
        "Consent scope: " + ", ".join(consent.get("scope", [])),
    ])


# ─── Build per-call system prompt ─────────────────────────────────────────────

def _build_proactive_system(category_slug: str, trigger_kind: str, is_customer_facing: bool) -> str:
    base = "You are Vera, magicpin's AI assistant for merchant WhatsApp engagement.\n\n"
    base += "Your job: compose ONE WhatsApp message a merchant (or their customer) will actually reply to.\n\n"
    base += "CRITICAL: Before writing the body, fill `decision_reasoning` and `key_facts`. "
    base += "These force you to commit to a single signal and cite specific facts — driving Decision Quality and Specificity.\n\n"
    base += RUBRIC + "\n"
    base += HARD_RULES + "\n"
    base += WINNING_PATTERNS + "\n"
    voice = get_voice(category_slug)
    if voice:
        base += voice + "\n"
    playbook = get_playbook(trigger_kind)
    base += playbook + "\n"

    if is_customer_facing:
        base += CUSTOMER_FACING_RULES + "\n"
    else:
        base += """## MERCHANT-FACING (send_as=vera):
- Address merchant by owner_first_name.
- Reference their actual performance numbers + signals.
- Reference their active offers from offer_catalog.
- Reference their locality/peer benchmarks where relevant.

"""

    base += """## send_as — DETERMINISTIC RULE:
- If trigger.scope="customer" OR a customer context is provided → send_as = "merchant_on_behalf"
- Otherwise → send_as = "vera"

Call compose_message with all fields. decision_reasoning + key_facts FIRST, body LAST.
"""
    return base


# ─── Deterministic fact validator ─────────────────────────────────────────────

def _validate_facts(body: str, key_facts: list[str], category: dict, merchant: dict,
                    trigger: dict, customer: Optional[dict]) -> list[str]:
    """Returns list of detected issues. Empty list = clean."""
    issues = []

    # Build a haystack of all valid strings from input context
    haystack_parts = [
        json.dumps(category, ensure_ascii=False).lower(),
        json.dumps(merchant, ensure_ascii=False).lower(),
        json.dumps(trigger, ensure_ascii=False).lower(),
    ]
    if customer:
        haystack_parts.append(json.dumps(customer, ensure_ascii=False).lower())
    haystack = " ".join(haystack_parts)

    # Check URLs
    if re.search(r"https?://|www\.", body, re.IGNORECASE):
        issues.append("URL detected in body — Meta will reject.")

    # Check key_facts each appear in haystack
    for fact in key_facts:
        f_lower = fact.lower()
        # Extract numeric tokens and check they exist in haystack
        nums = re.findall(r"\d{2,}", f_lower)
        for n in nums:
            if n not in haystack:
                # Allow common time formats and percent strings
                if not re.match(r"^\d+(am|pm|:\d+)?$", n) and len(n) >= 3:
                    issues.append(f"Fact '{fact}' contains number '{n}' not found in input context — possible fabrication.")
                    break

    # Check body for invented citations
    citations = re.findall(r"\b(JIDA|DCI|IDA|CDSCO|DCGI|NPPA)\s+(?:Oct|Nov|Dec|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep)\s*\d{4}\b", body, re.IGNORECASE)
    for cit in citations:
        if cit.lower() not in haystack:
            issues.append(f"Citation '{cit}' not in input context — likely fabricated.")

    # Check for taboo words
    taboos = category.get("voice", {}).get("vocab_taboo", [])
    for taboo in taboos:
        if taboo.lower() in body.lower():
            issues.append(f"Taboo word '{taboo}' for category — penalty.")

    # Check for owner_first_name usage (merchant-facing only)
    if not customer and trigger.get("scope") != "customer":
        owner = merchant.get("identity", {}).get("owner_first_name", "")
        if owner and owner.lower() not in body.lower():
            issues.append(f"Owner first name '{owner}' not used — merchant fit penalty.")

    # Check for multiple questions (>1 question mark)
    qcount = body.count("?")
    if qcount > 1:
        issues.append(f"{qcount} question marks — should be ONE CTA only.")

    return issues


# ─── LLM calls ────────────────────────────────────────────────────────────────

async def _compose_one_pass(system: str, user_msg: str, max_tokens: int = 700,
                           temperature: float = 1.0,
                           few_shot_messages: Optional[list] = None) -> Optional[dict]:
    messages = [{"role": "system", "content": system}]
    if few_shot_messages:
        messages.extend(few_shot_messages)
    messages.append({"role": "user", "content": user_msg})
    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=COMPOSE_MODEL,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=messages,
                tools=[{"type": "function", "function": _COMPOSE_FUNCTION}],
                tool_choice={"type": "function", "function": {"name": "compose_message"}},
            ),
            timeout=22.0,
        )
    except Exception as e:
        print(f"[VERA][compose_one_pass ERROR] {type(e).__name__}: {e}", flush=True)
        return None
    if resp.choices[0].message.tool_calls:
        tc = resp.choices[0].message.tool_calls[0]
        try:
            return json.loads(tc.function.arguments)
        except Exception:
            return None
    return None


def _build_few_shot_messages(category_slug: str, trigger_kind: str) -> list:
    """Build few-shot message pairs for injection into compose calls."""
    example = get_few_shot_compose_example(category_slug, trigger_kind)
    if not example:
        return []
    return [
        {"role": "user", "content": example["input"]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_fewshot_1",
                "type": "function",
                "function": {
                    "name": "compose_message",
                    "arguments": json.dumps(example["output"], ensure_ascii=False),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call_fewshot_1", "content": "accepted"},
    ]


async def _critique_and_revise(composed: dict, system: str, user_msg: str, issues: list[str]) -> Optional[dict]:
    """Run critique-and-revise pass. Returns revised composed dict (with original metadata)."""
    critique_prompt = system + """

## YOUR TASK NOW: CRITIQUE AND REVISE

You previously composed a message. Now critique it and produce an improved version.

ABSOLUTE RULE — DO NOT INVENT OR ALTER NUMBERS:
- Every number, percentage, date, count, citation in the revised body MUST EXIST VERBATIM in the input context.
- If the original used "38%", do NOT change it to "29%" or any other number.
- If you can't verify a number from the input context, REMOVE the claim — don't substitute.
- Cite digest sources exactly as written in the input (e.g., "JIDA Oct 2026, p.14" — do not abbreviate or rephrase).

CRITICAL ISSUES PRE-DETECTED:
""" + ("\n".join(f"- {i}" for i in issues) if issues else "(none auto-detected; still self-critique gently)")

    critique_prompt += """

Self-critique against:
- Decision quality: Is the SINGLE strongest signal picked? Does the message add value beyond describing the trigger?
- Specificity: Every fact verifiable in input context?
- Category fit: Voice exactly right? Vocabulary correct? No taboos?
- Merchant fit: Owner name used? Specific metrics? Active offers cited correctly?
- Engagement compulsion: ONE clear CTA in last sentence? Strong "why reply now" lever?

If the original is already strong, set improvements_needed=false and copy body verbatim.
If issues exist, fix ONLY those issues — don't rewrite for the sake of rewriting (preserves accurate numbers).

Call critique_and_revise.
"""

    user_with_prev = user_msg + "\n\n## PREVIOUS COMPOSITION:\n" + json.dumps({
        "decision_reasoning": composed.get("decision_reasoning", ""),
        "key_facts": composed.get("key_facts", []),
        "body": composed.get("body", ""),
        "cta": composed.get("cta", ""),
        "rationale": composed.get("rationale", ""),
    }, ensure_ascii=False, indent=2)

    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=CRITIQUE_MODEL,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": critique_prompt},
                    {"role": "user", "content": user_with_prev},
                ],
                tools=[{"type": "function", "function": _CRITIQUE_FUNCTION}],
                tool_choice={"type": "function", "function": {"name": "critique_and_revise"}},
            ),
            timeout=25.0,
        )
    except Exception as e:
        print(f"[VERA][critique ERROR] {type(e).__name__}: {e}", flush=True)
        return None

    if resp.choices[0].message.tool_calls:
        tc = resp.choices[0].message.tool_calls[0]
        try:
            critique = json.loads(tc.function.arguments)
            # Merge revision into original composition
            revised = dict(composed)
            revised["body"] = critique.get("revised_body", composed["body"])
            revised["cta"] = critique.get("revised_cta", composed["cta"])
            revised["rationale"] = critique.get("revised_rationale", composed["rationale"])
            revised["_critique_issues"] = critique.get("issues", [])
            revised["_improvements_made"] = critique.get("improvements_needed", False)
            return revised
        except Exception:
            return None
    return None


async def compose_proactive(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    now: str = "",
) -> Optional[dict]:
    cat_slug = category.get("slug", "")
    trg_kind = trigger.get("kind", "generic")
    is_customer = trigger.get("scope") == "customer" or customer is not None
    system = _build_proactive_system(cat_slug, trg_kind, is_customer)

    parts = [
        _fmt_merchant(merchant),
        _fmt_category_minimal(category, merchant.get("signals", []), merchant.get("customer_aggregate", {})),
        _fmt_trigger(trigger),
    ]
    if customer:
        parts.append(_fmt_customer(customer))
    if now:
        try:
            from datetime import datetime, timezone as _tz
            dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
            ist_offset = 19800  # IST = UTC+5:30
            from datetime import timedelta
            dt_ist = dt + timedelta(seconds=ist_offset)
            day_name = dt_ist.strftime("%A")
            parts.append("CURRENT TIME: " + now + " (" + day_name + ", IST " + dt_ist.strftime("%H:%M") + ")")
        except Exception:
            parts.append("CURRENT TIME: " + now)
    parts.append("Now compose the message. Fill decision_reasoning + key_facts BEFORE body.")
    user_msg = "\n\n".join(parts)

    few_shot = _build_few_shot_messages(cat_slug, trg_kind)

    async with _get_sem():
        # Always best-of-2 with temperature diversity:
        # t=0.7 → focused, lower hallucination risk
        # t=1.1 → creative, higher engagement compulsion variance
        # Both run in parallel (same wall-clock cost as a single call).
        # Validator picks the cleaner candidate; critique fixes the winner.
        candidates = await asyncio.gather(
            _compose_one_pass(system, user_msg, temperature=0.7, few_shot_messages=few_shot),
            _compose_one_pass(system, user_msg, temperature=1.1, few_shot_messages=few_shot),
        )
        candidates = [c for c in candidates if c]
        if not candidates:
            return None

        # Score each candidate by validator issues (fewer = better)
        def candidate_score(c: dict) -> int:
            issues = _validate_facts(
                c.get("body", ""), c.get("key_facts", []),
                category, merchant, trigger, customer,
            )
            return -len(issues)  # negative because lower issues = better

        candidates.sort(key=candidate_score, reverse=True)
        best = candidates[0]

        # Deterministic validation on the best candidate
        issues = _validate_facts(
            best.get("body", ""), best.get("key_facts", []),
            category, merchant, trigger, customer,
        )

        # Pass 2: Critique and revise (always run for max quality)
        revised = await _critique_and_revise(best, system, user_msg, issues)
        if revised:
            return revised

        return best


async def compose_reply(
    conversation: dict,
    merchant: dict,
    category: dict,
    message: str,
    turn_number: int,
    customer: Optional[dict] = None,
    from_role: str = "merchant",
) -> dict:
    cat_slug = category.get("slug", "") if category else ""
    voice = get_voice(cat_slug)

    turns_text = "\n".join(
        "[" + str(i + 1) + "] " + t["role"].upper() + ": " + t["body"]
        for i, t in enumerate(conversation.get("turns", []))
    )

    sender_label = "customer" if from_role == "customer" else "merchant"
    parts = [
        "CONVERSATION SO FAR:\n" + turns_text,
        "NEW MESSAGE (turn " + str(turn_number) + ", from " + sender_label + "): " + message,
        "FROM_ROLE: " + from_role + (" — reply DIRECTLY to the customer (use their name, confirm their request to them). Do NOT address the merchant in this reply." if from_role == "customer" else " — reply as Vera to the merchant."),
        "AUTO-REPLY COUNT so far: " + str(conversation.get("auto_reply_count", 0)),
        "TRIGGER that started this conversation: " + conversation.get("trigger_id", "unknown"),
        _fmt_merchant(merchant) if merchant else "",
    ]
    if voice:
        parts.append(voice)
    if customer:
        parts.append(_fmt_customer(customer))
    parts.append("Decide your next action.")
    user_msg = "\n\n".join(p for p in parts if p)

    async with _get_sem():
        try:
            resp = await asyncio.wait_for(
                _client.chat.completions.create(
                    model=REPLY_MODEL,
                    max_tokens=600,
                    temperature=0.5,
                    messages=[
                        {"role": "system", "content": REPLY_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    tools=[{"type": "function", "function": _REPLY_FUNCTION}],
                    tool_choice={"type": "function", "function": {"name": "reply_action"}},
                ),
                timeout=22.0,
            )
        except Exception:
            return {"action": "wait", "wait_seconds": 1800, "rationale": "Composition error; backing off 30 min"}

    if resp.choices[0].message.tool_calls:
        tc = resp.choices[0].message.tool_calls[0]
        try:
            return json.loads(tc.function.arguments)
        except Exception:
            pass
    return {"action": "wait", "wait_seconds": 1800, "rationale": "No valid function response; backing off"}
