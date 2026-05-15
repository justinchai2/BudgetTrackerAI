import os
import sqlite3
import calendar
from datetime import datetime, timedelta


# ── Next-charge-date helper ───────────────────────────────────────────────────

FREQ_DAYS = {
    "Weekly":      7,
    "Bi-Weekly":  14,
}

def _next_charge(last_date: str, frequency: str) -> str:
    """Return the predicted next charge date as YYYY-MM-DD."""
    try:
        d = datetime.strptime(last_date, "%Y-%m-%d")
        if frequency in FREQ_DAYS:
            return (d + timedelta(days=FREQ_DAYS[frequency])).strftime("%Y-%m-%d")

        # Month-based: advance by N months keeping day, clamped to month end
        month_jumps = {"Monthly": 1, "Quarterly": 3, "Semi-Annual": 6, "Annual": 12}
        jump = month_jumps.get(frequency)
        if jump is None:
            return ""
        m = d.month + jump
        y = d.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        day = min(d.day, calendar.monthrange(y, m)[1])
        return d.replace(year=y, month=m, day=day).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _days_until(date_str: str) -> int | None:
    """Return days from today until date_str (negative = overdue)."""
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d") - datetime.today()).days
    except Exception:
        return None

DB_FILE = "transactions.db"

def _get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_conn() as conn:
        # ── transactions table ────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id  TEXT PRIMARY KEY,
                bank            TEXT NOT NULL,
                date            TEXT NOT NULL,
                merchant        TEXT NOT NULL,
                category        TEXT NOT NULL,
                amount          REAL NOT NULL,
                pending         INTEGER NOT NULL,
                account_id      TEXT NOT NULL,
                payment_channel TEXT NOT NULL DEFAULT '',
                website         TEXT NOT NULL DEFAULT '',
                location        TEXT NOT NULL DEFAULT '',
                plaid_category  TEXT NOT NULL DEFAULT '',
                account_mask    TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON transactions(date)")
        for col in ("payment_channel", "website", "location", "plaid_category", "account_mask"):
            try:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                print(f"[db] Migrated: added column '{col}'")
            except sqlite3.OperationalError:
                pass

        # ── subscriptions table ───────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                merchant          TEXT PRIMARY KEY,
                source            TEXT NOT NULL DEFAULT 'auto',
                frequency         TEXT NOT NULL,
                billing_cycle     TEXT NOT NULL DEFAULT 'Monthly',
                last_amount       REAL NOT NULL,
                average_amount    REAL NOT NULL,
                last_date         TEXT NOT NULL,
                first_date        TEXT NOT NULL,
                next_charge_date  TEXT NOT NULL DEFAULT '',
                days_until_charge INTEGER,
                category          TEXT NOT NULL DEFAULT 'Other',
                bank              TEXT NOT NULL DEFAULT '',
                sub_type          TEXT NOT NULL DEFAULT '',
                occurrence_count  TEXT NOT NULL DEFAULT '',
                is_active         INTEGER NOT NULL DEFAULT 1,
                updated_at        TEXT NOT NULL DEFAULT '',
                variable_amount   INTEGER NOT NULL DEFAULT 0,
                tag               TEXT NOT NULL DEFAULT '',
                is_estimate       INTEGER NOT NULL DEFAULT 0,
                prev_amount       REAL
            )
        """)
        # Migrate existing subscriptions table if needed
        for col_def in (
            "source            TEXT NOT NULL DEFAULT 'auto'",
            "billing_cycle     TEXT NOT NULL DEFAULT 'Monthly'",
            "next_charge_date  TEXT NOT NULL DEFAULT ''",
            "days_until_charge INTEGER",
            "sub_type          TEXT NOT NULL DEFAULT ''",
            "occurrence_count  TEXT NOT NULL DEFAULT ''",
            "is_active         INTEGER NOT NULL DEFAULT 1",
            "updated_at        TEXT NOT NULL DEFAULT ''",
            "variable_amount   INTEGER NOT NULL DEFAULT 0",
            "tag               TEXT NOT NULL DEFAULT ''",
            "is_estimate       INTEGER NOT NULL DEFAULT 0",
            "prev_amount       REAL",
        ):
            col_name = col_def.split()[0]
            try:
                conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col_def}")
                print(f"[db] Migrated subscriptions: added column '{col_name}'")
            except sqlite3.OperationalError:
                pass

        # ── merchant_categories table ─────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS merchant_categories (
                merchant      TEXT PRIMARY KEY,
                category      TEXT NOT NULL,
                source        TEXT NOT NULL DEFAULT 'gemini',
                confidence    REAL NOT NULL DEFAULT 1.0,
                is_uncertain  INTEGER NOT NULL DEFAULT 0,
                sample_amount REAL,
                sample_date   TEXT,
                sample_bank   TEXT,
                confirmed_at  TEXT,
                merchant_id   TEXT UNIQUE
            )
        """)
        # Migrate: add merchant_id column to existing tables
        try:
            conn.execute("ALTER TABLE merchant_categories ADD COLUMN merchant_id TEXT UNIQUE")
            print("[db] Migrated merchant_categories: added column 'merchant_id'")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc_uncertain "
            "ON merchant_categories(is_uncertain)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_merchant_id "
            "ON merchant_categories(merchant_id)"
        )

        # Backfill merchant_id for any rows that don't have one yet
        _backfill_merchant_ids(conn)

        # ── One-time migration from JSON files ────────────────────────────
        _migrate_category_json(conn)

    print("[db] Initialized")


