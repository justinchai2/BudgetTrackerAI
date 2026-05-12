import json
import os
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, BUDGET_LIMITS

client = genai.Client(api_key=GEMINI_API_KEY)

MERCHANT_CACHE_FILE  = "merchant_cache.json"
UNCERTAIN_CACHE_FILE = "uncertain_merchants.json"
VALID_CATEGORIES     = list(BUDGET_LIMITS.keys()) + ["Excluded"]
CONFIDENCE_THRESHOLD = 0.75

def _load_cache():
    if os.path.exists(MERCHANT_CACHE_FILE):
        with open(MERCHANT_CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def _save_cache(cache):
    with open(MERCHANT_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def _load_uncertain():
    if os.path.exists(UNCERTAIN_CACHE_FILE):
        with open(UNCERTAIN_CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def _save_uncertain(uncertain):
    with open(UNCERTAIN_CACHE_FILE, "w") as f:
        json.dump(uncertain, f, indent=2)

def save_user_category(merchant, category):
    """
    Saves a user-confirmed category for a merchant.
    Moves it from uncertain into the main cache permanently.
    """
    cache = _load_cache()
    cache[merchant] = category
    _save_cache(cache)

    uncertain = _load_uncertain()
    if merchant in uncertain:
        del uncertain[merchant]
        _save_uncertain(uncertain)

    print(f"[gemini] Saved user category: '{merchant}' -> '{category}'")

def get_pending_uncertain():
    """
    Returns merchants that still need user review.
    """
    return _load_uncertain()

def categorize_transactions(transactions):
    """
    Categorizes all transactions using Gemini with confidence scoring.
    - High confidence (>=0.75): saved to main cache, applied immediately
    - Low confidence (<0.75): saved to uncertain cache, flagged for user review
      and temporarily assigned Gemini's best guess until user confirms
    Returns (transactions, uncertain_merchants) where uncertain_merchants is a
    dict of {merchant: {category, confidence, sample_transaction}} needing review.
    """
    cache     = _load_cache()
    uncertain = _load_uncertain()

    # Find merchants not in either cache
    uncached = {}
    for t in transactions:
        merchant = t["merchant"]
        if merchant not in cache and merchant not in uncertain:
            uncached[merchant] = t.get("category", "Other")

    new_uncertain = {}

    if uncached:
        print(f"[gemini] Categorizing {len(uncached)} new merchants...")
        mappings = _batch_categorize(uncached)

        confirmed = {}
        for merchant, data in mappings.items():
            if data["confidence"] >= CONFIDENCE_THRESHOLD:
                confirmed[merchant] = data["category"]
            else:
                new_uncertain[merchant] = {
                    "category":   data["category"],
                    "confidence": data["confidence"],
                }
                print(f"[gemini] Uncertain: '{merchant}' -> '{data['category']}' ({data['confidence']})")

        cache.update(confirmed)
        _save_cache(cache)

        if new_uncertain:
            # Find a sample transaction for each uncertain merchant for Discord context
            for t in transactions:
                m = t["merchant"]
                if m in new_uncertain and "sample" not in new_uncertain[m]:
                    new_uncertain[m]["sample"] = {
                        "amount": t["amount"],
                        "date":   t["date"],
                        "bank":   t["bank"],
                    }
            uncertain.update(new_uncertain)
            _save_uncertain(uncertain)

        print(f"[gemini] Done. {len(confirmed)} confirmed, {len(new_uncertain)} need review.")
    else:
        print("[gemini] All merchants already cached — skipping API call.")

    # Apply categories — use confirmed cache, fall back to uncertain's best guess
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
        f'{i+1}. Merchant: "{name}" | Plaid category: "{cat}"'
        for i, (name, cat) in enumerate(merchant_map.items())
    )

    prompt = f"""You are a personal finance categorization assistant.

Categorize each merchant below into exactly one of these budget categories:
{categories_list}

Rules:
- Use only the categories listed above, exactly as written
- Base your decision on the merchant name and the Plaid category hint
- Assign a confidence score from 0.0 to 1.0 (how certain you are)
  - 1.0 = completely obvious (e.g. "McDonald's" -> "Food and Drink")
  - 0.5 = plausible guess but ambiguous (e.g. "INTRST PYMNT" -> "Other")
  - 0.0 = no idea
- Reply ONLY with a valid JSON object, no markdown, no explanation

Merchants to categorize:
{merchant_lines}

Required response format:
{{
  "McDonald's": {{"category": "Food and Drink", "confidence": 1.0}},
  "Delta Airlines": {{"category": "Travel", "confidence": 0.95}}
}}"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.1),
    )

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

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.7),
    )

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

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.7),
    )
    return response.text.strip()

def generate_overage_message(overage):
    """
    Generates a short alert message for a single budget category overage.
    """
    prompt = f"""Write a short, friendly but firm 1-2 sentence Discord alert message.
The user has gone over budget in the {overage['category']} category.
They spent ${overage['spent']} against a ${overage['limit']} limit (${overage['over_by']} over, {overage['pct_used']}% of budget used).
Be direct and helpful. No markdown."""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.7),
    )

    return response.text.strip()
