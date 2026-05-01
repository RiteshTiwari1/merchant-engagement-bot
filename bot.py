import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from pydantic import BaseModel

from state import state
from composer import compose_proactive, compose_reply

app = FastAPI(title="Vera Bot — magicpin AI Challenge")
_START = time.time()

_OPT_OUT_PHRASES = {"stop", "unsubscribe", "band karo", "nahi chahiye", "mat bhejo", "remove me", "do not contact"}


def _now_z() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

# ─── Request models ────────────────────────────────────────────────────────────

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _START),
        "contexts_loaded": state.count_contexts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.environ.get("TEAM_NAME", "Solo"),
        "team_members": [os.environ.get("TEAM_NAME", "Solo")],
        "model": "gpt-4.1 (two-pass: compose + critique)",
        "approach": (
            "Two-pass composition: gpt-4.1 first draft + gpt-4.1 critique-and-revise. "
            "Per-category voice prompts (5 distinct voices) + per-trigger-kind playbooks. "
            "Forced reasoning fields (decision_reasoning, key_facts) before body — drives Decision Quality. "
            "Deterministic fact validator (numbers/citations checked vs input context) catches fabrications. "
            "Digest items pre-ranked by merchant signal alignment. "
            "Auto-reply detection (pattern + consecutive-repeat). Intent transition handler. "
            "Hindi-English code-mix for warm contexts; English for analytical. "
            "Customer-facing flow uses merchant voice (not Vera's) with explicit identity opener."
        ),
        "contact_email": os.environ.get("CONTACT_EMAIL", ""),
        "version": "1.0.0",
        "submitted_at": _now_z(),
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {valid_scopes}"}

    accepted, cur_version = await state.store_context(body.scope, body.context_id, body.version, body.payload)
    if not accepted:
        return {"accepted": False, "reason": "stale_version", "current_version": cur_version}

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_z(),
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    eligible = []

    for trg_id in body.available_triggers:
        trg = state.get_context("trigger", trg_id)
        if not trg:
            continue

        supp_key = trg.get("suppression_key", "")
        if state.is_suppressed(supp_key):
            continue

        merchant_id = trg.get("merchant_id")
        if not merchant_id:
            continue
        if state.is_merchant_blocked(merchant_id):
            continue
        if state.has_active_conversation(merchant_id):
            continue

        merchant = state.get_context("merchant", merchant_id)
        if not merchant:
            continue

        category_slug = merchant.get("category_slug", "")
        category = state.get_context("category", category_slug)
        if not category:
            continue

        customer_id = trg.get("customer_id")
        customer = state.get_context("customer", customer_id) if customer_id else None
        # Skip customer-scoped trigger if customer context not pushed — prevents hallucination
        if customer_id and not customer:
            continue

        eligible.append({
            "trg_id": trg_id,
            "trg": trg,
            "merchant_id": merchant_id,
            "merchant": merchant,
            "category": category,
            "customer_id": customer_id,
            "customer": customer,
            "urgency": trg.get("urgency", 1),
        })

    # Sort highest urgency first; cap at 20 (judge max per tick)
    eligible.sort(key=lambda x: -x["urgency"])
    eligible = eligible[:20]

    async def process_one(item: dict) -> Optional[dict]:
        result = await compose_proactive(
            category=item["category"],
            merchant=item["merchant"],
            trigger=item["trg"],
            customer=item["customer"],
            now=body.now,
        )
        if not result:
            return None
        msg_body = result.get("body", "").strip()
        if not msg_body:
            return None

        conv_id = f"conv_{item['merchant_id']}_{item['trg_id']}_{uuid.uuid4().hex[:6]}"
        supp_key = result.get("suppression_key") or item["trg"].get("suppression_key", "")

        state.create_conversation(conv_id, item["merchant_id"], item["customer_id"], item["trg_id"], msg_body)
        state.suppress(supp_key)

        kind = item["trg"].get("kind", "generic")
        # Deterministic send_as: customer-scoped trigger or customer context present → merchant_on_behalf
        is_customer_facing = item["trg"].get("scope") == "customer" or item["customer_id"]
        send_as = "merchant_on_behalf" if is_customer_facing else "vera"

        return {
            "conversation_id": conv_id,
            "merchant_id": item["merchant_id"],
            "customer_id": item["customer_id"],
            "send_as": send_as,
            "trigger_id": item["trg_id"],
            "template_name": result.get("template_name", f"vera_{kind}_v1"),
            "template_params": result.get("template_params", []),
            "body": msg_body,
            "cta": result.get("cta", "open_ended"),
            "suppression_key": supp_key,
            "rationale": result.get("rationale", ""),
        }

    results = await asyncio.gather(*[process_one(item) for item in eligible], return_exceptions=True)
    actions = [r for r in results if isinstance(r, dict)]
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = state.get_conversation(body.conversation_id)

    # Lazily create conversation if missing (judge may resume after bot restart,
    # or send replies for conversations whose initial /v1/tick we never saw).
    if not conv:
        merchant_id = body.merchant_id or "unknown"
        state.create_conversation(
            conv_id=body.conversation_id,
            merchant_id=merchant_id,
            customer_id=body.customer_id,
            trigger_id="resumed",
            initial_body="(conversation resumed; prior context not retained)",
        )
        conv = state.get_conversation(body.conversation_id)

    if conv["status"] == "ended":
        return {"action": "end", "rationale": "Conversation already closed."}

    state.record_merchant_turn(body.conversation_id, body.message)
    conv = state.get_conversation(body.conversation_id)

    # Deterministic opt-out: block merchant from all future outreach
    msg_lower = body.message.lower().strip()
    if any(phrase in msg_lower for phrase in _OPT_OUT_PHRASES):
        state.block_merchant(body.merchant_id or conv["merchant_id"])
        state.end_conversation(body.conversation_id)
        return {"action": "end", "rationale": "Merchant opted out; blocked from future outreach."}

    # Deterministic auto-reply handling — don't burn LLM calls on canned replies
    # auto_count tracks same conv_id; turn_number handles when judge uses fresh conv_id per turn
    auto_count = conv.get("auto_reply_count", 0)
    if auto_count >= 3 or (auto_count >= 1 and body.turn_number >= 4):
        state.end_conversation(body.conversation_id)
        return {"action": "end", "rationale": "Auto-reply loop detected; closing conversation."}
    if auto_count == 2 or (auto_count >= 1 and body.turn_number == 3):
        return {"action": "wait", "wait_seconds": 86400, "rationale": "Auto-reply detected twice; backing off 24 hours to wait for the owner."}

    merchant_id = body.merchant_id or conv["merchant_id"]
    merchant = state.get_context("merchant", merchant_id) or {}
    category_slug = merchant.get("category_slug", "")
    category = state.get_context("category", category_slug) if category_slug else {}
    customer_id = body.customer_id or conv.get("customer_id")
    customer = state.get_context("customer", customer_id) if customer_id else None

    action = await compose_reply(
        conversation=conv,
        merchant=merchant,
        category=category,
        message=body.message,
        turn_number=body.turn_number,
        customer=customer,
        from_role=body.from_role,
    )

    act = action.get("action", "wait")
    msg_body = action.get("body", "").strip()

    if act == "send":
        if not msg_body:
            act = "wait"
            action["wait_seconds"] = 1800
            action["rationale"] = "Empty body generated; backing off."
        else:
            # Anti-repetition guard
            if state.is_repeated_body(body.conversation_id, msg_body):
                msg_body = msg_body + " — let me know if you'd like more detail."
            state.record_vera_turn(body.conversation_id, msg_body)
            # Force-end after turn 5
            if body.turn_number >= 5:
                state.end_conversation(body.conversation_id)
    elif act == "end":
        state.end_conversation(body.conversation_id)

    response: dict[str, Any] = {"action": act, "rationale": action.get("rationale", "")}
    if act == "send":
        response["body"] = msg_body
        response["cta"] = action.get("cta", "open_ended")
    elif act == "wait":
        response["wait_seconds"] = action.get("wait_seconds", 3600)
    elif act == "end" and action.get("body", "").strip():
        # Include polite closing body when LLM provides one (e.g. hostile/off-topic acknowledgment)
        response["body"] = action["body"].strip()
    return response


@app.post("/v1/teardown")
async def teardown():
    state.wipe()
    return {"status": "wiped"}
