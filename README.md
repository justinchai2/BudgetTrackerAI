# BudgetTrackerAI

A Discord bot that connects to your Chase, Citi, and Capital One accounts via Plaid, consolidates all transactions into a Google Sheet, and uses Gemini AI to categorize spending and deliver daily budget digests and alerts.

---

## Features

- **Multi-bank sync** — pulls transactions from Chase, Citi, and Capital One via Plaid
- **AI categorization** — Gemini 2.5 Flash categorizes every merchant automatically, with confidence scoring
- **Google Sheets** — consolidated view with a tab per bank, a summary tab, and a subscriptions tab
- **Daily digest** — 10am Discord message with spending summary and Gemini-written insights
- **Budget alerts** — fires when you exceed a spending category limit
- **Large transaction alerts** — global and per-category dollar thresholds
- **Recurring transactions** — detects subscriptions and recurring charges via Plaid
- **Pending transactions** — shown in the Sheet with a Pending label, excluded from budget calculations
- **Twice-daily sync** — automatic pulls at 9am and 9pm
- **Recategorization** — change any merchant's category via `/recategorize` or by filling in the Custom Category column in Sheets
- **User review prompts** — uncertain categorizations are sent to a dedicated Discord channel for manual review

---

## Architecture

```
Plaid API (Chase / Citi / Capital One)
        ↓
  plaid_client.py  ─── fetch transactions + recurring streams
        ↓
 gemini_client.py  ─── AI categorization (cached by merchant)
        ↓
  budget_engine.py ─── overage detection, large txn alerts
        ↓
  sheets_client.py ─── write to Google Sheets
        ↓
  discord_bot.py   ─── commands, scheduled tasks, alerts
```

---

## Prerequisites

