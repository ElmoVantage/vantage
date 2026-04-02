"""
Amazon order email parser — handles Amazon.com order notifications.

Email types handled:
  - Confirmation: "Ordered: ..." (from auto-confirm@amazon.com) → Pending
  - Shipped: "Shipped: ..." / "Your package was shipped!"     → Shipped
  - Delivered: "Delivered: ..." / "Your package was delivered" → Delivered
  - Cancelled: "Item cancelled successfully" / "order has been cancelled/canceled" → Canceled
  - Payment declined: "Payment declined" / "payment couldn't be completed"  → payment_issue (webhook)
  - QLA cancel: "order has been canceled" from qla@amazon.com  → Canceled

Data is extracted primarily from the plain-text MIME part (more reliable than the
heavily styled HTML). Amazon order numbers follow the pattern NNN-NNNNNNN-NNNNNNN.

Items, quantities, and prices are listed as bullet points in the plain text:
  * Item Name
    Quantity: N
    XX.XX USD
"""

import re
from bs4 import BeautifulSoup


def _dollars(s: str):
    if not s:
        return None
    m = re.search(r'(\d+\.?\d*)\s*USD', s)
    if m:
        return float(m.group(1))
    m = re.search(r'\$([0-9,]+\.?\d*)', s)
    return float(m.group(1).replace(",", "")) if m else None


