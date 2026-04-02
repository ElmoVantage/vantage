# Vantage Tracker — Setup & Operation Guide

## Table of Contents

1. [Overview](#overview)
2. [Installation & Prerequisites](#installation--prerequisites)
3. [Configuration](#configuration)
   - [.env File (Secrets)](#env-file-secrets)
   - [settings.ini (App Config)](#settingsini-app-config)
4. [IMAP Email Accounts](#imap-email-accounts)
5. [Carrier Tracking APIs](#carrier-tracking-apis)
6. [Discord Setup](#discord-setup)
7. [Database](#database)
8. [Running the Application](#running-the-application)
9. [GUI — Tab by Tab](#gui--tab-by-tab)
   - [Order Tracker](#order-tracker-tab)
   - [Account Health](#account-health-tab)
   - [Inventory](#inventory-tab)
   - [Outbound Sales](#outbound-sales-tab)
   - [Expenses](#expenses-tab)
   - [Dashboard](#dashboard-tab)
   - [Settings](#settings-tab)
10. [Email Sync & Retailer Parsers](#email-sync--retailer-parsers)
11. [Shipment Tracking](#shipment-tracking)
12. [Discord Webhooks & Bot](#discord-webhooks--bot)
13. [Recurring Expenses & Reminders](#recurring-expenses--reminders)
14. [Database Backups & Migrations](#database-backups--migrations)
15. [File Reference](#file-reference)
16. [Troubleshooting](#troubleshooting)

---

## Overview

Vantage is a desktop resale business management tool built with PyQt6 and SQLite. It:

- Scrapes order confirmation, shipping, and delivery emails from multiple IMAP accounts (Gmail, AOL, etc.) and imports them automatically into a database
- Tracks shipment status in real time via UPS, FedEx, and USPS carrier APIs
- Manages inbound inventory, outbound sales, and business expenses
- Calculates profit and margin on every sale
- Sends Discord notifications for orders, deliveries, payment issues, daily forecasts, and more
- Provides a Discord bot with slash commands for adding inventory and recording sales remotely

Supported retailers for automatic email parsing: **Amazon, Walmart, Target, Nike, Best Buy, Pokémon Center, Five Below, Topps, eBay (sales), StockX (sales/listings), and any Shopify-powered store.**

---

## Installation & Prerequisites

### Requirements

- Python 3.11+
- Windows 10/11 (tested) or macOS

### Install Dependencies

```bash
cd C:\Users\Houston\Desktop\Vantage
pip install -r requirements.txt
```

Key packages used:

| Package | Purpose |
|---------|---------|
| `PyQt6` | Desktop GUI framework |
| `beautifulsoup4` | HTML email parsing |
| `requests` | Carrier API calls, Discord webhooks |
| `python-dotenv` | Load `.env` file |
| `matplotlib` | Pie charts in the dashboard |
| `discord.py` | Discord bot slash commands |

---

## Configuration

Vantage uses two config files in the same folder as the app:

| File | Purpose |
|------|---------|
| `.env` | **Secrets only** — API keys, passwords, tokens, webhook URLs |
| `settings.ini` | **App behavior** — DB path, backup settings, expense thresholds |

### .env File (Secrets)

Create a file named `.env` in `C:\Users\Houston\Desktop\Vantage\` with the following:

```dotenv
# ── Discord ────────────────────────────────────────────────────────────────────
# Webhook URL for order/delivery/sale/payment notifications
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN

# Bot token for slash commands (/addinventory, /addsale, /ordersummary, /inventory)
DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN

# ── IMAP Email Accounts ────────────────────────────────────────────────────────
# JSON array — supports Gmail, AOL, Outlook, and any IMAP provider
# Gmail: use App Password (not your real password). AOL: use App Password too.
# host and port are optional; defaults are imap.gmail.com:993
IMAP_ACCOUNTS=[
  {"label": "Main Gmail", "user": "you@gmail.com", "pass": "xxxx xxxx xxxx xxxx", "host": "imap.gmail.com", "port": 993},
  {"label": "AOL Account", "user": "you@aol.com",  "pass": "xxxx xxxx xxxx xxxx", "host": "imap.aol.com",  "port": 993}
]

# ── Carrier Tracking APIs ──────────────────────────────────────────────────────
# UPS — register at https://developer.ups.com
UPS_CLIENT_ID=
UPS_CLIENT_SECRET=

# FedEx — register at https://developer.fedex.com
FEDEX_CLIENT_ID=
FEDEX_CLIENT_SECRET=
FEDEX_SANDBOX=False

# USPS — register at https://developer.usps.com
USPS_CLIENT_ID=
USPS_CLIENT_SECRET=
```

> **Note:** Any `.env` value can also be set as a real environment variable. The file is read at startup with `python-dotenv`.

### settings.ini (App Config)

Create `settings.ini` in the same folder:

```ini
[database]
; Path to SQLite database file. Can be absolute or relative to the app folder.
path = tracker.db

[backup]
; Automatic backup on every app startup
enabled = true
; Where to save backup files (absolute path recommended)
directory = C:\Users\Houston\Desktop\Vantage\backups
; Maximum number of backup files to keep (oldest deleted automatically)
max_keep = 10

[expenses]
; Automatically log recurring expenses when they come due on startup
auto_log_recurring = true
; Send a Discord webhook whenever a recurring expense is auto-logged
notify_discord_on_recurring = true
; Amount (in cents) that triggers a "Large Expense" Discord alert
; Default 10000 = $100.00
large_expense_alert_threshold = 10000
; Folder where tax CSV exports are saved
tax_export_path = exports

[notifications]
; Send a monthly expense summary webhook on the 1st of each month
post_monthly_expense_summary = true
```

> **Overriding via environment variables:** Any `settings.ini` key can be overridden by setting `TRACKER_<SECTION>_<KEY>` as an environment variable. For example, `TRACKER_DATABASE_PATH=D:\data\tracker.db` overrides `[database] path`.

---

## IMAP Email Accounts

Vantage scans your email accounts for retailer order emails every time the app starts and on a background sync schedule.

### Gmail Setup

1. Go to **Google Account → Security → 2-Step Verification** — enable it if not already on
2. Go to **Google Account → Security → App passwords**
3. Create a new app password (select "Mail" and "Windows Computer")
4. Copy the 16-character password (e.g., `abcd efgh ijkl mnop`)
5. Use that as the `pass` value in `IMAP_ACCOUNTS`

```json
{"label": "Gmail", "user": "you@gmail.com", "pass": "abcd efgh ijkl mnop", "host": "imap.gmail.com", "port": 993}
```

### AOL / Yahoo Setup

1. Go to **AOL Account Security** (or Yahoo Account Security)
2. Generate an **App Password** for "Other app"
3. Use that password in `IMAP_ACCOUNTS`

```json
{"label": "AOL", "user": "you@aol.com", "pass": "xxxxxxxxxxxx", "host": "imap.aol.com", "port": 993}
```

### Multiple Accounts

You can add as many accounts as you want in the JSON array:

```dotenv
IMAP_ACCOUNTS=[
  {"label": "Main", "user": "main@gmail.com",  "pass": "app-password-1", "host": "imap.gmail.com", "port": 993},
  {"label": "Alt1", "user": "alt1@gmail.com",  "pass": "app-password-2", "host": "imap.gmail.com", "port": 993},
  {"label": "AOL",  "user": "name@aol.com",    "pass": "app-password-3", "host": "imap.aol.com",  "port": 993}
]
```

> Each account is scanned independently. Duplicate orders across accounts are automatically deduplicated by order number.

### What Mailboxes Are Scanned

For each account, Vantage scans:
- `INBOX`
- `[Gmail]/Spam` (catches filtered order emails)
- `[Gmail]/All Mail`

---

## Carrier Tracking APIs

Tracking requires registering for free developer accounts at each carrier. All APIs use OAuth2 (tokens are cached in memory and auto-refreshed).

### FedEx

1. Go to [developer.fedex.com](https://developer.fedex.com)
2. Create an account → Create a new project → Select "Track" API
3. Copy **Client ID** and **Client Secret** into `.env`

```dotenv
FEDEX_CLIENT_ID=l7xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEDEX_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEDEX_SANDBOX=False
```

> Set `FEDEX_SANDBOX=True` during testing. Production (`False`) is required for real tracking numbers.

### UPS

1. Go to [developer.ups.com](https://developer.ups.com)
2. Create an account → Add Apps → Select "Tracking" → Get credentials

```dotenv
UPS_CLIENT_ID=your_client_id
UPS_CLIENT_SECRET=your_client_secret
```

### USPS

1. Go to [developer.usps.com](https://developer.usps.com)
2. Create an account → Register an app → Add "Tracking v3" API

```dotenv
USPS_CLIENT_ID=your_client_id
USPS_CLIENT_SECRET=your_client_secret
```

### Carrier Auto-Detection

Tracking numbers are automatically assigned to the correct carrier by pattern matching:

| Carrier | Pattern |
|---------|---------|
| UPS | Starts with `1Z`, 18 characters total |
| FedEx | 12, 15, or 20 digits |
| USPS | 20–22 digits, often starts with 9400/9205/9261/9274/9300 |

If you leave a carrier's credentials empty, tracking for that carrier is silently skipped (no error, just no updates).

---

## Discord Setup

Vantage uses Discord in two ways: a **webhook** (one-way notifications) and a **bot** (interactive slash commands).

### Webhook (Notifications Only)

Webhooks send embeds to a Discord channel when events occur (orders, deliveries, payment issues, etc.).

1. Open your Discord server → go to the channel you want notifications in
2. Click **Edit Channel → Integrations → Webhooks → New Webhook**
3. Copy the **Webhook URL**
4. Paste into `.env`:

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/abcdefghij...
```

### Bot (Slash Commands)

The bot supports `/addinventory`, `/addsale`, `/ordersummary`, and `/inventory`.

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. **New Application** → give it a name
3. Go to **Bot** tab → **Add Bot** → copy the **Token**
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Use Slash Commands`
5. Open the generated URL to invite the bot to your server
6. Paste the token into `.env`:

```dotenv
DISCORD_BOT_TOKEN=MTQ4MzQyMjE2MTI0M...
```

7. Start the bot (runs separately from the GUI):

```bash
python discord_bot.py
```

### Webhook Events Reference

| Event | When It Fires |
|-------|--------------|
| New Order | Email sync imports a new inbound order |
| Inventory Added | Item moved to inventory (order delivered) |
| Sale Recorded | New outbound sale added |
| Profit Summary | 1st of every month (if enabled in settings.ini) |
| Recurring Expense | When a recurring expense auto-logs on startup |
| Large Expense | When an expense exceeds the threshold (default $100) |
| Return Reminder | Daily check, fires when return window is approaching |
| Daily Delivery Forecast | Every morning at 7:00 AM — packages expected today |
| Tracking Poll Summary | After each carrier API refresh cycle |
| Amazon Payment Issue | Amazon payment declined or verification needed |

---

## Database

The database is SQLite, stored at the path set in `settings.ini` (default: `tracker.db` in the app folder).

### Tables Overview

| Table | Purpose |
|-------|---------|
| `inbound_orders` | Orders placed with retailers (header: order number, status, tracking, address) |
| `inbound_order_items` | Line items within each inbound order (name, SKU, cost, quantity) |
| `inventory` | In-stock items available for sale |
| `outbound_sales` | Completed sales with profit/margin data |
| `sale_inventory_links` | Bridge table linking one sale to multiple inventory items |
| `listings` | Active platform listings (eBay, StockX, etc.) |
| `business_expenses` | One-time and recurring business expenses |
| `return_reminders` | Return window tracking per inventory item |
| `schema_migrations` | Migration version log |

### Money Storage

All dollar amounts are stored as **integer cents** (e.g., $29.99 → 2999). This prevents floating-point rounding errors. Displayed values are always formatted back to dollars in the UI.

### Migrations

The schema is versioned. On every startup, Vantage checks `schema_migrations` and applies any new migrations automatically. You never need to run migrations manually.

---

## Running the Application

### Start the GUI

```bash
cd C:\Users\Houston\Desktop\Vantage
python main.py
```

Or, if there's no `main.py`:

```bash
python gui_app.py
```

### Start the Discord Bot (Optional, Separate Process)

```bash
python discord_bot.py
```

Run this in a separate terminal window. It does not affect the GUI.

### Startup Sequence

When the app launches, it:

1. Loads `.env` and `settings.ini`
2. Backs up the database (if backup is enabled)
3. Runs any pending schema migrations
4. Auto-logs any recurring expenses that are due
5. Opens the main window
6. Immediately runs an email sync in the background
7. Schedules a 7:00 AM daily delivery forecast webhook
8. Sets up a 1-hour return reminder check timer

---

## GUI — Tab by Tab

### Order Tracker Tab

The main hub for all inbound purchases. Shows three sub-tabs:

#### Order Tracker (Sub-tab 1)
Displays all orders as **drop cards** — one card per unique item name, showing:
- Item name, drop date, total units, total cost
- Retailer color-coded (Walmart blue, Target red, Amazon orange, etc.)

**Left-click** a card to open the order detail dialog, which shows every individual order for that item with columns: Order #, Retailer, Date, Status, Cost/Unit, Qty, Tracking, Ship-To address.

**Right-click** a card for:
- Mark all orders in this group as a specific status
- Delete individual orders or the entire group
- Set a return reminder

**Search bar** at the top filters cards by item name in real time.

**Manual Scrape button**: Opens a dialog to re-scrape emails for a specific retailer going back N days. Use this to import older orders or fix missing data.

**Export CSV**: Downloads all inbound orders to a spreadsheet.

**+ Add Order**: Manually create an inbound order without an email.

#### Inbound Shipments (Sub-tab 2)
Table view of all orders with status = "shipped". Columns: Item, Retailer, Tracking #, Ship To, Est. Delivery.

- **Click** a tracking number to open it in the carrier's tracking page in your browser
- **Refresh Tracking** button: triggers an immediate carrier API check on all shipped orders
- The calendar on the right highlights dates that have packages arriving; click a date to see what's expected

#### Pending Pickups (Sub-tab 3)
Grid of delivered packages grouped by delivery address. Each card shows the address and a list of items. Use the **Picked Up** button on each card to mark items as received and move them to inventory.

---

### Account Health Tab

Shows a breakdown of your order performance by retailer and email account:

- Total orders placed per retailer
- Shipped % and Cancelled % 
- Identifies accounts with unusually high cancellation rates (QLA cancels, payment issues)

This helps identify which buying accounts are getting flagged by retailers.

---

### Inventory Tab

Three sub-tabs manage your sellable stock:

#### Inventory (Sub-tab 1)
Table of all in-stock items. Columns: Item, Category, Qty, Condition, Cost Basis, Storage Location, Date Received.

- **+ Add Item**: Add inventory manually (without an order)
- **Double-click** a row to edit: category, quantity, condition, cost, location, SKU, size variant
- **Right-click** for: delete, link to order, view linked orders
- The **pie chart** in the corner shows units by category

#### Listings (Sub-tab 2)
Active platform listings (eBay, StockX, Mercari, etc.). Columns: Item, Platform, Listing Price, Status, Date Listed, Est. Payout.

- **Add Listing**: Link to an inventory item, set platform and price
- Listings automatically move to "sold" when a sale email is detected
- **End Listing**: Mark a listing as ended without a sale

#### Sales Records (Sub-tab 3)
The same data as the Outbound Sales tab, presented from the inventory perspective.

---

### Outbound Sales Tab

Full view of all sales. Two sub-tabs:

#### Sales (Sub-tab 1)
Columns: Item, Platform, Sale Price, Fees, Shipping, Cost, Profit, Margin %, Qty, Tracking, Date Sold.

- Profit shown in **green** (positive) or **red** (negative)
- **+ Add Sale**: Record a sale manually; choose inventory item to link
- **Double-click** to edit: price, fees, shipping, platform, tracking, buyer info
- **Right-click**: delete, unlink inventory, view linked inventory
- When you link a sale to inventory, the inventory quantity decreases automatically. Unlinking restores it.

#### Listings (Sub-tab 2)
Same as the Listings sub-tab in Inventory — shown here for convenience when working in the sales context.

**Reports / Export**: The export button downloads all sales to CSV. Headers include item, platform, sale price, fees, shipping, cost, profit, margin, date sold.

---

### Expenses Tab

Tracks all business costs: fees, shipping supplies, storage, software subscriptions, etc.

**Columns**: Name, Category, Amount, Date, Vendor, Payment Method, Tax Deductible, Is Recurring.

- **+ Add Expense**: Opens dialog with fields for name, category, amount, date, vendor, payment method, tax-deductible flag, and optional recurrence settings
- **Recurring Expenses**: Set an interval (daily/weekly/biweekly/monthly/quarterly/yearly) and Vantage will automatically log the expense each time it comes due on startup
- **Double-click** to edit any expense
- **Export**: Downloads filtered expense data to CSV for tax reporting

The **status bar** at the bottom always shows MTD (month-to-date) and YTD (year-to-date) expense totals.

---

### Dashboard Tab

High-level business performance summary. Widgets include:

- **Revenue, Profit, Margin %** for the selected period
- **Units In Stock** and **Inventory Value**
- **Expenses MTD / YTD**
- **Net Profit** (revenue minus COGS minus expenses)
- **Orders by Retailer** — pie chart showing which retailers you buy from most
- **Sales by Platform** — pie chart showing eBay vs StockX vs Direct, etc.
- **Stick Rate** — percentage of inbound orders that weren't cancelled
- **Recent Activity** — timeline of recent orders, sales, and expense events

**Period selector** at the top lets you switch between Last 7 Days, Last 30 Days, This Month, This Year, and All Time.

---

### Settings Tab

- View and edit the database file path
- Enable/disable backups, set backup folder and retention count
- Test the Discord webhook (sends a test embed)
- Force a full email sync (clears the sync state to re-process all emails)
- View the database migration version log

---

## Email Sync & Retailer Parsers

### How Sync Works

On startup and periodically in the background, Vantage:

1. Connects to each IMAP account
2. Searches for emails from known retailer addresses since the last sync date
3. Downloads matching emails (HTML + plain text parts)
4. Identifies the retailer from the From address or Subject
5. Passes the email to the retailer-specific parser
6. Upserts the result into the database (deduplicates by order number + item name)
7. Saves the sync state so processed emails aren't re-imported

Emails within each sync batch are sorted **oldest-first** so that confirmation emails (with prices and dates) are always processed before shipping emails.

### Manual Scrape

Use **Manual Scrape** to re-import emails for a specific retailer going back N days. This ignores the skip list and will re-process already-seen emails, patching any missing data (prices, item names, dates).

### Supported Retailers

#### Inbound (Purchases)

| Retailer | Email Address Pattern | Email Types Handled |
|----------|----------------------|---------------------|
| **Amazon** | `@amazon.com`, `auto-confirm@amazon.com`, `order-update@amazon.com`, `qla@amazon.com` | Order confirmation ("Ordered: ..."), Shipped, Delivered, Cancelled, QLA cancel, Payment declined |
| **Walmart** | `walmart.com` | Confirmation, Shipped, Delivered, Cancelled |
| **Target** | `target.com` | Confirmation, Shipped, Delivered, Cancelled |
| **Nike** | `nike.com`, `notifications.nike.com` | Confirmation, Shipped (Narvar), Delivered, Cancelled |
| **Best Buy** | `emailinfo.bestbuy.com` | Thanks for your order, On its way, Delivered, Cancelled |
| **Pokémon Center** | `pokemoncenter.com`, Narvar delivery | Confirmation, On its way, Delivered, Cancelled |
| **Five Below** | `fivebelow.com` | Confirmation, Shipped (Narvar), Cancelled |
| **Topps** | `official.topps.com` (Shopify) | Confirmation, Shipped, Delivered, Cancelled |
| **Shopify (any store)** | `shopifyemail.com` | Confirmation, Shipped, Delivered — store name auto-detected from From header |

#### Outbound (Sales)

| Platform | Email Address | Email Types Handled |
|----------|--------------|---------------------|
| **eBay** | `ebay.com` | "You made the sale" (new sale), "eBay shipping label" (updates shipping cost + tracking), Listing live |
| **StockX** | `stockx.com` | "Your Ask Is Live" (new listing), "You Sold Your Item" (sale confirmed) |

### Special Cases

**Amazon Order Confirmation Format**: Amazon confirmation subjects look like `Ordered: 5 "Pokémon TCG: First Partner..."` (base64 encoded). Vantage matches these by the FROM address (`amazon.com`) rather than the subject, so they are always found.

**Amazon Payment Issues**: If Amazon sends a payment declined or payment verification required email, Vantage sets a flag and fires a **Discord webhook** with the order number, item name, and account email. These emails do NOT create orders in the tracker.

**Shopify Catch-All**: Any email from `shopifyemail.com` that isn't already matched to a known store (Topps, etc.) is handled by the generic Shopify parser, which auto-detects the store name from the From header display name.

**Multi-Item Orders**: All parsers support multi-item orders. Each line item becomes a separate row in `inbound_order_items`, each with its own cost and quantity.

---

## Shipment Tracking

Tracking is checked either manually (Refresh Tracking button) or automatically when you switch to the Inbound Shipments tab (if no refresh has happened in the last 30 minutes).

### Status Progression

Orders move through statuses in this order:

```
ordered → shipped → delivered
               ↘ cancelled / returned
```

Status only moves forward — a "delivered" order will not be downgraded back to "shipped".

### Estimated Delivery

When a carrier returns an estimated delivery date, it's stored and shown in the Inbound Shipments table and highlighted on the calendar.

### After-Tracking Refresh

After all carriers are polled, a single **Tracking Poll Summary** Discord webhook is sent with:
- Number of packages checked
- Number of errors
- Packages newly delivered (grouped by delivery address)
- Other status changes (e.g., "In transit", "Out for delivery")

---

## Discord Webhooks & Bot

### Webhook Color Guide

| Color | Meaning |
|-------|---------|
| 🟦 Blue | New order, tracking update, general info |
| 🟩 Green | Sale recorded (profit), delivery confirmed |
| 🟥 Red | Sale (loss), payment issue, return reminder due |
| 🟧 Orange | Out for delivery, recurring expense |
| 🟨 Yellow | Large expense warning |

### Bot Slash Commands

#### `/addinventory`
Add a new item to inventory directly from Discord.

Parameters:
- `item_name` *(required)*: Name of the item
- `category` *(required)*: e.g., "Pokemon Cards", "Plush", "Electronics"
- `cost_basis` *(required)*: What you paid (dollars, e.g. `29.99`)
- `quantity` *(required)*: Number of units
- `condition`: new / used / open box / like new / for parts (default: new)
- `sku`: Optional SKU/barcode
- `size_variant`: e.g., "XL", "Red", "1st Edition"
- `storage_location`: Where it's stored physically
- `date_received`: Date format YYYY-MM-DD (defaults to today)

#### `/addsale`
Record a completed sale.

Parameters:
- `inventory_id` *(required)*: The inventory item ID (use `/inventory` to look it up)
- `platform` *(required)*: ebay / stockx / mercari / offerup / direct / local / other
- `sale_price` *(required)*: What it sold for (dollars)
- `fees`: Platform fees (dollars)
- `shipping_cost`: Your shipping cost (dollars)
- `tracking_number`: Outbound tracking number
- `buyer_info`: Buyer name or username
- `date_sold`: Date format YYYY-MM-DD (defaults to today)

Profit and margin are calculated automatically and shown in the response.

#### `/ordersummary`
Get a breakdown of inbound orders.

Parameters:
- `period`: 7d / 30d / 90d / 365d / all (default: 30d)
- `retailer`: Filter to a specific retailer (optional)

#### `/inventory`
Search your in-stock items.

Parameters:
- `search`: Text to search for in item names (optional — omit to show all)

---

## Recurring Expenses & Reminders

### Recurring Expenses

When adding an expense, check **Is Recurring** and set an interval:

| Interval | How Often |
|----------|-----------|
| daily | Every day |
| weekly | Every 7 days |
| biweekly | Every 14 days |
| monthly | Same day each month |
| quarterly | Every 3 months |
| yearly | Same date each year |

On each startup, Vantage checks for recurring expenses that are due and automatically logs them. A Discord webhook is sent for each one (configurable in `settings.ini`).

### Return Reminders

Right-click any order card → **Set Return Reminder**. Vantage will send a daily Discord alert starting 25 days after the order date (configurable), reminding you that the return window may be closing.

You can deactivate a reminder once you no longer need it from the order detail dialog.

---

## Database Backups & Migrations

### Backups

A backup is created automatically on every app startup if `[backup] enabled = true`. Backups are named `tracker_YYYYMMDD_HHMMSS.db` and stored in the configured `directory`. The oldest files are deleted when the count exceeds `max_keep`.

To manually back up at any time, use the **Backup Now** button in the Settings tab.

### Restoring a Backup

1. Close the application
2. Copy the desired backup file (e.g., `tracker_20260401_120000.db`) and rename it to `tracker.db`
3. Replace the current `tracker.db` in the app folder
4. Restart the application

### Migrations

Database schema changes are versioned in `migrations.py`. Every time the app starts, it checks the current DB version and applies any pending migrations automatically. You do not need to do anything manually — just restart the app after updating.

Current migrations (as of this writing): 12 versioned changes covering the full schema history from initial tables through bridge table support for multi-inventory sales.

---

## File Reference

```
Vantage/
├── .env                     Secrets — IMAP passwords, API keys, webhook URLs
├── settings.ini             App configuration — DB path, backup, thresholds
├── tracker.db               SQLite database (created on first run)
├── sync_state.json          Tracks last sync time and processed email IDs
├── backups/                 Automatic DB backups
├── exports/                 Tax/expense CSV exports
│
├── config.py                Loads .env + settings.ini; exposes all config constants
├── database.py              All SQLite read/write operations
├── email_sync.py            IMAP fetch, retailer routing, parser pipeline, DB upserts
├── tracking.py              Carrier API (UPS/FedEx/USPS) OAuth2 + status polling
├── webhooks.py              Discord embed formatters for every notification event
├── discord_bot.py           Discord.py bot with slash commands
├── gui_app.py               Full PyQt6 GUI (all tabs, models, dialogs, workers)
├── theme.py                 Dark theme colors, Qt stylesheets
├── migrations.py            Versioned database schema DDL
│
└── parsers/
    ├── amazon.py            Amazon (confirmation, shipped, delivered, cancelled, payment)
    ├── walmart.py           Walmart (confirmation, shipped, delivered, cancelled)
    ├── target.py            Target
    ├── nike.py              Nike + SNKRS (HTML + Narvar shipping template)
    ├── bestbuy.py           Best Buy
    ├── pokemon_center.py    Pokémon Center + Narvar delivery
    ├── five_below.py        Five Below + Narvar shipping
    ├── topps.py             Topps (routed through Shopify generic)
    ├── shopify_generic.py   Any Shopify store — auto-detects store name
    ├── ebay.py              eBay sold emails + shipping label emails
    └── stockx.py            StockX listings + sale confirmation
```

---

## Troubleshooting

### No emails are being imported

- Verify your IMAP credentials are correct and the account allows IMAP access
- For Gmail: confirm IMAP is enabled in Gmail settings (Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP)
- For Gmail: use an **App Password**, not your real Gmail password
- Check that the email is in INBOX, Spam, or All Mail — Vantage scans all three
- Use **Manual Scrape** with a large `days_back` value to re-scan older emails

### Orders show $0.00 price

- The shipping or delivery email was processed before the confirmation email
- Use **Manual Scrape** for that retailer to re-process the confirmation email, which will patch the missing price

### Tracking is not updating

- Verify carrier API credentials are set correctly in `.env`
- FedEx: ensure `FEDEX_SANDBOX=False` for real tracking numbers
- Check that the tracking number format matches the carrier (UPS starts with `1Z`, etc.)
- Try the **Refresh Tracking** button manually

### App crashes on startup (exit code -1073741819)

- This is a Windows access violation in Qt's native rendering, usually triggered by matplotlib during a chart update
- The fix is already applied (using `draw_idle()` instead of `draw()`). If it recurs, check for Qt version conflicts with `pip show PyQt6`

### Discord webhooks not sending

- Verify `DISCORD_WEBHOOK_URL` is set in `.env` and the URL is valid
- Test it in the Settings tab → **Test Webhook** button
- Check that your Discord channel still has the webhook (they can be deleted in Discord settings)

### Parser returns wrong item names or misses items

- Amazon: email plain text format must be available. If missing, item names may be truncated. This is rare.
- Shopify stores: if a store uses a non-standard template, item names may not parse. Check the console output for `[Parser/Shopify]` log lines.
- Best Buy: re-scrape — the confirmation email must be found for prices to populate

### Sync state is corrupt / emails not re-processing

- Delete or rename `sync_state.json` in the app folder
- On next startup, the app will re-scan all emails from the beginning (this may take a few minutes for large inboxes)

### Discord bot slash commands not appearing

- Make sure the bot is invited with the `applications.commands` OAuth2 scope
- Wait up to 1 hour for global command propagation, or test in a server where the bot has been added fresh
- Restart `discord_bot.py` and watch the console for errors
