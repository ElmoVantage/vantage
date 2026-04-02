import re
import email as email_lib
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------

def _extract_html_body(raw_body):
    """Extract HTML from multipart/alternative Target emails."""
    if not raw_body:
        return ""

    if isinstance(raw_body, str):
        raw_bytes = raw_body.encode("utf-8", errors="replace")
    else:
        raw_bytes = raw_body

    try:
        msg = email_lib.message_from_bytes(raw_bytes)
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
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

    if isinstance(raw_body, str):
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
    # confirmation : "Thanks for shopping with us! Here's your order #:..."
    # shipped      : "...are about to ship."
    # delivered    : "Items have arrived from order #..."
    # canceled     : "Sorry, we had to cancel order #..."
    if "have arrived" in subject or "items have arrived" in subject:
        email_type = "delivered"
    elif "cancel" in subject:
        email_type = "canceled"
    elif "about to ship" in subject or "are about to ship" in subject:
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
        "retailer":         "target",
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

    # ---- Order number ----
    # Appears in subject as "#:102003314995364" or "#102003309239763"
    # Also in HTML as link text "Order #102003309239763"
    m = re.search(r'#:?(\d{15,})', email_data.get("subject", ""))
    if not m:
        m = re.search(r'order\s+#:?(\d{15,})', text, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d{15,})', text)
    if m:
        result["order_number"] = m.group(1)

    # ---- Order date ----
    # Confirmation: "Placed March 11, 2026"
    m = re.search(r'Placed\s+([A-Z][a-z]+ \d{1,2},?\s+\d{4})', text)
    if m:
        result["order_date"] = m.group(1).strip()

    # ---- Order total ----
    # Confirmation: large "$64.43" in order total block, also "Total $64.43" in summary
    # Shipped: "processed a payment of $64.43"
    # Find "Total" label then the dollar amount — check order summary section first
    total_m = re.search(r'\bTotal\b[^$\d]*\$([0-9,]+\.[0-9]{2})', text)
    if total_m:
        result["order_total"] = float(total_m.group(1).replace(",", ""))
    else:
        # Shipped email: "processed a payment of $X"
        pay_m = re.search(r'processed a payment of \$([0-9,]+\.[0-9]{2})', text, re.IGNORECASE)
        if pay_m:
            result["order_total"] = float(pay_m.group(1).replace(",", ""))

    # ---- Tracking number ----
    # Shipped: "FEDEX GROUND Tracking # 399463431466"
    # Also appears as plain text "Tracking # NNNN"
    m = re.search(r'[Tt]racking\s*#\s*([0-9]{10,})', text)
    if m:
        result["tracking_number"] = m.group(1).strip()

    # ---- Shipping address ----
    # Confirmation: "Delivers to: Suzann Boykins, 218 E Coler St, Jackson, MI 49201"
    # Delivered:    "Delivered to: Suzann Boykins, 218 E Coler St, Jackson, MI, 49201"
    # Shipped:      "Delivers to: Sharday Volnak, 312 Bates St, Level 4, Jackson, MI, 49203"
    # All use "Delivers to:" or "Delivered to:" followed by name + address on same line
    addr_m = re.search(
        r'(?:Delivers? to:|Delivered to:)\s*([^\n]+)',
        text, re.IGNORECASE
    )
    if addr_m:
        full = addr_m.group(1).strip()
        # Split name from address at first street number
        parts = re.split(r',\s*(?=\d)', full, maxsplit=1)
        if len(parts) == 2:
            # Check if the first part looks like a name (no digits)
            if not re.search(r'\d', parts[0]):
                result["shipping_name"]    = parts[0].strip()
                result["shipping_address"] = parts[1].strip()
            else:
                result["shipping_address"] = full
        else:
            result["shipping_address"] = full

    # ---- Items ----
    items = _parse_items(soup, email_type)
    result["items"] = items

    print(f"[Parser/Target] {result['order_number']} | type={email_type} | "
          f"total={result['order_total']} | tracking={result['tracking_number']} | "
          f"items={len(result['items'])}")

    if not result["order_number"]:
        return None

    return result


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------

def _parse_items(soup, email_type):
    """
    All Target email types share the same product block structure:
      <h2> or <h1> with product name as link text
      <p> Qty: 2
      <p> $31.99 / ea  (confirmation only)

    Delivered emails show no price per ea — only the item name and qty.
    Canceled emails show no items at all.
    """
    items = []
    seen  = set()

    if email_type == "canceled":
        return items

    # Target product names are in anchor tags inside h1/h2 inside the product block
    # The link text is the full product name
    # Strategy: find all <a> tags whose text looks like a product name
    # (long text, not "Order #...", not "Visit order details", etc.)
    for a in soup.find_all("a"):
        name = a.get_text(strip=True)
        # Filter out navigation links — product names are long and have no typical nav keywords
        if len(name) < 15:
            continue
        if re.search(r'order details|write a review|shop now|track status|receipts|start a return|fix an issue|visit|target\.com|help|returns|contact|find a store|terms|privacy', name, re.IGNORECASE):
            continue
        if re.search(r'^\$|^#\d|^Order #', name):
            continue
        # Skip anything that's purely a number or order number
        if re.match(r'^#?\d+$', name):
            continue
        # Must contain at least one product-like word
        if not re.search(r'[A-Za-z]{3,}', name):
            continue
        if name in seen:
            continue
        seen.add(name)

        # Walk up to find containing product block for qty/price
        container = a
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

        # Unit price: "$31.99 / ea" — present in confirmation email
        price = None
        p_m = re.search(r'\$([0-9,]+\.[0-9]{2})\s*/\s*ea', container_text, re.IGNORECASE)
        if p_m:
            price = float(p_m.group(1).replace(",", ""))

        # Only add if we found at least a qty — avoids picking up footer/promo links
        if qty is not None:
            items.append({
                "name":     name,
                "sku":      None,
                "quantity": qty,
                "price":    price,
            })

    return items