def _backfill_merchant_ids(conn):
    """Generate stable UUIDs for any merchant_categories rows that don't have one yet."""
    import uuid as _uuid
    rows = conn.execute(
        "SELECT merchant FROM merchant_categories WHERE merchant_id IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE merchant_categories SET merchant_id = ? WHERE merchant = ?",
            (str(_uuid.uuid4()), row["merchant"]),
        )
    if rows:
        print(f"[db] Backfilled merchant_id for {len(rows)} merchant_categories row(s)")


def _migrate_category_json(conn):
    """
    Import merchant_cache.json and uncertain_merchants.json into the DB,
    then rename them so the migration doesn't run again.
    """
    import json as _json

    cache_file     = "merchant_cache.json"
    uncertain_file = "uncertain_merchants.json"
    migrated       = 0

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cache = _json.load(f)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for merchant, category in cache.items():
            conn.execute("""
                INSERT INTO merchant_categories
                    (merchant, category, source, confidence, is_uncertain, confirmed_at)
                VALUES (?, ?, 'user', 1.0, 0, ?)
                ON CONFLICT(merchant) DO NOTHING
            """, (merchant, category, now))
            migrated += 1
        os.rename(cache_file, cache_file + ".migrated")
        print(f"[db] Migrated {migrated} entries from {cache_file}")

    if os.path.exists(uncertain_file):
        with open(uncertain_file) as f:
            uncertain = _json.load(f)
        for merchant, data in uncertain.items():
            sample = data.get("sample", {})
            conn.execute("""
                INSERT INTO merchant_categories
                    (merchant, category, source, confidence, is_uncertain,
                     sample_amount, sample_date, sample_bank)
                VALUES (?, ?, 'gemini', ?, 1, ?, ?, ?)
                ON CONFLICT(merchant) DO NOTHING
            """, (
                merchant, data.get("category", "Other"),
                data.get("confidence", 0.5),
                sample.get("amount"), sample.get("date"), sample.get("bank"),
            ))
        os.rename(uncertain_file, uncertain_file + ".migrated")
        print(f"[db] Migrated {len(uncertain)} uncertain merchants from {uncertain_file}")

