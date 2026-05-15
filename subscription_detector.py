"""
Local subscription detection using transaction history.

Algorithm:
  1. Group posted, non-Excluded transactions by merchant
  2. Find merchants with >= 2 occurrences and consistent amounts
  3. Check if the spacing between charges clusters around a known billing period
     (weekly / bi-weekly / monthly / quarterly / semi-annual / annual)
  4. Send all statistical candidates to Gemini to filter out false positives
     (e.g. a grocery store visited monthly isn't a subscription)
  5. Return confirmed subscriptions in the same dict schema as plaid_client
     recurring streams so existing code (sync_subscriptions, /subscriptions) works unchanged.
"""

import json
import statistics
from datetime import datetime
from collections import defaultdict


# ---------------------------------------------------------------------------
# Known billing intervals (days) and their tolerance (± fraction)
# ---------------------------------------------------------------------------
FREQUENCIES = [
    ("Weekly",       7,   0.40),
    ("Bi-Weekly",   14,   0.35),
    ("Monthly",     30,   0.30),
    ("Quarterly",   91,   0.25),
    ("Semi-Annual", 182,  0.20),
    ("Annual",      365,  0.20),
]

# Minimum number of matching charges to flag as a candidate
MIN_OCCURRENCES = 2

# Amount coefficient-of-variation thresholds (stdev / mean).
# ≤ FIXED_AMOUNT_CV  → fixed-price subscription (Netflix, Spotify, etc.)
# ≤ VARIABLE_AMOUNT_CV → variable-price recurring bill (electricity, water, insurance, etc.)
#   These are flagged as variable_amount=True and Gemini decides if they're real bills.
# > VARIABLE_AMOUNT_CV → too erratic, not a subscription
FIXED_AMOUNT_CV    = 0.25
VARIABLE_AMOUNT_CV = 0.60


def _match_frequency(avg_days: float):
    """Return the frequency label whose target interval best fits avg_days."""
    for label, target, tol in FREQUENCIES:
        lo = target * (1 - tol)
        hi = target * (1 + tol)
        if lo <= avg_days <= hi:
            return label
    return None


def _detect_candidates(transactions: list) -> list:
    """
    Pure statistical pass: returns merchants whose charges look periodic
    and amount-consistent, without any AI judgement yet.
    """
    from subscription_store import get_blacklist
    blacklist = set(get_blacklist())

    # Group by merchant — skip pending, transfers/excluded, and blacklisted
    groups: dict[str, list] = defaultdict(list)
    for t in transactions:
        if (not t["pending"]
                and t.get("category") != "Excluded"
                and float(t["amount"]) > 0
                and t["merchant"] not in blacklist):
            groups[t["merchant"]].append(t)

    candidates = []

    for merchant, txns in groups.items():
        if len(txns) < MIN_OCCURRENCES:
            continue

        # Deduplicate by date — keep only the first transaction per calendar day
        # (avoids 0-day intervals from pending/posted duplicates skewing frequency)
        seen_dates: set = set()
        deduped = []
        for t in sorted(txns, key=lambda x: x["date"]):
            if t["date"] not in seen_dates:
                seen_dates.add(t["date"])
                deduped.append(t)
        txns = deduped

        if len(txns) < MIN_OCCURRENCES:
            continue

        txns_sorted = sorted(txns, key=lambda x: x["date"])
        dates       = [datetime.strptime(t["date"], "%Y-%m-%d") for t in txns_sorted]
        amounts     = [float(t["amount"]) for t in txns_sorted]

        # Compute day-gaps between consecutive charges
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        if not intervals:
            continue

        avg_interval = statistics.mean(intervals)
        frequency    = _match_frequency(avg_interval)
        if frequency is None:
            continue

        # Amount consistency check
        mean_amt = statistics.mean(amounts)
        if mean_amt <= 0:
            continue
        stdev_amt = statistics.stdev(amounts) if len(amounts) > 1 else 0.0
        cv        = stdev_amt / mean_amt

        # Too erratic even for a variable bill — skip
        if cv > VARIABLE_AMOUNT_CV:
            continue

        # Flag as variable if amount varies more than a fixed subscription
        is_variable = cv > FIXED_AMOUNT_CV

        # Confidence: more occurrences + lower variance = higher confidence
        base_conf = min(0.60 + (len(txns) - 2) * 0.10, 0.90)
        conf      = round(base_conf - cv * 0.3, 2)

        candidates.append({
            "merchant":         merchant,
            "frequency":        frequency,
            "average_amount":   round(mean_amt, 2),
            "last_amount":      round(amounts[-1], 2),
            "last_date":        txns_sorted[-1]["date"],
            "first_date":       txns_sorted[0]["date"],
            "occurrence_count": len(txns),
            "avg_interval_days": round(avg_interval, 1),
            "amount_cv":        round(cv, 3),
            "confidence":       conf,
            "category":         txns_sorted[-1].get("category", "Other"),
            "bank":             txns_sorted[-1]["bank"],
            "stream_type":      "expense",
            "is_active":        True,
            "status":           "Active",
            "sub_type":         None,       # filled in by Gemini pass
            "variable_amount":  is_variable,
        })

    # Highest-amount first so Discord output is naturally sorted
    return sorted(candidates, key=lambda x: x["average_amount"], reverse=True)


