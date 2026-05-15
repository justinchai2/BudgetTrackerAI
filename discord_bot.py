import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import time
import threading
import webbrowser
from zoneinfo import ZoneInfo

from config import (
    DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, DISCORD_ALERT_CHANNEL_ID,
    DISCORD_REVIEW_CHANNEL_ID, DISCORD_GUILD_ID, BUDGET_LIMITS,
    SYNC_HOURS, DIGEST_MORNING_HOUR, DIGEST_EVENING_HOUR, DIGEST_EVENING_MINUTE,
    TIMEZONE, SUBSCRIPTION_REMINDER_HOUR,
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV, PLAID_ACCESS_TOKENS,
)
from plaid_client import (
    fetch_all_transactions,
    fetch_all_item_statuses, update_all_webhooks,
    create_update_link_token, PLAID_ACCESS_TOKENS,
)
from subscription_detector import detect_subscriptions
from subscription_store import (
    add_to_blacklist, remove_from_blacklist, get_blacklist,
    add_manual_subscription, remove_manual_subscription, list_manual_subscriptions,
    get_manual_subscriptions,
)
from webhook_server import start_webhook_server
from sheets_client import sync_transactions, sync_subscriptions, get_transaction_from_sheets
import db as transaction_db
from db import (
    get_recent_transactions, search_transactions,
    upsert_subscriptions, get_subscriptions, delete_subscription,
    refresh_next_charge_dates, get_today_transactions, get_transaction_by_id,
    find_merchant_transactions, auto_update_subscriptions_from_transactions,
    rename_subscription,
)
from budget_engine import check_budget_overages, check_large_transactions, get_budget_summary
import annual as annual_mgr
from gemini_client import (
    categorize_transactions, generate_budget_summary, generate_subscription_summary,
    generate_overage_message, generate_evening_digest, get_pending_uncertain,
    save_user_category, answer_finance_question, parse_nl_command,
    VALID_CATEGORIES, _load_cache,
)

TZ = ZoneInfo(TIMEZONE)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Per-channel conversation history for the NL @mention bot
# Stores the last MAX_HISTORY_EXCHANGES (user_text, bot_reply) pairs so
# Gemini can understand follow-up messages like "remove it" or "make that annual".
# Lives in memory only — resets when the bot restarts (intentional: no stale context).
# ---------------------------------------------------------------------------
_channel_history: dict[int, list[tuple[str, str]]] = {}
MAX_HISTORY_EXCHANGES = 5   # 5 back-and-forth pairs = 10 messages of context

# ---------------------------------------------------------------------------
# Subscription review UI
# After each sync, newly-detected subscriptions are shown in a single
# interactive message with a multi-select dropdown + Save button.
# The user deselects any that aren't real subscriptions, then clicks Save.
# ---------------------------------------------------------------------------

class SubReviewView(discord.ui.View):
    """Interactive subscription review: multi-select + Save / Skip All buttons."""

    # Discord limits: 25 options per Select, 5 rows per message.
    # We batch into multiple Selects if needed (up to 4 selects + 1 button row = 5 rows).
    MAX_PER_SELECT = 25

    def __init__(self, candidates: list[dict]):
        super().__init__(timeout=86400)   # 24-hour timeout
        self.candidates = candidates
        # Track which indices are selected (0-based); start with all selected
        self.selected: set[int] = set(range(len(candidates)))

        from sheets_client import _est_monthly
        # Build one Select per batch of 25
        for batch_start in range(0, len(candidates), self.MAX_PER_SELECT):
            batch = candidates[batch_start: batch_start + self.MAX_PER_SELECT]
            options = []
            for local_i, s in enumerate(batch):
                global_i = batch_start + local_i
                monthly  = _est_monthly(s)
                amt      = s.get("last_amount", 0)
                freq     = s.get("frequency", "")
                var_flag = " (variable)" if s.get("variable_amount") else ""
                options.append(discord.SelectOption(
                    label=s["merchant"][:100],
                    description=f"${amt} · {freq} · ~${monthly}/mo{var_flag}"[:100],
                    value=str(global_i),
                    default=True,   # pre-select all
                ))
            select = discord.ui.Select(
                placeholder=f"Subscriptions {batch_start + 1}–{batch_start + len(batch)} — deselect to exclude",
                min_values=0,
                max_values=len(batch),
                options=options,
            )
            select.callback = self._make_select_callback(batch_start, len(batch))
            self.add_item(select)

    def _make_select_callback(self, batch_start: int, batch_size: int):
        async def callback(interaction: discord.Interaction):
            # Rebuild selected set for this batch
            kept_in_batch = {int(v) for v in interaction.data.get("values", [])}
            # Remove all indices in this batch range, then re-add the kept ones
            for idx in range(batch_start, batch_start + batch_size):
                self.selected.discard(idx)
            self.selected.update(kept_in_batch)
            await interaction.response.defer()
        return callback

    @discord.ui.button(label="Save Selected", style=discord.ButtonStyle.green, emoji="✅", row=4)
    async def save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, self.selected)

    @discord.ui.button(label="Skip All", style=discord.ButtonStyle.red, emoji="❌", row=4)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, set())

    async def _finish(self, interaction: discord.Interaction, keep_indices: set[int]):
        confirmed = [self.candidates[i] for i in sorted(keep_indices)]
        rejected  = [self.candidates[i] for i in range(len(self.candidates))
                     if i not in keep_indices]

        if confirmed:
            manual_subs = get_manual_subscriptions()
            upsert_subscriptions(confirmed + manual_subs)
            sync_subscriptions(confirmed + manual_subs)

        # Blacklist rejected merchants so they never appear in the review again
        for s in rejected:
            add_to_blacklist(s["merchant"])

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        conf_str = ", ".join(f"**{s['merchant']}**" for s in confirmed) or "_(none)_"
        rej_str  = ", ".join(f"~~{s['merchant']}~~" for s in rejected)  or "_(none)_"
        result   = (
            f"✅ **Subscription review complete!**\n"
            f"> Saved ({len(confirmed)}): {conf_str}\n"
            f"> Skipped ({len(rejected)}): {rej_str} _(blacklisted — won't appear again)_"
        )
        if confirmed:
            result += "\n> Google Sheet updated."
        await interaction.followup.send(result)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

# ---------------------------------------------------------------------------
# Core sync pipeline — used by both scheduled and manual syncs
# ---------------------------------------------------------------------------

def _progress_bar(pct, label, width=20):
    filled = int(width * pct / 100)
    bar    = "█" * filled + "░" * (width - filled)
    return f"`[{bar}]` **{pct}%** — {label}"

