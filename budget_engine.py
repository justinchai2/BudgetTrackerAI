import json
import os
from datetime import datetime
from config import (
    BUDGET_LIMITS,
    COUNT_PENDING,
    LARGE_TRANSACTION_GLOBAL_THRESHOLD,
    LARGE_TRANSACTION_BY_CATEGORY,
)

# Tracks which transaction IDs have already triggered alerts — persisted to disk
ALERTED_IDS_FILE = "alerted_ids.json"

def _load_alerted_ids():
    if os.path.exists(ALERTED_IDS_FILE):
        with open(ALERTED_IDS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def _save_alerted_ids(ids):
    with open(ALERTED_IDS_FILE, "w") as f:
        json.dump(list(ids), f)

def _get_posted(transactions):
    if COUNT_PENDING:
        return [t for t in transactions if t["category"] != "Excluded"]
    return [t for t in transactions if not t["pending"] and t["category"] != "Excluded"]

def _category_totals(transactions):
    totals = {}
    for t in transactions:
        cat = t["category"]
        totals[cat] = totals.get(cat, 0) + t["amount"]
    return {cat: round(total, 2) for cat, total in totals.items()}

def check_budget_overages(transactions):
    """
    Returns a list of overage dicts for categories that have exceeded their limit.
    Only counts posted transactions.
    """
    posted = _get_posted(transactions)
    totals = _category_totals(posted)

    overages = []
    for cat, limit in BUDGET_LIMITS.items():
        spent = totals.get(cat, 0)
        if spent > limit:
            overages.append({
                "category": cat,
                "spent":    round(spent, 2),
                "limit":    limit,
                "over_by":  round(spent - limit, 2),
                "pct_used": round((spent / limit) * 100, 1),
            })

    return sorted(overages, key=lambda x: x["over_by"], reverse=True)

def check_large_transactions(transactions):
    """
    Returns new large transactions that haven't been alerted yet.
    Uses per-category threshold if set, otherwise falls back to global threshold.
    Only checks posted transactions. Pending transactions are always excluded.
    """
    alerted_ids = _load_alerted_ids()
    new_alerts   = []

    for t in transactions:
        if t["pending"]:
            continue
        if t["category"] == "Excluded":
            continue
        if t["transaction_id"] in alerted_ids:
            continue

        cat       = t["category"]
        threshold = LARGE_TRANSACTION_BY_CATEGORY.get(cat, LARGE_TRANSACTION_GLOBAL_THRESHOLD)

        if t["amount"] >= threshold:
            new_alerts.append({
                "transaction_id": t["transaction_id"],
                "bank":           t["bank"],
                "merchant":       t["merchant"],
                "amount":         round(t["amount"], 2),
                "category":       cat,
                "date":           t["date"],
                "threshold":      threshold,
            })
            alerted_ids.add(t["transaction_id"])

    if new_alerts:
        _save_alerted_ids(alerted_ids)

    return new_alerts

def get_budget_summary(transactions):
    """
    Returns a full summary dict for the daily digest.
    Includes posted totals, pending totals (for display only), and per-category breakdown.
    """
    posted  = _get_posted(transactions)
    pending = [t for t in transactions if t["pending"]]

    posted_totals  = _category_totals(posted)
    pending_totals = _category_totals(pending)

    categories = []
    for cat, limit in BUDGET_LIMITS.items():
        spent   = posted_totals.get(cat, 0)
        pending_amt = pending_totals.get(cat, 0)
        remaining   = round(limit - spent, 2)
        pct_used    = round((spent / limit) * 100, 1) if limit else 0
        categories.append({
            "category":    cat,
            "spent":       spent,
            "pending":     round(pending_amt, 2),
            "limit":       limit,
            "remaining":   remaining,
            "pct_used":    pct_used,
            "over_budget": spent > limit,
        })

    total_spent   = round(sum(v for k, v in posted_totals.items() if k != "Excluded"), 2)
    total_limit   = sum(BUDGET_LIMITS.values())
    total_pending = round(sum(pending_totals.values()), 2)

    return {
        "categories":    sorted(categories, key=lambda x: x["pct_used"], reverse=True),
        "total_spent":   total_spent,
        "total_limit":   total_limit,
        "total_pending": total_pending,
        "total_remaining": round(total_limit - total_spent, 2),
        "synced_at":     datetime.now().strftime("%Y-%m-%d %I:%M %p"),
        "transaction_count": {
            "posted":  len(posted),
            "pending": len(pending),
        }
    }
