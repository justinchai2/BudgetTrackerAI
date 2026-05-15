import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_webhook_update_request import ItemWebhookUpdateRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.transactions_recurring_get_request import TransactionsRecurringGetRequest
from datetime import date, timedelta
from config import (
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV,
    PLAID_ACCESS_TOKENS, TRANSACTION_LOOKBACK_DAYS
)

ENV_MAP = {
    "sandbox":    plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}

def _get_client():
    configuration = plaid.Configuration(
        host=ENV_MAP.get(PLAID_ENV, plaid.Environment.Sandbox),
        api_key={
            "clientId": PLAID_CLIENT_ID,
            "secret":   PLAID_SECRET,
        }
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))

def _top_category(categories):
    if not categories:
        return "Other"
    return categories[0]

def _format_location(loc):
    if not loc:
        return ""
    parts = [loc.get("address"), loc.get("city"), loc.get("region")]
    return ", ".join(p for p in parts if p)

def _fetch_account_masks(client, access_token):
    """Returns {account_id: last-4-digit mask} for all accounts on this token."""
    try:
        response = client.accounts_get(AccountsGetRequest(access_token=access_token))
        return {acc["account_id"]: acc.get("mask") or "" for acc in response["accounts"]}
    except Exception as e:
        print(f"[plaid] Warning: could not fetch account masks: {e}")
        return {}

def fetch_transactions(bank_name, access_token, days_back=TRANSACTION_LOOKBACK_DAYS):
    client = _get_client()
    end_date   = date.today()
    start_date = end_date - timedelta(days=days_back)

    masks = _fetch_account_masks(client, access_token)

    request  = TransactionsGetRequest(
        access_token=access_token,
        start_date=start_date,
        end_date=end_date,
    )
    response = client.transactions_get(request)
    transactions = list(response["transactions"])

    # Paginate if needed
    while len(transactions) < response["total_transactions"]:
        paged = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(offset=len(transactions)),
        )
        response = client.transactions_get(paged)
        transactions.extend(response["transactions"])

    return [_normalize(bank_name, txn, masks) for txn in transactions if txn["amount"] != 0]

def _normalize(bank_name, txn, masks=None):
    pfc            = txn.get("personal_finance_category") or {}
    plaid_category = pfc.get("detailed") or pfc.get("primary") or _top_category(txn.get("category"))
    account_id     = txn["account_id"]
    account_mask   = (masks or {}).get(account_id, "")
    return {
        "transaction_id":  txn["transaction_id"],
        "bank":            bank_name,
        "date":            str(txn["date"]),
        "merchant":        txn.get("merchant_name") or txn.get("name", "Unknown"),
        "category":        _top_category(txn.get("category")),
        "plaid_category":  plaid_category,        # e.g. "FOOD_AND_DRINK_FAST_FOOD"
        "amount":          txn["amount"],          # positive = expense in Plaid convention
        "pending":         txn["pending"],
        "account_id":           account_id,
        "account_mask":         account_mask,              # last 4 digits of card number
        "pending_transaction_id": txn.get("pending_transaction_id") or "",  # links to old pending ID
        "payment_channel": txn.get("payment_channel") or "",   # "in store", "online", "other"
        "website":         txn.get("website") or "",
        "location":        _format_location(txn.get("location")),
    }

def fetch_recurring(bank_name, access_token):
    client = _get_client()
    request  = TransactionsRecurringGetRequest(access_token=access_token)
    response = client.transactions_recurring_get(request)

    streams = []
    for stream in list(response.get("outflow_streams", [])):
        streams.append(_normalize_stream(bank_name, stream, stream_type="expense"))
    for stream in list(response.get("inflow_streams", [])):
        streams.append(_normalize_stream(bank_name, stream, stream_type="income"))

    return streams

def _normalize_stream(bank_name, stream, stream_type):
    avg  = stream.get("average_amount") or {}
    last = stream.get("last_amount") or {}
    return {
        "bank":           bank_name,
        "merchant":       stream.get("merchant_name") or stream.get("description", "Unknown"),
        "category":       _top_category(stream.get("category")),
        "frequency":      stream.get("frequency", "UNKNOWN").replace("_", " ").title(),
        "average_amount": round(float(avg.get("amount", 0)), 2),
        "last_amount":    round(float(last.get("amount", 0)), 2),
        "last_date":      str(stream.get("last_date", "")),
        "first_date":     str(stream.get("first_date", "")),
        "status":         stream.get("status", "UNKNOWN").title(),
        "stream_type":    stream_type,
        "is_active":      stream.get("is_active", True),
    }

