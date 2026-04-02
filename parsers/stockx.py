"""
StockX email parser — handles both listing ("Your Ask Is Live!") and
sale ("You Sold Your Item!") confirmation emails.

Listing data:  item_name, listing_price (ask), transaction_fee,
               payment_proc_fee, shipping_fee, total_payout, style_id, size
Sale data:     item_name, order_number, sale_price, platform_fee (total),
               transaction_fee, payment_proc_fee, shipping_fee, total_payout,
               style_id, size
"""

import re
from bs4 import BeautifulSoup


def _dollars(s: str):
    """Parse '$271' or '-$8.13' → float (sign preserved), or None."""
    if not s:
        return None
    s = s.strip()
    negative = s.startswith("-")
    m = re.search(r"\$([0-9,]+\.?[0-9]*)", s)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    return -val if negative else val


def parse(email_data: dict):
    subject  = email_data.get("subject", "")
    body     = email_data.get("body", "")
    email_id = email_data.get("email_id")

    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(separator="\n")
    sl   = subject.lower()

    # ── Email type ─────────────────────────────────────────────────────────
    email_type = "sale" if "you sold your item" in sl else "listing"

    result = {
        "email_type":       email_type,
        "platform":         "stockx",
        "email_id":         email_id,
        "item_name":        None,
        "order_number":     None,   # sale emails only
        "sale_price":       None,   # sale emails only (matched ask price)
        "listing_price":    None,   # listing emails only (ask price)
        "platform_fee":     None,   # sale emails: total of all fees
        "transaction_fee":  None,   # dollars (negative = deduction)
        "payment_proc_fee": None,
        "shipping_fee":     None,
        "total_payout":     None,
        "style_id":         None,
        "size":             None,
    }

    # ── Product title ──────────────────────────────────────────────────────
    if email_type == "sale":
        # Subject: "(Fw: ✅ )You Sold Your Item! PRODUCT NAME"
        m = re.search(r"you\s+sold\s+your\s+item[!]?\s+(.+)", subject, re.IGNORECASE)
        if m:
            result["item_name"] = m.group(1).strip()
    else:
        # 1. Subject: "(Fw: )Your Ask Is Live! PRODUCT NAME"
        m = re.search(r"your\s+ask\s+is\s+(?:now\s+)?live[!]?\s+(.+)",
                      subject, re.IGNORECASE)
        if m:
            result["item_name"] = m.group(1).strip()

    # 2. HTML: td with class containing "productName"
    if not result["item_name"]:
        for td in soup.find_all("td"):
            cls = " ".join(td.get("class") or [])
            if "productName" in cls:
                a = td.find("a")
                name = (a or td).get_text(strip=True)
                if name:
                    result["item_name"] = name
                    break

    # 3. Fallback: StockX product image alt text
    if not result["item_name"]:
        for img in soup.find_all("img", alt=True):
            alt = img["alt"].strip()
            src = img.get("src", "")
            if ("images.stockx.com" in src or "stockx.com/images" in src) and len(alt) > 8:
                result["item_name"] = alt
                break

    # ── Order number (sale emails) ─────────────────────────────────────────
    if email_type == "sale":
        m = re.search(r'Order\s+(?:Number|#)[:\s]+([A-Z0-9-]+)', text, re.IGNORECASE)
        if m:
            result["order_number"] = m.group(1).strip()

    # ── Ask / sale price ───────────────────────────────────────────────────
    # Primary: td whose id ends in "price" (the big price display cell)
    # Avoid "priceLeft" / "priceRight" sub-cells
    _price_val = None
    for td in soup.find_all("td", id=True):
        tid = td["id"].lower()
        if tid.endswith("price") and "left" not in tid and "right" not in tid:
            val = _dollars(td.get_text(strip=True))
            if val and val > 0:
                _price_val = val
                break

    # Fallback: "Your Ask:" / "Sale Price:" row in the fee breakdown text
    if not _price_val:
        m = re.search(r"(?:Your\s+Ask|Sale\s+Price):\s*\$([0-9,]+\.?[0-9]*)", text)
        if m:
            _price_val = float(m.group(1).replace(",", ""))

    if email_type == "sale":
        result["sale_price"] = _price_val
    else:
        result["listing_price"] = _price_val

    # ── Fee breakdown ──────────────────────────────────────────────────────
    # Walk text lines looking for labelled fee rows
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        m = re.match(r"(Transaction\s+Fee[^:]*)[:\s]+(-?\$[0-9,.]+)", line, re.IGNORECASE)
        if m:
            result["transaction_fee"] = _dollars(m.group(2))
            continue

        m = re.match(r"(Payment\s+Proc\.[^:]*)[:\s]+(-?\$[0-9,.]+)", line, re.IGNORECASE)
        if m:
            result["payment_proc_fee"] = _dollars(m.group(2))
            continue

        m = re.match(r"Shipping[:\s]+(-?\$[0-9,.]+)", line, re.IGNORECASE)
        if m:
            result["shipping_fee"] = _dollars(m.group(1))
            continue

        m = re.match(r"Total\s+Payout\s*:?\s*\$([0-9,.]+)", line, re.IGNORECASE)
        if m:
            result["total_payout"] = float(m.group(1).replace(",", ""))

    # ── Attributes ─────────────────────────────────────────────────────────
    m = re.search(r"Style\s+ID:\s*([A-Z0-9]+)", text)
    if m:
        result["style_id"] = m.group(1)

    m = re.search(r"Size:\s*(US\s+\S+)", text)
    if m:
        result["size"] = m.group(1).strip()

    # ── Platform fee total (sale emails) ──────────────────────────────────
    total_fees = sum(
        abs(v) for v in [
            result["transaction_fee"], result["payment_proc_fee"], result["shipping_fee"]
        ] if v is not None
    )
    if email_type == "sale":
        result["platform_fee"] = total_fees if total_fees else None

    if email_type == "sale":
        print(
            f"[Parser/StockX] SALE item={str(result['item_name'])[:50]} | "
            f"order={result['order_number']} | sale=${result['sale_price']} | "
            f"fees=${total_fees:.2f} | payout=${result['total_payout']}"
        )
        if not result["item_name"] and not result["sale_price"]:
            return None
    else:
        print(
            f"[Parser/StockX] LISTING item={str(result['item_name'])[:50]} | "
            f"ask=${result['listing_price']} | fees=${total_fees:.2f} | "
            f"payout=${result['total_payout']} | style={result['style_id']} | size={result['size']}"
        )
        if not result["item_name"] and not result["listing_price"]:
            return None

    return result
