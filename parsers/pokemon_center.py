import re
from bs4 import BeautifulSoup


def parse(email_data):
    body    = email_data.get("body", "")
    subject = email_data.get("subject", "").lower()
    soup    = BeautifulSoup(body, "html.parser")

    # Clean line list — the flat text structure is reliable and consistent
    text  = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Email type ────────────────────────────────────────────────────────────
    if "delivered" in subject:
        email_type = "delivered"
    elif "on its way" in subject or "shipped" in subject:
        email_type = "shipped"
    elif "cancel" in subject:
        email_type = "canceled"
    else:
        email_type = "confirmation"

    status_map = {
        "confirmation": "Pending",
        "shipped":      "Shipped",
        "delivered":    "Delivered",
        "canceled":     "Canceled",
    }

    result = {
        "retailer":         "pokemon_center",
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

    # ── Order number ──────────────────────────────────────────────────────────
    # Lines: "Order Number:" / "P0033XXXXXX"
    for i, line in enumerate(lines):
        if re.match(r"Order Number", line, re.I):
            # Value may be on the same line after a colon, or on the next line
            inline = re.search(r"Order Number[:\s]+([A-Z0-9\-]+)", line, re.I)
            if inline:
                result["order_number"] = inline.group(1).strip()
            elif i + 1 < len(lines):
                candidate = lines[i + 1].strip()
                if re.match(r"[A-Z0-9\-]{6,}", candidate):
                    result["order_number"] = candidate
            break

    # ── Order date ────────────────────────────────────────────────────────────
    # Lines: "Date Ordered:" / "March 11, 2026"
    for i, line in enumerate(lines):
        if re.match(r"Date Ordered", line, re.I):
            inline = re.search(r"Date Ordered[:\s]+(.+)", line, re.I)
            if inline:
                result["order_date"] = inline.group(1).strip()
            elif i + 1 < len(lines):
                result["order_date"] = lines[i + 1].strip()
            break

    # ── Totals ────────────────────────────────────────────────────────────────
    # Pattern: label line followed by "$XX.XX" line
    _AMOUNT = re.compile(r'^\$([0-9,]+\.[0-9]{2})$')

    def _next_amount(idx):
        """Return float from the first $X.XX line at or after idx, or None."""
        for j in range(idx, min(idx + 3, len(lines))):
            m = _AMOUNT.match(lines[j])
            if m:
                return float(m.group(1).replace(",", ""))
        return None

    for i, line in enumerate(lines):
        ll = line.lower()
        if ll == "order subtotal":
            result["subtotal"] = _next_amount(i + 1)
        elif ll == "sales tax":
            result["tax"] = _next_amount(i + 1)
        elif ll == "shipping":
            result["shipping"] = _next_amount(i + 1)
        elif ll == "order total":
            result["order_total"] = _next_amount(i + 1)

    # ── Shipping address ──────────────────────────────────────────────────────
    # Lines after "Shipping Address:" up to next section header
    _SECTION_HEADERS = {
        "order summary", "billing address", "payment & shipping",
        "order details", "new releases",
    }
    for i, line in enumerate(lines):
        if re.match(r"Shipping Address", line, re.I):
            addr_lines = []
            for j in range(i + 1, min(i + 8, len(lines))):
                if lines[j].lower() in _SECTION_HEADERS:
                    break
                addr_lines.append(lines[j])
            if addr_lines:
                result["shipping_name"]    = addr_lines[0]
                result["shipping_address"] = ", ".join(addr_lines[1:])
            break

    # ── Tracking number ───────────────────────────────────────────────────────
    m = re.search(r"Tracking Number[:\s]*\n?\s*([0-9]{10,})", text)
    if m:
        result["tracking_number"] = m.group(1).strip()
    if not result["tracking_number"]:
        m = re.search(r"tracking_numbers=([0-9]+)", body)
        if m:
            result["tracking_number"] = m.group(1).strip()

    # ── Line items ────────────────────────────────────────────────────────────
    # Structure in text:
    #   <item name>
    #   SKU #
    #   : <sku>
    #   Qty
    #   : <qty>
    #   Price
    #   : $<price>
    #
    # For delivered emails the structure may differ slightly; we do a best-effort.

    if email_type == "delivered":
        # Delivered emails use bold divs — fall back to name-only items
        name_divs = soup.find_all(
            "div", style=re.compile(r"font-weight\s*:\s*600", re.I)
        )
        for nd in name_divs:
            name_text = nd.get_text(strip=True)
            if not name_text or len(name_text) < 5:
                continue
            item = {"name": name_text, "sku": None, "quantity": None, "price": None}
            sib = nd.find_next_sibling("div")
            if sib:
                sib_text = sib.get_text()
                m = re.search(r"SKU[:\s#]+([^\n<]+)", sib_text, re.I)
                if m:
                    item["sku"] = m.group(1).strip()
                m = re.search(r"Qty[:\s]+(\d+)", sib_text, re.I)
                if m:
                    item["quantity"] = int(m.group(1))
            result["items"].append(item)
    else:
        # Confirmation / shipped emails: scan lines for the SKU # marker
        i = 0
        while i < len(lines):
            if lines[i] == "SKU #":
                # Item name is the line immediately before "SKU #"
                name = lines[i - 1] if i > 0 else ""
                sku      = None
                quantity = None

                # Scan forward for ": <value>" pairs
                j = i + 1
                while j < len(lines) and j < i + 10:
                    # Values appear as ": value" right after the label
                    if lines[j].startswith(":"):
                        val = lines[j][1:].strip()
                        # Which label came before this value?
                        label_line = lines[j - 1].lower() if j > 0 else ""
                        if "sku" in label_line:
                            sku = val
                        elif "qty" in label_line or "quantity" in label_line:
                            try:
                                quantity = int(val)
                            except ValueError:
                                pass
                    j += 1

                if name and len(name) >= 3:
                    result["items"].append({
                        "name":     name,
                        "sku":      sku,
                        "quantity": quantity,
                        "price":    None,   # cost derived from order_total / total_qty
                    })
            i += 1

    # Pokemon Center order numbers always start with P followed by digits (e.g. P0033680222)
    if not result["order_number"] or not re.match(r'^P\d+$', result["order_number"]):
        return None

    print(
        f"[PKC] {result['order_number']} | {email_type} | "
        f"total={result['order_total']} | subtotal={result['subtotal']} | "
        f"tax={result['tax']} | items={len(result['items'])} | "
        f"qty={sum((it.get('quantity') or 1) for it in result['items'])}"
    )
    return result
