"""
Core database layer — all CRUD for inbound_orders, inbound_order_items,
inventory, and outbound_sales.  Money is stored as integer cents throughout.
"""

import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import calendar

from config import (
    AUTO_LOG_RECURRING, BACKUP_DIR, BACKUP_ENABLED, BACKUP_MAX_KEEP,
    DB_PATH, LARGE_EXPENSE_THRESHOLD_CENTS, NOTIFY_DISCORD_ON_RECURRING,
)


def get_db_path() -> str:
    return DB_PATH


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Backup ───────────────────────────────────────────────────────────────────

def backup_db() -> Optional[str]:
    """
    Copy tracker.db to BACKUP_DIR/tracker_YYYYMMDD_HHMMSS.db.
    Prunes oldest files so at most BACKUP_MAX_KEEP backups are kept.
    Returns the path of the new backup, or None if skipped/disabled.
    """
    if not BACKUP_ENABLED or not os.path.exists(DB_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"tracker_{ts}.db")
    shutil.copy2(DB_PATH, dst)

    # Prune oldest backups beyond the keep limit
    backups = sorted(Path(BACKUP_DIR).glob("tracker_*.db"))
    for old in backups[:-BACKUP_MAX_KEEP]:
        old.unlink(missing_ok=True)

    print(f"[db] Backup: {dst}")
    return dst


# ── Startup ───────────────────────────────────────────────────────────────────

def startup() -> None:
    """Call once at process start: backup, migrate, then process due recurring expenses."""
    backup_db()
    from migrations import run_migrations
    run_migrations()
    if AUTO_LOG_RECURRING:
        _auto_log_recurring()


def _auto_log_recurring() -> None:
    new_ids = process_recurring_expenses()
    if NOTIFY_DISCORD_ON_RECURRING and new_ids:
        try:
            from webhooks import notify_recurring_expense
            for exp_id in new_ids:
                exp = get_expense_by_id(exp_id)
                if exp:
                    notify_recurring_expense(exp_id, exp["expense_name"], exp["amount_cents"])
        except Exception as exc:
            print(f"[db] Webhook error during recurring notify: {exc}")


# ── Money helpers ────────────────────────────────────────────────────────────

def dollars_to_cents(dollars: float) -> int:
    return round(float(dollars) * 100)


def cents_to_dollars(cents: int) -> float:
    return cents / 100.0


def format_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ── Inbound Orders ───────────────────────────────────────────────────────────

def add_inbound_order(
    order_number: str,
    retailer: str,
    order_date: str,
    item_name: str,
    cost_cents: int,
    quantity: int,
    sku: Optional[str] = None,
    tracking_number: Optional[str] = None,
    delivery_address: Optional[str] = None,
    status: str = "ordered",
    account_email: Optional[str] = None,
) -> Tuple[int, int]:
    """Create or append to an inbound order. Returns (order_id, item_id)."""
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM inbound_orders WHERE order_number = ? AND is_deleted = 0",
            (order_number,),
        ).fetchone()

        if row:
            order_id = row["id"]
            con.execute(
                """UPDATE inbound_orders
                   SET tracking_number  = COALESCE(?, tracking_number),
                       delivery_address = COALESCE(?, delivery_address),
                       account_email    = COALESCE(account_email, ?),
                       updated_at       = datetime('now')
                   WHERE id = ?""",
                (tracking_number, delivery_address, account_email, order_id),
            )
        else:
            cur = con.execute(
                """INSERT INTO inbound_orders
                   (order_number, retailer, order_date, status,
                    tracking_number, delivery_address, account_email)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order_number, retailer, order_date, status,
                 tracking_number, delivery_address, account_email),
            )
            order_id = cur.lastrowid

        cur = con.execute(
            """INSERT INTO inbound_order_items
               (order_id, item_name, sku, cost_cents, quantity)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, item_name, sku, cost_cents, quantity),
        )
        item_id = cur.lastrowid

    return order_id, item_id


def get_inbound_orders(
    status: Optional[str] = None,
    retailer: Optional[str] = None,
    days: Optional[int] = None,
) -> List[Dict]:
    """Flat join of orders + items."""
    sql = """
        SELECT
            io.id          AS order_id,
            ioi.id         AS item_id,
            ioi.item_name,
            ioi.sku,
            io.retailer,
            io.order_number,
            io.status,
            io.order_date,
            ioi.cost_cents,
            ioi.quantity,
            io.tracking_number,
            io.delivery_address,
            io.notes,
            io.tracking_carrier,
            io.tracking_status,
            io.estimated_delivery,
            io.tracking_checked_at,
            io.created_at,
            io.updated_at
        FROM inbound_orders io
        JOIN inbound_order_items ioi ON ioi.order_id = io.id
        WHERE io.is_deleted = 0 AND ioi.is_deleted = 0
    """
    params: List[Any] = []
    if status:
        sql += " AND io.status = ?"
        params.append(status)
    if retailer:
        sql += " AND LOWER(io.retailer) LIKE ?"
        params.append(f"%{retailer.lower()}%")
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND io.order_date >= ?"
        params.append(cutoff)
    sql += " ORDER BY io.order_date DESC, io.id DESC"

    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_inbound_order_by_id(order_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM inbound_orders WHERE id = ? AND is_deleted = 0", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def get_inbound_item_by_id(item_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM inbound_order_items WHERE id = ? AND is_deleted = 0", (item_id,)
        ).fetchone()
        return dict(row) if row else None


_ORDER_FIELDS = {"status", "tracking_number", "delivery_address", "notes", "retailer", "order_date", "order_number", "account_email"}
_ITEM_FIELDS = {"item_name", "sku", "cost_cents", "quantity"}


def update_inbound_order(order_id: int, field: str, value: Any) -> bool:
    if field not in _ORDER_FIELDS:
        return False
    with _conn() as con:
        con.execute(
            f"UPDATE inbound_orders SET {field} = ?, updated_at = datetime('now') WHERE id = ? AND is_deleted = 0",
            (value, order_id),
        )
    return True


def update_inbound_item(item_id: int, field: str, value: Any) -> bool:
    if field not in _ITEM_FIELDS:
        return False
    with _conn() as con:
        con.execute(
            f"UPDATE inbound_order_items SET {field} = ?, updated_at = datetime('now') WHERE id = ? AND is_deleted = 0",
            (value, item_id),
        )
    return True