def _gemini_filter(candidates: list) -> list:
    """
    Ask Gemini to confirm which statistical candidates are genuine
    subscriptions / recurring bills and to label their type.

    Returns the filtered + enriched list.
    """
    if not candidates:
        return []

    from gemini_client import _generate  # local import to avoid circular

    lines = []
    for i, c in enumerate(candidates, 1):
        variable_tag = " | VARIABLE_AMOUNT" if c.get("variable_amount") else ""
        lines.append(
            f'{i}. merchant="{c["merchant"]}" | freq={c["frequency"]} | '
            f'avg=${c["average_amount"]} | occurrences={c["occurrence_count"]} | '
            f'category={c["category"]}{variable_tag}'
        )

    prompt = f"""You are a personal finance analyst reviewing a list of merchants that were
detected as potentially recurring charges based on transaction interval analysis.

Your job is to classify each one and decide whether to keep or discard it.

For each entry decide:
- "keep"  → it is a genuine subscription, recurring bill, or automatic charge
  (streaming service, SaaS, gym, insurance, utilities, loan payment, rent, etc.)
- "skip"  → it appears regularly but is NOT a subscription/bill
  (e.g. a grocery store, gas station, restaurant, or retailer the user shops at frequently)

Entries marked VARIABLE_AMOUNT have charges that fluctuate month-to-month — this is
normal for utility bills, electricity, water, insurance, and similar recurring expenses.
Be MORE lenient with these: if the merchant looks like a utility, service provider, or
recurring bill, mark it "keep" even if the amount varies.

Also assign a short sub_type label for kept items:
  streaming | software | gym | insurance | utilities | loan | rent | phone |
  cloud | gaming | news | food_delivery | other_subscription

Candidates:
{chr(10).join(lines)}

Reply ONLY with a valid JSON object mapping the exact merchant name to an object.
No markdown, no explanation.

Example format:
{{
  "Netflix": {{"verdict": "keep", "sub_type": "streaming"}},
  "Oncor Electric": {{"verdict": "keep", "sub_type": "utilities"}},
  "Whole Foods Market": {{"verdict": "skip", "sub_type": null}}
}}"""

    try:
        response = _generate(prompt, temperature=0.1)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        verdicts = json.loads(raw)
    except Exception as e:
        print(f"[subscriptions] Gemini filter failed ({e}), returning all statistical candidates")
        return candidates  # fall back to unfiltered list

    confirmed = []
    for c in candidates:
        v = verdicts.get(c["merchant"], {})
        if v.get("verdict") == "keep":
            c["sub_type"] = v.get("sub_type") or "other_subscription"
            confirmed.append(c)
        else:
            print(f"[subscriptions] Filtered out: {c['merchant']} ({c['frequency']})")

    return confirmed


def detect_subscriptions(transactions: list) -> list:
    """
    Main entry point.  Returns a list of subscription dicts compatible with
    the existing sync_subscriptions() and /subscriptions command.

    Order: auto-detected (statistical + Gemini-confirmed) + manual entries.
    Manual entries are always included regardless of transaction history.
    If a merchant appears in both, the auto-detected entry takes precedence.
    """
    from subscription_store import get_manual_subscriptions

    candidates = _detect_candidates(transactions)
    print(f"[subscriptions] Statistical candidates: {len(candidates)}")

    confirmed = _gemini_filter(candidates)
    print(f"[subscriptions] Gemini-confirmed subscriptions: {len(confirmed)}")

    # Merge manual entries — skip any merchant already auto-detected
    auto_merchants = {s["merchant"].lower() for s in confirmed}
    manual = [s for s in get_manual_subscriptions()
              if s["merchant"].lower() not in auto_merchants]
    if manual:
        print(f"[subscriptions] Manual entries added: {len(manual)}")

    return confirmed + manual
