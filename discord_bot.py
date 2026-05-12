import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import threading
import webbrowser
from zoneinfo import ZoneInfo

from config import (
    DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, DISCORD_ALERT_CHANNEL_ID,
    DISCORD_REVIEW_CHANNEL_ID, DISCORD_GUILD_ID, BUDGET_LIMITS,
    SYNC_HOURS, DIGEST_HOUR, TIMEZONE,
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV, PLAID_ACCESS_TOKENS,
)
from plaid_client import fetch_all_transactions, fetch_all_recurring
from sheets_client import sync_transactions, sync_subscriptions, read_custom_category_overrides
from budget_engine import check_budget_overages, check_large_transactions, get_budget_summary
from gemini_client import (
    categorize_transactions, generate_budget_summary, generate_subscription_summary,
    generate_overage_message, get_pending_uncertain, save_user_category,
    VALID_CATEGORIES, _load_cache,
)

TZ = ZoneInfo(TIMEZONE)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Core sync pipeline — used by both scheduled and manual syncs
# ---------------------------------------------------------------------------

async def run_full_sync(source="manual"):
    digest_channel = bot.get_channel(DISCORD_CHANNEL_ID)
    alert_channel  = bot.get_channel(DISCORD_ALERT_CHANNEL_ID)

    if not digest_channel:
        print("[bot] Warning: digest channel not found")
        return

    await digest_channel.send(f"🔄 Syncing transactions from all banks ({source})...")

    # 1. Fetch from Plaid
    transactions = fetch_all_transactions()

    # 2. Gemini categorization
    transactions, uncertain = categorize_transactions(transactions)

    # 3. Sync to Google Sheets
    sync_transactions(transactions, BUDGET_LIMITS)

    # Fetch and sync recurring/subscriptions
    streams = fetch_all_recurring()
    if streams:
        sync_subscriptions(streams)

    # 4. Budget overage alerts
    overages = check_budget_overages(transactions)
    for overage in overages:
        msg = generate_overage_message(overage)
        target = alert_channel or digest_channel
        await target.send(
            f"**Budget Alert — {overage['category']}**\n"
            f"{msg}\n"
            f"> Spent: **${overage['spent']}** / Limit: **${overage['limit']}** "
            f"(${overage['over_by']} over · {overage['pct_used']}% used)"
        )

    # 5. Large transaction alerts
    large_txns = check_large_transactions(transactions)
    for txn in large_txns:
        target = alert_channel or digest_channel
        await target.send(
            f"**Large Transaction Detected**\n"
            f"> Bank: **{txn['bank']}**\n"
            f"> Merchant: **{txn['merchant']}**\n"
            f"> Amount: **${txn['amount']}**\n"
            f"> Category: {txn['category']}\n"
            f"> Date: {txn['date']}\n"
            f"> Threshold: ${txn['threshold']}"
        )

    # 6. Prompt user to review uncertain merchants in the review channel
    if uncertain:
        review_channel = bot.get_channel(DISCORD_REVIEW_CHANNEL_ID) or digest_channel
        await send_uncertain_prompts(review_channel, uncertain)

    posted  = sum(1 for t in transactions if not t["pending"])
    pending = sum(1 for t in transactions if t["pending"])
    await digest_channel.send(
        f"✅ Sync complete — **{posted}** posted, **{pending}** pending transactions."
    )

async def send_uncertain_prompts(channel, uncertain):
    for merchant, data in uncertain.items():
        sample = data.get("sample", {})
        view   = CategorySelectView(merchant)
        await channel.send(
            f"**Uncertain Transaction — Help me categorize this:**\n"
            f"> Merchant: **{merchant}**\n"
            f"> Amount: **${sample.get('amount', '?')}**\n"
            f"> Date: {sample.get('date', '?')} · Bank: {sample.get('bank', '?')}\n"
            f"> Gemini's best guess: **{data['category']}** "
            f"({int(data['confidence'] * 100)}% confident)\n\n"
            f"Please select the correct category:",
            view=view,
        )

