"""Discord webhook notifications for key tracker events."""

from datetime import datetime, timezone
from typing import List, Optional

import requests

from config import DISCORD_WEBHOOK_URL
from database import format_money


def _post(payload: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[webhook] DISCORD_WEBHOOK_URL not set in .env — notification skipped")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as exc:
        print(f"[webhook] Error: {exc}")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def notify_new_order(
    order_id: int,
    order_number: str,
    retailer: str,
    item_name: str,
    cost_cents: int,
    quantity: int,
) -> None:
    _post({"embeds": [{
        "title": "📦 New Inbound Order",
        "color": 0x5865F2,
        "fields": [
            {"name": "Order #",   "value": order_number,             "inline": True},
            {"name": "Retailer",  "value": retailer,                 "inline": True},
            {"name": "Item",      "value": item_name,                "inline": False},
            {"name": "Cost/Unit", "value": format_money(cost_cents), "inline": True},
            {"name": "Qty",       "value": str(quantity),            "inline": True},
        ],
        "timestamp": _ts(),
    }]})


def notify_inventory_added(
    inventory_id: int,
    item_name: str,
    quantity: int,
    cost_basis_cents: int,
) -> None:
    _post({"embeds": [{
        "title": "🗃️ Inventory Added",
        "color": 0x57F287,
        "fields": [
            {"name": "Item",       "value": item_name,                       "inline": False},
            {"name": "Quantity",   "value": str(quantity),                   "inline": True},
            {"name": "Purchase Price", "value": format_money(cost_basis_cents),  "inline": True},
        ],
        "timestamp": _ts(),
    }]})


def notify_sale(
    sale_id: int,
    item_name: str,
    platform: str,
    sale_price_cents: int,
    profit_cents: int,
    margin_percent: float,
) -> None:
    color = 0x57F287 if profit_cents >= 0 else 0xED4245
    _post({"embeds": [{
        "title": "💰 Sale Recorded",
        "color": color,
        "fields": [
            {"name": "Platform",   "value": platform.title(),               "inline": True},
            {"name": "Item",       "value": item_name,                      "inline": False},
            {"name": "Sale Price", "value": format_money(sale_price_cents), "inline": True},
            {"name": "Profit",     "value": format_money(profit_cents),     "inline": True},
            {"name": "Margin",     "value": f"{margin_percent:.1f}%",       "inline": True},
        ],
        "timestamp": _ts(),
    }]})


def notify_profit_summary(
    period: str,
    revenue_cents: int,
    profit_cents: int,
    margin_percent: float,
    sale_count: int,
) -> None:
    color = 0x57F287 if profit_cents >= 0 else 0xED4245
    _post({"embeds": [{
        "title": f"📊 Profit Summary — {period.title()}",
        "color": color,
        "fields": [
            {"name": "Revenue",     "value": format_money(revenue_cents), "inline": True},
            {"name": "Profit",      "value": format_money(profit_cents),  "inline": True},
            {"name": "Margin",      "value": f"{margin_percent:.1f}%",    "inline": True},
            {"name": "Sales",       "value": str(sale_count),             "inline": True},
        ],
        "timestamp": _ts(),
    }]})


def notify_recurring_expense(expense_id: int, expense_name: str, amount_cents: int) -> None:
    _post({"embeds": [{
        "title": "🔁 Recurring Expense Logged",
        "color": 0xFAB387,
        "fields": [
            {"name": "Name",   "value": expense_name,               "inline": True},
            {"name": "Amount", "value": format_money(amount_cents), "inline": True},
        ],
        "timestamp": _ts(),
    }]})


def notify_tracking_update(
    item_name: str,
    order_number: str,
    carrier: str,
    tracking_number: str,
    old_status: Optional[str],
    new_status: str,
    estimated_delivery: Optional[str] = None,
    delivery_address: Optional[str] = None,
) -> None:
    if _is_delivered(new_status):
        color = 0x57F287   # green — delivered
        title = "✅ Package Delivered"
    elif "out for delivery" in new_status.lower():
        color = 0xFAA61A   # orange — out for delivery
        title = "🚚 Out for Delivery"
    else:
        color = 0x5865F2   # blue — general update
        title = "📬 Tracking Update"

    status_display = f"{old_status or 'Unknown'} → **{new_status}**"
    fields = [
        {"name": "Item",     "value": item_name,      "inline": False},
        {"name": "Order #",  "value": order_number,   "inline": True},
        {"name": "Carrier",  "value": carrier,         "inline": True},
        {"name": "Tracking", "value": tracking_number, "inline": False},
        {"name": "Status",   "value": status_display,  "inline": False},
    ]
    if delivery_address:
        fields.append({"name": "Delivery Address", "value": delivery_address, "inline": False})
    if estimated_delivery:
        fields.append({"name": "Est. Delivery", "value": estimated_delivery, "inline": True})

    _post({"embeds": [{"title": title, "color": color, "fields": fields, "timestamp": _ts()}]})


def _is_delivered(status: Optional[str]) -> bool:
    return bool(status and "delivered" in status.lower()
                and "out for delivery" not in status.lower())


def notify_return_reminder_set(item_name: str) -> None:
    _post({"embeds": [{
        "title": "⏰ Return Reminder Set",
        "color": 0xFAB387,
        "description": f"Return reminder set for **{item_name}**\nYou'll be notified daily starting 25 days after the order date.",
        "timestamp": _ts(),
    }]})


def notify_return_reminder_due(item_name: str, order_date: str, days_elapsed: int) -> None:
    _post({"embeds": [{
        "title": "🔔 Return Reminder",
        "color": 0xED4245,
        "description": f"**{item_name}**",
        "fields": [
            {"name": "Order Date",    "value": order_date,          "inline": True},
            {"name": "Days Since Order", "value": str(days_elapsed), "inline": True},
        ],
        "footer": {"text": "Return window may be closing — check retailer policy"},
        "timestamp": _ts(),
    }]})


def notify_large_expense(expense_id: int, expense_name: str, amount_cents: int) -> None:
    _post({"embeds": [{
        "title": "⚠️ Large Expense Recorded",
        "color": 0xF38BA8,
        "fields": [
            {"name": "Name",   "value": expense_name,               "inline": True},
            {"name": "Amount", "value": format_money(amount_cents), "inline": True},
        ],
        "timestamp": _ts(),
    }]})


def notify_daily_delivery_report(
    date_str: str,
    by_address: dict,  # {display_addr: {"pkg_count": int, "items": [(name, qty), ...]}}
) -> None:
    """7am forecast: packages expected for delivery today, grouped by address."""
    if not by_address:
        return
    total_pkgs = sum(v["pkg_count"] for v in by_address.values())
    fields: List[dict] = []
    for addr, info in by_address.items():
        lines = "\n".join(
            f"• {qty}x {name}" if qty > 1 else f"• {name}"
            for name, qty in info["items"]
        )
        label = f"📍 {addr}  ({info['pkg_count']} pkg{'s' if info['pkg_count'] != 1 else ''})"
        fields.append({"name": label, "value": lines or "—", "inline": False})
        if len(fields) == 20:
            remaining = len(by_address) - 20
            if remaining > 0:
                fields.append({"name": "…", "value": f"+ {remaining} more address(es)", "inline": False})
            break
    _post({"embeds": [{
        "title": f"📦 Daily Delivery Report — {date_str}",
        "description": f"{total_pkgs} package{'s' if total_pkgs != 1 else ''} expected today",
        "color": 0x5865F2,
        "fields": fields,
        "timestamp": _ts(),
    }]})


def notify_amazon_payment_issue(
    order_number: str,
    item_name: str,
    account_email: str,
) -> None:
    """Alert when Amazon flags a payment issue (declined / verification needed)."""
    _post({"embeds": [{
        "title": "⚠️ Amazon Payment Issue",
        "color": 0xED4245,
        "fields": [
            {"name": "Order #",  "value": order_number,  "inline": True},
            {"name": "Account",  "value": account_email, "inline": True},
            {"name": "Item(s)",  "value": item_name,     "inline": False},
        ],
        "timestamp": _ts(),
    }]})


def notify_tracking_poll_summary(
    refreshed: int,
    failed: int,
    newly_delivered: List[dict],  # [{"display_addr": str, "pkg_count": int, "items": [(name, qty)]}]
    status_changes: Optional[List[dict]] = None,  # [{"item_name", "old_status", "new_status"}]
) -> None:
    """Summary webhook fired after each carrier API poll cycle."""
    status_changes = status_changes or []
    # Only post if something actually changed
    if refreshed == 0 and not newly_delivered and not status_changes:
        return
    delivered_total = sum(d["pkg_count"] for d in newly_delivered)
    has_changes = bool(newly_delivered or status_changes)
    if not has_changes:
        return  # nothing interesting to report

    fields: List[dict] = [
        {"name": "Packages checked", "value": str(refreshed), "inline": True},
        {"name": "Errors",           "value": str(failed),    "inline": True},
    ]

    # Delivered packages grouped by address
    if delivered_total:
        addr_lines = "\n".join(
            f"• {d['pkg_count']} pkg{'s' if d['pkg_count'] != 1 else ''} -> {d['display_addr']}"
            for d in newly_delivered
        )
        fields.append({
            "name": f"Delivered ({delivered_total} pkg{'s' if delivered_total != 1 else ''})",
            "value": addr_lines,
            "inline": False,
        })

    # Other status changes batched by new_status
    if status_changes:
        # Group by new_status for cleaner display
        by_status: dict = {}
        for sc in status_changes:
            key = sc["new_status"]
            by_status.setdefault(key, []).append(sc["item_name"])
        for status, items in by_status.items():
            # Deduplicate and count
            from collections import Counter
            item_counts = Counter(items)
            lines = "\n".join(
                f"• {cnt}x {name[:45]}" if cnt > 1 else f"• {name[:45]}"
                for name, cnt in item_counts.items()
            )
            fields.append({
                "name": f"📬 {status} ({len(items)})",
                "value": lines[:1024],  # Discord field value limit
                "inline": False,
            })

    color = 0x57F287 if delivered_total else 0x5865F2
    _post({"embeds": [{
        "title": "🔄 Tracking Poll Complete",
        "color": color,
        "fields": fields,
        "timestamp": _ts(),
    }]})
