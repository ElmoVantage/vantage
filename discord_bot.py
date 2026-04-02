"""
Vanguard Tracker — Discord Bot
Four slash commands: addinventory, addsale, ordersummary, inventory
"""

import sys
from collections import defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import webhooks
from config import DISCORD_BOT_TOKEN

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

PLATFORMS   = ["ebay", "stockx", "mercari", "offerup", "direct", "local", "other"]
CONDITIONS  = ["new", "like new", "open box", "used", "for parts"]


# ── Embed helpers ─────────────────────────────────────────────────────────────

def ok(title: str, fields: list = None, desc: str = None) -> discord.Embed:
    e = discord.Embed(title=title, color=0x57F287)
    if desc:
        e.description = desc
    for f in (fields or []):
        e.add_field(name=f["name"], value=str(f["value"]), inline=f.get("inline", True))
    return e


def info(title: str, fields: list = None, desc: str = None) -> discord.Embed:
    e = discord.Embed(title=title, color=0x5865F2)
    if desc:
        e.description = desc
    for f in (fields or []):
        e.add_field(name=f["name"], value=str(f["value"]), inline=f.get("inline", True))
    return e


def err(msg: str) -> discord.Embed:
    return discord.Embed(title="❌ Error", description=msg, color=0xED4245)


# ── /addinventory ─────────────────────────────────────────────────────────────

@bot.tree.command(name="addinventory", description="Add an item to inventory")
@app_commands.describe(
    item_name="Product name",
    category="Category (e.g. cards, sneakers, apparel, electronics)",
    cost_basis="Cost paid per unit in dollars (e.g. 24.99)",
    quantity="Number of units",
    condition="Item condition",
    sku="Product SKU (optional)",
    size_variant="Size or variant label (optional)",
    storage_location="Where it's stored (optional)",
    date_received="Date received YYYY-MM-DD (default today)",
)
@app_commands.choices(condition=[app_commands.Choice(name=c.title(), value=c) for c in CONDITIONS])
async def addinventory(
    interaction: discord.Interaction,
    item_name: str,
    category: str,
    cost_basis: float,
    quantity: int,
    condition: str = "new",
    sku: Optional[str] = None,
    size_variant: Optional[str] = None,
    storage_location: Optional[str] = None,
    date_received: Optional[str] = None,
):
    await interaction.response.defer()
    try:
        cost_cents = db.dollars_to_cents(cost_basis)
        inv_id = db.add_inventory(
            item_name=item_name,
            category=category,
            cost_basis_cents=cost_cents,
            quantity=quantity,
            condition=condition,
            sku=sku,
            size_variant=size_variant,
            storage_location=storage_location,
            date_received=date_received,
        )
        webhooks.notify_inventory_added(inv_id, item_name, quantity, cost_cents)
        await interaction.followup.send(embed=ok("🗃️ Inventory Added", fields=[
            {"name": "Item",          "value": item_name,                  "inline": True},
            {"name": "Category",      "value": category,                   "inline": True},
            {"name": "Purchase Price",   "value": db.format_money(cost_cents),"inline": True},
            {"name": "Qty",           "value": str(quantity),              "inline": True},
            {"name": "Condition",     "value": condition.title(),          "inline": True},
            {"name": "SKU",           "value": sku or "—",                 "inline": True},
            {"name": "Size/Variant",  "value": size_variant or "—",        "inline": True},
            {"name": "Location",      "value": storage_location or "—",    "inline": True},
        ]))
    except Exception as exc:
        await interaction.followup.send(embed=err(str(exc)))


# ── /addsale ──────────────────────────────────────────────────────────────────

async def _inventory_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[int]]:
    """Suggest in-stock inventory items matching what the user has typed."""
    items = db.get_inventory(in_stock_only=True)
    matches = [
        i for i in items
        if current.lower() in i["item_name"].lower() or current == str(i["id"])
    ]
    return [
        app_commands.Choice(name=i["item_name"][:100], value=i["id"])
        for i in matches[:25]
    ]


