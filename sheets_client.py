import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import hashlib
import json
import os
from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEET_ID

# ── Sync-state cache ──────────────────────────────────────────────────────────
# Stores MD5 fingerprints of the last data written to each Sheets tab.
# If the fingerprint matches on the next sync, we skip the expensive rewrite.
# Persisted to disk so the cache survives bot restarts.

_SYNC_STATE_FILE = "sheets_sync_state.json"
_sync_state: dict = {}

def _load_sync_state() -> dict:
    global _sync_state
    if not _sync_state:
        try:
            with open(_SYNC_STATE_FILE) as f:
                _sync_state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _sync_state = {}
    return _sync_state

def _save_sync_state() -> None:
    with open(_SYNC_STATE_FILE, "w") as f:
        json.dump(_sync_state, f)

def _hash_transactions(transactions: list) -> str:
    """Fingerprint a transaction list by (id, category, amount, pending)."""
    key = sorted(
        (t["transaction_id"], t["category"], float(t["amount"]), bool(t["pending"]))
        for t in transactions
    )
    return hashlib.md5(json.dumps(key).encode()).hexdigest()

def _hash_subscriptions(streams: list) -> str:
    """Fingerprint a subscription list by all user-visible fields."""
    key = sorted(
        (
            s.get("merchant", ""),
            s.get("frequency", ""),
            float(s.get("last_amount") or 0),
            s.get("next_charge_date", ""),
            s.get("category", ""),
            s.get("tag", ""),
        )
        for s in streams
    )
    return hashlib.md5(json.dumps(key).encode()).hexdigest()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BANK_TABS    = ["Chase", "Citi", "Capital One"]  # old tabs to remove if present
TXN_TAB      = "Transactions"
MONTHLY_TAB  = "Monthly"
YTD_TAB      = "Year to Date"
SUBS_TAB     = "Subscriptions"

TXN_HEADERS = [
    "Date", "Bank", "Merchant", "Category", "Amount",
    "Status", "Account (Last 4)", "Payment Channel", "Website", "Location", "Plaid Category", "Transaction ID"
]

# Column indices (1-indexed)
MERCHANT_COL = 3   # column C

SUMMARY_HEADERS = [
    "Category", "Total Spent", "Budget Limit", "Remaining", "% Used"
]

SUBS_HEADERS = [
    "Merchant", "Tag", "Category", "Type", "Billing Cycle", "Frequency",
    "Charge Amount", "Est. Monthly", "Annual Total",
    "Next Charge", "Days Away", "First Seen", "Last Seen", "Bank", "Source"
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

def _clear_all_formatting(sheet, worksheet):
    """Wipe all cell formatting on a sheet before re-writing it."""
    sheet.batch_update({"requests": [{
        "updateCells": {
            "range":  {"sheetId": worksheet.id},
            "fields": "userEnteredFormat",
        }
    }]})

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

def _style_month_separators(sheet, worksheet, separator_row_indices):
    """Apply a teal separator style to month header rows (0-indexed)."""
    if not separator_row_indices:
        return
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId":       worksheet.id,
                    "startRowIndex": row_idx,
                    "endRowIndex":   row_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.18, "green": 0.45, "blue": 0.55},
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold": True,
                            "fontSize": 10,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        }
        for row_idx in separator_row_indices
    ]
    sheet.batch_update({"requests": requests})