def delete_inbound_order(order_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE inbound_orders SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
            (order_id,),
        )
        con.execute(
            "UPDATE inbound_order_items SET is_deleted = 1, updated_at = datetime('now') WHERE order_id = ?",
            (order_id,),
        )


def delete_inbound_item(item_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE inbound_order_items SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
            (item_id,),
        )
        # If no active items remain on the parent order, soft-delete the order too
        row = con.execute(
            "SELECT order_id FROM inbound_order_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row:
            remaining = con.execute(
                "SELECT COUNT(*) FROM inbound_order_items WHERE order_id = ? AND is_deleted = 0",
                (row[0],)
            ).fetchone()[0]
            if remaining == 0:
                con.execute(
                    "UPDATE inbound_orders SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
                    (row[0],)
                )


def get_account_health_data() -> Dict:
    """
    Returns account health stats grouped by retailer and account_email.
    Structure:
      {
        "walmart": [
          {"account_email": "...", "address": "...", "orders": 5, "shipped": 3, "cancelled": 1},
          ...   # sorted by shipped desc
        ],
        ...
      }
    """
    with _conn() as con:
        rows = con.execute(
            """
            SELECT
                retailer,
                LOWER(COALESCE(account_email, '')) AS account_email,
                delivery_address,
                COUNT(*)                                                   AS total,
                SUM(CASE WHEN status IN ('shipped','delivered') THEN 1 ELSE 0 END) AS shipped,
                SUM(CASE WHEN status = 'cancelled'             THEN 1 ELSE 0 END) AS cancelled
            FROM inbound_orders
            WHERE is_deleted = 0
            GROUP BY retailer, account_email
            ORDER BY retailer, shipped DESC, cancelled ASC, total DESC
            """
        ).fetchall()

    result: Dict[str, List[Dict]] = {}
    for r in rows:
        retailer = r["retailer"]
        result.setdefault(retailer, []).append({
            "account_email": r["account_email"] or "(unknown)",
            "address":       r["delivery_address"] or "",
            "orders":        r["total"],
            "shipped":       r["shipped"],
            "cancelled":     r["cancelled"],
        })
    return result


def deliver_order_to_inventory(order_id: int) -> List[int]:
    """Mark order delivered and move items into inventory. Returns new inventory IDs."""
    with _conn() as con:
        items = con.execute(
            """SELECT ioi.* FROM inbound_order_items ioi
               WHERE ioi.order_id = ? AND ioi.is_deleted = 0""",
            (order_id,),
        ).fetchall()

        con.execute(
            "UPDATE inbound_orders SET status = 'delivered', updated_at = datetime('now') WHERE id = ?",
            (order_id,),
        )

        today = datetime.now().strftime("%Y-%m-%d")
        new_ids: List[int] = []
        for item in items:
            existing = con.execute(
                "SELECT id FROM inventory WHERE inbound_order_item_id = ? AND is_deleted = 0",
                (item["id"],),
            ).fetchone()
            if not existing:
                qty = item["quantity"] or 1
                unit_cost = round(item["cost_cents"] / qty) if qty else item["cost_cents"]
                for _ in range(qty):
                    cur = con.execute(
                        """INSERT INTO inventory
                           (item_name, sku, category, cost_basis_cents, quantity, date_received, inbound_order_item_id)
                           VALUES (?, ?, 'General', ?, 1, ?, ?)""",
                        (item["item_name"], item["sku"], unit_cost, today, item["id"]),
                    )
                    new_ids.append(cur.lastrowid)
        return new_ids


# ── Inventory ────────────────────────────────────────────────────────────────

def add_inventory(
    item_name: str,
    category: str,
    cost_basis_cents: int,
    quantity: int,
    condition: str = "new",
    sku: Optional[str] = None,
    size_variant: Optional[str] = None,
    storage_location: Optional[str] = None,
    date_received: Optional[str] = None,
    inbound_order_item_id: Optional[int] = None,
) -> int:
    if date_received is None:
        date_received = datetime.now().strftime("%Y-%m-%d")
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO inventory
               (item_name, sku, category, size_variant, condition, cost_basis_cents,
                quantity, storage_location, date_received, inbound_order_item_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_name, sku, category, size_variant, condition, cost_basis_cents,
             quantity, storage_location, date_received, inbound_order_item_id),
        )
        return cur.lastrowid


def get_inventory(
    category: Optional[str] = None,
    in_stock_only: bool = False,
) -> List[Dict]:
    sql = (
        "SELECT inv.*, io.order_number AS source_order_number, io.order_date AS order_date "
        "FROM inventory inv "
        "LEFT JOIN inbound_order_items ioi ON ioi.id = inv.inbound_order_item_id "
        "LEFT JOIN inbound_orders io ON io.id = ioi.order_id "
        "WHERE inv.is_deleted = 0"
    )
    params: List[Any] = []
    if category:
        sql += " AND LOWER(inv.category) LIKE ?"
        params.append(f"%{category.lower()}%")
    if in_stock_only:
        sql += " AND inv.quantity > 0"
    sql += " ORDER BY inv.date_received DESC, inv.id DESC"
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_inventory_by_id(inventory_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM inventory WHERE id = ? AND is_deleted = 0", (inventory_id,)
        ).fetchone()
        return dict(row) if row else None


_INV_FIELDS = {
    "item_name", "sku", "category", "size_variant", "condition",
    "cost_basis_cents", "quantity", "storage_location", "date_received",
}


def adjust_inventory(inventory_id: int, field: str, value: Any) -> bool:
    if field not in _INV_FIELDS:
        return False
    with _conn() as con:
        con.execute(
            f"UPDATE inventory SET {field} = ?, updated_at = datetime('now') WHERE id = ? AND is_deleted = 0",
            (value, inventory_id),
        )
    return True


def delete_inventory(inventory_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE inventory SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
            (inventory_id,),
        )


# ── Return Reminders ─────────────────────────────────────────────────────────

