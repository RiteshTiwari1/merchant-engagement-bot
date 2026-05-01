import asyncio
from typing import Optional

_AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "we will get back to you",
    "our team will respond",
    "will respond shortly",
    "we are currently",
    "outside business hours",
    "namaste, thank you",
    "thanks for reaching out",
    "we'll be in touch",
    "auto-reply",
    "nahi hain abhi",
    "is waqt available nahi",
]


def is_auto_reply(message: str) -> bool:
    msg = message.lower().strip()
    return any(p in msg for p in _AUTO_REPLY_PATTERNS)


class BotState:
    def __init__(self):
        self._lock = asyncio.Lock()
        # (scope, context_id) -> {version: int, payload: dict}
        self.contexts: dict[tuple[str, str], dict] = {}
        # conversation_id -> conversation state dict
        self.conversations: dict[str, dict] = {}
        # suppression keys already sent this session
        self.suppressed: set[str] = set()
        # merchants that replied STOP / opted out
        self.blocked_merchants: set[str] = set()

    async def store_context(self, scope: str, ctx_id: str, version: int, payload: dict) -> tuple[bool, Optional[int]]:
        async with self._lock:
            key = (scope, ctx_id)
            cur = self.contexts.get(key)
            if cur and cur["version"] > version:
                return False, cur["version"]
            self.contexts[key] = {"version": version, "payload": payload}
            return True, None

    def get_context(self, scope: str, ctx_id: str) -> Optional[dict]:
        entry = self.contexts.get((scope, ctx_id))
        return entry["payload"] if entry else None

    def count_contexts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for scope, _ in self.contexts:
            if scope in counts:
                counts[scope] += 1
        return counts

    def is_suppressed(self, key: str) -> bool:
        return bool(key) and key in self.suppressed

    def suppress(self, key: str):
        if key:
            self.suppressed.add(key)

    def block_merchant(self, merchant_id: str):
        if merchant_id:
            self.blocked_merchants.add(merchant_id)

    def is_merchant_blocked(self, merchant_id: str) -> bool:
        return bool(merchant_id) and merchant_id in self.blocked_merchants

    def has_active_conversation(self, merchant_id: str) -> bool:
        return any(
            c["merchant_id"] == merchant_id and c["status"] == "active"
            for c in self.conversations.values()
        )

    def create_conversation(self, conv_id: str, merchant_id: str, customer_id: Optional[str],
                            trigger_id: str, initial_body: str):
        self.conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trigger_id,
            "status": "active",
            "turns": [{"role": "vera", "body": initial_body}],
            "auto_reply_count": 0,
            "bodies_sent": {initial_body},
        }

    def get_conversation(self, conv_id: str) -> Optional[dict]:
        return self.conversations.get(conv_id)

    def end_conversation(self, conv_id: str):
        conv = self.conversations.get(conv_id)
        if conv:
            conv["status"] = "ended"

    def record_merchant_turn(self, conv_id: str, message: str):
        conv = self.conversations.get(conv_id)
        if not conv:
            return
        conv["turns"].append({"role": "merchant", "body": message})
        msg_lower = message.lower().strip()
        if is_auto_reply(message):
            conv["auto_reply_count"] += 1
        else:
            # Repeated identical message also signals auto-reply
            prev_merchant = [t["body"] for t in conv["turns"][:-1] if t["role"] == "merchant"]
            if prev_merchant and prev_merchant[-1].strip().lower() == msg_lower:
                conv["auto_reply_count"] += 1
            else:
                conv["auto_reply_count"] = 0

    def record_vera_turn(self, conv_id: str, body: str):
        conv = self.conversations.get(conv_id)
        if not conv:
            return
        conv["turns"].append({"role": "vera", "body": body})
        conv["bodies_sent"].add(body)

    def is_repeated_body(self, conv_id: str, body: str) -> bool:
        conv = self.conversations.get(conv_id)
        return bool(conv and body in conv.get("bodies_sent", set()))

    def wipe(self):
        self.contexts.clear()
        self.conversations.clear()
        self.suppressed.clear()
        self.blocked_merchants.clear()


state = BotState()
