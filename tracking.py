"""
Shipment tracking via direct carrier REST APIs.

Supported carriers: UPS, FedEx, USPS
All three use OAuth2 client-credentials flow with in-memory token caching.

Setup — add to .env:
  UPS_CLIENT_ID=...        UPS_CLIENT_SECRET=...
  FEDEX_CLIENT_ID=...      FEDEX_CLIENT_SECRET=...
  USPS_CLIENT_ID=...       USPS_CLIENT_SECRET=...

Registration:
  UPS:   https://developer.ups.com   (free, instant)
  FedEx: https://developer.fedex.com (free, instant)
  USPS:  https://developer.usps.com  (free, requires approval ~1 day)
"""

import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

import database as db
import webhooks
from config import (
    DB_PATH,
    UPS_CLIENT_ID, UPS_CLIENT_SECRET,
    FEDEX_CLIENT_ID, FEDEX_CLIENT_SECRET, FEDEX_SANDBOX,
    USPS_CLIENT_ID, USPS_CLIENT_SECRET,
)


# ── Carrier detection ─────────────────────────────────────────────────────────

_CARRIER_PATTERNS = [
    (re.compile(r'^1Z[A-Z0-9]{16}$', re.I),          "ups"),
    (re.compile(r'^(94|93|92|91|90|95)\d{18,20}$'),   "usps"),
    (re.compile(r'^\d{20,22}$'),                        "usps"),
    (re.compile(r'^\d{15}$'),                           "fedex"),
    (re.compile(r'^\d{12}$'),                           "fedex"),
    (re.compile(r'^\d{20}$'),                           "fedex"),
]


def detect_carrier(tracking_number: str) -> Optional[str]:
    t = tracking_number.strip().upper()
    for pattern, carrier in _CARRIER_PATTERNS:
        if pattern.match(t):
            return carrier
    return None


# ── Token cache ───────────────────────────────────────────────────────────────

_token_cache: Dict[str, Dict] = {}   # carrier -> {token, expires_at}


def _get_token(carrier: str) -> Optional[str]:
    cached = _token_cache.get(carrier)
    if cached and time.time() < cached["expires_at"] - 60:
        return cached["token"]

    if carrier == "ups":
        return _fetch_ups_token()
    if carrier == "fedex":
        return _fetch_fedex_token()
    if carrier == "usps":
        return _fetch_usps_token()
    return None


def _store_token(carrier: str, token: str, expires_in: int) -> None:
    _token_cache[carrier] = {"token": token, "expires_at": time.time() + expires_in}


# ── UPS ───────────────────────────────────────────────────────────────────────

_UPS_TOKEN_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
_UPS_TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details/{}"