def set_return_reminder(inventory_id: int, item_name: str, order_date: str, days: int = 25) -> None:
    """Create or reactivate a return reminder for an inventory item."""
    reminder_date = (
        datetime.strptime(order_date, "%Y-%m-%d").date() + timedelta(days=days)
    ).isoformat()
    with _conn() as con:
        existing = con.execute(
            "SELECT id FROM return_reminders WHERE inventory_id = ?", (inventory_id,)
        ).fetchone()
        if existing:
            con.execute(
                """UPDATE return_reminders
                   SET item_name = ?, order_date = ?, reminder_date = ?,
                       days = ?, last_notified = NULL, is_active = 1
                   WHERE inventory_id = ?""",
                (item_name, order_date, reminder_date, days, inventory_id),
            )
        else:
            con.execute(
                """INSERT INTO return_reminders (inventory_id, item_name, order_date, reminder_date, days)
                   VALUES (?, ?, ?, ?, ?)""",
                (inventory_id, item_name, order_date, reminder_date, days),
            )


def cancel_return_reminder(inventory_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE return_reminders SET is_active = 0 WHERE inventory_id = ?",
            (inventory_id,),
        )


def get_active_reminder_for_inventory(inventory_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM return_reminders WHERE inventory_id = ? AND is_active = 1",
            (inventory_id,),
        ).fetchone()
        return dict(row) if row else None


def get_due_return_reminders() -> List[Dict]:
    """Return active reminders where today >= reminder_date and not yet notified today."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM return_reminders
               WHERE is_active = 1
                 AND date('now') >= reminder_date
                 AND (last_notified IS NULL OR last_notified < date('now'))"""
        ).fetchall()
        return [dict(r) for r in rows]


def mark_reminder_notified(reminder_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE return_reminders SET last_notified = date('now') WHERE id = ?",
            (reminder_id,),
        )


# ── Outbound Sales ───────────────────────────────────────────────────────────

def _calc_profit(sale_price: int, cost_basis: int, fees: int, shipping: int) -> Tuple[int, float]:
    profit = sale_price - cost_basis - fees - shipping
    margin = (profit / sale_price * 100) if sale_price > 0 else 0.0
    return profit, margin


def add_sale(
    inventory_id: int,
    platform: str,
    sale_price_cents: int,
    platform_fees_cents: int,
    shipping_cost_cents: int,
    tracking_number: Optional[str] = None,
    buyer_info: Optional[str] = None,
    date_listed: Optional[str] = None,
    date_sold: Optional[str] = None,
    status: str = "sold",
    quantity: int = 1,
) -> int:
    inv = get_inventory_by_id(inventory_id)
    if not inv:
        raise ValueError(f"Inventory item #{inventory_id} not found")

    today = datetime.now().strftime("%Y-%m-%d")
    total_cost = inv["cost_basis_cents"] * quantity
    profit, margin = _calc_profit(
        sale_price_cents, total_cost, platform_fees_cents, shipping_cost_cents
    )

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO outbound_sales
               (inventory_id, item_name, platform, sale_price_cents, platform_fees_cents,
                shipping_cost_cents, cost_basis_cents, profit_cents, margin_percent,
                tracking_number, buyer_info, date_listed, date_sold, status, quantity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                inventory_id, inv["item_name"], platform, sale_price_cents,
                platform_fees_cents, shipping_cost_cents, total_cost,
                profit, margin, tracking_number, buyer_info,
                date_listed or inv.get("date_received"), date_sold or today, status,
                quantity,
            ),
        )
        sale_id = cur.lastrowid
        con.execute(
            "UPDATE inventory SET quantity = MAX(0, quantity - ?) , updated_at = datetime('now') WHERE id = ?",
            (quantity, inventory_id),
        )
        # Create a bridge-table link so multi-link queries stay consistent
        con.execute(
            "INSERT INTO sale_inventory_links (sale_id, inventory_id, quantity, cost_cents_each) "
            "VALUES (?, ?, ?, ?)",
            (sale_id, inventory_id, quantity, inv["cost_basis_cents"]),
        )
        return sale_id


def get_sales(
    platform: Optional[str] = None,
    days: Optional[int] = None,
    profit_only: bool = False,
) -> List[Dict]:
    sql = "SELECT * FROM outbound_sales WHERE is_deleted = 0"
    params: List[Any] = []
    if platform:
        sql += " AND LOWER(platform) = ?"
        params.append(platform.lower())
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND (date_sold >= ? OR date_listed >= ?)"
        params.extend([cutoff, cutoff])
    if profit_only:
        sql += " AND profit_cents > 0"
    sql += " ORDER BY date_sold DESC, id DESC"
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_sale_by_id(sale_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM outbound_sales WHERE id = ? AND is_deleted = 0", (sale_id,)
        ).fetchone()
        return dict(row) if row else None


def find_unshipped_sale_by_item_name(item_name: str) -> Optional[Dict]:
    """
    Return the most recent eBay sale whose item_name matches (case-insensitive, trimmed)
    and whose status is not yet 'shipped'. Used as a fallback when the label email
    doesn't contain an order number.
    """
    with _conn() as con:
        row = con.execute(
            """SELECT * FROM outbound_sales
               WHERE LOWER(TRIM(item_name)) = LOWER(TRIM(?))
                 AND platform = 'ebay'
                 AND status != 'shipped'
                 AND is_deleted = 0
               ORDER BY created_at DESC
               LIMIT 1""",
            (item_name,),
        ).fetchone()
        return dict(row) if row else None


def sale_exists_by_order_id(source_order_id: str) -> bool:
    """True if a sale with this source_order_id already exists (email dedup)."""
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM outbound_sales WHERE source_order_id = ? AND is_deleted = 0",
            (source_order_id,),
        ).fetchone()
        return row is not None


