# Vera Bot — magicpin AI Challenge

**Submitted by**: Ritesh Tiwari | ritiktiwari2212@gmail.com

---

## Approach

Two-pass composition using GPT-4.1 (compose + critique-and-revise). Every message is built from all four context layers — category voice rules, merchant-specific metrics, trigger signals, and optional customer state — before writing a single word.

**Key design decisions:**

1. **Trigger relevance first**: Every message leads with WHY NOW — the specific trigger, cited by source, connected to this merchant's actual signals. Not generic advice.

2. **Per-category voice + per-trigger playbooks**: 5 distinct category voices (dentist=peer_clinical, restaurant=lively_commercial, etc.) and 18 trigger-kind playbooks ensure the right tone and structure for every scenario.

3. **Two-pass composition**: GPT-4.1 first draft → deterministic fact validator (checks numbers, citations, taboo words) → GPT-4.1 critique-and-revise. Fixes fabrications and weak CTAs before sending.

4. **Best-of-2 sampling**: Two parallel candidates at t=0.7 and t=1.1; the one with fewer validator issues wins. Ensures genuine variation, not just reruns.

5. **Auto-reply detection (two-layer)**: Pattern matching handles canned WA Business replies instantly. `auto_reply_count` accumulates: casual flag to owner (count=1) → wait 24h (count=2) → end gracefully (count≥3).

6. **Intent transition hardcoded**: "yes / haan karo / confirm / let's go" → immediate action mode. Never asks another qualifying question.

7. **Customer-facing deterministic rule**: `send_as = "merchant_on_behalf"` if trigger scope is "customer" OR customer_id is set. Vera persona drops completely; merchant identity + locality opener is mandatory.

8. **Hindi/Devanagari support**: Matches `language_pref` per customer AND detects language switches mid-conversation in replies.

---

## Stack

- Python 3.9+ / FastAPI / Uvicorn
- OpenAI GPT-4.1 (function calling for structured output)
- In-memory state (sufficient for 60-min test window)

---

## Run locally

```bash
cp .env.example .env
# Edit .env: add OPENAI_API_KEY, TEAM_NAME, CONTACT_EMAIL

pip install -r requirements.txt

uvicorn bot:app --host 0.0.0.0 --port 8080
```

---

## Deploy (Railway)

1. Push to GitHub
2. Connect repo on railway.app
3. Set environment variables: `OPENAI_API_KEY`, `TEAM_NAME`, `CONTACT_EMAIL`
4. Railway reads `Procfile` automatically — start command: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
