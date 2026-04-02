"""
Generic Shopify order email parser — handles any Shopify-powered store.

All Shopify stores use the same HTML email template with consistent CSS
classes (order-list__item-title, subtotal-line, etc.), so one parser covers
them all.

The store name is extracted from the From header display name (e.g.,
"Topps <store+12345@t.shopifyemail.com>" → retailer = "topps").

Subject patterns handled:
  - "Order #1234 confirmed"         → Pending
  - "Your order is on its way"      → Shipped
  - "Your order has been delivered"  → Delivered
  - "Your order has been canceled"   → Canceled

Data extracted:
  retailer (store name), order_number, items [{name, sku, quantity, price}],
  subtotal, tax, shipping, order_total, shipping_address, tracking_number,
  status
"""

import re
from bs4 import BeautifulSoup


def _dollars(s: str):
    """Parse '$2,899.95' → float, or None."""
    if not s:
        return None
    m = re.search(r'\$([0-9,]+\.?\d*)', s)
    return float(m.group(1).replace(",", "")) if m else None


def _extract_store_name(from_addr: str) -> str:
    """Extract the display name from a From header like 'StoreName <email>'."""
    if not from_addr:
        return "shopify"
    # Strip angle-bracket email portion
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', from_addr)
    if m:
        name = m.group(1).strip()
        if name:
            return name.lower().replace(" ", "_")
    return "shopify"