def upsert_transactions(transactions):
    """
    Insert or update transactions.

    Pre-dedup step: when the same bank is linked under two access tokens,
    Plaid returns identical charges with different transaction_ids but the same
    (merchant, date, amount, account_mask).  We collapse those to one record
    in the incoming batch before touching the DB — keeping the posted copy over
    a pending one, and the one with the longer/non-empty transaction_id otherwise.
    """
    # --- Pre-dedup incoming batch by (merchant, date, amount, account_mask) ---
    seen: dict[tuple, dict] = {}
    for t in transactions:
        mask = t.get("account_mask") or ""
        key  = (t["merchant"], t["date"], float(t["amount"]), mask)
        if key not in seen:
            seen[key] = t
        else:
            existing = seen[key]
            # Prefer posted over pending
            if existing["pending"] and not t["pending"]:
                seen[key] = t
    deduped = list(seen.values())
    skipped = len(transactions) - len(deduped)
    if skipped:
        print(f"[db] Pre-dedup: dropped {skipped} cross-token duplicate(s) before insert")

    rows = [
        {
            "transaction_id":  t["transaction_id"],
            "bank":            t["bank"],
            "date":            t["date"],
            "merchant":        t["merchant"],
            "category":        t["category"],
            "amount":          t["amount"],
            "pending":         int(t["pending"]),
            "account_id":      t["account_id"],
            "payment_channel": t.get("payment_channel", ""),
            "website":         t.get("website", ""),
            "location":        t.get("location", ""),
            "plaid_category":  t.get("plaid_category", ""),
            "account_mask":    t.get("account_mask", ""),
        }
        for t in deduped
    ]
    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO transactions
                (transaction_id, bank, date, merchant, category, amount, pending, account_id,
                 payment_channel, website, location, plaid_category, account_mask)
            VALUES
                (:transaction_id, :bank, :date, :merchant, :category, :amount, :pending, :account_id,
                 :payment_channel, :website, :location, :plaid_category, :account_mask)
            ON CONFLICT(transaction_id) DO UPDATE SET
                category        = excluded.category,
                pending         = excluded.pending,
                amount          = excluded.amount,
                payment_channel = excluded.payment_channel,
                website         = excluded.website,
                location        = excluded.location,
                plaid_category  = excluded.plaid_category,
                account_mask    = excluded.account_mask
        """, rows)
    print(f"[db] Upserted {len(rows)} transactions")

def resolve_pending_transactions(transactions):
    """
    Delete old pending records that have been superseded by their posted counterparts.
    Plaid sets pending_transaction_id on the posted transaction pointing back to the
    old pending ID — we use that to find and remove the stale pending record.
    """
    pending_ids = [
        t["pending_transaction_id"]
        for t in transactions
        if not t["pending"] and t.get("pending_transaction_id")
    ]
    if not pending_ids:
        return 0
    with _get_conn() as conn:
        placeholders = ",".join("?" * len(pending_ids))
        deleted = conn.execute(
            f"DELETE FROM transactions WHERE transaction_id IN ({placeholders})",
            pending_ids,
        ).rowcount
    if deleted:
        print(f"[db] Resolved {deleted} pending→posted duplicate(s)")
    return deleted

def deduplicate_transactions():
    """
    Two-pass deduplication:

    Pass 1 — same transaction_id conflict (shouldn't happen with upsert, safety net).

    Pass 2 — cross-token duplicates: same merchant + date + amount + account_mask
      but DIFFERENT transaction_ids.  This happens when the same bank is linked
      under two Plaid access tokens (e.g. original token + re-link token).  Each
      token returns the same physical card's transactions with different Plaid IDs.
      We keep the posted copy (pending=0) and discard the rest.

    Runs on startup and after every sync.
    """
    deleted = 0
    with _get_conn() as conn:
        # --- Pass 1: exact account_id duplicates (original logic) ---
        dupes = conn.execute("""
            SELECT merchant, date, amount, account_id
            FROM transactions
            GROUP BY merchant, date, amount, account_id
            HAVING COUNT(*) > 1
        """).fetchall()
        for row in dupes:
            records = conn.execute("""
                SELECT transaction_id, pending FROM transactions
                WHERE merchant = ? AND date = ? AND amount = ? AND account_id = ?
                ORDER BY pending ASC
            """, (row["merchant"], row["date"], row["amount"], row["account_id"])).fetchall()
            for record in records[1:]:
                conn.execute("DELETE FROM transactions WHERE transaction_id = ?",
                             (record["transaction_id"],))
                deleted += 1

        # --- Pass 2: cross-token duplicates matched by account_mask ---
        # Only applies when account_mask is non-empty (most production cards)
        mask_dupes = conn.execute("""
            SELECT merchant, date, amount, account_mask
            FROM transactions
            WHERE account_mask != ''
            GROUP BY merchant, date, amount, account_mask
            HAVING COUNT(*) > 1
        """).fetchall()
        for row in mask_dupes:
            records = conn.execute("""
                SELECT transaction_id, pending FROM transactions
                WHERE merchant = ? AND date = ? AND amount = ? AND account_mask = ?
                ORDER BY pending ASC
            """, (row["merchant"], row["date"], row["amount"], row["account_mask"])).fetchall()
            # Keep first (posted sorts before pending), delete duplicates
            keep_id = records[0]["transaction_id"]
            for record in records[1:]:
                if record["transaction_id"] != keep_id:
                    conn.execute("DELETE FROM transactions WHERE transaction_id = ?",
                                 (record["transaction_id"],))
                    deleted += 1

    if deleted:
        print(f"[db] Deduplicated {deleted} transaction(s)")
    return deleted

def delete_transaction(transaction_id):
    """Permanently delete a single transaction from the DB by transaction ID."""
    with _get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM transactions WHERE transaction_id = ?", (transaction_id,)
        ).rowcount
    if deleted:
        print(f"[db] Deleted transaction: {transaction_id}")
    return deleted

def get_transaction_by_id(transaction_id: str) -> dict | None:
    """Return a single transaction by its ID, or None if not found."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
        ).fetchone()
    return _to_dict(row) if row else None


