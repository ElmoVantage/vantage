"""
eBay email parser — handles two email types:

1. SOLD emails (email_type = "sold")
   Subject patterns:
     - "You made the sale for ITEM"
     - "You sold: ITEM"
     - "Congratulations, you sold: ITEM"
   Data extracted: Sold, Shipping, Order, Date sold, Buyer
   sale_price = item price + buyer-paid shipping (total revenue received).

2. SHIPPING LABEL emails (email_type = "label")
   Subject patterns:
     - "eBay shipping label for ITEM"
   Data extracted: tracking number (labelValueLabel "Tracking"), order number
     (from URL orderid= or PDF filename), label cost (seller's postage cost).

Handles forwarded emails (From is iCloud, not ebay.com) by matching on subject.
"""

import re
import email as email_lib
from bs4 import BeautifulSoup


def _extract_html_body(raw_body):
    if not raw_body:
        return ""
    if isinstance(raw_body, str):
        stripped = raw_body.lstrip()
        if stripped.startswith("<") or "doctype" in stripped[:100].lower():
            return raw_body
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


def _parse_label_value_table(soup):
    """
    Extract the structured label→value pairs from eBay's sale email table.
    Looks for <h4 class="labelValueLabel"> inside a <td>, then reads the
    next sibling <td>'s text as the value.

    Returns dict with lowercase keys, e.g.:
      {"sold": "$2.00", "shipping": "$1.25", "order": "15-14394-04605",
       "date sold": "Mar 22, 2026 16:43", "buyer": "ryanajochims"}
    """
    data = {}
    for h4 in soup.find_all("h4", class_=lambda c: c and any("labelvaluelabel" in cls.lower() for cls in (c if isinstance(c, list) else [c]))):
        label = h4.get_text(strip=True).rstrip(":").lower()
        parent_td = h4.find_parent("td")
        if not parent_td:
            continue
        next_td = parent_td.find_next_sibling("td")
        if not next_td:
            continue
        # Prefer link text for buyer (the eBay username is a link)
        a = next_td.find("a")
        if a:
            value = a.get_text(strip=True)
        else:
            value = next_td.get_text(strip=True)
        if value:
            data[label] = value
    return data


def _dollars(s):
    """Parse '$2.00' or '2.00' → float, or return None."""
    if not s:
        return None
    m = re.search(r'\$?([0-9,]+\.[0-9]{2})', s)
    return float(m.group(1).replace(",", "")) if m else None


def _is_label_email(subject: str) -> bool:
    return bool(re.search(r'ebay\s+shipping\s+label\s+for', subject, re.IGNORECASE))


def _is_listing_email(subject: str) -> bool:
    return bool(re.search(
        r'(your\s+item\s+is\s+(live|listed)|your\s+listing\s+is\s+(live|active)|'
        r'congratulations.{0,20}(listed|live)|item\s+(is\s+)?listed\s+on\s+ebay)',
        subject, re.IGNORECASE
    ))


def _parse_listing_email(email_data, html, soup):
    """Parse a listing-confirmation email. Returns result dict with email_type='listing'."""
    subject  = email_data.get("subject", "")
    email_id = email_data.get("email_id")

    result = {
        "email_type":    "listing",
        "platform":      "ebay",
        "email_id":      email_id,
        "item_name":     None,
        "listing_price": None,
        "listing_id":    None,
        "date_listed":   None,
    }

    text = soup.get_text(separator="\n")

    # Item name: try prominent headings first, then subject
    for tag in soup.find_all(["h1", "h2", "h3"]):
        t = tag.get_text(strip=True)
        if 10 < len(t) < 200 and not re.search(
            r'\b(ebay|congrat|listed|live|listing|item)\b', t, re.IGNORECASE
        ):
            result["item_name"] = t
            break
    if not result["item_name"]:
        m = re.search(r'(?:item|listing)[:\s]+(.{10,120}?)(?:\n|$)', text, re.IGNORECASE)
        if m:
            result["item_name"] = m.group(1).strip()

    # eBay item/listing number
    m = re.search(r'(?:item\s*(?:number|#|id)|listing\s*(?:number|id))[:\s#]*(\d{10,13})', text, re.IGNORECASE)
    if m:
        result["listing_id"] = m.group(1)
    if not result["listing_id"]:
        m = re.search(r'(?:/itm/|[?&]item[=])(\d{10,13})', html)
        if m:
            result["listing_id"] = m.group(1)

    # Listing price
    m = re.search(r'(?:buy\s+it\s+now|price|listed\s+(?:for|at|price))[:\s]*\$([0-9,]+\.[0-9]{2})', text, re.IGNORECASE)
    if m:
        result["listing_price"] = _dollars(m.group(0))
    if result["listing_price"] is None:
        prices = re.findall(r'\$([0-9]+\.[0-9]{2})', text)
        if prices:
            result["listing_price"] = float(prices[0].replace(",", ""))

    print(
        f"[Parser/eBay/listing] id={result['listing_id']} | "
        f"item={str(result['item_name'])[:45]} | price={result['listing_price']}"
    )

    if not result["item_name"] and not result["listing_id"]:
        return None

    return result


