# BudgetTrackerAI

A self-hosted Discord bot that connects to your bank accounts via Plaid, categorizes every transaction with Gemini AI, syncs everything to a Google Sheet, and delivers daily budget digests — all controlled through Discord slash commands and a natural language AI assistant.

---

## Features

- **Multi-bank sync** — pulls transactions from any bank via Plaid (Chase, Citi, Capital One, etc.)
- **AI categorization** — Gemini 2.5 Flash categorizes every merchant automatically with confidence scoring; uncertain ones are sent to a review channel for manual approval
- **Natural language bot** — @mention the bot in any message to add subscriptions, ask budget questions, find transactions, recategorize merchants, and more
- **Google Sheets sync** — one tab per bank + a Summary tab + a Subscriptions tab, updated on every sync
- **Subscription tracking** — auto-detects recurring charges, supports variable-amount bills, manual entries, tags, and estimate amounts that auto-update when the real charge arrives
- **Subscription review** — newly detected subscriptions are staged in a single interactive dropdown for you to approve before saving
- **Twice-daily digest** — 9 AM budget overview and 11:59 PM day-recap with a Gemini-written narrative
- **Budget alerts** — fires when you exceed a spending category limit
- **Large transaction alerts** — global and per-category dollar thresholds
- **Scheduled auto-sync** — pulls transactions at 9 AM and 9 PM daily
- **Plaid webhook support** — real-time transaction alerts when a new charge posts

---

## Architecture

```
Plaid API  ──────────────────────────────────────────────────────
  plaid_client.py      fetch transactions + item health checks
        │
  gemini_client.py     AI categorization (cached in SQLite)
        │
  budget_engine.py     overage detection, large-txn alerts
        │
  sheets_client.py     write to Google Sheets
        │
  discord_bot.py       slash commands, NL bot, scheduled tasks
        │
  db.py                SQLite — transactions, subscriptions,
                                merchant_categories
        │
  subscription_detector.py   detect recurring charges from history
  subscription_store.py      manual subscription overrides (JSON)
  webhook_server.py          Plaid webhook receiver (optional)
```

---

## Prerequisites

