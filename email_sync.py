"""
Email sync: fetch Gmail order emails and import them into the tracker DB.

On each run:
  1. Reads sync_state.json for the last sync time and already-processed Message-IDs.
  2. Fetches emails since that date via IMAP.
  3. Parses each with the appropriate retailer parser.
  4. Upserts orders/items into the DB (deduplicates by order number + item name).
  5. Writes sync_state.json back.

Returns {"imported": int, "updated": int, "skipped": int, "errors": int}.
"""

import imaplib
import email as email_lib
from email.header import decode_header
from datetime import datetime, timedelta
import json
import os
import re
from typing import Callable, Dict, List, Optional

import config
import database as db
import webhooks
from parsers import pokemon_center as _pkc, walmart as _wm, target as _tgt, ebay as _ebay
from parsers import stockx as _stockx
from parsers import five_below as _fb
from parsers import topps as _topps
from parsers import shopify_generic as _shopify
from parsers import nike as _nike
from parsers import bestbuy as _bestbuy
from parsers import amazon as _amazon

# ── Constants ────────────────────────────────────────────────────────────────

# Parsers whose results go into inbound_orders
INBOUND_PARSERS = {
    "pokemon_center": _pkc,
    "walmart":        _wm,
    "target":         _tgt,
    "five_below":     _fb,
    "topps":          _topps,
    "nike":           _nike,
    "bestbuy":        _bestbuy,
    "amazon":         _amazon,
    "shopify":        _shopify,
}

# Parsers whose results go into outbound_sales
SALE_PARSERS = {
    "ebay":   _ebay,
    "stockx": _stockx,
}

# Parsers whose results go into listings (listing emails from sale-platform parsers
# are routed here too when email_type == "listing")
LISTING_PARSERS = {}

RETAILER_PARSERS = {**INBOUND_PARSERS, **SALE_PARSERS, **LISTING_PARSERS}

RETAILER_SUBJECTS_IMAP = {
    "pokemon_center": [
        "Thank you for shopping at PokemonCenter.com",
        "order is on its way",
        "Your package has been delivered",
        "Your order has been canceled",
    ],
    "walmart": [
        "Thanks for your delivery order",
        "Thanks for your order",
        "Shipped:",
        "Arrived: Your",
        "Canceled: delivery",
        "Your delivery was canceled",
    ],
    "target": [
        "Thanks for shopping with us",
        "are about to ship",
        "Items have arrived from order",
        "we had to cancel order",
    ],
    "ebay": [
        "You made the sale for",
        "You sold:",
        "Congratulations, you sold",
        "Great news - you made a sale",
        "Great news\xe2\x80\x94your item has sold",  # em-dash variant in body header
        "eBay shipping label for",
        "Your item is live on eBay",
        "Your item is listed",
        "Your listing is live",
        "Congratulations, your item is listed",
        "item is now available on eBay",
    ],
    "stockx": [
        "Your Ask Is Live",
        "Your Ask is now live",
        "You Sold Your Item",
    ],
    "five_below": [
        "is on the way",
        "your order is confirmed",
        "your order has shipped",
        "your order has been canceled",  # plain-ASCII subject — searchable
    ],
    "nike": [
        "We just received your order",
        "Your order has shipped",
        "Your Nike gear has shipped",
        "Delivery confirmed",
        "Your order has been delivered",
        "Your order has been cancelled",
        "We had to cancel your order",
    ],
    "bestbuy": [
        "Thanks for your order",
        "Your package is on its way",
        "Your package has been delivered",
        "Your order has been cancelled",
    ],
    "amazon": [
        "Ordered:",
        "Shipped:",
        "Delivered:",
        "Your package was shipped",
        "Item cancelled successfully",
        "order has been cancelled",
        "order has been canceled",
        "Payment declined",
        "payment couldn't be completed",
        "Your Amazon.com order",
        "Approval required",
    ],
    "shopify": [
        "shipment from order",
        "is on the way",
        "has been delivered",
        "order confirmed",
    ],
}

# Retailers whose subjects are RFC-2047 encoded (non-ASCII / base64) and therefore
# can't be matched by IMAP SUBJECT search.  Use FROM domain instead.
RETAILER_IMAP_FROM = {
    "five_below": "fivebelow.com",
    "topps":      "Topps",             # FROM display name — Shopify store
    "amazon":     "amazon.com",        # FROM-based search is most reliable for Amazon
    "bestbuy":    "bestbuy.com",       # subject "Thanks for your order" is too generic for SUBJECT search
    "shopify":    "shopifyemail.com",   # catch-all for any Shopify store
}

