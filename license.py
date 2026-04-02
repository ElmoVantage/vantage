"""
Vantage — License management via LemonSqueezy.

Flow:
  1. First launch: no license_cache.json → show activation dialog
  2. User enters license key → activate() called → instance_id saved
  3. Every launch: validate() called in background → updates cache
  4. Subscription lapses → validate() returns invalid → show renewal dialog
  5. Offline grace period: if API unreachable, allow use for GRACE_DAYS

LemonSqueezy API docs: https://docs.lemonsqueezy.com/api/licenses
"""

import json
import os
import sys
import uuid
import socket
import datetime
import requests

import config

# ── Constants ─────────────────────────────────────────────────────────────────

_API_BASE    = "https://api.lemonsqueezy.com/v1/licenses"
GRACE_DAYS   = 7          # allow offline use for this many days after last valid check
_CACHE_FILE  = os.path.join(config.BASE_DIR, "license_cache.json")

# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[License] Could not save cache: {e}")


def _machine_name() -> str:
    """Human-readable machine identifier used as the LemonSqueezy instance name."""
    try:
        return f"{socket.gethostname()}-{sys.platform}"
    except Exception:
        return f"machine-{uuid.uuid4().hex[:8]}"


# ── Public API ────────────────────────────────────────────────────────────────

def get_cached_key() -> str:
    """Returns the stored license key, or empty string if none."""
    return _load_cache().get("license_key", "")


def activate(key: str) -> dict:
    """
    Activate a license key on this machine.
    Returns {"ok": bool, "error": str | None, "instance_id": str | None}
    """
    key = key.strip()
    try:
        resp = requests.post(
            f"{_API_BASE}/activate",
            json={"license_key": key, "instance_name": _machine_name()},
            timeout=10,
        )
        data = resp.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"Network error: {e}", "instance_id": None}

    if resp.status_code == 200 and data.get("activated"):
        instance_id = data.get("instance", {}).get("id", "")
        _save_cache({
            "license_key":   key,
            "instance_id":   instance_id,
            "valid":         True,
            "last_checked":  _now(),
            "status":        data.get("license_key", {}).get("status", "active"),
        })
        return {"ok": True, "error": None, "instance_id": instance_id}

    # Already activated on this machine (duplicate activation attempt)
    err = data.get("error", "Activation failed")
    if "already activated" in str(err).lower() or resp.status_code == 400:
        # Try to validate instead — maybe this machine already has the instance
        cached = _load_cache()
        if cached.get("instance_id"):
            return validate(silent=False)
        # No instance_id cached — the key is activated on another machine
        return {"ok": False, "error": "This key is already active on another machine. "
                                      "Deactivate it there first, or contact support.", "instance_id": None}

    return {"ok": False, "error": err, "instance_id": None}


def validate(silent: bool = True) -> dict:
    """
    Validate the cached license against the API.
    Returns {"ok": bool, "error": str | None}
    silent=True suppresses print output (for background checks).
    """
    cache = _load_cache()
    key         = cache.get("license_key", "")
    instance_id = cache.get("instance_id", "")

    if not key or not instance_id:
        return {"ok": False, "error": "No license key found."}

    try:
        resp = requests.post(
            f"{_API_BASE}/validate",
            json={"license_key": key, "instance_id": instance_id},
            timeout=10,
        )
        data = resp.json()
    except requests.RequestException:
        # Offline — fall back to grace period
        return _grace_period_check(cache)

    if not silent:
        print(f"[License] Validate response {resp.status_code}: {data.get('valid')}")

    valid  = data.get("valid", False)
    status = data.get("license_key", {}).get("status", "unknown")

    cache.update({
        "valid":        valid,
        "last_checked": _now(),
        "status":       status,
    })
    _save_cache(cache)

    if valid and status == "active":
        return {"ok": True, "error": None}

    # Map LemonSqueezy status to a user-facing message
    messages = {
        "inactive":  "Your license is inactive. Please reactivate or contact support.",
        "expired":   "Your license has expired. Please renew your subscription.",
        "disabled":  "Your license has been disabled. Please contact support.",
    }
    return {"ok": False, "error": messages.get(status, f"License invalid (status: {status}).")}


def deactivate() -> bool:
    """Deactivate this machine's instance (e.g. before moving to a new machine)."""
    cache = _load_cache()
    key         = cache.get("license_key", "")
    instance_id = cache.get("instance_id", "")
    if not key or not instance_id:
        return False
    try:
        resp = requests.post(
            f"{_API_BASE}/deactivate",
            json={"license_key": key, "instance_id": instance_id},
            timeout=10,
        )
        if resp.status_code == 200:
            _save_cache({})
            return True
    except Exception:
        pass
    return False


def is_licensed() -> tuple:
    """
    Fast startup check. Returns (licensed: bool, message: str).
    Uses cached state; background revalidation updates the cache separately.
    """
    if config.DEV_MODE:
        return (True, "dev mode")

    cache = _load_cache()
    if not cache.get("license_key"):
        return (False, "no_key")

    # If last check was recent and cached as valid, trust it
    last = cache.get("last_checked", "")
    if last and cache.get("valid") and cache.get("status") == "active":
        try:
            age = datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last)
            if age.total_seconds() < 3600:   # checked within last hour → trust cache
                return (True, "cached")
        except ValueError:
            pass

    # Otherwise do a live check
    result = validate(silent=True)
    return (result["ok"], result.get("error") or "ok")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def _grace_period_check(cache: dict) -> dict:
    """Allow offline use if we successfully validated within GRACE_DAYS."""
    last = cache.get("last_checked", "")
    if not last or not cache.get("valid"):
        return {"ok": False, "error": "Cannot verify license — no internet connection "
                                      "and no recent valid check on record."}
    try:
        age = datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last)
        if age.days <= GRACE_DAYS:
            print(f"[License] Offline — using grace period ({age.days}/{GRACE_DAYS} days used)")
            return {"ok": True, "error": None}
        return {"ok": False, "error": f"Offline for {age.days} days — license verification required. "
                                      f"Please connect to the internet to continue."}
    except ValueError:
        return {"ok": False, "error": "License cache corrupted. Please re-enter your license key."}
