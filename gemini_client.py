import json
import time
from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from config import GEMINI_API_KEY, BUDGET_LIMITS

client = genai.Client(api_key=GEMINI_API_KEY)

def _generate(prompt, temperature=0.1, retries=3, delay=5):
    """
    Wrapper around generate_content with retry logic for transient 503/500 errors.
    Raises on final failure.
    """
    for attempt in range(1, retries + 1):
        try:
            return client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=temperature),
            )
        except (genai_errors.ServerError, genai_errors.APIError) as e:
            if attempt < retries:
                print(f"[gemini] Attempt {attempt} failed ({e}), retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2  # exponential backoff
            else:
                print(f"[gemini] All {retries} attempts failed: {e}")
                raise

VALID_CATEGORIES     = list(BUDGET_LIMITS.keys()) + ["Excluded"]
CONFIDENCE_THRESHOLD = 0.75

# ---------------------------------------------------------------------------
# DB-backed category cache (replaces merchant_cache.json / uncertain_merchants.json)
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, str]:
    """Return {merchant: category} for all confirmed merchants (DB-backed)."""
    from db import get_all_merchant_categories
    return get_all_merchant_categories()

def save_user_category(merchant: str, category: str):
    """User-confirmed category — writes to DB and clears uncertain flag."""
    from db import confirm_merchant_category
    confirm_merchant_category(merchant, category)
    print(f"[gemini] Saved user category: '{merchant}' → '{category}'")

def get_pending_uncertain() -> dict:
    """Return uncertain merchants still awaiting user review (DB-backed)."""
    from db import get_uncertain_merchants
    return get_uncertain_merchants()

def categorize_transactions(transactions):
    """
    Categorizes all transactions using Gemini with confidence scoring.
    - High confidence (>=0.75): saved to DB as confirmed, applied immediately
    - Low confidence (<0.75):   saved to DB as uncertain, flagged for user review
      and temporarily assigned Gemini's best guess until user confirms
    Returns (transactions, uncertain_merchants) where uncertain_merchants is a
    dict of {merchant: {category, confidence, sample}} needing review.
    """
    from db import save_merchant_category, get_all_merchant_categories, get_uncertain_merchants

    cache     = get_all_merchant_categories()
    uncertain = get_uncertain_merchants()

    # Find merchants not yet in either set
    uncached = {}
    for t in transactions:
        merchant = t["merchant"]
        if merchant not in cache and merchant not in uncertain:
            uncached[merchant] = t.get("plaid_category") or t.get("category", "Other")

    new_uncertain = {}

    if uncached:
        print(f"[gemini] Categorizing {len(uncached)} new merchants...")
        mappings = _batch_categorize(uncached)

        for merchant, data in mappings.items():
            if data["confidence"] >= CONFIDENCE_THRESHOLD:
                save_merchant_category(merchant, data["category"],
                                       source="gemini", confidence=data["confidence"],
                                       is_uncertain=False)
                cache[merchant] = data["category"]
            else:
                new_uncertain[merchant] = {
                    "category":   data["category"],
                    "confidence": data["confidence"],
                }
                print(f"[gemini] Uncertain: '{merchant}' → '{data['category']}' ({data['confidence']})")

        if new_uncertain:
            # Attach a sample transaction for each uncertain merchant (for Discord review)
            for t in transactions:
                m = t["merchant"]
                if m in new_uncertain and "sample" not in new_uncertain[m]:
                    new_uncertain[m]["sample"] = {
                        "amount": t["amount"],
                        "date":   t["date"],
                        "bank":   t["bank"],
                    }
            for merchant, data in new_uncertain.items():
                save_merchant_category(
                    merchant, data["category"],
                    source="gemini", confidence=data["confidence"],
                    is_uncertain=True, sample=data.get("sample"),
                )
            uncertain.update(new_uncertain)

        confirmed_count = len(mappings) - len(new_uncertain)
        print(f"[gemini] Done. {confirmed_count} confirmed, {len(new_uncertain)} need review.")
    else:
        print("[gemini] All merchants already cached — skipping API call.")

    # Apply categories — confirmed cache first, then uncertain best-guess, then Other
    for t in transactions:
        merchant = t["merchant"]
        if merchant in cache:
            t["category"] = cache[merchant]
        elif merchant in uncertain:
            t["category"] = uncertain[merchant]["category"]
        else:
            t["category"] = "Other"

    return transactions, uncertain

