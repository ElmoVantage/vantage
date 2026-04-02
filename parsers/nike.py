"""
Nike order email parser — handles Nike.com / SNKRS order confirmations.

Subject patterns:
  - "We just received your order"     → Pending
  - "Your order has shipped"          → Shipped
  - "Your order has been delivered"   → Delivered
  - "Your order has been cancelled"   → Canceled

Nike uses a custom email template (not Shopify). Key data is in:
  - dynamicProductContainer__name  → item name
  - dynamicProductContainer__price → price
  - textCardBodyColumns            → subtotal/shipping/tax/total
  - Order # in hidden preheader div or Manage Order URL
  - Shipping address in a labeled text card section
"""

import re
from bs4 import BeautifulSoup


def _dollars(s: str):
    if not s:
        return None
    m = re.search(r'\$([0-9,]+\.?\d*)', s)
    return float(m.group(1).replace(",", "")) if m else None


def parse(email_data: dict):
    subject  = email_data.get("subject", "")
    body     = email_data.get("body", "")
    email_id = email_data.get("email_id")

    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(separator="\n")
    sl   = subject.lower()

    # ── Status ─────────────────────────────────────────────────────────
    if "cancel" in sl:
        status = "Canceled"
    elif "deliver" in sl:
        status = "Delivered"
    elif "shipped" in sl or "on its way" in sl:
        status = "Shipped"
    else:
        status = "Pending"

    result = {
        "retailer":         "nike",
        "email_id":         email_id,
        "order_number":     None,
        "order_date":       None,
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
    # 1. Hidden preheader: "Order #: C01571252356"
    m = re.search(r'Order\s*#?:?\s*(C\d{10,})', text)
    if m:
        result["order_number"] = m.group(1)
    # 2. Manage Order URL: /orders/details/C01571252356/
    if not result["order_number"]:
        m = re.search(r'/orders/details/(C\d{10,})/', body)
        if m:
            result["order_number"] = m.group(1)
    # 3. Generic fallback: any C followed by 10+ digits
    if not result["order_number"]:
        m = re.search(r'\b(C\d{10,})\b', text)
        if m:
            result["order_number"] = m.group(1)

    # ── Order date ─────────────────────────────────────────────────────
    # SNKRS template: "Order Date" eyebrow label
    for eyebrow in soup.find_all("p", class_=lambda c: c and "eyebrow" in str(c)):
        if "order date" in eyebrow.get_text(strip=True).lower():
            parent_td = eyebrow.find_parent("td")
            if parent_td:
                copy_div = parent_td.find("div", class_=lambda c: c and "copy" in str(c))
                if copy_div:
                    date_text = copy_div.get_text(strip=True)
                    result["order_date"] = date_text
            break
    # Narvar shipping template: "Order\n  Date\n...\n03/07/2026"
    if not result["order_date"]:
        m = re.search(r'Order\s+Date\s*\n\s*(\d{1,2}/\d{1,2}/\d{4})', text)
        if m:
            result["order_date"] = m.group(1)

    # ── Items ──────────────────────────────────────────────────────────
    # Nike uses dynamicProductContainer divs
    containers = soup.find_all("table", class_=lambda c: c and "dynamicProductsContainer" in str(c))
    if not containers:
        # Fallback: find product name/price divs directly
        containers = [soup]

    for container in containers:
        name_div  = container.find("div", class_=lambda c: c and "dynamicProductContainer__name" in str(c))
        price_div = container.find("div", class_=lambda c: c and "dynamicProductContainer__price" in str(c))
        size_div  = None
        # Size is in a generic base div containing "Size:"
        for div in container.find_all("div", class_=lambda c: c and "dynamicProductContainer__base" in str(c)):
            dt = div.get_text(strip=True)
            if dt.startswith("Size:"):
                size_div = div
                break

        if not name_div:
            continue

        item_name = name_div.get_text(strip=True)
        price     = _dollars(price_div.get_text(strip=True)) if price_div else None
        sku       = size_div.get_text(strip=True).replace("Size:", "").strip() if size_div else None

        result["items"].append({
            "name":     item_name,
            "sku":      sku,
            "quantity": 1,
            "price":    price,
        })

    # Narvar shipping template fallback: product name div is followed by a
    # "Size:" div in the next <tr>.  Scan all divs with font-weight:500 and
    # look ahead for the Size line to confirm it's actually a product name
    # (not body copy or a label).
    if not result["items"]:
        for div in soup.find_all("div"):
            style = div.get("style") or ""
            txt = div.get_text(strip=True)
            if "font-weight:500" not in style or "font-size:18px" not in style:
                continue
            if not txt or len(txt) < 5:
                continue
            # Skip labels and body copy
            tl = txt.lower()
            if any(lbl in tl for lbl in [
                "order", "shipping method", "nike.com", "hi ", "your gear",
                "please contact", "estimated delivery", "it's on",
            ]):
                continue

            # Confirm by checking if a nearby element has "Size:"
            sku = None
            parent_tr = div.find_parent("tr")
            if parent_tr:
                next_tr = parent_tr.find_next_sibling("tr")
                if next_tr:
                    next_div = next_tr.find("div")
                    if next_div and "Size:" in next_div.get_text():
                        sku = next_div.get_text(strip=True).replace("Size:", "").strip()

            # Only accept if we found a Size line (confirms it's a product)
            # or the text matches a known product pattern (brand + model)
            if sku or re.search(r"(Jordan|Nike|Air|Dunk|Foamposite|Kobe)", txt, re.IGNORECASE):
                result["items"].append({
                    "name":     txt,
                    "sku":      sku,
                    "quantity": 1,
                    "price":    None,
                })
                break

    # ── Estimated delivery ─────────────────────────────────────────────
    date_div = soup.find("div", class_=lambda c: c and "dynamicProductContainer__date" in str(c))
    if date_div:
        m = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', date_div.get_text(strip=True))
        if m:
            # Store as estimated_delivery (used by tracking system)
            result["estimated_delivery"] = m.group(1)

    # ── Totals ─────────────────────────────────────────────────────────
    # Nike uses textCardBodyColumns tables with left/right columns
    for tbl in soup.find_all("table", class_=lambda c: c and "textCardBodyColumns" in str(c)):
        left  = tbl.find("td", class_=lambda c: c and "textCardBodyColumn--left" in str(c))
        right = tbl.find("td", class_=lambda c: c and "textCardBodyColumn--right" in str(c))
        if not left or not right:
            continue
        label = left.get_text(strip=True).lower()
        val   = _dollars(right.get_text(strip=True))
        if "subtotal" in label:
            result["subtotal"] = val
        elif "shipping" in label:
            result["shipping"] = val
        elif "tax" in label:
            result["tax"] = val
        elif "total" in label:
            result["order_total"] = val

    # ── Shipping address ───────────────────────────────────────────────
    for eyebrow in soup.find_all("p", class_=lambda c: c and "eyebrow" in str(c)):
        if "shipping address" in eyebrow.get_text(strip=True).lower():
            parent_td = eyebrow.find_parent("td")
            if parent_td:
                copy_div = parent_td.find("div", class_=lambda c: c and "copy" in str(c))
                if copy_div:
                    # Address lines are separated by <br>
                    addr = copy_div.get_text(separator=", ").strip()
                    # Clean up trailing commas from empty lines
                    addr = re.sub(r',\s*,', ',', addr).strip(", ")
                    result["shipping_address"] = addr
            break

    # ── Tracking number (shipping emails only) ──────────────────────────
    # Delivery/cancel emails have tracking_id in survey URLs but that's not
    # a shipping tracking number — only extract tracking from shipped emails.
    if status == "Shipped":
        # 1. Plain text: "tracking number: ..."
        m = re.search(r'tracking[_\-]?number[s]?[=:]\s*(\w+)', text, re.IGNORECASE)
        if m:
            result["tracking_number"] = m.group(1)
        # 2. URL param: tracking_id=1Z6R014V0321797539 (in Narvar/Qualtrics links)
        if not result["tracking_number"]:
            m = re.search(r'tracking_id[=:](\w{10,})', body, re.IGNORECASE)
            if m:
                result["tracking_number"] = m.group(1)
        # 3. UPS pattern: 1Z followed by 16 alphanumeric chars
        if not result["tracking_number"]:
            m = re.search(r'\b(1Z[A-Z0-9]{16})\b', body, re.IGNORECASE)
            if m:
                result["tracking_number"] = m.group(1)

    # ── Log ────────────────────────────────────────────────────────────
    item_names = ", ".join(f"{i['name'][:40]}" for i in result["items"]) or "no items"
    print(
        f"[Parser/Nike] order={result['order_number']} | status={status} | "
        f"items=[{item_names}] | total={result['order_total']}"
    )

    if not result["order_number"]:
        return None

    return result