def _parse_label_email(email_data, html, soup):
    """Parse a shipping-label email. Returns result dict with email_type='label'."""
    subject = email_data.get("subject", "")

    result = {
        "email_type":      "label",
        "platform":        "ebay",
        "email_id":        email_data.get("email_id"),
        "item_name":       None,
        "order_number":    None,
        "tracking_number": None,
        "label_cost":      None,   # seller's postage cost in dollars
    }

    # Item name from subject: "eBay shipping label for ITEM"
    m = re.search(r'ebay\s+shipping\s+label\s+for\s+(.+)', subject, re.IGNORECASE)
    if m:
        result["item_name"] = m.group(1).strip()

    # Order number: from URL orderid= or PDF filename ebay-label-NN-NNNNN-NNNNN.pdf
    m = re.search(r'orderid=([\d\-]{13,15})', html, re.IGNORECASE)
    if m:
        result["order_number"] = m.group(1)
    if not result["order_number"]:
        m = re.search(r'ebay-label-(\d{2}-\d{5}-\d{5})', html, re.IGNORECASE)
        if m:
            result["order_number"] = m.group(1)
    if not result["order_number"]:
        text = soup.get_text(separator="\n")
        m = re.search(r'\b(\d{2}-\d{5}-\d{5})\b', text)
        if m:
            result["order_number"] = m.group(1)

    # Tracking number from labelValueLabel table
    lv = _parse_label_value_table(soup)
    tracking_raw = lv.get("tracking") or lv.get("tracking number") or lv.get("tracking #")
    if tracking_raw:
        m = re.search(r'[A-Z0-9]{10,}', tracking_raw.upper())
        if m:
            result["tracking_number"] = m.group(0)

    # Fallback: tracking pattern anywhere in text
    if not result["tracking_number"]:
        text = soup.get_text(separator="\n")
        m = re.search(r'(?:Tracking(?:\s+number|\s+#)?)[^\w]*([A-Z0-9]{10,})', text, re.IGNORECASE)
        if m:
            result["tracking_number"] = m.group(1).strip()

    # Label/postage cost — "Order total" row
    cost_raw = lv.get("order total") or lv.get("label cost") or lv.get("postage")
    result["label_cost"] = _dollars(cost_raw)

    print(
        f"[Parser/eBay/label] order={result['order_number']} | "
        f"item={str(result['item_name'])[:45]} | "
        f"tracking={result['tracking_number']} | cost={result['label_cost']}"
    )

    if not result["order_number"] and not result["tracking_number"]:
        return None

    return result