@bot.tree.command(name="addsale", description="Record a sale — links to an inventory item by ID")
@app_commands.describe(
    inventory_id="Start typing an item name to search inventory",
    platform="Platform the item was sold on",
    sale_price="Sale price in dollars",
    fees="Platform fees in dollars",
    shipping_cost="Shipping cost in dollars",
    tracking_number="Tracking number (optional)",
    buyer_info="Buyer name or username (optional)",
    date_sold="Date sold YYYY-MM-DD (default today)",
)
@app_commands.autocomplete(inventory_id=_inventory_autocomplete)
@app_commands.choices(platform=[app_commands.Choice(name=p.title(), value=p) for p in PLATFORMS])
async def addsale(
    interaction: discord.Interaction,
    inventory_id: int,
    platform: str,
    sale_price: float,
    fees: float,
    shipping_cost: float,
    tracking_number: Optional[str] = None,
    buyer_info: Optional[str] = None,
    date_sold: Optional[str] = None,
):
    await interaction.response.defer()
    try:
        inv = db.get_inventory_by_id(inventory_id)
        if not inv:
            await interaction.followup.send(embed=err("Item not found. Use `/inventory` to search."))
            return
        if inv["quantity"] < 1:
            await interaction.followup.send(embed=err(f"**{inv['item_name']}** is out of stock."))
            return

        price_c = db.dollars_to_cents(sale_price)
        fees_c  = db.dollars_to_cents(fees)
        ship_c  = db.dollars_to_cents(shipping_cost)

        sale_id = db.add_sale(
            inventory_id=inventory_id,
            platform=platform,
            sale_price_cents=price_c,
            platform_fees_cents=fees_c,
            shipping_cost_cents=ship_c,
            tracking_number=tracking_number,
            buyer_info=buyer_info,
            date_sold=date_sold,
        )
        sale = db.get_sale_by_id(sale_id)
        profit = sale["profit_cents"]
        margin = sale["margin_percent"]

        webhooks.notify_sale(sale_id, inv["item_name"], platform, price_c, profit, margin)

        color = 0x57F287 if profit >= 0 else 0xED4245
        e = discord.Embed(title="💰 Sale Recorded", color=color)
        for name, val in [
            ("Item",       inv["item_name"]),
            ("Platform",   platform.title()),
            ("Sale Price", db.format_money(price_c)),
            ("Fees",       db.format_money(fees_c)),
            ("Shipping",   db.format_money(ship_c)),
            ("Purchase Price", db.format_money(inv["cost_basis_cents"])),
            ("Profit",     db.format_money(profit)),
            ("Margin",     f"{margin:.1f}%"),
        ]:
            e.add_field(name=name, value=val, inline=True)
        await interaction.followup.send(embed=e)
    except Exception as exc:
        await interaction.followup.send(embed=err(str(exc)))


# ── /ordersummary ─────────────────────────────────────────────────────────────

@bot.tree.command(name="ordersummary", description="Break down inbound orders by product and retailer for a time range")
@app_commands.describe(
    period="Preset time period",
    from_date="Custom start date YYYY-MM-DD (overrides period)",
    to_date="Custom end date YYYY-MM-DD (overrides period)",
    retailer="Filter to a specific retailer (optional)",
)
@app_commands.choices(period=[
    app_commands.Choice(name="Last 7 Days",   value="7"),
    app_commands.Choice(name="Last 30 Days",  value="30"),
    app_commands.Choice(name="Last 90 Days",  value="90"),
    app_commands.Choice(name="Last 365 Days", value="365"),
    app_commands.Choice(name="All Time",      value="0"),
])
async def ordersummary(
    interaction: discord.Interaction,
    period: str = "30",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    retailer: Optional[str] = None,
):
    await interaction.response.defer()
    try:
        days = None if from_date else (int(period) or None)
        orders = db.get_inbound_orders(retailer=retailer, days=days)

        # Apply custom date range if provided
        if from_date or to_date:
            def _in_range(o):
                d = (o.get("order_date") or "")[:10]
                if from_date and d < from_date:
                    return False
                if to_date and d > to_date:
                    return False
                return True
            orders = [o for o in orders if _in_range(o)]

        if not orders:
            await interaction.followup.send(embed=info("📦 Order Summary", desc="No orders found for that range."))
            return

        # Aggregate by item name + retailer
        by_product: dict = defaultdict(lambda: {"qty": 0, "cost_cents": 0, "retailers": set()})
        by_retailer: dict = defaultdict(lambda: {"qty": 0, "cost_cents": 0, "items": set()})
        total_qty   = 0
        total_cost  = 0

        for o in orders:
            key = o["item_name"]
            ret = o["retailer"].title()
            qty = o.get("quantity") or 1
            cost = (o.get("cost_cents") or 0) * qty

            by_product[key]["qty"]        += qty
            by_product[key]["cost_cents"] += cost
            by_product[key]["retailers"].add(ret)

            by_retailer[ret]["qty"]        += qty
            by_retailer[ret]["cost_cents"] += cost
            by_retailer[ret]["items"].add(key)

            total_qty  += qty
            total_cost += cost

        # Build product lines (top 15 by qty)
        top_products = sorted(by_product.items(), key=lambda x: x[1]["qty"], reverse=True)[:15]
        product_lines = []
        for name, data in top_products:
            retailers = ", ".join(sorted(data["retailers"]))
            product_lines.append(
                f"**{name}** — {data['qty']}× | {db.format_money(data['cost_cents'])} | {retailers}"
            )

        # Build retailer lines
        retailer_lines = []
        for ret, data in sorted(by_retailer.items(), key=lambda x: x[1]["qty"], reverse=True):
            retailer_lines.append(
                f"**{ret}** — {data['qty']} units | {db.format_money(data['cost_cents'])} | {len(data['items'])} products"
            )

        range_label = f"{from_date} → {to_date}" if (from_date or to_date) else f"Last {period} days" if period != "0" else "All Time"
        if retailer:
            range_label += f" · {retailer.title()}"

        e = discord.Embed(title=f"📦 Order Summary — {range_label}", color=0x5865F2)
        e.add_field(name="Total Units",  value=str(total_qty),               inline=True)
        e.add_field(name="Total Spent",  value=db.format_money(total_cost),  inline=True)
        e.add_field(name="Unique Items", value=str(len(by_product)),         inline=True)

        if retailer_lines:
            e.add_field(name="By Retailer", value="\n".join(retailer_lines), inline=False)

        if product_lines:
            label = f"By Product (top {len(product_lines)})" if len(by_product) > 15 else "By Product"
            e.add_field(name=label, value="\n".join(product_lines), inline=False)

        await interaction.followup.send(embed=e)
    except Exception as exc:
        await interaction.followup.send(embed=err(str(exc)))