def find_merchant_transactions(query: str, limit: int = 300) -> list:
    """
    Fuzzy-search transactions by merchant name only.
    Splits the query into keywords and requires ALL of them to appear in
    the merchant name (case-insensitive), so "walmart plus" finds
    "Walmart+ *" but not unrelated "Walmart #4821" entries if "plus" is present.
    Falls back to a single LIKE if no multi-word split helps.
    Returns results sorted by date descending.
    """
    keywords = [kw.strip() for kw in query.lower().split() if kw.strip()]
    if not keywords:
        return []

    with _get_conn() as conn:
        # Try matching all keywords first
        if len(keywords) > 1:
            conditions = " AND ".join("LOWER(merchant) LIKE ?" for _ in keywords)
            params     = [f"%{kw}%" for kw in keywords] + [limit]
            rows = conn.execute(
                f"SELECT * FROM transactions WHERE {conditions} ORDER BY date DESC LIMIT ?",
                params,
            ).fetchall()
            if rows:
                return [_to_dict(r) for r in rows]

        # Fall back: match any keyword (broadest search)
        like = f"%{keywords[0]}%"
        rows = conn.execute(
            "SELECT * FROM transactions WHERE LOWER(merchant) LIKE ? "
            "ORDER BY date DESC LIMIT ?",
            (like, limit),
        ).fetchall()
    return [_to_dict(r) for r in rows]


def get_recent_transactions(limit=200):
    """Returns the most recent transactions for autocomplete lookups."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_to_dict(r) for r in rows]

def search_transactions(query, limit=25):
    """Full-DB search by merchant, date, or transaction ID for autocomplete."""
    like = f"%{query}%"
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM transactions
               WHERE merchant LIKE ?
                  OR date LIKE ?
                  OR transaction_id LIKE ?
               ORDER BY date DESC
               LIMIT ?""",
            (like, like, like, limit),
        ).fetchall()
    return [_to_dict(r) for r in rows]

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
    today    = datetime.today()
    start    = today.replace(day=1).strftime("%Y-%m-%d")
    last_day = calendar.monthrange(today.year, today.month)[1]
    end      = today.replace(day=last_day).strftime("%Y-%m-%d")
    return get_transactions(start_date=start, end_date=end)

def get_year_to_date_transactions():
    start = datetime.today().replace(month=1, day=1).strftime("%Y-%m-%d")
    return get_transactions(start_date=start)

def get_today_transactions():
    """Return all transactions (posted and pending) dated today."""
    today = datetime.today().strftime("%Y-%m-%d")
    return get_transactions(start_date=today, end_date=today)

def purge_old_transactions(max_days=730):
    cutoff = (datetime.today() - timedelta(days=max_days)).strftime("%Y-%m-%d")
    with _get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM transactions WHERE date < ?", (cutoff,)
        ).rowcount
    if deleted:
        print(f"[db] Purged {deleted} transactions older than {max_days} days")
    return deleted

# ── Subscriptions ─────────────────────────────────────────────────────────────