# ---------------------------------------------------------------------------
# Discord UI — category dropdown for uncertain merchants
# ---------------------------------------------------------------------------

class CategorySelect(discord.ui.Select):
    def __init__(self, merchant):
        self.merchant = merchant
        options = [
            discord.SelectOption(label=cat, value=cat)
            for cat in VALID_CATEGORIES
        ]
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        save_user_category(self.merchant, chosen)
        await interaction.response.send_message(
            f"Got it! **{self.merchant}** is now permanently categorized as **{chosen}**. "
            f"It won't be flagged again.",
            ephemeral=True,
        )
        self.view.stop()

class CategorySelectView(discord.ui.View):
    def __init__(self, merchant):
        super().__init__(timeout=86400)  # 24 hour timeout
        self.add_item(CategorySelect(merchant))

class RecategorizeSelect(discord.ui.Select):
    def __init__(self, merchant, current_category):
        self.merchant = merchant
        options = [
            discord.SelectOption(
                label=cat,
                value=cat,
                default=(cat == current_category),
            )
            for cat in VALID_CATEGORIES
        ]
        super().__init__(
            placeholder="Select new category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        save_user_category(self.merchant, chosen)

        # Re-sync sheets with updated category
        await interaction.response.send_message(
            f"Updated! **{self.merchant}** is now **{chosen}**. "
            f"Re-syncing your Google Sheet...",
            ephemeral=True,
        )
        transactions = fetch_all_transactions()
        transactions, _ = categorize_transactions(transactions)
        sync_transactions(transactions, BUDGET_LIMITS)

        channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if channel:
            await channel.send(
                f"**Category Updated**\n"
                f"> **{self.merchant}** recategorized to **{chosen}**\n"
                f"> Google Sheet updated — all past and future transactions affected."
            )
        self.view.stop()

class RecategorizeView(discord.ui.View):
    def __init__(self, merchant, current_category):
        super().__init__(timeout=300)
        self.add_item(RecategorizeSelect(merchant, current_category))

# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="sync", description="Manually sync transactions from all banks now")
async def sync_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Starting manual sync...")
    await run_full_sync(source="manual /sync")

@bot.tree.command(name="budget", description="Show current month's budget status")
async def budget_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    transactions = fetch_all_transactions()
    transactions, _ = categorize_transactions(transactions)
    summary = get_budget_summary(transactions)

    lines = ["**Budget Status — This Month**\n"]
    for c in summary["categories"]:
        if c["over_budget"]:
            icon = "🔴"
        elif c["pct_used"] >= 75:
            icon = "🟡"
        else:
            icon = "🟢"
        line = f"{icon} **{c['category']}**: ${c['spent']} / ${c['limit']} ({c['pct_used']}%)"
        if c["pending"] > 0:
            line += f" _(+${c['pending']} pending)_"
        lines.append(line)

    lines.append(
        f"\n**Total: ${summary['total_spent']} / ${summary['total_limit']}** "
        f"— ${summary['total_remaining']} remaining"
    )
    lines.append(f"_Last synced: {summary['synced_at']}_")

    await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="subscriptions", description="Show all active recurring subscriptions and their costs")
async def subscriptions_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    streams  = fetch_all_recurring()
    expenses = [s for s in streams if s["stream_type"] == "expense" and s["is_active"]]
    income   = [s for s in streams if s["stream_type"] == "income"  and s["is_active"]]

    if not expenses and not income:
        await interaction.followup.send("No recurring transactions detected yet. Try `/sync` first.")
        return

    lines = ["**Active Subscriptions & Recurring Expenses**\n"]

    if expenses:
        monthly_total = 0
        for s in sorted(expenses, key=lambda x: x["last_amount"], reverse=True):
            freq = s["frequency"]
            amt  = s["last_amount"]
            if "Annual" in freq:
                monthly = round(amt / 12, 2)
            elif "Weekly" in freq:
                monthly = round(amt * 4.33, 2)
            elif "Biweekly" in freq or "Semi" in freq:
                monthly = round(amt * 2, 2)
            else:
                monthly = amt
            monthly_total += monthly
            lines.append(
                f"**{s['merchant']}** — ${amt} {freq} "
                f"_(~${monthly}/mo)_ · {s['bank']} · {s['status']}"
            )
        lines.append(f"\n**Estimated monthly total: ${round(monthly_total, 2)}**")

    if income:
        lines.append("\n**Recurring Income**")
        for s in sorted(income, key=lambda x: x["last_amount"], reverse=True):
            lines.append(
                f"**{s['merchant']}** — ${s['last_amount']} {s['frequency']} · {s['bank']}"
            )

    # Gemini summary at the bottom
    summary = generate_subscription_summary(streams)
    lines.append(f"\n_{summary}_")

    await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="recategorize", description="Change a merchant's budget category")
