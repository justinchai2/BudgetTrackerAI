import plaid
from plaid.api import plaid_api
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

def fetch_transactions(bank_name, access_token, days_back=TRANSACTION_LOOKBACK_DAYS):
    client = _get_client()
    end_date   = date.today()
    start_date = end_date - timedelta(days=days_back)

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

    return [_normalize(bank_name, txn) for txn in transactions]

def _normalize(bank_name, txn):
    return {
        "transaction_id": txn["transaction_id"],
        "bank":           bank_name,
        "date":           str(txn["date"]),
        "merchant":       txn.get("merchant_name") or txn.get("name", "Unknown"),
        "category":       _top_category(txn.get("category")),
        "amount":         txn["amount"],   # positive = expense in Plaid convention
        "pending":        txn["pending"],
        "account_id":     txn["account_id"],
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