def get_transaction_from_sheets(transaction_id: str) -> dict | None:
    """
    Look up a single transaction by ID directly from the Google Sheet.
    Used as a fallback when the local DB doesn't have the transaction yet
    (e.g. on a fresh production deployment before the first sync).
    Returns a dict matching the DB transaction format, or None if not found.
    """
    try:
        gc    = _get_client()
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        ws    = sheet.worksheet(TXN_TAB)
        rows  = ws.get_all_values()
        if not rows:
            return None

        # TXN_HEADERS: Date, Bank, Merchant, Category, Amount, Status,
        #              Account (Last 4), Payment Channel, Website, Location,
        #              Plaid Category, Transaction ID
        txn_id_col = TXN_HEADERS.index("Transaction ID")

        for row in rows[1:]:   # skip header
            if len(row) > txn_id_col and row[txn_id_col] == transaction_id:
                def _col(name):
                    idx = TXN_HEADERS.index(name)
                    return row[idx] if idx < len(row) else ""
                try:
                    amount = float(_col("Amount").replace("$", "").replace(",", ""))
                except (ValueError, AttributeError):
                    amount = 0.0
                return {
                    "transaction_id":  transaction_id,
                    "date":            _col("Date"),
                    "bank":            _col("Bank"),
                    "merchant":        _col("Merchant"),
                    "category":        _col("Category"),
                    "amount":          amount,
                    "pending":         _col("Status").lower() == "pending",
                    "account_id":      "",
                    "account_mask":    _col("Account (Last 4)"),
                    "payment_channel": _col("Payment Channel"),
                    "website":         _col("Website"),
                    "location":        _col("Location"),
                    "plaid_category":  _col("Plaid Category"),
                }
    except Exception as e:
        print(f"[sheets] get_transaction_from_sheets error: {e}")
    return None


def sync_transactions(transactions, budget_limits, current_month=None):
    import time as _time
    state    = _load_sync_state()
    txn_hash = _hash_transactions(transactions)
    gc       = _get_client()
    sheet    = gc.open_by_key(GOOGLE_SHEET_ID)

    _delete_old_bank_tabs(sheet)

    if state.get("txn_hash") == txn_hash:
        # Nothing changed — skip the expensive rewrite of all three tabs
        print(f"[sheets] Transactions: unchanged ({len(transactions)} txns), skipping rewrite")
    else:
        t0 = _time.time()
        ws   = _get_or_create_tab(sheet, TXN_TAB)
        ws.clear()
        _clear_all_formatting(sheet, ws)

        rows              = [TXN_HEADERS]
        separator_rows    = []
        current_month_grp = None
        num_cols          = len(TXN_HEADERS)

        for t in sorted(transactions, key=lambda x: x["date"], reverse=True):
            month = t["date"][:7]
            if month != current_month_grp:
                current_month_grp = month
                label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
                separator_rows.append(len(rows))
                rows.append([f"── {label} ──"] + [""] * (num_cols - 1))

            status = "Pending" if t["pending"] else "Posted"
            rows.append([
                t["date"],
                t["bank"],
                t["merchant"],
                t["category"],
                _format_amount(t["amount"]),
                status,
                f"...{t['account_mask']}" if t.get("account_mask") else "",
                t.get("payment_channel", ""),
                t.get("website", ""),
                t.get("location", ""),
                t.get("plaid_category", ""),
                t["transaction_id"],
            ])

        ws.update(rows, "A1")
        _style_header_row(sheet, ws)
        _style_month_separators(sheet, ws, separator_rows)
        print(f"[sheets] Transactions tab: {len(transactions)} rows written ({_time.time()-t0:.1f}s)")

        t0 = _time.time()
        _write_monthly_tab(sheet, current_month or [], budget_limits)
        print(f"[sheets] Monthly tab: written ({_time.time()-t0:.1f}s)")

        t0 = _time.time()
        _write_ytd_tab(sheet, transactions, budget_limits)
        print(f"[sheets] YTD tab: written ({_time.time()-t0:.1f}s)")

        state["txn_hash"] = txn_hash
        _save_sync_state()

    print("[sheets] Transactions sync complete")

def _category_totals_for_summary(transactions, budget_limits):
    from annual import prorated_amount
    posted = [t for t in transactions if not t["pending"] and t["category"] != "Excluded"]
    totals = {}
    for t in posted:
        cat = t["category"]
        totals[cat] = totals.get(cat, 0) + prorated_amount(t)
    all_cats = (set(budget_limits.keys()) | set(totals.keys())) - {"Excluded"}
    return totals, sorted(all_cats)


