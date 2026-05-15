import os
from dotenv import load_dotenv

load_dotenv()

# Plaid
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")

def _load_plaid_tokens():
    """Auto-discovers any PLAID_ACCESS_TOKEN_<BANK> entries in .env.
    The suffix becomes the bank name (underscores → spaces, title-cased).
    Example: PLAID_ACCESS_TOKEN_WELLS_FARGO → 'Wells Fargo'
    """
    tokens = {}
    prefix = "PLAID_ACCESS_TOKEN_"
    for key, val in os.environ.items():
        if key.startswith(prefix) and val:
            bank_name = key[len(prefix):].replace("_", " ").title()
            tokens[bank_name] = val
    return tokens

PLAID_ACCESS_TOKENS = _load_plaid_tokens()

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Discord
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
def _safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

DISCORD_CHANNEL_ID        = _safe_int(os.getenv("DISCORD_CHANNEL_ID"))
DISCORD_ALERT_CHANNEL_ID  = _safe_int(os.getenv("DISCORD_ALERT_CHANNEL_ID"))
DISCORD_REVIEW_CHANNEL_ID = _safe_int(os.getenv("DISCORD_REVIEW_CHANNEL_ID"))
DISCORD_GUILD_ID          = _safe_int(os.getenv("DISCORD_GUILD_ID"))

# Timezone — change to match your local timezone
# Common US options: America/New_York, America/Chicago, America/Denver, America/Los_Angeles
TIMEZONE = "America/New_York"

# Sync schedule — twice daily: 9am and 9pm pulls
SYNC_HOURS = [9, 21]

# Daily digest schedule
DIGEST_MORNING_HOUR   = 9    # 9:00 AM — budget overview for the day
DIGEST_EVENING_HOUR   = 23   # 11:59 PM — end-of-day spending recap
DIGEST_EVENING_MINUTE = 59

# Subscription reminder — hour to send the daily upcoming-charge alert (9am)
SUBSCRIPTION_REMINDER_HOUR = 9

# How many days back to pull transactions on each sync
TRANSACTION_LOOKBACK_DAYS = 132  # from Jan 1 2026

# Plaid webhook server
WEBHOOK_PORT = _safe_int(os.getenv("WEBHOOK_PORT"), 3000)
PLAID_WEBHOOK_SECRET = os.getenv("PLAID_WEBHOOK_SECRET", "")

# Pending transactions: show in Sheet but exclude from budget calculations and alerts
COUNT_PENDING = False

# Budget limits by category (monthly, in USD)
# Edit these to match your actual spending limits
BUDGET_LIMITS = {
    "Food and Drink":       600,
    "Groceries":            500,
    "Travel":               800,
    "Entertainment":        300,
    "Shopping":             300,
    "Health and Fitness":   250,
    "Gas":                  150,
    "Necessities":          1250,
    "Other":                500,
}

# Large transaction alerts (Option C: global threshold + per-category overrides)
# A transaction triggers an alert if it exceeds the category override (if set),
# otherwise falls back to the global threshold. Pending transactions are excluded.
LARGE_TRANSACTION_GLOBAL_THRESHOLD = 100  # USD — catches anything not covered below

LARGE_TRANSACTION_BY_CATEGORY = {
    "Food and Drink":       75,
    "Groceries":            150,
    "Travel":               300,
    "Entertainment":        100,
    "Shopping":             150,
    "Health and Fitness":   100,
    "Gas":                  80,
    "Necessities":            200,
    "Other":                100,
}
