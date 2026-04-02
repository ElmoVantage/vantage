"""
Best Buy order email parser.

Subject patterns:
  - "Thanks for your order"                → Pending
  - "Your package is on its way"           → Shipped
  - "Your package has been delivered"      → Delivered
  - "Your order has been cancelled"        → Canceled

Data extracted:
  order_number (BBY01-...), items [{name, sku, quantity, price}],
  subtotal, tax, order_total, shipping_address, tracking_number, status
"""

import re
from bs4 import BeautifulSoup


def _dollars(s: str):
    if not s:
        return None
    m = re.search(r'\$([0-9,]+\.?\d*)', s)
    return float(m.group(1).replace(",", "")) if m else None


def parse(email_data: dict):
    subject   = email_data.get("subject", "")
    body      = email_data.get("body", "")
    email_id  = email_data.get("email_id")
    email_date = email_data.get("email_date")

    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(separator="\n")
    sl   = subject.lower()

    # ── Status ─────────────────────────────────────────────────────────
    if "cancel" in sl:
        status = "Canceled"
    elif "delivered" in sl:
        status = "Delivered"
    elif "on its way" in sl or "shipped" in sl:
        status = "Shipped"
    else:
        status = "Pending"

    result = {
        "retailer":         "bestbuy",
        "email_id":         email_id,
        "order_number":     None,
        "order_date":       email_date,
        "subtotal":         None,
        "tax":              None,
        "shipping":         None,
        "order_total":      None,
        "shipping_address": None,
        "tracking_number":  None,
        "status":           status,
        "items":            [],
    }

    # ── Order number ───────────────────────────────────────────────────
    # "Order number: BBY01-807159434048"
    m = re.search(r'Order\s+number:\s*(BBY\d+-\d+)', text, re.IGNORECASE)
    if m:
        result["order_number"] = m.group(1)
    if not result["order_number"]:
        m = re.search(r'\b(BBY\d+-\d{9,})\b', text)
        if m:
            result["order_number"] = m.group(1)

    # ── Tracking number ────────────────────────────────────────────────
    # "Tracking number: 517176624787"
    m = re.search(r'Tracking\s+number:\s*(\w{10,})', text, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1)

    # ── Items ──────────────────────────────────────────────────────────
    # Item name is in an <a> tag linking to bestbuy.com product page,
    # inside a td with max-width:359px. Price nearby in bold $XX.XX.
    # Qty in a "Qty:" label row.
    # Scan for "Product Details" sections
    for td in soup.find_all("td"):
        style = td.get("style") or ""
        if "max-width:359px" not in style and "max-width: 359px" not in style:
            continue
        # Find item name - first <a> with text > 10 chars
        name = None
        for a in td.find_all("a"):
            t = a.get_text(strip=True)
            if len(t) > 10 and "track" not in t.lower() and "view" not in t.lower():
                name = t
                break
        if not name:
            continue

        # Find price — search the item td and its parent tables for a bold $XX.XX
        price = None
        for search_el in [td] + [p for p in td.parents if p.name == "table"][:3]:
            for span in search_el.find_all("span"):
                s = span.get("style") or ""
                if ("font-weight: 700" in s or "font-weight:700" in s) and "$" in span.get_text():
                    p = _dollars(span.get_text(strip=True))
                    if p:
                        price = p
                        break
            if price:
                break
        parent_table = td.find_parent("table")

        # Find quantity
        qty = 1
        if parent_table:
            qty_text = parent_table.get_text()
            m = re.search(r'Qty:\s*(\d+)', qty_text)
            if m:
                qty = int(m.group(1))

        # Find model/sku
        sku = None
        if parent_table:
            m = re.search(r'Model\s*#?:\s*([\w-]+)', parent_table.get_text())
            if m:
                sku = m.group(1)

        result["items"].append({
            "name":     name,
            "sku":      sku,
            "quantity": qty,
            "price":    price,
        })

    # ── Order summary (totals) ─────────────────────────────────────────
    # Scan text for Subtotal, Shipping, Tax, Total with dollar amounts
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if not stripped:
            continue
        # Look for label on one line and value nearby (Best Buy HTML
        # inserts many blank lines between label and value — scan up to 10)
        if "subtotal" == stripped:
            for j in range(i+1, min(i+10, len(lines))):
                val = _dollars(lines[j].strip())
                if val is not None:
                    result["subtotal"] = val
                    break
        elif stripped.startswith("estimated sales") and "tax" in stripped:
            for j in range(i+1, min(i+10, len(lines))):
                val = _dollars(lines[j].strip())
                if val is not None:
                    result["tax"] = val
                    break
        elif stripped == "total":
            for j in range(i+1, min(i+10, len(lines))):
                val = _dollars(lines[j].strip())
                if val is not None:
                    result["order_total"] = val
                    break

    # Fallback: scan for labels and values on same line
    for line in lines:
        stripped = line.strip()
        m = re.match(r'Subtotal\s*\$([0-9,.]+)', stripped)
        if m:
            result["subtotal"] = float(m.group(1).replace(",", ""))
        m = re.match(r'Total\s*\$([0-9,.]+)', stripped)
        if m and not result["order_total"]:
            result["order_total"] = float(m.group(1).replace(",", ""))

    # ── Shipping address ───────────────────────────────────────────────
    # "Shipping to:" label followed by name and address
    m = re.search(
        r'Shipping\s+to:\s*\n\s*(.+?)(?:\n\s*){2,}',
        text, re.DOTALL
    )
    if m:
        addr = re.sub(r'\s*\n\s*', ', ', m.group(1).strip())
        result["shipping_address"] = addr

    # ── Log ────────────────────────────────────────────────────────────
    if not result["order_number"]:
        return None

    item_names = ", ".join(f"{i['quantity']}x {i['name'][:35]}" for i in result["items"]) or "no items"
    print(
        f"[Parser/BestBuy] order={result['order_number']} | status={status} | "
        f"items=[{item_names}] | total={result['order_total']}"
    )

    return result
