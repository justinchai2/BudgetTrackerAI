"""
Manages the list of merchants whose transactions should be prorated
across 12 months (e.g. annual subscriptions, insurance, memberships).
"""
import json
import os

ANNUAL_FILE = "annual_merchants.json"

def _load():
    if os.path.exists(ANNUAL_FILE):
        with open(ANNUAL_FILE, "r") as f:
            return set(json.load(f))
    return set()

def _save(merchants):
    with open(ANNUAL_FILE, "w") as f:
        json.dump(sorted(merchants), f, indent=2)

def add_merchant(merchant):
    merchants = _load()
    merchants.add(merchant)
    _save(merchants)
    print(f"[annual] Added: '{merchant}'")

def remove_merchant(merchant):
    merchants = _load()
    merchants.discard(merchant)
    _save(merchants)
    print(f"[annual] Removed: '{merchant}'")

def get_merchants():
    return _load()

def is_annual(merchant):
    return merchant in _load()

def prorated_amount(transaction):
    """Returns the monthly-prorated amount if the merchant is annual, else the original."""
    if is_annual(transaction["merchant"]):
        return round(transaction["amount"] / 12, 2)
    return transaction["amount"]