def _build_budget_table(transactions, budget_limits):
    totals, cats = _category_totals_for_summary(transactions, budget_limits)
    rows = [SUMMARY_HEADERS]
    for cat in cats:
        spent     = round(totals.get(cat, 0), 2)
        limit     = budget_limits.get(cat, 0)
        remaining = round(limit - spent, 2) if limit else "N/A"
        pct_used  = f"{round((spent / limit) * 100, 1)}%" if limit else "N/A"
        rows.append([cat, spent, limit if limit else "No limit", remaining, pct_used])
    total_spent = round(sum(totals.get(c, 0) for c in budget_limits), 2)
    total_limit = sum(budget_limits.values())
    rows.append([
        "TOTAL", total_spent, total_limit,
        round(total_limit - total_spent, 2),
        f"{round((total_spent / total_limit) * 100, 1)}%" if total_limit else "N/A",
    ])
    return rows, len(cats)  # rows, number of data rows (excluding header + TOTAL)

def _build_monthly_breakdown(ytd_transactions, budget_limits):
    """Builds a month-by-month spending table for the YTD tab."""
    from annual import prorated_amount
    from collections import defaultdict

    monthly_data = defaultdict(lambda: defaultdict(float))
    for t in ytd_transactions:
        if not t["pending"] and t["category"] != "Excluded":
            month = t["date"][:7]  # "2026-01"
            monthly_data[month][t["category"]] += prorated_amount(t)

    sorted_months = sorted(monthly_data.keys())
    categories    = sorted(budget_limits.keys())

    headers = ["Month"] + categories + ["Total"]
    rows    = [headers]
    for month in sorted_months:
        label = datetime.strptime(month, "%Y-%m").strftime("%b %Y")
        row   = [label]
        total = 0
        for cat in categories:
            amt = round(monthly_data[month].get(cat, 0), 2)
            row.append(amt)
            total += amt
        row.append(round(total, 2))
        rows.append(row)

    return rows

def _refresh_chart(sheet, spreadsheet_id, ws, data_start, data_end, title):
    """Delete all existing charts on this sheet and add a single pie chart."""
    try:
        resp = sheet.client.request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={"fields": "sheets(properties.sheetId,charts.chartId)"},
        )
        delete_requests = [
            {"deleteEmbeddedObject": {"objectId": c["chartId"]}}
            for s in resp.json().get("sheets", [])
            if s.get("properties", {}).get("sheetId") == ws.id
            for c in s.get("charts", [])
        ]
    except Exception as e:
        print(f"[sheets] Warning: could not fetch chart IDs: {e}")
        delete_requests = []

    add_request = {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "pieChart": {
                        "legendPosition": "RIGHT_LEGEND",
                        "domain": {
                            "sourceRange": {"sources": [{
                                "sheetId":          ws.id,
                                "startRowIndex":    data_start,
                                "endRowIndex":      data_end,
                                "startColumnIndex": 0,
                                "endColumnIndex":   1,
                            }]}
                        },
                        "series": {
                            "sourceRange": {"sources": [{
                                "sheetId":          ws.id,
                                "startRowIndex":    data_start,
                                "endRowIndex":      data_end,
                                "startColumnIndex": 1,
                                "endColumnIndex":   2,
                            }]}
                        },
                        "threeDimensional": False,
                    }
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId":     ws.id,
                            "rowIndex":    1,
                            "columnIndex": 6,
                        },
                        "widthPixels":  480,
                        "heightPixels": 360,
                    }
                }
            }
        }
    }

    all_requests = delete_requests + [add_request]
    if all_requests:
        sheet.batch_update({"requests": all_requests})

