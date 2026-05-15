"""
Persistent subscription overrides:
  - Blacklist  : merchants to always exclude from detection
  - Manual list: subscriptions the user added by hand
"""
import json
import os
from datetime import date

BLACKLIST_FILE = "subscription_blacklist.json"
MANUAL_FILE    = "subscription_manual.json"


# ── Blacklist ─────────────────────────────────────────────────────────────────

def _load_bl() -> list:
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE) as f:
            return json.load(f)
    return []

def _save_bl(bl: list):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(sorted(set(bl)), f, indent=2)

def get_blacklist() -> list:
    return _load_bl()

def add_to_blacklist(merchant: str):
    bl = _load_bl()
    if merchant not in bl:
        bl.append(merchant)
        _save_bl(bl)
        print(f"[subscriptions] Blacklisted: {merchant}")

def remove_from_blacklist(merchant: str):
    bl = _load_bl()
    if merchant in bl:
        bl.remove(merchant)
        _save_bl(bl)
        print(f"[subscriptions] Un-blacklisted: {merchant}")

def is_blacklisted(merchant: str) -> bool:
    return merchant in _load_bl()


# ── Manual subscriptions ──────────────────────────────────────────────────────

def _load_manual() -> list:
    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE) as f:
            return json.load(f)
    return []

def _save_manual(entries: list):
    with open(MANUAL_FILE, "w") as f:
        json.dump(entries, f, indent=2)

def get_manual_subscriptions() -> list:
    """Return manual entries formatted as subscription dicts."""
    entries = _load_manual()
    results = []
    for e in entries:
        amt = float(e.get("amount") or 0)
        entry = {
            "merchant":         e["merchant"],
            "frequency":        e["frequency"],
            "average_amount":   amt,
            "last_amount":      amt,
            "last_date":        e.get("added_date", str(date.today())),
            "first_date":       e.get("added_date", str(date.today())),
            "occurrence_count": "manual",
            "avg_interval_days": None,
            "amount_cv":        0,
            "confidence":       1.0,
            "category":         e.get("category", "Other"),
            "bank":             e.get("bank", "Manual"),
            "stream_type":      "expense",
            "is_active":        True,
            "status":           "Active",
            "sub_type":         e.get("sub_type", "other_subscription"),
            "variable_amount":  e.get("variable_amount", False),
            "tag":              e.get("tag", ""),
            "is_estimate":      e.get("is_estimate", False),
        }
        results.append(entry)
    return results

def add_manual_subscription(merchant: str, frequency: str, amount: float | None = None,
                             category: str = "Other", sub_type: str = "other_subscription",
                             bank: str = "Manual", tag: str = "",
                             is_estimate: bool = False) -> bool:
    """
    Add or update a manual subscription entry. Returns True if new, False if updated.

    If `amount` is None or 0, the average is looked up from recent transactions
    automatically and the subscription is flagged as variable_amount=True.
    """
    # Resolve amount from transaction history if not provided
    variable_amount = False
    if not amount or amount <= 0:
        from db import get_merchant_avg_amount
        avg = get_merchant_avg_amount(merchant)
        if avg:
            amount = avg
        else:
            amount = 0.0
        variable_amount = True

    entries = _load_manual()
    for e in entries:
        if e["merchant"].lower() == merchant.lower():
            update = {
                "frequency": frequency, "amount": amount,
                "category": category, "sub_type": sub_type, "bank": bank,
                "variable_amount": variable_amount,
                "is_estimate": is_estimate,
            }
            if tag:
                update["tag"] = tag
            e.update(update)
            _save_manual(entries)
            print(f"[subscriptions] Updated manual: {merchant} (variable={variable_amount})")
            return False   # updated
    entries.append({
        "merchant":        merchant,
        "frequency":       frequency,
        "amount":          amount,
        "category":        category,
        "sub_type":        sub_type,
        "bank":            bank,
        "added_date":      str(date.today()),
        "variable_amount": variable_amount,
        "tag":             tag,
        "is_estimate":     is_estimate,
    })
    _save_manual(entries)
    print(f"[subscriptions] Added manual: {merchant} (variable={variable_amount})")
    return True   # new

def remove_manual_subscription(merchant: str) -> bool:
    """Remove a manual subscription. Returns True if it existed."""
    entries = _load_manual()
    new = [e for e in entries if e["merchant"].lower() != merchant.lower()]
    if len(new) < len(entries):
        _save_manual(new)
        print(f"[subscriptions] Removed manual: {merchant}")
        return True
    return False

def list_manual_subscriptions() -> list:
    return _load_manual()