def get_merchant_avg_amount(merchant: str, limit: int = 12) -> float | None:
    """
    Return the average posted transaction amount for a merchant from the
    most recent `limit` transactions.  Used to auto-fill amount for
    variable-price subscriptions (utilities, electricity, etc.).
    Returns None if no transactions exist for this merchant.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT amount FROM transactions "
            "WHERE merchant = ? AND pending = 0 AND amount > 0 "
            "ORDER BY date DESC LIMIT ?",
            (merchant, limit),
        ).fetchall()
    if not rows:
        return None
    amounts = [r[0] for r in rows]
    return round(sum(amounts) / len(amounts), 2)


def upsert_subscriptions(streams: list):
    """
    Save detected + manual subscriptions to the DB.
    Computes next_charge_date and days_until_charge automatically.
    """
    from sheets_client import _billing_cycle   # local import to avoid circular
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for s in streams:
        freq       = s.get("frequency", "")
        next_date  = _next_charge(s.get("last_date", ""), freq)
        days_until = _days_until(next_date) if next_date else None
        rows.append({
            "merchant":          s["merchant"],
            "source":            "manual" if str(s.get("occurrence_count", "")) == "manual" else "auto",
            "frequency":         freq,
            "billing_cycle":     _billing_cycle(freq),
            "last_amount":       float(s.get("last_amount", 0)),
            "average_amount":    float(s.get("average_amount", 0)),
            "last_date":         s.get("last_date", ""),
            "first_date":        s.get("first_date", ""),
            "next_charge_date":  next_date,
            "days_until_charge": days_until,
            "category":          s.get("category", "Other"),
            "bank":              s.get("bank", ""),
            "sub_type":          s.get("sub_type") or "",
            "occurrence_count":  str(s.get("occurrence_count", "")),
            "is_active":         1 if s.get("is_active", True) else 0,
            "updated_at":        now,
            "variable_amount":   1 if s.get("variable_amount") else 0,
            "tag":               s.get("tag") or "",
            "is_estimate":       1 if s.get("is_estimate") else 0,
        })

    with _get_conn() as conn:
        conn.executemany("""
            INSERT INTO subscriptions
                (merchant, source, frequency, billing_cycle, last_amount, average_amount,
                 last_date, first_date, next_charge_date, days_until_charge,
                 category, bank, sub_type, occurrence_count, is_active, updated_at,
                 variable_amount, tag)
            VALUES
                (:merchant, :source, :frequency, :billing_cycle, :last_amount, :average_amount,
                 :last_date, :first_date, :next_charge_date, :days_until_charge,
                 :category, :bank, :sub_type, :occurrence_count, :is_active, :updated_at,
                 :variable_amount, :tag)
            ON CONFLICT(merchant) DO UPDATE SET
                source            = excluded.source,
                frequency         = excluded.frequency,
                billing_cycle     = excluded.billing_cycle,
                last_amount       = excluded.last_amount,
                average_amount    = excluded.average_amount,
                last_date         = excluded.last_date,
                first_date        = excluded.first_date,
                next_charge_date  = excluded.next_charge_date,
                days_until_charge = excluded.days_until_charge,
                category          = excluded.category,
                bank              = excluded.bank,
                sub_type          = excluded.sub_type,
                occurrence_count  = excluded.occurrence_count,
                is_active         = excluded.is_active,
                updated_at        = excluded.updated_at,
                variable_amount   = excluded.variable_amount,
                tag               = CASE WHEN excluded.tag != '' THEN excluded.tag
                                         ELSE subscriptions.tag END,
                is_estimate       = excluded.is_estimate
        """, rows)

    # ── Sync annual merchants list ────────────────────────────────────────
    # Any subscription with billing_cycle=Annual is automatically added to
    # annual_merchants.json so budget calculations prorate the charge ÷12.
    # Non-annual subscriptions are removed from the list (unless manually
    # added there independently via /annual add).
    import annual as _annual
    sub_merchants_annual    = {r["merchant"] for r in rows if r["billing_cycle"] == "Annual"}
    sub_merchants_noannual  = {r["merchant"] for r in rows if r["billing_cycle"] != "Annual"}

    added = removed = 0
    for merchant in sub_merchants_annual:
        if not _annual.is_annual(merchant):
            _annual.add_merchant(merchant)
            added += 1
    # Only remove from annual list if the subscription itself is non-annual
    # (don't touch merchants the user added manually that aren't subscriptions)
    for merchant in sub_merchants_noannual:
        if _annual.is_annual(merchant):
            _annual.remove_merchant(merchant)
            removed += 1

    if added or removed:
        print(f"[db] Annual merchants synced: +{added} added, -{removed} removed")

    print(f"[db] Upserted {len(rows)} subscription(s)")


def get_subscriptions() -> list:
    """Return all subscriptions sorted by next charge date, then amount desc."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM subscriptions
            ORDER BY
                CASE WHEN next_charge_date = '' THEN 1 ELSE 0 END,
                next_charge_date ASC,
                last_amount DESC
        """).fetchall()
    return [_sub_to_dict(r) for r in rows]


def delete_subscription(merchant: str) -> bool:
    """Remove a subscription from the DB and clean up annual merchants if needed."""
    with _get_conn() as conn:
        # Check if it was annual before deleting
        row = conn.execute(
            "SELECT billing_cycle FROM subscriptions WHERE merchant = ?", (merchant,)
        ).fetchone()
        was_annual = row and row["billing_cycle"] == "Annual"

        deleted = conn.execute(
            "DELETE FROM subscriptions WHERE merchant = ?", (merchant,)
        ).rowcount

    if deleted:
        print(f"[db] Deleted subscription: {merchant}")
        if was_annual:
            import annual as _annual
            _annual.remove_merchant(merchant)
            print(f"[db] Removed '{merchant}' from annual merchants (was Annual billing)")
    return bool(deleted)


def auto_update_subscriptions_from_transactions() -> list[dict]:
    """
    After every transaction sync, scan posted transactions to see if any match
    an existing subscription's merchant + expected billing window.  When a match
    is found the subscription's last_amount / last_date / average_amount /
    next_charge_date are updated automatically.

    Matching rules:
      - Date window  : transaction date is within ±7 days of next_charge_date
                       AND strictly after the subscription's current last_date
                       (so we never re-process the same charge)
      - Merchant     : at least one keyword from the subscription name (>3 chars)
                       appears (case-insensitive) in the transaction merchant name
      - Amount guard : for fixed-amount subs (not variable, not estimate) the
                       transaction amount must be within ±50 % of the stored amount
                       — wide enough to allow small price changes, tight enough to
                       avoid false matches

    Returns a list of dicts describing each update:
      { merchant, old_amount, new_amount, txn_date, transaction_id }
    """
    subs = get_subscriptions()
    if not subs:
        return []

    updates = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _get_conn() as conn:
        for sub in subs:
            merchant  = sub["merchant"]
            freq      = sub["frequency"]
            last_date = sub.get("last_date", "")
            next_date = sub.get("next_charge_date", "")

            if not last_date or not next_date:
                continue

            # Date search window: ±7 days around expected next charge
            try:
                next_dt = datetime.strptime(next_date, "%Y-%m-%d")
            except ValueError:
                continue
            window_start = (next_dt - timedelta(days=7)).strftime("%Y-%m-%d")
            window_end   = (next_dt + timedelta(days=7)).strftime("%Y-%m-%d")

            # Build keyword list from subscription merchant name
            # Skip short/common words ("of", "the", "and", etc.)
            raw_keywords = [
                kw.lower().strip(".,+-&")
                for kw in merchant.replace("-", " ").split()
                if len(kw) > 3
            ]
            if not raw_keywords:
                raw_keywords = [merchant.lower()]

            # SQL: merchant matches ANY keyword AND date in window AND newer than last_date
            kw_clause = " OR ".join("LOWER(merchant) LIKE ?" for _ in raw_keywords)
            kw_params = [f"%{kw}%" for kw in raw_keywords]

            rows = conn.execute(
                f"""SELECT * FROM transactions
                    WHERE ({kw_clause})
                      AND pending   = 0
                      AND amount    > 0
                      AND category != 'Excluded'
                      AND date BETWEEN ? AND ?
                      AND date      > ?
                    ORDER BY ABS(julianday(date) - julianday(?)) ASC
                    LIMIT 5""",
                kw_params + [window_start, window_end, last_date, next_date],
            ).fetchall()

            if not rows:
                continue

            # Amount guard for fixed subscriptions
            txn = None
            for row in rows:
                candidate = _to_dict(row)
                if sub.get("variable_amount") or sub.get("is_estimate"):
                    txn = candidate
                    break
                else:
                    expected = sub["last_amount"]
                    if expected > 0:
                        diff_pct = abs(candidate["amount"] - expected) / expected
                        if diff_pct <= 0.50:   # within 50 %
                            txn = candidate
                            break
                    else:
                        txn = candidate
                        break

            if not txn:
                continue

            # Compute new rolling average
            old_avg = sub.get("average_amount") or txn["amount"]
            occ     = sub.get("occurrence_count", "1")
            try:
                n = int(occ)
            except (ValueError, TypeError):
                n = 1
            new_avg  = round((old_avg * n + txn["amount"]) / (n + 1), 2)
            new_next = _next_charge(txn["date"], freq)
            new_days = _days_until(new_next) if new_next else None

            conn.execute(
                """UPDATE subscriptions SET
                       prev_amount       = last_amount,
                       last_amount       = ?,
                       average_amount    = ?,
                       last_date         = ?,
                       next_charge_date  = ?,
                       days_until_charge = ?,
                       is_estimate       = 0,
                       occurrence_count  = ?,
                       updated_at        = ?
                   WHERE merchant = ?""",
                (txn["amount"], new_avg, txn["date"],
                 new_next, new_days,
                 str(n + 1), now_str,
                 merchant),
            )

            print(f"[db] Auto-updated subscription '{merchant}': "
                  f"${sub['last_amount']} → ${txn['amount']} "
                  f"(next: {new_next}, txn: {txn['transaction_id'][:14]}...)")

            updates.append({
                "merchant":       merchant,
                "old_amount":     sub["last_amount"],
                "new_amount":     txn["amount"],
                "txn_date":       txn["date"],
                "next_charge":    new_next,
                "transaction_id": txn["transaction_id"],
                "was_estimate":   sub.get("is_estimate", False),
            })

    return updates


def refresh_next_charge_dates():
    """Recompute next_charge_date and days_until for every subscription (run daily)."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT merchant, last_date, frequency FROM subscriptions").fetchall()
        for row in rows:
            next_date  = _next_charge(row["last_date"], row["frequency"])
            days_until = _days_until(next_date) if next_date else None
            conn.execute(
                "UPDATE subscriptions SET next_charge_date=?, days_until_charge=? WHERE merchant=?",
                (next_date, days_until, row["merchant"]),
            )
    print(f"[db] Refreshed next charge dates for {len(rows)} subscription(s)")