def fetch_all_recurring():
    all_streams = []
    for bank_name, access_token in PLAID_ACCESS_TOKENS.items():
        if not access_token:
            continue
        try:
            streams = fetch_recurring(bank_name, access_token)
            expenses = sum(1 for s in streams if s["stream_type"] == "expense")
            income   = sum(1 for s in streams if s["stream_type"] == "income")
            print(f"[plaid] {bank_name} recurring: {expenses} expenses, {income} income streams")
            all_streams.extend(streams)
        except Exception as e:
            print(f"[plaid] Error fetching recurring for {bank_name}: {e}")
    return all_streams

def fetch_all_transactions():
    all_transactions = []
    for bank_name, access_token in PLAID_ACCESS_TOKENS.items():
        if not access_token:
            print(f"[plaid] Skipping {bank_name} — no access token in .env")
            continue
        try:
            txns = fetch_transactions(bank_name, access_token)
            posted  = sum(1 for t in txns if not t["pending"])
            pending = sum(1 for t in txns if t["pending"])
            print(f"[plaid] {bank_name}: {posted} posted, {pending} pending")
            all_transactions.extend(txns)
        except Exception as e:
            print(f"[plaid] Error fetching {bank_name}: {e}")

    return all_transactions

def fetch_item_status(bank_name, access_token):
    """Returns the connection health of a single linked bank Item."""
    client = _get_client()
    try:
        response = client.item_get(ItemGetRequest(access_token=access_token))
        item     = response["item"]
        txn_status = (response.get("status") or {}).get("transactions") or {}
        error    = item.get("error")
        return {
            "bank":                   bank_name,
            "institution_id":         item.get("institution_id", ""),
            "healthy":                error is None,
            "error_code":             error.get("error_code")    if error else None,
            "error_message":          error.get("error_message") if error else None,
            "last_successful_update": txn_status.get("last_successful_update"),
            "last_failed_update":     txn_status.get("last_failed_update"),
        }
    except Exception as e:
        return {
            "bank":                   bank_name,
            "institution_id":         "",
            "healthy":                False,
            "error_code":             "REQUEST_FAILED",
            "error_message":          str(e),
            "last_successful_update": None,
            "last_failed_update":     None,
        }

def create_update_link_token(access_token, redirect_uri=None):
    """
    Creates a Plaid Link token in update mode for re-authorizing an existing Item.
    The returned token is used to open Plaid Link in the browser without creating
    a new access token — it just refreshes the existing connection.
    """
    client = _get_client()
    kwargs = dict(
        user=LinkTokenCreateRequestUser(client_user_id="budget-tracker-user"),
        client_name="BudgetTrackerAI",
        access_token=access_token,
        language="en",
        country_codes=[CountryCode("US")],
    )
    if redirect_uri:
        kwargs["redirect_uri"] = redirect_uri
    response = client.link_token_create(LinkTokenCreateRequest(**kwargs))
    return response["link_token"]

def update_item_webhook(bank_name, access_token, webhook_url):
    """Register or update the webhook URL for a single Plaid Item."""
    client = _get_client()
    client.item_webhook_update(
        ItemWebhookUpdateRequest(access_token=access_token, webhook=webhook_url)
    )
    print(f"[plaid] Webhook updated for {bank_name} → {webhook_url}")

def update_all_webhooks(webhook_url):
    """Register the webhook URL across all linked banks."""
    results = {}
    for bank_name, access_token in PLAID_ACCESS_TOKENS.items():
        if not access_token:
            continue
        try:
            update_item_webhook(bank_name, access_token, webhook_url)
            results[bank_name] = "ok"
        except Exception as e:
            print(f"[plaid] Failed to update webhook for {bank_name}: {e}")
            results[bank_name] = str(e)
    return results

def fetch_all_item_statuses():
    """Returns health status for every linked bank."""
    statuses = []
    for bank_name, access_token in PLAID_ACCESS_TOKENS.items():
        if access_token:
            statuses.append(fetch_item_status(bank_name, access_token))
    return statuses