RETAILER_SUBJECTS = {
    "pokemon_center": [
        "pokemoncenter.com", "pokemon center", "pokemoncenter.narvar.com",
        "your package has been delivered", "on its way",
    ],
    "walmart": [
        "walmart.com", "help@walmart.com", "thanks for your delivery order",
        "thanks for your order", "shipped:", "arrived: your",
        "canceled: delivery", "your delivery was canceled", "your package shipped",
    ],
    "target": [
        "thanks for shopping with us", "are about to ship",
        "items have arrived from order", "we had to cancel order",
    ],
    "ebay": [
        "you made the sale for", "you sold:", "congratulations, you sold",
        "great news - you made a sale", "ebay shipping label for",
        "your item is live on ebay", "your item is listed", "your listing is live",
        "congratulations, your item is listed", "item is now available on ebay",
    ],
    "stockx": [
        "your ask is live",
        "your ask is now live",
        "you sold your item",
    ],
    "five_below": [
        "fivebelow.com", "five below", "o.fivebelow.com",
    ],
    "topps": [
        "official.topps.com", "shop.topps.com",
    ],
    "nike": [
        "we just received your order", "nike.com",
        "notifications.nike.com", "nike gear has shipped",
    ],
    "bestbuy": [
        "bestbuy", "bestbuyinfo", "emailinfo.bestbuy.com",
        "thanks for your order", "your package has been delivered",
    ],
    "amazon": [
        "amazon.com", "@amazon", "shipped:", "payment declined",
        "item cancelled", "order has been cancel",
    ],
}

MAILBOXES = [
    "INBOX",
    '"[Gmail]/Spam"',
    '"[Gmail]/All Mail"',
]

_AMAZON_FROM      = ["amazon.com", "amazon.", "@amazon"]
_WALMART_FROM     = ["walmart.com", "walmart", "help@walmart"]
_EBAY_FROM        = ["ebay.com", "@ebay", "ebay@"]
_STOCKX_FROM      = ["stockx.com", "@stockx", "noreply@stockx"]
_FIVE_BELOW_FROM  = ["fivebelow.com", "@fivebelow", "o.fivebelow.com"]
_TOPPS_FROM       = ["topps", "official.topps.com", "shop.topps.com"]
_NIKE_FROM        = ["nike", "notifications.nike.com", "ship.notifications.nike.com"]
_BESTBUY_FROM     = ["bestbuy", "bestbuyinfo", "emailinfo.bestbuy.com"]
_WALMART_UNAMBIGUOUS = ["arrived: your", "thanks for your delivery order", "canceled: delivery"]

# ordered=1, cancelled=2, shipped=3, delivered=4 — cancelled overwrites ordered but not shipped/delivered
_STATUS_PRIORITY = {"ordered": 1, "cancelled": 2, "shipped": 3, "delivered": 4}

_PARSER_STATUS_MAP = {
    "Pending":   "ordered",
    "Shipped":   "shipped",
    "Delivered": "delivered",
    "Canceled":  "cancelled",
}

DEFAULT_DAYS_BACK = 60  # used on first-ever sync

import sys as _sys
_STATE_FILE = os.path.join(
    os.path.dirname(_sys.executable) if getattr(_sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__)),
    "sync_state.json"
)

imaplib._MAXLINE = 10_000_000


# ── Sync state ────────────────────────────────────────────────────────────────

def _load_state() -> Dict:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_sync": None, "synced_ids": []}