def _sub_to_dict(row) -> dict:
    return {
        "merchant":          row["merchant"],
        "source":            row["source"],
        "frequency":         row["frequency"],
        "billing_cycle":     row["billing_cycle"],
        "last_amount":       row["last_amount"],
        "average_amount":    row["average_amount"],
        "last_date":         row["last_date"],
        "first_date":        row["first_date"],
        "next_charge_date":  row["next_charge_date"],
        "days_until_charge": row["days_until_charge"],
        "category":          row["category"],
        "bank":              row["bank"],
        "sub_type":          row["sub_type"],
        "occurrence_count":  row["occurrence_count"],
        "is_active":         bool(row["is_active"]),
        "updated_at":        row["updated_at"],
        "variable_amount":   bool(row["variable_amount"]),
        "tag":               row["tag"] or "",
        "is_estimate":       bool(row["is_estimate"]),
        "prev_amount":       row["prev_amount"],
        # Fields expected by sync_subscriptions / Discord command
        "stream_type":       "expense",
        "status":            "Active",
    }


# ── Merchant categories ───────────────────────────────────────────────────────

def get_all_merchant_categories() -> dict[str, str]:
    """Return {merchant: category} for every confirmed (non-uncertain) entry."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT merchant, category FROM merchant_categories WHERE is_uncertain = 0"
        ).fetchall()
    return {r["merchant"]: r["category"] for r in rows}

def get_uncertain_merchants() -> dict:
    """
    Return uncertain merchants as:
      {merchant: {merchant_id, category, confidence, sample: {amount, date, bank}}}
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM merchant_categories WHERE is_uncertain = 1"
        ).fetchall()
    result = {}
    for r in rows:
        result[r["merchant"]] = {
            "merchant_id": r["merchant_id"],
            "category":    r["category"],
            "confidence":  r["confidence"],
            "sample": {
                "amount": r["sample_amount"],
                "date":   r["sample_date"],
                "bank":   r["sample_bank"],
            },
        }
    return result