def _refresh_bar_chart(sheet, spreadsheet_id, ws, data_start, data_end, title):
    """
    Delete all existing charts on this sheet and add a horizontal grouped bar chart
    showing Spent (col B) vs Budget Limit (col C) per category.
    """
    try:
        resp = sheet.client.request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={"fields": "sheets(properties.sheetId,charts.chartId)"},
        )
        delete_requests = [
            {"deleteEmbeddedObject": {"objectId": c["chartId"]}}
            for s in resp.json().get("sheets", [])
            if s.get("properties", {}).get("sheetId") == ws.id
            for c in s.get("charts", [])
        ]
    except Exception as e:
        print(f"[sheets] Warning: could not fetch chart IDs: {e}")
        delete_requests = []

    def _src(col_start, col_end):
        return {"sourceRange": {"sources": [{
            "sheetId":          ws.id,
            "startRowIndex":    data_start,
            "endRowIndex":      data_end,
            "startColumnIndex": col_start,
            "endColumnIndex":   col_end,
        }]}}

    add_request = {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "basicChart": {
                        "chartType":      "BAR",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Amount ($)"},
                            {"position": "LEFT_AXIS",   "title": "Category"},
                        ],
                        "domains": [{"domain": _src(0, 1)}],
                        "series": [
                            {
                                "series":     _src(1, 2),   # Total Spent
                                "targetAxis": "BOTTOM_AXIS",
                                "color":      {"red": 0.29, "green": 0.53, "blue": 0.91},
                            },
                            {
                                "series":     _src(2, 3),   # Budget Limit
                                "targetAxis": "BOTTOM_AXIS",
                                "color":      {"red": 0.78, "green": 0.78, "blue": 0.78},
                            },
                        ],
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId":     ws.id,
                            "rowIndex":    1,
                            "columnIndex": 6,
                        },
                        "widthPixels":  560,
                        "heightPixels": 400,
                    }
                },
            }
        }
    }

    all_requests = delete_requests + [add_request]
    if all_requests:
        sheet.batch_update({"requests": all_requests})

def _build_pending_section(monthly_transactions):
    """Returns rows for a pending-transactions summary block."""
    pending = [t for t in monthly_transactions if t["pending"]]
    if not pending:
        return None, 0.0

    by_category = {}
    for t in pending:
        cat = t["category"]
        by_category[cat] = by_category.get(cat, 0.0) + float(t["amount"])

    rows = [["Pending Transactions", "Amount"]]
    for cat in sorted(by_category):
        rows.append([cat, round(by_category[cat], 2)])
    total = round(sum(by_category.values()), 2)
    rows.append(["TOTAL PENDING", total])
    return rows, total