def parse(email_data):
    raw_body = email_data.get("body", "")
    subject  = email_data.get("subject", "")

    html = _extract_html_body(raw_body)
    soup = BeautifulSoup(html, "html.parser")

    # Route to label parser or listing parser based on subject
    if _is_label_email(subject):
        return _parse_label_email(email_data, html, soup)
    if _is_listing_email(subject):
        return _parse_listing_email(email_data, html, soup)

    result = {
        "email_type":      "sold",
        "platform":        "ebay",
        "email_id":        email_data.get("email_id"),
        "item_name":       None,
        "order_number":    None,
        "sale_price":      None,
        "ebay_fee":        None,
        "shipping_cost":   None,   # buyer-paid shipping (added to sale_price)
        "quantity":        1,
        "buyer":           None,
        "date_sold":       None,
        "tracking_number": None,
    }

    # ── Item name from subject ─────────────────────────────────────────────
    # "You made the sale for SV07: Stellar Crown #166/142 Lacey"
    # "Fwd: You made the sale for ..."
    # "You sold: Item Name"
    # "Congratulations, you sold: Item Name"
    m = re.search(
        r"(?:you(?:\s+made\s+the\s+sale\s+for|\s+sold:?))\s*(.+)",
        subject, re.IGNORECASE
    )
    if m:
        result["item_name"] = m.group(1).strip()

    # ── Item name fallback: product image alt text ─────────────────────────
    if not result["item_name"]:
        for img in soup.find_all("img", alt=True):
            alt = img.get("alt", "").strip()
            # Skip icons, logos, and very short strings
            if len(alt) > 10 and not re.search(
                r'(?:logo|icon|shop|store|instagram|facebook|star|package|label|ebay)',
                alt, re.IGNORECASE
            ):
                result["item_name"] = alt
                break

    # ── Structured label-value table ───────────────────────────────────────
    lv = _parse_label_value_table(soup)

    # Order number — from table, then URL pattern, then order-id in any URL
    order_val = lv.get("order", "")
    m = re.search(r'\b(\d{2}-\d{5}-\d{5})\b', order_val)
    if m:
        result["order_number"] = m.group(1)
    if not result["order_number"]:
        # Try URLs: orderid=15-14394-04605
        m = re.search(r'orderid=(\d{2}-\d{5}-\d{5})', html, re.IGNORECASE)
        if m:
            result["order_number"] = m.group(1)
    if not result["order_number"]:
        # Generic NN-NNNNN-NNNNN anywhere in body text
        text = soup.get_text(separator="\n")
        m = re.search(r'\b(\d{2}-\d{5}-\d{5})\b', text)
        if m:
            result["order_number"] = m.group(1)

    # Sale price — item price + buyer-paid shipping = total revenue
    item_price    = _dollars(lv.get("sold"))
    buyer_shipping = _dollars(lv.get("shipping"))
    if item_price is not None:
        result["sale_price"]    = item_price + (buyer_shipping or 0.0)
        result["shipping_cost"] = buyer_shipping  # stored for reference
    elif buyer_shipping is not None:
        result["sale_price"] = buyer_shipping

    # Quantity — eBay shows "Qty" or "Quantity" in the label-value table
    qty_raw = lv.get("qty") or lv.get("quantity")
    if qty_raw:
        m = re.search(r'(\d+)', qty_raw)
        if m:
            result["quantity"] = int(m.group(1))

    # Buyer username
    result["buyer"] = lv.get("buyer")

    # Date sold — strip time portion if present ("Mar 22, 2026 16:43" → "Mar 22, 2026")
    date_raw = lv.get("date sold") or lv.get("date") or lv.get("sold on")
    if date_raw:
        m = re.match(r'([A-Z][a-z]+ \d{1,2},?\s+\d{4})', date_raw.strip())
        result["date_sold"] = m.group(1) if m else date_raw.strip()

    # Tracking number (not usually in the sold email, but handle if present)
    text = soup.get_text(separator="\n")
    m = re.search(r'(?:Tracking number|Tracking #)[^\d]*(\d{10,})', text, re.IGNORECASE)
    if m:
        result["tracking_number"] = m.group(1).strip()

    print(
        f"[Parser/eBay] order={result['order_number']} | "
        f"item={str(result['item_name'])[:45]} | "
        f"price={result['sale_price']} | buyer={result['buyer']} | "
        f"date={result['date_sold']}"
    )

    if not result["item_name"] and not result["order_number"]:
        return None

    return result