def save_merchant_category(merchant: str, category: str, source: str = "gemini",
                            confidence: float = 1.0, is_uncertain: bool = False,
                            sample: dict | None = None):
    """Insert or update a merchant's category. Generates a stable merchant_id on first insert."""
    import uuid as _uuid
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sample = sample or {}
    mid    = str(_uuid.uuid4())   # only used if this is a brand-new row
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO merchant_categories
                (merchant, merchant_id, category, source, confidence, is_uncertain,
                 sample_amount, sample_date, sample_bank, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(merchant) DO UPDATE SET
                category      = excluded.category,
                source        = excluded.source,
                confidence    = excluded.confidence,
                is_uncertain  = excluded.is_uncertain,
                sample_amount = COALESCE(excluded.sample_amount, merchant_categories.sample_amount),
                sample_date   = COALESCE(excluded.sample_date,   merchant_categories.sample_date),
                sample_bank   = COALESCE(excluded.sample_bank,   merchant_categories.sample_bank),
                confirmed_at  = excluded.confirmed_at
                -- merchant_id is intentionally NOT updated on conflict (stays stable forever)
        """, (
            merchant, mid, category, source, confidence, int(is_uncertain),
            sample.get("amount"), sample.get("date"), sample.get("bank"),
            now if not is_uncertain else None,
        ))

def confirm_merchant_category(merchant: str, category: str):
    """
    Mark a merchant as user-confirmed: update category, clear uncertain flag,
    set confirmed_at timestamp.  Generates a merchant_id if this is a new row.
    """
    import uuid as _uuid
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mid = str(_uuid.uuid4())
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO merchant_categories
                (merchant, merchant_id, category, source, confidence, is_uncertain, confirmed_at)
            VALUES (?, ?, ?, 'user', 1.0, 0, ?)
            ON CONFLICT(merchant) DO UPDATE SET
                category     = excluded.category,
                source       = 'user',
                confidence   = 1.0,
                is_uncertain = 0,
                confirmed_at = excluded.confirmed_at
                -- merchant_id preserved on conflict
        """, (merchant, mid, category, now))
    print(f"[db] Confirmed category: '{merchant}' → '{category}'")