def _fetch_ups_token() -> Optional[str]:
    if not UPS_CLIENT_ID or not UPS_CLIENT_SECRET:
        return None
    try:
        resp = requests.post(
            _UPS_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(UPS_CLIENT_ID, UPS_CLIENT_SECRET),
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            token = body["access_token"]
            _store_token("ups", token, int(body.get("expires_in", 3600)))
            return token
        print(f"[Tracking/UPS] token error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[Tracking/UPS] token fetch error: {e}")
    return None


def _track_ups(tracking_number: str) -> Optional[Dict]:
    token = _get_token("ups")
    if not token:
        return None
    try:
        resp = requests.get(
            _UPS_TRACK_URL.format(tracking_number),
            headers={"Authorization": f"Bearer {token}", "transId": "tracker", "transactionSrc": "Vantage"},
            params={"locale": "en_US", "returnMilestones": "false"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Tracking/UPS] {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        shipment = data.get("trackResponse", {}).get("shipment", [{}])[0]
        package  = shipment.get("package", [{}])[0]
        activity = package.get("activity", [])
        latest   = activity[0] if activity else {}

        status_desc = latest.get("status", {}).get("description", "")
        status_type = latest.get("status", {}).get("type", "")
        status = status_desc or status_type

        # Estimated delivery
        est_raw = package.get("deliveryTime", {}).get("endTime") or \
                  shipment.get("deliveryTime", {}).get("endTime")
        est_date = None
        if est_raw:
            try:
                est_date = datetime.strptime(est_raw[:8], "%Y%m%d").strftime("%Y-%m-%d")
            except Exception:
                pass

        # Try scheduledDelivery date
        if not est_date:
            sched = package.get("scheduledDelivery", {})
            date_str = sched.get("date")
            if date_str:
                try:
                    est_date = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
                except Exception:
                    pass

        return {"carrier": "UPS", "tracking_status": status, "estimated_delivery": est_date}
    except Exception as e:
        print(f"[Tracking/UPS] track error: {e}")
        return None


# ── FedEx ─────────────────────────────────────────────────────────────────────

_FEDEX_BASE      = "https://apis-sandbox.fedex.com" if FEDEX_SANDBOX else "https://apis.fedex.com"
_FEDEX_TOKEN_URL = f"{_FEDEX_BASE}/oauth/token"
_FEDEX_TRACK_URL = f"{_FEDEX_BASE}/track/v1/trackingnumbers"


def _fetch_fedex_token() -> Optional[str]:
    if not FEDEX_CLIENT_ID or not FEDEX_CLIENT_SECRET:
        return None
    try:
        resp = requests.post(
            _FEDEX_TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     FEDEX_CLIENT_ID,
                "client_secret": FEDEX_CLIENT_SECRET,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            token = body["access_token"]
            _store_token("fedex", token, int(body.get("expires_in", 3600)))
            return token
        print(f"[Tracking/FedEx] token error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[Tracking/FedEx] token fetch error: {e}")
    return None


def _track_fedex(tracking_number: str) -> Optional[Dict]:
    token = _get_token("fedex")
    if not token:
        return None
    try:
        payload = {
            "includeDetailedScans": True,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}],
        }
        resp = requests.post(
            _FEDEX_TRACK_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Tracking/FedEx] {resp.status_code}: {resp.text[:200]}")
            return None

        data    = resp.json()
        results = data.get("output", {}).get("completeTrackResults", [])
        if not results:
            return None

        pkg      = results[0].get("trackResults", [{}])[0]
        status   = pkg.get("latestStatusDetail", {}).get("description", "")
        events   = pkg.get("dateAndTimes", [])

        est_date = None
        for ev in events:
            if ev.get("type") in ("ESTIMATED_DELIVERY", "ACTUAL_DELIVERY"):
                raw = ev.get("dateTime", "")
                if raw:
                    try:
                        est_date = raw[:10]
                    except Exception:
                        pass
                    break

        return {"carrier": "FedEx", "tracking_status": status, "estimated_delivery": est_date}
    except Exception as e:
        print(f"[Tracking/FedEx] track error: {e}")
        return None


# ── USPS ──────────────────────────────────────────────────────────────────────

_USPS_TOKEN_URL = "https://apis.usps.com/oauth2/v3/token"
_USPS_TRACK_URL = "https://apis.usps.com/tracking/v3/tracking/{}"


def _fetch_usps_token() -> Optional[str]:
    if not USPS_CLIENT_ID or not USPS_CLIENT_SECRET:
        return None
    try:
        resp = requests.post(
            _USPS_TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     USPS_CLIENT_ID,
                "client_secret": USPS_CLIENT_SECRET,
                "scope":         "tracking",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            body = resp.json()
            token = body["access_token"]
            _store_token("usps", token, int(body.get("expires_in", 3600)))
            return token
        print(f"[Tracking/USPS] token error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[Tracking/USPS] token fetch error: {e}")
    return None


def _track_usps(tracking_number: str) -> Optional[Dict]:
    token = _get_token("usps")
    if not token:
        return None
    try:
        resp = requests.get(
            _USPS_TRACK_URL.format(tracking_number),
            headers={"Authorization": f"Bearer {token}"},
            params={"expand": "DETAIL"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Tracking/USPS] {resp.status_code}: {resp.text[:200]}")
            return None

        data   = resp.json()
        events = data.get("trackSummary", {})
        status = events.get("eventDescription", "") or data.get("statusSummary", "")

        est_date = None
        est_raw  = data.get("expectedDeliveryDate") or data.get("predictedDeliveryDate")
        if est_raw:
            try:
                est_date = est_raw[:10]
            except Exception:
                pass

        return {"carrier": "USPS", "tracking_status": status, "estimated_delivery": est_date}
    except Exception as e:
        print(f"[Tracking/USPS] track error: {e}")
        return None


# ── Public interface ──────────────────────────────────────────────────────────

def get_tracking(tracking_number: str, carrier: Optional[str] = None) -> Optional[Dict]:
    """
    Fetch latest tracking info for a tracking number.
    carrier overrides auto-detection ('ups', 'fedex', 'usps').
    Returns dict with carrier, tracking_status, estimated_delivery — or None.
    """
    c = (carrier or detect_carrier(tracking_number) or "").lower()
    if c == "ups":
        return _track_ups(tracking_number)
    if c == "fedex":
        return _track_fedex(tracking_number)
    if c == "usps":
        return _track_usps(tracking_number)
    print(f"[Tracking] Unknown carrier for {tracking_number} (detected: '{c}')")
    return None


def is_configured() -> bool:
    """Return True if at least one carrier API is configured."""
    return bool(
        (UPS_CLIENT_ID and UPS_CLIENT_SECRET) or
        (FEDEX_CLIENT_ID and FEDEX_CLIENT_SECRET) or
        (USPS_CLIENT_ID and USPS_CLIENT_SECRET)
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def _is_delivered(tracking_status: Optional[str]) -> bool:
    """Return True if the carrier status string indicates the package was delivered.
    Must match 'delivered' (past tense) — not 'out for delivery', 'delivery attempted', etc.
    """
    if not tracking_status:
        return False
    return "delivered" in tracking_status.lower()


def _update_order_tracking(order_id: int, info: Dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    delivered = _is_delivered(info.get("tracking_status"))
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """UPDATE inbound_orders
               SET tracking_carrier    = ?,
                   tracking_status     = ?,
                   estimated_delivery  = ?,
                   tracking_checked_at = ?,
                   status              = CASE
                       WHEN ? AND status NOT IN ('received', 'returned') THEN 'delivered'
                       ELSE status
                   END,
                   updated_at          = datetime('now')
               WHERE id = ?""",
            (
                info.get("carrier"),
                info.get("tracking_status"),
                info.get("estimated_delivery"),
                now,
                1 if delivered else 0,
                order_id,
            ),
        )
        con.commit()


def refresh_order(order_id: int, tracking_number: str,
                  carrier: Optional[str] = None,
                  item_name: Optional[str] = None) -> Optional[Dict]:
    """Refresh tracking for one order. Updates DB and returns info dict or None."""
    info = get_tracking(tracking_number, carrier)
    if not info:
        return None

    # Read current status before overwriting
    order      = db.get_inbound_order_by_id(order_id)
    old_status = order.get("tracking_status") if order else None
    new_status = info.get("tracking_status") or ""

    _update_order_tracking(order_id, info)
    print(f"[Tracking] order {order_id} | {tracking_number} | "
          f"{info.get('carrier')} | {new_status} | "
          f"eta={info.get('estimated_delivery')}")

    # Store old_status on the info dict so callers can detect changes
    info["_old_status"] = old_status

    return info


def _addr_short(raw: str) -> str:
    """Return 'Street, ZIP' from a raw delivery address (mirrors GUI helper)."""
    if not raw:
        return ""
    lines = [l.strip() for l in re.split(r"[\n,]", raw.strip()) if l.strip()]
    street = next((l for l in lines if re.match(r"^\d", l)), None)
    zips   = re.findall(r"\b(\d{5}(?:-\d{4})?)\b", raw)
    if street and zips:
        return f"{street}, {zips[-1]}"
    return street or (lines[1] if len(lines) > 1 else raw)


def _norm_addr(addr: str) -> str:
    """Canonical key for address deduplication (same logic as GUI _normalize_addr_key)."""
    s = addr.lower()
    s = re.sub(r"[.,]", "", s)
    for pat, rep in [
        (r"\bsouth\b", "s"), (r"\bnorth\b", "n"), (r"\beast\b", "e"), (r"\bwest\b", "w"),
        (r"\bstreet\b", "st"), (r"\bavenue\b", "ave"), (r"\bdrive\b", "dr"),
        (r"\broad\b", "rd"), (r"\bboulevard\b", "blvd"), (r"\blane\b", "ln"),
        (r"\bcourt\b", "ct"), (r"\bplace\b", "pl"),
    ]:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def refresh_all_shipped() -> Dict[str, int]:
    """
    Refresh every inbound order that is 'shipped' with a tracking number.
    Skips orders whose carrier has no credentials configured.
    Fires a summary Discord webhook when the poll completes.
    Returns {'refreshed': int, 'failed': int, 'skipped': int}.
    """
    if not is_configured():
        print("[Tracking] No carrier credentials configured — see .env")
        return {"refreshed": 0, "failed": 0, "skipped": 0}

    orders  = db.get_inbound_orders()
    shipped = [o for o in orders if o["status"] == "shipped" and o.get("tracking_number")]

    counts = {"refreshed": 0, "failed": 0, "skipped": 0}
    # Collect all status changes during this poll
    _delivered_by_addr: Dict[str, dict] = {}   # newly delivered, grouped by address
    _status_changes: list = []                  # all other status transitions

    for o in shipped:
        tn      = o["tracking_number"].strip()
        carrier = (o.get("tracking_carrier") or detect_carrier(tn) or "").lower()

        # Skip if this carrier's credentials aren't set up
        if carrier == "ups"   and not (UPS_CLIENT_ID and UPS_CLIENT_SECRET):
            counts["skipped"] += 1; continue
        if carrier == "fedex" and not (FEDEX_CLIENT_ID and FEDEX_CLIENT_SECRET):
            counts["skipped"] += 1; continue
        if carrier == "usps"  and not (USPS_CLIENT_ID and USPS_CLIENT_SECRET):
            counts["skipped"] += 1; continue
        if not carrier:
            counts["skipped"] += 1; continue

        info = refresh_order(o["order_id"], tn, carrier, item_name=o.get("item_name"))
        if info:
            counts["refreshed"] += 1
            old_tracking = (info.get("_old_status") or "").lower()
            new_tracking = (info.get("tracking_status") or "").lower()

            # Skip if no status change
            if not new_tracking or new_tracking == old_tracking:
                continue

            name = o.get("item_name") or "Unknown"

            if _is_delivered(new_tracking) and not _is_delivered(old_tracking):
                # Group newly-delivered by address
                raw_addr  = o.get("delivery_address") or ""
                disp_addr = _addr_short(raw_addr) or raw_addr or "Unknown address"
                norm_key  = _norm_addr(disp_addr)
                if norm_key not in _delivered_by_addr:
                    _delivered_by_addr[norm_key] = {
                        "display_addr": disp_addr,
                        "pkg_count":    0,
                        "item_qty":     {},
                    }
                grp = _delivered_by_addr[norm_key]
                grp["pkg_count"] += 1
                grp["item_qty"][name] = grp["item_qty"].get(name, 0) + (o.get("quantity") or 1)
            else:
                # Other status changes (in transit, out for delivery, etc.)
                _status_changes.append({
                    "item_name":  name,
                    "old_status": info.get("_old_status") or "Unknown",
                    "new_status": info.get("tracking_status") or "",
                })
        else:
            counts["failed"] += 1

    # Build summary and fire one batched webhook
    newly_delivered = [
        {
            "display_addr": grp["display_addr"],
            "pkg_count":    grp["pkg_count"],
            "items":        list(grp["item_qty"].items()),
        }
        for grp in _delivered_by_addr.values()
    ]
    webhooks.notify_tracking_poll_summary(
        counts["refreshed"], counts["failed"], newly_delivered, _status_changes,
    )

    return counts
