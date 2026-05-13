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

def sync_transactions(transactions, budget_limits, current_month=None):
    gc    = _get_client()
    sheet = gc.open_by_key(GOOGLE_SHEET_ID)

    read_custom_category_overrides()
    _delete_old_bank_tabs(sheet)

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
            "",
        ])

    ws.update(rows, "A1")
    _style_header_row(sheet, ws)
    print(f"[sheets] Transactions: {len(transactions)} rows written ({TXN_TAB} tab)")

    _write_summary(sheet, transactions, budget_limits, current_month or [])
    _write_last_synced(sheet)
    print("[sheets] Sync complete")

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

def _write_summary(sheet, ytd_transactions, budget_limits, monthly_transactions):
    from config import GOOGLE_SHEET_ID
    from datetime import datetime
    ws = _get_or_create_tab(sheet, SUMMARY_TAB)
    ws.clear()

    now         = datetime.now()
    month_label = now.strftime("%B %Y")
    year_label  = str(now.year)

    # --- Monthly table ---
    monthly_table, monthly_n = _build_budget_table(monthly_transactions, budget_limits)
    monthly_section = [[f"=== This Month — {month_label} ===", "", "", "", ""]] + monthly_table
    ws.update(monthly_section, "A1")
    _style_header_row(sheet, ws)

    # monthly_section layout (0-indexed rows in sheet):
    #   0: section label
    #   1: column headers
    #   2 .. 2+monthly_n-1: category data rows
    #   2+monthly_n: TOTAL row
    monthly_data_start_0 = 2
    monthly_data_end_0   = 2 + monthly_n  # exclusive

    # --- YTD table ---
    ytd_table, ytd_n = _build_budget_table(ytd_transactions, budget_limits)
    ytd_section       = [[f"=== Year to Date — {year_label} ===", "", "", "", ""]] + ytd_table
    ytd_start_1       = len(monthly_section) + 3   # 1-indexed, leave 2 blank rows
    ws.update(ytd_section, f"A{ytd_start_1}")

    # ytd layout in sheet (0-indexed):
    #   ytd_start_1-1: section label
    #   ytd_start_1:   column headers
    #   ytd_start_1+1 .. ytd_start_1+ytd_n: category data rows
    ytd_data_start_0 = ytd_start_1      # 0-indexed = ytd_start_1 (after section label + header = +2, but section label is at ytd_start_1-1 so header is at ytd_start_1)
    ytd_data_start_0 = (ytd_start_1 - 1) + 2   # skip section label + column header
    ytd_data_end_0   = ytd_data_start_0 + ytd_n

    # --- Bank date ranges ---
    bank_dates = {}
    for t in ytd_transactions:
        bank, d = t["bank"], t["date"]
        if bank not in bank_dates:
            bank_dates[bank] = {"earliest": d, "latest": d}
        else:
            if d < bank_dates[bank]["earliest"]: bank_dates[bank]["earliest"] = d
            if d > bank_dates[bank]["latest"]:   bank_dates[bank]["latest"]   = d

    dates_start_1 = ytd_start_1 + len(ytd_section) + 3
    date_rows = [["Bank", "Earliest Transaction", "Latest Transaction"]]
    for bank in sorted(bank_dates):
        date_rows.append([bank, bank_dates[bank]["earliest"], bank_dates[bank]["latest"]])
    ws.update(date_rows, f"A{dates_start_1}")

    # --- Pie charts ---
    _refresh_charts(
        sheet, GOOGLE_SHEET_ID, ws,
        monthly_data_start_0, monthly_data_end_0, month_label,
        ytd_data_start_0,     ytd_data_end_0,     year_label,
    )

    print(f"[sheets] Summary written (monthly + YTD, {len(bank_dates)} banks)")

def _refresh_charts(sheet, spreadsheet_id, ws,
                    m_start, m_end, month_label,
                    y_start, y_end, year_label):
    """Delete existing charts in the Summary sheet and recreate monthly + YTD pie charts."""
    # Get existing chart IDs
    try:
        resp = sheet.client.request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={"fields": "sheets(properties.sheetId,charts.chartId)"},
        )
        delete_requests = []
        for s in resp.json().get("sheets", []):
            if s.get("properties", {}).get("sheetId") == ws.id:
                for c in s.get("charts", []):
                    delete_requests.append(
                        {"deleteEmbeddedObject": {"objectId": c["chartId"]}}
                    )
    except Exception as e:
        print(f"[sheets] Warning: could not fetch chart IDs: {e}")
        delete_requests = []

    def _pie_req(title, data_start, data_end, anchor_row, anchor_col):
        return {
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
                                "rowIndex":    anchor_row,
                                "columnIndex": anchor_col,
                            },
                            "widthPixels":  480,
                            "heightPixels": 360,
                        }
                    }
                }
            }
        }

    add_requests = [
        _pie_req(f"Spending — {month_label}",  m_start, m_end, anchor_row=1,    anchor_col=6),
        _pie_req(f"Spending — YTD {year_label}", y_start, y_end, anchor_row=22, anchor_col=6),
    ]

    all_requests = delete_requests + add_requests
    if all_requests:
        sheet.batch_update({"requests": all_requests})
    print("[sheets] Charts refreshed")

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