def _style_pending_section(sheet, worksheet, header_row_idx, total_row_idx):
    """Style the pending section header (orange) and total row (bold)."""
    requests = [
        # Section header row — orange background
        {
            "repeatCell": {
                "range": {
                    "sheetId":       worksheet.id,
                    "startRowIndex": header_row_idx,
                    "endRowIndex":   header_row_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.95, "green": 0.60, "blue": 0.20},
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold": True,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        # TOTAL PENDING row — bold
        {
            "repeatCell": {
                "range": {
                    "sheetId":       worksheet.id,
                    "startRowIndex": total_row_idx,
                    "endRowIndex":   total_row_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat(textFormat)",
            }
        },
    ]
    sheet.batch_update({"requests": requests})


def _write_monthly_tab(sheet, monthly_transactions, budget_limits):
    from config import GOOGLE_SHEET_ID
    ws          = _get_or_create_tab(sheet, MONTHLY_TAB)
    ws.clear()
    _clear_all_formatting(sheet, ws)
    month_label = datetime.now().strftime("%B %Y")

    table, n = _build_budget_table(monthly_transactions, budget_limits)
    section  = [[f"=== This Month — {month_label} ===", "", "", "", ""]] + table
    ws.update(section, "A1")
    _style_header_row(sheet, ws)

    # Horizontal bar chart — category rows start at index 2 (skip section label + header)
    # Exclude the TOTAL row from the chart (data_end = 2 + n, not 2 + n + 1)
    _refresh_bar_chart(
        sheet, GOOGLE_SHEET_ID, ws,
        data_start=2, data_end=2 + n,
        title=f"Spent vs Budget — {month_label}",
    )

    # --- Pending transactions summary ---
    pending_rows, pending_total = _build_pending_section(monthly_transactions)
    if pending_rows:
        # section = 1 title + 1 header + n cats + 1 TOTAL = n + 3 rows
        # Leave 2 blank rows before pending block
        pending_start = len(section) + 3          # 1-indexed row number
        ws.update(pending_rows, f"A{pending_start}")

        # Style: header row and total row (0-indexed for API)
        pending_header_idx = pending_start - 1           # 0-indexed
        pending_total_idx  = pending_header_idx + len(pending_rows) - 1
        _style_pending_section(sheet, ws, pending_header_idx, pending_total_idx)
        print(f"[sheets] Pending total: ${pending_total} across {len(pending_rows) - 2} categories")
    else:
        print("[sheets] No pending transactions this month")

    print(f"[sheets] Monthly tab written ({month_label})")

def _write_ytd_tab(sheet, ytd_transactions, budget_limits):
    from config import GOOGLE_SHEET_ID
    ws         = _get_or_create_tab(sheet, YTD_TAB)
    ws.clear()
    _clear_all_formatting(sheet, ws)
    year_label = str(datetime.now().year)

    # --- YTD budget table ---
    table, n = _build_budget_table(ytd_transactions, budget_limits)
    section  = [[f"=== Year to Date — {year_label} ===", "", "", "", ""]] + table
    ws.update(section, "A1")
    _style_header_row(sheet, ws)

    # Pie chart for YTD — data rows start at index 2
    _refresh_chart(
        sheet, GOOGLE_SHEET_ID, ws,
        data_start=2, data_end=2 + n,
        title=f"Spending — YTD {year_label}",
    )

    # --- Month-by-month breakdown ---
    breakdown       = _build_monthly_breakdown(ytd_transactions, budget_limits)
    breakdown_start = len(section) + 3   # leave 2 blank rows
    ws.update([[f"=== Month-by-Month — {year_label} ==="]], f"A{breakdown_start}")
    ws.update(breakdown, f"A{breakdown_start + 1}")

    # --- Bank date ranges ---
    bank_dates = {}
    for t in ytd_transactions:
        bank, d = t["bank"], t["date"]
        if bank not in bank_dates:
            bank_dates[bank] = {"earliest": d, "latest": d}
        else:
            if d < bank_dates[bank]["earliest"]: bank_dates[bank]["earliest"] = d
            if d > bank_dates[bank]["latest"]:   bank_dates[bank]["latest"]   = d

    dates_start = breakdown_start + 1 + len(breakdown) + 3
    date_rows   = [["Bank", "Earliest Transaction", "Latest Transaction"]]
    for bank in sorted(bank_dates):
        date_rows.append([bank, bank_dates[bank]["earliest"], bank_dates[bank]["latest"]])
    ws.update(date_rows, f"A{dates_start}")

    # --- Last synced ---
    last_row = dates_start + len(date_rows) + 2
    ws.update(
        [[f"Last synced: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"]],
        f"A{last_row}"
    )

    print(f"[sheets] YTD tab written ({year_label}, {len(bank_dates)} banks)")

def _est_monthly(stream) -> float:
    """
    Estimate monthly cost from a detected subscription stream.
    For variable-amount subscriptions, uses average_amount as the basis.
    """
    freq = stream.get("frequency", "")
    # Variable-amount subs: use average rather than last charge
    if stream.get("variable_amount"):
        amt = float(stream.get("average_amount") or stream.get("last_amount") or 0)
    else:
        amt = float(stream.get("last_amount") or stream.get("average_amount") or 0)
    if "Annual"      in freq: return round(amt / 12, 2)
    if "Semi"        in freq: return round(amt / 6,  2)
    if "Quarterly"   in freq: return round(amt / 3,  2)
    if "Bi-Weekly"   in freq: return round(amt * 2.17, 2)
    if "Weekly"      in freq: return round(amt * 4.33, 2)
    return round(amt, 2)   # Monthly


def _billing_cycle(frequency: str) -> str:
    """
    Classify a frequency string into a high-level billing cycle label.
    Annual / Semi-Annual / Quarterly → their own labels.
    Everything else → "Monthly" (weekly/bi-weekly/monthly all recur at sub-month cadence).
    """
    if "Annual" in frequency:   return "Annual"
    if "Semi"   in frequency:   return "Semi-Annual"
    if "Quarterly" in frequency: return "Quarterly"
    return "Monthly"


def _style_subscription_rows(sheet, worksheet, annual_row_indices: list,
                              monthly_row_indices: list):
    """
    Style subscription rows:
      Annual  → gold background + bold  (stands out clearly)
      Monthly → soft steel-blue tint    (subtle, just enough to group them)
    """
    requests = []

    for row_idx in annual_row_indices:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId":       worksheet.id,
                    "startRowIndex": row_idx,
                    "endRowIndex":   row_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 0.84, "blue": 0.0},
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

    for row_idx in monthly_row_indices:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId":       worksheet.id,
                    "startRowIndex": row_idx,
                    "endRowIndex":   row_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        # Muted slate-blue — light enough to not distract
                        "backgroundColor": {"red": 0.88, "green": 0.92, "blue": 0.97},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor)",
            }
        })

    if requests:
        sheet.batch_update({"requests": requests})