def _batch_categorize(merchant_map):
    """
    Sends all uncached merchants to Gemini in one prompt.
    Returns a dict of merchant -> {category, confidence}.
    """
    categories_list = "\n".join(f"- {c}" for c in VALID_CATEGORIES)
    merchant_lines  = "\n".join(
        f'{i+1}. Merchant: "{name}" | Plaid PFC: "{cat}"'
        for i, (name, cat) in enumerate(merchant_map.items())
    )

    prompt = f"""You are a personal finance categorization assistant.

Categorize each merchant below into exactly one of these budget categories:
{categories_list}

Rules:
- Use only the categories listed above, exactly as written
- "Necessities" covers rent, car payments, and utilities (electric, gas, water, internet, phone bills)
- The "Plaid PFC" field is Plaid's detailed Personal Finance Category (e.g. FOOD_AND_DRINK_FAST_FOOD,
  TRANSPORTATION_GAS_STATION, LOAN_PAYMENTS_CAR_PAYMENT, TRANSFER_OUT_ACCOUNT_TRANSFER).
  Use it as a strong directional hint — it is usually accurate but must still map to one of your categories.
- If the Plaid PFC contains TRANSFER, PAYMENT, LOAN_PAYMENTS_CREDIT_CARD_PAYMENT, or INCOME, and it
  represents a payment to another account or credit card, assign "Excluded".
- Assign a confidence score from 0.0 to 1.0 (how certain you are)
  - 1.0 = completely obvious (e.g. "McDonald's" + FOOD_AND_DRINK_FAST_FOOD -> "Food and Drink")
  - 0.5 = plausible guess but ambiguous
  - 0.0 = no idea
- Reply ONLY with a valid JSON object, no markdown, no explanation

Merchants to categorize:
{merchant_lines}

Required response format:
{{
  "McDonald's": {{"category": "Food and Drink", "confidence": 1.0}},
  "Delta Airlines": {{"category": "Travel", "confidence": 0.95}}
}}"""

    response = _generate(prompt, temperature=0.1)

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        mappings = json.loads(raw)
    except json.JSONDecodeError:
        print("[gemini] Warning: could not parse response, defaulting all to uncertain")
        mappings = {}

    result = {}
    for merchant in merchant_map:
        data       = mappings.get(merchant, {})
        category   = data.get("category", "Other")
        confidence = float(data.get("confidence", 0.0))

        if category not in VALID_CATEGORIES:
            category   = "Other"
            confidence = 0.0

        result[merchant] = {"category": category, "confidence": confidence}

    return result

def generate_budget_summary(budget_summary):
    """
    Generates a natural language spending summary for the daily Discord digest.
    """
    lines = []
    for c in budget_summary["categories"]:
        status = "OVER BUDGET" if c["over_budget"] else f"{c['pct_used']}% used"
        lines.append(
            f"- {c['category']}: ${c['spent']} spent of ${c['limit']} limit ({status})"
        )

    prompt = f"""You are a friendly personal finance assistant delivering a daily budget update.

Here is today's spending summary:
Total spent: ${budget_summary['total_spent']} of ${budget_summary['total_limit']} budget
Total remaining: ${budget_summary['total_remaining']}
Pending (not counted): ${budget_summary['total_pending']}

Category breakdown:
{chr(10).join(lines)}

Write a concise, friendly 3-5 sentence daily digest for a Discord message.
- Highlight any over-budget categories with concern
- Mention categories doing well
- Give one actionable tip based on the data
- Keep it conversational, not robotic
- Do not use markdown headers, just plain text with occasional emphasis"""

    response = _generate(prompt, temperature=0.7)

    return response.text.strip()

