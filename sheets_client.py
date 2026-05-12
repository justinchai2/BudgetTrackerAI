import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BANK_TABS    = ["Chase", "Citi", "Capital One"]
SUMMARY_TAB  = "Summary"
SUBS_TAB     = "Subscriptions"

TXN_HEADERS = [
    "Date", "Merchant", "Category", "Amount", "Status", "Account ID", "Transaction ID",
    "Custom Category"
]

CUSTOM_CAT_COL = 8   # column H (1-indexed)

SUMMARY_HEADERS = [
    "Category", "Total Spent", "Budget Limit", "Remaining", "% Used"
]

SUBS_HEADERS = [
    "Merchant", "Category", "Frequency", "Last Amount", "Average Amount",
    "Last Date", "Status", "Bank"
]

def _get_client():
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def _get_or_create_tab(sheet, title):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=1000, cols=20)

def _style_header_row(sheet, worksheet):
    tab_id = worksheet.id
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
                    "textFormat": {
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        "bold": True,
                    }
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }
    }]
    sheet.batch_update({"requests": requests})

def _format_amount(amount):
    return round(float(amount), 2)

def read_custom_category_overrides():
    """
    Reads all Custom Category overrides from existing bank tabs.
    Returns a dict of {merchant: custom_category} for any non-empty overrides found.
    Called before sync so overrides can update the merchant cache first.
    """
    from gemini_client import save_user_category, VALID_CATEGORIES
    gc    = _get_client()
    sheet = gc.open_by_key(GOOGLE_SHEET_ID)

    overrides = {}
    for bank in BANK_TABS:
        try:
            ws   = sheet.worksheet(bank)
            rows = ws.get_all_values()
            if len(rows) < 2:
                continue
            for row in rows[1:]:
                if len(row) >= CUSTOM_CAT_COL:
                    merchant   = row[1].strip()   # column B
                    custom_cat = row[CUSTOM_CAT_COL - 1].strip()  # column H
                    if custom_cat and custom_cat in VALID_CATEGORIES and merchant:
                        overrides[merchant] = custom_cat
        except gspread.WorksheetNotFound:
            continue

    if overrides:
        print(f"[sheets] Found {len(overrides)} custom category override(s) in sheet")
        for merchant, cat in overrides.items():
            save_user_category(merchant, cat)
            print(f"[sheets] Override applied: '{merchant}' -> '{cat}'")

    return overrides

def sync_transactions(transactions, budget_limits):
    gc     = _get_client()
    sheet  = gc.open_by_key(GOOGLE_SHEET_ID)

    # Check for any custom category overrides filled in by user before writing
    read_custom_category_overrides()

    # Group transactions by bank
    by_bank = {bank: [] for bank in BANK_TABS}
    for txn in transactions:
        bank = txn.get("bank")
        if bank in by_bank:
            by_bank[bank].append(txn)

    # Write per-bank tabs
    for bank, txns in by_bank.items():
        ws = _get_or_create_tab(sheet, bank)
        ws.clear()

        rows = [TXN_HEADERS]
        for t in sorted(txns, key=lambda x: x["date"], reverse=True):
            status = "Pending" if t["pending"] else "Posted"
            rows.append([
                t["date"],
                t["merchant"],
                t["category"],
                _format_amount(t["amount"]),
                status,
                t["account_id"],
                t["transaction_id"],
                "",   # Custom Category — left blank, filled by user when needed
            ])

        ws.update(rows, "A1")
        _style_header_row(sheet, ws)
        print(f"[sheets] {bank}: {len(txns)} transactions written")

    # Write summary tab (posted only, grouped by category)
    _write_summary(sheet, transactions, budget_limits)

    # Update last synced timestamp on summary tab
    _write_last_synced(sheet)

    print("[sheets] Sync complete")

def _write_summary(sheet, transactions, budget_limits):
    ws = _get_or_create_tab(sheet, SUMMARY_TAB)
    ws.clear()

    # Only count posted transactions toward budget
    posted = [t for t in transactions if not t["pending"]]

    # Sum by category
    category_totals = {}
    for t in posted:
        cat = t["category"]
        category_totals[cat] = category_totals.get(cat, 0) + t["amount"]

    rows = [SUMMARY_HEADERS]
    all_categories = set(list(budget_limits.keys()) + list(category_totals.keys()))

    for cat in sorted(all_categories):
        spent  = round(category_totals.get(cat, 0), 2)
        limit  = budget_limits.get(cat, 0)
        remaining = round(limit - spent, 2) if limit else "N/A"
        pct_used  = f"{round((spent / limit) * 100, 1)}%" if limit else "N/A"
        rows.append([cat, spent, limit if limit else "No limit", remaining, pct_used])

    # Totals row
    total_spent = round(sum(category_totals.values()), 2)
    total_limit = sum(budget_limits.values())
    rows.append(["TOTAL", total_spent, total_limit,
                 round(total_limit - total_spent, 2),
                 f"{round((total_spent / total_limit) * 100, 1)}%" if total_limit else "N/A"])

    ws.update(rows, "A1")
    _style_header_row(sheet, ws)
    print(f"[sheets] Summary written ({len(rows) - 1} categories)")

def sync_subscriptions(recurring_streams):
    gc    = _get_client()
    sheet = gc.open_by_key(GOOGLE_SHEET_ID)
    ws    = _get_or_create_tab(sheet, SUBS_TAB)
    ws.clear()

    rows = [SUBS_HEADERS]
    for stream in recurring_streams:
        rows.append([
            stream.get("merchant_name", "Unknown"),
            stream.get("category", "Other"),
            stream.get("frequency", "Unknown"),
            _format_amount(stream.get("last_amount", 0)),
            _format_amount(stream.get("average_amount", 0)),
            stream.get("last_date", ""),
            stream.get("status", "Unknown"),
            stream.get("bank", ""),
        ])

    ws.update(rows, "A1")
    _style_header_row(sheet, ws)
    print(f"[sheets] Subscriptions: {len(recurring_streams)} streams written")

def _write_last_synced(sheet):
    ws = _get_or_create_tab(sheet, SUMMARY_TAB)
    last_row = len(ws.get_all_values()) + 2
    ws.update([[f"Last synced: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"]],
              f"A{last_row}")