def sync_subscriptions(recurring_streams):
    state    = _load_sync_state()
    sub_hash = _hash_subscriptions(recurring_streams)
    if state.get("sub_hash") == sub_hash:
        print(f"[sheets] Subscriptions: unchanged ({len(recurring_streams)} subs), skipping rewrite")
        return

    gc    = _get_client()
    sheet = gc.open_by_key(GOOGLE_SHEET_ID)
    ws    = _get_or_create_tab(sheet, SUBS_TAB)
    ws.clear()
    _clear_all_formatting(sheet, ws)

    rows          = [SUBS_HEADERS]
    annual_rows   = []   # 0-indexed row numbers → gold + bold
    monthly_rows  = []   # 0-indexed row numbers → subtle blue tint

    for stream in recurring_streams:
        sub_type  = (stream.get("sub_type") or "").replace("_", " ").title() or "—"
        freq      = stream.get("frequency", "Unknown")
        cycle     = _billing_cycle(freq)
        is_annual = "Annual" in cycle

        days_until = stream.get("days_until_charge")
        if days_until is None:
            days_label = ""
        elif days_until < 0:
            days_label = f"Overdue ({abs(days_until)}d ago)"
        elif days_until == 0:
            days_label = "Today"
        else:
            days_label = f"{days_until}d"

        is_variable = stream.get("variable_amount", False)
        charge_amt  = "Variable" if is_variable else _format_amount(stream.get("last_amount", 0))
        monthly_amt = _est_monthly(stream)
        annual_total = (_format_amount(stream.get("last_amount", 0))
                        if is_annual else round(monthly_amt * 12, 2))

        row_idx = len(rows)   # 0-indexed position of the row about to be appended
        if is_annual:
            annual_rows.append(row_idx)
        else:
            monthly_rows.append(row_idx)

        rows.append([
            stream.get("merchant", "Unknown"),
            stream.get("tag", ""),
            stream.get("category", "Other"),
            sub_type,
            cycle,
            freq,
            charge_amt,
            monthly_amt,
            annual_total,
            stream.get("next_charge_date", ""),
            days_label,
            stream.get("first_date", ""),
            stream.get("last_date", ""),
            stream.get("bank", ""),
            stream.get("source", "auto").title(),
        ])

    # Grand totals row
    if len(rows) > 1:
        total_monthly = round(sum(_est_monthly(s) for s in recurring_streams), 2)
        total_annual  = round(total_monthly * 12, 2)
        rows.append(["TOTAL", "", "", "", "", "", "", total_monthly, total_annual,
                     "", "", "", "", "", ""])

    ws.update(rows, "A1")
    _style_header_row(sheet, ws)
    _style_subscription_rows(sheet, ws, annual_rows, monthly_rows)

    # Bold + border the TOTAL row
    if len(rows) > 1:
        total_row_idx = len(rows) - 1
        sheet.batch_update({"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId":       ws.id,
                    "startRowIndex": total_row_idx,
                    "endRowIndex":   total_row_idx + 1,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(textFormat)",
            }
        }]})

    state["sub_hash"] = sub_hash
    _save_sync_state()
    print(f"[sheets] Subscriptions: {len(recurring_streams)} streams written")