def get_sale_by_order_id(source_order_id: str) -> Optional[Dict]:
    """Return the sale row for the given source_order_id, or None."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM outbound_sales WHERE source_order_id = ? AND is_deleted = 0",
            (source_order_id,),
        ).fetchone()
        return dict(row) if row else None


def link_sale_to_inventory(sale_id: int, inventory_id: int, qty: int = 1) -> bool:
    """Add a link between a sale and an inventory item (bridge table).

    Deducts *qty* from inventory, records cost per unit, then recalculates
    the sale's total cost_basis / profit / margin from all its links.
    """
    with _conn() as con:
        inv = con.execute(
            "SELECT * FROM inventory WHERE id = ? AND is_deleted = 0", (inventory_id,)
        ).fetchone()
        sale = con.execute(
            "SELECT * FROM outbound_sales WHERE id = ? AND is_deleted = 0", (sale_id,)
        ).fetchone()
        if not inv or not sale:
            return False

        cost_each = inv["cost_basis_cents"]

        # Pull qty from inventory
        con.execute(
            "UPDATE inventory SET quantity = MAX(0, quantity - ?), updated_at = datetime('now') WHERE id = ?",
            (qty, inventory_id),
        )

        # Insert bridge row
        con.execute(
            "INSERT INTO sale_inventory_links (sale_id, inventory_id, quantity, cost_cents_each) "
            "VALUES (?, ?, ?, ?)",
            (sale_id, inventory_id, qty, cost_each),
        )

        # Recalculate cost / profit / margin from all links
        _recalc_sale_cost(con, sale_id)

        # Set inventory_id on the sale to the first linked item (for quick-check)
        first = con.execute(
            "SELECT inventory_id FROM sale_inventory_links WHERE sale_id = ? ORDER BY id LIMIT 1",
            (sale_id,),
        ).fetchone()
        if first:
            con.execute(
                "UPDATE outbound_sales SET inventory_id = ?, updated_at = datetime('now') WHERE id = ?",
                (first["inventory_id"], sale_id),
            )

        return True


def unlink_sale_inventory(link_id: int) -> bool:
    """Remove one bridge-table link and restore inventory."""
    with _conn() as con:
        link = con.execute("SELECT * FROM sale_inventory_links WHERE id = ?", (link_id,)).fetchone()
        if not link:
            return False

        # Restore inventory quantity
        con.execute(
            "UPDATE inventory SET quantity = quantity + ?, updated_at = datetime('now') WHERE id = ?",
            (link["quantity"], link["inventory_id"]),
        )
        con.execute("DELETE FROM sale_inventory_links WHERE id = ?", (link_id,))

        # Recalculate sale cost
        _recalc_sale_cost(con, link["sale_id"])

        # Update inventory_id on the sale
        first = con.execute(
            "SELECT inventory_id FROM sale_inventory_links WHERE sale_id = ? ORDER BY id LIMIT 1",
            (link["sale_id"],),
        ).fetchone()
        con.execute(
            "UPDATE outbound_sales SET inventory_id = ?, updated_at = datetime('now') WHERE id = ?",
            (first["inventory_id"] if first else None, link["sale_id"]),
        )
        return True


def get_sale_inventory_links(sale_id: int) -> List[Dict]:
    """Return all bridge-table links for a sale, with inventory item names."""
    with _conn() as con:
        rows = con.execute(
            """SELECT sil.*, i.item_name AS inv_item_name, i.cost_basis_cents AS inv_cost
               FROM sale_inventory_links sil
               JOIN inventory i ON i.id = sil.inventory_id
               WHERE sil.sale_id = ?
               ORDER BY sil.id""",
            (sale_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_sale_linked_qty(sale_id: int) -> int:
    """Return total quantity already linked for a sale."""
    with _conn() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(quantity), 0) AS total FROM sale_inventory_links WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()
        return row["total"] if row else 0


def _recalc_sale_cost(con, sale_id: int) -> None:
    """Recalculate cost_basis, profit, margin for a sale from its bridge-table links."""
    total_cost = 0
    links = con.execute(
        "SELECT quantity, cost_cents_each FROM sale_inventory_links WHERE sale_id = ?",
        (sale_id,),
    ).fetchall()
    for link in links:
        total_cost += link["quantity"] * link["cost_cents_each"]

    sale = con.execute(
        "SELECT sale_price_cents, platform_fees_cents, shipping_cost_cents FROM outbound_sales WHERE id = ?",
        (sale_id,),
    ).fetchone()
    if not sale:
        return

    profit, margin = _calc_profit(
        sale["sale_price_cents"], total_cost,
        sale["platform_fees_cents"], sale["shipping_cost_cents"],
    )
    con.execute(
        """UPDATE outbound_sales
           SET cost_basis_cents = ?, profit_cents = ?, margin_percent = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (total_cost, profit, margin, sale_id),
    )


def add_sale_from_email(
    item_name: str,
    platform: str,
    sale_price_cents: int,
    platform_fees_cents: int = 0,
    shipping_cost_cents: int = 0,
    date_sold: Optional[str] = None,
    buyer_info: Optional[str] = None,
    tracking_number: Optional[str] = None,
    source_order_id: Optional[str] = None,
    quantity: int = 1,
) -> int:
    """Insert a sale imported from email with no linked inventory item."""
    today = datetime.now().strftime("%Y-%m-%d")
    profit, margin = _calc_profit(sale_price_cents, 0, platform_fees_cents, shipping_cost_cents)
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO outbound_sales
               (inventory_id, item_name, platform, sale_price_cents, platform_fees_cents,
                shipping_cost_cents, cost_basis_cents, profit_cents, margin_percent,
                tracking_number, buyer_info, date_sold, status, source_order_id, quantity)
               VALUES (NULL, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'sold', ?, ?)""",
            (
                item_name, platform, sale_price_cents, platform_fees_cents,
                shipping_cost_cents, profit, margin,
                tracking_number, buyer_info,
                date_sold or today, source_order_id, quantity,
            ),
        )
        return cur.lastrowid


def _apply_shipped_update(
    con,
    sale_id: int,
    tracking_number: Optional[str],
    shipping_cost_cents: int,
) -> None:
    """Internal: apply shipped status + tracking + recalculate profit on a known sale row."""
    row = con.execute(
        "SELECT sale_price_cents, platform_fees_cents, shipping_cost_cents, cost_basis_cents "
        "FROM outbound_sales WHERE id = ?",
        (sale_id,),
    ).fetchone()
    new_shipping = shipping_cost_cents if shipping_cost_cents > 0 else row["shipping_cost_cents"]
    profit, margin = _calc_profit(
        row["sale_price_cents"], row["cost_basis_cents"],
        row["platform_fees_cents"], new_shipping,
    )
    con.execute(
        """UPDATE outbound_sales
           SET status = 'shipped',
               tracking_number = COALESCE(?, tracking_number),
               shipping_cost_cents = ?,
               profit_cents = ?,
               margin_percent = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (tracking_number, new_shipping, profit, margin, sale_id),
    )