async def run_full_sync(source="manual"):
    digest_channel = bot.get_channel(DISCORD_CHANNEL_ID)
    alert_channel  = bot.get_channel(DISCORD_ALERT_CHANNEL_ID)

    if not digest_channel:
        print("[bot] Warning: digest channel not found")
        return

    t_total = time.time()
    print(f"[sync] ── Starting sync ({source}) ──")

    msg = await digest_channel.send(
        f"**BudgetTrackerAI Sync** ({source})\n{_progress_bar(0, 'Checking bank connections...')}"
    )

    # 0. Check bank connection health — warn but don't abort
    t0 = time.time()
    statuses = fetch_all_item_statuses()
    unhealthy = [s for s in statuses if not s["healthy"]]
    if unhealthy:
        alert_channel = bot.get_channel(DISCORD_ALERT_CHANNEL_ID)
        target = alert_channel or digest_channel
        for s in unhealthy:
            await target.send(
                f"⚠️ **Bank Connection Issue — {s['bank']}**\n"
                f"> Error: `{s['error_code']}`\n"
                f"> {s['error_message']}\n"
                f"> _You may need to re-link this account using `/add_account`._"
            )
    print(f"[sync]   health check:      {time.time() - t0:.1f}s")

    # 1. Fetch from Plaid
    t0 = time.time()
    transactions = fetch_all_transactions()
    print(f"[sync]   plaid fetch:       {time.time() - t0:.1f}s  ({len(transactions)} txns)")
    await msg.edit(content=f"**BudgetTrackerAI Sync** ({source})\n{_progress_bar(20, 'Categorizing with Gemini...')}")

    # 2. Gemini categorization
    t0 = time.time()
    transactions, uncertain = categorize_transactions(transactions)
    print(f"[sync]   gemini categories: {time.time() - t0:.1f}s")
    await msg.edit(content=f"**BudgetTrackerAI Sync** ({source})\n{_progress_bar(40, 'Saving to database...')}")

    # 3. Persist to SQLite, resolve pending→posted duplicates, purge old records
    t0 = time.time()
    transaction_db.upsert_transactions(transactions)
    transaction_db.resolve_pending_transactions(transactions)
    transaction_db.deduplicate_transactions()
    transaction_db.purge_old_transactions(730)
    print(f"[sync]   db upsert:         {time.time() - t0:.1f}s")

    # 3b. Auto-update subscriptions whose real charge just came in
    t0 = time.time()
    sub_updates = auto_update_subscriptions_from_transactions()
    print(f"[sync]   sub auto-update:   {time.time() - t0:.1f}s  ({len(sub_updates)} updated)")
    if sub_updates:
        target = alert_channel or digest_channel
        for u in sub_updates:
            amt_changed = u["old_amount"] != u["new_amount"]
            was_est     = u["was_estimate"]
            if was_est:
                note = f"_(was your estimate of ${u['old_amount']})_"
            elif amt_changed:
                note = f"_(previously ${u['old_amount']})_"
            else:
                note = ""
            await target.send(
                f"🔄 **Subscription auto-updated: {u['merchant']}**\n"
                f"> Actual charge: **${u['new_amount']}** on {u['txn_date']} {note}\n"
                f"> Next charge: {u['next_charge']} · Google Sheet will update shortly."
            )

    await msg.edit(content=f"**BudgetTrackerAI Sync** ({source})\n{_progress_bar(55, 'Updating Google Sheets...')}")

    # 4. Sync year-to-date from DB to Google Sheets
    t0 = time.time()
    current_month = transaction_db.get_current_month_transactions()
    ytd           = transaction_db.get_year_to_date_transactions()
    sync_transactions(ytd, BUDGET_LIMITS, current_month=current_month)
    print(f"[sync]   sheets txns:       {time.time() - t0:.1f}s")
    await msg.edit(content=f"**BudgetTrackerAI Sync** ({source})\n{_progress_bar(70, 'Syncing subscriptions...')}")

    # Detect subscriptions
    # • Already-tracked merchants → upsert + sync as before (re-detection = update)
    # • Brand-new merchants       → stage for user review instead of auto-saving
    t0 = time.time()
    streams = detect_subscriptions(ytd)
    manual_subs = get_manual_subscriptions()

    existing_merchants = {s["merchant"].lower() for s in get_subscriptions()}
    blacklist          = {m.lower() for m in get_blacklist()}
    known_streams  = [s for s in streams if s["merchant"].lower() in existing_merchants]
    new_candidates = [s for s in streams
                      if s["merchant"].lower() not in existing_merchants
                      and s["merchant"].lower() not in blacklist]

    # Always re-upsert known subscriptions (keeps amounts/dates fresh) + manual ones
    all_known = known_streams + manual_subs
    upsert_subscriptions(all_known)
    if all_known:
        sync_subscriptions(all_known)
    print(f"[sync]   sheets subs:       {time.time() - t0:.1f}s  ({len(all_known)} subs)")

    # Stage new candidates for review via interactive dropdown (sent to review channel)
    if new_candidates:
        review_channel = bot.get_channel(DISCORD_REVIEW_CHANNEL_ID) or digest_channel
        view = SubReviewView(new_candidates)
        names_preview = ", ".join(s["merchant"] for s in new_candidates[:5])
        if len(new_candidates) > 5:
            names_preview += f" + {len(new_candidates) - 5} more"
        await review_channel.send(
            f"📋 **Subscription Review** — {len(new_candidates)} new recurring charge(s) detected: "
            f"_{names_preview}_\n"
            f"All are pre-selected. **Deselect** any that aren't real subscriptions, then click **Save Selected**.",
            view=view,
        )

    await msg.edit(content=f"**BudgetTrackerAI Sync** ({source})\n{_progress_bar(85, 'Checking budget alerts...')}")

    # 5. Budget overage alerts — current month only
    t0 = time.time()
    overages = check_budget_overages(current_month)
    for overage in overages:
        try:
            alert_msg = generate_overage_message(overage)
        except Exception as e:
            print(f"[bot] Gemini unavailable for overage message: {e}")
            alert_msg = "You've exceeded your budget limit for this category."
        target = alert_channel or digest_channel
        await target.send(
            f"**Budget Alert — {overage['category']}**\n"
            f"{alert_msg}\n"
            f"> Spent: **${overage['spent']}** / Limit: **${overage['limit']}** "
            f"(${overage['over_by']} over · {overage['pct_used']}% used)"
        )

    # 6. Large transaction alerts — current month only
    large_txns = check_large_transactions(current_month)
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
    print(f"[sync]   budget alerts:     {time.time() - t0:.1f}s")

    # 7. Prompt user to review uncertain merchants in the review channel
    if uncertain:
        review_channel = bot.get_channel(DISCORD_REVIEW_CHANNEL_ID) or digest_channel
        await send_uncertain_prompts(review_channel, uncertain)

    posted  = sum(1 for t in transactions if not t["pending"])
    pending = sum(1 for t in transactions if t["pending"])
    total_elapsed = time.time() - t_total
    print(f"[sync] ── Done in {total_elapsed:.1f}s ──")
    await msg.edit(content=
        f"**BudgetTrackerAI Sync** ({source})\n"
        f"{_progress_bar(100, 'Done!')}\n"
        f"✅ **{posted}** posted · **{pending}** pending transactions synced."
    )

async def send_uncertain_prompts(channel, uncertain: dict):
    """Send a single paginated review message for all uncertain transactions."""
    if not uncertain:
        return
    view = UncertainReviewView(uncertain)
    await channel.send(
        view._build_content(),
        view=view,
    )

# ---------------------------------------------------------------------------
# Discord UI — paginated uncertain-category review (one message for all)
# ---------------------------------------------------------------------------

class UncertainReviewView(discord.ui.View):
    """
    Single interactive message that steps through all uncertain merchants one
    at a time.

    Button behaviour:
      • Dropdown selection  → change the category, save it, auto-advance
      • Next ▶              → accept the currently shown category (Gemini's
                              guess if unchanged), save it, advance
      • ◀ Prev              → go back (does not change any saved results)
      • ⏭️ Skip              → leave this merchant uncategorized, advance
      • ✅ Done              → accept the current category and finish
    """

    def __init__(self, uncertain: dict):
        super().__init__(timeout=86400)   # 24-hour window
        # list of (merchant, data) where data = {category, confidence, sample}
        self.items: list[tuple[str, dict]] = list(uncertain.items())
        self.index   = 0
        self.results: dict[str, str] = {}   # merchant → chosen category
        # Tracks what's currently shown in the dropdown for the active page
        # (Gemini's guess by default, updated when user picks from dropdown)
        self.current_selection: str = self.items[0][1].get("category", "Other")
        self._rebuild()

    # ── helpers ───────────────────────────────────────────────────────────────

    def build_content(self) -> str:
        merchant, data = self.items[self.index]
        sample  = data.get("sample", {})
        guess   = data.get("category", "Other")
        conf    = int(data.get("confidence", 0) * 100)
        done    = len(self.results)
        total   = len(self.items)
        status  = "✅" if merchant in self.results else "❓"
        return (
            f"**{status} Categorize Transactions — {self.index + 1} of {total}** "
            f"_({done} done, {total - done} remaining)_\n"
            f"> Merchant: **{merchant}**\n"
            f"> Amount: **${sample.get('amount', '?')}** "
            f"· Date: {sample.get('date', '?')} · Bank: {sample.get('bank', '?')}\n"
            f"> Gemini's best guess: **{guess}** _({conf}% confident)_\n\n"
            f"**Next** accepts Gemini's guess · pick from the dropdown to choose a different category:"
        )

    # Alias used by send_uncertain_prompts before the view is attached to a message
    _build_content = build_content

    def _rebuild(self):
        """Rebuild the component tree for the current page index."""
        self.clear_items()
        merchant, data = self.items[self.index]
        # Reset current_selection to whatever is shown for this page
        self.current_selection = self.results.get(merchant) or data.get("category", "Other")

        # Row 0 — category dropdown (pre-set to saved result or Gemini's guess)
        options = [
            discord.SelectOption(
                label=cat, value=cat,
                default=(cat == self.current_selection),
                emoji="✅" if cat == self.results.get(merchant) else None,
            )
            for cat in VALID_CATEGORIES
        ]
        select = discord.ui.Select(
            placeholder="Select a different category…",
            min_values=1, max_values=1,
            options=options,
            row=0,
        )
        select.callback = self._select_callback
        self.add_item(select)

        # Row 1 — navigation buttons
        n = len(self.items)
        is_last = self.index == n - 1

        prev_btn = discord.ui.Button(
            label="◀ Prev", style=discord.ButtonStyle.secondary,
            disabled=(self.index == 0), row=1,
        )
        prev_btn.callback = self._prev
        self.add_item(prev_btn)

        if is_last:
            next_btn = discord.ui.Button(
                label="💾 Save", style=discord.ButtonStyle.green, row=1,
            )
            next_btn.callback = self._save_and_finish
        else:
            next_btn = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.primary, row=1,
            )
            next_btn.callback = self._next
        self.add_item(next_btn)

    def _save_current(self):
        """Save self.current_selection for the active merchant."""
        merchant = self.items[self.index][0]
        save_user_category(merchant, self.current_selection)
        self.results[merchant] = self.current_selection

    # ── interaction callbacks ─────────────────────────────────────────────────

    async def _select_callback(self, interaction: discord.Interaction):
        """User picked a specific category from the dropdown — save and auto-advance."""
        chosen   = interaction.data["values"][0]
        merchant = self.items[self.index][0]
        self.current_selection = chosen
        save_user_category(merchant, chosen)
        self.results[merchant] = chosen

        if self.index < len(self.items) - 1:
            self.index += 1
            self._rebuild()
            await interaction.response.edit_message(content=self.build_content(), view=self)
        else:
            await self._finish(interaction)

    async def _prev(self, interaction: discord.Interaction):
        """Go back without saving."""
        self.index = max(0, self.index - 1)
        self._rebuild()
        await interaction.response.edit_message(content=self.build_content(), view=self)

    async def _next(self, interaction: discord.Interaction):
        """Save current selection and advance to the next merchant."""
        self._save_current()
        self.index += 1
        self._rebuild()
        await interaction.response.edit_message(content=self.build_content(), view=self)

    async def _save_and_finish(self, interaction: discord.Interaction):
        """Save the last merchant's selection and finish the review."""
        self._save_current()
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction):
        done    = len(self.results)
        skipped = len(self.items) - done
        for item in self.children:
            item.disabled = True
        summary = (
            f"✅ **Category review complete!** "
            f"{done} categorized · {skipped} skipped.\n"
        )
        if self.results:
            summary += "> " + ", ".join(
                f"**{m}** → {c}" for m, c in self.results.items()
            )
        await interaction.response.edit_message(content=summary, view=self)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

