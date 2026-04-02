"""
One-time script: import sales from a CSV file into outbound_sales.

CSV format expected:
  Item, Cost, Date purchased, Sale Price, Sale Date, Fees, Shipping

Identical rows (same item + cost + dates + price + fees + shipping) are
batched into a single sale record with the appropriate quantity.
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime

import database as db
from migrations import run_migrations


def _parse_date(raw: str) -> str:
    """Parse M/D/YY or M/D/YYYY → YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # fallback — return as-is


def import_csv(path: str) -> dict:
    counts = {"imported": 0, "rows": 0}

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        # Batch identical rows: key → count
        # Normalise header keys so we handle varying column names
        batched: dict = defaultdict(int)
        for row in reader:
            counts["rows"] += 1
            lk = {k.strip().lower(): v.strip() for k, v in row.items()}
            item = lk.get("item") or lk.get("item_name") or lk.get("item name") or ""
            size = lk.get("size") or ""
            if size:
                item = f"{item} (Size {size})"
            key = (
                item,
                lk.get("cost") or lk.get("cost_basis") or "0",
                lk.get("date purchased") or lk.get("date_listed") or "",
                lk.get("sale price") or lk.get("sale_price") or "0",
                lk.get("sale date") or lk.get("date_sold") or "",
                lk.get("fees") or lk.get("platform_fees") or "0",
                lk.get("shipping") or lk.get("shipping_cost") or "0",
            )
            batched[key] += 1

    print(f"Read {counts['rows']} CSV rows -> {len(batched)} unique sale groups")

    for (item, cost, date_purchased, sale_price, sale_date, fees, shipping), qty in batched.items():
        cost_cents     = db.dollars_to_cents(float(cost or 0))
        sale_cents     = db.dollars_to_cents(float(sale_price or 0))
        fees_cents     = db.dollars_to_cents(float(fees or 0))
        ship_cents     = db.dollars_to_cents(float(shipping or 0))
        total_cost     = cost_cents * qty
        date_sold      = _parse_date(sale_date)
        date_listed    = _parse_date(date_purchased)

        # Use add_sale_from_email since there's no linked inventory item
        sale_id = db.add_sale_from_email(
            item_name           = item,
            platform            = "direct",    # no platform specified in CSV
            sale_price_cents    = sale_cents * qty,  # total revenue for all units
            platform_fees_cents = fees_cents * qty,
            shipping_cost_cents = ship_cents * qty,
            date_sold           = date_sold,
            quantity            = qty,
        )

        # Manually set cost_basis and recalculate profit since these are known costs
        from database import _conn, _calc_profit
        with _conn() as con:
            profit, margin = _calc_profit(
                sale_cents * qty, total_cost,
                fees_cents * qty, ship_cents * qty,
            )
            con.execute(
                """UPDATE outbound_sales
                   SET cost_basis_cents = ?, profit_cents = ?, margin_percent = ?,
                       date_listed = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (total_cost, profit, margin, date_listed, sale_id),
            )

        print(
            f"  {qty:>3}x {item:<45} | cost={db.format_money(cost_cents)}/ea "
            f"| sale={db.format_money(sale_cents)}/ea | profit={db.format_money(sale_cents * qty - total_cost - fees_cents * qty - ship_cents * qty)}"
        )
        counts["imported"] += 1

    print(f"\nDone — {counts['imported']} sale records imported from {counts['rows']} CSV rows.")
    return counts


if __name__ == "__main__":
    run_migrations()
    path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Houston\Downloads\2026 Tracking - Current Inventory (3).csv"
    import_csv(path)
