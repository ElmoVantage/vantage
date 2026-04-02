import re
import email as email_lib
import quopri
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------

def _extract_html_body(raw_body):
    """
    Handle both plain text/html and multipart/related emails.
    imap_client.get_email_body already extracts HTML — detect that and return directly.
    """
    if not raw_body:
        return ""

    # If it's already a decoded HTML string (from imap_client.get_email_body),
    # return it directly — no need to re-parse as MIME
    if isinstance(raw_body, str):
        stripped = raw_body.lstrip()
        if stripped.startswith("<") or "doctype" in stripped[:100].lower():
            return raw_body
        # Not HTML — try MIME parse then quopri fallback
        raw_bytes = raw_body.encode("utf-8", errors="replace")
    else:
        raw_bytes = raw_body

    try:
        msg = email_lib.message_from_bytes(raw_bytes)
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    except Exception:
        pass

    # Fallback: decode as quoted-printable string
    if isinstance(raw_body, str):
        try:
            decoded = quopri.decodestring(raw_body.encode("utf-8", errors="replace"))
            return decoded.decode("utf-8", errors="replace")
        except Exception:
            return raw_body

    return raw_body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse(email_data):
    raw_body = email_data.get("body", "")
    subject  = email_data.get("subject", "").lower()

    html = _extract_html_body(raw_body)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

    # ---- Email type detection ----
    # confirmation : "Thanks for your delivery order, {name}"
    # shipped      : "Shipped: Pokemon Trading Cards..."
    # delivered    : "Arrived: Your Pokemon Trading Cards..."
    # canceled     : "Canceled: delivery of Pokemon Trading Cards..."
    if "arrived:" in subject:
        email_type = "delivered"
    elif "canceled" in subject or "cancelled" in subject:
        email_type = "canceled"
    elif "shipped" in subject:
        email_type = "shipped"
    else:
        email_type = "confirmation"

    status_map = {
        "confirmation": "Pending",
        "shipped":      "Shipped",
        "delivered":    "Delivered",
        "canceled":     "Canceled",
    }

    result = {
        "retailer":         "walmart",
        "email_id":         email_data.get("email_id"),
        "email_type":       email_type,
        "order_number":     None,
        "order_date":       None,
        "subtotal":         None,
        "tax":              None,
        "shipping":         None,
        "order_total":      None,
        "shipping_name":    None,
        "shipping_address": None,
        "tracking_number":  None,
        "status":           status_map[email_type],
        "items":            [],
    }

    # ---- Order number (format: 2000146-53317659) ----
    m = re.search(r'#?(\d{7}-\d{8})', text)
    if m:
        result["order_number"] = m.group(1)
    else:
        m = re.search(r'aria-label=["\']([0-9 ]{20,})["\']', html)
        if m:
            result["order_number"] = m.group(1).replace(" ", "")

    if email_type == "delivered" and not result["order_number"]:
        print(f"[Parser/Walmart] WARNING: Could not extract order number from delivered email")

    # ---- Order date ----
    m = re.search(r'Order date:\s*(.+?)(?:\n|<)', text)
    if m:
        result["order_date"] = m.group(1).strip()

    # ---- Order total ----
    # Confirmation: automation-id="order-total" table
    # Delivered: pulled from item line total (see below)
    order_total_table = soup.find(attrs={"automation-id": "order-total"})
    if order_total_table:
        total_text = order_total_table.get_text()
        m = re.search(r'\$([0-9,]+\.[0-9]{2})', total_text)
        if m:
            result["order_total"] = float(m.group(1).replace(",", ""))

    # ---- Tracking number ----
    m = re.search(
        r'(?:tracking number|tracking #)[^0-9]*([0-9]{10,})',
        text, re.IGNORECASE
    )
    if m:
        result["tracking_number"] = m.group(1).strip()

    # ---- Shipping address ----
    # Walmart prepends a 2-4 char routing code: "KXQ 1428 S Jackson St..."
    # Search line-by-line to avoid matching junk whitespace from soup.get_text()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        addr_m = re.match(
            r'^[A-Z]{2,4}\s+(\d+\s+[A-Za-z].{5,},\s*[A-Za-z\s]+,\s*[A-Z]{2},?\s*\d{5}(?:-\d{4})?,?\s*USA)$',
            line, re.IGNORECASE
        )
        if addr_m:
            result["shipping_address"] = addr_m.group(1).strip()
            break

    # ---- Items ----
    # Delivery email: rich item table with name, qty, price
    # Shipped/Canceled/Confirmation: img alt with "quantity N item {name}"
    items = _parse_delivery_items(soup)
    if not items:
        items = _parse_img_alt_items(soup)
    result["items"] = items

    # Canceled: extract total from "Temporary hold released" payment section
    if email_type == "canceled" and result["order_total"] is None:
        # Look for dollar amount in secondaryCss cells (payment summary table)
        for td in soup.find_all("td", class_=re.compile(r'secondaryCss')):
            m = re.search(r'\$([0-9,]+\.[0-9]{2})', td.get_text())
            if m:
                result["order_total"] = float(m.group(1).replace(",", ""))
                break
        # Fallback: "Temporary hold released" heading then next dollar amount
        if result["order_total"] is None:
            hold_m = re.search(
                r'Temporary hold released[^$]*\$([0-9,]+\.[0-9]{2})',
                text, re.IGNORECASE
            )
            if hold_m:
                result["order_total"] = float(hold_m.group(1).replace(",", ""))

    # Delivery: use item line total as order_total if not already set
    if email_type == "delivered" and result["order_total"] is None and items:
        for item in items:
            if item.get("price") is not None:
                result["order_total"] = item["price"]
                break

    print(f"[Parser/Walmart] {result['order_number']} | type={email_type} | "
          f"total={result['order_total']} | tracking={result['tracking_number']} | "
          f"items={len(result['items'])}")

    # For delivered emails, allow through even without order number
    # so save_order can try to match by tracking number
    if not result["order_number"] and email_type != "delivered":
        return None

    return result