def generate_subscription_summary(streams):
    """
    Generates a concise subscription overview for the daily digest.
    """
    expenses = [s for s in streams if s["stream_type"] == "expense" and s["is_active"]]
    if not expenses:
        return "No active recurring subscriptions detected."

    monthly_total = 0
    lines = []
    for s in sorted(expenses, key=lambda x: x["last_amount"], reverse=True):
        freq = s["frequency"]
        amt  = s["last_amount"]
        # Estimate monthly cost
        if "Annual" in freq:
            monthly = round(amt / 12, 2)
        elif "Weekly" in freq:
            monthly = round(amt * 4.33, 2)
        elif "Biweekly" in freq or "Semi" in freq:
            monthly = round(amt * 2, 2)
        else:
            monthly = amt
        monthly_total += monthly
        lines.append(f"- {s['merchant']}: ${amt} ({freq}) ~${monthly}/mo")

    prompt = f"""You are a personal finance assistant summarizing recurring subscriptions.

Active subscriptions:
{chr(10).join(lines)}
Estimated monthly total: ${round(monthly_total, 2)}

Write a 2-3 sentence friendly summary for a Discord message.
- Mention the total monthly cost
- Flag any surprisingly expensive subscriptions
- Keep it brief and conversational. No markdown headers."""

    response = _generate(prompt, temperature=0.7)
    return response.text.strip()

def answer_finance_question(question, monthly_summary, ytd_transactions, streams=None):
    """
    Answers a free-form finance question using the user's real budget data as context.
    """
    # Monthly category breakdown
    monthly_lines = []
    for c in monthly_summary["categories"]:
        status = "OVER BUDGET" if c["over_budget"] else f"{c['pct_used']}% used"
        monthly_lines.append(
            f"  - {c['category']}: ${c['spent']} spent / ${c['limit']} limit "
            f"({status}, ${c['remaining']} remaining)"
        )

    # YTD totals per category
    from annual import prorated_amount
    ytd_totals = {}
    for t in ytd_transactions:
        if not t["pending"] and t["category"] != "Excluded":
            cat = t["category"]
            ytd_totals[cat] = round(ytd_totals.get(cat, 0) + prorated_amount(t), 2)
    ytd_lines = [f"  - {cat}: ${amt}" for cat, amt in sorted(ytd_totals.items(), key=lambda x: -x[1])]

    # Recent 25 transactions
    recent = sorted(ytd_transactions, key=lambda x: x["date"], reverse=True)[:25]
    txn_lines = [
        f"  - {t['date']} | {t['merchant']} | ${t['amount']} | {t['category']} | {t['bank']}"
        for t in recent
    ]

    # Active subscriptions
    subs_lines = []
    if streams:
        expenses = [s for s in streams if s.get("stream_type") == "expense" and s.get("is_active")]
        for s in sorted(expenses, key=lambda x: x["last_amount"], reverse=True):
            subs_lines.append(f"  - {s['merchant']}: ${s['last_amount']} {s['frequency']} ({s['bank']})")

    context = f"""This Month ({monthly_summary.get('synced_at', 'recent')}):
  Total spent: ${monthly_summary['total_spent']} of ${monthly_summary['total_limit']} budget
  Total remaining: ${monthly_summary['total_remaining']}
  Pending (not counted): ${monthly_summary['total_pending']}

Monthly category breakdown:
{chr(10).join(monthly_lines)}

Year-to-date totals by category:
{chr(10).join(ytd_lines) if ytd_lines else '  No YTD data yet.'}

Recent transactions (last 25):
{chr(10).join(txn_lines) if txn_lines else '  No transactions found.'}
"""
    if subs_lines:
        context += f"\nActive subscriptions:\n{chr(10).join(subs_lines)}\n"

    prompt = f"""You are a personal finance assistant with access to the user's real spending data.
Answer their question using the actual data below. Be conversational, specific, and helpful.
Reference real numbers and merchants when relevant. Keep your response concise (under 400 words).
Do not use markdown headers — just plain text with light emphasis where needed.

--- Financial Data ---
{context}
--- End of Data ---

User question: {question}"""

    response = _generate(prompt, temperature=0.7)
    return response.text.strip()