def parse(email_data: dict):
    subject   = email_data.get("subject", "")
    body      = email_data.get("body", "")
    body_text = email_data.get("body_text")  # plain-text MIME part (preferred)
    email_id  = email_data.get("email_id")
    email_date = email_data.get("email_date")

    # Use plain text MIME part if available (has clean bullet-point format);
    # fall back to extracting text from HTML via BeautifulSoup.
    if body_text:
        text = body_text
    else:
        soup = BeautifulSoup(body, "html.parser")
        text = soup.get_text(separator="\n")

    sl = subject.lower()

    # ── Status ─────────────────────────────────────────────────────────
    # Payment issues are special — they trigger webhooks, not orders
    is_payment_issue = False
    if "payment declined" in sl or "payment couldn't be completed" in text.lower():
        is_payment_issue = True
        status = "Canceled"
    elif "cancel" in sl or "cancelled" in sl or "canceled" in sl:
        status = "Canceled"
    elif "delivered" in sl or "your package was delivered" in text.lower():
        status = "Delivered"
    elif "shipped" in sl or "on its way" in sl or "package was shipped" in text.lower():
        status = "Shipped"
    else:
        status = "Pending"

    result = {
        "retailer":         "amazon",
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
        "_payment_issue":   is_payment_issue,
    }

    # ── Order number (NNN-NNNNNNN-NNNNNNN) ─────────────────────────────
    # Strip Unicode directional marks before matching
    clean_text = re.sub(r'[\u200e\u200f\u202a-\u202e]', '', text)
    m = re.search(r'\b(\d{3}-\d{7}-\d{7})\b', clean_text)
    if m:
        result["order_number"] = m.group(1)

    # ── Items from plain text ──────────────────────────────────────────
    # Amazon plain text lists items as:
    # * Item Name
    #   Quantity: N
    #   XX.XX USD
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("* "):
            item_name = line[2:].strip()
            qty = 1
            price = None
            # Look ahead for Quantity and price
            for j in range(i + 1, min(i + 5, len(lines))):
                next_line = lines[j].strip()
                if next_line.startswith("* "):
                    break
                qm = re.match(r'Quantity:\s*(\d+)', next_line)
                if qm:
                    qty = int(qm.group(1))
                pm = re.match(r'(\d+\.?\d*)\s*USD', next_line)
                if pm:
                    price = float(pm.group(1))
            if item_name and len(item_name) > 5:
                result["items"].append({
                    "name":     item_name,
                    "sku":      None,
                    "quantity": qty,
                    "price":    price,
                })
        i += 1

    # ── Fallback: items from HTML (rio-text spans with product links) ──
    if not result["items"]:
        soup = BeautifulSoup(body, "html.parser") if body_text else soup
        for span in soup.find_all("span", class_=lambda c: c and "rio-text" in str(c)):
            a = span.find("a")
            if a and "amazon.com/dp/" in (a.get("href") or ""):
                name = a.get_text(strip=True)
                if name and len(name) > 5 and "..." not in name[-4:]:
                    result["items"].append({
                        "name": name, "sku": None, "quantity": 1, "price": None,
                    })
        # Also check product image alt text
        if not result["items"]:
            for img in soup.find_all("img", class_="productImage"):
                alt = (img.get("alt") or "").strip()
                if alt and len(alt) > 10:
                    result["items"].append({
                        "name": alt, "sku": None, "quantity": 1, "price": None,
                    })

    # ── Fallback: items from HTML soup text (no bullet points) ─────────
    # When only HTML is available, look for "Quantity: N" near item names
    if not result["items"]:
        soup = BeautifulSoup(body, "html.parser") if body_text else soup
        soup_text = soup.get_text(separator="\n")
        soup_lines = soup_text.split("\n")
        for idx, ln in enumerate(soup_lines):
            qm = re.match(r'\s*Quantity:\s*(\d+)', ln)
            if qm:
                qty = int(qm.group(1))
                # Look backwards for item name (non-empty, long enough)
                for back in range(idx - 1, max(idx - 5, -1), -1):
                    candidate = soup_lines[back].strip()
                    if candidate and len(candidate) > 8 and not candidate.startswith("$"):
                        # Skip navigation/header text
                        if any(kw in candidate.lower() for kw in [
                            "arriving", "order #", "view or edit", "track",
                            "delivery", "shipped", "ordered",
                        ]):
                            continue
                        # Clean trailing ellipsis
                        name = re.sub(r'\.\.\.\s*$', '', candidate).strip()
                        if len(name) > 5:
                            result["items"].append({
                                "name": name, "sku": None, "quantity": qty, "price": None,
                            })
                            break

    # ── Total ──────────────────────────────────────────────────────────
    # Plain text: "Grand Total:\n150.04 USD" or "Grand Total:\n$150.04"
    m = re.search(r'Grand\s+Total:\s*\n\s*(\d+\.?\d*)\s*USD', text)
    if m:
        result["order_total"] = float(m.group(1))
    if not result["order_total"]:
        m = re.search(r'Grand\s+Total:\s*\n\s*\$([0-9,]+\.?\d*)', text)
        if m:
            result["order_total"] = float(m.group(1).replace(",", ""))
    # Fallback patterns
    if not result["order_total"]:
        m = re.search(r'Total\s*\n\s*(\d+\.?\d*)\s*USD', text)
        if m:
            result["order_total"] = float(m.group(1))
    if not result["order_total"]:
        m = re.search(r'Order Total:\s*\$([0-9,.]+)', text)
        if m:
            result["order_total"] = float(m.group(1).replace(",", ""))
    if not result["order_total"]:
        m = re.search(r'Total Pending Payment:\s*\$([0-9,.]+)', text)
        if m:
            result["order_total"] = float(m.group(1).replace(",", ""))
    # HTML soup fallback: "Grand Total:" on one line, "$XXX.XX" nearby
    if not result["order_total"] and not body_text:
        soup_text = BeautifulSoup(body, "html.parser").get_text(separator="\n")
        soup_lines = soup_text.split("\n")
        for idx, ln in enumerate(soup_lines):
            if "grand total" in ln.strip().lower():
                for j in range(idx + 1, min(idx + 5, len(soup_lines))):
                    val = _dollars(soup_lines[j].strip())
                    if val:
                        result["order_total"] = val
                        break
                break

    # ── Log ────────────────────────────────────────────────────────────
    if not result["order_number"]:
        return None

    item_names = ", ".join(f"{i['quantity']}x {i['name'][:35]}" for i in result["items"]) or "no items"
    tag = "PAYMENT_ISSUE" if is_payment_issue else status
    print(
        f"[Parser/Amazon] order={result['order_number']} | {tag} | "
        f"items=[{item_names}] | total={result['order_total']}"
    )

    return result