# ── /inventory ────────────────────────────────────────────────────────────────

@bot.tree.command(name="inventory", description="View current inventory — optionally filter by category")
@app_commands.describe(
    category="Filter by category (e.g. cards, sneakers, apparel)",
    in_stock_only="Only show items with quantity > 0 (default true)",
)
async def inventory(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    in_stock_only: bool = True,
):
    await interaction.response.defer()
    try:
        items = db.get_inventory(category=category, in_stock_only=in_stock_only)
        if not items:
            label = f" in **{category}**" if category else ""
            await interaction.followup.send(embed=info("🗃️ Inventory", desc=f"No items found{label}."))
            return

        # Summary stats
        total_units = sum(i["quantity"] for i in items)
        total_value = sum(i["cost_basis_cents"] * i["quantity"] for i in items)

        # Group by category
        by_cat: dict = defaultdict(list)
        for item in items:
            by_cat[item.get("category") or "Uncategorized"].append(item)

        title = f"🗃️ Inventory" + (f" — {category.title()}" if category else "")
        e = discord.Embed(title=title, color=0x5865F2)
        e.add_field(name="Total Units", value=str(total_units),              inline=True)
        e.add_field(name="Total Value", value=db.format_money(total_value),  inline=True)
        e.add_field(name="SKUs",        value=str(len(items)),               inline=True)

        # Per-category breakdown (up to 8 categories, 8 items each)
        for cat, cat_items in sorted(by_cat.items())[:8]:
            lines = []
            for item in cat_items[:8]:
                qty_str = f"**{item['quantity']}×**" if item["quantity"] > 0 else "~~0×~~"
                variant = f" · {item['size_variant']}" if item.get("size_variant") else ""
                lines.append(
                    f"{qty_str} {item['item_name']}{variant} — {db.format_money(item['cost_basis_cents'])}"
                )
            if len(cat_items) > 8:
                lines.append(f"*…and {len(cat_items) - 8} more*")
            e.add_field(name=cat.title(), value="\n".join(lines), inline=False)

        if not category and len(by_cat) > 8:
            e.set_footer(text=f"Showing 8 of {len(by_cat)} categories. Use /inventory category:<name> to filter.")

        await interaction.followup.send(embed=e)
    except Exception as exc:
        await interaction.followup.send(embed=err(str(exc)))


# ── Registration & startup ────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[bot] Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        # Clear stale global commands
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()

        # Register commands guild-scoped for instant propagation
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"[bot] Synced {len(synced)} commands to {guild.name}")
    except Exception as exc:
        print(f"[bot] Sync error: {exc}")


def run():
    db.startup()
    token = DISCORD_BOT_TOKEN
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN is not set. Add it to your .env file.")
        sys.exit(1)
    bot.run(token)


if __name__ == "__main__":
    run()