# ---------------------------------------------------------------------------
# Discord UI — single-merchant category dropdown (used by /recategorize)
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
        super().__init__(timeout=86400)
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
        await interaction.response.defer(ephemeral=True)

        save_user_category(self.merchant, chosen)
        transaction_db.update_merchant_category(self.merchant, chosen)

        # Re-sync sheets with updated category
        ytd = transaction_db.get_year_to_date_transactions()
        sync_transactions(ytd, BUDGET_LIMITS, current_month=transaction_db.get_current_month_transactions())

        await interaction.followup.send(
            f"✅ **{self.merchant}** recategorized to **{chosen}**.\n"
            f"> Google Sheet updated — all past and future transactions affected.",
            ephemeral=True,
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
    await interaction.response.defer(ephemeral=True)
    await run_full_sync(source="manual /sync")
    await interaction.followup.send("✅ Sync complete!", ephemeral=True)

@bot.tree.command(name="budget", description="Show current month's budget status")
async def budget_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    summary = get_budget_summary(transaction_db.get_current_month_transactions())

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

    streams = get_subscriptions()

    if not streams:
        await interaction.followup.send(
            "No subscriptions in the database yet. Run `/sync` first to detect them."
        )
        return

    from sheets_client import _est_monthly, _billing_cycle
    monthly_total = 0
    annual_total  = 0

    # Separate annual from recurring for grouped display
    annuals    = [s for s in streams if _billing_cycle(s["frequency"]) == "Annual"]
    recurring  = [s for s in streams if _billing_cycle(s["frequency"]) != "Annual"]

    lines = ["**Subscriptions & Recurring Charges**\n"]

    def _charge_tag(s):
        days    = s.get("days_until_charge")
        next_str = s.get("next_charge_date", "")
        if days is None or not next_str: return ""
        if days < 0:  return f" ⚠️ overdue {abs(days)}d"
        if days == 0: return " 🔔 **today**"
        if days <= 7: return f" 🔔 in {days}d ({next_str})"
        return f" · next {next_str}"

    def _fmt_sub_line(s, annual=False):
        """
        Format one subscription line.
        Shows the last confirmed charge amount.
        🔺 (was $X) shown when the charge increased vs the previous one.
        🔽 (was $X) shown when it decreased.
        """
        is_estimate = s.get("is_estimate", False)
        amt         = s["last_amount"]
        prev_amt    = s.get("prev_amount")
        sub_type    = (s.get("sub_type") or "").replace("_", " ").title()
        type_tag    = f" `{sub_type}`" if sub_type else ""
        src_tag     = " _(manual)_" if s.get("source") == "manual" else ""
        user_tag    = f" — _{s['tag']}_" if s.get("tag") else ""
        yr          = "/yr" if annual else ""

        # Trend vs previous charge
        if prev_amt and prev_amt != amt:
            arrow       = "🔺" if amt > prev_amt else "🔽"
            trend       = f" {arrow} _(was ${prev_amt})_"
        else:
            trend       = ""

        if is_estimate:
            amt_display = f"~${amt}{yr} _(est. — auto-updates on next charge)_{trend}"
        else:
            amt_display = f"${amt}{yr}{trend}"

        return f"  **{s['merchant']}**{user_tag}{type_tag} — {amt_display} · {s['frequency']}{_charge_tag(s)}{src_tag}"

    if recurring:
        lines.append("**Monthly / Recurring**")
        for s in recurring:
            monthly = _est_monthly(s)
            monthly_total += monthly
            annual_total  += monthly * 12
            lines.append(_fmt_sub_line(s))

    if annuals:
        lines.append("\n**Annual Charges** _(shown as monthly split)_")
        for s in annuals:
            monthly = _est_monthly(s)
            monthly_total += monthly
            annual_total  += s["last_amount"]
            lines.append(_fmt_sub_line(s, annual=True))

    lines.append(
        f"\n**Monthly total: ~${round(monthly_total, 2)}** "
        f"· **Annual total: ~${round(annual_total, 2)}**"
    )

    try:
        summary = generate_subscription_summary(streams)
        lines.append(f"\n_{summary}_")
    except Exception:
        pass

    await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="add_subscription", description="Manually add a subscription that wasn't auto-detected")
@app_commands.describe(
    merchant  = "Merchant name (e.g. Netflix, Spotify)",
    frequency = "Billing frequency",
    amount    = "Charge amount in USD (e.g. 15.99) — leave blank for variable bills like utilities",
    category  = "Budget category (default: Other)",
    bank      = "Which bank/card it charges (default: Manual)",
    tag       = "Short label for your own reference (e.g. 'Water & Trash', '4K plan')",
)
@app_commands.choices(frequency=[
    app_commands.Choice(name="Weekly",      value="Weekly"),
    app_commands.Choice(name="Bi-Weekly",   value="Bi-Weekly"),
    app_commands.Choice(name="Monthly",     value="Monthly"),
    app_commands.Choice(name="Quarterly",   value="Quarterly"),
    app_commands.Choice(name="Semi-Annual", value="Semi-Annual"),
    app_commands.Choice(name="Annual",      value="Annual"),
])
async def add_subscription_cmd(
    interaction: discord.Interaction,
    merchant: str,
    frequency: str,
    amount: float = 0.0,
    category: str = "Other",
    bank: str = "Manual",
    tag: str = "",
):
    if category not in VALID_CATEGORIES:
        await interaction.response.send_message(
            f"Unknown category **{category}**. Valid options: {', '.join(VALID_CATEGORIES)}",
            ephemeral=True,
        )
        return

    # amount=0 means "variable" — store.py will look it up from transactions
    is_new = add_manual_subscription(
        merchant, frequency, amount if amount > 0 else None,
        category=category, bank=bank, tag=tag,
    )

    # Re-detect, save to DB, sync Sheets
    ytd     = transaction_db.get_year_to_date_transactions()
    streams = detect_subscriptions(ytd)
    upsert_subscriptions(streams)
    if streams:
        sync_subscriptions(streams)

    from sheets_client import _est_monthly
    from db import get_subscriptions as _get_subs
    # Look up the stored entry to get the resolved amount
    stored = next((s for s in _get_subs() if s["merchant"].lower() == merchant.lower()), None)
    is_variable = stored and stored.get("variable_amount")
    monthly = _est_monthly(stored) if stored else 0.0
    amt_display = f"~${stored['average_amount']} avg (variable)" if is_variable else f"${stored['last_amount'] if stored else amount}"

    action   = "Added" if is_new else "Updated"
    tag_line = f"\n> Tag: _{tag}_" if tag else ""
    await interaction.response.send_message(
        f"{'✅' if is_new else '🔄'} **{action}: {merchant}**\n"
        f"> {frequency} · {amt_display} · ~${monthly}/mo\n"
        f"> Category: {category} · Bank: {bank}{tag_line}\n"
        f"> Google Sheet updated.",
        ephemeral=True,
    )

@add_subscription_cmd.autocomplete("category")
async def add_sub_category_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=c, value=c)
        for c in VALID_CATEGORIES
        if current.lower() in c.lower()
    ][:25]