def parse(email_data: dict):
    subject    = email_data.get("subject", "")
    body       = email_data.get("body", "")
    email_id   = email_data.get("email_id")
    from_addr  = email_data.get("from_addr", "")
    email_date = email_data.get("email_date")  # YYYY-MM-DD from email Date header

    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(separator="\n")
    sl   = subject.lower()

    # ── Retailer (store name) ──────────────────────────────────────────
    retailer = _extract_store_name(from_addr)

    # ── Status ─────────────────────────────────────────────────────────
    if "cancel" in sl:
        status = "Canceled"
    elif "deliver" in sl:
        status = "Delivered"
    elif "on its way" in sl or "shipped" in sl or "on the way" in sl:
        status = "Shipped"
    else:
        status = "Pending"

    result = {
        "retailer":         retailer,
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
    # Shopify subjects: "Order #1234 confirmed", "Order US-1234-S confirmed"
    m = re.search(r'Order\s+#?([\w-]+)', subject, re.IGNORECASE)
    if m:
        result["order_number"] = m.group(1)
    # Fallback: order-number span in HTML
    if not result["order_number"]:
        for span in soup.find_all("span", class_=lambda c: c and "order-number" in str(c)):
            m = re.search(r'(?:Order\s+)?#?([\w-]+)', span.get_text(strip=True))
            if m and len(m.group(1)) >= 4:
                result["order_number"] = m.group(1)
                break
    # Fallback: Confirmation # in body
    if not result["order_number"]:
        m = re.search(r'Confirmation\s*#?:?\s*([\w-]+)', text)
        if m and len(m.group(1)) >= 4:
            result["order_number"] = m.group(1)

    # ── Items ──────────────────────────────────────────────────────────
    # Shopify order-list: span.order-list__item-title contains
    # "Item Name × Qty" and sibling td has the price.
    for item_tr in soup.find_all("tr", class_="order-list__item"):
        name_span = item_tr.find("span", class_="order-list__item-title")
        if not name_span:
            continue

        raw_name = name_span.get_text(strip=True)
        # Parse "Item Name × 5" or "Item Name x 5"
        qty = 1
        m = re.search(r'(.+?)\s*[×xX]\s*(\d+)\s*$', raw_name)
        if m:
            raw_name = m.group(1).strip()
            qty = int(m.group(2))

        # Price: try <p class="order-list__item-price"> first,
        # then fall back to the price-cell <td> text (some stores use
        # <strong> or <span> instead of <p>)
        price = None
        price_p = item_tr.find("p", class_="order-list__item-price")
        if price_p:
            price = _dollars(price_p.get_text(strip=True))
        if price is None:
            price_td = item_tr.find("td", class_=lambda c: c and "price" in str(c).lower())
            if price_td:
                price = _dollars(price_td.get_text(strip=True))

        # Look for variant/size info
        variant_spans = item_tr.find_all("span", class_=lambda c: c and "variant" in str(c).lower())
        sku = None
        for vs in variant_spans:
            vt = vs.get_text(strip=True)
            if vt:
                sku = vt

        result["items"].append({
            "name":     raw_name,
            "sku":      sku,
            "quantity": qty,
            "price":    price,
        })

    # ── Subtotal / Shipping / Tax / Total ──────────────────────────────
    for row in soup.find_all("tr", class_="subtotal-line"):
        title_td = row.find("td", class_="subtotal-line__title")
        value_td = row.find("td", class_="subtotal-line__value")
        if not title_td or not value_td:
            continue
        label = title_td.get_text(strip=True).lower()
        val   = _dollars(value_td.get_text(strip=True))
        if "subtotal" in label:
            result["subtotal"] = val
        elif "shipping" in label:
            result["shipping"] = val
        elif "tax" in label:
            result["tax"] = val
        elif "total" in label:
            result["order_total"] = val

    # ── Shipping address ───────────────────────────────────────────────
    for h4 in soup.find_all("h4"):
        if "shipping address" in h4.get_text(strip=True).lower():
            p = h4.find_next_sibling("p")
            if p:
                result["shipping_address"] = p.get_text(separator=", ").strip()
            break

    # ── Tracking number ────────────────────────────────────────────────
    # 1. "FedEx tracking number: 396805263281" / "tracking number: 1Z..."
    m = re.search(r'tracking\s*(?:#|number|num)[s]?\s*[:\s]\s*(\w{10,})', text, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1)
    # 2. Carrier tracking URL (fedex, ups, usps)
    if not result["tracking_number"]:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(c in href.lower() for c in ["fedex.com", "ups.com", "usps.com", "narvar.com"]):
                tm = re.search(r'(\d{12,22}|1Z[A-Z0-9]{16})', href, re.IGNORECASE)
                if tm:
                    result["tracking_number"] = tm.group(1)
                    break
    # 3. Generic: any link with "track" in href containing a long number
    if not result["tracking_number"]:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "track" in href.lower():
                tm = re.search(r'(\d{12,22}|1Z[A-Z0-9]{16})', href, re.IGNORECASE)
                if tm:
                    result["tracking_number"] = tm.group(1)
                    break

    # ── Text fallback for items (non-standard templates) ─────────────
    # If no items found via Shopify CSS classes, scan plain text for
    # "Items in this shipment" or "Order summary" sections
    if not result["items"] and result["order_number"]:
        lines = text.split("\n")
        in_items = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if any(h in stripped.lower() for h in ["items in this shipment", "order summary"]):
                in_items = True
                continue
            if in_items:
                # Stop at next section header
                if any(h in stripped.lower() for h in [
                    "subtotal", "shipping address", "customer information",
                    "billing address", "payment", "if you have any",
                ]):
                    break
                # Skip prices, quantities, short strings
                if stripped.startswith("$") or len(stripped) < 5:
                    continue
                # This might be an item name
                if re.match(r'^[A-Z]', stripped) and len(stripped) > 8:
                    qty = 1
                    m = re.search(r'(.+?)\s*[×xX]\s*(\d+)\s*$', stripped)
                    if m:
                        stripped = m.group(1).strip()
                        qty = int(m.group(2))
                    result["items"].append({
                        "name": stripped, "sku": None, "quantity": qty, "price": None,
                    })

    if not result["order_number"]:
        return None

    # ── Log (only for actual orders, not marketing emails) ─────────────
    item_names = ", ".join(f"{i['quantity']}x {i['name'][:30]}" for i in result["items"]) or "no items"
    print(
        f"[Parser/Shopify] store={retailer} | order={result['order_number']} | "
        f"status={status} | items=[{item_names}] | total={result['order_total']}"
    )

    return result