def _format_history(history: list[tuple[str, str]]) -> str:
    """Format conversation history pairs as a readable block for the Gemini prompt."""
    lines = []
    for user_text, bot_reply in history:
        # Truncate very long bot replies to keep prompt size reasonable
        reply_preview = bot_reply[:500] + "..." if len(bot_reply) > 500 else bot_reply
        lines.append(f"User: {user_text}")
        lines.append(f"Bot: {reply_preview}")
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """
    Rough token estimate: ~4 characters per token (GPT/Gemini heuristic).
    Used only for logging — not a hard limit check.
    """
    return max(1, len(text) // 4)


# Free-tier soft warning threshold (tokens per single request).
# Gemini 2.5 Flash free tier: 1M context window, 1M TPM, 1,500 RPD.
# Our prompts are ~2k-5k tokens — well within limits.
# Warn if a single request exceeds this (indicates something unexpected).
_TOKEN_LOG_THRESHOLD = 20_000


def parse_nl_command(text: str, context: dict, history: list[tuple[str, str]] | None = None) -> dict:
    """
    Parse a natural-language message into one or more structured bot actions.

    context keys (all optional but improve accuracy):
      merchants           : list of known merchant names
      categories          : list of valid budget categories
      subscriptions       : list of current subscription merchant names
      annual_merchants    : list of merchants marked as annual
      recent_txns         : list of {date, merchant, amount, transaction_id} dicts
      banks               : list of linked bank names
      mentioned_merchants : list of merchant names explicitly @-mentioned by the user
                            (these are treated as authoritative — Gemini must use them exactly)

    history (optional):
      List of (user_text, bot_reply) tuples from the same channel, most recent last.
      Capped at 5 exchanges by the caller. Allows Gemini to understand follow-ups
      like "remove it", "make that annual", "undo that", etc.

    Returns:
      {
        "actions": [
          {"action": <str>, "params": <dict>, "response": <str>},
          ...
        ],
        "clarification": <str|None>   # set when intent is ambiguous
      }
    """
    merchants            = context.get("merchants", [])
    categories           = context.get("categories", [])
    subscriptions        = context.get("subscriptions", [])
    annual_merchants     = context.get("annual_merchants", [])
    banks                = context.get("banks", [])
    recent_txns          = context.get("recent_txns", [])
    mentioned_merchants  = context.get("mentioned_merchants", [])
    history              = history or []

    txn_lines = "\n".join(
        f"  - id={t['transaction_id']} | {t['date']} | {t['merchant']} | ${t['amount']}"
        for t in recent_txns[:20]
    ) or "  (none)"

    prompt = f"""You are an AI assistant for a personal budget tracker Discord bot.
The user has @-mentioned the bot with a natural-language request.
Your job is to translate their message into one or more structured bot actions.

=== AVAILABLE ACTIONS ===

add_subscription
  params: merchant (str), frequency (one of: Weekly|Bi-Weekly|Monthly|Quarterly|Semi-Annual|Annual),
          amount (float, OPTIONAL — omit for variable bills like utilities/electricity),
          category (str, optional), bank (str, optional),
          tag (str, optional — short personal label, e.g. "Water & Trash", "4K plan", "Work tool"),
          is_estimate (bool, optional — set true when the user gives a rough/approximate amount)
  NOTE: If the user doesn't mention a specific amount, or the bill is variable
        (electricity, water, gas, internet, insurance), omit amount entirely.
        The bot will look it up automatically from transaction history.
        If the user says "around", "roughly", "approximately", "I think it's", "about" before
        an amount — include the amount but set is_estimate: true. The bot will auto-correct
        it to the real amount once the actual charge appears in transactions.
  examples:
    "add Netflix monthly $15.99"                  → amount: 15.99
    "add Spotify, I think it's around $11/month"  → amount: 11, is_estimate: true
    "add my electricity bill monthly"             → omit amount (variable)
    "add US Retailers as a subscription"          → omit amount (variable utility)

remove_subscription
  params: merchant (str)
  example: "remove OpenAI from subscriptions"

find_transaction
  params: query (str) — keyword(s) to search for in merchant names
  use when the user wants to look up a specific merchant or check if a transaction exists.
  Returns matching transactions grouped by merchant with recurring-pattern analysis.
  examples:
    "find my Walmart Plus transactions"  → query: "walmart plus"
    "show me any Hulu charges"           → query: "hulu"
    "did I pay for iCloud this month?"   → query: "icloud"

find_and_add_subscription
  params: query (str), merchant (str, optional — clean name override),
          frequency (str, optional), category (str, optional), tag (str, optional)
  use when the user wants to find a merchant AND add it as a subscription in one step.
  Searches transactions, auto-detects the billing frequency and average amount,
  then saves it as a subscription. Use this when the intent clearly includes adding.
  examples:
    "find my Walmart Plus and add it as a subscription"
      → query: "walmart plus"
    "I can't find my iCloud subscription — can you find it and add it?"
      → query: "icloud", merchant: "iCloud"
    "find Netflix and add it, tag: Family plan"
      → query: "netflix", tag: "Family plan"

update_subscription
  params: merchant (str), transaction_id (str, optional), frequency (str, optional),
          category (str, optional),
          tag (str, optional — short personal label shown next to the subscription name)
  Two modes:
    • Tag / metadata update (no transaction ID needed): when the user just wants to set
      a tag, change the frequency, or recategorize an existing subscription by name.
      Only provide merchant + the fields to change.
    • Transaction-based update: when the user gives a transaction ID and wants that
      transaction's amount/date/bank applied to a subscription entry.
      Provide transaction_id (and optionally merchant as a name override).
  The tag is a short personal note (e.g. "Family plan", "Water & Trash", "Justin only").
  examples:
    "tag my Netflix subscription as Family plan"
      → merchant: "Netflix", tag: "Family plan"
    "label Spotify as Justin only"
      → merchant: "Spotify", tag: "Justin only"
    "update my City of Plano subscription using transaction abc123, tag: Water & Trash"
      → transaction_id: "abc123", merchant: "City of Plano", tag: "Water & Trash"
    "use transaction xyz to set my water bill subscription"
      → transaction_id: "xyz", merchant: "Water Bill"

recategorize
  params: merchant (str), category (str — must be from the valid categories list)
  example: "recategorize Starbucks as Food and Drink"

delete_transaction
  params: transaction_ids (list of str) — use the IDs from recent transactions below
  example: "delete the Starbucks charge on May 10" → match from recent transactions

annual_add
  params: merchant (str)
  use ONLY when the user wants to mark an EXISTING transaction merchant as annual
  WITHOUT adding it as a new subscription entry.
  Do NOT use this alongside add_subscription — when frequency=Annual is passed to
  add_subscription, monthly proration is handled automatically.
  example: "mark my Costco charge as annual" (already in transactions, not a new sub)

annual_remove
  params: merchant (str)
  example: "remove Costco from annual"

annual_list
  params: (none)
  example: "show annual merchants"

ask
  params: question (str) — a finance/budget question to answer with real data
  example: "how much did I spend on food this month?"

budget
  params: (none) — show current month budget status

subscriptions
  params: (none) — list all detected subscriptions

status
  params: (none) — show bank connection health

sync
  params: (none) — trigger a full transaction sync

unknown
  params: clarification (str) — when the intent is genuinely unclear, ask the user

=== CONTEXT ===
Known merchants    : {', '.join(merchants[:60]) or 'none'}
Valid categories   : {', '.join(categories)}
Current subs       : {', '.join(subscriptions) or 'none'}
Annual merchants   : {', '.join(annual_merchants) or 'none'}
Linked banks       : {', '.join(banks) or 'none'}
Explicitly @mentioned merchants: {', '.join(mentioned_merchants) if mentioned_merchants else 'none'}
Recent transactions:
{txn_lines}

=== CONVERSATION HISTORY (most recent last) ===
{_format_history(history) if history else "(no previous messages)"}

=== CURRENT USER MESSAGE ===
{text}

=== RULES ===
- Return ONLY valid JSON, no markdown, no explanation.
- Use "CONVERSATION HISTORY" to resolve pronouns and follow-ups ("it", "that", "the same one",
  "undo that", "make it annual", etc.) — refer to the most recently discussed merchant/action.
- IMPORTANT: If "Explicitly @mentioned merchants" is non-empty, use THOSE EXACT names for any merchant
  params in your actions — they are the authoritative merchant names the user specified. Do not
  substitute or fuzzy-match to a different name when a mention is present.
- For other merchant names not explicitly mentioned, match against "Known merchants" if possible (fuzzy match).
- For categories, use EXACT strings from "Valid categories".
- For delete_transaction, match the description to a transaction in "Recent transactions" and use its id.
- Multiple actions are allowed if the user asks for multiple things.
- If frequency is missing for add_subscription, set clarification and do NOT include the action.
- If amount is missing, OMIT it from params (do not set clarification) — the bot resolves it from transactions.
- Prefer specific actions over "ask" when the intent matches a command.
- NEVER pair annual_add with add_subscription for the same merchant — add_subscription with frequency=Annual handles proration automatically.
- The "response" field is a short, friendly human-readable description of what you're doing (shown to user).

=== RESPONSE FORMAT ===
{{
  "actions": [
    {{"action": "action_name", "params": {{}}, "response": "Doing X..."}}
  ],
  "clarification": null
}}"""

    # Token estimation — log so we can monitor free-tier usage.
    # Gemini 2.5 Flash free tier: 1M TPM, 1,500 RPD, 15 RPM.
    # Our requests are ~3k-6k tokens; history adds ~500-1,500 tokens — well within limits.
    estimated_tokens = _estimate_tokens(prompt)
    if estimated_tokens > _TOKEN_LOG_THRESHOLD:
        print(f"[gemini] Warning: large NL prompt (~{estimated_tokens:,} tokens). "
              f"Free tier limit: 1,000,000 TPM.")
    else:
        print(f"[gemini] NL prompt ~{estimated_tokens:,} tokens "
              f"(history={len(history)} exchanges)")

    response = _generate(prompt, temperature=0.1)
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"actions": [], "clarification": "Sorry, I couldn't parse that. Could you rephrase?"}

    # Ensure required keys exist
    result.setdefault("actions", [])
    result.setdefault("clarification", None)
    return result


