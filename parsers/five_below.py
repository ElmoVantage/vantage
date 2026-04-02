"""
Five Below order confirmation email parser.

Data extracted:
  order_number, status, shipping_address, order_total, subtotal,
  shipping, tax, items (name, style, quantity, line price)
"""

import re
from datetime import datetime
from bs4 import BeautifulSoup


def parse(email_data: dict):
    subject  = email_data.get("subject", "")
    body     = email_data.get("body", "")
    email_id = email_data.get("email_id")

    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(separator="\n")
    sl   = subject.lower()

    # ── Email type / status ───────────────────────────────────────────────────
    if "delivered" in sl:
        status = "Delivered"
    elif "shipped" in sl or "on the way" in sl:
        status = "Shipped"
    elif "cancel" in sl:
        status = "Canceled"
    else:
        status = "Pending"   # order confirmed / packing

    # ── Order number ──────────────────────────────────────────────────────────
    order_number = None
    for pattern in [
        r'Order\s+Number[:\s]+([A-Z0-9]+)',
        r'order\s+#\s*([A-Z0-9]+)',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            order_number = m.group(1).strip()
            break

    # Five Below order numbers start with W followed by digits
    if not order_number or not re.match(r'^W\d+', order_number):
        return None

    # ── Tracking number ───────────────────────────────────────────────────────
    # Narvar tracking URL: ...tracking_numbers=517086813728... (may be %3D encoded)
    tracking_number = None
    tn_m = re.search(r'tracking_numbers(?:%3D|=)(\d+)', body, re.IGNORECASE)
    if tn_m:
        tracking_number = tn_m.group(1)

    # ── Order date ────────────────────────────────────────────────────────────
    order_date = datetime.now().strftime("%Y-%m-%d")

    # ── Amounts ───────────────────────────────────────────────────────────────
    _AMT = re.compile(r'\$([0-9,]+\.[0-9]{2})')

    def _after(label_re: str) -> float | None:
        """Find the first $X.XX that follows `label_re` within 300 chars."""
        m = re.search(label_re + r'[\s\S]{0,300}?' + r'\$([0-9,]+\.[0-9]{2})',
                      text, re.IGNORECASE)
        return float(m.group(1).replace(',', '')) if m else None

    order_total = _after(r'Order\s+Total')

    # Subtotal/Shipping/Tax appear together; pick amounts from that block
    sub_block = re.search(
        r'Subtotal[\s\S]{0,400}?'
        r'\$([0-9,]+\.[0-9]{2})[\s\S]{0,50}?'   # subtotal amount
        r'\$([0-9,]+\.[0-9]{2})[\s\S]{0,50}?'   # shipping amount
        r'\$([0-9,]+\.[0-9]{2})',                # tax amount
        text, re.IGNORECASE,
    )
    subtotal = shipping = tax = None
    if sub_block:
        subtotal = float(sub_block.group(1).replace(',', ''))
        shipping = float(sub_block.group(2).replace(',', ''))
        tax      = float(sub_block.group(3).replace(',', ''))

    # ── Shipping address ──────────────────────────────────────────────────────
    shipping_address = None
    m = re.search(
        r'Shipping\s+Address[:\s]*\n+([^\n]+)\n+([^\n]+)\n+([^\n]+)',
        text, re.IGNORECASE,
    )
    if m:
        shipping_address = ", ".join(g.strip() for g in m.groups() if g.strip())

    # ── Items from HTML ───────────────────────────────────────────────────────
    # Product name links have font-weight:600 + text-decoration:underline.
    # Style / Qty / Price are in sibling <tr>s of the same parent <table>.
    items = []
    _NAV_NAMES = {
        "new & now", "room", "toys & games", "beauty", "tech",
        "shop by price", "unsubscribe", "view email", "privacy",
        "terms", "five below",
    }

    # Product links have BOTH font-weight:600 AND text-decoration:underline.
    # CTA buttons (Track Delivery, Check Order Status) have font-weight:600
    # but text-decoration:none — exclude them.
    _PRODUCT_LINK_RE = re.compile(
        r'(?=.*font-weight\s*:\s*600)(?=.*text-decoration\s*:\s*underline)', re.I
    )

    seen_names = set()
    for a_tag in soup.find_all("a", style=_PRODUCT_LINK_RE):
        item_name = a_tag.get_text(strip=True)
        if not item_name or len(item_name) < 10:
            continue
        if item_name.lower() in _NAV_NAMES:
            continue
        if item_name in seen_names:
            continue

        table = a_tag.find_parent("table")
        if not table:
            continue
        t_text = table.get_text(separator="\n")

        style_m = re.search(r'Style[:\s]+([^\n]+)', t_text, re.IGNORECASE)
        qty_m   = re.search(r'Qty[:\s]+(\d+)',       t_text, re.IGNORECASE)
        price_m = _AMT.search(t_text)

        style_val = style_m.group(1).strip() if style_m else None
        qty       = int(qty_m.group(1)) if qty_m else 1
        # price is the line total (may be None; order_total used as fallback)
        price     = float(price_m.group(1).replace(',', '')) if price_m else None

        full_name = f"{item_name} - {style_val}" if style_val else item_name
        seen_names.add(item_name)

        items.append({
            "name":     full_name,
            "sku":      None,
            "quantity": qty,
            "price":    price,
        })

    print(
        f"[Parser/FiveBelow] order={order_number} | {status} | "
        f"items={len(items)} | total=${order_total}"
    )

    return {
        "retailer":         "five_below",
        "email_id":         email_id,
        "email_type":       "order",
        "order_number":     order_number,
        "order_date":       order_date,
        "status":           status,
        "tracking_number":  tracking_number,
        "shipping_address": shipping_address,
        "items":            items,
        "subtotal":         subtotal,
        "tax":              tax,
        "shipping":         shipping,
        "order_total":      order_total,
    }
