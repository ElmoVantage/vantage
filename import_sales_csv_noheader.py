"""
One-time script: import sales from a headerless CSV file.

Columns by position:
  0: Item, 1: (unused), 2: Cost, 3: Date purchased, 4: Sale Price,
  5: Sale date, 6: Fees, 7: Shipping, 8+: (empty/ignored)
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime

import database as db
from database import _conn, _calc_profit
from migrations import run_migrations


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def import_csv(path: str) -> dict:
    counts = {"imported": 0, "rows": 0}

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        batched: dict = defaultdict(int)
        for cols in reader:
            if not cols or not cols[0].strip():
                continue
            counts["rows"] += 1
            item     = cols[0].strip()
            cost     = cols[2].strip() if len(cols) > 2 else "0"
            date_p   = cols[3].strip() if len(cols) > 3 else ""
            sale_p   = cols[4].strip() if len(cols) > 4 else "0"
            date_s   = cols[5].strip() if len(cols) > 5 else ""
            fees     = cols[6].strip() if len(cols) > 6 else "0"
            shipping = cols[7].strip() if len(cols) > 7 else "0"
            key = (item, cost, date_p, sale_p, date_s, fees, shipping)
            batched[key] += 1

    print(f"Read {counts['rows']} CSV rows -> {len(batched)} unique sale groups")

    for (item, cost, date_purch, sale_price, sale_date, fees, shipping), qty in batched.items():
        cost_cents = db.dollars_to_cents(float(cost or 0))
        sale_cents = db.dollars_to_cents(float(sale_price or 0))
        fees_cents = db.dollars_to_cents(float(fees or 0))
        ship_cents = db.dollars_to_cents(float(shipping or 0))
        total_cost = cost_cents * qty
        d_sold     = _parse_date(sale_date) if sale_date else None
        d_listed   = _parse_date(date_purch) if date_purch else None

        sale_id = db.add_sale_from_email(
            item_name           = item,
            platform            = "direct",
            sale_price_cents    = sale_cents * qty,
            platform_fees_cents = fees_cents * qty,
            shipping_cost_cents = ship_cents * qty,
            date_sold           = d_sold,
            quantity            = qty,
        )

        with _conn() as con:
            profit, margin = _calc_profit(
                sale_cents * qty, total_cost,
                fees_cents * qty, ship_cents * qty,
            )
            con.execute(
                """UPDATE outbound_sales
                   SET cost_basis_cents = ?, profit_cents = ?, margin_percent = ?,
                       date_listed = COALESCE(?, date_listed),
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (total_cost, profit, margin, d_listed, sale_id),
            )

        print(
            f"  {qty:>3}x {item:<50} | cost={db.format_money(cost_cents)}/ea "
            f"| sale={db.format_money(sale_cents)}/ea "
            f"| profit={db.format_money(sale_cents * qty - total_cost - fees_cents * qty - ship_cents * qty)}"
        )
        counts["imported"] += 1

    print(f"\nDone -- {counts['imported']} sale records imported from {counts['rows']} CSV rows.")
    return counts


if __name__ == "__main__":
    run_migrations()
    path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Houston\Downloads\2026 Tracking - Current Inventory (5).csv"
    import_csv(path)