def generate_evening_digest(today_txns: list, monthly_summary: dict) -> str:
    """
    Generates a short end-of-day narrative for the evening digest.
    Covers what was spent today and how that sits against the monthly budget.
    """
    posted  = [t for t in today_txns if not t["pending"] and t["category"] != "Excluded"]
    pending = [t for t in today_txns if t["pending"]]

    today_total = round(sum(t["amount"] for t in posted), 2)

    # Per-category breakdown for today
    by_cat: dict[str, float] = {}
    for t in posted:
        by_cat[t["category"]] = round(by_cat.get(t["category"], 0) + t["amount"], 2)

    cat_lines = "\n".join(
        f"  - {cat}: ${amt}" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])
    ) or "  (none)"

    txn_lines = "\n".join(
        f"  - {t['merchant']}: ${t['amount']} ({t['category']})"
        for t in sorted(posted, key=lambda x: -x["amount"])
    ) or "  (no posted transactions today)"

    prompt = f"""You are a friendly personal finance assistant delivering a concise end-of-day spending recap.

Today's spending:
Total spent today: ${today_total}
Pending (not settled): {len(pending)} transaction(s)

By category today:
{cat_lines}

Individual transactions today:
{txn_lines}

Month so far:
  Total spent this month: ${monthly_summary['total_spent']} of ${monthly_summary['total_limit']} budget
  Remaining: ${monthly_summary['total_remaining']}

Write a 2-4 sentence friendly end-of-day recap for Discord.
- Lead with today's total
- Highlight the biggest spend category today
- Mention how the month is tracking overall (on track vs. over)
- Keep it casual and conversational, no markdown headers, no bullet points"""

    response = _generate(prompt, temperature=0.7)
    return response.text.strip()


def generate_overage_message(overage):
    """
    Generates a short alert message for a single budget category overage.
    """
    prompt = f"""Write a short, friendly but firm 1-2 sentence Discord alert message.
The user has gone over budget in the {overage['category']} category.
They spent ${overage['spent']} against a ${overage['limit']} limit (${overage['over_by']} over, {overage['pct_used']}% of budget used).
Be direct and helpful. No markdown."""

    response = _generate(prompt, temperature=0.7)

    return response.text.strip()
