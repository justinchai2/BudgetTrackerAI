import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BANK_TABS       = ["Chase", "Citi", "Capital One"]  # old tabs to remove if present
TXN_TAB         = "Transactions"
SUMMARY_TAB     = "Summary"
SUBS_TAB        = "Subscriptions"

TXN_HEADERS = [
    "Date", "Bank", "Merchant", "Category", "Amount",
    "Status", "Account ID", "Transaction ID", "Custom Category"
]

# Column indices (1-indexed)
MERCHANT_COL   = 3   # column C
CUSTOM_CAT_COL = 9   # column I

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
        return sheet.add_worksheet(title=title, rows=2000, cols=20)

def _delete_old_bank_tabs(sheet):
    for bank in BANK_TABS:
        try:
            ws = sheet.worksheet(bank)
            sheet.del_worksheet(ws)
            print(f"[sheets] Removed old tab: {bank}")
        except gspread.WorksheetNotFound:
            pass

def _style_header_row(sheet, worksheet):
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": worksheet.id,
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
    Reads Custom Category overrides from the combined Transactions tab.
    Any non-empty value in the Custom Category column triggers a permanent
    cache update and is cleared on next sync.
    """
    from gemini_client import save_user_category, VALID_CATEGORIES
    gc    = _get_client()
    sheet = gc.open_by_key(GOOGLE_SHEET_ID)

    overrides = {}
    try:
        ws   = sheet.worksheet(TXN_TAB)
        rows = ws.get_all_values()
        if len(rows) < 2:
            return overrides
        for row in rows[1:]:
            if len(row) >= CUSTOM_CAT_COL:
                merchant   = row[MERCHANT_COL - 1].strip()   # column C
                custom_cat = row[CUSTOM_CAT_COL - 1].strip() # column I
                if custom_cat and custom_cat in VALID_CATEGORIES and merchant:
                    overrides[merchant] = custom_cat
    except gspread.WorksheetNotFound:
        pass

    if overrides:
        print(f"[sheets] Found {len(overrides)} custom category override(s)")
        for merchant, cat in overrides.items():
            save_user_category(merchant, cat)
            print(f"[sheets] Override applied: '{merchant}' -> '{cat}'")

    return overrides

def sync_transactions(transactions, budget_limits):
    gc    = _get_client()
    sheet = gc.open_by_key(GOOGLE_SHEET_ID)

    # Pick up any custom category overrides before writing
    read_custom_category_overrides()

    # Remove old per-bank tabs if they still exist
    _delete_old_bank_tabs(sheet)

    # Write all transactions into one combined tab
    ws   = _get_or_create_tab(sheet, TXN_TAB)
    ws.clear()

    rows = [TXN_HEADERS]
    for t in sorted(transactions, key=lambda x: x["date"], reverse=True):
        status = "Pending" if t["pending"] else "Posted"
        rows.append([
            t["date"],
            t["bank"],
            t["merchant"],
            t["category"],
            _format_amount(t["amount"]),
            status,
            t["account_id"],
            t["transaction_id"],
            "",   # Custom Category — fill in to override, picked up on next sync
        ])

    ws.update(rows, "A1")
    _style_header_row(sheet, ws)
    print(f"[sheets] Transactions: {len(transactions)} rows written ({TXN_TAB} tab)")

    # Summary and timestamp
    _write_summary(sheet, transactions, budget_limits)
    _write_last_synced(sheet)
    print("[sheets] Sync complete")

def _write_summary(sheet, transactions, budget_limits):
    ws = _get_or_create_tab(sheet, SUMMARY_TAB)
    ws.clear()

    posted = [t for t in transactions if not t["pending"]]

    category_totals = {}
    for t in posted:
        cat = t["category"]
        category_totals[cat] = category_totals.get(cat, 0) + t["amount"]

    rows = [SUMMARY_HEADERS]
    all_categories = set(list(budget_limits.keys()) + list(category_totals.keys()))

    for cat in sorted(all_categories):
        spent     = round(category_totals.get(cat, 0), 2)
        limit     = budget_limits.get(cat, 0)
        remaining = round(limit - spent, 2) if limit else "N/A"
        pct_used  = f"{round((spent / limit) * 100, 1)}%" if limit else "N/A"
        rows.append([cat, spent, limit if limit else "No limit", remaining, pct_used])

    total_spent = round(sum(category_totals.values()), 2)
    total_limit = sum(budget_limits.values())
    rows.append([
        "TOTAL", total_spent, total_limit,
        round(total_limit - total_spent, 2),
        f"{round((total_spent / total_limit) * 100, 1)}%" if total_limit else "N/A"
    ])

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
            stream.get("merchant", "Unknown"),
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
    ws       = _get_or_create_tab(sheet, SUMMARY_TAB)
    last_row = len(ws.get_all_values()) + 2
    ws.update(
        [[f"Last synced: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"]],
        f"A{last_row}"
    )