@app_commands.describe(merchant="Start typing a merchant name to search")
async def recategorize_cmd(interaction: discord.Interaction, merchant: str):
    cache = _load_cache()
    if merchant not in cache:
        await interaction.response.send_message(
            f"**{merchant}** wasn't found in the merchant cache. "
            f"Check the spelling or run `/sync` first.",
            ephemeral=True,
        )
        return

    current = cache[merchant]
    view    = RecategorizeView(merchant, current)
    await interaction.response.send_message(
        f"**Recategorize: {merchant}**\n"
        f"Current category: **{current}**\n"
        f"Select the new category:",
        view=view,
        ephemeral=True,
    )

@recategorize_cmd.autocomplete("merchant")
async def merchant_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cache   = _load_cache()
    matches = [
        m for m in cache.keys()
        if current.lower() in m.lower()
    ][:25]
    return [app_commands.Choice(name=m, value=m) for m in matches]

@bot.tree.command(name="add_account", description="Connect a new credit card or bank account")
async def add_account_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Adding a new account**\n"
        "I'll start Plaid Link on your local machine.\n"
        "Run this command in your terminal:\n"
        "```\npython plaid_setup.py\n```\n"
        "After linking, paste the new access token into your `.env` file and restart the bot.",
        ephemeral=True,
    )

# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

@tasks.loop(time=[
    datetime.time(hour=h, minute=0, tzinfo=TZ)
    for h in SYNC_HOURS
])
async def scheduled_sync():
    print(f"[scheduler] Running scheduled sync...")
    await run_full_sync(source="scheduled")

@tasks.loop(time=datetime.time(hour=DIGEST_HOUR, minute=0, tzinfo=TZ))
async def daily_digest():
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

    print("[scheduler] Posting daily digest...")
    transactions = fetch_all_transactions()
    transactions, uncertain = categorize_transactions(transactions)
    summary = get_budget_summary(transactions)
    digest  = generate_budget_summary(summary)

    # Subscription summary
    streams      = fetch_all_recurring()
    subs_summary = generate_subscription_summary(streams)
    if streams:
        sync_subscriptions(streams)

    await channel.send(
        f"**Daily Budget Digest**\n\n"
        f"{digest}\n\n"
        f"**Subscriptions:** {subs_summary}\n\n"
        f"_Posted: {summary['transaction_count']['posted']} txns · "
        f"Pending: {summary['transaction_count']['pending']} txns · "
        f"{summary['synced_at']}_"
    )

    # Re-prompt any still-unreviewed uncertain merchants in the review channel
    pending_uncertain = get_pending_uncertain()
    if pending_uncertain:
        review_channel = bot.get_channel(DISCORD_REVIEW_CHANNEL_ID) or channel
        await send_uncertain_prompts(review_channel, pending_uncertain)

# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[bot] Logged in as {bot.user} ({bot.user.id})")

    try:
        guild  = discord.Object(id=DISCORD_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"[bot] Synced {len(synced)} slash commands to guild")
    except Exception as e:
        print(f"[bot] Failed to sync commands: {e}")

    if not scheduled_sync.is_running():
        scheduled_sync.start()
    if not daily_digest.is_running():
        daily_digest.start()

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send("BudgetTrackerAI is online. Type `/sync` to sync now or `/budget` to check your budget.")

def run():
    bot.run(DISCORD_BOT_TOKEN)