# ---------------------------------------------------------------------------
# Item parsing helpers
# ---------------------------------------------------------------------------

def _parse_delivery_items(soup):
    """
    Delivery email only. Has a full line-item table:
      <span class="itemName-...">Product Name - Styles May Vary</span>
      $22.97/EA
      Qty: 5
      <span style="font-weight:bold;">$114.85</span>
    """
    items = []
    seen  = set()

    name_spans = soup.find_all("span", class_=re.compile(r'itemName-'))
    if not name_spans:
        return items

    for name_span in name_spans:
        name = name_span.get_text(strip=True)
        name = re.sub(r'\s*-\s*Styles? May Vary$', '', name, flags=re.IGNORECASE).strip()
        if not name or name in seen:
            continue
        seen.add(name)

        # Walk up several levels to find the row container
        container = name_span
        for _ in range(6):
            if container.parent:
                container = container.parent
            else:
                break

        container_text = container.get_text(separator="\n")

        qty = None
        q_m = re.search(r'Qty[:\s]+(\d+)', container_text, re.IGNORECASE)
        if q_m:
            qty = int(q_m.group(1))

        # All dollar amounts — skip unit prices ending in /EA
        price = None
        prices = re.findall(r'\$([0-9,]+\.[0-9]{2})(?!\s*/[Ee][Aa])', container_text)
        if prices:
            price = float(prices[-1].replace(",", ""))

        items.append({
            "name":     name,
            "sku":      None,
            "quantity": qty,
            "price":    price,
        })

    return items


def _parse_img_alt_items(soup):
    """
    Shipped/canceled/confirmation emails only.
    alt="quantity 5 item Pokemon Trading Cards SV 8 5 Prismatic Evolutions..."
    No price available in these layouts.
    """
    items = []
    seen  = set()

    for img in soup.find_all("img", alt=re.compile(r'quantity \d+ item', re.I)):
        alt = img.get("alt", "")
        m   = re.search(r'quantity (\d+) item\s+(.+)', alt, re.IGNORECASE)
        if not m:
            continue

        qty  = int(m.group(1))
        name = m.group(2).strip()
        name = re.sub(r'\s+Styles? May Vary$', '', name, flags=re.IGNORECASE).strip()

        if name in seen:
            continue
        seen.add(name)

        items.append({
            "name":     name,
            "sku":      None,
            "quantity": qty,
            "price":    None,
        })

    return items