- Python 3.9+
- A [Plaid](https://dashboard.plaid.com) account (free sandbox tier)
- A [Google Cloud](https://console.cloud.google.com) project with Sheets and Drive APIs enabled
- A [Google AI Studio](https://aistudio.google.com) Gemini API key (free)
- A [Discord](https://discord.com/developers/applications) application and bot token

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/justinchai2/BudgetTrackerAI.git
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

Fill in all values in `.env` — see the API Keys section below for where to get each one.

### 4. Set up Plaid

1. Create a free account at [dashboard.plaid.com](https://dashboard.plaid.com)
2. Go to **Team Settings → Keys** and copy your **Client ID** and **Sandbox Secret**
3. Enable the **Transactions** product (covers both regular and recurring transactions)
4. Run the setup script once per bank:

```bash
python plaid_setup.py
```

- A browser window will open with Plaid Link
- In sandbox, search for **First Platypus Bank** and use:
  - Username: `user_good`
  - Password: `pass_good`
  - MFA code: `1234`
- The terminal will print an access token — paste it into `.env`
- Repeat for all three banks (Chase, Citi, Capital One)

> **Switching to production:** Change `PLAID_ENV=production` in `.env` and re-run `plaid_setup.py` with real bank credentials. Plaid requires a security review before granting production access.

### 5. Set up Google Sheets

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project
2. Enable **Google Sheets API** and **Google Drive API**
3. Go to **IAM & Admin → Service Accounts** → Create a service account with **Editor** role
4. Under the service account's **Keys** tab → **Add Key → JSON** → download the file
5. Rename it `service_account.json` and place it in the project folder
6. Create a new Google Sheet and share it with the service account email (Editor access)
7. Copy the Sheet ID from the URL (`/d/SHEET_ID/edit`) into `.env`

### 6. Get a Gemini API key

1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Click **Get API Key** → **Create API key**
3. Paste it into `.env` as `GEMINI_API_KEY`

### 7. Set up the Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → name it `BudgetTrackerAI`
3. Go to **Bot** tab → **Reset Token** → copy it into `.env` as `DISCORD_BOT_TOKEN`
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Permissions: `Send Messages`, `View Channels`, `Use Slash Commands`, `Embed Links`
6. Open the generated URL and invite the bot to your server
7. Create two channels in your server:
   - `#budget-tracker` — daily digest + alerts
   - `#budget-review` — uncertain merchant categorization prompts
8. Right-click each channel → **Copy Channel ID** → paste into `.env`
9. Right-click your server name → **Copy Server ID** → paste as `DISCORD_GUILD_ID`

> **Enable Developer Mode in Discord:** Settings → Advanced → Developer Mode

### 8. Configure your budget limits

Edit `config.py` and set your monthly spending limits:

```python
BUDGET_LIMITS = {
    "Food and Drink":    600,
    "Groceries":         400,
    "Travel":            500,
    "Entertainment":     200,
    "Shopping":          300,
    "Health and Fitness": 150,
    "Gas":               200,
    "Utilities":         250,
    "Other":             300,
}
```

Also set your large transaction thresholds:

```python
LARGE_TRANSACTION_GLOBAL_THRESHOLD = 100  # fires if no category override matches

LARGE_TRANSACTION_BY_CATEGORY = {
    "Travel":   300,
    "Groceries": 150,
    "Gas":        80,
    # ...
}
```

### 9. Run the bot

```bash
python main.py
```

The bot will:
- Sync slash commands to your server instantly on startup
- Post a startup message to `#budget-tracker`
- Begin the scheduled sync and digest tasks

---

## Discord Commands

| Command | Description |
|---------|-------------|
| `/sync` | Manually trigger a full transaction sync from all banks |
| `/budget` | Show current month's spending vs limits for every category |
| `/subscriptions` | List all recurring charges with estimated monthly costs |
| `/recategorize` | Change a merchant's category (autocomplete search) |
| `/add_account` | Instructions for linking a new credit card |

---

## Google Sheet Structure

| Tab | Contents |
|-----|----------|
| **Chase** | All Chase transactions — Date, Merchant, Category, Amount, Status, Custom Category |
| **Citi** | All Citi transactions |
| **Capital One** | All Capital One transactions |
| **Summary** | Monthly totals per category vs budget limits |
| **Subscriptions** | Recurring charges and income streams from all banks |

### Recategorizing via Sheets

Fill in the **Custom Category** column (column H) on any transaction row with a valid category name. The next sync will pick it up, update the merchant cache permanently, and rewrite all matching transactions.

Valid categories:
`Food and Drink`, `Groceries`, `Travel`, `Entertainment`, `Shopping`, `Health and Fitness`, `Gas`, `Utilities`, `Other`

---

## Scheduled Tasks

| Time | Action |
|------|--------|
| 9:00 AM | Sync transactions from all banks → update Sheet → fire any alerts |
| 9:00 PM | Same as 9am sync |
| 10:00 AM | Post daily budget digest to `#budget-tracker` with Gemini summary |

Times use the timezone set in `config.py` (`TIMEZONE = "America/New_York"` by default).

---

## Cost Breakdown

### Plaid
| Tier | Cost |
|------|------|
| Sandbox | Free (unlimited, fake data) |
| Production | $0.30 per connected account/month |

With 10 credit cards across 3 banks, expect **~$3.00/month**.

> You choose which accounts to connect during Plaid Link — deselect accounts you don't need to minimize cost.

### Gemini AI
| Usage | Estimated Cost |
|-------|---------------|
| Per merchant categorization | ~$0.0001 |
| Daily digest + alerts | ~$0.001/day |
| Monthly total (typical) | **< $0.10/month** |

Gemini caches results by merchant name — the same merchant is never re-categorized, keeping API calls minimal.

### Google Sheets API
Free within standard quota (read/write limits well above what this bot uses).

### Discord Bot
Free.

**Total estimated monthly cost in production: ~$3.10/month**

---

## Limitations

### Plaid
- **Sandbox data is fake** — transactions, merchants, and amounts are all test data. Switch to `PLAID_ENV=production` when ready to use real bank data.
- **Transaction history** — Plaid provides up to 24 months of history depending on the bank. Not all banks guarantee the full 2 years.
- **Pending transactions** — shown in the Sheet but excluded from budget calculations and alerts. Pending amounts may change before they post.
- **Recurring detection** — Plaid's recurring transaction detection requires a history of charges. Newly opened accounts or new subscriptions may not be detected immediately.
- **Bank connectivity** — Plaid connections can occasionally expire and require re-authentication. Run `python plaid_setup.py` again for the affected bank if this happens.
- **New credit cards** — adding a new card requires re-running `python plaid_setup.py` for that bank, or using the `/add_account` command.

### Gemini AI
- **Categorization accuracy** — Gemini uses the merchant name and Plaid's raw category hint. Ambiguous or generic merchant names (e.g. `FUN`, `ACH PAYMENT`) may be categorized incorrectly. These are flagged in `#budget-review` for manual review.
- **Not a financial advisor** — AI-generated budget summaries and insights are for informational purposes only.

### Google Sheets
- **Overwrites on every sync** — the Sheet is fully rewritten each sync. Any manual edits outside the Custom Category column will be lost.
- **Custom Category column** — only fill this in when you want to permanently override a merchant's category. It is cleared after each sync once the override is applied.

### Discord Bot
- **Must be running** — the bot needs to be running on your machine for scheduled syncs and alerts to fire. If your computer is off at 9am, that sync will be skipped.
- **Slash command propagation** — commands are synced guild-specifically on startup for instant availability. Global sync can take up to 1 hour.

---

## Troubleshooting

**Bot not showing slash commands**
Make sure the bot has been invited with `applications.commands` scope and that `DISCORD_GUILD_ID` is set correctly in `.env`.

**Plaid `client_id` error**
Your `PLAID_CLIENT_ID` or `PLAID_SECRET` in `.env` is missing or still a placeholder. Get them from [dashboard.plaid.com](https://dashboard.plaid.com) → Team Settings → Keys.

**Google Sheets 404 error**
Your `GOOGLE_SHEET_ID` is wrong, or the sheet hasn't been shared with the service account email.

**Gemini `API key not valid` error**
Your `GEMINI_API_KEY` is missing or invalid. Get a free key at [aistudio.google.com](https://aistudio.google.com).

**Bot is online but channels show nothing**
Ensure the bot has `Send Messages` and `View Channel` permissions in those specific channels, and that the channel IDs in `.env` are correct.

---

## Security Notes

- Never commit your `.env` file or `service_account.json` — both are in `.gitignore`
- Your Plaid access tokens grant read-only access to transaction data — they cannot move money
- The `merchant_cache.json`, `alerted_ids.json`, and `uncertain_merchants.json` files are local runtime state and are also excluded from version control
- Run in sandbox mode during development — no real financial data is ever accessed until you switch to production

---

## Built With

- [Plaid](https://plaid.com) — bank connectivity
- [Google Gemini 2.5 Flash](https://aistudio.google.com) — AI categorization and summaries
- [gspread](https://github.com/burnash/gspread) — Google Sheets integration
- [discord.py](https://discordpy.readthedocs.io) — Discord bot framework
- [APScheduler](https://apscheduler.readthedocs.io) — scheduled tasks
- [pandas](https://pandas.pydata.org) — data processing