def _save_state(state: Dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[EmailSync] Could not save state: {e}")


# ── IMAP ──────────────────────────────────────────────────────────────────────

def _decode_str(value) -> str:
    if value is None:
        return ""
    parts = decode_header(value)
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return " ".join(out)


def _get_body(msg) -> tuple:
    """Return (html_or_text, plain_text_or_None)."""
    html = text = None
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/html" and html is None:
                html = decoded
            elif ct == "text/plain" and text is None:
                text = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                text = decoded
    return (html or text or "", text)


def _identify_retailer(subject: str, from_addr: str) -> Optional[str]:
    sl = subject.lower()
    fl = from_addr.lower()

    if any(s in fl for s in _AMAZON_FROM):
        return "amazon"

    if any(kw.lower() in sl for kw in RETAILER_SUBJECTS["walmart"]):
        if any(u in sl for u in _WALMART_UNAMBIGUOUS):
            return "walmart"
        if any(s in fl for s in _WALMART_FROM):
            return "walmart"

    if any(kw.lower() in sl for kw in RETAILER_SUBJECTS["target"]):
        if not any(s in fl for s in _AMAZON_FROM):
            if not any(s in fl for s in _WALMART_FROM):
                return "target"

    # StockX: subject is unambiguous enough to not need from-check
    # (also catches forwarded StockX emails where from is the user's address)
    if any(kw in sl for kw in RETAILER_SUBJECTS["stockx"]):
        return "stockx"

    # eBay: subject or from check
    if any(kw in sl for kw in RETAILER_SUBJECTS["ebay"]):
        if any(s in fl for s in _EBAY_FROM) or any(kw in sl for kw in RETAILER_SUBJECTS["ebay"]):
            return "ebay"

    # Pokemon Center: require "pokemon" in the subject or from address —
    # generic subjects like "on its way" / "your package has been delivered"
    # match too many other senders otherwise.
    _is_pokemon = "pokemon" in sl or "pokemon" in fl or "narvar" in fl
    if _is_pokemon:
        for kw in RETAILER_SUBJECTS.get("pokemon_center", []):
            if kw in sl or kw in fl:
                return "pokemon_center"

    # Five Below: identify by from-domain (subjects are personalized / non-ASCII)
    if any(s in fl for s in _FIVE_BELOW_FROM):
        return "five_below"

    # Best Buy: from or subject contains "bestbuy"
    if any(s in fl for s in _BESTBUY_FROM):
        return "bestbuy"

    # Nike: subject or from contains "nike"
    if any(s in fl for s in _NIKE_FROM):
        return "nike"
    if any(kw in sl for kw in RETAILER_SUBJECTS["nike"]):
        if "nike" in fl or "nike" in sl:
            return "nike"

    # Topps: Shopify store — FROM display name contains "topps"
    if any(s in fl for s in _TOPPS_FROM):
        return "topps"

    # Generic Shopify: catch-all for any Shopify-powered store not matched above
    if "shopifyemail.com" in fl:
        return "shopify"

    return None


def _build_subject_or(subjects: List[str]) -> str:
    """Build a balanced nested IMAP OR expression for a list of subjects."""
    if len(subjects) == 1:
        return f'SUBJECT "{subjects[0]}"'
    mid = len(subjects) // 2
    left  = _build_subject_or(subjects[:mid])
    right = _build_subject_or(subjects[mid:])
    return f'OR ({left}) ({right})'


def _scan_account(mail, since_date: str, skip_ids: set,
                  subject_or: str, seen: Dict[str, Dict]) -> None:
    """Scan all MAILBOXES on an already-logged-in IMAP connection, adding to `seen`."""
    _CHUNK = 50

    for mailbox in MAILBOXES:
        try:
            status, _ = mail.select(mailbox, readonly=True)
            if status != "OK":
                continue

            query = f'(SINCE "{since_date}" {subject_or})'
            status, ids = mail.search(None, query)
            if status != "OK" or not ids[0]:
                continue

            email_ids = ids[0].split()
            if not email_ids:
                continue

            # Phase 1 — batch-fetch only the Message-ID header to cheaply
            # identify which emails are already synced.
            new_eids: List[bytes] = []
            for i in range(0, len(email_ids), _CHUNK):
                chunk  = email_ids[i:i + _CHUNK]
                id_set = b",".join(chunk).decode()
                hs, hdata = mail.fetch(id_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if hs != "OK" or not hdata:
                    new_eids.extend(chunk)   # fallback: process all
                    continue

                seq_to_msgid: Dict[str, str] = {}
                for hitem in hdata:
                    if not isinstance(hitem, tuple):
                        continue
                    try:
                        seq = hitem[0].decode().split()[0]
                    except Exception:
                        continue
                    hdr    = email_lib.message_from_bytes(hitem[1])
                    msg_id = hdr.get("Message-ID", "").strip()
                    if msg_id:
                        seq_to_msgid[seq] = msg_id

                for eid in chunk:
                    seq  = eid.decode()
                    key  = seq_to_msgid.get(seq, seq)
                    if key not in skip_ids and key not in seen:
                        new_eids.append(eid)

            if not new_eids:
                continue

            # Phase 2 — fetch full body only for emails we haven't seen before.
            for eid in new_eids:
                status, data = mail.fetch(eid, "(BODY[])")
                if status != "OK" or not data or not data[0]:
                    status, data = mail.fetch(eid, "(RFC822)")
                    if status != "OK":
                        continue

                raw = data[0][1]
                if not isinstance(raw, bytes):
                    continue

                msg = email_lib.message_from_bytes(raw)
                msg_id = msg.get("Message-ID", "").strip()
                key    = msg_id if msg_id else eid.decode()
                if key in seen:
                    continue

                subject   = _decode_str(msg.get("Subject", ""))
                from_addr = _decode_str(msg.get("From", ""))
                retailer  = _identify_retailer(subject, from_addr)
                if not retailer:
                    continue

                # Use To: (original bot account the retailer sent to).
                # Delivered-To is the forwarding inbox — we don't want that.
                raw_to = msg.get("To") or msg.get("Delivered-To") or ""
                account_email = _decode_str(raw_to).strip()
                # Strip display name if present, e.g. "John <john@gmail.com>"
                import re as _re
                m = _re.search(r'[\w.+\-]+@[\w.\-]+', account_email)
                account_email = m.group(0).lower() if m else ""

                # Extract date from email Date header → YYYY-MM-DD
                _email_date = None
                _raw_date = msg.get("Date")
                if _raw_date:
                    try:
                        from email.utils import parsedate_to_datetime
                        _email_date = parsedate_to_datetime(_raw_date).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                body_html, body_text = _get_body(msg)
                seen[key] = {
                    "message_id":    key,
                    "email_id":      eid.decode(),
                    "retailer":      retailer,
                    "subject":       subject,
                    "body":          body_html,
                    "body_text":     body_text,
                    "from_addr":     from_addr,
                    "email_date":    _email_date,
                    "account_email": account_email,
                }

        except Exception as e:
            print(f"[EmailSync] Mailbox {mailbox} error: {e}")


def _fetch_emails(since_date: str, skip_ids: set = None) -> List[Dict]:
    accounts = config.IMAP_ACCOUNTS
    if not accounts:
        print("[EmailSync] No IMAP accounts configured — skipping.")
        return []

    skip_ids = skip_ids or set()
    all_subjects = [
        sf for sfs in RETAILER_SUBJECTS_IMAP.values() for sf in sfs
        if sf.isascii()
    ]
    subject_or = _build_subject_or(all_subjects)
    seen: Dict[str, Dict] = {}

    for account in accounts:
        user = account.get("user", "")
        pw   = account.get("pass", "")
        if not user or not pw:
            continue
        label = account.get("label") or user
        host  = account.get("host") or config.IMAP_HOST
        port  = account.get("port") or config.IMAP_PORT
        print(f"[EmailSync] Connecting ({label} — {user} @ {host})...")
        try:
            mail = imaplib.IMAP4_SSL(host, port)
            mail.login(user, pw)
        except Exception as e:
            print(f"[EmailSync] IMAP login failed for {user}: {e}")
            continue
        _scan_account(mail, since_date, skip_ids, subject_or, seen)
        mail.logout()

    result = list(seen.values())
    print(f"[EmailSync] Fetched {len(result)} unique emails across {len(accounts)} account(s).")
    return result


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_order_by_number(order_number: str) -> Optional[Dict]:
    with db._conn() as con:
        row = con.execute(
            "SELECT * FROM inbound_orders WHERE order_number = ? AND is_deleted = 0",
            (order_number,),
        ).fetchone()
        return dict(row) if row else None


def _item_exists(order_id: int, item_name: str) -> bool:
    """Return True if a sufficiently similar item already exists for this order."""
    def _words(s: str):
        return set(re.sub(r'[^a-z0-9]', ' ', s.lower()).split())

    with db._conn() as con:
        rows = con.execute(
            "SELECT item_name FROM inbound_order_items WHERE order_id = ? AND is_deleted = 0",
            (order_id,)
        ).fetchall()

    target = _words(item_name)
    for row in rows:
        existing = _words(row[0])
        union = target | existing
        if not union:
            continue
        if len(target & existing) / len(union) >= 0.6:
            return True
    return False


def _patch_item_cost_if_zero(order_id: int, item_name: str, cost_cents: int) -> None:
    """Update cost_cents for the first item in this order with cost=0 whose name fuzzy-matches."""
    def _words(s: str):
        return set(re.sub(r'[^a-z0-9]', ' ', s.lower()).split())

    target = _words(item_name)
    with db._conn() as con:
        rows = con.execute(
            "SELECT id, item_name, cost_cents FROM inbound_order_items WHERE order_id = ? AND is_deleted = 0",
            (order_id,)
        ).fetchall()
        for row in rows:
            existing = _words(row["item_name"])
            union = target | existing
            if not union:
                continue
            if len(target & existing) / len(union) >= 0.6 and row["cost_cents"] == 0:
                con.execute(
                    "UPDATE inbound_order_items SET cost_cents = ? WHERE id = ?",
                    (cost_cents, row["id"])
                )
                con.commit()
                break


def _insert_item(order_id: int, item: Dict, cost_cents: int) -> int:
    """Insert item and return its new id."""
    with db._conn() as con:
        cur = con.execute(
            "INSERT INTO inbound_order_items (order_id, item_name, sku, cost_cents, quantity) VALUES (?,?,?,?,?)",
            (order_id, item["name"], item.get("sku"), cost_cents, item.get("quantity") or 1),
        )
        return cur.lastrowid


def _maybe_upgrade_status(order_id: int, new_status: str, current_status: str,
                           has_tracking: bool = False) -> None:
    if _STATUS_PRIORITY.get(new_status, 0) > _STATUS_PRIORITY.get(current_status, 0):
        db.update_inbound_order(order_id, "status", new_status)


# ── Save parsed result ────────────────────────────────────────────────────────

def _cost_cents(item: Dict, order_total: Optional[float], total_qty: int) -> int:
    qty = item.get("quantity") or 1
    if order_total and total_qty:
        # order_total includes tax — distribute evenly across every unit in the order.
        return db.dollars_to_cents(order_total / total_qty)
    price = item.get("price")
    if price is not None:
        # Parsers return the line total (price × qty), so divide to get per-unit cost.
        # No tax in this path — only used when order_total is unavailable.
        return db.dollars_to_cents(price / qty)
    return 0


_DATE_FORMATS = [
    "%Y-%m-%d",
    "%a, %b %d, %Y",   # Thu, Mar 26, 2026
    "%A, %B %d, %Y",   # Thursday, March 26, 2026
    "%B %d, %Y",       # March 26, 2026
    "%b %d, %Y",       # Mar 26, 2026
    "%b. %d, %Y",      # Mar. 26, 2026
    "%m/%d/%Y",        # 03/26/2026
    "%m/%d/%y",        # 03/26/26
]


def _normalize_date(raw: Optional[str]) -> str:
    """Parse any retailer date string and return YYYY-MM-DD, or today as fallback."""
    if not raw:
        return datetime.now().strftime("%Y-%m-%d")
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.now().strftime("%Y-%m-%d")


def _fire_payment_issue_webhook(parsed: Dict) -> None:
    """Send a Discord webhook if the parser flagged a payment issue."""
    if not parsed.get("_payment_issue"):
        return
    item_names = ", ".join(
        i.get("name", "Unknown item") for i in (parsed.get("items") or [])
    ) or "Unknown item"
    account_email = parsed.get("account_email") or "Unknown account"
    webhooks.notify_amazon_payment_issue(
        order_number  = parsed.get("order_number", "N/A"),
        item_name     = item_names,
        account_email = account_email,
    )


def _save_parsed(parsed: Dict) -> str:
    """Returns 'imported' | 'updated' | 'skipped'."""
    order_number = parsed.get("order_number")
    if not order_number:
        return "skipped"

    retailer      = parsed["retailer"]
    order_date    = _normalize_date(parsed.get("order_date"))
    tracking      = parsed.get("tracking_number")
    address       = parsed.get("shipping_address")
    account_email = parsed.get("account_email") or None
    db_status     = _PARSER_STATUS_MAP.get(parsed.get("status", "Pending"), "ordered")
    items       = parsed.get("items") or []
    order_total = parsed.get("order_total")
    # Fall back to subtotal + tax when order_total wasn't found in the email
    if not order_total:
        subtotal = parsed.get("subtotal") or 0
        tax      = parsed.get("tax") or 0
        if subtotal:
            order_total = subtotal + tax
    total_qty   = sum(i.get("quantity") or 1 for i in items) if items else 1

    existing = _get_order_by_number(order_number)

    if existing:
        order_id = existing["id"]
        if tracking and not existing.get("tracking_number"):
            db.update_inbound_order(order_id, "tracking_number", tracking)
        if address and not existing.get("delivery_address"):
            db.update_inbound_order(order_id, "delivery_address", address)
        if account_email and not existing.get("account_email"):
            db.update_inbound_order(order_id, "account_email", account_email)

        # If this email has an earlier date than the stored one (confirmation
        # email arriving after a shipping email), update to the earlier date.
        if order_date and order_date < (existing.get("order_date") or "9999"):
            db.update_inbound_order(order_id, "order_date", order_date)

        has_tracking = bool(existing.get("tracking_number") or tracking)
        _maybe_upgrade_status(order_id, db_status, existing.get("status", "ordered"),
                              has_tracking=has_tracking)

        # If the existing order only has a placeholder item ("Retailer Order #...")
        # and we now have real item names, delete the placeholder first.
        if items and db_status == "ordered":
            with db._conn() as con:
                existing_items = con.execute(
                    "SELECT id, item_name FROM inbound_order_items "
                    "WHERE order_id = ? AND is_deleted = 0", (order_id,)
                ).fetchall()
                if len(existing_items) == 1:
                    ei_name = existing_items[0]["item_name"]
                    if " Order " in ei_name and ei_name.endswith(order_number):
                        # It's a placeholder — remove it so real items can be inserted
                        con.execute(
                            "UPDATE inbound_order_items SET is_deleted = 1 WHERE id = ?",
                            (existing_items[0]["id"],)
                        )

        # Patch item costs: always try when we have cost data and existing
        # items have zero cost, regardless of which email type this is.
        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            cost = _cost_cents(item, order_total, total_qty)
            if _item_exists(order_id, name):
                if cost:
                    _patch_item_cost_if_zero(order_id, name, cost)
                continue
            _insert_item(order_id, item, cost)

        _fire_payment_issue_webhook(parsed)
        return "updated"

    # New order
    if not items:
        items = [{
            "name":     f"{retailer.replace('_', ' ').title()} Order {order_number}",
            "sku":      None,
            "quantity": 1,
            "price":    order_total,
        }]
        total_qty = 1

    first = True
    for item in items:
        name = (item.get("name") or f"Item from {order_number}").strip()
        db.add_inbound_order(
            order_number     = order_number,
            retailer         = retailer,
            order_date       = order_date,
            item_name        = name,
            cost_cents       = _cost_cents(item, order_total, total_qty),
            quantity         = item.get("quantity") or 1,
            sku              = item.get("sku"),
            tracking_number  = tracking       if first else None,
            delivery_address = address        if first else None,
            account_email    = account_email  if first else None,
            status           = db_status,
        )
        first = False

    _fire_payment_issue_webhook(parsed)
    return "imported"


# ── Save eBay sale ────────────────────────────────────────────────────────────

def _save_ebay_sale(parsed: Dict) -> str:
    """Returns 'imported' | 'updated' | 'skipped'."""
    platform   = parsed.get("platform", "ebay")
    order_id   = parsed.get("order_number")
    item_name  = parsed.get("item_name") or f"{platform.title()} Sale {order_id or 'Unknown'}"
    sale_price = parsed.get("sale_price") or 0
    # platform_fee covers StockX (transaction+proc+shipping); ebay_fee covers eBay
    ebay_fee   = parsed.get("platform_fee") or parsed.get("ebay_fee") or 0

    # If already exists, patch any fields that were previously missing
    if order_id:
        existing = db.get_sale_by_order_id(order_id)
        if existing:
            updated = False
            if sale_price and existing.get("sale_price_cents", 0) == 0:
                db.update_sale(existing["id"], "sale_price_cents", db.dollars_to_cents(sale_price))
                updated = True
            if parsed.get("buyer") and not existing.get("buyer_info"):
                db.update_sale(existing["id"], "buyer_info", parsed["buyer"])
                updated = True
            if parsed.get("date_sold") and not existing.get("date_sold"):
                db.update_sale(existing["id"], "date_sold", _normalize_date(parsed["date_sold"]))
                updated = True
            return "updated" if updated else "skipped"

    qty = parsed.get("quantity") or 1

    # shipping_cost_cents is the seller's postage cost, unknown until the
    # shipping label email arrives — left 0 here, updated by _update_ebay_sale_shipped.
    sale_id = db.add_sale_from_email(
        item_name          = item_name,
        platform           = platform,
        sale_price_cents   = db.dollars_to_cents(sale_price),
        platform_fees_cents= db.dollars_to_cents(ebay_fee),
        shipping_cost_cents= 0,
        date_sold          = _normalize_date(parsed.get("date_sold")),
        buyer_info         = parsed.get("buyer"),
        tracking_number    = parsed.get("tracking_number"),
        source_order_id    = order_id,
        quantity           = qty,
    )

    # If this item had an active listing, mark it sold and link inventory
    listing = db.find_active_listing_for_sale(item_name, platform)
    if listing:
        db.mark_listing_sold(listing["id"], date_sold=_normalize_date(parsed.get("date_sold")))
        if listing.get("inventory_id"):
            db.link_sale_to_inventory(sale_id, listing["inventory_id"])
        print(f"[EmailSync] Listing #{listing['id']} → sold (matched sale '{item_name}')")

    return "imported"


# ── Update eBay sale from shipping label email ────────────────────────────────

def _update_ebay_sale_shipped(parsed: Dict) -> str:
    """Returns 'updated' | 'skipped'."""
    order_id  = parsed.get("order_number")
    item_name = parsed.get("item_name")
    tracking  = parsed.get("tracking_number")
    label_cost = parsed.get("label_cost") or 0
    shipping_cost_cents = db.dollars_to_cents(label_cost) if label_cost else 0

    updated = False

    # Primary: match by order number (source_order_id)
    if order_id:
        updated = db.update_sale_shipped_by_order_id(
            source_order_id     = order_id,
            tracking_number     = tracking,
            shipping_cost_cents = shipping_cost_cents,
        )

    # Fallback: match by item name (eBay label emails often omit the order number)
    if not updated and item_name:
        sale = db.find_unshipped_sale_by_item_name(item_name)
        if sale:
            updated = db.update_sale_shipped_by_sale_id(
                sale_id             = sale["id"],
                tracking_number     = tracking,
                shipping_cost_cents = shipping_cost_cents,
            )
            if updated:
                print(
                    f"[EmailSync] Matched label by item name: '{item_name}' "
                    f"-> sale_id={sale['id']} | tracking={tracking} | label_cost=${label_cost}"
                )

    if updated:
        return "updated"
    else:
        ref = order_id or item_name or "unknown"
        print(f"[EmailSync] Label email for '{ref}' — no matching unshipped sale found (skipped).")
        return "skipped"


# ── Save eBay listing ─────────────────────────────────────────────────────────

def _save_listing(parsed: Dict) -> str:
    """Returns 'imported' | 'skipped'."""
    email_id  = parsed.get("email_id")
    item_name = parsed.get("item_name")
    if not item_name:
        return "skipped"
    if email_id and db.listing_exists_by_email_id(email_id):
        return "skipped"
    price   = parsed.get("listing_price") or 0
    payout  = parsed.get("total_payout") or 0
    db.add_listing(
        item_name               = item_name,
        platform                = parsed.get("platform", "unknown"),
        listing_price_cents     = db.dollars_to_cents(price),
        listing_id              = parsed.get("listing_id") or parsed.get("style_id"),
        source                  = "email",
        source_email_id         = email_id,
        estimated_payout_cents  = db.dollars_to_cents(payout) if payout else None,
        size_variant            = parsed.get("size"),
    )
    return "imported"


# ── Public entry point ────────────────────────────────────────────────────────

def run_scrape(
    retailer: str,
    days_back: int,
    progress_callback: Optional[Callable[[str], None]] = None,
    accounts: Optional[List[Dict]] = None,
) -> Dict:
    """
    Manual scrape: fetch emails for a single retailer going back `days_back` days.
    Does NOT update sync state — purely additive, re-processes already-seen IDs too.
    `accounts` is an optional subset of config.IMAP_ACCOUNTS to scan; None = all.
    """
    def _log(msg: str) -> None:
        print(f"[ManualScrape] {msg}")
        if progress_callback:
            progress_callback(msg)

    if retailer not in RETAILER_PARSERS:
        _log(f"Unknown retailer: {retailer}")
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 1}

    since_dt   = datetime.now() - timedelta(days=days_back)
    since_date = since_dt.strftime("%d-%b-%Y")
    _log(f"Scraping {retailer} since {since_date} ({days_back} days back)…")

    # Build the IMAP search filter for the chosen retailer.
    # Prefer FROM-based search for retailers whose subjects are RFC-2047 encoded
    # (base64/QP) — IMAP SUBJECT search operates on raw headers and won't decode them.
    imap_from  = RETAILER_IMAP_FROM.get(retailer)
    subjects   = RETAILER_SUBJECTS_IMAP.get(retailer, [])
    if not imap_from and not subjects:
        _log(f"No subject filters defined for {retailer}")
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 1}

    if imap_from:
        imap_filter = f'FROM "{imap_from}"'
    else:
        imap_filter = _build_subject_or([s for s in subjects if s.isascii()])

    accounts_to_scan = accounts if accounts is not None else config.IMAP_ACCOUNTS
    if not accounts_to_scan:
        _log("No IMAP accounts configured — skipping.")
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 1}

    seen: Dict[str, Dict] = {}
    _CHUNK = 50

    for account in accounts_to_scan:
        acct_user = account.get("user", "")
        acct_pass = account.get("pass", "")
        if not acct_user or not acct_pass:
            continue
        acct_label = account.get("label") or acct_user
        acct_host  = account.get("host") or config.IMAP_HOST
        acct_port  = account.get("port") or config.IMAP_PORT
        _log(f"Connecting ({acct_label} — {acct_user} @ {acct_host})...")
        try:
            mail = imaplib.IMAP4_SSL(acct_host, acct_port)
            mail.login(acct_user, acct_pass)
        except Exception as e:
            _log(f"IMAP login failed for {acct_user}: {e}")
            continue

        for mailbox in MAILBOXES:
            try:
                status, _ = mail.select(mailbox, readonly=True)
                if status != "OK":
                    continue
                query = f'(SINCE "{since_date}" {imap_filter})'
                status, ids = mail.search(None, query)
                if status != "OK" or not ids[0]:
                    continue
                email_ids = ids[0].split()
                if not email_ids:
                    continue

                for i in range(0, len(email_ids), _CHUNK):
                    chunk  = email_ids[i:i + _CHUNK]
                    id_set = b",".join(chunk).decode()
                    hs, hdata = mail.fetch(id_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    if hs != "OK" or not hdata:
                        new_eids = list(chunk)
                    else:
                        seq_to_msgid: Dict[str, str] = {}
                        for hitem in hdata:
                            if not isinstance(hitem, tuple):
                                continue
                            try:
                                seq = hitem[0].decode().split()[0]
                            except Exception:
                                continue
                            hdr    = email_lib.message_from_bytes(hitem[1])
                            msg_id = hdr.get("Message-ID", "").strip()
                            if msg_id:
                                seq_to_msgid[seq] = msg_id
                        new_eids = []
                        for eid in chunk:
                            seq = eid.decode()
                            key = seq_to_msgid.get(seq, seq)
                            if key not in seen:
                                new_eids.append(eid)

                    for eid in new_eids:
                        status2, data = mail.fetch(eid, "(BODY[])")
                        if status2 != "OK" or not data or not data[0]:
                            continue
                        try:
                            raw       = data[0][1]
                            msg       = email_lib.message_from_bytes(raw)
                            msg_id    = (msg.get("Message-ID") or eid.decode()).strip()
                            subject   = _decode_str(msg.get("Subject", ""))
                            from_addr = _decode_str(msg.get("From", ""))
                            detected  = _identify_retailer(subject, from_addr)
                            if detected != retailer:
                                continue
                            body_html, body_text = _get_body(msg)
                            raw_to    = msg.get("To") or msg.get("Delivered-To") or ""
                            acc_email_raw = _decode_str(raw_to).strip()
                            m = re.search(r'[\w.+\-]+@[\w.\-]+', acc_email_raw)
                            account_email = m.group(0).lower() if m else ""
                            _email_date = None
                            _raw_date = msg.get("Date")
                            if _raw_date:
                                try:
                                    from email.utils import parsedate_to_datetime
                                    _email_date = parsedate_to_datetime(_raw_date).strftime("%Y-%m-%d")
                                except Exception:
                                    pass
                            seen[msg_id] = {
                                "email_id":      eid.decode(),
                                "message_id":    msg_id,
                                "subject":       subject,
                                "body":          body_html,
                                "body_text":     body_text,
                                "from_addr":     from_addr,
                                "email_date":    _email_date,
                                "retailer":      detected,
                                "account_email": account_email,
                            }
                        except Exception as ex:
                            print(f"[ManualScrape] header parse error: {ex}")
            except Exception as ex:
                print(f"[ManualScrape] mailbox error: {ex}")

        mail.logout()

    parser_mod = RETAILER_PARSERS[retailer]
    counts     = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    # Process oldest emails first so confirmation emails (with prices + correct
    # dates) are imported before shipping emails (no prices, later dates).
    sorted_entries = sorted(seen.values(), key=lambda e: e.get("email_date") or "9999")

    for entry in sorted_entries:
        try:
            parsed = parser_mod.parse({
                "email_id":   entry["email_id"],
                "subject":    entry["subject"],
                "body":       entry["body"],
                "body_text":  entry.get("body_text"),
                "from_addr":  entry.get("from_addr", ""),
                "email_date": entry.get("email_date"),
            })
            if parsed is None:
                counts["skipped"] += 1
            elif retailer in SALE_PARSERS:
                etype = parsed.get("email_type")
                if etype == "label":
                    outcome = _update_ebay_sale_shipped(parsed)
                elif etype == "listing":
                    outcome = _save_listing(parsed)
                else:
                    outcome = _save_ebay_sale(parsed)
                counts[outcome] = counts.get(outcome, 0) + 1
            elif retailer in LISTING_PARSERS:
                outcome = _save_listing(parsed)
                counts[outcome] = counts.get(outcome, 0) + 1
            else:
                parsed["account_email"] = entry.get("account_email") or None
                outcome = _save_parsed(parsed)
                counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as e:
            print(f"[ManualScrape] parse error ({entry['subject'][:50]}): {e}")
            counts["errors"] += 1

    _log(
        f"Scrape complete — "
        f"{counts['imported']} imported, {counts['updated']} updated, "
        f"{counts['skipped']} skipped, {counts['errors']} errors"
    )
    return counts


def run_sync(progress_callback: Optional[Callable[[str], None]] = None) -> Dict:
    """
    Run a full email sync and return result counts.
    progress_callback(msg) is called with human-readable status updates.
    """
    def _log(msg: str) -> None:
        print(f"[EmailSync] {msg}")
        if progress_callback:
            progress_callback(msg)

    state      = _load_state()
    synced_ids = set(state.get("synced_ids", []))

    last_sync_str = state.get("last_sync")
    if last_sync_str:
        try:
            since_dt = datetime.fromisoformat(last_sync_str) - timedelta(days=1)
        except Exception:
            since_dt = datetime.now() - timedelta(days=DEFAULT_DAYS_BACK)
    else:
        since_dt = datetime.now() - timedelta(days=DEFAULT_DAYS_BACK)

    since_date = since_dt.strftime("%d-%b-%Y")
    _log(f"Scanning emails since {since_date}...")

    try:
        emails = _fetch_emails(since_date, skip_ids=synced_ids)
    except Exception as e:
        _log(f"Fetch error: {e}")
        return {"imported": 0, "updated": 0, "skipped": 0, "errors": 1}

    counts = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

    # Process oldest emails first so confirmations (with prices) come before
    # shipping notifications (no prices, later dates).
    emails.sort(key=lambda e: e.get("email_date") or "9999")

    for entry in emails:
        msg_id  = entry["message_id"]
        if msg_id in synced_ids:
            counts["skipped"] += 1
            continue

        parser_mod = RETAILER_PARSERS.get(entry["retailer"])
        if not parser_mod:
            counts["skipped"] += 1
            synced_ids.add(msg_id)
            continue

        try:
            parsed = parser_mod.parse({
                "email_id":   entry["email_id"],
                "subject":    entry["subject"],
                "body":       entry["body"],
                "body_text":  entry.get("body_text"),
                "from_addr":  entry.get("from_addr", ""),
                "email_date": entry.get("email_date"),
            })
            if parsed is None:
                counts["skipped"] += 1
            elif entry["retailer"] in LISTING_PARSERS:
                outcome = _save_listing(parsed)
                counts[outcome] = counts.get(outcome, 0) + 1
            elif entry["retailer"] in SALE_PARSERS:
                etype = parsed.get("email_type")
                if etype == "label":
                    outcome = _update_ebay_sale_shipped(parsed)
                elif etype == "listing":
                    outcome = _save_listing(parsed)
                else:
                    outcome = _save_ebay_sale(parsed)
                counts[outcome] = counts.get(outcome, 0) + 1
            else:
                parsed["account_email"] = entry.get("account_email") or None
                outcome = _save_parsed(parsed)
                counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as e:
            print(f"[EmailSync] Parse error ({entry['subject'][:50]}): {e}")
            counts["errors"] += 1

        synced_ids.add(msg_id)

    state["last_sync"] = datetime.now().isoformat()
    state["synced_ids"] = list(synced_ids)
    _save_state(state)

    _log(
        f"Sync complete — "
        f"{counts['imported']} imported, {counts['updated']} updated, "
        f"{counts['skipped']} skipped, {counts['errors']} errors"
    )
    return counts
