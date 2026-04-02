"""
Versioned schema migrations for tracker.db.

HOW TO ADD A MIGRATION
─────────────────────
Append a new tuple to MIGRATIONS:

    (
        <next_version_int>,
        "<short description>",
        "<SQL to execute>",
    ),

Rules:
  - Never edit or reorder existing entries — only append.
  - Use ALTER TABLE … ADD COLUMN for additive changes.
  - Use CREATE TABLE IF NOT EXISTS for new tables.
  - The SQL string may contain multiple statements separated by semicolons.
  - Version numbers must be consecutive integers starting at 1.
"""

import sqlite3
from typing import List, Tuple

from config import DB_PATH

# ── Migrations tracking table ─────────────────────────────────────────────────

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# ── Migration list ─────────────────────────────────────────────────────────────
# (version, description, sql)

MIGRATIONS: List[Tuple[int, str, str]] = [
    (
        1,
        "Initial schema",
        """
        CREATE TABLE IF NOT EXISTS inbound_orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number     TEXT    NOT NULL,
            retailer         TEXT    NOT NULL,
            order_date       TEXT    NOT NULL,
            status           TEXT    NOT NULL DEFAULT 'ordered',
            tracking_number  TEXT,
            delivery_address TEXT,
            notes            TEXT,
            is_deleted       INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS inbound_order_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   INTEGER NOT NULL REFERENCES inbound_orders(id),
            item_name  TEXT    NOT NULL,
            sku        TEXT,
            cost_cents INTEGER NOT NULL,
            quantity   INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS inventory (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name             TEXT    NOT NULL,
            sku                   TEXT,
            category              TEXT    NOT NULL DEFAULT 'General',
            size_variant          TEXT,
            condition             TEXT    NOT NULL DEFAULT 'new',
            cost_basis_cents      INTEGER NOT NULL,
            quantity              INTEGER NOT NULL DEFAULT 0,
            storage_location      TEXT,
            date_received         TEXT    NOT NULL,
            inbound_order_item_id INTEGER REFERENCES inbound_order_items(id),
            is_deleted            INTEGER NOT NULL DEFAULT 0,
            created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS outbound_sales (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id         INTEGER NOT NULL REFERENCES inventory(id),
            item_name            TEXT    NOT NULL,
            platform             TEXT    NOT NULL,
            sale_price_cents     INTEGER NOT NULL,
            platform_fees_cents  INTEGER NOT NULL DEFAULT 0,
            shipping_cost_cents  INTEGER NOT NULL DEFAULT 0,
            cost_basis_cents     INTEGER NOT NULL,
            profit_cents         INTEGER NOT NULL,
            margin_percent       REAL    NOT NULL,
            tracking_number      TEXT,
            buyer_info           TEXT,
            date_listed          TEXT,
            date_sold            TEXT,
            status               TEXT    NOT NULL DEFAULT 'sold',
            is_deleted           INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),

    (
        2,
        "Add business_expenses table",
        """
        CREATE TABLE IF NOT EXISTS business_expenses (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_name         TEXT    NOT NULL,
            amount_cents         INTEGER NOT NULL,
            expense_date         TEXT    NOT NULL,
            vendor               TEXT,
            payment_method       TEXT,
            notes                TEXT,
            is_recurring         INTEGER NOT NULL DEFAULT 0,
            recurrence_interval  TEXT,
            next_due_date        TEXT,
            tax_deductible       INTEGER NOT NULL DEFAULT 1,
            is_deleted           INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),

    (
        3,
        "Make outbound_sales.inventory_id nullable; add source_order_id for email dedup",
        """
        ALTER TABLE outbound_sales RENAME TO outbound_sales_old;

        CREATE TABLE outbound_sales (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id         INTEGER REFERENCES inventory(id),
            item_name            TEXT    NOT NULL,
            platform             TEXT    NOT NULL,
            sale_price_cents     INTEGER NOT NULL,
            platform_fees_cents  INTEGER NOT NULL DEFAULT 0,
            shipping_cost_cents  INTEGER NOT NULL DEFAULT 0,
            cost_basis_cents     INTEGER NOT NULL DEFAULT 0,
            profit_cents         INTEGER NOT NULL DEFAULT 0,
            margin_percent       REAL    NOT NULL DEFAULT 0,
            tracking_number      TEXT,
            buyer_info           TEXT,
            date_listed          TEXT,
            date_sold            TEXT,
            status               TEXT    NOT NULL DEFAULT 'sold',
            source_order_id      TEXT,
            is_deleted           INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        INSERT INTO outbound_sales
            (id, inventory_id, item_name, platform, sale_price_cents, platform_fees_cents,
             shipping_cost_cents, cost_basis_cents, profit_cents, margin_percent,
             tracking_number, buyer_info, date_listed, date_sold, status,
             is_deleted, created_at, updated_at)
        SELECT
            id, inventory_id, item_name, platform, sale_price_cents, platform_fees_cents,
            shipping_cost_cents, cost_basis_cents, profit_cents, margin_percent,
            tracking_number, buyer_info, date_listed, date_sold, status,
            is_deleted, created_at, updated_at
        FROM outbound_sales_old;

        DROP TABLE outbound_sales_old;
        """,
    ),

    (
        4,
        "Add tracking status fields to inbound_orders",
        """
        ALTER TABLE inbound_orders ADD COLUMN tracking_carrier    TEXT;
        ALTER TABLE inbound_orders ADD COLUMN tracking_status     TEXT;
        ALTER TABLE inbound_orders ADD COLUMN estimated_delivery  TEXT;
        ALTER TABLE inbound_orders ADD COLUMN tracking_checked_at TEXT;
        """,
    ),
    (
        5,
        "Add return_reminders table",
        """
        CREATE TABLE IF NOT EXISTS return_reminders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id     INTEGER NOT NULL REFERENCES inventory(id),
            item_name        TEXT    NOT NULL,
            order_date       TEXT    NOT NULL,
            reminder_date    TEXT    NOT NULL,
            last_notified    TEXT,
            is_active        INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        6,
        "Add days column to return_reminders",
        """
        ALTER TABLE return_reminders ADD COLUMN days INTEGER NOT NULL DEFAULT 25;
        """,
    ),
    (
        7,
        "Add account_email to inbound_orders",
        """
        ALTER TABLE inbound_orders ADD COLUMN account_email TEXT;
        """,
    ),
    (
        8,
        "Add category to business_expenses",
        """
        ALTER TABLE business_expenses ADD COLUMN category TEXT NOT NULL DEFAULT 'General';
        """,
    ),
    (
        9,
        "Add image_url and image_path to inbound_order_items",
        """
        ALTER TABLE inbound_order_items ADD COLUMN image_url  TEXT;
        ALTER TABLE inbound_order_items ADD COLUMN image_path TEXT;
        """,
    ),
    (
        10,
        "Add listings table for active/pending platform listings",
        """
        CREATE TABLE IF NOT EXISTS listings (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id         INTEGER REFERENCES inventory(id),
            item_name            TEXT    NOT NULL,
            platform             TEXT    NOT NULL,
            listing_price_cents  INTEGER NOT NULL DEFAULT 0,
            listing_id           TEXT,
            listing_url          TEXT,
            status               TEXT    NOT NULL DEFAULT 'active',
            date_listed          TEXT,
            date_ended           TEXT,
            notes                TEXT,
            source               TEXT    NOT NULL DEFAULT 'manual',
            source_email_id      TEXT,
            is_deleted           INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        11,
        "Add estimated_payout_cents and size_variant to listings",
        """
        ALTER TABLE listings ADD COLUMN estimated_payout_cents INTEGER;
        ALTER TABLE listings ADD COLUMN size_variant TEXT;
        """,
    ),
    (
        12,
        "Add quantity to outbound_sales and sale_inventory_links table",
        """
        ALTER TABLE outbound_sales ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1;

        CREATE TABLE IF NOT EXISTS sale_inventory_links (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id          INTEGER NOT NULL REFERENCES outbound_sales(id),
            inventory_id     INTEGER NOT NULL REFERENCES inventory(id),
            quantity         INTEGER NOT NULL DEFAULT 1,
            cost_cents_each  INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    # ── Add new migrations below this line ────────────────────────────────────
]


# ── Runner ────────────────────────────────────────────────────────────────────

def run_migrations() -> List[int]:
    """
    Bootstrap the schema_migrations table then apply every unapplied migration in order.
    Returns the list of version numbers newly applied this run.
    Safe to call on an existing database — already-applied migrations are skipped.
    """
    # Bootstrap: ensure the tracking table exists before we query it.
    con = sqlite3.connect(DB_PATH)
    con.executescript(_BOOTSTRAP)
    con.close()

    applied: List[int] = []

    for version, description, sql in MIGRATIONS:
        # Check whether this version has already been applied.
        con = sqlite3.connect(DB_PATH)
        already = con.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        con.close()

        if already:
            continue

        # Apply the DDL. executescript issues an implicit COMMIT first, so it
        # works correctly for CREATE TABLE / ALTER TABLE statements.
        con = sqlite3.connect(DB_PATH)
        con.executescript(sql)
        con.close()

        # Record the migration as applied.
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
            (version, description),
        )
        con.commit()
        con.close()

        applied.append(version)
        print(f"[migrations] Applied v{version}: {description}")

    return applied


def applied_versions() -> List[dict]:
    """Return a list of all applied migrations for diagnostics."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT version, description, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []  # Table doesn't exist yet
    finally:
        con.close()
