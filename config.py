"""
Application configuration.
  - settings.ini  — non-secret app settings (db path, backup options)
  - .env          — secrets only (bot token, webhook URL)
Environment variables always override settings.ini values.
Env var format for ini overrides: TRACKER_<SECTION>_<KEY>  (uppercase, underscores)
"""

import os
import sys
import configparser
from dotenv import load_dotenv

# When running as a PyInstaller bundle, __file__ is inside _internal/.
# User data (.env, tracker.db, sync_state.json) lives next to the exe.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

_ini = configparser.ConfigParser()
_ini.read(os.path.join(BASE_DIR, "settings.ini"))


def _cfg(section: str, key: str, fallback: str = "") -> str:
    env_key = f"TRACKER_{section.upper()}_{key.upper().replace('-', '_')}"
    return os.environ.get(env_key) or _ini.get(section, key, fallback=fallback)


def _abs(raw: str) -> str:
    return raw if os.path.isabs(raw) else os.path.join(BASE_DIR, raw)


# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = _abs(_cfg("database", "path", fallback="tracker.db"))

# ── Backup ────────────────────────────────────────────────────────────────────
BACKUP_ENABLED  = _cfg("backup", "enabled",  fallback="true").lower() == "true"
BACKUP_DIR      = _abs(_cfg("backup", "directory", fallback="backups"))
BACKUP_MAX_KEEP = int(_cfg("backup", "max_keep", fallback="10"))

# ── Expenses ──────────────────────────────────────────────────────────────────
AUTO_LOG_RECURRING              = _cfg("expenses", "auto_log_recurring",              fallback="true").lower() == "true"
NOTIFY_DISCORD_ON_RECURRING     = _cfg("expenses", "notify_discord_on_recurring",     fallback="true").lower() == "true"
LARGE_EXPENSE_THRESHOLD_CENTS   = int(_cfg("expenses", "large_expense_alert_threshold", fallback="10000"))
TAX_EXPORT_PATH                 = _abs(_cfg("expenses", "tax_export_path",            fallback="exports"))

# ── Notifications ─────────────────────────────────────────────────────────────
POST_MONTHLY_EXPENSE_SUMMARY = _cfg("notifications", "post_monthly_expense_summary", fallback="true").lower() == "true"

# ── Discord — secrets live in .env only, never in settings.ini ────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN", "")

# ── IMAP Accounts — secrets live in .env only ─────────────────────────────────
# Stored as IMAP_ACCOUNTS=<JSON array> in .env, e.g.:
#   [{"label": "Main Gmail", "user": "foo@gmail.com", "pass": "xxxx",
#     "host": "imap.gmail.com", "port": 993}, ...]
# host/port are optional — defaults to imap.gmail.com:993 if omitted.
# Falls back to legacy GMAIL_USER / GMAIL_PASS for single-account setups.
import json as _json

_imap_accounts_raw = os.environ.get("IMAP_ACCOUNTS", "")
if _imap_accounts_raw:
    try:
        IMAP_ACCOUNTS: list = _json.loads(_imap_accounts_raw)
    except (ValueError, TypeError):
        IMAP_ACCOUNTS = []
else:
    _legacy_user = os.environ.get("GMAIL_USER", "")
    _legacy_pass = os.environ.get("GMAIL_PASS", "")
    IMAP_ACCOUNTS = (
        [{"label": "Gmail", "user": _legacy_user, "pass": _legacy_pass,
          "host": "imap.gmail.com", "port": 993}]
        if _legacy_user and _legacy_pass else []
    )

# Default IMAP settings (used as fallback when an account omits host/port)
IMAP_HOST  = "imap.gmail.com"
IMAP_PORT  = 993

# Legacy single-account shims
GMAIL_USER = IMAP_ACCOUNTS[0]["user"] if IMAP_ACCOUNTS else os.environ.get("GMAIL_USER", "")
GMAIL_PASS = IMAP_ACCOUNTS[0]["pass"] if IMAP_ACCOUNTS else os.environ.get("GMAIL_PASS", "")

# ── License (LemonSqueezy) ────────────────────────────────────────────────────
# These are PUBLIC values — safe to bundle in the exe.
# Get them from your LemonSqueezy dashboard after creating the product.
LEMON_STORE_ID   = _cfg("license", "store_id",   fallback="")   # e.g. "12345"
LEMON_PRODUCT_ID = _cfg("license", "product_id", fallback="")   # e.g. "67890"

# License enforcement disabled during open beta — flip to False when ready to charge.
DEV_MODE = True

# ── Carrier tracking APIs ──────────────────────────────────────────────────────
UPS_CLIENT_ID       = ""
UPS_CLIENT_SECRET   = ""
FEDEX_CLIENT_ID     = "l7264fa0fd8205415fa755f8678a1bb133"
FEDEX_CLIENT_SECRET = "2e771b0722064ff99884fabf5fbe6052"
FEDEX_SANDBOX       = False
USPS_CLIENT_ID      = ""
USPS_CLIENT_SECRET  = ""