def update_sale_shipped_by_order_id(
    source_order_id: str,
    tracking_number: Optional[str] = None,
    shipping_cost_cents: int = 0,
) -> bool:
    """
    Mark a sale as 'shipped' by source_order_id, set tracking_number, and
    recalculate profit/margin with the actual postage cost.
    Returns True if a row was found and updated.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM outbound_sales WHERE source_order_id = ? AND is_deleted = 0",
            (source_order_id,),
        ).fetchone()
        if not row:
            return False
        _apply_shipped_update(con, row["id"], tracking_number, shipping_cost_cents)
        return True


def update_sale_shipped_by_sale_id(
    sale_id: int,
    tracking_number: Optional[str] = None,
    shipping_cost_cents: int = 0,
) -> bool:
    """Same as update_sale_shipped_by_order_id but looks up by primary key."""
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM outbound_sales WHERE id = ? AND is_deleted = 0",
            (sale_id,),
        ).fetchone()
        if not row:
            return False
        _apply_shipped_update(con, sale_id, tracking_number, shipping_cost_cents)
        return True


_SALE_FIELDS = {
    "status", "tracking_number", "buyer_info", "date_listed", "date_sold",
    "platform", "sale_price_cents", "platform_fees_cents", "shipping_cost_cents",
    "quantity",
}


def update_sale(sale_id: int, field: str, value: Any) -> bool:
    if field not in _SALE_FIELDS:
        return False
    with _conn() as con:
        con.execute(
            f"UPDATE outbound_sales SET {field} = ?, updated_at = datetime('now') WHERE id = ? AND is_deleted = 0",
            (value, sale_id),
        )
        if field in {"sale_price_cents", "platform_fees_cents", "shipping_cost_cents"}:
            row = con.execute(
                "SELECT sale_price_cents, platform_fees_cents, shipping_cost_cents, cost_basis_cents FROM outbound_sales WHERE id = ?",
                (sale_id,),
            ).fetchone()
            if row:
                profit, margin = _calc_profit(
                    row["sale_price_cents"], row["cost_basis_cents"],
                    row["platform_fees_cents"], row["shipping_cost_cents"],
                )
                con.execute(
                    "UPDATE outbound_sales SET profit_cents = ?, margin_percent = ?, updated_at = datetime('now') WHERE id = ?",
                    (profit, margin, sale_id),
                )
    return True


def delete_sale(sale_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE outbound_sales SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
            (sale_id,),
        )


# ── Business Expenses ─────────────────────────────────────────────────────────

RECURRENCE_INTERVALS = ["daily", "weekly", "biweekly", "monthly", "quarterly", "yearly"]


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year = dt.year + m // 12
    month = m % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def _calc_next_due(base_date: str, interval: str) -> Optional[str]:
    if not interval:
        return None
    dt = datetime.strptime(base_date, "%Y-%m-%d")
    if interval == "daily":
        dt += timedelta(days=1)
    elif interval == "weekly":
        dt += timedelta(weeks=1)
    elif interval == "biweekly":
        dt += timedelta(weeks=2)
    elif interval == "monthly":
        dt = _add_months(dt, 1)
    elif interval == "quarterly":
        dt = _add_months(dt, 3)
    elif interval == "yearly":
        dt = _add_months(dt, 12)
    else:
        return None
    return dt.strftime("%Y-%m-%d")


def add_expense(
    expense_name: str,
    amount_cents: int,
    expense_date: str,
    vendor: Optional[str] = None,
    payment_method: Optional[str] = None,
    notes: Optional[str] = None,
    is_recurring: bool = False,
    recurrence_interval: Optional[str] = None,
    tax_deductible: bool = True,
    category: str = "General",
) -> int:
    next_due = _calc_next_due(expense_date, recurrence_interval) if is_recurring and recurrence_interval else None
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO business_expenses
               (expense_name, amount_cents, expense_date, vendor, payment_method, notes,
                is_recurring, recurrence_interval, next_due_date, tax_deductible, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (expense_name, amount_cents, expense_date, vendor, payment_method, notes,
             1 if is_recurring else 0, recurrence_interval, next_due, 1 if tax_deductible else 0,
             category or "General"),
        )
        exp_id = cur.lastrowid
    if LARGE_EXPENSE_THRESHOLD_CENTS > 0 and amount_cents >= LARGE_EXPENSE_THRESHOLD_CENTS:
        try:
            from webhooks import notify_large_expense
            notify_large_expense(exp_id, expense_name, amount_cents)
        except Exception:
            pass
    return exp_id


def get_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
    year: Optional[int] = None,
    recurring_filter: Optional[str] = None,  # "recurring" | "one_time"
    tax_deductible: Optional[bool] = None,
    search: Optional[str] = None,
    min_cents: Optional[int] = None,
    max_cents: Optional[int] = None,
) -> List[Dict]:
    sql = "SELECT * FROM business_expenses WHERE is_deleted = 0"
    params: List[Any] = []

    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND expense_date >= ?"
        params.append(cutoff)
    elif year is not None:
        sql += " AND expense_date >= ? AND expense_date <= ?"
        params.extend([f"{year}-01-01", f"{year}-12-31"])
    else:
        if start_date:
            sql += " AND expense_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND expense_date <= ?"
            params.append(end_date)

    if recurring_filter == "recurring":
        sql += " AND is_recurring = 1"
    elif recurring_filter == "one_time":
        sql += " AND is_recurring = 0"

    if tax_deductible is True:
        sql += " AND tax_deductible = 1"
    elif tax_deductible is False:
        sql += " AND tax_deductible = 0"

    if search:
        sql += " AND (LOWER(expense_name) LIKE ? OR LOWER(COALESCE(vendor,'')) LIKE ?)"
        params.extend([f"%{search.lower()}%", f"%{search.lower()}%"])

    if min_cents is not None:
        sql += " AND amount_cents >= ?"
        params.append(min_cents)
    if max_cents is not None:
        sql += " AND amount_cents <= ?"
        params.append(max_cents)

    sql += " ORDER BY expense_date DESC, id DESC"
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_expense_by_id(expense_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM business_expenses WHERE id = ? AND is_deleted = 0", (expense_id,)
        ).fetchone()
        return dict(row) if row else None


_EXPENSE_FIELDS = {
    "expense_name", "amount_cents", "expense_date", "vendor", "payment_method",
    "notes", "is_recurring", "recurrence_interval", "next_due_date", "tax_deductible",
    "category",
}


def get_expense_category_breakdown(year: Optional[int] = None) -> List[Dict]:
    """Return [{category, total_cents}] sorted by total desc."""
    sql = "SELECT COALESCE(category,'General') AS category, SUM(amount_cents) AS total_cents FROM business_expenses WHERE is_deleted=0"
    params: list = []
    if year:
        sql += " AND expense_date LIKE ?"
        params.append(f"{year}%")
    sql += " GROUP BY category ORDER BY total_cents DESC"
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def update_expense(expense_id: int, field: str, value: Any) -> bool:
    if field not in _EXPENSE_FIELDS:
        return False
    with _conn() as con:
        con.execute(
            f"UPDATE business_expenses SET {field} = ?, updated_at = datetime('now') WHERE id = ? AND is_deleted = 0",
            (value, expense_id),
        )
    return True


def delete_expense(expense_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE business_expenses SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
            (expense_id,),
        )


def process_recurring_expenses() -> List[int]:
    """
    Find all recurring expense templates whose next_due_date <= today and clone them
    as one-time instances. Loops until all templates are advanced past today so
    missed days are caught up automatically.
    Returns IDs of all newly created instance records.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    new_ids: List[int] = []

    while True:
        with _conn() as con:
            due = con.execute(
                """SELECT * FROM business_expenses
                   WHERE is_recurring = 1 AND is_deleted = 0 AND next_due_date <= ?""",
                (today,),
            ).fetchall()
        if not due:
            break

        for expense in due:
            due_date = expense["next_due_date"]
            with _conn() as con:
                cur = con.execute(
                    """INSERT INTO business_expenses
                       (expense_name, amount_cents, expense_date, vendor, payment_method,
                        notes, is_recurring, recurrence_interval, tax_deductible)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (expense["expense_name"], expense["amount_cents"], due_date,
                     expense["vendor"], expense["payment_method"], expense["notes"],
                     expense["recurrence_interval"], expense["tax_deductible"]),
                )
                new_ids.append(cur.lastrowid)
            next_due = _calc_next_due(due_date, expense["recurrence_interval"])
            with _conn() as con:
                con.execute(
                    "UPDATE business_expenses SET next_due_date = ?, updated_at = datetime('now') WHERE id = ?",
                    (next_due, expense["id"]),
                )
    return new_ids


def get_expense_totals() -> Dict:
    """Month-to-date and year-to-date expense totals (instances only, not templates)."""
    now = datetime.now()
    month_start = now.strftime("%Y-%m-01")
    year_start  = now.strftime("%Y-01-01")
    with _conn() as con:
        month = con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM business_expenses WHERE is_deleted=0 AND is_recurring=0 AND expense_date>=?",
            (month_start,),
        ).fetchone()[0]
        year = con.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM business_expenses WHERE is_deleted=0 AND is_recurring=0 AND expense_date>=?",
            (year_start,),
        ).fetchone()[0]
    return {"month_cents": month, "year_cents": year}


# ── Reports ──────────────────────────────────────────────────────────────────

def get_profit_report(
    period: Optional[str] = None,
    platform: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict:
    # Determine date cutoff for the period
    cutoff: Optional[str] = None
    if period and period != "all":
        offsets = {"today": 0, "week": 7, "month": 30, "year": 365}
        d = offsets.get(period)
        if d is not None:
            cutoff = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")

    sql = """
        SELECT os.*, inv.category
        FROM outbound_sales os
        JOIN inventory inv ON inv.id = os.inventory_id
        WHERE os.is_deleted = 0 AND os.status NOT IN ('cancelled', 'refunded')
    """
    params: List[Any] = []
    if cutoff:
        sql += " AND os.date_sold >= ?"
        params.append(cutoff)
    if platform:
        sql += " AND LOWER(os.platform) = ?"
        params.append(platform.lower())
    if category:
        sql += " AND LOWER(inv.category) LIKE ?"
        params.append(f"%{category.lower()}%")

    with _conn() as con:
        rows = con.execute(sql, params).fetchall()

    revenue       = sum(r["sale_price_cents"] for r in rows)
    cogs          = sum(r["cost_basis_cents"] for r in rows)
    fees_shipping = sum(r["platform_fees_cents"] + r["shipping_cost_cents"] for r in rows)
    gross_profit  = revenue - cogs - fees_shipping

    # Expense instances for the same period (exclude recurring templates)
    exp_rows       = get_expenses(start_date=cutoff, recurring_filter="one_time")
    expenses_total = sum(e["amount_cents"] for e in exp_rows)

    net_profit = gross_profit - expenses_total
    net_margin = (net_profit / revenue * 100) if revenue > 0 else 0.0

    return {
        # Full breakdown (new)
        "revenue_cents":       revenue,
        "cogs_cents":          cogs,
        "fees_shipping_cents": fees_shipping,
        "gross_profit_cents":  gross_profit,
        "expenses_cents":      expenses_total,
        "net_profit_cents":    net_profit,
        "net_margin_percent":  net_margin,
        "sale_count":          len(rows),
        # Legacy keys kept for backward compatibility
        "total_revenue_cents": revenue,
        "total_costs_cents":   cogs + fees_shipping,
        "total_profit_cents":  net_profit,
        "margin_percent":      net_margin,
    }


def get_inventory_report() -> Dict:
    with _conn() as con:
        rows = con.execute("SELECT * FROM inventory WHERE is_deleted = 0").fetchall()

    today = datetime.now()
    total_units = sum(r["quantity"] for r in rows)
    total_value = sum(r["cost_basis_cents"] * r["quantity"] for r in rows)
    in_stock = sum(1 for r in rows if r["quantity"] > 0)
    out_of_stock = sum(1 for r in rows if r["quantity"] == 0)

    aged_30 = aged_60 = aged_90 = 0
    for r in rows:
        if r["quantity"] > 0 and r["date_received"]:
            try:
                recv = datetime.strptime(r["date_received"], "%Y-%m-%d")
                age = (today - recv).days
                if age > 90:
                    aged_90 += r["quantity"]
                elif age > 60:
                    aged_60 += r["quantity"]
                elif age > 30:
                    aged_30 += r["quantity"]
            except ValueError:
                pass

    return {
        "total_units": total_units,
        "total_value_cents": total_value,
        "in_stock_records": in_stock,
        "out_of_stock_records": out_of_stock,
        "aged_30_days": aged_30,
        "aged_60_days": aged_60,
        "aged_90_days": aged_90,
    }


def get_dashboard_data(period: str = "month") -> Dict:
    """Return all metrics needed by the Dashboard tab for the given period."""
    now = datetime.now()

    # ── Date ranges ────────────────────────────────────────────────────────────
    if period == "week":
        cur_start  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_start = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        prev_end   = cur_start
    elif period == "days30":
        cur_start  = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        prev_start = (now - timedelta(days=60)).strftime("%Y-%m-%d")
        prev_end   = cur_start
    elif period == "month":
        cur_start  = now.strftime("%Y-%m-01")
        pm         = _add_months(now, -1)
        prev_start = pm.strftime("%Y-%m-01")
        prev_end   = cur_start
    elif period == "year":
        cur_start  = now.strftime("%Y-01-01")
        prev_start = now.replace(year=now.year - 1).strftime("%Y-01-01")
        prev_end   = cur_start
    else:  # all
        cur_start = prev_start = prev_end = None

    def _af(col: str, start: Optional[str], end: Optional[str] = None) -> Tuple[str, List]:
        """Build ' AND col >= ? [AND col < ?]' fragment + params."""
        parts: List[str] = []
        params: List[Any] = []
        if start:
            parts.append(f"{col} >= ?"); params.append(start)
        if end:
            parts.append(f"{col} < ?");  params.append(end)
        return (" AND " + " AND ".join(parts) if parts else ""), params

    def _q(con, sql: str, params: List = []) -> int:
        return con.execute(sql, params).fetchone()[0] or 0

    with _conn() as con:
        # ── Cards 1-2: Orders & Product Cost ──────────────────────────────────
        cf, cp   = _af("order_date",    cur_start)
        pf, pp   = _af("order_date",    prev_start, prev_end)
        cf2, cp2 = _af("io.order_date", cur_start)
        pf2, pp2 = _af("io.order_date", prev_start, prev_end)

        cur_orders  = _q(con, f"SELECT COUNT(*) FROM inbound_orders WHERE is_deleted=0 AND status!='cancelled'{cf}", cp)
        prev_orders = _q(con, f"SELECT COUNT(*) FROM inbound_orders WHERE is_deleted=0 AND status!='cancelled'{pf}", pp)

        _cost_sql = (
            "SELECT COALESCE(SUM(ioi.cost_cents * ioi.quantity),0) "
            "FROM inbound_order_items ioi "
            "JOIN inbound_orders io ON io.id=ioi.order_id "
            "WHERE io.is_deleted=0 AND ioi.is_deleted=0 AND io.status!='cancelled'"
        )
        cur_cost  = _q(con, _cost_sql + cf2, cp2)
        prev_cost = _q(con, _cost_sql + pf2, pp2)

        # ── Cards 3 & 5: Revenue & Net Profit ─────────────────────────────────
        cf3, cp3 = _af("date_sold", cur_start)
        pf3, pp3 = _af("date_sold", prev_start, prev_end)
        _sb = "FROM outbound_sales WHERE is_deleted=0 AND status NOT IN ('cancelled','refunded')"

        cur_revenue      = _q(con, f"SELECT COALESCE(SUM(sale_price_cents),0) {_sb}{cf3}", cp3)
        prev_revenue     = _q(con, f"SELECT COALESCE(SUM(sale_price_cents),0) {_sb}{pf3}", pp3)
        cur_gross_profit = _q(con, f"SELECT COALESCE(SUM(profit_cents),0) {_sb}{cf3}", cp3)
        prev_gross_profit= _q(con, f"SELECT COALESCE(SUM(profit_cents),0) {_sb}{pf3}", pp3)

        # ── Card 4: Expenses ───────────────────────────────────────────────────
        cf4, cp4 = _af("expense_date", cur_start)
        pf4, pp4 = _af("expense_date", prev_start, prev_end)
        _eb = "FROM business_expenses WHERE is_deleted=0 AND is_recurring=0"

        cur_expenses  = _q(con, f"SELECT COALESCE(SUM(amount_cents),0) {_eb}{cf4}", cp4)
        prev_expenses = _q(con, f"SELECT COALESCE(SUM(amount_cents),0) {_eb}{pf4}", pp4)

        cur_net  = cur_gross_profit  - cur_expenses
        prev_net = prev_gross_profit - prev_expenses

        # ── Card 6: Inventory ──────────────────────────────────────────────────
        inv_rows  = con.execute("SELECT cost_basis_cents, quantity FROM inventory WHERE is_deleted=0").fetchall()
        inv_value = sum(r["cost_basis_cents"] * r["quantity"] for r in inv_rows)
        inv_units = sum(r["quantity"] for r in inv_rows)
        inv_items = sum(1 for r in inv_rows if r["quantity"] > 0)

        # ── Orders by retailer (pie chart) ────────────────────────────────────
        retailer_rows = con.execute(
            f"SELECT retailer, COUNT(*) AS cnt "
            f"FROM inbound_orders WHERE is_deleted=0{cf} "
            f"GROUP BY retailer ORDER BY cnt DESC",
            cp,
        ).fetchall()
        orders_by_retailer = [{"retailer": r["retailer"], "count": r["cnt"]}
                               for r in retailer_rows]

        # ── Stick rate (all time — not period-filtered) ────────────────────────
        total_all   = _q(con, "SELECT COUNT(*) FROM inbound_orders WHERE is_deleted=0")
        cancelled_all = _q(con, "SELECT COUNT(*) FROM inbound_orders WHERE is_deleted=0 AND status='cancelled'")
        stuck_all   = total_all - cancelled_all

        # ── Secondary stats (Zone 3) ───────────────────────────────────────────
        sale_count = _q(con, f"SELECT COUNT(*) {_sb}{cf3}", cp3)
        avg_sale   = cur_revenue // sale_count if sale_count else 0
        avg_profit = cur_gross_profit // sale_count if sale_count else 0

        best_day_row = con.execute(
            f"SELECT date_sold, COUNT(*) AS cnt {_sb}{cf3} "
            f"GROUP BY date_sold ORDER BY cnt DESC LIMIT 1",
            cp3,
        ).fetchone()
        best_day = (f"{best_day_row['date_sold']} · {best_day_row['cnt']} sales"
                    if best_day_row else "—")

        # ── Recent activity (Zone 4) ───────────────────────────────────────────
        r_sales = con.execute(
            "SELECT item_name, date_sold AS date, sale_price_cents AS amt "
            "FROM outbound_sales WHERE is_deleted=0 ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
        r_orders = con.execute(
            "SELECT ioi.item_name, io.order_date AS date, ioi.cost_cents AS amt "
            "FROM inbound_orders io "
            "JOIN inbound_order_items ioi ON ioi.order_id=io.id "
            "WHERE io.is_deleted=0 AND ioi.is_deleted=0 AND io.status!='cancelled' "
            "ORDER BY io.created_at DESC LIMIT 6"
        ).fetchall()
        r_exp = con.execute(
            "SELECT expense_name AS item_name, expense_date AS date, amount_cents AS amt "
            "FROM business_expenses WHERE is_deleted=0 AND is_recurring=0 "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall()

    activities = (
        [{"type": "Sale",    "label": r["item_name"], "date": r["date"], "amount": r["amt"]} for r in r_sales] +
        [{"type": "Order",   "label": r["item_name"], "date": r["date"], "amount": r["amt"]} for r in r_orders] +
        [{"type": "Expense", "label": r["item_name"], "date": r["date"], "amount": r["amt"]} for r in r_exp]
    )
    activities.sort(key=lambda x: x["date"] or "", reverse=True)

    return {
        "cur_orders":   cur_orders,    "prev_orders":   prev_orders,
        "cur_cost":     cur_cost,      "prev_cost":     prev_cost,
        "cur_revenue":  cur_revenue,   "prev_revenue":  prev_revenue,
        "cur_expenses": cur_expenses,  "prev_expenses": prev_expenses,
        "cur_net":      cur_net,       "prev_net":      prev_net,
        "inv_value":    inv_value,     "inv_units":     inv_units,   "inv_items": inv_items,
        "orders_by_retailer": orders_by_retailer,
        "stick_total":    total_all,
        "stick_cancelled":cancelled_all,
        "stick_stuck":    stuck_all,
        "sale_count":     sale_count,
        "avg_sale_cents": avg_sale,
        "avg_profit_cents": avg_profit,
        "best_day":       best_day,
        "recent_activity": activities[:15],
    }


def get_expense_report(period: Optional[str] = "month") -> Dict:
    cutoff: Optional[str] = None
    if period and period != "all":
        offsets = {"today": 0, "week": 7, "month": 30, "year": 365}
        d = offsets.get(period)
        if d is not None:
            cutoff = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")

    instances = get_expenses(start_date=cutoff, recurring_filter="one_time")
    total      = sum(e["amount_cents"] for e in instances)
    tax_ded    = sum(e["amount_cents"] for e in instances if e["tax_deductible"])
    # Instances that came from a recurring template carry recurrence_interval
    rec_inst   = sum(e["amount_cents"] for e in instances if e.get("recurrence_interval"))
    one_time   = total - rec_inst
    largest    = max(instances, key=lambda e: e["amount_cents"], default=None)

    return {
        "total_cents":           total,
        "tax_deductible_cents":  tax_ded,
        "recurring_cents":       rec_inst,
        "one_time_cents":        one_time,
        "expense_count":         len(instances),
        "largest_expense":       largest,
    }


# ── Listings ──────────────────────────────────────────────────────────────────

def add_listing(
    item_name: str,
    platform: str,
    listing_price_cents: int,
    inventory_id: Optional[int] = None,
    listing_id: Optional[str] = None,
    listing_url: Optional[str] = None,
    date_listed: Optional[str] = None,
    date_ended: Optional[str] = None,
    notes: Optional[str] = None,
    source: str = "manual",
    source_email_id: Optional[str] = None,
    estimated_payout_cents: Optional[int] = None,
    size_variant: Optional[str] = None,
) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO listings
               (inventory_id, item_name, platform, listing_price_cents, listing_id,
                listing_url, status, date_listed, date_ended, notes, source,
                source_email_id, estimated_payout_cents, size_variant)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
            (inventory_id, item_name, platform, listing_price_cents, listing_id,
             listing_url, date_listed or today, date_ended, notes, source,
             source_email_id, estimated_payout_cents, size_variant),
        )
        return cur.lastrowid


def get_listings(
    platform: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict]:
    sql = "SELECT * FROM listings WHERE is_deleted = 0"
    params: List[Any] = []
    if platform:
        sql += " AND LOWER(platform) = ?"
        params.append(platform.lower())
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY date_listed DESC, id DESC"
    with _conn() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_listing_by_id(listing_id: int) -> Optional[Dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM listings WHERE id = ? AND is_deleted = 0", (listing_id,)
        ).fetchone()
        return dict(row) if row else None


def listing_exists_by_email_id(email_id: str) -> bool:
    with _conn() as con:
        return bool(con.execute(
            "SELECT 1 FROM listings WHERE source_email_id = ? AND is_deleted = 0",
            (email_id,),
        ).fetchone())


def update_listing(listing_id: int, field: str, value: Any) -> None:
    _ALLOWED = {
        "status", "listing_price_cents", "listing_id", "listing_url",
        "date_listed", "date_ended", "notes", "item_name", "platform",
        "inventory_id", "estimated_payout_cents", "size_variant",
    }
    if field not in _ALLOWED:
        raise ValueError(f"update_listing: field '{field}' not allowed")
    with _conn() as con:
        con.execute(
            f"UPDATE listings SET {field} = ?, updated_at = datetime('now') "
            f"WHERE id = ? AND is_deleted = 0",
            (value, listing_id),
        )


def find_active_listing_for_sale(item_name: str, platform: str) -> Optional[Dict]:
    """Find an active listing on *platform* whose name fuzzy-matches *item_name*."""
    import re as _re
    def _words(s: str):
        return set(_re.sub(r'[^a-z0-9]', ' ', s.lower()).split())

    target = _words(item_name)
    if not target:
        return None

    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM listings WHERE is_deleted = 0 AND status = 'active' "
            "AND LOWER(platform) = ? ORDER BY date_listed DESC",
            (platform.lower(),),
        ).fetchall()

    best, best_score = None, 0.0
    for row in rows:
        existing = _words(row["item_name"])
        union = target | existing
        if not union:
            continue
        score = len(target & existing) / len(union)
        if score > best_score:
            best, best_score = dict(row), score

    return best if best_score >= 0.6 else None


def mark_listing_sold(listing_id: int, date_sold: Optional[str] = None) -> None:
    """Set a listing's status to 'sold' and record the end date."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as con:
        con.execute(
            "UPDATE listings SET status = 'sold', date_ended = ?, updated_at = datetime('now') "
            "WHERE id = ? AND is_deleted = 0",
            (date_sold or today, listing_id),
        )


def delete_listing(listing_id: int) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE listings SET is_deleted = 1, updated_at = datetime('now') WHERE id = ?",
            (listing_id,),
        )