@add_subscription_cmd.autocomplete("merchant")
async def add_sub_merchant_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete from existing transaction merchants + already-manual subscriptions."""
    cache   = _load_cache()
    manual  = {e["merchant"] for e in list_manual_subscriptions()}
    matches = sorted(
        {m for m in (set(cache.keys()) | manual) if current.lower() in m.lower()}
    )[:25]
    return [app_commands.Choice(name=m, value=m) for m in matches]


@bot.tree.command(name="remove_subscription", description="Remove a merchant from the subscriptions list")
@app_commands.describe(
    merchant="Merchant to remove (use autocomplete)",
    undo="Set to True to restore a previously removed subscription",
)
async def remove_subscription_cmd(interaction: discord.Interaction, merchant: str, undo: bool = False):
    if undo:
        remove_from_blacklist(merchant)
        await interaction.response.send_message(
            f"**{merchant}** restored — it will appear in `/subscriptions` again on the next detection.",
            ephemeral=True,
        )
        return

    # Remove from manual list + blacklist auto-detection
    was_manual = remove_manual_subscription(merchant)
    add_to_blacklist(merchant)
    delete_subscription(merchant)   # remove from DB immediately

    # Re-detect remaining, save to DB, sync Sheets
    ytd     = transaction_db.get_year_to_date_transactions()
    streams = detect_subscriptions(ytd)
    upsert_subscriptions(streams)
    if streams:
        sync_subscriptions(streams)

    source = "manual entry" if was_manual else "auto-detected subscription"
    await interaction.response.send_message(
        f"**{merchant}** removed ({source}) and Google Sheet updated.\n"
        f"Use `/remove_subscription undo:True merchant:{merchant}` to restore it.",
        ephemeral=True,
    )

@remove_subscription_cmd.autocomplete("merchant")
async def remove_subscription_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    ytd      = transaction_db.get_year_to_date_transactions()
    streams  = detect_subscriptions(ytd)
    blacklist = get_blacklist()

    choices = []
    # Currently detected (can be removed)
    for s in streams:
        if current.lower() in s["merchant"].lower():
            choices.append(app_commands.Choice(
                name=f"{s['merchant']} — ${s['average_amount']} {s['frequency']}",
                value=s["merchant"],
            ))
    # Previously blacklisted (can be restored — shown with a prefix)
    for m in blacklist:
        if current.lower() in m.lower():
            choices.append(app_commands.Choice(
                name=f"[removed] {m}",
                value=m,
            ))
    return choices[:25]


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

@bot.tree.command(name="annual", description="Mark a merchant's transactions as annual (splits cost across 12 months)")
@app_commands.describe(
    action="add or remove",
    merchant="Merchant name to add or remove"
)
@app_commands.choices(action=[
    app_commands.Choice(name="add",    value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list",   value="list"),
])
async def annual_cmd(interaction: discord.Interaction, action: str, merchant: str = ""):
    if action == "list":
        merchants = annual_mgr.get_merchants()
        if not merchants:
            await interaction.response.send_message(
                "No annual merchants set. Use `/annual add <merchant>` to add one.",
                ephemeral=True,
            )
        else:
            lines = ["**Annual Merchants (÷12 in budget calculations)**"]
            for m in sorted(merchants):
                lines.append(f"> {m}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return

    if not merchant:
        await interaction.response.send_message("Please provide a merchant name.", ephemeral=True)
        return

    if action == "add":
        annual_mgr.add_merchant(merchant)
        await interaction.response.send_message(
            f"**{merchant}** marked as annual — its cost will be divided by 12 in budget calculations.",
            ephemeral=True,
        )
    elif action == "remove":
        annual_mgr.remove_merchant(merchant)
        await interaction.response.send_message(
            f"**{merchant}** removed from annual merchants — full amount will now count each month.",
            ephemeral=True,
        )

@annual_cmd.autocomplete("merchant")
async def annual_merchant_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    choices = {}

    # Match by merchant name from cache
    cache = _load_cache()
    for m in cache.keys():
        if current_lower in m.lower():
            choices[m] = m  # name = value = merchant

    # Also match by transaction ID from recent transactions
    if current_lower:
        for t in get_recent_transactions(limit=200):
            merchant = t["merchant"]
            if current_lower in t["transaction_id"].lower() and merchant not in choices:
                label = f"{merchant} (txn: ...{t['transaction_id'][-8:]})"
                choices[label] = merchant  # name shows ID hint, value is merchant

    return [
        app_commands.Choice(name=name[:100], value=value)
        for name, value in list(choices.items())[:25]
    ]

@bot.tree.command(name="ask", description="Ask Gemini AI anything about your budget or finances")
@app_commands.describe(question="Your finance question (e.g. 'Where am I spending the most?' or 'Can I afford a vacation?')")
async def ask_cmd(interaction: discord.Interaction, question: str):
    await interaction.response.defer()

    monthly = get_budget_summary(transaction_db.get_current_month_transactions())
    ytd     = transaction_db.get_year_to_date_transactions()

    try:
        streams = detect_subscriptions(ytd)
    except Exception:
        streams = []

    try:
        answer = answer_finance_question(question, monthly, ytd, streams)
    except Exception as e:
        await interaction.followup.send(
            f"Gemini is temporarily unavailable (server overload). Try again in a moment.\n_Error: {e}_"
        )
        return

    header = f"**Q: {question}**\n\n"
    full   = header + answer

    if len(full) <= 2000:
        await interaction.followup.send(full)
    else:
        # Split into chunks if response exceeds Discord's 2000 char limit
        chunks = []
        remaining = answer
        first_chunk = remaining[:2000 - len(header)]
        chunks.append(header + first_chunk)
        remaining = remaining[len(first_chunk):]
        while remaining:
            chunks.append(remaining[:2000])
            remaining = remaining[2000:]
        for chunk in chunks:
            await interaction.followup.send(chunk)

@bot.tree.command(name="delete_transaction", description="Permanently delete one or more transactions from the database and sheets")
@app_commands.describe(transaction_ids="One or more Transaction IDs separated by commas. Use autocomplete to find the first one.")
async def delete_transaction_cmd(interaction: discord.Interaction, transaction_ids: str):
    await interaction.response.defer(ephemeral=True)

    ids = [tid.strip() for tid in transaction_ids.split(",") if tid.strip()]
    if not ids:
        await interaction.followup.send("No transaction IDs provided.", ephemeral=True)
        return

    deleted_ids   = []
    not_found_ids = []
    for tid in ids:
        if transaction_db.delete_transaction(tid):
            deleted_ids.append(tid)
        else:
            not_found_ids.append(tid)

    if not deleted_ids:
        await interaction.followup.send(
            f"No transactions found for the provided ID(s). "
            f"Check the Transaction ID column in Google Sheets.",
            ephemeral=True,
        )
        return

    # Re-sync sheets so deleted rows are removed immediately
    ytd           = transaction_db.get_year_to_date_transactions()
    current_month = transaction_db.get_current_month_transactions()
    sync_transactions(ytd, BUDGET_LIMITS, current_month=current_month)

    lines = [f"**{len(deleted_ids)} transaction(s) deleted** and Google Sheets updated.\n"]
    for tid in deleted_ids:
        lines.append(f"✅ `{tid}`")
    if not_found_ids:
        lines.append(f"\n**Not found ({len(not_found_ids)}):**")
        for tid in not_found_ids:
            lines.append(f"❌ `{tid}`")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

@delete_transaction_cmd.autocomplete("transaction_ids")
async def delete_transaction_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    # Autocomplete against the last ID in the comma-separated list
    parts       = current.split(",")
    search_term = parts[-1].strip().lower()
    prefix      = ",".join(parts[:-1])
    if prefix:
        prefix += ","

    matches = search_transactions(search_term, limit=25) if search_term else get_recent_transactions(limit=25)
    return [
        app_commands.Choice(
            name=f"{t['date']} · {t['merchant']} · ${t['amount']} ({t['bank']})"[:100],
            value=f"{prefix}{t['transaction_id']}",
        )
        for t in matches
    ]

@bot.tree.command(name="status", description="Check the connection health of all linked bank accounts")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    statuses = fetch_all_item_statuses()
    if not statuses:
        await interaction.followup.send("No bank accounts linked yet. Use `/add_account` to connect one.")
        return

    lines = ["**Bank Connection Status**\n"]
    for s in statuses:
        if s["healthy"]:
            icon = "✅"
            detail = ""
            if s["last_successful_update"]:
                detail = f" · Last updated: {str(s['last_successful_update'])[:16]}"
            lines.append(f"{icon} **{s['bank']}** — Healthy{detail}")
        else:
            icon = "⚠️" if s["error_code"] == "ITEM_LOGIN_REQUIRED" else "❌"
            lines.append(
                f"{icon} **{s['bank']}** — `{s['error_code']}`\n"
                f"> {s['error_message']}"
            )
            if s["error_code"] == "ITEM_LOGIN_REQUIRED":
                lines.append(f"> _Re-link this account using `/add_account`_")

    healthy_count   = sum(1 for s in statuses if s["healthy"])
    unhealthy_count = len(statuses) - healthy_count
    lines.append(f"\n_{healthy_count}/{len(statuses)} banks healthy" +
                 (f" · {unhealthy_count} need attention_" if unhealthy_count else "_"))

    await interaction.followup.send("\n".join(lines))

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
# Plaid webhook handler
# ---------------------------------------------------------------------------

# Maps Plaid item_id → bank name, built at startup
_item_id_to_bank: dict[str, str] = {}
_public_base_url: str = ""

async def _build_item_map():
    """Populate item_id → bank_name mapping from live Plaid data."""
    statuses = fetch_all_item_statuses()
    for s in statuses:
        if s.get("institution_id"):
            # item_get returns institution_id, not item_id directly —
            # we fetch it separately so we can map properly
            pass
    # We'll resolve bank name by checking all statuses when a webhook fires
    # and falling back to "/status" if item_id is unknown

async def on_plaid_webhook(webhook_type, webhook_code, item_id, error, body):
    """Dispatch incoming Plaid webhook events to the Discord alert channel."""
    alert_channel  = bot.get_channel(DISCORD_ALERT_CHANNEL_ID)
    digest_channel = bot.get_channel(DISCORD_CHANNEL_ID)
    target         = alert_channel or digest_channel
    if not target:
        return

    bank = _item_id_to_bank.get(item_id, f"item `{item_id[:8]}...`")

    if webhook_type == "ITEM":
        if webhook_code == "ERROR":
            code = (error or {}).get("error_code", "UNKNOWN")
            msg  = (error or {}).get("error_message", "No details available.")
            if code == "ITEM_LOGIN_REQUIRED":
                await target.send(
                    f"⚠️ **Re-authentication Required — {bank}**\n"
                    f"> Your bank credentials have changed or expired.\n"
                    f"> Run `/add_account` to re-link this bank and restore data access."
                )
            else:
                await target.send(
                    f"❌ **Bank Connection Error — {bank}**\n"
                    f"> Error: `{code}`\n"
                    f"> {msg}\n"
                    f"> Run `/status` for details."
                )

        elif webhook_code == "PENDING_EXPIRATION":
            expiry = body.get("consent_expiration_time", "soon")
            await target.send(
                f"⏰ **OAuth Token Expiring Soon — {bank}**\n"
                f"> Your connection will expire: **{str(expiry)[:19]}**\n"
                f"> Run `/add_account` before then to re-link and avoid interruption."
            )

        elif webhook_code == "USER_PERMISSION_REVOKED":
            await target.send(
                f"🚫 **Access Revoked — {bank}**\n"
                f"> You (or someone at your bank) revoked BudgetTrackerAI's data access.\n"
                f"> Run `/add_account` to re-link if this was unintentional."
            )

    elif webhook_type == "TRANSACTIONS" and webhook_code == "SYNC_UPDATES_AVAILABLE":
        # Optionally log — we already sync on a schedule so no auto-trigger needed
        print(f"[webhook] New transactions available for {bank} — will sync on next schedule")

@bot.tree.command(name="reauth", description="Re-authorize a bank that has lost connection or expired")
@app_commands.describe(bank="Which bank to re-authorize")
async def reauth_cmd(interaction: discord.Interaction, bank: str):
    await interaction.response.defer(ephemeral=True)

    access_token = PLAID_ACCESS_TOKENS.get(bank)
    if not access_token:
        await interaction.followup.send(
            f"Bank **{bank}** not found. Check your `.env` file.",
            ephemeral=True,
        )
        return

    if not _public_base_url:
        await interaction.followup.send(
            "No public URL is set yet. Run `/set_webhook <your-ngrok-or-server-url>` first.",
            ephemeral=True,
        )
        return

    try:
        redirect_uri = f"{_public_base_url}/oauth-return"
        link_token   = create_update_link_token(access_token, redirect_uri=redirect_uri)
        reauth_url   = f"{_public_base_url}/reauth?token={link_token}&bank={bank}"
        await interaction.followup.send(
            f"**Re-authorize {bank}**\n"
            f"Click the link below to reconnect your account.\n"
            f"The link expires in **30 minutes**.\n\n"
            f"{reauth_url}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(
            f"Failed to generate re-auth link: `{e}`",
            ephemeral=True,
        )

@reauth_cmd.autocomplete("bank")
async def reauth_bank_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=bank, value=bank)
        for bank in PLAID_ACCESS_TOKENS.keys()
        if current.lower() in bank.lower()
    ][:25]

@bot.tree.command(name="set_webhook", description="Register the Plaid webhook URL for all linked banks")
@app_commands.describe(url="Your public webhook URL, e.g. https://xxxx.ngrok-free.app")
async def set_webhook_cmd(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)

    global _public_base_url
    _public_base_url = url.rstrip("/")
    webhook_url      = _public_base_url + "/plaid/webhook"
    results          = update_all_webhooks(webhook_url)

    lines = [f"**Webhook registered:** `{webhook_url}`\n"]
    for bank, status in results.items():
        icon = "✅" if status == "ok" else "❌"
        lines.append(f"{icon} {bank}" + (f" — {status}" if status != "ok" else ""))
    lines.append(f"\n_Re-auth links will use:_ `{_public_base_url}/reauth`")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

# ---------------------------------------------------------------------------
# Natural language @ mention handler
# ---------------------------------------------------------------------------

async def _dispatch_action(action: str, params: dict) -> str:
    """
    Execute a single parsed action and return a human-readable result string.
    Reuses the same business logic as the slash commands.
    """
    # ── add_subscription ──────────────────────────────────────────────────
    if action == "add_subscription":
        merchant  = params.get("merchant", "").strip()
        frequency = params.get("frequency", "Monthly")
        amount    = params.get("amount")
        category  = params.get("category", "Other")
        bank      = params.get("bank", "Manual")
        tag         = params.get("tag", "")
        is_estimate = bool(params.get("is_estimate", False))

        if not merchant:
            return "I need a merchant name to add a subscription."
        if category not in VALID_CATEGORIES:
            category = "Other"

        # If merchant was previously blacklisted, clear that so it isn't filtered on next sync
        remove_from_blacklist(merchant)

        # Pass None when amount is missing/0 — store.py will look it up from transactions
        resolved_amount = float(amount) if amount and float(amount) > 0 else None
        is_new = add_manual_subscription(merchant, frequency, resolved_amount,
                                         category=category, bank=bank, tag=tag,
                                         is_estimate=is_estimate)
        ytd     = transaction_db.get_year_to_date_transactions()
        streams = detect_subscriptions(ytd)
        upsert_subscriptions(streams)
        if streams:
            sync_subscriptions(streams)

        from sheets_client import _est_monthly
        stored = next((s for s in get_subscriptions() if s["merchant"].lower() == merchant.lower()), None)
        is_variable = stored and stored.get("variable_amount")
        monthly  = _est_monthly(stored) if stored else 0.0
        amt_line = (f"~${stored['average_amount']} avg (variable)" if is_variable
                    else f"${stored['last_amount'] if stored else (resolved_amount or 0)}")
        verb     = "Added" if is_new else "Updated"
        tag_line = f" · _{tag}_" if tag else ""
        return (f"{'✅' if is_new else '🔄'} **{verb}:** {merchant} — "
                f"{amt_line} {frequency} (~${monthly}/mo) · {category} · {bank}{tag_line}\n"
                f"> Google Sheet updated.")

    # ── get_transaction ──────────────────────────────────────────────────
    elif action == "get_transaction":
        transaction_id = params.get("transaction_id", "").strip()
        if not transaction_id:
            return "Please provide a transaction ID to look up."

        txn = get_transaction_by_id(transaction_id) or get_transaction_from_sheets(transaction_id)
        if not txn:
            return (f"❌ Transaction `{transaction_id}` not found in the database or Google Sheet.\n"
                    f"> Try running `/sync` first to pull the latest transactions from your bank.")

        status = "⏳ Pending" if txn.get("pending") else "✅ Posted"
        lines = [
            f"**Transaction Details**",
            f"> **ID:** `{txn['transaction_id']}`",
            f"> **Merchant:** {txn['merchant']}",
            f"> **Amount:** ${txn['amount']}",
            f"> **Date:** {txn['date']}",
            f"> **Category:** {txn['category']}",
            f"> **Bank:** {txn['bank']}",
            f"> **Status:** {status}",
        ]
        if txn.get("payment_channel"):
            lines.append(f"> **Payment Channel:** {txn['payment_channel']}")
        if txn.get("account_mask"):
            lines.append(f"> **Account:** ···{txn['account_mask']}")
        if txn.get("location"):
            lines.append(f"> **Location:** {txn['location']}")
        if txn.get("plaid_category"):
            lines.append(f"> **Plaid Category:** {txn['plaid_category']}")
        return "\n".join(lines)

    # ── find_transaction ─────────────────────────────────────────────────
    elif action == "find_transaction":
        import statistics
        from collections import defaultdict
        from subscription_detector import _match_frequency

        query = params.get("query", "").strip()
        if not query:
            return "What merchant or subscription are you looking for?"

        matches = find_merchant_transactions(query, limit=300)
        if not matches:
            return (
                f"🔍 No transactions found matching **{query}**.\n"
                f"> Try a shorter keyword — e.g. `walmart` instead of `walmart plus`."
            )

        # Group by merchant name
        groups: dict[str, list] = defaultdict(list)
        for t in matches:
            if not t["pending"] and t["category"] != "Excluded" and t["amount"] > 0:
                groups[t["merchant"]].append(t)

        if not groups:
            return f"🔍 Found {len(matches)} transaction(s) for **{query}** but all were pending or excluded."

        lines = [f"🔍 **Search results for \"{query}\"** — {len(matches)} transaction(s) across {len(groups)} merchant(s):\n"]

        for merchant, txns in sorted(groups.items(), key=lambda x: -len(x[1])):
            txns_sorted = sorted(txns, key=lambda x: x["date"])
            amounts     = [t["amount"] for t in txns_sorted]
            avg_amount  = round(statistics.mean(amounts), 2)

            lines.append(f"**{merchant}** — {len(txns)} charge(s)")
            for t in txns_sorted[-5:]:   # last 5 transactions
                lines.append(f"  • `{t['date']}` ${t['amount']} · {t['bank']} · {t['category']}")
            if len(txns) > 5:
                lines.append(f"  _(+ {len(txns) - 5} earlier transactions)_")

            # Pattern analysis
            if len(txns_sorted) >= 2:
                from datetime import datetime as _dt
                dates     = [_dt.strptime(t["date"], "%Y-%m-%d") for t in txns_sorted]
                intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates) - 1)]
                avg_gap   = statistics.mean(intervals)
                frequency = _match_frequency(avg_gap)
                if frequency:
                    lines.append(f"  📅 Pattern detected: **{frequency}** · ~${avg_amount}/charge")
                    lines.append(f"  → Say `@BudgetBot find and add {merchant} as a subscription` to track it.")
                else:
                    lines.append(f"  _(No clear recurring pattern — avg {round(avg_gap)}d between charges)_")
            else:
                lines.append(f"  _(Only 1 transaction found — need at least 2 to detect a pattern)_")

            lines.append("")

        return "\n".join(lines).rstrip()

    # ── find_and_add_subscription ─────────────────────────────────────────
    elif action == "find_and_add_subscription":
        import statistics
        from collections import defaultdict
        from datetime import datetime as _dt
        from subscription_detector import _match_frequency

        query             = params.get("query", "").strip()
        merchant_override = params.get("merchant", "").strip()
        frequency_override = params.get("frequency", "")
        category          = params.get("category", "")
        tag               = params.get("tag", "")

        if not query:
            return "What subscription should I search for?"

        matches = find_merchant_transactions(query, limit=300)
        posted  = [t for t in matches
                   if not t["pending"] and t["category"] != "Excluded" and t["amount"] > 0]

        if not posted:
            return (
                f"🔍 No posted transactions found matching **{query}**.\n"
                f"> Try a shorter keyword or check `/sync` is up to date."
            )

        # Group by merchant, pick the largest group
        groups: dict[str, list] = defaultdict(list)
        for t in posted:
            groups[t["merchant"]].append(t)

        best_merchant, best_txns = max(groups.items(), key=lambda x: len(x[1]))
        best_txns = sorted(best_txns, key=lambda x: x["date"])

        amounts    = [t["amount"] for t in best_txns]
        avg_amount = round(statistics.mean(amounts), 2)
        last_txn   = best_txns[-1]

        # Detect frequency from intervals
        frequency = frequency_override
        if not frequency:
            if len(best_txns) >= 2:
                dates     = [_dt.strptime(t["date"], "%Y-%m-%d") for t in best_txns]
                intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates) - 1)]
                frequency = _match_frequency(statistics.mean(intervals)) or "Monthly"
            else:
                frequency = "Monthly"

        # Resolve category
        if not category or category not in VALID_CATEGORIES:
            plaid_cat = last_txn.get("plaid_category", "")
            if "UTILITIES" in plaid_cat or "RENT" in plaid_cat:
                category = "Necessities"
            else:
                category = last_txn.get("category", "Other")

        # Detect variable amount (CV > 0.25)
        import statistics as _stats
        stdev       = _stats.stdev(amounts) if len(amounts) > 1 else 0.0
        cv          = stdev / avg_amount if avg_amount else 0
        is_variable = cv > 0.25

        sub_merchant = merchant_override or best_merchant

        # If merchant was previously blacklisted, clear that so it isn't filtered on next sync
        remove_from_blacklist(sub_merchant)

        # Show what was found
        found_lines = [
            f"🔍 **Found {len(best_txns)} transaction(s) matching \"{query}\":**",
        ]
        for t in best_txns[-5:]:
            found_lines.append(f"  • `{t['date']}` ${t['amount']} · {t['bank']}")
        if len(best_txns) > 5:
            found_lines.append(f"  _(+ {len(best_txns) - 5} earlier)_")
        found_lines.append(
            f"  📅 Detected: **{frequency}** · "
            + (f"~${avg_amount} avg (variable)" if is_variable else f"${amounts[-1]}")
        )

        # Build and save the subscription
        stream = {
            "merchant":         sub_merchant,
            "frequency":        frequency,
            "last_amount":      amounts[-1],
            "average_amount":   avg_amount,
            "last_date":        last_txn["date"],
            "first_date":       best_txns[0]["date"],
            "occurrence_count": len(best_txns),
            "category":         category,
            "bank":             last_txn["bank"],
            "sub_type":         "other_subscription",
            "is_active":        True,
            "variable_amount":  is_variable,
            "tag":              tag,
        }
        upsert_subscriptions([stream])
        all_streams = get_subscriptions()
        sync_subscriptions(all_streams)

        from sheets_client import _est_monthly
        monthly  = _est_monthly(stream)
        amt_line = (f"~${avg_amount} avg (variable)" if is_variable else f"${amounts[-1]}")
        tag_line = f"\n> Tag: _{tag}_" if tag else ""

        add_result = (
            f"✅ **Added subscription: {sub_merchant}**\n"
            f"> {frequency} · {amt_line} · ~${monthly}/mo\n"
            f"> Category: {category} · Bank: {last_txn['bank']}{tag_line}\n"
            f"> Based on {len(best_txns)} transaction(s) · Google Sheet updated."
        )

        return "\n".join(found_lines) + "\n\n" + add_result

    # ── remove_subscription ───────────────────────────────────────────────
    elif action == "remove_subscription":
        merchant    = params.get("merchant", "").strip()
        was_manual  = remove_manual_subscription(merchant)
        add_to_blacklist(merchant)
        delete_subscription(merchant)
        ytd     = transaction_db.get_year_to_date_transactions()
        streams = detect_subscriptions(ytd)
        upsert_subscriptions(streams)
        if streams:
            sync_subscriptions(streams)
        return f"🗑️ **{merchant}** removed from subscriptions. Google Sheet updated."

    # ── restore_subscription ──────────────────────────────────────────────
    elif action == "restore_subscription":
        merchant = params.get("merchant", "").strip()
        if not merchant:
            return "I need a merchant name to restore. Which subscription should I bring back?"
        remove_from_blacklist(merchant)
        # Re-detect so if it's in the transaction history it gets re-added automatically
        ytd     = transaction_db.get_year_to_date_transactions()
        streams = detect_subscriptions(ytd)
        match   = next((s for s in streams if s["merchant"].lower() == merchant.lower()), None)
        if match:
            upsert_subscriptions([match])
            sync_subscriptions([match])
            return (f"✅ **{merchant}** has been restored and re-added to your subscriptions! "
                    f"Google Sheet updated.")
        else:
            return (f"✅ **{merchant}** removed from the blacklist — it will reappear automatically "
                    f"the next time a recurring charge is detected in your transactions. "
                    f"If it doesn't show up, you can also add it manually: "
                    f"\"add {merchant} as a subscription\".")

    # ── rename_subscription ───────────────────────────────────────────────
    elif action == "rename_subscription":
        old_merchant = params.get("old_merchant", "").strip()
        new_merchant = params.get("new_merchant", "").strip()
        if not old_merchant or not new_merchant:
            return "I need both the current name and the new name to rename a subscription."
        success = rename_subscription(old_merchant, new_merchant)
        if not success:
            return (f"❌ No subscription found matching **{old_merchant}**.\n"
                    f"> Use `/subscriptions` to see the exact merchant names being tracked.")
        # Sync the renamed entry to Google Sheets
        all_subs = get_subscriptions()
        if all_subs:
            sync_subscriptions(all_subs)
        return (f"✏️ Renamed **{old_merchant}** → **{new_merchant}**.\n"
                f"> Merchant ID preserved — category history and links are intact.\n"
                f"> Google Sheet updated.")

    # ── update_subscription ───────────────────────────────────────────────
    elif action == "update_subscription":
        transaction_id = params.get("transaction_id", "").strip()
        merchant       = params.get("merchant", "").strip()   # user-supplied name override
        frequency      = params.get("frequency", "")
        category       = params.get("category", "")
        tag            = params.get("tag", "")

        # ── Path A: no transaction ID — update metadata on existing subscription ──
        if not transaction_id:
            if not merchant:
                return "I need at least a merchant name to update a subscription."
            existing_sub = next(
                (s for s in get_subscriptions()
                 if s["merchant"].lower() == merchant.lower()),
                None
            )
            if not existing_sub:
                return (f"❌ No subscription found for **{merchant}**.\n"
                        f"> Use `/subscriptions` to see what's tracked, or add it first.")

            # Patch only the fields the user specified; preserve everything else
            stream = dict(existing_sub)
            if tag:
                stream["tag"] = tag
            if frequency:
                stream["frequency"] = frequency
            if category and category in VALID_CATEGORIES:
                stream["category"] = category

            upsert_subscriptions([stream])
            all_streams = get_subscriptions()
            sync_subscriptions(all_streams)

            changes = []
            if tag:      changes.append(f"Tag: _{tag}_")
            if frequency: changes.append(f"Frequency: {frequency}")
            if category:  changes.append(f"Category: {category}")
            change_str = " · ".join(changes) if changes else "no changes"
            return (f"✅ **Updated subscription: {merchant}**\n"
                    f"> {change_str}\n"
                    f"> Google Sheet updated.")

        # ── Path B: transaction ID provided — use transaction data ────────────
        txn = get_transaction_by_id(transaction_id)

        # Fallback: search Google Sheets directly if not in local DB yet
        if not txn:
            txn = get_transaction_from_sheets(transaction_id)

        if not txn:
            return (f"❌ Transaction `{transaction_id}` not found in the database or Google Sheet.\n"
                    f"> Make sure the ID is correct, then try `/sync` to pull the latest transactions.")

        # Resolve merchant name: user override > transaction merchant
        sub_merchant = merchant or txn["merchant"]

        # Resolve category: user override > transaction category > "Necessities" for utilities
        if not category:
            plaid_cat = txn.get("plaid_category", "")
            if "UTILITIES" in plaid_cat or "RENT" in plaid_cat:
                category = "Necessities"
            else:
                category = txn.get("category", "Other")
        if category not in VALID_CATEGORIES:
            category = txn.get("category", "Other")

        # Preserve frequency from existing subscription if not specified
        if not frequency:
            existing = next(
                (s for s in get_subscriptions()
                 if s["merchant"].lower() == sub_merchant.lower()),
                None
            )
            frequency = existing["frequency"] if existing else "Monthly"

        # Build a stream dict using the transaction's real data
        stream = {
            "merchant":         sub_merchant,
            "frequency":        frequency,
            "last_amount":      txn["amount"],
            "average_amount":   txn["amount"],
            "last_date":        txn["date"],
            "first_date":       txn["date"],
            "occurrence_count": "manual",
            "category":         category,
            "bank":             txn["bank"],
            "sub_type":         "utilities" if "UTILITIES" in txn.get("plaid_category", "") else "other_subscription",
            "is_active":        True,
            "variable_amount":  False,
            "tag":              tag,
        }

        # Preserve first_date from existing entry if one already exists
        existing_sub = next(
            (s for s in get_subscriptions()
             if s["merchant"].lower() == sub_merchant.lower()),
            None
        )
        if existing_sub and existing_sub.get("first_date"):
            stream["first_date"] = existing_sub["first_date"]
            # Use average of old average + new amount for rolling estimate
            old_avg = existing_sub.get("average_amount", txn["amount"])
            stream["average_amount"] = round((old_avg + txn["amount"]) / 2, 2)

        upsert_subscriptions([stream])
        all_streams = get_subscriptions()
        sync_subscriptions(all_streams)

        from sheets_client import _est_monthly
        monthly = _est_monthly(stream)
        tag_line = f"\n> Tag: _{tag}_" if tag else ""
        return (
            f"✅ **Updated subscription: {sub_merchant}**\n"
            f"> Amount: **${txn['amount']}** · {frequency} · ~${monthly}/mo\n"
            f"> Date: {txn['date']} · Bank: {txn['bank']} · Category: {category}{tag_line}\n"
            f"> Source transaction: `{transaction_id}`\n"
            f"> Google Sheet updated."
        )

    # ── recategorize ──────────────────────────────────────────────────────
    elif action == "recategorize":
        merchant = params.get("merchant", "").strip()
        category = params.get("category", "Other")
        if category not in VALID_CATEGORIES:
            return f"**{category}** isn't a valid category. Choose from: {', '.join(VALID_CATEGORIES)}"
        save_user_category(merchant, category)
        transaction_db.update_merchant_category(merchant, category)
        ytd = transaction_db.get_year_to_date_transactions()
        sync_transactions(ytd, BUDGET_LIMITS,
                          current_month=transaction_db.get_current_month_transactions())
        return f"🏷️ **{merchant}** recategorized to **{category}**. Google Sheet updated."

    # ── delete_transaction ────────────────────────────────────────────────
    elif action == "delete_transaction":
        ids = params.get("transaction_ids", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return "No transaction ID(s) found. Try mentioning the merchant name and date."
        deleted, not_found = [], []
        for tid in ids:
            if transaction_db.delete_transaction(tid):
                deleted.append(tid)
            else:
                not_found.append(tid)
        if deleted:
            ytd = transaction_db.get_year_to_date_transactions()
            sync_transactions(ytd, BUDGET_LIMITS,
                              current_month=transaction_db.get_current_month_transactions())
        lines = [f"🗑️ Deleted **{len(deleted)}** transaction(s)."]
        for tid in deleted:
            lines.append(f"> ✅ `{tid}`")
        for tid in not_found:
            lines.append(f"> ❌ Not found: `{tid}`")
        return "\n".join(lines)

    # ── annual_add ────────────────────────────────────────────────────────
    elif action == "annual_add":
        merchant = params.get("merchant", "").strip()
        annual_mgr.add_merchant(merchant)
        return f"📅 **{merchant}** marked as annual — cost will be divided by 12 in budget calculations."

    # ── annual_remove ─────────────────────────────────────────────────────
    elif action == "annual_remove":
        merchant = params.get("merchant", "").strip()
        annual_mgr.remove_merchant(merchant)
        return f"📅 **{merchant}** removed from annual merchants."

    # ── annual_list ───────────────────────────────────────────────────────
    elif action == "annual_list":
        merchants = annual_mgr.get_merchants()
        if not merchants:
            return "No annual merchants set."
        return "**Annual merchants (÷12):**\n" + "\n".join(f"> {m}" for m in sorted(merchants))

    # ── ask ───────────────────────────────────────────────────────────────
    elif action == "ask":
        question = params.get("question", "")
        monthly  = get_budget_summary(transaction_db.get_current_month_transactions())
        ytd      = transaction_db.get_year_to_date_transactions()
        try:
            streams = detect_subscriptions(ytd)
        except Exception:
            streams = []
        return answer_finance_question(question, monthly, ytd, streams)

    # ── budget ────────────────────────────────────────────────────────────
    elif action == "budget":
        summary = get_budget_summary(transaction_db.get_current_month_transactions())
        lines   = ["**Budget Status — This Month**\n"]
        for c in summary["categories"]:
            icon = "🔴" if c["over_budget"] else ("🟡" if c["pct_used"] >= 75 else "🟢")
            line = f"{icon} **{c['category']}**: ${c['spent']} / ${c['limit']} ({c['pct_used']}%)"
            if c["pending"] > 0:
                line += f" _(+${c['pending']} pending)_"
            lines.append(line)
        lines.append(f"\n**Total: ${summary['total_spent']} / ${summary['total_limit']}** "
                     f"— ${summary['total_remaining']} remaining")
        return "\n".join(lines)

    # ── subscriptions ─────────────────────────────────────────────────────
    elif action == "subscriptions":
        streams = get_subscriptions()
        if not streams:
            return "No subscriptions in the database yet. Run `/sync` first."
        from sheets_client import _est_monthly
        lines = ["**Subscriptions**\n"]
        total = 0
        for s in streams:
            m = _est_monthly(s)
            total += m
            days = s.get("days_until_charge")
            next_str = s.get("next_charge_date", "")
            charge = f" · next {next_str}" if next_str else ""
            if days is not None and days <= 7 and next_str:
                charge = f" 🔔 in {days}d" if days > 0 else (" 🔔 **today**" if days == 0 else f" ⚠️ overdue {abs(days)}d")
            lines.append(f"**{s['merchant']}** — ${s['last_amount']} {s['frequency']} (~${m}/mo){charge}")
        lines.append(f"\n**Est. monthly total: ${round(total, 2)}**")
        return "\n".join(lines)

    # ── status ────────────────────────────────────────────────────────────
    elif action == "status":
        statuses = fetch_all_item_statuses()
        lines    = ["**Bank Connection Status**\n"]
        for s in statuses:
            if s["healthy"]:
                lines.append(f"✅ **{s['bank']}** — Healthy")
            else:
                lines.append(f"⚠️ **{s['bank']}** — `{s['error_code']}`: {s['error_message']}")
        return "\n".join(lines)

    # ── sync ──────────────────────────────────────────────────────────────
    elif action == "sync":
        return "🔄 Starting sync... (use `/sync` command — it shows live progress)"

    # ── unknown ───────────────────────────────────────────────────────────
    else:
        return params.get("clarification", "Sorry, I didn't understand that request.")


def _extract_merchant_mentions(text: str, cache: dict) -> tuple[str, list[str]]:
    """
    Scan the message for @MerchantName tokens (not the bot mention — those are
    already stripped by this point).  Returns:
      - cleaned text with @tokens replaced by plain merchant names
      - list of explicitly-mentioned merchant names (exact or fuzzy-matched)

    Examples:
      "@Spotify"        → matched to "Spotify" in cache, or kept as "Spotify"
      "@Harris_County"  → underscores replaced with spaces → "Harris County"
      "@netflix"        → case-insensitive match against known merchants
    """
    import re
    pattern = r'@([\w]+(?:[\w]*)?)'   # @word, allows letters/digits/underscores
    found   = re.findall(pattern, text)

    mentioned = []
    cleaned   = text

    cache_lower = {k.lower(): k for k in cache.keys()}

    for token in found:
        # Normalise: underscores → spaces, title-case
        normalised = token.replace("_", " ").strip()

        # Try exact cache match (case-insensitive)
        matched = cache_lower.get(normalised.lower())

        # Fuzzy fallback: find any cache key that contains the token
        if not matched:
            for key_lower, key in cache_lower.items():
                if normalised.lower() in key_lower or key_lower in normalised.lower():
                    matched = key
                    break

        merchant = matched or normalised   # use cache name or raw token
        mentioned.append(merchant)

        # Replace @token in the text with the resolved merchant name
        cleaned = cleaned.replace(f"@{token}", merchant)

    return cleaned.strip(), mentioned


@bot.event
async def on_message(message: discord.Message):
    """Handle @ mentions as natural language commands via Gemini."""
    if message.author.bot:
        return

    # Ignore messages that don't mention this bot
    if bot.user not in message.mentions:
        return

    # Strip the bot @mention and whitespace
    text = message.content
    for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        text = text.replace(mention, "")
    text = text.strip()

    if not text:
        await message.reply(
            "Hey! Mention me with a request and I'll take care of it. Examples:\n"
            "> @BudgetBot add @Netflix monthly $15.99\n"
            "> @BudgetBot how much did I spend at @Starbucks this month?\n"
            "> @BudgetBot remove @OpenAI from subscriptions\n"
            "> @BudgetBot recategorize @Uber as Travel\n"
            "> @BudgetBot show my budget"
        )
        return

    # Show hourglass immediately so the user knows we're working
    await message.add_reaction("⏳")

    async def _fail(reason: str):
        """Remove the hourglass, add ❌, and reply with the error."""
        try:
            await message.remove_reaction("⏳", bot.user)
        except Exception:
            pass
        await message.add_reaction("❌")
        await message.reply(f"❌ {reason}")

    try:
        async with message.channel.typing():
            # Build context for Gemini
            cache   = _load_cache()
            ytd     = transaction_db.get_year_to_date_transactions()
            streams = []
            try:
                streams = get_subscriptions()
            except Exception:
                pass

            # Extract @Merchant mentions → clean text + explicit merchant list
            text, mentioned_merchants = _extract_merchant_mentions(text, cache)

            context = {
                "merchants":           list(cache.keys()),
                "categories":          VALID_CATEGORIES,
                "subscriptions":       [s["merchant"] for s in streams],
                "annual_merchants":    list(annual_mgr.get_merchants()),
                "banks":               list(PLAID_ACCESS_TOKENS.keys()),
                "recent_txns":         transaction_db.get_recent_transactions(limit=30),
                "mentioned_merchants": mentioned_merchants,   # explicit @mentions
            }

            channel_id = message.channel.id
            history    = _channel_history.get(channel_id, [])

            try:
                parsed = parse_nl_command(text, context, history=history)
            except Exception as e:
                await _fail(f"Gemini is temporarily unavailable. Try again in a moment.\n_Error: {e}_")
                return

            clarification = parsed.get("clarification")
            actions       = parsed.get("actions", [])

            # If Gemini needs clarification and has no actions, just ask
            if clarification and not actions:
                await message.remove_reaction("⏳", bot.user)
                await message.reply(clarification)
                return

            # If add_subscription with Annual is present, drop any redundant annual_add
            # for the same merchant — upsert_subscriptions handles proration automatically.
            annual_sub_merchants = {
                item["params"].get("merchant", "").lower()
                for item in actions
                if item.get("action") == "add_subscription"
                and item.get("params", {}).get("frequency") == "Annual"
            }
            actions = [
                item for item in actions
                if not (
                    item.get("action") == "annual_add"
                    and item.get("params", {}).get("merchant", "").lower() in annual_sub_merchants
                )
            ]

            # Execute each action and collect results
            results      = []
            had_error    = False
            for item in actions:
                action_name = item.get("action", "unknown")
                params      = item.get("params", {})

                try:
                    result = await _dispatch_action(action_name, params)
                except Exception as e:
                    result     = f"❌ Error running **{action_name}**: {e}"
                    had_error  = True

                results.append(result)

            # Special case: if the only action is "sync", actually trigger it
            if len(actions) == 1 and actions[0].get("action") == "sync":
                await message.remove_reaction("⏳", bot.user)
                await message.reply("🔄 Starting a full sync...")
                await run_full_sync(source=f"@mention by {message.author.display_name}")
                return

            # Remove hourglass — done processing
            try:
                await message.remove_reaction("⏳", bot.user)
            except Exception:
                pass

            # Add ❌ if any action failed
            if had_error:
                await message.add_reaction("❌")

            # Reply — split into chunks if needed
            if clarification:
                results.append(f"_Note: {clarification}_")

            full_reply = "\n\n".join(results)

            # Save this exchange to per-channel history (trim to last N exchanges)
            if not had_error:
                history = _channel_history.get(channel_id, [])
                history = (history + [(text, full_reply)])[-MAX_HISTORY_EXCHANGES:]
                _channel_history[channel_id] = history

            if len(full_reply) <= 2000:
                await message.reply(full_reply)
            else:
                chunks, remaining = [], full_reply
                while remaining:
                    chunks.append(remaining[:2000])
                    remaining = remaining[2000:]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk)
                    else:
                        await message.channel.send(chunk)

    except Exception as e:
        # Catch-all for any unexpected top-level error
        await _fail(f"Something went wrong: {e}")

    # Still process slash commands if any
    await bot.process_commands(message)


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


@tasks.loop(time=datetime.time(hour=SUBSCRIPTION_REMINDER_HOUR, minute=0, tzinfo=TZ))
async def subscription_reminder():
    """
    Every morning: refresh next-charge dates and send alerts for any subscription
    charging today or tomorrow.
    """
    refresh_next_charge_dates()
    subs = get_subscriptions()

    today_charges    = [s for s in subs if s.get("days_until_charge") == 0]
    tomorrow_charges = [s for s in subs if s.get("days_until_charge") == 1]

    if not today_charges and not tomorrow_charges:
        return

    alert_channel  = bot.get_channel(DISCORD_ALERT_CHANNEL_ID)
    digest_channel = bot.get_channel(DISCORD_CHANNEL_ID)
    target         = alert_channel or digest_channel
    if not target:
        return

    from sheets_client import _est_monthly, _billing_cycle

    for s in today_charges:
        is_annual   = _billing_cycle(s["frequency"]) == "Annual"
        amount_line = (f"**${s['last_amount']}/year** _(~${_est_monthly(s)}/mo)_"
                       if is_annual else f"**${s['last_amount']}**")
        await target.send(
            f"🔔 **Subscription Charging Today — {s['merchant']}**\n"
            f"> Amount: {amount_line} · {s['frequency']}\n"
            f"> Category: {s['category']} · Bank: {s['bank']}"
        )

    for s in tomorrow_charges:
        is_annual   = _billing_cycle(s["frequency"]) == "Annual"
        amount_line = (f"**${s['last_amount']}/year** _(~${_est_monthly(s)}/mo)_"
                       if is_annual else f"**${s['last_amount']}**")
        await target.send(
            f"⏰ **Upcoming Charge Tomorrow — {s['merchant']}**\n"
            f"> Amount: {amount_line} · {s['frequency']}\n"
            f"> Charging on: {s['next_charge_date']}\n"
            f"> Category: {s['category']} · Bank: {s['bank']}"
        )

    print(f"[reminder] Sent {len(today_charges)} today + {len(tomorrow_charges)} tomorrow alerts")

@tasks.loop(time=datetime.time(hour=DIGEST_MORNING_HOUR, minute=0, tzinfo=TZ))
async def morning_digest():
    """
    9:00 AM — Monthly budget overview for the day.
    Shows how the month is tracking, subscriptions, and any pending review items.
    Reads from DB (the 9 AM sync already populated fresh data).
    """
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

    print("[scheduler] Posting morning digest...")
    current_month = transaction_db.get_current_month_transactions()
    summary       = get_budget_summary(current_month)

    try:
        digest = generate_budget_summary(summary)
    except Exception as e:
        digest = f"_(Gemini unavailable: {e})_"

    # Subscription summary
    streams      = get_subscriptions()
    refresh_next_charge_dates()
    try:
        subs_summary = generate_subscription_summary(streams)
    except Exception:
        subs_summary = f"{len(streams)} active subscription(s)."

    today_label = datetime.datetime.now(TZ).strftime("%A, %B %-d")
    await channel.send(
        f"☀️ **Good Morning — {today_label}**\n\n"
        f"{digest}\n\n"
        f"**Subscriptions:** {subs_summary}\n\n"
        f"_Posted: {summary['transaction_count']['posted']} txns · "
        f"Pending: {summary['transaction_count']['pending']} txns_"
    )

    # Re-prompt any still-unreviewed uncertain merchants
    pending_uncertain = get_pending_uncertain()
    if pending_uncertain:
        review_channel = bot.get_channel(DISCORD_REVIEW_CHANNEL_ID) or channel
        await send_uncertain_prompts(review_channel, pending_uncertain)


@tasks.loop(time=datetime.time(hour=DIGEST_EVENING_HOUR, minute=DIGEST_EVENING_MINUTE, tzinfo=TZ))
async def evening_digest():
    """
    11:59 PM — End-of-day spending recap.
    Shows every transaction posted today, per-category totals, and where
    the month stands heading into tomorrow.
    """
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

    print("[scheduler] Posting evening digest...")
    today_txns    = get_today_transactions()
    current_month = transaction_db.get_current_month_transactions()
    summary       = get_budget_summary(current_month)

    posted  = [t for t in today_txns if not t["pending"] and t["category"] != "Excluded"]
    pending = [t for t in today_txns if t["pending"]]

    today_label  = datetime.datetime.now(TZ).strftime("%A, %B %-d")
    today_total  = round(sum(t["amount"] for t in posted), 2)

    # Per-category totals for today
    by_cat: dict[str, float] = {}
    for t in posted:
        by_cat[t["category"]] = round(by_cat.get(t["category"], 0) + t["amount"], 2)

    # Build the transaction list section
    if posted:
        txn_lines = []
        for t in sorted(posted, key=lambda x: -x["amount"]):
            txn_lines.append(f"> **{t['merchant']}** — ${t['amount']} · {t['category']} · {t['bank']}")
        txn_block = "\n".join(txn_lines)
    else:
        txn_block = "> _No posted transactions today._"

    # Category summary for today
    if by_cat:
        cat_lines = "  ".join(
            f"**{cat}** ${amt}" for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1])
        )
    else:
        cat_lines = "_none_"

    # Gemini narrative
    try:
        narrative = generate_evening_digest(today_txns, summary)
    except Exception as e:
        narrative = f"_(Gemini unavailable: {e})_"

    # Month budget bar
    month_lines = []
    for c in summary["categories"]:
        if c["spent"] > 0:
            icon = "🔴" if c["over_budget"] else ("🟡" if c["pct_used"] >= 75 else "🟢")
            month_lines.append(
                f"{icon} **{c['category']}**: ${c['spent']} / ${c['limit']} ({c['pct_used']}%)"
            )

    pending_note = f"\n> _{len(pending)} pending transaction(s) not yet settled._" if pending else ""

    msg = (
        f"🌙 **End of Day — {today_label}**\n\n"
        f"**Today's Spending: ${today_total}**\n"
        f"{txn_block}{pending_note}\n\n"
        f"**By Category Today:** {cat_lines}\n\n"
        f"{narrative}\n\n"
        f"**Month So Far:**\n" + "\n".join(month_lines) + "\n" +
        f"\n_Total: **${summary['total_spent']}** / ${summary['total_limit']} "
        f"· ${summary['total_remaining']} remaining_"
    )

    # Split into chunks if needed (Discord 2000 char limit)
    if len(msg) <= 2000:
        await channel.send(msg)
    else:
        chunks, remaining = [], msg
        while remaining:
            chunks.append(remaining[:2000])
            remaining = remaining[2000:]
        for chunk in chunks:
            await channel.send(chunk)

# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"[bot] Logged in as {bot.user} ({bot.user.id})")
    transaction_db.init_db()
    cleaned = transaction_db.deduplicate_transactions()
    if cleaned:
        print(f"[bot] Startup cleanup: removed {cleaned} duplicate transaction(s)")
    refresh_next_charge_dates()

    # Start Plaid webhook receiver + re-auth server
    try:
        await start_webhook_server(on_plaid_webhook, public_base_url=_public_base_url)
    except Exception as e:
        print(f"[bot] Warning: webhook server failed to start: {e}")

    try:
        guild  = discord.Object(id=DISCORD_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"[bot] Synced {len(synced)} slash commands to guild")
    except Exception as e:
        print(f"[bot] Failed to sync commands: {e}")

    if not scheduled_sync.is_running():
        scheduled_sync.start()
    if not morning_digest.is_running():
        morning_digest.start()
    if not evening_digest.is_running():
        evening_digest.start()
    if not subscription_reminder.is_running():
        subscription_reminder.start()

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel:
        await channel.send("BudgetTrackerAI is online. Type `/sync` to sync now or `/budget` to check your budget.")

def run():
    bot.run(DISCORD_BOT_TOKEN)