- **Python 3.11+**
- A [Plaid](https://dashboard.plaid.com) account (free sandbox, ~$0.30/account/month in production)
- A [Google Cloud](https://console.cloud.google.com) service account with Sheets + Drive APIs enabled
- A [Google AI Studio](https://aistudio.google.com) Gemini API key (free tier: 1,500 requests/day)
- A [Discord](https://discord.com/developers/applications) application + bot token

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/justinchai22/BudgetTrackerAI.git
cd BudgetTrackerAI
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Then fill in every value — the sections below explain where to get each one.

---

### 4. Plaid — bank connectivity

1. Create a free account at [dashboard.plaid.com](https://dashboard.plaid.com)
2. Go to **Team Settings → Keys** and copy your **Client ID** and **Sandbox Secret**
3. Add them to `.env`:
   ```
   PLAID_CLIENT_ID=your_client_id
   PLAID_SECRET=your_sandbox_secret
   PLAID_ENV=sandbox
   ```
4. Run the link script **once per bank**:
   ```bash
   python plaid_setup.py
   ```
   - A browser window opens with Plaid Link
   - In sandbox, search for **First Platypus Bank** and log in with:
     - Username: `user_good` · Password: `pass_good` · MFA: `1234`
   - The terminal prints an access token — add it to `.env` as:
     ```
     PLAID_ACCESS_TOKEN_CHASE=access-sandbox-...
     PLAID_ACCESS_TOKEN_CITI=access-sandbox-...
     ```
   - The suffix after `PLAID_ACCESS_TOKEN_` becomes the bank name (underscores → spaces, title-cased)
   - Repeat for every bank you want to connect

> **Going to production:** Change `PLAID_ENV=production` in `.env` and re-run `plaid_setup.py` with real bank credentials. Plaid requires a short security review before granting production access.

---

### 5. Google Sheets — spreadsheet output

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create (or select) a project
2. Enable **Google Sheets API** and **Google Drive API** under _APIs & Services → Library_
3. Go to **IAM & Admin → Service Accounts** → **Create Service Account**
   - Grant it the **Editor** role
4. Open the service account → **Keys** tab → **Add Key → JSON** → download the file
5. Rename it `service_account.json` and place it in the project folder
6. Create a new Google Sheet and **share it** with the service account's email address (Editor access)
7. Copy the Sheet ID from the URL (`docs.google.com/spreadsheets/d/SHEET_ID/edit`) into `.env`:
   ```
   GOOGLE_SHEET_ID=your_sheet_id
   GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
   ```

---

### 6. Gemini AI — categorization & summaries

1. Go to [aistudio.google.com](https://aistudio.google.com) → **Get API Key → Create API key**
2. Add it to `.env`:
   ```
   GEMINI_API_KEY=your_api_key
   ```

> **Important:** Use the AI Studio key (free tier), **not** a Google Cloud key with billing enabled. The AI Studio free tier covers all normal usage of this bot.

Free tier limits: 1,500 requests/day · 1M tokens/minute · 15 requests/minute — well above what the bot needs.

---

### 7. Discord bot — commands & alerts

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. Go to the **Bot** tab:
   - Click **Reset Token** and copy it into `.env` as `DISCORD_BOT_TOKEN`
   - Under **Privileged Gateway Intents**, enable **Server Members Intent** and **Message Content Intent**
3. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Bot Permissions: `Send Messages`, `View Channels`, `Use Slash Commands`, `Embed Links`, `Add Reactions`
4. Open the generated URL and invite the bot to your server

5. Create **three channels** in your server:
   | Channel | Purpose |
   |---------|---------|
   | `#budget-tracker` | Daily digest, sync results, budget alerts, large transaction alerts |
   | `#budget-alerts` | Bank connection issues, subscription auto-updates (can be same as above) |
   | `#budget-review` | Uncertain category prompts + subscription review dropdowns |

6. Enable **Developer Mode** in Discord: _User Settings → Advanced → Developer Mode_
7. Right-click each channel → **Copy Channel ID** → add to `.env`:
   ```
   DISCORD_CHANNEL_ID=111111111111111111        # #budget-tracker
   DISCORD_ALERT_CHANNEL_ID=222222222222222222  # #budget-alerts
   DISCORD_REVIEW_CHANNEL_ID=333333333333333333 # #budget-review
   ```
8. Right-click your server name → **Copy Server ID** → add to `.env`:
   ```
   DISCORD_GUILD_ID=444444444444444444
   ```

---

### 8. Configure budget limits

Open `config.py` and set your monthly spending limits and alert thresholds to match your actual budget:

```python
BUDGET_LIMITS = {
    "Food and Drink":     600,
    "Groceries":          500,
    "Travel":             800,
    "Entertainment":      300,
    "Shopping":           300,
    "Health and Fitness": 250,
    "Gas":                150,
    "Necessities":        1250,   # utilities, rent, etc.
    "Other":              500,
}

LARGE_TRANSACTION_GLOBAL_THRESHOLD = 100   # fires if no category override matches

LARGE_TRANSACTION_BY_CATEGORY = {
    "Food and Drink":     75,
    "Groceries":          150,
    "Travel":             300,
    "Entertainment":      100,
    "Shopping":           150,
    "Health and Fitness": 100,
    "Gas":                80,
    "Necessities":        200,
    "Other":              100,
}
```

You can also adjust the timezone and sync schedule:

```python
TIMEZONE   = "America/Chicago"   # change to your local timezone
SYNC_HOURS = [9, 21]             # 9 AM and 9 PM daily pulls
```

---

### 9. Run the bot

```bash
python main.py
```

On first startup the bot will:
- Initialize the SQLite database (`transactions.db`)
- Sync slash commands to your server
- Post a startup message to `#budget-tracker`
- Begin all scheduled tasks

---

## Discord Commands

### Slash Commands

| Command | Description |
|---------|-------------|
| `/sync` | Manually trigger a full transaction sync from all banks |
| `/budget` | Show current month's spending vs limits for every category |
| `/subscriptions` | List all tracked recurring charges with monthly cost estimates |
| `/add_subscription` | Manually add a subscription (merchant, frequency, amount, tag) |
| `/remove_subscription` | Remove a tracked subscription (with optional undo) |
| `/recategorize` | Change a merchant's category permanently via a dropdown |
| `/ask` | Ask Gemini a free-form question about your finances |
| `/delete_transaction` | Delete one or more transactions by ID |
| `/status` | Check the connection health of all linked bank accounts |
| `/add_account` | Instructions for linking a new bank or credit card |
| `/reauth` | Re-authenticate an expired bank connection |
| `/annual` | Mark/unmark a merchant as an annual charge (prorates cost ÷ 12) |
| `/set_webhook` | Set your public URL for Plaid real-time webhooks |

### Natural Language Bot

Mention the bot in any message (`@BudgetBot ...`) and it will understand plain English:

```
@BudgetBot add Netflix monthly $15.99
@BudgetBot tag my Spotify subscription as "Justin only"
@BudgetBot how much did I spend on food this month?
@BudgetBot find my Walmart Plus subscription and add it
@BudgetBot recategorize Target as Shopping
@BudgetBot remove OpenAI from subscriptions
@BudgetBot update my City of Plano subscription using transaction abc123, tag: Water & Trash
@BudgetBot what's my biggest expense category this year?
```

The bot keeps the last 5 exchanges of conversation context per channel, so follow-ups work naturally:
```
@BudgetBot show my subscriptions
@BudgetBot remove it    ← "it" resolves from context
```

---

## Subscription Workflow

### Auto-detection
Every sync runs a pattern detector over your transaction history. Recurring charges (same merchant, consistent interval, consistent amount) are identified as subscriptions.

**New merchants** are never auto-saved — instead, a single review message appears in `#budget-review` with a dropdown showing all detected candidates. All are pre-selected; deselect any that aren't real subscriptions, then click **Save Selected**.

**Already-tracked** subscriptions are updated silently on every sync (refreshed amounts and dates).

### Variable-amount subscriptions
Bills like electricity or water that vary each month can be tracked without a fixed price. The bot stores an average and updates it automatically when each real charge arrives.

### Estimate amounts
If you don't know the exact price yet, add the subscription with a rough estimate. When the actual charge posts, the bot auto-updates the subscription and clears the estimate flag — no action needed from you.

### Tags
Any subscription can have a short personal label:
```
@BudgetBot tag Netflix as "Family plan"
```
Tags appear in `/subscriptions` and the Google Sheet.

### Previous charge indicator
`/subscriptions` shows an ↑/↓ trend arrow when the most recent charge differs from the prior one:
```
Netflix — $17.99 ↑ (was $15.99) · Monthly
```

---

## Category Review Workflow

When Gemini is less than 75% confident about a merchant's category, it flags it for review instead of auto-saving. After a sync, a single paginated review message appears in `#budget-review`:

```
❓ Categorize Transactions — 1 of 6  (0 done, 6 remaining)
> Merchant: Target
> Amount: $43.21 · Date: 2026-05-10 · Bank: Chase
> Gemini's best guess: Shopping  (72% confident)

Pick the correct category — selecting one auto-advances to the next:
[ Shopping ▼ ]   [ ◀ Prev ]  [ Next ▶ ]  [ ⏭️ Skip ]  [ ✅ Done ]
```

Selecting a category saves it permanently and advances to the next merchant automatically. All categorizations are stored in the `merchant_categories` SQLite table — the same merchant is never asked about twice.

---

## Google Sheet Structure

| Tab | Contents |
|-----|----------|
| **[Bank Name]** | All transactions for that bank — Date, Merchant, Category, Amount, Status, Tag, Custom Category |
| **Summary** | Monthly totals per category vs budget limits, YTD chart, per-bank date ranges |
| **Subscriptions** | All tracked recurring charges — merchant, tag, frequency, amount, next charge date, category, bank |

### Overriding a category via Sheets
Fill in the **Custom Category** column on any transaction row. The next sync will:
1. Apply the new category to that merchant permanently
2. Rewrite all past and future transactions for that merchant
3. Clear the Custom Category cell

Valid categories: `Food and Drink`, `Groceries`, `Travel`, `Entertainment`, `Shopping`, `Health and Fitness`, `Gas`, `Necessities`, `Other`, `Excluded`

> Setting a category to `Excluded` hides the merchant from all budget calculations and alerts.

---

## Scheduled Tasks

| Time | Task |
|------|------|
| 9:00 AM | Full sync (Plaid → DB → Sheets) + budget/large-txn alerts + subscription charge reminders |
| 9:00 PM | Full sync (same as above) |
| 9:00 AM | Morning budget digest — month overview, upcoming subscription charges |
| 11:59 PM | Evening recap — today's transactions listed individually, per-category totals, Gemini narrative |

All times use the `TIMEZONE` set in `config.py` (default: `America/New_York`).

---

## Cost Breakdown

### Plaid
| Tier | Cost |
|------|------|
| Sandbox | Free (test data only) |
| Production | ~$0.30 per connected account / month |

Connecting 3 banks with 5 cards total ≈ **$1.50/month**.

### Gemini AI (Google AI Studio — free tier)
| Usage | Cost |
|-------|------|
| Per merchant categorization | $0 (cached after first categorization) |
| Daily digests + alerts | $0 |
| Monthly total | **$0** on the free tier |

### Google Sheets API
Free within standard quota (well above what this bot uses).

### Discord Bot
Free.

**Estimated total: ~$1.50/month** (just Plaid production accounts).

---

## Troubleshooting

**`PrivilegedIntentsRequired` on startup**
Go to [discord.com/developers/applications](https://discord.com/developers/applications) → your app → **Bot** → enable **Server Members Intent** and **Message Content Intent**.

**Slash commands not appearing in Discord**
Ensure the bot was invited with the `applications.commands` scope and that `DISCORD_GUILD_ID` is set in `.env`. Commands sync to your specific server on startup.

**`Unknown interaction` error in logs**
A slash command timed out before responding. All slow commands use `defer()` so this shouldn't happen — if it does, check that your machine isn't under heavy load when the command runs.

**Plaid `INVALID_ACCESS_TOKEN` error**
Your Plaid access token has expired or the bank connection needs re-authentication. Run `/reauth` in Discord or re-run `python plaid_setup.py` for that bank.

**Google Sheets 404 / permission error**
Either `GOOGLE_SHEET_ID` is wrong, or the sheet hasn't been shared with the service account email (found in `service_account.json` under `"client_email"`).

**Gemini `API_KEY_INVALID` error**
Your `GEMINI_API_KEY` is missing or invalid. Make sure you're using an **AI Studio** key from [aistudio.google.com](https://aistudio.google.com), not a Google Cloud key.

**Bot is online but nothing appears in channels**
Check that the bot has `Send Messages` and `View Channel` permissions in each specific channel, and that all three channel IDs in `.env` are correct.

**Subscriptions not being detected**
Plaid's recurring detection needs a history of at least 2–3 charges. New subscriptions or newly linked accounts may not show up immediately. Use `/add_subscription` or ask the NL bot to add them manually.

---

## Security Notes

- **Never commit `.env` or `service_account.json`** — both are in `.gitignore`
- **Plaid access tokens** grant read-only access to transaction history — they cannot initiate payments or transfers
- **Gemini API key** — use the AI Studio key on the free tier to avoid any billing exposure
- **SQLite database** (`transactions.db`) contains your full transaction history — keep it local and backed up
- **Sandbox mode** during development — no real financial data is accessed until you switch `PLAID_ENV=production`

---

## Setting Up With Claude AI

If you get stuck at any point, you can paste the following prompt into [Claude.ai](https://claude.ai) and it will walk you through the setup with full context about this project:

---

> I'm setting up **BudgetTrackerAI**, a self-hosted Discord bot that connects to bank accounts via Plaid, categorizes transactions with Gemini AI, syncs everything to a Google Sheet, and delivers daily budget digests.
>
> **Tech stack:**
> - Python 3.11+
> - Plaid API (bank transactions)
> - Google Gemini 2.5 Flash via AI Studio (free tier) — categorization + NL commands
> - Google Sheets API + service account (gspread)
> - discord.py 2.x — slash commands + interactive UI components (buttons, dropdowns)
> - SQLite — local DB for transactions, subscriptions, merchant categories
>
> **Key files:**
> - `main.py` — entry point
> - `discord_bot.py` — all slash commands, NL bot, scheduled tasks, UI views
> - `gemini_client.py` — Gemini API calls, categorization logic
> - `db.py` — SQLite schema + all DB functions
> - `plaid_client.py` — Plaid transaction fetching
> - `sheets_client.py` — Google Sheets sync
> - `subscription_detector.py` — recurring charge detection
> - `config.py` — budget limits, schedule, timezone (edit this to customize)
> - `.env` — all secrets (Plaid keys, Gemini key, Discord token, channel IDs)
>
> **Environment variables needed (in `.env`):**
> `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV`,
> `PLAID_ACCESS_TOKEN_<BANKNAME>` (one per bank, generated by `python plaid_setup.py`),
> `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID`,
> `GEMINI_API_KEY`,
> `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_ALERT_CHANNEL_ID`, `DISCORD_REVIEW_CHANNEL_ID`, `DISCORD_GUILD_ID`
>
> **Three Discord channels required:**
> `#budget-tracker` (digest + sync results), `#budget-alerts` (bank issues), `#budget-review` (subscription + category review dropdowns)
>
> **I need help with:** [describe your specific issue here — e.g. "setting up the Google service account", "Plaid Link isn't opening", "the bot isn't showing slash commands", "a Python error I'm getting", etc.]

---

Claude can help you debug errors, walk through any of the setup steps, explain how the code works, or customize the bot for your own needs (different budget categories, extra banks, etc.).

---

## Built With

- [Plaid](https://plaid.com) — bank connectivity and transaction data
- [Google Gemini 2.5 Flash](https://aistudio.google.com) — AI categorization, summaries, and NL commands
- [discord.py 2.x](https://discordpy.readthedocs.io) — Discord bot framework with slash commands and UI components
- [gspread](https://github.com/burnash/gspread) — Google Sheets integration
- [python-dotenv](https://github.com/theskumar/python-dotenv) — environment variable management
- [SQLite](https://sqlite.org) — local database for transactions, subscriptions, and merchant categories
