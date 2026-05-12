import sqlite3
from datetime import datetime, timedelta

DB_FILE = "transactions.db"

def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                bank           TEXT NOT NULL,
                date           TEXT NOT NULL,
                merchant       TEXT NOT NULL,
                category       TEXT NOT NULL,
                amount         REAL NOT NULL,
                pending        INTEGER NOT NULL,
                account_id     TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON transactions(date)")
    print("[db] Initialized")

def upsert_transactions(transactions):
    rows = [
        {
            "transaction_id": t["transaction_id"],
            "bank":           t["bank"],
            "date":           t["date"],
            "merchant":       t["merchant"],
            "category":       t["category"],
            "amount":         t["amount"],
            "pending":        int(t["pending"]),
            "account_id":     t["account_id"],
        }
        for t in transactions
    ]
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO transactions
                (transaction_id, bank, date, merchant, category, amount, pending, account_id)
            VALUES
                (:transaction_id, :bank, :date, :merchant, :category, :amount, :pending, :account_id)
            ON CONFLICT(transaction_id) DO UPDATE SET
                category = excluded.category,
                pending  = excluded.pending,
                amount   = excluded.amount
        """, rows)
    print(f"[db] Upserted {len(rows)} transactions")

def update_merchant_category(merchant, category):
    """Update all stored transactions for a merchant when user recategorizes."""
    with _get_conn() as conn:
        updated = conn.execute(
            "UPDATE transactions SET category = ? WHERE merchant = ?",
            (category, merchant)
        ).rowcount
    if updated:
        print(f"[db] Updated {updated} transactions: '{merchant}' -> '{category}'")

def get_transactions(start_date=None, end_date=None):
    conditions, params = [], []
    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)

    query = "SELECT * FROM transactions"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY date DESC"

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_to_dict(r) for r in rows]

def get_current_month_transactions():
    today = datetime.today()
    start = today.replace(day=1).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")
    return get_transactions(start_date=start, end_date=end)

def get_year_to_date_transactions():
    start = datetime.today().replace(month=1, day=1).strftime("%Y-%m-%d")
    return get_transactions(start_date=start)

def purge_old_transactions(max_days=730):
    cutoff = (datetime.today() - timedelta(days=max_days)).strftime("%Y-%m-%d")
    with _get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM transactions WHERE date < ?", (cutoff,)
        ).rowcount
    if deleted:
        print(f"[db] Purged {deleted} transactions older than {max_days} days")
    return deleted

def _to_dict(row):
    return {
        "transaction_id": row["transaction_id"],
        "bank":           row["bank"],
        "date":           row["date"],
        "merchant":       row["merchant"],
        "category":       row["category"],
        "amount":         row["amount"],
        "pending":        bool(row["pending"]),
        "account_id":     row["account_id"],
    }