def rename_subscription(old_merchant: str, new_merchant: str) -> bool:
    """
    Rename a subscription merchant in both the subscriptions and merchant_categories
    tables.  The merchant_id in merchant_categories stays unchanged — it is the
    stable identifier that survives name changes.
    Returns True if the subscription was found and renamed, False otherwise.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT merchant FROM subscriptions WHERE LOWER(merchant) = LOWER(?)",
            (old_merchant,)
        ).fetchone()
        if not row:
            return False
        actual_old = row["merchant"]

        conn.execute(
            "UPDATE subscriptions SET merchant = ? WHERE merchant = ?",
            (new_merchant, actual_old),
        )
        conn.execute(
            "UPDATE merchant_categories SET merchant = ? WHERE LOWER(merchant) = LOWER(?)",
            (new_merchant, actual_old),
        )
    print(f"[db] Renamed subscription: '{actual_old}' → '{new_merchant}'")
    return True


def _to_dict(row):
    return {
        "transaction_id":  row["transaction_id"],
        "bank":            row["bank"],
        "date":            row["date"],
        "merchant":        row["merchant"],
        "category":        row["category"],
        "amount":          row["amount"],
        "pending":         bool(row["pending"]),
        "account_id":      row["account_id"],
        "payment_channel": row["payment_channel"],
        "website":         row["website"],
        "location":        row["location"],
        "plaid_category":  row["plaid_category"],
        "account_mask":    row["account_mask"],
    }
