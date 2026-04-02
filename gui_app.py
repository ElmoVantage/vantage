"""
Vantage Tracker — Desktop GUI
PyQt6 tabbed interface with live SQLite monitoring.
"""

import csv
import json
import re
import sys
import os
import subprocess
from datetime import datetime

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import (
    Qt, QTimer, QFileSystemWatcher, QSortFilterProxyModel, QAbstractTableModel,
    QModelIndex, QVariant, pyqtSignal, QThread, QDate, QStringListModel,
)
from PyQt6.QtGui import QColor, QAction, QFont, QCursor, QClipboard, QTextCharFormat, QDesktopServices, QIcon
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTableView, QPushButton, QLabel, QLineEdit, QComboBox, QDialog, QFormLayout,
    QDialogButtonBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit,
    QMessageBox, QMenu, QStatusBar, QSizePolicy, QCheckBox, QHeaderView,
    QAbstractItemView, QFrame, QGroupBox, QInputDialog, QFileDialog, QScrollArea,
    QTabBar, QStackedWidget, QCalendarWidget, QSplitter, QProgressBar, QCompleter,
    QListWidget,
)
from PyQt6.QtCore import QDate

import database as db


def _fmt_date(date_str: str) -> str:
    """Convert YYYY-MM-DD → MM-DD-YY for display. Returns the original string on failure."""
    if not date_str:
        return date_str
    try:
        dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        return dt.strftime("%m-%d-%y")
    except ValueError:
        return date_str


def _write_csv(parent, headers: list, rows: list, default_name: str) -> None:
    """Open a Save dialog and write rows to a CSV file."""
    from PyQt6.QtWidgets import QFileDialog, QMessageBox
    path, _ = QFileDialog.getSaveFileName(
        parent, "Export CSV", default_name, "CSV Files (*.csv)"
    )
    if not path:
        return
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        QMessageBox.information(parent, "Export Complete", f"Saved {len(rows)} rows to:\n{path}")
    except Exception as exc:
        QMessageBox.warning(parent, "Export Failed", str(exc))
import webhooks
import license as _license
from config import TAX_EXPORT_PATH

try:
    import matplotlib
    matplotlib.use("QtAgg")
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False

import theme

# ── Constants ────────────────────────────────────────────────────────────────

INBOUND_STATUSES = ["ordered", "shipped", "delivered", "cancelled", "returned"]
SALE_STATUSES    = ["listed",  "sold",    "shipped",   "delivered", "cancelled", "refunded"]
CONDITIONS       = ["new", "used", "open box", "like new", "for parts"]
PLATFORMS        = ["ebay", "stockx", "direct", "local"]

COL_GREEN = theme.QCOLOR_GREEN
COL_RED   = theme.QCOLOR_RED
COL_GOLD  = theme.QCOLOR_GOLD

APP_STYLE = theme.APP_STYLE


# ── Date picker with auto-sized popup calendar ────────────────────────────────

class DateEdit(QDateEdit):
    """QDateEdit pre-configured with a calendar popup that shows all days."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setDisplayFormat("MM/dd/yyyy")
        cal = self.calendarWidget()
        cal.setMinimumSize(320, 240)
        cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)


# ── Generic table model ───────────────────────────────────────────────────────

class RecordTableModel(QAbstractTableModel):
    def __init__(self, headers: List[str], data: List[List[Any]], parent=None):
        super().__init__(parent)
        self._headers = headers
        self._data = data

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return QVariant()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return QVariant()
        value = self._data[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(value) if value is not None else ""
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(index.row(), index.column(), value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if isinstance(value, (int, float)):
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.UserRole:
            return self._data[index.row()]
        return QVariant()

    def _foreground(self, row, col, value):
        return QVariant()

    def raw_row(self, row: int) -> List[Any]:
        return self._data[row]


def _tracking_url(tracking_number: str, carrier: str = "") -> str:
    """Return the carrier's web tracking URL for a tracking number."""
    c = carrier.lower()
    t = tracking_number.strip()
    if c == "ups" or t.upper().startswith("1Z"):
        return f"https://www.ups.com/track?tracknum={t}"
    if c == "usps" or (t.isdigit() and len(t) >= 20):
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={t}"
    if c == "fedex":
        return f"https://www.fedex.com/fedextrack/?trknbr={t}"
    # Fallback — let FedEx handle it (most common for this use case)
    return f"https://www.fedex.com/fedextrack/?trknbr={t}"


class ShipmentsTableModel(RecordTableModel):
    """Shipments table — col 2 is Tracking # and renders as a clickable link."""
    TRACKING_COL = 2

    def _foreground(self, row, col, value):
        if col == self.TRACKING_COL and value:
            return QColor(theme.BLUE)
        return QVariant()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.FontRole and index.column() == self.TRACKING_COL:
            f = QFont()
            f.setUnderline(True)
            return f
        return super().data(index, role)


class SalesTableModel(RecordTableModel):
    PROFIT_COL   = 7   # index in SALE_HEADERS (after Qty column)
    COST_COL     = 6
    TRACKING_COL = 10

    def __init__(self, headers, data, raw_records=None, parent=None):
        super().__init__(headers, data, parent)
        self._raw_records = raw_records or []

    def _is_unlinked(self, row: int) -> bool:
        if row < len(self._raw_records):
            return not self._raw_records[row].get("inventory_id")
        return False

    def _foreground(self, row, col, value):
        if col == self.TRACKING_COL and value:
            return QColor(theme.BLUE)
        if self._is_unlinked(row) and col == self.COST_COL:
            return COL_GOLD
        if col == self.PROFIT_COL:
            raw = self._data[row][col]
            if isinstance(raw, str) and "-" in raw:
                return COL_RED
            return COL_GREEN
        return QVariant()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.FontRole and index.column() == self.TRACKING_COL:
            val = super().data(index, Qt.ItemDataRole.DisplayRole)
            if val:
                f = QFont()
                f.setUnderline(True)
                return f
        if role == Qt.ItemDataRole.ToolTipRole and self._is_unlinked(index.row()):
            if index.column() == self.COST_COL:
                return "No inventory item linked — right-click to link"
        return super().data(index, role)


# ── Shared filter bar widget ──────────────────────────────────────────────────

class FilterBar(QWidget):
    changed = pyqtSignal()

    def __init__(self, filters: List[Dict], parent=None):
        """
        filters: list of dicts with keys:
          label, type ('text'|'combo'|'check'), choices (for combo), key
        """
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._widgets: Dict[str, Any] = {}

        for f in filters:
            lbl = QLabel(f["label"] + ":")
            lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            layout.addWidget(lbl)

            if f["type"] == "text":
                w = QLineEdit()
                w.setPlaceholderText("All")
                w.setFixedWidth(140)
                w.textChanged.connect(self.changed)
            elif f["type"] == "combo":
                w = QComboBox()
                w.addItem("All", "")
                for c in f.get("choices", []):
                    w.addItem(c.title(), c)
                w.setFixedWidth(120)
                w.currentIndexChanged.connect(self.changed)
            elif f["type"] == "check":
                w = QCheckBox(f.get("label2", ""))
                if f.get("default", False):
                    w.setChecked(True)
                w.stateChanged.connect(self.changed)
            else:
                continue

            layout.addWidget(w)
            self._widgets[f["key"]] = (f["type"], w)

        layout.addStretch()
        btn_clear = QPushButton("Clear Filters")
        btn_clear.setFixedWidth(100)
        btn_clear.setStyleSheet(f"""
            QPushButton {{
                background: {theme.BG_ELEVATED}; color: {theme.TEXT_SECONDARY};
                border: 1px solid {theme.BORDER}; border-radius: 6px;
                padding: 5px 12px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {theme.BG_CARD}; color: {theme.TEXT_PRIMARY}; }}
        """)
        btn_clear.clicked.connect(self._clear)
        layout.addWidget(btn_clear)

    def _clear(self):
        for key, (typ, w) in self._widgets.items():
            if typ == "text":
                w.setText("")
            elif typ == "combo":
                w.setCurrentIndex(0)
            elif typ == "check":
                w.setChecked(False)

    def values(self) -> Dict[str, Any]:
        result = {}
        for key, (typ, w) in self._widgets.items():
            if typ == "text":
                result[key] = w.text().strip()
            elif typ == "combo":
                result[key] = w.currentData() or ""
            elif typ == "check":
                result[key] = w.isChecked()
        return result


# ── Dialogs ───────────────────────────────────────────────────────────────────

def _address_number_key(address: str) -> str:
    """
    Canonical merge key for jigged addresses.
    Uses only the house/building number (first digit sequence) and the ZIP code
    (last 5-digit number). Room, floor, suite, unit, apt numbers are ignored
    because those are the parts that get jigged.
    e.g. "218 E Coler St Room 15, 49203" and "218 E Coler St Floor 48, 49203"
    both map to "218,49203" and are merged onto the same card.
    """
    nums = re.findall(r'\d+', address)
    if not nums:
        return address.strip().lower()
    house = nums[0]
    zips = re.findall(r'\b\d{5}\b', address)
    zip_code = zips[-1] if zips else nums[-1]
    return f"{house},{zip_code}"


def _format_address_short(address: str) -> str:
    """Return 'Street line, ZIP' from a full address string."""
    if not address:
        return ""
    lines = [l.strip() for l in re.split(r"[\n,]", address.strip()) if l.strip()]
    # Street line = first line that starts with a digit (house number)
    street = next((l for l in lines if re.match(r"^\d", l)), None)
    zips = re.findall(r"\b(\d{5}(?:-\d{4})?)\b", address)
    if street and zips:
        return f"{street}, {zips[-1]}"
    if street:
        return street
    # Fallback: second line if present (skip name on first line), else raw
    return lines[1] if len(lines) > 1 else address


def _normalize_addr_key(addr: str) -> str:
    """Return a canonical key for address deduplication.

    Lowercases, strips punctuation, expands directional and street-type
    abbreviations, then collapses whitespace.  The house number and ZIP are
    the most stable parts, so even 'South Jackson Street' vs 's jackson st'
    hash to the same key.
    """
    s = addr.lower()
    s = re.sub(r"[.,]", "", s)
    _abbr = [
        (r"\bsouth\b", "s"), (r"\bnorth\b", "n"),
        (r"\beast\b",  "e"), (r"\bwest\b",  "w"),
        (r"\bstreet\b", "st"), (r"\bavenue\b", "ave"),
        (r"\bdrive\b",  "dr"), (r"\broad\b",   "rd"),
        (r"\bboulevard\b", "blvd"), (r"\blane\b", "ln"),
        (r"\bcourt\b",  "ct"),  (r"\bplace\b", "pl"),
    ]
    for pat, rep in _abbr:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def _address_line1(address: str) -> str:
    """Return just the street line (line 1) from a full address string."""
    if not address:
        return "Unknown Address"
    lines = [l.strip() for l in re.split(r"[\n,]", address.strip()) if l.strip()]
    # Prefer the first line that starts with a digit (house number + street)
    street = next((l for l in lines if re.match(r"^\d", l)), None)
    if street:
        return street
    return lines[0] if lines else address


def _make_label(text: str, required: bool = False) -> QLabel:
    lbl = QLabel(f"{'* ' if required else ''}{text}:")
    lbl.setFixedWidth(140)
    return lbl


class LicenseDialog(QDialog):
    """
    Modal dialog shown when no valid license is found.
    Blocks the app from opening until a valid key is entered.
    """
    # !! Replace with your actual LemonSqueezy checkout URL !!
    _PURCHASE_URL = "https://elmovantage.lemonsqueezy.com"

    def __init__(self, message: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vantage — Activate License")
        self.setFixedWidth(460)
        self.setModal(True)
        self.setStyleSheet(APP_STYLE)
        self._build_ui(message)

    def _build_ui(self, message: str):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(16)

        # Header
        title = QLabel("Vantage Tracker")
        title.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {theme.BLUE};")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        sub = QLabel("Enter your license key to activate")
        sub.setStyleSheet(f"font-size: 12px; color: {theme.TEXT_SECONDARY};")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)

        lay.addSpacing(8)

        # Error / info message
        self._msg_lbl = QLabel(message)
        self._msg_lbl.setWordWrap(True)
        self._msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._msg_lbl.setStyleSheet(f"font-size: 11px; color: {theme.RED};")
        self._msg_lbl.setVisible(bool(message))
        lay.addWidget(self._msg_lbl)

        # Key input
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX")
        self._key_input.setFixedHeight(38)
        self._key_input.setStyleSheet(
            f"background: {theme.BG_ELEVATED}; color: {theme.TEXT_PRIMARY};"
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 0 10px;"
            f"font-size: 13px; letter-spacing: 1px;"
        )
        lay.addWidget(self._key_input)

        # Pre-fill if there's a cached key
        cached = _license.get_cached_key()
        if cached:
            self._key_input.setText(cached)

        # Activate button
        self._btn = QPushButton("Activate")
        self._btn.setFixedHeight(38)
        self._btn.setStyleSheet(
            f"background: {theme.BLUE}; color: #000; font-weight: 700;"
            f"border-radius: 6px; font-size: 13px;"
        )
        self._btn.clicked.connect(self._activate)
        self._key_input.returnPressed.connect(self._activate)
        lay.addWidget(self._btn)

        # Purchase link
        buy_lbl = QLabel(
            f"Don't have a license? "
            f"<a href='{self._PURCHASE_URL}' style='color:{theme.BLUE};'>Subscribe here</a>"
        )
        buy_lbl.setOpenExternalLinks(True)
        buy_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        buy_lbl.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_SECONDARY};")
        lay.addWidget(buy_lbl)

        # Transfer link
        transfer_lbl = QLabel(
            "<a href='transfer' style='color:#666;'>Moving to a new machine? Deactivate this key</a>"
        )
        transfer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        transfer_lbl.setStyleSheet("font-size: 10px;")
        transfer_lbl.linkActivated.connect(self._deactivate)
        lay.addWidget(transfer_lbl)

    def _activate(self):
        key = self._key_input.text().strip()
        if not key:
            self._show_error("Please enter your license key.")
            return
        self._btn.setEnabled(False)
        self._btn.setText("Activating…")
        QApplication.processEvents()

        result = _license.activate(key)
        self._btn.setEnabled(True)
        self._btn.setText("Activate")

        if result["ok"]:
            self.accept()
        else:
            self._show_error(result.get("error") or "Activation failed. Check your key and try again.")

    def _deactivate(self):
        if QMessageBox.question(
            self, "Deactivate License",
            "This will remove the license from this machine so you can activate it elsewhere.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            ok = _license.deactivate()
            self._show_error(
                "Deactivated. Enter the key again to re-activate on this machine."
                if ok else "Deactivation failed — check your internet connection."
            )

    def _show_error(self, msg: str):
        self._msg_lbl.setText(msg)
        self._msg_lbl.setVisible(True)


class InboundDialog(QDialog):
    """Add or edit an inbound order+item record."""

    def __init__(self, parent=None, row: Optional[Dict] = None):
        super().__init__(parent)
        self.row = row
        self.setWindowTitle("Edit Inbound Order" if row else "Add Inbound Order")
        self.setMinimumWidth(480)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        r = self.row or {}

        self.item_name    = QLineEdit(r.get("item_name", ""))
        self.sku          = QLineEdit(r.get("sku", "") or "")
        self.retailer     = QLineEdit(r.get("retailer", ""))
        self.order_number = QLineEdit(r.get("order_number", ""))

        self.order_date = DateEdit()
        if r.get("order_date"):
            try:
                self.order_date.setDate(QDate.fromString(r["order_date"], "yyyy-MM-dd"))
            except Exception:
                self.order_date.setDate(QDate.currentDate())
        else:
            self.order_date.setDate(QDate.currentDate())

        self.cost = QDoubleSpinBox()
        self.cost.setRange(0, 99999.99)
        self.cost.setDecimals(2)
        self.cost.setPrefix("$")
        if r.get("cost_cents") is not None:
            self.cost.setValue(db.cents_to_dollars(r["cost_cents"]))

        self.quantity = QSpinBox()
        self.quantity.setRange(1, 9999)
        self.quantity.setValue(r.get("quantity", 1))

        self.status = QComboBox()
        for s in INBOUND_STATUSES:
            self.status.addItem(s.title(), s)
        if r.get("status"):
            idx = self.status.findData(r["status"])
            if idx >= 0:
                self.status.setCurrentIndex(idx)

        self.tracking  = QLineEdit(r.get("tracking_number", "") or "")
        self.address   = QLineEdit(r.get("delivery_address", "") or "")
        self.notes     = QTextEdit(r.get("notes", "") or "")
        self.notes.setFixedHeight(60)

        form.addRow(_make_label("Item Name", True),  self.item_name)
        form.addRow(_make_label("SKU"),               self.sku)
        form.addRow(_make_label("Retailer", True),    self.retailer)
        form.addRow(_make_label("Order Number", True),self.order_number)
        form.addRow(_make_label("Order Date", True),  self.order_date)
        form.addRow(_make_label("Purchase Price", True), self.cost)
        form.addRow(_make_label("Quantity", True),    self.quantity)
        form.addRow(_make_label("Status"),             self.status)
        form.addRow(_make_label("Tracking"),           self.tracking)
        form.addRow(_make_label("Delivery Address"),   self.address)
        form.addRow(_make_label("Notes"),              self.notes)

        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        if self.row:
            del_btn = btns.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
            del_btn.setObjectName("btnDelete")
            del_btn.clicked.connect(self._delete)

        layout.addWidget(btns)

    def _validate(self) -> bool:
        for name, widget in [("Item Name", self.item_name), ("Retailer", self.retailer),
                               ("Order Number", self.order_number)]:
            if not widget.text().strip():
                QMessageBox.warning(self, "Validation", f"{name} is required.")
                return False
        return True

    def _save(self):
        if not self._validate():
            return

        if self.row:
            order_id = self.row["order_id"]
            item_id  = self.row["item_id"]
            db.update_inbound_order(order_id, "retailer",         self.retailer.text().strip())
            db.update_inbound_order(order_id, "order_number",     self.order_number.text().strip())
            db.update_inbound_order(order_id, "order_date",       self.order_date.date().toString("yyyy-MM-dd"))
            db.update_inbound_order(order_id, "status",           self.status.currentData())
            db.update_inbound_order(order_id, "tracking_number",  self.tracking.text().strip() or None)
            db.update_inbound_order(order_id, "delivery_address", self.address.text().strip() or None)
            db.update_inbound_order(order_id, "notes",            self.notes.toPlainText().strip() or None)
            db.update_inbound_item(item_id, "item_name", self.item_name.text().strip())
            db.update_inbound_item(item_id, "sku",       self.sku.text().strip() or None)
            db.update_inbound_item(item_id, "cost_cents",db.dollars_to_cents(self.cost.value()))
            db.update_inbound_item(item_id, "quantity",  self.quantity.value())

            if self.status.currentData() == "delivered":
                new_ids = db.deliver_order_to_inventory(order_id)
                for inv_id in new_ids:
                    inv = db.get_inventory_by_id(inv_id)
                    if inv:
                        webhooks.notify_inventory_added(inv_id, inv["item_name"], inv["quantity"], inv["cost_basis_cents"])
        else:
            order_id, item_id = db.add_inbound_order(
                order_number     = self.order_number.text().strip(),
                retailer         = self.retailer.text().strip(),
                order_date       = self.order_date.date().toString("yyyy-MM-dd"),
                item_name        = self.item_name.text().strip(),
                cost_cents       = db.dollars_to_cents(self.cost.value()),
                quantity         = self.quantity.value(),
                sku              = self.sku.text().strip() or None,
                tracking_number  = self.tracking.text().strip() or None,
                delivery_address = self.address.text().strip() or None,
                status           = self.status.currentData(),
            )
            webhooks.notify_new_order(order_id, self.order_number.text().strip(),
                                      self.retailer.text().strip(), self.item_name.text().strip(),
                                      db.dollars_to_cents(self.cost.value()), self.quantity.value())

        self.accept()

    def _delete(self):
        if QMessageBox.question(self, "Confirm Delete",
                                "Soft-delete this order item?\n(Data is retained but hidden.)",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_inbound_item(self.row["item_id"])
            self.accept()


def _ask_category(title: str, label: str, suggestions: List[str], parent=None) -> Optional[str]:
    """Input dialog with autocomplete for category fields."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(320)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(10)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.addWidget(QLabel(label))
    edit = QLineEdit()
    completer = QCompleter(suggestions, edit)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    edit.setCompleter(completer)
    lay.addWidget(edit)
    btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    lay.addWidget(btns)
    if dlg.exec() == QDialog.DialogCode.Accepted and edit.text().strip():
        return edit.text().strip()
    return None


class InventoryDialog(QDialog):
    def __init__(self, parent=None, row: Optional[Dict] = None):
        super().__init__(parent)
        self.row = row
        self.setWindowTitle("Edit Inventory" if row else "Add Inventory")
        self.setMinimumWidth(480)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        r = self.row or {}

        # Pull existing names/categories for autocomplete
        _inv = db.get_inventory()
        _names = sorted({i["item_name"] for i in _inv if i.get("item_name")})
        _cats  = sorted({i["category"]  for i in _inv if i.get("category")})
        if "General" not in _cats:
            _cats.insert(0, "General")

        self.item_name   = QLineEdit(r.get("item_name", ""))
        _name_completer  = QCompleter(_names, self.item_name)
        _name_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        _name_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        _name_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.item_name.setCompleter(_name_completer)

        self.sku         = QLineEdit(r.get("sku", "") or "")
        self.category    = QLineEdit(r.get("category", "General"))
        _cat_completer   = QCompleter(_cats, self.category)
        _cat_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        _cat_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        _cat_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.category.setCompleter(_cat_completer)

        self.size_variant= QLineEdit(r.get("size_variant", "") or "")

        self.condition = QComboBox()
        for c in CONDITIONS:
            self.condition.addItem(c.title(), c)
        if r.get("condition"):
            idx = self.condition.findData(r["condition"])
            if idx >= 0:
                self.condition.setCurrentIndex(idx)

        self.cost_basis = QDoubleSpinBox()
        self.cost_basis.setRange(0, 99999.99)
        self.cost_basis.setDecimals(2)
        self.cost_basis.setPrefix("$")
        if r.get("cost_basis_cents") is not None:
            self.cost_basis.setValue(db.cents_to_dollars(r["cost_basis_cents"]))

        self.quantity = QSpinBox()
        self.quantity.setRange(0, 99999)
        self.quantity.setValue(r.get("quantity", 1))

        self.storage = QLineEdit(r.get("storage_location", "") or "")

        self.date_received = DateEdit()
        if r.get("date_received"):
            try:
                self.date_received.setDate(QDate.fromString(r["date_received"], "yyyy-MM-dd"))
            except Exception:
                self.date_received.setDate(QDate.currentDate())
        else:
            self.date_received.setDate(QDate.currentDate())

        form.addRow(_make_label("Item Name", True),  self.item_name)
        form.addRow(_make_label("SKU"),               self.sku)
        form.addRow(_make_label("Category", True),    self.category)
        form.addRow(_make_label("Size / Variant"),    self.size_variant)
        form.addRow(_make_label("Condition"),          self.condition)
        form.addRow(_make_label("Purchase Price", True),  self.cost_basis)
        form.addRow(_make_label("Quantity", True),     self.quantity)
        form.addRow(_make_label("Storage Location"),   self.storage)
        form.addRow(_make_label("Date Ordered"),        self.date_received)

        if r.get("source_order_number"):
            src_lbl = QLabel(r["source_order_number"])
            form.addRow(_make_label("Source Order"), src_lbl)

        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        if self.row:
            del_btn = btns.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
            del_btn.setObjectName("btnDelete")
            del_btn.clicked.connect(self._delete)

        layout.addWidget(btns)

    def _validate(self) -> bool:
        for name, w in [("Item Name", self.item_name), ("Category", self.category)]:
            if not w.text().strip():
                QMessageBox.warning(self, "Validation", f"{name} is required.")
                return False
        return True

    def _save(self):
        if not self._validate():
            return
        if self.row:
            inv_id = self.row["id"]
            db.adjust_inventory(inv_id, "item_name",        self.item_name.text().strip())
            db.adjust_inventory(inv_id, "sku",              self.sku.text().strip() or None)
            db.adjust_inventory(inv_id, "category",         self.category.text().strip())
            db.adjust_inventory(inv_id, "size_variant",     self.size_variant.text().strip() or None)
            db.adjust_inventory(inv_id, "condition",        self.condition.currentData())
            db.adjust_inventory(inv_id, "cost_basis_cents", db.dollars_to_cents(self.cost_basis.value()))
            db.adjust_inventory(inv_id, "quantity",         self.quantity.value())
            db.adjust_inventory(inv_id, "storage_location", self.storage.text().strip() or None)
            db.adjust_inventory(inv_id, "date_received",    self.date_received.date().toString("yyyy-MM-dd"))
        else:
            qty = max(self.quantity.value(), 1)
            inv_id = None
            for _ in range(qty):
                inv_id = db.add_inventory(
                    item_name        = self.item_name.text().strip(),
                    category         = self.category.text().strip(),
                    cost_basis_cents = db.dollars_to_cents(self.cost_basis.value()),
                    quantity         = 1,
                    condition        = self.condition.currentData(),
                    sku              = self.sku.text().strip() or None,
                    size_variant     = self.size_variant.text().strip() or None,
                    storage_location = self.storage.text().strip() or None,
                    date_received    = self.date_received.date().toString("yyyy-MM-dd"),
                )
            webhooks.notify_inventory_added(inv_id, self.item_name.text().strip(),
                                            qty,
                                            db.dollars_to_cents(self.cost_basis.value()))
        self.accept()

    def _delete(self):
        if QMessageBox.question(self, "Confirm Delete",
                                "Soft-delete this inventory item?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_inventory(self.row["id"])
            self.accept()


class SaleDialog(QDialog):
    def __init__(self, parent=None, row: Optional[Dict] = None):
        super().__init__(parent)
        self.row = row
        self.setWindowTitle("Edit Sale" if row else "Record Sale")
        self.setMinimumWidth(520)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        r = self.row or {}

        # Item selector (combo for add, label for edit)
        if not self.row:
            self.inv_combo = QComboBox()
            inv_items = db.get_inventory(in_stock_only=True)
            for item in inv_items:
                self.inv_combo.addItem(
                    f"{item['item_name']} (Qty: {item['quantity']})",
                    item["id"],
                )
            self.inv_combo.currentIndexChanged.connect(self._update_cost_basis)
            form.addRow(_make_label("Item", True), self.inv_combo)
            self._inv_items = {item["id"]: item for item in inv_items}
        else:
            form.addRow(_make_label("Item"), QLabel(r.get("item_name", "")))

        self.platform = QComboBox()
        for p in PLATFORMS:
            self.platform.addItem(p.title(), p)
        if r.get("platform"):
            idx = self.platform.findData(r["platform"])
            if idx >= 0:
                self.platform.setCurrentIndex(idx)

        self.qty = QSpinBox()
        self.qty.setRange(1, 9999)
        self.qty.setValue(r.get("quantity") or 1)

        self.sale_price = QDoubleSpinBox()
        self.sale_price.setRange(0, 999999.99)
        self.sale_price.setDecimals(2)
        self.sale_price.setPrefix("$")
        if r.get("sale_price_cents"):
            self.sale_price.setValue(db.cents_to_dollars(r["sale_price_cents"]))
        self.sale_price.valueChanged.connect(self._recalc)

        self.fees = QDoubleSpinBox()
        self.fees.setRange(0, 99999.99)
        self.fees.setDecimals(2)
        self.fees.setPrefix("$")
        if r.get("platform_fees_cents"):
            self.fees.setValue(db.cents_to_dollars(r["platform_fees_cents"]))
        self.fees.valueChanged.connect(self._recalc)

        self.shipping = QDoubleSpinBox()
        self.shipping.setRange(0, 99999.99)
        self.shipping.setDecimals(2)
        self.shipping.setPrefix("$")
        if r.get("shipping_cost_cents"):
            self.shipping.setValue(db.cents_to_dollars(r["shipping_cost_cents"]))
        self.shipping.valueChanged.connect(self._recalc)

        self._cost_basis_cents = r.get("cost_basis_cents", 0)

        self.lbl_profit = QLabel("—")
        self.lbl_profit.setFont(QFont("Inter", 13, QFont.Weight.Bold))

        self.status = QComboBox()
        for s in SALE_STATUSES:
            self.status.addItem(s.title(), s)
        if r.get("status"):
            idx = self.status.findData(r["status"])
            if idx >= 0:
                self.status.setCurrentIndex(idx)

        self.tracking  = QLineEdit(r.get("tracking_number", "") or "")
        self.buyer_info = QLineEdit(r.get("buyer_info", "") or "")

        self.date_listed = DateEdit()
        self.date_listed.setSpecialValueText(" ")  # show blank when at minimum
        self.date_listed.setMinimumDate(QDate(2000, 1, 1))
        if r.get("date_listed"):
            self.date_listed.setDate(QDate.fromString(r["date_listed"], "yyyy-MM-dd"))
        else:
            self.date_listed.setDate(self.date_listed.minimumDate())  # blank

        self.date_sold = DateEdit()
        if r.get("date_sold"):
            self.date_sold.setDate(QDate.fromString(r["date_sold"], "yyyy-MM-dd"))
        else:
            self.date_sold.setDate(QDate.currentDate())

        form.addRow(_make_label("Platform", True),   self.platform)
        form.addRow(_make_label("Quantity"),           self.qty)
        form.addRow(_make_label("Sale Price", True),  self.sale_price)
        form.addRow(_make_label("Platform Fees"),     self.fees)
        form.addRow(_make_label("Shipping Cost"),     self.shipping)
        form.addRow(_make_label("Profit Preview"),    self.lbl_profit)
        form.addRow(_make_label("Status"),             self.status)
        form.addRow(_make_label("Tracking #"),         self.tracking)
        form.addRow(_make_label("Buyer Info"),         self.buyer_info)
        form.addRow(_make_label("Date Ordered"),       self.date_listed)
        form.addRow(_make_label("Date Sold"),          self.date_sold)

        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        if self.row:
            del_btn = btns.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
            del_btn.setObjectName("btnDelete")
            del_btn.clicked.connect(self._delete)

        layout.addWidget(btns)
        self._recalc()

    def _update_cost_basis(self):
        inv_id = self.inv_combo.currentData()
        item = self._inv_items.get(inv_id)
        if item:
            self._cost_basis_cents = item["cost_basis_cents"]
        self._recalc()

    def _recalc(self):
        price_c = db.dollars_to_cents(self.sale_price.value())
        fees_c  = db.dollars_to_cents(self.fees.value())
        ship_c  = db.dollars_to_cents(self.shipping.value())
        profit  = price_c - self._cost_basis_cents - fees_c - ship_c
        margin  = (profit / price_c * 100) if price_c > 0 else 0.0
        text    = f"{db.format_money(profit)}  ({margin:.1f}%)"
        self.lbl_profit.setText(text)
        self.lbl_profit.setStyleSheet(f"color: {theme.GREEN};" if profit >= 0 else f"color: {theme.RED};")

    def _validate(self) -> bool:
        if self.sale_price.value() <= 0:
            QMessageBox.warning(self, "Validation", "Sale price must be greater than $0.")
            return False
        if not self.row:
            if self.inv_combo.currentData() is None:
                QMessageBox.warning(self, "Validation", "Select an inventory item.")
                return False
        return True

    def _save(self):
        if not self._validate():
            return
        if self.row:
            sale_id = self.row["id"]
            db.update_sale(sale_id, "platform",            self.platform.currentData())
            db.update_sale(sale_id, "quantity",             self.qty.value())
            db.update_sale(sale_id, "sale_price_cents",    db.dollars_to_cents(self.sale_price.value()))
            db.update_sale(sale_id, "platform_fees_cents", db.dollars_to_cents(self.fees.value()))
            db.update_sale(sale_id, "shipping_cost_cents", db.dollars_to_cents(self.shipping.value()))
            db.update_sale(sale_id, "status",              self.status.currentData())
            db.update_sale(sale_id, "tracking_number",     self.tracking.text().strip() or None)
            db.update_sale(sale_id, "buyer_info",          self.buyer_info.text().strip() or None)
            dl = self.date_listed.date()
            db.update_sale(sale_id, "date_listed",
                           None if dl == self.date_listed.minimumDate()
                           else dl.toString("yyyy-MM-dd"))
            db.update_sale(sale_id, "date_sold",           self.date_sold.date().toString("yyyy-MM-dd"))
        else:
            inv_id = self.inv_combo.currentData()
            dl = self.date_listed.date()
            sale_id = db.add_sale(
                inventory_id        = inv_id,
                platform            = self.platform.currentData(),
                sale_price_cents    = db.dollars_to_cents(self.sale_price.value()),
                platform_fees_cents = db.dollars_to_cents(self.fees.value()),
                shipping_cost_cents = db.dollars_to_cents(self.shipping.value()),
                tracking_number     = self.tracking.text().strip() or None,
                buyer_info          = self.buyer_info.text().strip() or None,
                date_listed         = None if dl == self.date_listed.minimumDate()
                                      else dl.toString("yyyy-MM-dd"),
                date_sold           = self.date_sold.date().toString("yyyy-MM-dd"),
                status              = self.status.currentData(),
                quantity            = self.qty.value(),
            )
            sale = db.get_sale_by_id(sale_id)
            webhooks.notify_sale(sale_id, sale["item_name"], self.platform.currentData(),
                                 sale["sale_price_cents"], sale["profit_cents"], sale["margin_percent"])
        self.accept()

    def _delete(self):
        if QMessageBox.question(self, "Confirm Delete", "Soft-delete this sale?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_sale(self.row["id"])
            self.accept()


class BulkSaleDialog(QDialog):
    """Record a sale for one or more inventory items using shared sale details."""

    def __init__(self, items: List[Dict], parent=None):
        super().__init__(parent)
        self._items = items
        n = len(items)
        self.setWindowTitle(f"Mark as Sold — {n} item{'s' if n != 1 else ''}")
        self.setMinimumWidth(540)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Summary of selected items
        lines = "\n".join(
            f"  \u2022  {r['item_name']}  (cost: {db.format_money(r['cost_basis_cents'])})"
            for r in self._items
        )
        summary = QLabel(lines)
        summary.setWordWrap(True)
        summary.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 11px; padding: 10px;"
            f"background: {theme.BG_ELEVATED}; border-radius: 6px; border: 1px solid {theme.BORDER};"
        )
        layout.addWidget(summary)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        self.platform = QComboBox()
        for p in PLATFORMS:
            self.platform.addItem(p.title(), p)

        self.sale_price = QDoubleSpinBox()
        self.sale_price.setRange(0, 999999.99)
        self.sale_price.setDecimals(2)
        self.sale_price.setPrefix("$")
        self.sale_price.valueChanged.connect(self._recalc)

        self.fees = QDoubleSpinBox()
        self.fees.setRange(0, 99999.99)
        self.fees.setDecimals(2)
        self.fees.setPrefix("$")
        self.fees.valueChanged.connect(self._recalc)

        self.shipping = QDoubleSpinBox()
        self.shipping.setRange(0, 99999.99)
        self.shipping.setDecimals(2)
        self.shipping.setPrefix("$")
        self.shipping.valueChanged.connect(self._recalc)

        self.lbl_profit = QLabel("—")
        self.lbl_profit.setFont(QFont("Inter", 12, QFont.Weight.Bold))

        self.status = QComboBox()
        for s in SALE_STATUSES:
            self.status.addItem(s.title(), s)

        self.date_sold = DateEdit()
        self.date_sold.setDate(QDate.currentDate())
        self.date_sold.dateChanged.connect(self._recalc)

        form.addRow(_make_label("Platform", True),       self.platform)
        form.addRow(_make_label("Sale Price / Unit", True), self.sale_price)
        form.addRow(_make_label("Fees / Unit"),           self.fees)
        form.addRow(_make_label("Shipping / Unit"),       self.shipping)
        form.addRow(_make_label("Profit Preview"),        self.lbl_profit)
        form.addRow(_make_label("Status"),                self.status)
        form.addRow(_make_label("Date Sold"),             self.date_sold)

        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._recalc()

    def _recalc(self):
        n       = len(self._items)
        price_c = db.dollars_to_cents(self.sale_price.value())
        fees_c  = db.dollars_to_cents(self.fees.value())
        ship_c  = db.dollars_to_cents(self.shipping.value())

        total_profit = sum(
            price_c - item["cost_basis_cents"] - fees_c - ship_c
            for item in self._items
        )
        margin = (total_profit / (price_c * n) * 100) if price_c > 0 and n > 0 else 0.0

        text = f"{db.format_money(total_profit)} total  ({margin:.1f}%)"
        if n > 1:
            text += f"  —  {db.format_money(total_profit // n)} / unit"
        self.lbl_profit.setText(text)
        self.lbl_profit.setStyleSheet(f"color: {theme.GREEN};" if total_profit >= 0 else f"color: {theme.RED};")

    def _save(self):
        if self.sale_price.value() <= 0:
            QMessageBox.warning(self, "Validation", "Sale price must be greater than $0.")
            return

        platform = self.platform.currentData()
        price_c  = db.dollars_to_cents(self.sale_price.value())
        fees_c   = db.dollars_to_cents(self.fees.value())
        ship_c   = db.dollars_to_cents(self.shipping.value())
        status   = self.status.currentData()
        date     = self.date_sold.date().toString("yyyy-MM-dd")

        for item in self._items:
            sale_id = db.add_sale(
                inventory_id        = item["id"],
                platform            = platform,
                sale_price_cents    = price_c,
                platform_fees_cents = fees_c,
                shipping_cost_cents = ship_c,
                date_sold           = date,
                status              = status,
            )
            sale = db.get_sale_by_id(sale_id)
            if sale:
                webhooks.notify_sale(sale_id, sale["item_name"], platform,
                                     price_c, sale["profit_cents"], sale["margin_percent"])

        self.accept()


# ── Tab: Order Tracker ────────────────────────────────────────────────────────

_STATUS_COLORS = {
    "ordered":   theme.BLUE,
    "shipped":   theme.ORANGE,
    "delivered": theme.GREEN,
    "cancelled": theme.RED,
    "returned":  theme.RED,
}

_SHOPIFY_PURPLE = "#a68cff"

_RETAILER_COLORS = {
    "pokemon_center": "#FFCB05",   # Pokemon yellow
    "walmart":        "#0071CE",   # Walmart blue
    "target":         "#CC0000",   # Target red
    "ebay":           "#F5AF02",   # eBay yellow
    "five_below":     "#FF6347",   # Five Below red-orange
    "topps":          _SHOPIFY_PURPLE,
    "nike":           "#FF6B35",   # Nike orange
    "bestbuy":        "#0046BE",   # Best Buy blue
    "amazon":         "#FF9900",   # Amazon orange
    "stockx":         "#00A862",   # StockX green
    "shopify":        _SHOPIFY_PURPLE,
}


def _dominant_status(orders: List[Dict]) -> str:
    """Return the status of the most recently dated order in the group."""
    if not orders:
        return "ordered"
    return max(orders, key=lambda o: o["order_date"])["status"]


class FilterPill(QPushButton):
    """Pill-shaped checkable filter button."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setFixedHeight(26)
        self.toggled.connect(lambda _: self._refresh_style())
        self._refresh_style()

    def _refresh_style(self):
        if self.isChecked():
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {theme.BG_ELEVATED}; color: {theme.BLUE};
                    border: 1px solid {theme.BLUE};
                    border-radius: 13px; padding: 0 16px;
                    font-size: 11px; font-weight: 600;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {theme.TEXT_SECONDARY};
                    border: 1px solid {theme.BG_CARD}; border-radius: 13px;
                    padding: 0 16px; font-size: 11px;
                }}
                QPushButton:hover {{
                    background: {theme.BG_ELEVATED}; color: {theme.TEXT_PRIMARY}; border-color: {theme.BORDER};
                }}
            """)


class OrderCard(QFrame):
    """Clickable card representing one product drop (item + order date)."""
    clicked        = pyqtSignal(str, str)   # emits (item_name, drop_date)
    right_clicked  = pyqtSignal(str, str)   # emits (item_name, drop_date)
    selection_changed = pyqtSignal()

    def __init__(self, item_name: str, drop_date: str, orders: List[Dict], parent=None):
        super().__init__(parent)
        self.item_name = item_name
        self.drop_date = drop_date
        self._selected = False

        status     = _dominant_status(orders)
        self._status_color = _STATUS_COLORS.get(status, theme.TEXT_SECONDARY)
        total_qty  = sum(o["quantity"] for o in orders)
        n_orders   = len(set(o["order_id"] for o in orders))
        cost_cents = sum(o["cost_cents"] * o["quantity"] for o in orders)

        retailer_key          = orders[0]["retailer"] if orders else ""
        retailer              = retailer_key.replace("_", " ").title()
        retailer_color        = _RETAILER_COLORS.get(retailer_key, _SHOPIFY_PURPLE)
        self._retailer_color  = retailer_color

        self.setObjectName("orderCard")
        self.setMinimumHeight(130)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._apply_style()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 10, 12)
        outer.setSpacing(10)

        # Text column
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        date_lbl = QLabel(_fmt_date(drop_date))
        date_lbl.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 9px; font-weight: 700; "
            f"letter-spacing: 0.8px; background: transparent;"
        )
        top_row.addWidget(date_lbl)
        top_row.addStretch()
        if retailer:
            ret_lbl = QLabel(retailer)
            ret_lbl.setStyleSheet(
                f"color: {retailer_color}; font-size: 9px; font-weight: 700; "
                f"letter-spacing: 0.5px; background: transparent;"
            )
            top_row.addWidget(ret_lbl)
        lay.addLayout(top_row)

        n_lbl = QLabel(item_name)
        n_lbl.setWordWrap(True)
        n_lbl.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        lay.addWidget(n_lbl)
        outer.addLayout(lay, 1)

        lay.addStretch()

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 2, 0, 0)
        qty_lbl = QLabel(f"{total_qty} units · {n_orders} order{'s' if n_orders != 1 else ''}")
        qty_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;")
        bottom.addWidget(qty_lbl)
        bottom.addStretch()
        cost_lbl = QLabel(db.format_money(cost_cents))
        cost_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;")
        bottom.addWidget(cost_lbl)
        lay.addLayout(bottom)

    def _apply_style(self):
        accent = getattr(self, "_retailer_color", self._status_color)
        if self._selected:
            self.setStyleSheet(f"""
                QFrame#orderCard {{
                    background: {theme.BG_ELEVATED};
                    border: 1px solid {theme.BLUE};
                    border-radius: 10px;
                    border-left: 3px solid {accent};
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame#orderCard {{
                    background: {theme.BG_CARD};
                    border: 1px solid {theme.BG_ELEVATED};
                    border-radius: 10px;
                    border-left: 3px solid {accent};
                }}
                QFrame#orderCard:hover {{
                    background: {theme.BG_ELEVATED};
                    border-color: {theme.BORDER};
                    border-left-color: {accent};
                }}
            """)

    def set_selected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_style()

    def is_selected(self) -> bool:
        return self._selected

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            modifiers = event.modifiers()
            if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                self.set_selected(not self._selected)
                self.selection_changed.emit()
            else:
                self.clicked.emit(self.item_name, self.drop_date)
        elif event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit(self.item_name, self.drop_date)


class _OrderDetailModel(RecordTableModel):
    _STATUS_COL = 3

    def _foreground(self, row, col, value):
        if col == self._STATUS_COL:
            return QColor(_STATUS_COLORS.get(str(value).lower(), theme.TEXT_SECONDARY))
        return QVariant()


class OrderDetailDialog(QDialog):
    """Shows all orders for a given product drop with edit/add controls."""

    def __init__(self, item_name: str, drop_date: str, orders: List[Dict], parent=None):
        super().__init__(parent)
        self._item_name = item_name
        self._drop_date = drop_date
        self._orders    = sorted(orders, key=lambda o: _address_number_key(o.get("delivery_address") or ""))
        self.setWindowTitle(f"{item_name} — {_fmt_date(drop_date)}")
        self.setMinimumSize(780, 480)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(16)

        # Title
        title = QLabel(self._item_name)
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.PURPLE};")
        lay.addWidget(title)

        sub = QLabel(f"Drop: {_fmt_date(self._drop_date)}")
        sub.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_SECONDARY};")
        lay.addWidget(sub)

        # Drop summary tiles
        total_orders = len(set(o["order_id"] for o in self._orders))
        n_shipped    = sum(1 for o in self._orders if o["status"] == "shipped")
        n_delivered  = sum(1 for o in self._orders if o["status"] == "delivered")
        n_cancelled  = sum(1 for o in self._orders if o["status"] == "cancelled")
        n_pending    = total_orders - n_shipped - n_delivered - n_cancelled

        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        for label, value in [
            ("Total Orders", str(total_orders)),
            ("Shipped",      str(n_shipped)),
            ("Delivered",    str(n_delivered)),
            ("Pending",      str(n_pending)),
            ("Cancelled",    str(n_cancelled)),
        ]:
            f = QFrame()
            f.setStyleSheet(
                f"QFrame {{ background: {theme.BG_ELEVATED}; border-radius: 6px; border: 1px solid {theme.BORDER}; }}"
            )
            fl = QVBoxLayout(f)
            fl.setContentsMargins(14, 9, 14, 9)
            fl.setSpacing(2)
            lk = QLabel(label)
            lk.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;")
            lv = QLabel(value)
            lv.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; font-size: 15px; font-weight: 700; background: transparent;"
            )
            fl.addWidget(lk)
            fl.addWidget(lv)
            stat_row.addWidget(f)
        stat_row.addStretch()
        lay.addLayout(stat_row)

        # Orders table
        headers = ["Order #", "Retailer", "Date", "Status", "Cost/Unit", "Qty", "Tracking", "Ship To"]
        rows = [
            [o["order_number"], o["retailer"], _fmt_date(o["order_date"]), o["status"],
             db.format_money(o["cost_cents"]), o["quantity"],
             o.get("tracking_number") or "",
             _format_address_short(o.get("delivery_address") or "")]
            for o in self._orders
        ]
        self._source_model = _OrderDetailModel(headers, rows)
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._source_model)
        self._proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.resizeColumnsToContents()
        lay.addWidget(self._table)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_add = QPushButton("+ Add Another Order")
        btn_add.setObjectName("btnAdd")
        btn_add.clicked.connect(self._add_order)
        btn_row.addWidget(btn_add)
        btn_row.addStretch()
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

    def _source_row(self, proxy_row: int) -> int:
        return self._proxy.mapToSource(self._proxy.index(proxy_row, 0)).row()

    def _selected_orders(self) -> List[Dict]:
        rows = {self._source_row(idx.row()) for idx in self._table.selectionModel().selectedRows()}
        return [self._orders[r] for r in sorted(rows) if 0 <= r < len(self._orders)]

    def _context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        selected = self._selected_orders()
        if not selected:
            selected = [self._orders[self._source_row(index.row())]]
        menu = QMenu(self)
        if len(selected) == 1:
            menu.addAction("Edit").triggered.connect(lambda: self._edit_order(selected[0]))
            menu.addSeparator()
        n = len(selected)
        status_menu = menu.addMenu(f"Set Status ({n} order{'s' if n > 1 else ''})")
        for s in INBOUND_STATUSES:
            status_menu.addAction(s.title()).triggered.connect(
                lambda checked, st=s: self._bulk_set_status(selected, st)
            )
        menu.addSeparator()
        menu.addAction(f"Delete{f' ({n})' if n > 1 else ''}").triggered.connect(
            lambda: self._delete_orders(selected)
        )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _edit_order(self, r: Dict):
        dlg = InboundDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.accept()

    def _bulk_set_status(self, orders: List[Dict], status: str):
        for o in orders:
            db.update_inbound_order(o["order_id"], "status", status)
        self.accept()

    def _delete_orders(self, orders: List[Dict]):
        n = len(orders)
        msg = (f"Soft-delete {n} orders?\n(Data is retained but hidden.)"
               if n > 1 else "Soft-delete this order?\n(Data is retained but hidden.)")
        if QMessageBox.question(self, "Confirm Delete", msg,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            for o in orders:
                db.delete_inbound_item(o["item_id"])
            self.accept()

    def _on_double_click(self, index: QModelIndex):
        r = self._orders[self._source_row(index.row())]
        dlg = InboundDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.accept()

    def _add_order(self):
        dlg = InboundDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.accept()


class PickupDialog(QDialog):
    """Mark delivered packages as picked up — moves them to inventory.

    Batches identical items (same name + retailer + cost) into one row with
    a quantity spinner so the user can choose how many to pick up.
    """

    def __init__(self, display_address: str, orders: List[Dict], parent=None):
        super().__init__(parent)
        self._orders = orders
        self._rows: List[dict] = []   # [{spin, orders, total_pkg}]
        self.setWindowTitle(f"Pickup — {_address_line1(display_address)}")
        self.setMinimumSize(720, 480)
        self._build(display_address)

    def _build(self, display_address: str):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        title = QLabel(_address_line1(display_address))
        title.setWordWrap(True)
        title.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {theme.PURPLE};")
        lay.addWidget(title)

        total_pkgs = len(self._orders)
        sub = QLabel(
            f"{total_pkgs} package{'s' if total_pkgs != 1 else ''} pending pickup. "
            "Set the quantity to pick up for each item, then confirm."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.BORDER};")
        lay.addWidget(sep)

        # ── Batch identical items ─────────────────────────────────────────
        batched: dict = {}   # (item_name, retailer, cost_cents) → [order_dicts]
        for o in self._orders:
            key = (o["item_name"], o["retailer"], o["cost_cents"])
            batched.setdefault(key, []).append(o)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_w = QWidget()
        scroll_l = QVBoxLayout(scroll_w)
        scroll_l.setContentsMargins(0, 0, 0, 0)
        scroll_l.setSpacing(6)

        for (item_name, retailer, cost_cents), group_orders in batched.items():
            total_qty = sum(o["quantity"] for o in group_orders)
            pkg_count = len(group_orders)

            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(6, 6, 6, 6)
            row_l.setSpacing(10)

            # Quantity input
            spin = QSpinBox()
            spin.setRange(0, pkg_count)
            spin.setValue(pkg_count)
            spin.setFixedWidth(70)
            spin.setPrefix("")
            spin.setSuffix(f" / {pkg_count}")
            spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            row_l.addWidget(spin)

            info = QLabel(
                f"<b>{item_name}</b>"
                f"  ·  {retailer.replace('_', ' ').title()}"
                f"  ·  {db.format_money(cost_cents)}/ea"
                f"  ·  {total_qty} units across {pkg_count} pkg{'s' if pkg_count != 1 else ''}"
            )
            info.setWordWrap(True)
            info.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 11px;")
            row_l.addWidget(info, 1)

            scroll_l.addWidget(row_w)
            self._rows.append({
                "spin": spin,
                "orders": group_orders,
                "total_pkg": pkg_count,
            })

        scroll_l.addStretch()
        scroll.setWidget(scroll_w)
        lay.addWidget(scroll, 1)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_all = QPushButton("Pick Up All")
        btn_all.setObjectName("btnAdd")
        btn_all.clicked.connect(self._mark_selected)
        btn_row.addWidget(btn_all)

        btn_row.addStretch()

        btn_del_all = QPushButton("Delete All")
        btn_del_all.setObjectName("btnDelete")
        btn_del_all.clicked.connect(self._delete_all)
        btn_row.addWidget(btn_del_all)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        lay.addLayout(btn_row)

    def _delete_all(self):
        n = len(self._orders)
        if QMessageBox.question(
            self, "Delete All",
            f"Delete all {n} pending order{'s' if n != 1 else ''} for this address?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            for o in self._orders:
                db.delete_inbound_order(o["order_id"])
            self.accept()

    def _mark_selected(self):
        today = datetime.now().strftime("%Y-%m-%d")
        marked_order_ids: set = set()

        for row in self._rows:
            pick_count = row["spin"].value()
            if pick_count == 0:
                continue
            # Pick up the first N orders in this batch
            for o in row["orders"][:pick_count]:
                for _ in range(max(o["quantity"], 1)):
                    db.add_inventory(
                        item_name            = o["item_name"],
                        category             = "General",
                        cost_basis_cents     = o["cost_cents"],
                        quantity             = 1,
                        sku                  = o.get("sku"),
                        date_received        = o.get("order_date") or today,
                        inbound_order_item_id= o["item_id"],
                    )
                marked_order_ids.add(o["order_id"])

        for order_id in marked_order_ids:
            db.update_inbound_order(order_id, "status", "received")

        self.accept()


class PickupAddressCard(QFrame):
    """Clickable card showing packages awaiting pickup at one address."""
    clicked        = pyqtSignal(str)          # emits address
    delete_address = pyqtSignal(str, list)    # emits (address, orders)

    def __init__(self, address: str, orders: List[Dict], parent=None):
        super().__init__(parent)
        self._address = address
        self._orders  = orders
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setObjectName("orderCard")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        color = _STATUS_COLORS.get("delivered", theme.GREEN)
        self.setStyleSheet(f"""
            QFrame#orderCard {{
                background: {theme.BG_CARD}; border-radius: 10px;
                border: 1px solid {theme.BG_ELEVATED}; border-left: 3px solid {color};
            }}
            QFrame#orderCard:hover {{
                background: {theme.BG_ELEVATED}; border-color: {theme.BORDER};
                border-left-color: {color};
            }}
        """)

        def _delivery_date(orders):
            dates = []
            for o in orders:
                d = o.get("estimated_delivery") or ""
                if d:
                    dates.append(d[:10])
                elif o.get("updated_at"):
                    dates.append(str(o["updated_at"])[:10])
            return max(dates) if dates else None

        delivered_on = _delivery_date(orders)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)

        pkg_lbl = QLabel(f"{len(orders)} PACKAGE{'S' if len(orders) != 1 else ''}")
        pkg_lbl.setStyleSheet(
            f"color: {color}; font-size: 9px; font-weight: 700; "
            f"letter-spacing: 0.8px; background: transparent;"
        )
        lay.addWidget(pkg_lbl)

        addr_lbl = QLabel(_address_line1(address))
        addr_lbl.setWordWrap(True)
        addr_lbl.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        lay.addWidget(addr_lbl)

        lay.addStretch()

        if delivered_on:
            del_lbl = QLabel(f"Delivered {_fmt_date(delivered_on)}")
            del_lbl.setStyleSheet(
                f"color: {color}; font-size: 10px; font-weight: 600; background: transparent;"
            )
            lay.addWidget(del_lbl)

    def _context_menu(self, pos):
        menu = QMenu(self)
        n = len(self._orders)
        menu.addAction(f"Delete All ({n} order{'s' if n != 1 else ''})").triggered.connect(
            lambda: self.delete_address.emit(self._address, self._orders)
        )
        menu.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._address)


class OrderTrackerTab(QWidget):
    refresh_needed = pyqtSignal()
    _COLS = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw: List[Dict] = []
        self._shipped_orders: List[Dict] = []
        self._cards: List[OrderCard] = []
        self._build_ui()

        self._tracking_timer = QTimer(self)
        self._tracking_timer.setInterval(60 * 60 * 1000)  # 1 hour in ms
        self._tracking_timer.timeout.connect(self._auto_refresh_tracking_if_stale)
        self._tracking_timer.start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Persistent header
        hdr = QHBoxLayout()
        lbl = QLabel("Order Tracker")
        lbl.setObjectName("lblHeader")
        hdr.addWidget(lbl)
        hdr.addStretch()
        self._btn_scrape = QPushButton("⚙ Manual Scrape")
        self._btn_scrape.clicked.connect(self._open_manual_scrape)
        hdr.addWidget(self._btn_scrape)
        btn_export = QPushButton("Export CSV")
        btn_export.clicked.connect(self._export_csv)
        hdr.addWidget(btn_export)
        btn_add = QPushButton("+ Add Order")
        btn_add.setObjectName("btnAdd")
        btn_add.clicked.connect(self._add)
        hdr.addWidget(btn_add)
        layout.addLayout(hdr)

        # Sub-tab bar
        self._sub_tabs = QTabBar()
        self._sub_tabs.addTab("Order Tracker")
        self._sub_tabs.addTab("Inbound Shipments")
        self._sub_tabs.addTab("Pending Pickups")
        self._sub_tabs.currentChanged.connect(self._on_sub_tab_changed)
        layout.addWidget(self._sub_tabs)

        # Stacked pages
        self._stack = QStackedWidget()

        tracker_page = QWidget()
        self._build_tracker_page(tracker_page)
        self._stack.addWidget(tracker_page)

        shipments_page = QWidget()
        self._build_shipments_page(shipments_page)
        self._stack.addWidget(shipments_page)

        pickups_page = QWidget()
        self._build_pickups_page(pickups_page)
        self._stack.addWidget(pickups_page)

        layout.addWidget(self._stack)

    def _build_tracker_page(self, page: QWidget):
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(10)

        search_row = QHBoxLayout()
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search items…")
        self._search_box.setFixedWidth(220)
        self._search_box.textChanged.connect(self._apply_filter)
        search_row.addWidget(self._search_box)
        search_row.addStretch()
        lay.addLayout(search_row)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._cards_widget = QWidget()
        self._cards_widget.setStyleSheet("background: transparent;")
        self._cards_grid = QGridLayout(self._cards_widget)
        self._cards_grid.setSpacing(12)
        self._cards_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for c in range(self._COLS):
            self._cards_grid.setColumnStretch(c, 1)
        self._scroll.setWidget(self._cards_widget)
        lay.addWidget(self._scroll)

    def _build_shipments_page(self, page: QWidget):
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left — in-transit table
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        ship_hdr = QHBoxLayout()
        hdr_lbl = QLabel("In Transit")
        hdr_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {theme.TEXT_SECONDARY};"
        )
        ship_hdr.addWidget(hdr_lbl)
        ship_hdr.addStretch()
        self._btn_refresh_tracking = QPushButton("↻ Refresh Tracking")
        self._btn_refresh_tracking.clicked.connect(self._refresh_all_tracking)
        ship_hdr.addWidget(self._btn_refresh_tracking)
        ll.addLayout(ship_hdr)

        self._shipments_table = QTableView()
        self._shipments_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._shipments_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._shipments_table.setAlternatingRowColors(True)
        self._shipments_table.verticalHeader().setVisible(False)
        self._shipments_table.verticalHeader().setDefaultSectionSize(34)
        self._shipments_table.horizontalHeader().setStretchLastSection(True)
        self._shipments_table.setSortingEnabled(True)
        self._shipments_table.doubleClicked.connect(self._on_shipment_double_click)
        self._shipments_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._shipments_table.customContextMenuRequested.connect(self._on_shipment_context_menu)
        ll.addWidget(self._shipments_table)
        splitter.addWidget(left)

        # Right — calendar + deliveries list
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 0, 0, 0)
        rl.setSpacing(8)
        cal_lbl = QLabel("Deliveries")
        cal_lbl.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {theme.TEXT_SECONDARY};"
        )
        rl.addWidget(cal_lbl)
        self._calendar = QCalendarWidget()
        self._calendar.setGridVisible(True)
        self._calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self._calendar.setMaximumHeight(230)
        self._calendar.clicked.connect(self._on_calendar_date_clicked)
        rl.addWidget(self._calendar)

        self._delivery_label = QLabel("Click a highlighted date to see packages")
        self._delivery_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 11px; font-style: italic;"
        )
        rl.addWidget(self._delivery_label)

        self._delivery_list = QWidget()
        self._delivery_list_lay = QVBoxLayout(self._delivery_list)
        self._delivery_list_lay.setContentsMargins(0, 0, 0, 0)
        self._delivery_list_lay.setSpacing(4)

        self._delivery_scroll = QScrollArea()
        self._delivery_scroll.setWidget(self._delivery_list)
        self._delivery_scroll.setWidgetResizable(True)
        self._delivery_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._delivery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rl.addWidget(self._delivery_scroll, 1)  # stretch=1 so it fills remaining space
        splitter.addWidget(right)

        splitter.setSizes([560, 300])
        lay.addWidget(splitter)

    def _build_pickups_page(self, page: QWidget):
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(8)

        sub_lbl = QLabel("Packages marked as delivered, grouped by delivery address.")
        sub_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(sub_lbl)

        self._pickups_scroll = QScrollArea()
        self._pickups_scroll.setWidgetResizable(True)
        self._pickups_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._pickups_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._pickups_scroll.viewport().setStyleSheet("background: transparent;")
        self._pickups_widget = QWidget()
        self._pickups_widget.setStyleSheet("background: transparent;")
        self._pickups_grid = QGridLayout(self._pickups_widget)
        self._pickups_grid.setSpacing(12)
        self._pickups_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for c in range(self._COLS):
            self._pickups_grid.setColumnStretch(c, 1)
        self._pickups_scroll.setWidget(self._pickups_widget)
        lay.addWidget(self._pickups_scroll)

    # ── Sub-tab switching ─────────────────────────────────────────────────────

    _TRACKING_COOLDOWN_SECS = 30 * 60   # 30 minutes

    def _on_sub_tab_changed(self, index: int):
        self._stack.setCurrentIndex(index)
        if index == 1:
            self._refresh_shipments()
            self._auto_refresh_tracking_if_stale()
        elif index == 2:
            self._refresh_pickups()

    def _auto_refresh_tracking_if_stale(self):
        """Kick off a background tracking refresh if last check was > 30 min ago."""
        import time
        import tracking
        if not tracking.is_configured():
            return
        if hasattr(self, "_tracking_worker") and self._tracking_worker.isRunning():
            return
        last = getattr(self, "_last_tracking_refresh", 0)
        if time.time() - last < self._TRACKING_COOLDOWN_SECS:
            return
        self._last_tracking_refresh = time.time()
        self._tracking_worker = TrackingRefreshWorker()
        self._tracking_worker.finished.connect(self._on_tracking_done)
        self._tracking_worker.start()

    # ── Order Tracker page ────────────────────────────────────────────────────

    def _apply_filter(self):
        search = self._search_box.text().strip().lower()

        drops: Dict[tuple, List[Dict]] = {}
        for o in self._raw:
            name = o["item_name"]
            if search and search not in name.lower():
                continue
            drops.setdefault((name, o["order_date"]), []).append(o)

        while self._cards_grid.count():
            item = self._cards_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not drops:
            empty = QLabel(
                "No orders match the current filter."
                if search
                else "No orders yet — click '+ Add Order' to get started."
            )
            empty.setStyleSheet(
                f"color: {theme.TEXT_SECONDARY}; font-size: 13px; background: transparent;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._cards_grid.addWidget(empty, 0, 0, 1, self._COLS)
            return

        self._cards: List[OrderCard] = []
        for i, ((name, date), ords) in enumerate(
            sorted(drops.items(), key=lambda x: x[0][1], reverse=True)
        ):
            row, col = divmod(i, self._COLS)
            card = OrderCard(name, date, ords)
            card.clicked.connect(self._on_card_clicked)
            card.right_clicked.connect(self._on_card_right_clicked)
            card.selection_changed.connect(self._on_card_selection_changed)
            self._cards_grid.addWidget(card, row, col)
            self._cards.append(card)

    def _selected_cards(self) -> List[OrderCard]:
        return [c for c in self._cards if c.is_selected()]

    def _clear_card_selection(self):
        for c in self._cards:
            c.set_selected(False)

    def _on_card_selection_changed(self):
        # Nothing extra needed — selection state lives on the cards themselves
        pass

    def _on_card_clicked(self, item_name: str, drop_date: str):
        # If any cards are selected, treat plain click as a toggle instead of opening dialog
        if self._selected_cards():
            sender = self.sender()
            if isinstance(sender, OrderCard):
                sender.set_selected(not sender.is_selected())
            return
        orders = [o for o in self._raw
                  if o["item_name"] == item_name and o["order_date"] == drop_date]
        dlg = OrderDetailDialog(item_name, drop_date, orders, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _on_card_right_clicked(self, item_name: str, drop_date: str):
        selected = self._selected_cards()
        menu = QMenu(self)

        if len(selected) > 1:
            # Multi-card context menu
            total_orders = sum(
                len([o for o in self._raw if o["item_name"] == c.item_name and o["order_date"] == c.drop_date])
                for c in selected
            )
            menu.addAction(f"Delete {len(selected)} Drops ({total_orders} orders)").triggered.connect(
                lambda: self._delete_selected_drops(selected)
            )
            menu.addSeparator()
            menu.addAction("Clear Selection").triggered.connect(self._clear_card_selection)
        else:
            # Single-card context menu (original behaviour)
            orders = [o for o in self._raw
                      if o["item_name"] == item_name and o["order_date"] == drop_date]
            menu.addAction("Edit Drop").triggered.connect(
                lambda: self._on_card_clicked(item_name, drop_date)
            )
            menu.addSeparator()
            n = len(orders)
            menu.addAction(f"Delete Drop ({n} order{'s' if n != 1 else ''})").triggered.connect(
                lambda: self._delete_drop(item_name, drop_date, orders)
            )
        menu.exec(QCursor.pos())

    def _delete_selected_drops(self, cards: List[OrderCard]):
        all_orders = []
        lines = []
        for c in cards:
            ords = [o for o in self._raw if o["item_name"] == c.item_name and o["order_date"] == c.drop_date]
            all_orders.extend(ords)
            lines.append(f"  • {c.item_name}  —  {c.drop_date}  ({len(ords)} orders)")
        if QMessageBox.question(
            self, "Confirm Delete",
            f"Delete {len(cards)} drops ({len(all_orders)} orders total)?\n\n"
            + "\n".join(lines) + "\n\n(Data is retained but hidden.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            for o in all_orders:
                db.delete_inbound_item(o["item_id"])
            self.load_data()
            self.refresh_needed.emit()

    def _delete_drop(self, item_name: str, drop_date: str, orders: List[Dict]):
        n = len(orders)
        if QMessageBox.question(
            self, "Confirm Delete",
            f"Delete all {n} order{'s' if n != 1 else ''} in this drop?\n\n"
            f"{item_name}  —  {drop_date}\n\n"
            "(Data is retained but hidden.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            for o in orders:
                db.delete_inbound_item(o["item_id"])
            self.load_data()
            self.refresh_needed.emit()

    # ── Inbound Shipments page ────────────────────────────────────────────────

    def _refresh_shipments(self):
        self._shipped_orders = [o for o in self._raw if o["status"] == "shipped"]
        shipped = self._shipped_orders

        # 5-column view — click row for full details, click tracking # to open browser
        headers = ["Item", "Retailer", "Tracking #", "Ship To", "Est. Delivery"]
        rows = [
            [o["item_name"],
             o["retailer"],
             o.get("tracking_number") or "",
             _format_address_short(o.get("delivery_address") or ""),
             _fmt_date(o.get("estimated_delivery") or "")]
            for o in shipped
        ]
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(ShipmentsTableModel(headers, rows))
        proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._shipments_table.setModel(proxy)
        self._shipments_table.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        hdr = self._shipments_table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)          # Item
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)            # Retailer
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)            # Tracking #
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)            # Ship To
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)            # Est. Delivery
        self._shipments_table.setColumnWidth(1, 90)
        self._shipments_table.setColumnWidth(2, 160)
        self._shipments_table.setColumnWidth(3, 140)
        self._shipments_table.setColumnWidth(4, 95)

        # Connect single-click for tracking link (disconnect old first to avoid stacking)
        try:
            self._shipments_table.clicked.disconnect(self._on_shipment_clicked)
        except Exception:
            pass
        self._shipments_table.clicked.connect(self._on_shipment_clicked)

        # Highlight estimated delivery dates on calendar
        fmt_highlight = QTextCharFormat()
        fmt_highlight.setBackground(QColor(theme.BG_ELEVATED))
        fmt_highlight.setForeground(QColor(theme.BLUE))

        # Clear previous highlights before re-drawing
        self._calendar.setDateTextFormat(QDate(), QTextCharFormat())

        # Key by estimated_delivery; fall back to order_date if no ETA yet
        self._shipped_by_date: Dict[str, List[Dict]] = {}
        for o in shipped:
            date_key = (o.get("estimated_delivery") or o["order_date"])[:10]
            self._shipped_by_date.setdefault(date_key, []).append(o)

        for date_str in self._shipped_by_date:
            parts = date_str.split("-")
            if len(parts) == 3:
                qdate = QDate(int(parts[0]), int(parts[1]), int(parts[2]))
                self._calendar.setDateTextFormat(qdate, fmt_highlight)

    def _on_calendar_date_clicked(self, qdate: QDate):
        date_str = qdate.toString("yyyy-MM-dd")
        orders = getattr(self, "_shipped_by_date", {}).get(date_str, [])

        # Clear old list
        while self._delivery_list_lay.count():
            item = self._delivery_list_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not orders:
            self._delivery_label.setText("No in-transit packages on this date")
            return

        # Batch: group by (item_name, normalized_addr), sum quantities.
        # Normalize addresses so "1428 S Jackson St", "1428 South Jackson Street",
        # "1428 s jackson st." etc. all collapse to the same key.
        _batched: dict = {}   # (item_name, norm_key) → [qty, display_addr]
        for o in orders:
            addr     = _format_address_short(o.get("delivery_address") or "") or "No address"
            norm_key = _normalize_addr_key(addr)
            key      = (o["item_name"], norm_key)
            if key not in _batched:
                _batched[key] = [0, addr]   # [qty, first-seen display addr]
            _batched[key][0] += (o.get("quantity") or 1)

        pkg_count = len(orders)
        self._delivery_label.setText(
            f"{pkg_count} package{'s' if pkg_count != 1 else ''} scheduled for delivery on {_fmt_date(date_str)}:"
        )
        for (name, _norm), (qty, addr) in _batched.items():
            prefix = f"{qty}x " if qty > 1 else ""
            row_lbl = QLabel(f"• {prefix}{name}  —  {addr}")
            row_lbl.setWordWrap(True)
            row_lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; font-size: 11px; background: transparent;"
            )
            self._delivery_list_lay.addWidget(row_lbl)
        self._delivery_list_lay.addStretch()

    def _get_shipped_raw(self, proxy_row: int) -> Optional[Dict]:
        model = self._shipments_table.model()
        if model is None:
            return None
        if isinstance(model, QSortFilterProxyModel):
            source_row = model.mapToSource(model.index(proxy_row, 0)).row()
        else:
            source_row = proxy_row
        if 0 <= source_row < len(self._shipped_orders):
            return self._shipped_orders[source_row]
        return None

    def _on_shipment_clicked(self, index: QModelIndex):
        """Single click on tracking column opens carrier tracking page in browser."""
        if index.column() != ShipmentsTableModel.TRACKING_COL:
            return
        r = self._get_shipped_raw(index.row())
        if not r:
            return
        tn = r.get("tracking_number", "").strip()
        if not tn:
            return
        url = _tracking_url(tn, r.get("tracking_carrier") or "")
        QDesktopServices.openUrl(QUrl(url))

    def _on_shipment_double_click(self, index: QModelIndex):
        r = self._get_shipped_raw(index.row())
        if r:
            dlg = InboundDialog(self, row=r)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.load_data()
                self.refresh_needed.emit()

    def _on_shipment_context_menu(self, pos):
        index = self._shipments_table.indexAt(pos)
        if not index.isValid():
            return
        r = self._get_shipped_raw(index.row())
        if not r:
            return
        menu = QMenu(self)
        menu.addAction("Edit").triggered.connect(lambda: self._edit_shipment(r))
        menu.addSeparator()
        menu.addAction("Delete").triggered.connect(lambda: self._delete_shipment(r))
        menu.exec(self._shipments_table.viewport().mapToGlobal(pos))

    def _edit_shipment(self, r: Dict):
        dlg = InboundDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _delete_shipment(self, r: Dict):
        if QMessageBox.question(self, "Confirm", "Soft-delete this order item?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_inbound_item(r["item_id"])
            self.load_data()
            self.refresh_needed.emit()

    # ── Pending Pickups page ──────────────────────────────────────────────────

    def _refresh_pickups(self):
        delivered = [o for o in self._raw if o["status"] == "delivered"]

        # Merge jigged addresses by their digit sequences
        # groups: canonical_key → (display_address, [orders])
        groups: Dict[str, tuple] = {}
        for o in delivered:
            addr = (o.get("delivery_address") or "Unknown Address").strip()
            key = _address_number_key(addr)
            if key not in groups:
                groups[key] = (addr, [])
            groups[key][1].append(o)

        while self._pickups_grid.count():
            item = self._pickups_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not groups:
            empty = QLabel("No packages pending pickup.")
            empty.setStyleSheet(
                f"color: {theme.TEXT_SECONDARY}; font-size: 13px; background: transparent;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._pickups_grid.addWidget(empty, 0, 0, 1, self._COLS)
            return

        for i, (key, (display_addr, orders)) in enumerate(sorted(groups.items())):
            row, col = divmod(i, self._COLS)
            card = PickupAddressCard(display_addr, orders)
            card.clicked.connect(
                lambda _, da=display_addr, ords=orders: self._on_pickup_card_clicked(da, ords)
            )
            card.delete_address.connect(self._on_delete_address)
            self._pickups_grid.addWidget(card, row, col)

    def _on_pickup_card_clicked(self, display_address: str, orders: List[Dict]):
        dlg = PickupDialog(display_address, orders, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _on_delete_address(self, display_address: str, orders: List[Dict]):
        n = len(orders)
        line1 = _address_line1(display_address)
        if QMessageBox.question(
            self, "Delete All",
            f"Delete all {n} order{'s' if n != 1 else ''} for:\n{line1}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            for o in orders:
                db.delete_inbound_order(o["order_id"])
            self.load_data()
            self.refresh_needed.emit()

    # ── Shared actions ────────────────────────────────────────────────────────

    def _refresh_all_tracking(self):
        import tracking
        if not tracking.is_configured():
            QMessageBox.information(
                self, "Tracking APIs Not Configured",
                "Add at least one carrier's credentials to .env:\n\n"
                "  UPS_CLIENT_ID=...\n"
                "  UPS_CLIENT_SECRET=...\n\n"
                "  FEDEX_CLIENT_ID=...\n"
                "  FEDEX_CLIENT_SECRET=...\n\n"
                "  USPS_CLIENT_ID=...\n"
                "  USPS_CLIENT_SECRET=...\n\n"
                "Register free at:\n"
                "  UPS:   developer.ups.com\n"
                "  FedEx: developer.fedex.com\n"
                "  USPS:  developer.usps.com"
            )
            return
        if hasattr(self, "_tracking_worker") and self._tracking_worker.isRunning():
            return
        self._btn_refresh_tracking.setEnabled(False)
        self._btn_refresh_tracking.setText("Refreshing…")
        self._tracking_worker = TrackingRefreshWorker()
        self._tracking_worker.finished.connect(self._on_tracking_done)
        self._tracking_worker.start()

    def _on_tracking_done(self, counts: dict):
        self._btn_refresh_tracking.setEnabled(True)
        self._btn_refresh_tracking.setText("↻ Refresh Tracking")
        # Defer the refresh to the next event-loop tick to avoid native access
        # violations when matplotlib tries to redraw while Qt is mid-signal-chain.
        QTimer.singleShot(0, self._do_post_tracking_refresh)

    def _do_post_tracking_refresh(self):
        try:
            self.load_data()
            self.refresh_needed.emit()
        except Exception as e:
            print(f"[TrackingDone] UI refresh error: {e}")

    def _open_manual_scrape(self):
        dlg = ManualScrapeDialog(self)
        dlg.scrape_requested.connect(self._on_scrape_done)
        dlg.exec()

    def _on_scrape_done(self, _retailer: str, _days: int):
        self.load_data()
        self.refresh_needed.emit()

    def _add(self):
        dlg = InboundDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _export_csv(self):
        headers = ["Item", "Retailer", "Order #", "Status", "Cost", "Qty",
                   "Order Date", "Tracking", "Delivery Address"]
        rows = [
            [r["item_name"], r["retailer"], r["order_number"], r["status"],
             db.format_money(r["cost_cents"]), r["quantity"],
             _fmt_date(r["order_date"]), r.get("tracking_number") or "",
             r.get("delivery_address") or ""]
            for r in self._raw
        ]
        _write_csv(self, headers, rows, "orders.csv")

    def load_data(self):
        self._raw = db.get_inbound_orders()
        idx = self._sub_tabs.currentIndex()
        if idx == 0:
            self._apply_filter()
        elif idx == 1:
            self._refresh_shipments()
        elif idx == 2:
            self._refresh_pickups()


# ── Tab: Inventory ────────────────────────────────────────────────────────────

INV_HEADERS = ["Item Name", "SKU", "Category", "Condition",
               "Purchase Price", "Location", "Date Ordered"]


class CategoryPieChart(FigureCanvas):
    """Pie chart showing inventory units per category, styled to match the app theme."""

    _COLORS = ["#99f7ff", "#99f7ff", "#f7c99e", "#99ffc3", "#f79eb8",
               "#c3f799", "#f7e999", "#b8a0f7", "#80e8e8", "#d4aaff"]

    def __init__(self, parent=None):
        self._fig = Figure(figsize=(3, 3.2), facecolor=theme.BG_CARD)
        super().__init__(self._fig)
        self.setParent(parent)
        self.setFixedWidth(240)
        self.setFixedHeight(280)
        self.setStyleSheet(f"background: {theme.BG_CARD}; border-radius: 10px;")
        self._ax = self._fig.add_subplot(111)
        self._fig.subplots_adjust(top=0.88, bottom=0.24, left=0.05, right=0.95)
        self._draw_empty()

    def _draw_empty(self):
        self._ax.clear()
        self._ax.set_facecolor(theme.BG_CARD)
        self._ax.text(0.5, 0.5, "No data", ha="center", va="center",
                      color=theme.TEXT_MUTED, fontsize=9,
                      transform=self._ax.transAxes)
        self._ax.axis("off")
        self._fig.canvas.draw_idle()

    def update_data(self, items: list):
        try:
            self._update_data_inner(items)
        except Exception as e:
            print(f"[CategoryPieChart] render error: {e}")

    def _update_data_inner(self, items):
        self._ax.clear()
        self._ax.set_facecolor(theme.BG_CARD)

        # Aggregate units by category
        totals: dict = {}
        for item in items:
            cat = (item.get("category") or "Other").title()
            totals[cat] = totals.get(cat, 0) + max(item.get("quantity", 0), 0)

        totals = {k: v for k, v in totals.items() if v > 0}
        if not totals:
            self._draw_empty()
            return

        labels = list(totals.keys())
        sizes  = list(totals.values())
        colors = [self._COLORS[i % len(self._COLORS)] for i in range(len(labels))]

        wedges, _ = self._ax.pie(
            sizes,
            colors=colors,
            startangle=90,
            wedgeprops={"linewidth": 1.5, "edgecolor": theme.BG_CARD},
        )

        # Title
        self._ax.set_title("By Category", color=theme.TEXT_SECONDARY,
                            fontsize=9, pad=8)

        # Legend below the pie — inside figure bounds, labels truncated to fit
        self._ax.legend(
            wedges, [f"{l[:13]} ({v})" for l, v in zip(labels, sizes)],
            loc="upper center",
            bbox_to_anchor=(0.5, -0.04),
            ncol=2,
            fontsize=7,
            frameon=False,
            labelcolor=theme.TEXT_SECONDARY,
        )

        self._fig.canvas.draw_idle()


class InventoryCard(QFrame):
    """Grid-view card showing a grouped product summary."""
    clicked = pyqtSignal(str)   # emits item_name

    def __init__(self, item_name: str, total_qty: int, avg_cost_cents: int,
                 total_value_cents: int, parent=None):
        super().__init__(parent)
        self.item_name = item_name
        self.setObjectName("orderCard")
        self.setMinimumSize(180, 120)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"""
            QFrame#orderCard {{
                background: {theme.BG_CARD};
                border: 1px solid {theme.BG_CARD};
                border-radius: 10px;
                border-left: 3px solid {theme.BLUE};
            }}
            QFrame#orderCard:hover {{
                background: {theme.BG_ELEVATED};
                border-color: {theme.BORDER};
                border-left-color: {theme.BLUE};
            }}
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)

        name_lbl = QLabel(item_name)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        lay.addWidget(name_lbl)

        lay.addStretch()

        qty_lbl = QLabel(f"{total_qty} unit{'s' if total_qty != 1 else ''} in stock")
        qty_lbl.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;"
        )
        lay.addWidget(qty_lbl)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 2, 0, 0)
        avg_lbl = QLabel(f"Avg {db.format_money(avg_cost_cents)}/unit")
        avg_lbl.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;"
        )
        bottom.addWidget(avg_lbl)
        bottom.addStretch()
        val_lbl = QLabel(db.format_money(total_value_cents))
        val_lbl.setStyleSheet(
            f"color: {theme.GREEN}; font-size: 10px; font-weight: 600; background: transparent;"
        )
        bottom.addWidget(val_lbl)
        lay.addLayout(bottom)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.item_name)


class InventoryTab(QWidget):
    refresh_needed = pyqtSignal()
    _GRID_COLS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw: List[Dict] = []
        self._all_rows: List[List] = []
        self._view_mode = "list"   # "list" | "grid"
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        lbl = QLabel("Current Inventory")
        lbl.setObjectName("lblHeader")
        hdr.addWidget(lbl)
        hdr.addStretch()

        # View toggle buttons
        self._btn_list_view = QPushButton("☰  List")
        self._btn_grid_view = QPushButton("⊞  Grid")
        for btn in (self._btn_list_view, self._btn_grid_view):
            btn.setFixedHeight(28)
            btn.setCheckable(True)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {theme.BG_ELEVATED}; color: {theme.TEXT_SECONDARY};
                    border: 1px solid {theme.BORDER}; border-radius: 5px;
                    padding: 0 12px; font-size: 10px;
                }}
                QPushButton:checked {{
                    background: {theme.BG_CARD}; color: {theme.BLUE}; border-color: {theme.BLUE};
                }}
                QPushButton:hover:!checked {{
                    color: {theme.TEXT_PRIMARY};
                }}
            """)
        self._btn_list_view.setChecked(True)
        self._btn_list_view.clicked.connect(lambda: self._set_view("list"))
        self._btn_grid_view.clicked.connect(lambda: self._set_view("grid"))
        hdr.addWidget(self._btn_list_view)
        hdr.addWidget(self._btn_grid_view)

        hdr.addSpacing(8)
        btn_export = QPushButton("Export CSV")
        btn_export.setFixedHeight(28)
        btn_export.clicked.connect(self._export_csv)
        hdr.addWidget(btn_export)
        self._btn_sell = QPushButton("Mark as Sold")
        self._btn_sell.setObjectName("btnAdd")
        self._btn_sell.clicked.connect(self._mark_sold)
        self._btn_sell.setEnabled(False)
        hdr.addWidget(self._btn_sell)
        btn_add = QPushButton("+ Add Item")
        btn_add.setObjectName("btnAdd")
        btn_add.clicked.connect(self._add)
        hdr.addWidget(btn_add)
        layout.addLayout(hdr)

        self._filter = FilterBar([
            {"label": "Search",       "type": "text",  "key": "search"},
            {"label": "Category",     "type": "text",  "key": "category"},
        ])
        self._filter.changed.connect(self._apply_filter)
        layout.addWidget(self._filter)

        # ── Content row: table/grid stack + pie chart ─────────────────────────
        content_row = QHBoxLayout()
        content_row.setSpacing(12)

        # Stacked widget: index 0 = list, index 1 = grid
        self._stack = QStackedWidget()

        # ── List view ──────────────────────────────────────────────────────────
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._stack.addWidget(self._table)

        # ── Grid view ──────────────────────────────────────────────────────────
        grid_container = QWidget()
        grid_container.setStyleSheet("background: transparent;")
        grid_outer = QVBoxLayout(grid_container)
        grid_outer.setContentsMargins(0, 0, 0, 0)

        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_scroll.setStyleSheet("background: transparent;")
        self._grid_scroll.viewport().setStyleSheet("background: transparent;")

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background: transparent;")
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setSpacing(12)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for c in range(self._GRID_COLS):
            self._grid_layout.setColumnStretch(c, 1)

        self._grid_scroll.setWidget(self._grid_widget)
        grid_outer.addWidget(self._grid_scroll)
        self._stack.addWidget(grid_container)

        content_row.addWidget(self._stack, 1)

        # ── Pie chart panel ────────────────────────────────────────────────────
        chart_panel = QWidget()
        chart_panel.setFixedWidth(240)
        chart_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        chart_panel.setStyleSheet(
            f"background: {theme.BG_CARD}; border-radius: 10px; border: 1px solid {theme.BORDER};"
        )
        chart_vlay = QVBoxLayout(chart_panel)
        chart_vlay.setContentsMargins(0, 4, 0, 4)
        chart_vlay.setSpacing(0)

        self._pie_chart = CategoryPieChart(chart_panel)
        chart_vlay.addWidget(self._pie_chart)

        content_row.addWidget(chart_panel, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(content_row)

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def _set_view(self, mode: str):
        self._view_mode = mode
        self._btn_list_view.setChecked(mode == "list")
        self._btn_grid_view.setChecked(mode == "grid")
        self._btn_sell.setVisible(mode == "list")
        self._stack.setCurrentIndex(0 if mode == "list" else 1)
        self._apply_filter()

    def _export_csv(self):
        rows = [
            [r["item_name"], r.get("sku") or "", r["category"], r["condition"],
             db.format_money(r["cost_basis_cents"]), r.get("storage_location") or "",
             _fmt_date(r.get("order_date") or r.get("date_received") or ""), r["quantity"]]
            for r in self._filtered_raw
        ]
        _write_csv(self, INV_HEADERS + ["Qty"], rows, "inventory.csv")

    def load_data(self):
        scroll = self._table.verticalScrollBar().value()
        self._raw = db.get_inventory()

        rows = []
        for r in self._raw:
            rows.append([
                r["item_name"], r.get("sku") or "",
                r["category"], r["condition"],
                db.format_money(r["cost_basis_cents"]),
                r.get("storage_location") or "", _fmt_date(r.get("order_date") or r.get("date_received") or ""),
            ])
        self._all_rows = rows
        self._apply_filter()
        self._table.verticalScrollBar().setValue(scroll)

    def _apply_filter(self):
        vals = self._filter.values()
        search   = vals.get("search", "").lower()
        category = vals.get("category", "").lower()


        filtered_raw  = []
        filtered_rows = []
        for i, r in enumerate(self._raw):
            if category and category not in r["category"].lower():
                continue
            if search:
                haystack = f"{r['item_name']} {r.get('sku', '')} {r['category']}".lower()
                if search not in haystack:
                    continue
            filtered_raw.append(r)
            filtered_rows.append(self._all_rows[i])

        self._filtered_raw = filtered_raw
        self._pie_chart.update_data(filtered_raw)

        if self._view_mode == "list":
            self._proxy.setSourceModel(RecordTableModel(INV_HEADERS, filtered_rows))
            self._table.setModel(self._proxy)
            self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)
            self._table.resizeColumnsToContents()
            self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        else:
            self._refresh_grid(filtered_raw)

    def _refresh_grid(self, items: List[Dict]):
        # Clear existing cards
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not items:
            empty = QLabel("No inventory items match the current filter.")
            empty.setStyleSheet(
                f"color: {theme.TEXT_SECONDARY}; font-size: 13px; background: transparent;"
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid_layout.addWidget(empty, 0, 0, 1, self._GRID_COLS)
            return

        # Group by item_name
        groups: Dict[str, Dict] = {}
        for r in items:
            name = r["item_name"]
            if name not in groups:
                groups[name] = {"qty": 0, "cost_sum": 0}
            groups[name]["qty"]      += r["quantity"]
            groups[name]["cost_sum"] += r["cost_basis_cents"] * r["quantity"]

        # Sort alphabetically
        for i, (name, g) in enumerate(sorted(groups.items())):
            total_qty         = g["qty"]
            total_value_cents = g["cost_sum"]
            avg_cost_cents    = (total_value_cents // total_qty) if total_qty else 0

            card = InventoryCard(name, total_qty, avg_cost_cents, total_value_cents)
            card.clicked.connect(self._on_grid_card_clicked)
            row, col = divmod(i, self._GRID_COLS)
            self._grid_layout.addWidget(card, row, col)

    def _on_grid_card_clicked(self, item_name: str):
        # Pre-fill the search filter and jump to list view showing just that item
        _, search_widget = self._filter._widgets["search"]
        search_widget.setText(item_name)
        self._set_view("list")

    def _on_selection_changed(self):
        self._btn_sell.setEnabled(bool(self._table.selectionModel().selectedRows()))

    def _get_raw(self, proxy_row: int) -> Optional[Dict]:
        try:
            source_row = self._proxy.mapToSource(self._proxy.index(proxy_row, 0)).row()
            return self._filtered_raw[source_row]
        except (IndexError, AttributeError):
            return None

    def _get_selected_raw(self) -> List[Dict]:
        result = []
        for idx in self._table.selectionModel().selectedRows():
            source_row = self._proxy.mapToSource(self._proxy.index(idx.row(), 0)).row()
            if 0 <= source_row < len(self._filtered_raw):
                result.append(self._filtered_raw[source_row])
        return result

    def _on_double_click(self, index: QModelIndex):
        r = self._get_raw(index.row())
        if r:
            self._edit(r)

    def _add(self):
        dlg = InventoryDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _edit(self, r: Dict):
        dlg = InventoryDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _mark_sold(self):
        items = self._get_selected_raw()
        if not items:
            return
        dlg = BulkSaleDialog(items, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        selected = self._get_selected_raw()
        r = self._get_raw(index.row())
        if not r:
            return

        # Make sure right-clicked row is included in selection
        targets = selected if selected else [r]

        menu = QMenu(self)
        if len(targets) > 1:
            n = len(targets)
            menu.addAction(f"Mark {n} Items as Sold").triggered.connect(self._mark_sold)
            menu.addSeparator()
            # Bulk field edits
            menu.addAction(f"Set Category ({n} items)…").triggered.connect(
                lambda: self._bulk_set_category(targets)
            )
            cond_menu = menu.addMenu(f"Set Condition ({n} items)")
            for cond in CONDITIONS:
                cond_menu.addAction(cond.title()).triggered.connect(
                    lambda checked, cv=cond: self._bulk_set_field(targets, "condition", cv)
                )
            loc_act = menu.addAction(f"Set Storage Location ({n} items)…")
            loc_act.triggered.connect(lambda: self._bulk_set_location(targets))
            menu.addSeparator()
            menu.addAction(f"Delete ({n} items)").triggered.connect(
                lambda: self._delete_items(targets)
            )
        else:
            menu.addAction("Mark as Sold").triggered.connect(lambda: self._mark_sold())
            menu.addAction("List for Sale").triggered.connect(lambda: self._open_ebay_sold(r))
            menu.addSeparator()
            menu.addAction("Edit").triggered.connect(lambda: self._edit(r))
            menu.addAction("Copy Item Name").triggered.connect(
                lambda: QApplication.clipboard().setText(r["item_name"])
            )
            menu.addSeparator()
            existing_reminder = db.get_active_reminder_for_inventory(r["id"])
            if existing_reminder:
                menu.addAction("Cancel Return Reminder").triggered.connect(
                    lambda: self._cancel_return_reminder(r)
                )
            else:
                menu.addAction("Set Return Reminder").triggered.connect(
                    lambda: self._set_return_reminder(r)
                )
            menu.addSeparator()
            menu.addAction("Delete").triggered.connect(lambda: self._delete_items([r]))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _set_return_reminder(self, r: Dict):
        order_date = r.get("order_date") or r.get("date_received") or ""
        if not order_date:
            QMessageBox.warning(self, "No Order Date",
                                "This item has no order date — cannot set a reminder.")
            return
        days, ok = QInputDialog.getInt(
            self, "Return Reminder",
            "Start daily notifications after how many days?",
            value=30, min=1, max=365,
        )
        if not ok:
            return
        db.set_return_reminder(r["id"], r["item_name"], order_date, days)
        import webhooks
        webhooks.notify_return_reminder_set(r["item_name"])
        QMessageBox.information(self, "Reminder Set",
                                f"Return reminder set for:\n{r['item_name']}\n\n"
                                f"Daily notifications start {days} days after {order_date}.")

    def _cancel_return_reminder(self, r: Dict):
        db.cancel_return_reminder(r["id"])
        QMessageBox.information(self, "Reminder Cancelled",
                                f"Return reminder cancelled for:\n{r['item_name']}")

    def _open_ebay_sold(self, r: Dict):
        from urllib.parse import quote_plus
        query = quote_plus(r["item_name"])
        url = f"https://www.ebay.com/sch/i.html?_nkw={query}&LH_Sold=1&LH_Complete=1&_sop=13"
        QDesktopServices.openUrl(QUrl(url))

    def _bulk_set_field(self, targets: List[Dict], field: str, value: str):
        for item in targets:
            db.adjust_inventory(item["id"], field, value)
        self.load_data()
        self.refresh_needed.emit()

    def _bulk_set_category(self, targets: List[Dict]):
        suggestions = sorted({i["category"] for i in db.get_inventory() if i.get("category")})
        cat = _ask_category("Set Category", "Category for selected items:", suggestions, self)
        if cat:
            self._bulk_set_field(targets, "category", cat)

    def _bulk_set_location(self, targets: List[Dict]):
        loc, ok = QInputDialog.getText(self, "Set Storage Location",
                                       "Storage location for selected items:")
        if ok:
            self._bulk_set_field(targets, "storage_location", loc.strip())

    def _delete_items(self, items: List[Dict]):
        unique = list({item["id"]: item for item in items}.values())
        n = len(unique)
        msg = (f"Soft-delete {n} inventory item{'s' if n > 1 else ''}?\n(Data is retained but hidden.)"
               if n > 1 else "Soft-delete this inventory item?")
        if QMessageBox.question(self, "Confirm", msg,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            for item in unique:
                db.delete_inventory(item["id"])
            self.load_data()
            self.refresh_needed.emit()


# ── Link Inventory Dialog ─────────────────────────────────────────────────────

class LinkInventoryDialog(QDialog):
    """Fuzzy-search picker to link a sale to an inventory item."""

    def __init__(self, sale_name: str, parent=None, max_qty: int = 1):
        super().__init__(parent)
        self.setWindowTitle("Link to Inventory Item")
        self.setMinimumSize(560, 420)
        self.selected_inventory_id: Optional[int] = None
        self.selected_qty: int = 1
        self._sale_name = sale_name
        self._max_qty = max_qty
        self._inv = db.get_inventory()
        self._build()

    def _score(self, inv_name: str) -> float:
        import re as _re
        def tokens(s):
            return set(_re.sub(r"[^\w\s]", "", s.lower()).split())
        a = tokens(self._sale_name)
        b = tokens(inv_name)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        title = QLabel(f"Linking sale: <b>{self._sale_name}</b>")
        title.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 12px;")
        title.setWordWrap(True)
        lay.addWidget(title)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search inventory…")
        self._search.setText(self._sale_name)
        self._search.textChanged.connect(self._refresh)
        lay.addWidget(self._search)

        if self._max_qty > 1:
            qty_row = QHBoxLayout()
            qty_lbl = QLabel("Quantity to link:")
            qty_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
            self._qty_spin = QSpinBox()
            self._qty_spin.setRange(1, self._max_qty)
            self._qty_spin.setValue(self._max_qty)
            qty_row.addWidget(qty_lbl)
            qty_row.addWidget(self._qty_spin)
            qty_row.addStretch()
            lay.addLayout(qty_row)

        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(28)
        self._table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self._table)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._refresh()

    def _refresh(self):
        query = self._search.text().strip().lower()
        scored = []
        for r in self._inv:
            name = r["item_name"]
            if query and query not in name.lower():
                # Still include if there's a decent score even without substring match
                score = self._score(name)
                if score < 0.1:
                    continue
            else:
                score = self._score(name)
            scored.append((score, r))
        scored.sort(key=lambda x: -x[0])

        headers = ["Item Name", "Qty Avail", "Purchase Price", "Date Ordered"]
        rows = [
            [r["item_name"],
             str(r.get("quantity", 0)),
             db.format_money(r["cost_basis_cents"]),
             _fmt_date(r.get("order_date") or r.get("date_received") or "")]
            for _, r in scored
        ]
        self._scored_raw = [r for _, r in scored]
        model = RecordTableModel(headers, rows)
        self._table.setModel(model)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        if rows:
            self._table.selectRow(0)

    def _accept(self):
        idx = self._table.currentIndex()
        if not idx.isValid():
            return
        row = idx.row()
        if 0 <= row < len(self._scored_raw):
            self.selected_inventory_id = self._scored_raw[row]["id"]
            self.selected_qty = self._qty_spin.value() if hasattr(self, "_qty_spin") else 1
            self.accept()


# ── Tab: Outbound Sales ───────────────────────────────────────────────────────

LISTING_HEADERS   = ["Item Name", "Platform", "List Price", "Est. Payout",
                     "Status", "Size", "Listing ID", "Date Listed", "Source"]
LISTING_STATUSES  = ["active", "sold", "ended", "cancelled"]
LISTING_STATUS_COL = 4

SALE_HEADERS = ["Item Name", "Platform", "Qty", "Sale Price", "Fees", "Shipping",
                "Purchase Price", "Profit", "Margin %", "Status", "Tracking",
                "Buyer", "Date Ordered", "Date Sold"]
SALE_STATUS_COL   = 9
SALE_TRACKING_COL = 10


# ── Listing dialog ─────────────────────────────────────────────────────────────

class ListingDialog(QDialog):
    def __init__(self, parent=None, row: Optional[Dict] = None):
        super().__init__(parent)
        self._row = row
        self.setWindowTitle("Edit Listing" if row else "Add Listing")
        self.setMinimumWidth(420)
        self._build()

    def _build(self):
        layout = QFormLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 18, 18, 18)

        r = self._row or {}

        self.item_name = QLineEdit(r.get("item_name", ""))
        self.item_name.setPlaceholderText("Item / product name")
        layout.addRow("Item Name *", self.item_name)

        self.platform = QComboBox()
        self.platform.addItems([p.title() for p in PLATFORMS])
        if r.get("platform"):
            idx = self.platform.findText(r["platform"].title(), Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self.platform.setCurrentIndex(idx)
        layout.addRow("Platform", self.platform)

        self.list_price = QDoubleSpinBox()
        self.list_price.setRange(0, 999999)
        self.list_price.setDecimals(2)
        self.list_price.setPrefix("$")
        if r.get("listing_price_cents"):
            self.list_price.setValue(r["listing_price_cents"] / 100)
        layout.addRow("List Price", self.list_price)

        self.status = QComboBox()
        self.status.addItems([s.title() for s in LISTING_STATUSES])
        if r.get("status"):
            idx = self.status.findText(r["status"].title(), Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self.status.setCurrentIndex(idx)
        layout.addRow("Status", self.status)

        self.listing_id = QLineEdit(r.get("listing_id") or "")
        self.listing_id.setPlaceholderText("Platform listing / item number (optional)")
        layout.addRow("Listing ID", self.listing_id)

        self.date_listed = QLineEdit(r.get("date_listed") or datetime.now().strftime("%Y-%m-%d"))
        self.date_listed.setPlaceholderText("YYYY-MM-DD")
        layout.addRow("Date Listed", self.date_listed)

        self.notes = QLineEdit(r.get("notes") or "")
        self.notes.setPlaceholderText("Optional notes")
        layout.addRow("Notes", self.notes)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def _save(self):
        name = self.item_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation", "Item Name is required.")
            return
        platform = self.platform.currentText().lower()
        price_c  = db.dollars_to_cents(self.list_price.value())
        status   = self.status.currentText().lower()
        lid      = self.listing_id.text().strip() or None
        date_l   = self.date_listed.text().strip() or None
        notes    = self.notes.text().strip() or None

        if self._row:
            db.update_listing(self._row["id"], "item_name", name)
            db.update_listing(self._row["id"], "platform", platform)
            db.update_listing(self._row["id"], "listing_price_cents", price_c)
            db.update_listing(self._row["id"], "status", status)
            db.update_listing(self._row["id"], "listing_id", lid)
            db.update_listing(self._row["id"], "date_listed", date_l)
            db.update_listing(self._row["id"], "notes", notes)
        else:
            db.add_listing(
                item_name=name, platform=platform,
                listing_price_cents=price_c, listing_id=lid,
                date_listed=date_l, notes=notes,
            )
        self.accept()


# ── Listings sub-tab ──────────────────────────────────────────────────────────

class _ListingsSubTab(QWidget):
    refresh_needed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw: List[Dict] = []
        self._all_rows: List[List] = []
        self._filtered_raw: List[Dict] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)

        self._filter = FilterBar([
            {"label": "Search",   "type": "text",  "key": "search"},
            {"label": "Platform", "type": "combo", "key": "platform", "choices": PLATFORMS},
            {"label": "Status",   "type": "combo", "key": "status",   "choices": LISTING_STATUSES},
        ])
        self._filter.changed.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.clicked.connect(self._on_click)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        layout.addWidget(self._table)

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def load_data(self):
        scroll = self._table.verticalScrollBar().value()
        self._raw = db.get_listings()
        rows = []
        for r in self._raw:
            payout = r.get("estimated_payout_cents")
            rows.append([
                r["item_name"],
                r["platform"].title(),
                db.format_money(r["listing_price_cents"]),
                db.format_money(payout) if payout else "—",
                r["status"],
                r.get("size_variant") or "",
                r.get("listing_id") or "",
                _fmt_date(r.get("date_listed") or ""),
                r.get("source", "manual"),
            ])
        self._all_rows = rows
        self._apply_filter()
        self._table.verticalScrollBar().setValue(scroll)

    def _apply_filter(self):
        vals     = self._filter.values()
        search   = vals.get("search", "").lower()
        platform = vals.get("platform", "")
        status   = vals.get("status", "")

        filtered_raw  = []
        filtered_rows = []
        for i, r in enumerate(self._raw):
            if platform and r["platform"] != platform:
                continue
            if status and r["status"] != status:
                continue
            if search:
                haystack = f"{r['item_name']} {r['platform']} {r.get('listing_id', '')}".lower()
                if search not in haystack:
                    continue
            filtered_raw.append(r)
            filtered_rows.append(self._all_rows[i])

        self._filtered_raw = filtered_raw
        model = RecordTableModel(LISTING_HEADERS, filtered_rows)
        self._proxy.setSourceModel(model)
        self._table.setModel(self._proxy)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    def _export_csv(self):
        rows = []
        for r in self._filtered_raw:
            payout = r.get("estimated_payout_cents")
            rows.append([
                r["item_name"], r["platform"].title(),
                db.format_money(r["listing_price_cents"]),
                db.format_money(payout) if payout else "",
                r["status"], r.get("size_variant") or "",
                r.get("listing_id") or "", _fmt_date(r.get("date_listed") or ""),
                r.get("source", "manual"),
            ])
        _write_csv(self, LISTING_HEADERS, rows, "listings.csv")

    def _get_raw(self, proxy_row: int) -> Optional[Dict]:
        try:
            src = self._proxy.mapToSource(self._proxy.index(proxy_row, 0)).row()
            return self._filtered_raw[src]
        except (IndexError, AttributeError):
            return None

    def _get_selected_raw(self) -> List[Dict]:
        result = []
        for idx in self._table.selectionModel().selectedRows():
            src = self._proxy.mapToSource(self._proxy.index(idx.row(), 0)).row()
            if 0 <= src < len(self._filtered_raw):
                result.append(self._filtered_raw[src])
        return result

    def _on_click(self, index: QModelIndex):
        if index.column() == LISTING_STATUS_COL:
            r = self._get_raw(index.row())
            if not r:
                return
            menu = QMenu(self)
            for s in LISTING_STATUSES:
                a = QAction(s.title(), menu)
                a.setCheckable(True)
                a.setChecked(r["status"] == s)
                a.setData(s)
                menu.addAction(a)
            chosen = menu.exec(QCursor.pos())
            if chosen:
                db.update_listing(r["id"], "status", chosen.data())
                self.load_data()

    def _on_double_click(self, index: QModelIndex):
        if index.column() == LISTING_STATUS_COL:
            return
        r = self._get_raw(index.row())
        if r:
            self._edit(r)

    def _add(self):
        dlg = ListingDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()

    def _edit(self, r: Dict):
        dlg = ListingDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()

    def _context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        r = self._get_raw(index.row())
        if not r:
            return
        selected = self._get_selected_raw()
        targets  = selected if selected else [r]
        n        = len(targets)
        menu     = QMenu(self)
        if n == 1:
            menu.addAction("Edit").triggered.connect(lambda: self._edit(r))
            menu.addSeparator()
        status_menu = menu.addMenu(f"Set Status ({n} listing{'s' if n > 1 else ''})")
        for s in LISTING_STATUSES:
            a = status_menu.addAction(s.title())
            a.triggered.connect(lambda checked, st=s: self._bulk_set_status(targets, st))
        menu.addSeparator()
        menu.addAction(f"Delete ({n} listing{'s' if n > 1 else ''})").triggered.connect(
            lambda: self._bulk_delete(targets)
        )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _bulk_set_status(self, targets: List[Dict], status: str):
        for r in targets:
            db.update_listing(r["id"], "status", status)
        self.load_data()

    def _bulk_delete(self, targets: List[Dict]):
        n = len(targets)
        if QMessageBox.question(
            self, "Confirm", f"Delete {n} listing{'s' if n > 1 else ''}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            for r in targets:
                db.delete_listing(r["id"])
            self.load_data()


class _SalesRecordsTab(QWidget):
    refresh_needed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw: List[Dict] = []
        self._all_rows: List[List] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)

        self._filter = FilterBar([
            {"label": "Search",        "type": "text",  "key": "search"},
            {"label": "Platform",      "type": "combo", "key": "platform", "choices": PLATFORMS},
            {"label": "Status",        "type": "combo", "key": "status",   "choices": SALE_STATUSES},
        ])
        self._filter.changed.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.clicked.connect(self._on_click)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        layout.addWidget(self._table)

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def load_data(self):
        scroll = self._table.verticalScrollBar().value()
        self._raw = db.get_sales()

        rows = []
        for r in self._raw:
            rows.append([
                r["item_name"], r["platform"].title(),
                str(r.get("quantity") or 1),
                db.format_money(r["sale_price_cents"]),
                db.format_money(r["platform_fees_cents"]),
                db.format_money(r["shipping_cost_cents"]),
                db.format_money(r["cost_basis_cents"]),
                db.format_money(r["profit_cents"]),
                f"{r['margin_percent']:.1f}%",
                r["status"],
                r.get("tracking_number") or "",
                r.get("buyer_info") or "",
                _fmt_date(r.get("date_listed") or ""),
                _fmt_date(r.get("date_sold") or ""),
            ])
        self._all_rows = rows
        self._apply_filter()
        self._table.verticalScrollBar().setValue(scroll)

    def _apply_filter(self):
        vals = self._filter.values()
        search        = vals.get("search", "").lower()
        platform      = vals.get("platform", "")
        status        = vals.get("status", "")


        filtered_raw  = []
        filtered_rows = []
        for i, r in enumerate(self._raw):
            if platform and r["platform"] != platform:
                continue
            if status and r["status"] != status:
                continue
            if search:
                haystack = f"{r['item_name']} {r['platform']} {r.get('buyer_info', '')}".lower()
                if search not in haystack:
                    continue
            filtered_raw.append(r)
            filtered_rows.append(self._all_rows[i])

        self._filtered_raw = filtered_raw
        self._proxy.setSourceModel(SalesTableModel(SALE_HEADERS, filtered_rows, filtered_raw))
        self._table.setModel(self._proxy)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    def _import_csv(self):
        """Import sales from a CSV file.

        Expected columns (header names are flexible, matched case-insensitively):
          Item, Cost, Date purchased, Sale Price, Sale Date, Fees, Shipping
        Identical rows are batched into a single sale with summed quantity.
        """
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Sales CSV", "", "CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return

        try:
            import csv as _csv
            from collections import defaultdict as _dd
            from datetime import datetime as _dt

            def _parse_date(raw: str) -> str:
                raw = raw.strip()
                for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
                    try:
                        return _dt.strptime(raw, fmt).strftime("%Y-%m-%d")
                    except ValueError:
                        continue
                return raw

            # ── Read & batch ──────────────────────────────────────────────
            with open(path, newline="", encoding="utf-8") as f:
                reader = _csv.DictReader(f)
                # Normalise header keys
                batched: dict = _dd(int)
                row_count = 0
                for row in reader:
                    row_count += 1
                    # Support flexible column names
                    lk = {k.strip().lower(): v.strip() for k, v in row.items()}
                    key = (
                        lk.get("item") or lk.get("item_name") or lk.get("item name") or "",
                        lk.get("cost") or lk.get("cost_basis") or "0",
                        lk.get("date purchased") or lk.get("date_listed") or "",
                        lk.get("sale price") or lk.get("sale_price") or "0",
                        lk.get("sale date") or lk.get("date_sold") or "",
                        lk.get("fees") or lk.get("platform_fees") or "0",
                        lk.get("shipping") or lk.get("shipping_cost") or "0",
                        lk.get("platform") or "direct",
                    )
                    batched[key] += 1

            # ── Import ────────────────────────────────────────────────────
            imported = 0
            for (item, cost, date_purch, sale_price, sale_date,
                 fees, shipping, platform), qty in batched.items():
                if not item:
                    continue
                cost_cents  = db.dollars_to_cents(float(cost or 0))
                sale_cents  = db.dollars_to_cents(float(sale_price or 0))
                fees_cents  = db.dollars_to_cents(float(fees or 0))
                ship_cents  = db.dollars_to_cents(float(shipping or 0))
                total_cost  = cost_cents * qty
                d_sold      = _parse_date(sale_date) if sale_date else None
                d_listed    = _parse_date(date_purch) if date_purch else None

                sale_id = db.add_sale_from_email(
                    item_name           = item,
                    platform            = platform,
                    sale_price_cents    = sale_cents * qty,
                    platform_fees_cents = fees_cents * qty,
                    shipping_cost_cents = ship_cents * qty,
                    date_sold           = d_sold,
                    quantity            = qty,
                )
                # Set known cost basis + profit
                from database import _conn, _calc_profit
                with _conn() as con:
                    profit, margin = _calc_profit(
                        sale_cents * qty, total_cost,
                        fees_cents * qty, ship_cents * qty,
                    )
                    con.execute(
                        """UPDATE outbound_sales
                           SET cost_basis_cents = ?, profit_cents = ?, margin_percent = ?,
                               date_listed = COALESCE(?, date_listed),
                               updated_at = datetime('now')
                           WHERE id = ?""",
                        (total_cost, profit, margin, d_listed, sale_id),
                    )
                imported += 1

            QMessageBox.information(
                self, "Import Complete",
                f"Imported {imported} sale records ({row_count} CSV rows) from:\n{path}"
            )
            self.load_data()
            self.refresh_needed.emit()

        except Exception as exc:
            QMessageBox.warning(self, "Import Failed", str(exc))

    def _export_csv(self):
        rows = [
            [r["item_name"], r["platform"].title(),
             str(r.get("quantity") or 1),
             db.format_money(r["sale_price_cents"]), db.format_money(r["platform_fees_cents"]),
             db.format_money(r["shipping_cost_cents"]), db.format_money(r["cost_basis_cents"]),
             db.format_money(r["profit_cents"]), f"{r['margin_percent']:.1f}%",
             r["status"], r.get("tracking_number") or "", r.get("buyer_info") or "",
             _fmt_date(r.get("date_listed") or ""), _fmt_date(r.get("date_sold") or "")]
            for r in self._filtered_raw
        ]
        _write_csv(self, SALE_HEADERS, rows, "sales.csv")

    def _get_raw(self, proxy_row: int) -> Optional[Dict]:
        try:
            source_row = self._proxy.mapToSource(self._proxy.index(proxy_row, 0)).row()
            return self._filtered_raw[source_row]
        except (IndexError, AttributeError):
            return None

    def _get_selected_raw(self) -> List[Dict]:
        result = []
        for idx in self._table.selectionModel().selectedRows():
            source_row = self._proxy.mapToSource(self._proxy.index(idx.row(), 0)).row()
            if 0 <= source_row < len(self._filtered_raw):
                result.append(self._filtered_raw[source_row])
        return result

    def _on_click(self, index: QModelIndex):
        if index.column() == SALE_TRACKING_COL:
            r = self._get_raw(index.row())
            if not r:
                return
            tn = (r.get("tracking_number") or "").strip()
            if tn:
                url = _tracking_url(tn)
                QDesktopServices.openUrl(QUrl(url))
            return
        if index.column() == SALE_STATUS_COL:
            r = self._get_raw(index.row())
            if not r:
                return
            menu = QMenu(self)
            for s in SALE_STATUSES:
                a = QAction(s.title(), menu)
                a.setCheckable(True)
                a.setChecked(r["status"] == s)
                a.setData(s)
                menu.addAction(a)
            chosen = menu.exec(QCursor.pos())
            if chosen:
                db.update_sale(r["id"], "status", chosen.data())
                self.load_data()
                self.refresh_needed.emit()

    def _on_double_click(self, index: QModelIndex):
        if index.column() == SALE_STATUS_COL:
            return
        r = self._get_raw(index.row())
        if r:
            self._edit(r)

    def _add(self):
        dlg = SaleDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _edit(self, r: Dict):
        dlg = SaleDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        r = self._get_raw(index.row())
        if not r:
            return
        selected = self._get_selected_raw()
        targets  = selected if selected else [r]
        n        = len(targets)
        menu     = QMenu(self)

        if n == 1:
            menu.addAction("Edit").triggered.connect(lambda: self._edit(r))
            sale_qty   = r.get("quantity") or 1
            linked_qty = db.get_sale_linked_qty(r["id"])
            remaining  = sale_qty - linked_qty
            if remaining > 0:
                label = (f"Link to Inventory ({linked_qty}/{sale_qty} linked)"
                         if linked_qty else "Link to Inventory")
                menu.addAction(label).triggered.connect(
                    lambda: self._link_to_inventory(r)
                )
            if linked_qty > 0:
                menu.addAction("View / Remove Links").triggered.connect(
                    lambda: self._view_links(r)
                )
            if r.get("tracking_number"):
                menu.addAction("Copy Tracking Number").triggered.connect(
                    lambda: QApplication.clipboard().setText(r["tracking_number"])
                )
            menu.addSeparator()

        status_menu = menu.addMenu(f"Set Status ({n} sale{'s' if n > 1 else ''})")
        for s in SALE_STATUSES:
            a = status_menu.addAction(s.title())
            a.triggered.connect(lambda checked, st=s: self._bulk_set_status(targets, st))

        menu.addSeparator()
        menu.addAction(f"Delete ({n} sale{'s' if n > 1 else ''})").triggered.connect(
            lambda: self._bulk_delete(targets)
        )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _bulk_set_status(self, targets: List[Dict], status: str):
        for r in targets:
            db.update_sale(r["id"], "status", status)
        self.load_data()
        self.refresh_needed.emit()

    def _bulk_delete(self, targets: List[Dict]):
        n = len(targets)
        if QMessageBox.question(
            self, "Confirm", f"Delete {n} sale{'s' if n > 1 else ''}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            for r in targets:
                db.delete_sale(r["id"])
            self.load_data()
            self.refresh_needed.emit()

    def _link_to_inventory(self, r: Dict):
        sale_qty   = r.get("quantity") or 1
        linked_qty = db.get_sale_linked_qty(r["id"])
        remaining  = max(1, sale_qty - linked_qty)

        dlg = LinkInventoryDialog(r["item_name"], self, max_qty=remaining)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_inventory_id:
            qty = dlg.selected_qty if hasattr(dlg, "selected_qty") else 1
            db.link_sale_to_inventory(r["id"], dlg.selected_inventory_id, qty=qty)
            self.load_data()
            self.refresh_needed.emit()

    def _view_links(self, r: Dict):
        links = db.get_sale_inventory_links(r["id"])
        if not links:
            QMessageBox.information(self, "Links", "No inventory links for this sale.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Inventory Links — {r['item_name'][:40]}")
        dlg.setMinimumWidth(500)
        lay = QVBoxLayout(dlg)
        for lk in links:
            row = QHBoxLayout()
            lbl = QLabel(
                f"{lk['quantity']}x {lk['inv_item_name']} @ {db.format_money(lk['cost_cents_each'])}/ea"
            )
            lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY};")
            btn = QPushButton("Remove")
            btn.setFixedWidth(70)
            link_id = lk["id"]
            btn.clicked.connect(lambda _checked, lid=link_id: self._remove_link(lid, dlg, r))
            row.addWidget(lbl, 1)
            row.addWidget(btn)
            lay.addLayout(row)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        dlg.exec()

    def _remove_link(self, link_id: int, dlg: QDialog, sale_row: Dict):
        db.unlink_sale_inventory(link_id)
        dlg.accept()
        self.load_data()
        self.refresh_needed.emit()
        self._view_links(db.get_sale_by_id(sale_row["id"]) or sale_row)

    def _delete(self, r: Dict):
        if QMessageBox.question(self, "Confirm", "Soft-delete this sale?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_sale(r["id"])
            self.load_data()
            self.refresh_needed.emit()


# ── Tab: Outbound Sales (outer wrapper with Listings + Sales sub-tabs) ────────

class SalesTab(QWidget):
    refresh_needed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Persistent header
        hdr = QHBoxLayout()
        lbl = QLabel("Outbound Sales")
        lbl.setObjectName("lblHeader")
        hdr.addWidget(lbl)
        hdr.addStretch()

        # Listings-tab buttons (visible when Listings is active)
        self._btn_listings_export = QPushButton("Export CSV")
        self._btn_listings_add    = QPushButton("+ Add Listing")
        self._btn_listings_add.setObjectName("btnAdd")
        hdr.addWidget(self._btn_listings_export)
        hdr.addWidget(self._btn_listings_add)

        # Sales-tab buttons (visible when Sales is active)
        self._btn_sales_scrape = QPushButton("⚙ Scrape Emails")
        self._btn_sales_import = QPushButton("Import CSV")
        self._btn_sales_export = QPushButton("Export CSV")
        self._btn_sales_add    = QPushButton("+ Record Sale")
        self._btn_sales_add.setObjectName("btnAdd")
        hdr.addWidget(self._btn_sales_scrape)
        hdr.addWidget(self._btn_sales_import)
        hdr.addWidget(self._btn_sales_export)
        hdr.addWidget(self._btn_sales_add)

        layout.addLayout(hdr)

        # Sub-tab bar
        self._tab_bar = QTabBar()
        self._tab_bar.addTab("Sales")
        self._tab_bar.addTab("Listings")
        self._tab_bar.currentChanged.connect(self._on_subtab_changed)
        layout.addWidget(self._tab_bar)

        # Stacked pages
        self._stack = QStackedWidget()
        self._listings_tab = _ListingsSubTab()
        self._sales_tab    = _SalesRecordsTab()
        self._stack.addWidget(self._sales_tab)
        self._stack.addWidget(self._listings_tab)
        layout.addWidget(self._stack)

        self._listings_tab.refresh_needed.connect(self.refresh_needed)
        self._sales_tab.refresh_needed.connect(self.refresh_needed)

        # Wire header buttons to sub-tab methods
        self._btn_listings_export.clicked.connect(self._listings_tab._export_csv)
        self._btn_listings_add.clicked.connect(self._listings_tab._add)
        self._btn_sales_scrape.clicked.connect(self._open_sale_scrape)
        self._btn_sales_import.clicked.connect(self._sales_tab._import_csv)
        self._btn_sales_export.clicked.connect(self._sales_tab._export_csv)
        self._btn_sales_add.clicked.connect(self._sales_tab._add)

        self._update_buttons(0)

    def _update_buttons(self, index: int):
        is_sales = (index == 0)
        self._btn_sales_scrape.setVisible(is_sales)
        self._btn_sales_import.setVisible(is_sales)
        self._btn_sales_export.setVisible(is_sales)
        self._btn_sales_add.setVisible(is_sales)
        self._btn_listings_export.setVisible(not is_sales)
        self._btn_listings_add.setVisible(not is_sales)

    def _open_sale_scrape(self):
        dlg = ManualScrapeDialog(self, retailers=ManualScrapeDialog._RETAILERS_SALES)
        dlg.scrape_requested.connect(self._on_scrape_done)
        dlg.exec()

    def _on_scrape_done(self, _retailer: str, _days: int):
        self._sales_tab.load_data()
        self.refresh_needed.emit()

    def load_data(self):
        idx = self._tab_bar.currentIndex()
        if idx == 0:
            self._sales_tab.load_data()
        else:
            self._listings_tab.load_data()

    def _on_subtab_changed(self, index: int):
        self._stack.setCurrentIndex(index)
        self._update_buttons(index)
        if index == 0:
            self._sales_tab.load_data()
        else:
            self._listings_tab.load_data()


# ── Tab: Business Expenses ───────────────────────────────────────────────────

RECURRENCE_INTERVALS = db.RECURRENCE_INTERVALS
EXP_HEADERS = ["Date", "Expense Name", "Category", "Vendor", "Amount",
               "Payment Method", "Recurring", "Notes"]


class ExpenseDialog(QDialog):
    def __init__(self, parent=None, row: Optional[Dict] = None):
        super().__init__(parent)
        self.row = row
        self.setWindowTitle("Edit Expense" if row else "Add Expense")
        self.setMinimumWidth(480)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        r = self.row or {}

        self.name     = QLineEdit(r.get("expense_name", ""))
        self.vendor   = QLineEdit(r.get("vendor", "") or "")
        self.payment  = QLineEdit(r.get("payment_method", "") or "")

        self.category = QComboBox()
        self.category.setEditable(True)
        for cat in ["General", "Inventory", "Shipping", "Packaging", "Software",
                    "Marketing", "Equipment", "Fees", "Travel", "Office", "Other"]:
            self.category.addItem(cat)
        saved_cat = r.get("category") or "General"
        idx = self.category.findText(saved_cat)
        if idx >= 0:
            self.category.setCurrentIndex(idx)
        else:
            self.category.setCurrentText(saved_cat)
        self.notes   = QTextEdit(r.get("notes", "") or "")
        self.notes.setFixedHeight(60)

        self.amount = QDoubleSpinBox()
        self.amount.setRange(0, 999999.99)
        self.amount.setDecimals(2)
        self.amount.setPrefix("$")
        if r.get("amount_cents") is not None:
            self.amount.setValue(db.cents_to_dollars(r["amount_cents"]))

        self.date = DateEdit()
        if r.get("expense_date"):
            self.date.setDate(QDate.fromString(r["expense_date"], "yyyy-MM-dd"))
        else:
            self.date.setDate(QDate.currentDate())

        # Recurring section
        self.recurring_chk = QCheckBox("Recurring Expense")
        self.recurring_chk.setChecked(bool(r.get("is_recurring", False)))

        self._rec_box = QGroupBox()
        self._rec_box.setFlat(True)
        rec_layout = QFormLayout(self._rec_box)
        rec_layout.setSpacing(8)

        self.interval = QComboBox()
        for i in RECURRENCE_INTERVALS:
            self.interval.addItem(i.title(), i)
        if r.get("recurrence_interval"):
            idx = self.interval.findData(r["recurrence_interval"])
            if idx >= 0:
                self.interval.setCurrentIndex(idx)

        self.lbl_next_due = QLabel("—")

        rec_layout.addRow(_make_label("Interval"), self.interval)
        rec_layout.addRow(_make_label("Next Due Date"), self.lbl_next_due)

        self.recurring_chk.toggled.connect(self._rec_box.setVisible)
        self.recurring_chk.toggled.connect(self._update_next_due)
        self.interval.currentIndexChanged.connect(self._update_next_due)
        self.date.dateChanged.connect(self._update_next_due)
        self._rec_box.setVisible(self.recurring_chk.isChecked())

        form.addRow(_make_label("Expense Name", True), self.name)
        form.addRow(_make_label("Amount", True),       self.amount)
        form.addRow(_make_label("Date", True),         self.date)
        form.addRow(_make_label("Category"),           self.category)
        form.addRow(_make_label("Vendor"),             self.vendor)
        form.addRow(_make_label("Payment Method"),     self.payment)
        form.addRow(_make_label("Notes"),              self.notes)
        form.addRow("",                                self.recurring_chk)

        layout.addLayout(form)
        layout.addWidget(self._rec_box)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        if self.row:
            del_btn = btns.addButton("Delete", QDialogButtonBox.ButtonRole.DestructiveRole)
            del_btn.setObjectName("btnDelete")
            del_btn.clicked.connect(self._delete)
        layout.addWidget(btns)

        self._update_next_due()

    def _update_next_due(self):
        if not self.recurring_chk.isChecked():
            self.lbl_next_due.setText("—")
            return
        base = self.date.date().toString("yyyy-MM-dd")
        interval = self.interval.currentData()
        next_due = db._calc_next_due(base, interval) if interval else None
        self.lbl_next_due.setText(_fmt_date(next_due) if next_due else "—")

    def _validate(self) -> bool:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Validation", "Expense name is required.")
            return False
        if self.amount.value() <= 0:
            QMessageBox.warning(self, "Validation", "Amount must be greater than $0.")
            return False
        return True

    def _save(self):
        if not self._validate():
            return
        recurring  = self.recurring_chk.isChecked()
        interval   = self.interval.currentData() if recurring else None
        exp_date   = self.date.date().toString("yyyy-MM-dd")

        cat = self.category.currentText().strip() or "General"
        if self.row:
            eid = self.row["id"]
            db.update_expense(eid, "expense_name",        self.name.text().strip())
            db.update_expense(eid, "amount_cents",        db.dollars_to_cents(self.amount.value()))
            db.update_expense(eid, "expense_date",        exp_date)
            db.update_expense(eid, "category",            cat)
            db.update_expense(eid, "vendor",              self.vendor.text().strip() or None)
            db.update_expense(eid, "payment_method",      self.payment.text().strip() or None)
            db.update_expense(eid, "notes",               self.notes.toPlainText().strip() or None)
            db.update_expense(eid, "is_recurring",        1 if recurring else 0)
            db.update_expense(eid, "recurrence_interval", interval)
            next_due = db._calc_next_due(exp_date, interval) if recurring and interval else None
            db.update_expense(eid, "next_due_date", next_due)
        else:
            db.add_expense(
                expense_name        = self.name.text().strip(),
                amount_cents        = db.dollars_to_cents(self.amount.value()),
                expense_date        = exp_date,
                category            = cat,
                vendor              = self.vendor.text().strip() or None,
                payment_method      = self.payment.text().strip() or None,
                notes               = self.notes.toPlainText().strip() or None,
                is_recurring        = recurring,
                recurrence_interval = interval,
            )
        self.accept()

    def _delete(self):
        if QMessageBox.question(self, "Confirm Delete", "Soft-delete this expense?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_expense(self.row["id"])
            self.accept()


class ExpensesTab(QWidget):
    refresh_needed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw: List[Dict] = []
        self._all_rows: List[List] = []
        self._filtered_raw: List[Dict] = []
        self._build_ui()
        self._init_mileage()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Header row
        hdr = QHBoxLayout()
        lbl = QLabel("Business Expenses")
        lbl.setObjectName("lblHeader")
        hdr.addWidget(lbl)
        hdr.addStretch()
        btn_export = QPushButton("Export CSV")
        btn_export.clicked.connect(self._export_csv)
        btn_add = QPushButton("+ Add Expense")
        btn_add.setObjectName("btnAdd")
        btn_add.clicked.connect(self._add)
        hdr.addWidget(btn_export)
        hdr.addWidget(btn_add)
        layout.addLayout(hdr)

        # Filter bar
        filters = QHBoxLayout()
        filters.setSpacing(8)

        filters.addWidget(QLabel("Search:"))
        self._f_search = QLineEdit()
        self._f_search.setPlaceholderText("Name or vendor")
        self._f_search.setFixedWidth(150)
        self._f_search.textChanged.connect(self._apply_filter)
        filters.addWidget(self._f_search)

        filters.addWidget(QLabel("From:"))
        self._f_from = DateEdit()
        self._f_from.setDate(QDate.currentDate().addMonths(-3))
        self._f_from.setSpecialValueText("Any")
        self._f_from.dateChanged.connect(self._apply_filter)
        filters.addWidget(self._f_from)

        filters.addWidget(QLabel("To:"))
        self._f_to = DateEdit()
        self._f_to.setDate(QDate.currentDate())
        self._f_to.dateChanged.connect(self._apply_filter)
        filters.addWidget(self._f_to)

        filters.addWidget(QLabel("Show:"))
        self._f_recurring = QComboBox()
        self._f_recurring.addItems(["All", "Recurring Only", "One Time Only"])
        self._f_recurring.setFixedWidth(130)
        self._f_recurring.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self._f_recurring)

        filters.addWidget(QLabel("Min $:"))
        self._f_min = QDoubleSpinBox()
        self._f_min.setRange(0, 999999)
        self._f_min.setDecimals(2)
        self._f_min.setFixedWidth(80)
        self._f_min.valueChanged.connect(self._apply_filter)
        filters.addWidget(self._f_min)

        filters.addWidget(QLabel("Max $:"))
        self._f_max = QDoubleSpinBox()
        self._f_max.setRange(0, 999999)
        self._f_max.setDecimals(2)
        self._f_max.setValue(0)
        self._f_max.setFixedWidth(80)
        self._f_max.setSpecialValueText("Any")
        self._f_max.valueChanged.connect(self._apply_filter)
        filters.addWidget(self._f_max)

        filters.addStretch()
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedWidth(60)
        btn_clear.setStyleSheet(f"""
            QPushButton {{
                background: {theme.BG_ELEVATED}; color: {theme.TEXT_SECONDARY};
                border: 1px solid {theme.BORDER}; border-radius: 6px;
                padding: 5px 12px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {theme.BG_CARD}; color: {theme.TEXT_PRIMARY}; }}
        """)
        btn_clear.clicked.connect(self._clear_filters)
        filters.addWidget(btn_clear)

        layout.addLayout(filters)

        # ── Main area: table on the left, side panel on the right ────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")

        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._context_menu)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        splitter.addWidget(self._table)

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        # ── Right panel ───────────────────────────────────────────────────────
        right = QWidget()
        right.setMinimumWidth(220)
        right.setMaximumWidth(300)
        right.setStyleSheet(f"background: transparent;")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 0, 0, 0)
        right_lay.setSpacing(12)

        # Category breakdown
        cat_lbl = QLabel("Spend by Category")
        cat_lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {theme.TEXT_SECONDARY}; letter-spacing: 0.4px;")
        right_lay.addWidget(cat_lbl)

        self._cat_scroll = QScrollArea()
        self._cat_scroll.setWidgetResizable(True)
        self._cat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cat_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
            f"QScrollBar:vertical {{ background: {theme.BG_ELEVATED}; width: 6px; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.BORDER}; border-radius: 3px; }}"
        )
        self._cat_inner = QWidget()
        self._cat_inner.setStyleSheet(f"background: transparent;")
        self._cat_layout = QVBoxLayout(self._cat_inner)
        self._cat_layout.setContentsMargins(0, 0, 0, 0)
        self._cat_layout.setSpacing(4)
        self._cat_scroll.setWidget(self._cat_inner)
        right_lay.addWidget(self._cat_scroll, 1)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color: {theme.BORDER};")
        right_lay.addWidget(div)

        # Mileage tracker
        mil_lbl = QLabel("Mileage Tracker")
        mil_lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {theme.TEXT_SECONDARY}; letter-spacing: 0.4px;")
        right_lay.addWidget(mil_lbl)

        # Running total display
        self._mil_total_lbl = QLabel("0.0 mi")
        self._mil_total_lbl.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: 20px; font-weight: 700; background: transparent;"
        )
        right_lay.addWidget(self._mil_total_lbl)

        self._mil_deduction_lbl = QLabel("Deduction: $0.00")
        self._mil_deduction_lbl.setStyleSheet(
            f"color: {theme.GREEN}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        right_lay.addWidget(self._mil_deduction_lbl)

        mil_form = QFormLayout()
        mil_form.setSpacing(6)

        self._mil_rate = QDoubleSpinBox()
        self._mil_rate.setRange(0, 10)
        self._mil_rate.setDecimals(3)
        self._mil_rate.setPrefix("$")
        self._mil_rate.setSuffix(" /mi")
        self._mil_rate.setValue(0.67)
        self._mil_rate.valueChanged.connect(lambda _: self._update_mileage_display())
        mil_form.addRow("IRS Rate:", self._mil_rate)

        self._mil_add = QDoubleSpinBox()
        self._mil_add.setRange(0, 99999)
        self._mil_add.setDecimals(1)
        self._mil_add.setSuffix(" mi")
        mil_form.addRow("Add Miles:", self._mil_add)

        right_lay.addLayout(mil_form)

        btn_mil_save = QPushButton("Save")
        btn_mil_save.clicked.connect(self._save_mileage)
        right_lay.addWidget(btn_mil_save)

        btn_mil_reset = QPushButton("Reset Counter")
        btn_mil_reset.setObjectName("btnDelete")
        btn_mil_reset.clicked.connect(self._reset_mileage)
        right_lay.addWidget(btn_mil_reset)

        right_lay.addStretch()
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    # ── Mileage rolling counter ───────────────────────────────────────────────

    _MILEAGE_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mileage_state.json"
    )

    def _load_mileage_state(self) -> dict:
        try:
            with open(self._MILEAGE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {"total_miles": 0.0, "rate": 0.67}

    def _save_mileage_state(self, state: dict):
        try:
            with open(self._MILEAGE_FILE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[mileage] save error: {e}")

    def _init_mileage(self):
        state = self._load_mileage_state()
        self._mil_rate.blockSignals(True)
        self._mil_rate.setValue(state.get("rate", 0.67))
        self._mil_rate.blockSignals(False)
        self._update_mileage_display(state.get("total_miles", 0.0))

    def _update_mileage_display(self, total_miles: float = None):
        if total_miles is None:
            state = self._load_mileage_state()
            total_miles = state.get("total_miles", 0.0)
        rate = self._mil_rate.value()
        deduction = total_miles * rate
        self._mil_total_lbl.setText(f"{total_miles:,.1f} mi")
        self._mil_deduction_lbl.setText(f"Deduction: ${deduction:,.2f}")

    def _save_mileage(self):
        added = self._mil_add.value()
        if added <= 0:
            return
        state = self._load_mileage_state()
        state["total_miles"] = state.get("total_miles", 0.0) + added
        state["rate"] = self._mil_rate.value()
        self._save_mileage_state(state)
        self._mil_add.setValue(0)
        self._update_mileage_display(state["total_miles"])

    def _reset_mileage(self):
        if QMessageBox.question(
            self, "Reset Mileage", "Reset the mileage counter to zero?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            state = self._load_mileage_state()
            state["total_miles"] = 0.0
            self._save_mileage_state(state)
            self._update_mileage_display(0.0)

    def _refresh_category_breakdown(self):
        # Clear old widgets
        while self._cat_layout.count():
            item = self._cat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        breakdown = db.get_expense_category_breakdown()
        if not breakdown:
            lbl = QLabel("No data yet")
            lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
            self._cat_layout.addWidget(lbl)
            return

        total = sum(r["total_cents"] for r in breakdown)
        accent_cycle = [theme.BLUE, theme.TEAL, theme.GREEN, theme.ORANGE, theme.PURPLE, theme.RED]

        for i, row in enumerate(breakdown):
            color  = accent_cycle[i % len(accent_cycle)]
            pct    = (row["total_cents"] / total * 100) if total else 0
            amount = db.format_money(row["total_cents"])

            row_w = QWidget()
            row_w.setStyleSheet("background: transparent;")
            row_l = QVBoxLayout(row_w)
            row_l.setContentsMargins(0, 2, 0, 2)
            row_l.setSpacing(2)

            top = QHBoxLayout()
            cat_name = QLabel(row["category"])
            cat_name.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 11px; font-weight: 600;")
            top.addWidget(cat_name)
            top.addStretch()
            amt_lbl = QLabel(amount)
            amt_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
            top.addWidget(amt_lbl)
            row_l.addLayout(top)

            # Progress bar
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(pct))
            bar.setFixedHeight(4)
            bar.setTextVisible(False)
            bar.setStyleSheet(f"""
                QProgressBar {{ background: {theme.BG_ELEVATED}; border-radius: 2px; border: none; }}
                QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}
            """)
            row_l.addWidget(bar)
            self._cat_layout.addWidget(row_w)

        self._cat_layout.addStretch()

    def _clear_filters(self):
        self._f_search.setText("")
        self._f_from.setDate(QDate.currentDate().addMonths(-3))
        self._f_to.setDate(QDate.currentDate())
        self._f_recurring.setCurrentIndex(0)
        self._f_min.setValue(0)
        self._f_max.setValue(0)

    def load_data(self):
        scroll = self._table.verticalScrollBar().value()
        self._raw = db.get_expenses()

        rows = []
        for r in self._raw:
            if r["is_recurring"] and r.get("recurrence_interval"):
                rec_label = f"Recurring ({r['recurrence_interval'].title()})"
            elif r.get("recurrence_interval"):
                rec_label = f"From Recurring ({r['recurrence_interval'].title()})"
            else:
                rec_label = "One Time"
            rows.append([
                _fmt_date(r["expense_date"]),
                r["expense_name"],
                r.get("category") or "General",
                r.get("vendor") or "",
                db.format_money(r["amount_cents"]),
                r.get("payment_method") or "",
                rec_label,
                r.get("notes") or "",
            ])
        self._all_rows = rows
        self._apply_filter()
        self._refresh_category_breakdown()
        self._table.verticalScrollBar().setValue(scroll)

    def _apply_filter(self):
        search   = self._f_search.text().strip().lower()
        from_dt  = self._f_from.date().toString("yyyy-MM-dd")
        to_dt    = self._f_to.date().toString("yyyy-MM-dd")
        rec_idx  = self._f_recurring.currentIndex()  # 0=all 1=recurring 2=one_time
        min_c    = db.dollars_to_cents(self._f_min.value()) if self._f_min.value() > 0 else None
        max_c    = db.dollars_to_cents(self._f_max.value()) if self._f_max.value() > 0 else None

        filtered_raw  = []
        filtered_rows = []
        for i, r in enumerate(self._raw):
            if r["expense_date"] < from_dt or r["expense_date"] > to_dt:
                continue
            if rec_idx == 1 and not r["is_recurring"]:
                continue
            if rec_idx == 2 and r["is_recurring"]:
                continue
            if min_c is not None and r["amount_cents"] < min_c:
                continue
            if max_c is not None and r["amount_cents"] > max_c:
                continue
            if search:
                haystack = f"{r['expense_name']} {r.get('vendor', '')}".lower()
                if search not in haystack:
                    continue
            filtered_raw.append(r)
            filtered_rows.append(self._all_rows[i])

        self._filtered_raw = filtered_raw
        self._proxy.setSourceModel(RecordTableModel(EXP_HEADERS, filtered_rows))
        self._table.setModel(self._proxy)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def _get_raw(self, proxy_row: int) -> Optional[Dict]:
        try:
            source_row = self._proxy.mapToSource(self._proxy.index(proxy_row, 0)).row()
            return self._filtered_raw[source_row]
        except (IndexError, AttributeError):
            return None

    def _get_selected_raw(self) -> List[Dict]:
        result = []
        for idx in self._table.selectionModel().selectedRows():
            source_row = self._proxy.mapToSource(self._proxy.index(idx.row(), 0)).row()
            if 0 <= source_row < len(self._filtered_raw):
                result.append(self._filtered_raw[source_row])
        return result

    def _on_double_click(self, index: QModelIndex):
        r = self._get_raw(index.row())
        if r:
            self._edit(r)

    def _add(self):
        dlg = ExpenseDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _edit(self, r: Dict):
        dlg = ExpenseDialog(self, row=r)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.load_data()
            self.refresh_needed.emit()

    def _context_menu(self, pos):
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        r = self._get_raw(index.row())
        if not r:
            return
        selected = self._get_selected_raw()
        targets  = selected if selected else [r]
        n        = len(targets)
        menu     = QMenu(self)

        if n == 1:
            menu.addAction("Edit").triggered.connect(lambda: self._edit(r))
            menu.addSeparator()

        # Category change
        menu.addAction(f"Set Category ({n} expense{'s' if n > 1 else ''})…").triggered.connect(
            lambda: self._bulk_set_expense_category(targets)
        )

        menu.addSeparator()
        menu.addAction(f"Delete ({n} expense{'s' if n > 1 else ''})").triggered.connect(
            lambda: self._bulk_delete_expenses(targets)
        )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _bulk_set_expense_field(self, targets: List[Dict], field: str, value: str):
        for r in targets:
            db.update_expense(r["id"], field, value)
        self.load_data()
        self.refresh_needed.emit()

    def _bulk_set_expense_category(self, targets: List[Dict]):
        suggestions = sorted({e["category"] for e in db.get_expenses() if e.get("category")})
        cat = _ask_category("Set Category", "Category for selected expenses:", suggestions, self)
        if cat:
            self._bulk_set_expense_field(targets, "category", cat)

    def _bulk_delete_expenses(self, targets: List[Dict]):
        n = len(targets)
        if QMessageBox.question(
            self, "Confirm", f"Soft-delete {n} expense{'s' if n > 1 else ''}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            for r in targets:
                db.delete_expense(r["id"])
            self.load_data()
            self.refresh_needed.emit()

    def _delete(self, r: Dict):
        if QMessageBox.question(self, "Confirm", "Soft-delete this expense?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                ) == QMessageBox.StandardButton.Yes:
            db.delete_expense(r["id"])
            self.load_data()
            self.refresh_needed.emit()

    def _export_csv(self):
        rows = []
        for r in self._filtered_raw:
            if r["is_recurring"] and r.get("recurrence_interval"):
                rec = f"Recurring ({r['recurrence_interval'].title()})"
            elif r.get("recurrence_interval"):
                rec = f"From Recurring ({r['recurrence_interval'].title()})"
            else:
                rec = "One Time"
            rows.append([
                _fmt_date(r["expense_date"]), r["expense_name"],
                r.get("category") or "General", r.get("vendor") or "",
                db.format_money(r["amount_cents"]), r.get("payment_method") or "",
                rec, r.get("notes") or "",
            ])
        _write_csv(self, EXP_HEADERS, rows, "expenses.csv")


# ── Dashboard widgets ─────────────────────────────────────────────────────────

_CARD_ACCENTS = [theme.BLUE, theme.ORANGE, theme.GREEN, theme.RED, theme.PURPLE, theme.TEAL]


class SummaryCard(QFrame):
    """A metric card with a colored top border, value, and trend indicator."""

    def __init__(self, title: str, accent_idx: int, parent=None):
        super().__init__(parent)
        self._accent = _CARD_ACCENTS[accent_idx % len(_CARD_ACCENTS)]
        self.setObjectName("summaryCard")
        self.setMinimumWidth(155)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            QFrame#summaryCard {{
                background: {theme.BG_CARD};
                border-radius: 10px;
                border-top: 2px solid {self._accent};
                border-left: 1px solid {theme.BG_ELEVATED};
                border-right: 1px solid {theme.BG_ELEVATED};
                border-bottom: 1px solid {theme.BG_ELEVATED};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(3)

        self._lbl_title = QLabel(title)
        self._lbl_title.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; font-weight: normal; background: transparent;")

        self._lbl_value = QLabel("—")
        self._lbl_value.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 20px; font-weight: bold; background: transparent;")

        self._lbl_sub = QLabel("")
        self._lbl_sub.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;")
        self._lbl_sub.setTextFormat(Qt.TextFormat.RichText)
        self._lbl_sub.setWordWrap(True)

        layout.addWidget(self._lbl_title)
        layout.addWidget(self._lbl_value)
        layout.addWidget(self._lbl_sub)

    def set_value(self, text: str, color: Optional[str] = None):
        self._lbl_value.setText(text)
        c = color or theme.TEXT_PRIMARY
        self._lbl_value.setStyleSheet(f"color: {c}; font-size: 20px; font-weight: bold; background: transparent;")

    def set_trend(self, current: float, previous: float, invert: bool = False):
        """Show ▲/▼ pct vs prior period. invert=True makes up-trend red (e.g. expenses)."""
        if not previous:
            self._lbl_sub.setText("No prior period data")
            return
        pct = (current - previous) / abs(previous) * 100
        up = pct >= 0
        arrow = "▲" if up else "▼"
        good = (up and not invert) or (not up and invert)
        color = theme.GREEN if good else theme.RED
        self._lbl_sub.setText(
            f'<span style="color:{color}">{arrow} {abs(pct):.1f}%</span>'
            f'<span style="color:{theme.TEXT_SECONDARY}"> vs prior period</span>'
        )

    def set_subtext(self, text: str):
        self._lbl_sub.setTextFormat(Qt.TextFormat.PlainText)
        self._lbl_sub.setText(text)
        self._lbl_sub.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;")


class StatTile(QFrame):
    """Compact secondary metric tile for Zone 3."""

    def __init__(self, title: str, accent: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statTile")
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            QFrame#statTile {{
                background: {theme.BG_CARD};
                border-radius: 8px;
                border: 1px solid {theme.BG_ELEVATED};
                border-left: 3px solid {accent};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(2)

        self._lbl_title = QLabel(title)
        self._lbl_title.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 10px; background: transparent;")

        self._lbl_value = QLabel("—")
        self._lbl_value.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 15px; font-weight: bold; background: transparent;")
        self._lbl_value.setWordWrap(True)

        layout.addWidget(self._lbl_title)
        layout.addWidget(self._lbl_value)

    def set_value(self, text: str, color: Optional[str] = None):
        self._lbl_value.setText(text)
        c = color or theme.TEXT_PRIMARY
        self._lbl_value.setStyleSheet(f"color: {c}; font-size: 15px; font-weight: bold; background: transparent;")


def _make_chart_frame(title: str) -> tuple:
    """Return (outer QFrame, inner QVBoxLayout, title QLabel)."""
    frame = QFrame()
    frame.setObjectName("chartFrame")
    frame.setStyleSheet(f"""
        QFrame#chartFrame {{
            background: {theme.BG_CARD};
            border-radius: 10px;
            border: 1px solid {theme.BG_ELEVATED};
        }}
    """)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 10)
    layout.setSpacing(8)

    lbl = QLabel(title)
    lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {theme.TEXT_SECONDARY}; background: transparent;")
    layout.addWidget(lbl)
    return frame, layout, lbl


class _NoDataLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__("No data for this period", parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"color: {theme.BORDER}; font-size: 12px; background: transparent;")
        self.setFixedHeight(180)


class AccountHealthTab(QWidget):
    """Per-retailer account health — shows email checkout success rates."""

    refresh_needed = pyqtSignal()

    _RETAILER_ICONS = {
        "walmart":       "🛒",
        "pokemon_center":"🎴",
        "target":        "🎯",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        title = QLabel("Account Health")
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.PURPLE};")
        root.addWidget(title)

        sub = QLabel("Track which accounts are successfully checking out across retailers")
        sub.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
        root.addWidget(sub)

        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cards_scroll.setStyleSheet("background: transparent; border: none;")
        self._cards_scroll.viewport().setStyleSheet("background: transparent;")

        self._cards_widget = QWidget()
        self._cards_widget.setStyleSheet("background: transparent;")
        self._cards_layout = QHBoxLayout(self._cards_widget)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(14)
        self._cards_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._cards_scroll.setWidget(self._cards_widget)
        root.addWidget(self._cards_scroll)
        root.addStretch()

    def load_data(self):
        # Clear existing cards
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        data = db.get_account_health_data()
        if not data:
            lbl = QLabel("No order data yet — run an email sync to populate account health.")
            lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 12px;")
            self._cards_layout.addWidget(lbl)
            return

        for retailer, accounts in sorted(data.items()):
            card = self._make_retailer_card(retailer, accounts)
            self._cards_layout.addWidget(card)

    def _make_retailer_card(self, retailer: str, accounts: List[Dict]) -> QFrame:
        icon        = self._RETAILER_ICONS.get(retailer, "🏪")
        label       = retailer.replace("_", " ").title()
        total       = sum(a["orders"]    for a in accounts)
        shipped     = sum(a["shipped"]   for a in accounts)
        cancelled   = sum(a["cancelled"] for a in accounts)
        n_accounts  = len(accounts)

        card = QFrame()
        card.setFixedWidth(220)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setStyleSheet(f"""
            QFrame {{
                background: {theme.BG_CARD};
                border: 1px solid {theme.BG_ELEVATED};
                border-radius: 10px;
            }}
            QFrame:hover {{
                border-color: {theme.BLUE};
            }}
        """)

        vl = QVBoxLayout(card)
        vl.setContentsMargins(18, 16, 18, 16)
        vl.setSpacing(10)

        # Header
        hdr = QLabel(f"{icon}  {label}")
        hdr.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {theme.TEXT_PRIMARY};")
        vl.addWidget(hdr)

        acct_lbl = QLabel(f"{n_accounts} account{'s' if n_accounts != 1 else ''}")
        acct_lbl.setStyleSheet(f"font-size: 10px; color: {theme.TEXT_SECONDARY};")
        vl.addWidget(acct_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {theme.BG_ELEVATED}; border: none; max-height: 1px;")
        vl.addWidget(sep)

        # Stats
        for stat_label, value, color in [
            ("Orders",    str(total),     theme.TEXT_PRIMARY),
            ("Shipped",   str(shipped),   theme.GREEN),
            ("Cancelled", str(cancelled), theme.RED),
        ]:
            row = QHBoxLayout()
            lk = QLabel(stat_label)
            lk.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
            lv = QLabel(value)
            lv.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: 700;")
            row.addWidget(lk)
            row.addStretch()
            row.addWidget(lv)
            vl.addLayout(row)

        # Click area — install event filter on card
        card.mousePressEvent = lambda e, r=retailer, a=accounts: self._open_detail(r, a)
        return card

    def _open_detail(self, retailer: str, accounts: List[Dict]):
        dlg = AccountHealthDetailDialog(retailer, accounts, self)
        dlg.exec()


class AccountHealthDetailDialog(QDialog):
    """Shows per-email breakdown for a retailer, sorted by shipped desc (live)."""

    _HEADERS = ["Email Account", "Orders", "Shipped", "Cancelled", "Delivery Address"]

    def __init__(self, retailer: str, accounts: List[Dict], parent=None):
        super().__init__(parent)
        self._retailer = retailer
        self._accounts = accounts
        self.setWindowTitle(f"{retailer.replace('_', ' ').title()} — Account Health")
        self.setMinimumSize(820, 480)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)

        title = QLabel(self._retailer.replace("_", " ").title())
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.PURPLE};")
        lay.addWidget(title)

        sub = QLabel("Sorted by shipped orders — best performing accounts first")
        sub.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_SECONDARY};")
        lay.addWidget(sub)

        rows = [
            [a["account_email"], str(a["orders"]), str(a["shipped"]),
             str(a["cancelled"]), a["address"]]
            for a in sorted(self._accounts, key=lambda x: (x["shipped"], -x["cancelled"]), reverse=True)
        ]

        model = RecordTableModel(self._HEADERS, rows)
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(model)
        proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self._table = QTableView()
        self._table.setModel(proxy)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(2, Qt.SortOrder.DescendingOrder)   # Shipped col
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.resizeColumnsToContents()
        lay.addWidget(self._table)

        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(btn)
        lay.addLayout(btn_row)


class RetailerPieChart(QWidget):
    """Pie chart showing order distribution by retailer."""

    _COLORS = [theme.PURPLE, theme.BLUE, theme.TEAL, theme.ORANGE, theme.GREEN, theme.RED, "#e880c5"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._frame, self._layout, _ = _make_chart_frame("Orders by Retailer")

        if _HAS_MPL:
            self._fig = Figure(figsize=(4.5, 2.8), dpi=96, facecolor=theme.BG_CARD)
            self._ax  = self._fig.add_subplot(111, facecolor=theme.BG_CARD)
            self._fig.subplots_adjust(left=0.05, right=0.65, top=0.95, bottom=0.05)
            self._canvas = FigureCanvas(self._fig)
            self._canvas.setStyleSheet("background: transparent;")
            self._canvas.setFixedHeight(200)
            self._layout.addWidget(self._canvas)
        else:
            self._layout.addWidget(_NoDataLabel())

        outer.addWidget(self._frame)

    def update_data(self, orders_by_retailer: List[Dict]):
        if not _HAS_MPL:
            return
        try:
            self._update_data_inner(orders_by_retailer)
        except Exception as e:
            print(f"[RetailerPieChart] render error: {e}")

    def _update_data_inner(self, orders_by_retailer):
        self._ax.clear()
        if not orders_by_retailer:
            self._ax.text(0.5, 0.5, "No orders yet",
                          ha="center", va="center", color=theme.BORDER,
                          transform=self._ax.transAxes, fontsize=10)
            self._canvas.draw_idle()
            return

        labels = [d["retailer"].replace("_", " ").title() for d in orders_by_retailer]
        raw_keys = [d["retailer"] for d in orders_by_retailer]
        sizes  = [d["count"] for d in orders_by_retailer]
        # Use the same retailer color map as the order tracker drop cards;
        # unknown retailers default to Shopify purple
        colors = [_RETAILER_COLORS.get(key, _SHOPIFY_PURPLE) for key in raw_keys]

        wedges, _ = self._ax.pie(
            sizes,
            colors=colors,
            startangle=90,
            wedgeprops={"linewidth": 2, "edgecolor": theme.BG_CARD},
        )

        # Legend to the right with count
        legend_labels = [f"{lbl}  ({n})" for lbl, n in zip(labels, sizes)]
        self._ax.legend(
            wedges, legend_labels,
            loc="center left",
            bbox_to_anchor=(1.05, 0.5),
            fontsize=8,
            facecolor=theme.BG_CARD,
            edgecolor=theme.BORDER,
            labelcolor=theme.TEXT_PRIMARY,
            framealpha=1,
        )
        self._ax.set_aspect("equal")
        self._canvas.draw_idle()


class StickRateCard(QWidget):
    """Card showing the order stick rate — percentage of orders that weren't cancelled."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._frame, body, _ = _make_chart_frame("Order Stick Rate")
        body.setSpacing(6)

        # Big percentage number
        self._pct_lbl = QLabel("—")
        self._pct_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pct_lbl.setStyleSheet(
            f"font-size: 52px; font-weight: 800; color: {theme.GREEN}; background: transparent;"
        )
        body.addWidget(self._pct_lbl)

        # Progress bar
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                border: none; border-radius: 4px;
                background: {theme.BG_ELEVATED};
            }}
            QProgressBar::chunk {{
                border-radius: 4px;
                background: {theme.GREEN};
            }}
        """)
        body.addWidget(self._bar)

        # Sub-text breakdown
        self._sub_lbl = QLabel("")
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_SECONDARY}; background: transparent;"
        )
        body.addWidget(self._sub_lbl)

        body.addStretch()
        self._frame.setFixedHeight(200)
        outer.addWidget(self._frame)

    def update_data(self, total: int, cancelled: int, stuck: int):
        if total == 0:
            self._pct_lbl.setText("—")
            self._bar.setValue(0)
            self._sub_lbl.setText("No orders yet")
            return

        pct = round(stuck / total * 100, 1)
        color = theme.GREEN if pct >= 70 else theme.ORANGE if pct >= 40 else theme.RED
        self._pct_lbl.setText(f"{pct:.1f}%")
        self._pct_lbl.setStyleSheet(
            f"font-size: 52px; font-weight: 800; color: {color}; background: transparent;"
        )
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                border: none; border-radius: 4px;
                background: {theme.BG_ELEVATED};
            }}
            QProgressBar::chunk {{
                border-radius: 4px;
                background: {color};
            }}
        """)
        self._bar.setValue(int(pct))
        self._sub_lbl.setText(
            f"{stuck} of {total} orders stuck  ·  {cancelled} cancelled"
        )


class ActivityTableModel(QAbstractTableModel):
    _HEADERS = ["Type", "Description", "Date", "Amount"]
    _TYPE_COLORS = {"Sale": theme.GREEN, "Order": theme.BLUE, "Expense": theme.RED}

    def __init__(self, rows: List[Dict], parent=None):
        super().__init__(parent)
        self._rows = rows

    def rowCount(self, parent=QModelIndex()):    return len(self._rows)
    def columnCount(self, parent=QModelIndex()): return 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._HEADERS[section]
        return QVariant()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return QVariant()
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return [row["type"], row["label"], _fmt_date(row["date"] or ""), db.format_money(row["amount"])][col]
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return QColor(self._TYPE_COLORS.get(row["type"], theme.TEXT_PRIMARY))
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 3:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return QVariant()


# ── Tab: Dashboard ────────────────────────────────────────────────────────────

class DashboardTab(QWidget):
    refresh_needed = pyqtSignal()

    _PERIODS = [("Last 7 Days",  "week"), ("Last 30 Days", "days30"),
                ("This Month",  "month"), ("This Year",   "year"),
                ("All Time",    "all")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._period = "days30"
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ────────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet(
            f"background: {theme.BG_CARD}; border-bottom: 1px solid {theme.BG_ELEVATED};"
        )
        header.setFixedHeight(50)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(22, 0, 22, 0)

        title = QLabel("Dashboard")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT_PRIMARY}; letter-spacing: 0.3px;"
        )
        hl.addWidget(title)
        hl.addStretch()

        hl.addWidget(QLabel("Period:"))
        self._period_combo = QComboBox()
        self._period_combo.setFixedWidth(140)
        for label, val in self._PERIODS:
            self._period_combo.addItem(label, val)
        self._period_combo.setCurrentIndex(1)  # default: Last 30 Days
        self._period_combo.currentIndexChanged.connect(self._on_period_changed)
        hl.addWidget(self._period_combo)

        root.addWidget(header)

        # ── Scrollable content ────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background: {theme.BG_BASE}; border: none; }}")
        scroll.viewport().setStyleSheet(f"background: {theme.BG_BASE};")

        content = QWidget()
        content.setStyleSheet(f"background: {theme.BG_BASE};")
        vl = QVBoxLayout(content)
        vl.setContentsMargins(20, 18, 20, 20)
        vl.setSpacing(16)

        # Zone 1 — Summary cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)
        self._card_orders   = SummaryCard("Total Orders Placed",   0)
        self._card_cost     = SummaryCard("Total Product Cost",    1)
        self._card_revenue  = SummaryCard("Total Revenue",         2)
        self._card_expenses = SummaryCard("Total Expenses",        3)
        self._card_profit   = SummaryCard("Net Profit",            4)
        self._card_inv      = SummaryCard("Inventory Value",       5)
        for c in (self._card_orders, self._card_cost, self._card_revenue,
                  self._card_expenses, self._card_profit, self._card_inv):
            cards_row.addWidget(c)
        vl.addLayout(cards_row)

        # Zone 2 — Charts
        charts_row = QHBoxLayout()
        charts_row.setSpacing(12)
        self._chart_retailer = RetailerPieChart()
        self._stick_rate     = StickRateCard()
        charts_row.addWidget(self._chart_retailer, 3)
        charts_row.addWidget(self._stick_rate,     2)
        vl.addLayout(charts_row)

        # Zone 3 — Secondary stat tiles
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self._stat_sales    = StatTile("Sales Count",       theme.BLUE)
        self._stat_avg_sale = StatTile("Avg Sale Price",    theme.ORANGE)
        self._stat_avg_prof = StatTile("Avg Profit / Sale", theme.GREEN)
        self._stat_best_day = StatTile("Best Sales Day",    theme.PURPLE)
        self._stat_in_stock = StatTile("Items In Stock",    theme.TEAL)
        self._stat_units    = StatTile("Units On Hand",     "#e880c5")
        for s in (self._stat_sales, self._stat_avg_sale, self._stat_avg_prof,
                  self._stat_best_day, self._stat_in_stock, self._stat_units):
            stats_row.addWidget(s)
        vl.addLayout(stats_row)

        # Zone 4 — Recent activity
        act_frame = QFrame()
        act_frame.setObjectName("actFrame")
        act_frame.setStyleSheet(f"""
            QFrame#actFrame {{
                background: {theme.BG_CARD};
                border-radius: 10px;
                border: 1px solid {theme.BG_ELEVATED};
            }}
        """)
        act_layout = QVBoxLayout(act_frame)
        act_layout.setContentsMargins(16, 13, 16, 13)
        act_layout.setSpacing(8)

        act_title = QLabel("Recent Activity")
        act_title.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {theme.TEXT_SECONDARY}; background: transparent;"
        )
        act_layout.addWidget(act_title)

        self._act_table = QTableView()
        self._act_table.setStyleSheet(f"QTableView {{ border: none; background: {theme.BG_CARD}; }}")
        self._act_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._act_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._act_table.verticalHeader().setVisible(False)
        self._act_table.horizontalHeader().setStretchLastSection(False)
        self._act_table.setAlternatingRowColors(True)
        self._act_table.setFixedHeight(270)
        act_layout.addWidget(self._act_table)
        vl.addWidget(act_frame)

        vl.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)

    def _on_period_changed(self):
        self._period = self._period_combo.currentData()
        self.load_data()

    def load_data(self):
        try:
            d = db.get_dashboard_data(self._period)
            self._update_cards(d)
            self._chart_retailer.update_data(d["orders_by_retailer"])
            self._stick_rate.update_data(d["stick_total"], d["stick_cancelled"], d["stick_stuck"])
            self._update_stats(d)
            self._update_activity(d)
        except Exception as exc:
            print(f"[dashboard] load error: {exc}")

    def _update_cards(self, d: Dict):
        self._card_orders.set_value(str(d["cur_orders"]))
        self._card_orders.set_trend(d["cur_orders"], d["prev_orders"])

        self._card_cost.set_value(db.format_money(d["cur_cost"]))
        self._card_cost.set_trend(d["cur_cost"], d["prev_cost"], invert=True)

        self._card_revenue.set_value(db.format_money(d["cur_revenue"]))
        self._card_revenue.set_trend(d["cur_revenue"], d["prev_revenue"])

        self._card_expenses.set_value(db.format_money(d["cur_expenses"]))
        self._card_expenses.set_trend(d["cur_expenses"], d["prev_expenses"], invert=True)

        net = d["cur_net"]
        self._card_profit.set_value(
            db.format_money(net),
            color=theme.GREEN if net >= 0 else theme.RED,
        )
        self._card_profit.set_trend(d["cur_net"], d["prev_net"])

        self._card_inv.set_value(db.format_money(d["inv_value"]))
        self._card_inv.set_subtext(f"{d['inv_items']} items · {d['inv_units']} units")

    def _update_stats(self, d: Dict):
        self._stat_sales.set_value(str(d["sale_count"]))
        self._stat_avg_sale.set_value(db.format_money(d["avg_sale_cents"]))
        ap = d["avg_profit_cents"]
        self._stat_avg_prof.set_value(
            db.format_money(ap),
            color=theme.GREEN if ap >= 0 else theme.RED,
        )
        # Best day: show date and count on two lines
        best = d["best_day"]
        if " · " in best:
            date_part, count_part = best.split(" · ", 1)
            self._stat_best_day.set_value(f"{_fmt_date(date_part)}\n{count_part}")
        else:
            self._stat_best_day.set_value(best)
        self._stat_in_stock.set_value(str(d["inv_items"]))
        self._stat_units.set_value(str(d["inv_units"]))

    def _update_activity(self, d: Dict):
        model = ActivityTableModel(d["recent_activity"])
        self._act_table.setModel(model)
        self._act_table.resizeColumnsToContents()
        self._act_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )


# ── IMAP account dialog ───────────────────────────────────────────────────────

class _ImapAccountDialog(QDialog):
    _PROVIDERS = [
        ("Gmail",         "imap.gmail.com",          993),
        ("AOL",           "imap.aol.com",             993),
        ("Yahoo",         "imap.mail.yahoo.com",      993),
        ("Outlook",       "outlook.office365.com",    993),
        ("Other (manual)", "",                         993),
    ]

    def __init__(self, parent=None, account: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Account" if account else "Add Account")
        self.setMinimumWidth(420)
        self._build(account or {})

    def _build(self, a: dict):
        layout = QFormLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 18, 18, 18)

        self._label = QLineEdit(a.get("label", ""))
        self._label.setPlaceholderText("e.g. Main AOL")

        # Provider selector — auto-fills host
        self._provider = QComboBox()
        for name, host, _ in self._PROVIDERS:
            self._provider.addItem(name, host)

        saved_host = a.get("host", "imap.gmail.com")
        matched = False
        for i, (_, host, _) in enumerate(self._PROVIDERS[:-1]):  # skip "Other"
            if host == saved_host:
                self._provider.setCurrentIndex(i)
                matched = True
                break
        if not matched:
            self._provider.setCurrentIndex(len(self._PROVIDERS) - 1)  # Other

        self._provider.currentIndexChanged.connect(self._on_provider_changed)

        self._user = QLineEdit(a.get("user", ""))
        self._user.setPlaceholderText("you@example.com")
        self._pw   = QLineEdit(a.get("pass", ""))
        self._pw.setPlaceholderText("App password / password")
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)

        # Manual host (shown only for "Other")
        self._host = QLineEdit(saved_host if not matched else "")
        self._host.setPlaceholderText("imap.example.com")

        layout.addRow("Label", self._label)
        layout.addRow("Provider", self._provider)
        layout.addRow("Email Address", self._user)
        layout.addRow("Password", self._pw)
        self._host_row_label = QLabel("IMAP Host")
        layout.addRow(self._host_row_label, self._host)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

        self._on_provider_changed()  # set initial visibility

    def _on_provider_changed(self):
        is_other = self._provider.currentData() == ""
        self._host.setVisible(is_other)
        self._host_row_label.setVisible(is_other)

    def values(self) -> dict:
        host = self._provider.currentData() or self._host.text().strip()
        return {
            "label": self._label.text().strip(),
            "user":  self._user.text().strip(),
            "pass":  self._pw.text().strip(),
            "host":  host,
            "port":  993,
        }


# ── Settings tab ──────────────────────────────────────────────────────────────

class SettingsTab(QWidget):
    """Editable settings panel — writes secrets to .env, non-secrets to settings.ini."""

    _LABEL_W = 190   # fixed label column width

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_values()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _card(self, title: str) -> tuple:
        """Return (card QFrame, body QVBoxLayout) styled as a dark card."""
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background: {theme.BG_CARD};
                border: 1px solid {theme.BG_ELEVATED};
                border-radius: 10px;
            }}
        """)
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(20, 14, 20, 16)
        vbox.setSpacing(10)

        hdr = QLabel(title)
        hdr.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 11px; font-weight: 600;"
            f" text-transform: uppercase; letter-spacing: 0.5px;"
            f" border: none; background: transparent;"
        )
        vbox.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.BG_ELEVATED}; border: none; background: {theme.BG_ELEVATED}; max-height: 1px;")
        vbox.addWidget(sep)

        return card, vbox

    def _field_row(self, body: QVBoxLayout, label: str, widget) -> None:
        """Add a [Label | Widget] row to a card body layout."""
        row = QHBoxLayout()
        row.setSpacing(12)
        lbl = QLabel(label)
        lbl.setFixedWidth(self._LABEL_W)
        lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; background: transparent; border: none; font-size: 12px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        body.addLayout(row)

    def _check_row(self, body: QVBoxLayout, widget: QCheckBox) -> None:
        """Add a checkbox indented to align with the field column."""
        row = QHBoxLayout()
        row.setSpacing(0)
        row.addSpacing(self._LABEL_W + 12)
        row.addWidget(widget, 1)
        body.addLayout(row)

    def _divider(self, body: QVBoxLayout) -> None:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.BG_ELEVATED}; border: none; background: {theme.BG_ELEVATED}; max-height: 1px;")
        body.addWidget(sep)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"QScrollArea {{ background: {theme.BG_BASE}; border: none; }}")
        root.addWidget(scroll)

        inner = QWidget()
        inner.setStyleSheet(f"background: {theme.BG_BASE};")
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 8, 8, 16)
        layout.setSpacing(14)

        # ── Discord ───────────────────────────────────────────────────────────
        self._discord_webhook   = QLineEdit()
        self._discord_webhook.setPlaceholderText("https://discord.com/api/webhooks/…")
        self._discord_bot_token = QLineEdit()
        self._discord_bot_token.setPlaceholderText("Bot token (optional)")
        self._discord_bot_token.setEchoMode(QLineEdit.EchoMode.Password)

        card, body = self._card("Discord")
        self._field_row(body, "Webhook URL", self._discord_webhook)
        self._field_row(body, "Bot Token", self._discord_bot_token)
        layout.addWidget(card)

        # ── IMAP Accounts ─────────────────────────────────────────────────────
        self._imap_accounts: List[Dict] = []

        self._imap_list = QListWidget()
        self._imap_list.setFixedHeight(116)
        self._imap_list.setStyleSheet(f"""
            QListWidget {{
                background: {theme.BG_BASE};
                border: 1px solid {theme.BG_ELEVATED};
                border-radius: 6px;
                color: {theme.TEXT_PRIMARY};
                font-size: 12px;
                padding: 2px;
            }}
            QListWidget::item {{ padding: 4px 8px; }}
            QListWidget::item:selected {{ background: {theme.BLUE}; border-radius: 4px; }}
        """)

        imap_btn_row = QHBoxLayout()
        imap_btn_row.setSpacing(6)
        btn_imap_add  = QPushButton("+ Add")
        btn_imap_edit = QPushButton("Edit")
        btn_imap_del  = QPushButton("Remove")
        for b in (btn_imap_add, btn_imap_edit, btn_imap_del):
            b.setFixedWidth(80)
        imap_btn_row.addWidget(btn_imap_add)
        imap_btn_row.addWidget(btn_imap_edit)
        imap_btn_row.addWidget(btn_imap_del)
        imap_btn_row.addStretch()

        btn_imap_add.clicked.connect(self._imap_add)
        btn_imap_edit.clicked.connect(self._imap_edit)
        btn_imap_del.clicked.connect(self._imap_remove)

        card, body = self._card("IMAP Accounts")
        body.addWidget(self._imap_list)
        body.addLayout(imap_btn_row)
        layout.addWidget(card)

        # ── Backup ────────────────────────────────────────────────────────────
        self._backup_enabled  = QCheckBox("Enable automatic backups on startup")
        self._backup_enabled.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; background: transparent;")
        self._backup_dir      = QLineEdit(); self._backup_dir.setPlaceholderText("backups")
        self._backup_max_keep = QSpinBox()
        self._backup_max_keep.setRange(1, 999)
        self._backup_max_keep.setValue(10)
        self._backup_max_keep.setFixedWidth(100)

        card, body = self._card("Backup")
        self._check_row(body, self._backup_enabled)
        self._field_row(body, "Backup Folder", self._backup_dir)
        self._field_row(body, "Max Copies to Keep", self._backup_max_keep)
        layout.addWidget(card)

        # ── Expenses / Notifications ──────────────────────────────────────────
        self._auto_recurring   = QCheckBox("Auto-log recurring expenses on startup")
        self._notify_recurring = QCheckBox("Send Discord notification for each auto-logged expense")
        self._notify_monthly   = QCheckBox("Post monthly expense summary to Discord")
        for cb in (self._auto_recurring, self._notify_recurring, self._notify_monthly):
            cb.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; background: transparent;")

        self._large_threshold = QSpinBox()
        self._large_threshold.setRange(0, 9999999)
        self._large_threshold.setFixedWidth(160)
        self._large_threshold.setSuffix("  ¢")

        card, body = self._card("Expenses & Notifications")
        self._check_row(body, self._auto_recurring)
        self._check_row(body, self._notify_recurring)
        self._check_row(body, self._notify_monthly)
        self._divider(body)
        self._field_row(body, "Large Expense Alert (¢)", self._large_threshold)
        layout.addWidget(card)

        layout.addStretch()

        # ── Save button ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 8, 8, 0)
        btn_row.addStretch()
        self._btn_save = QPushButton("Save Settings")
        self._btn_save.setFixedWidth(160)
        self._btn_save.setObjectName("btnAdd")
        self._btn_save.clicked.connect(self._save)
        btn_row.addWidget(self._btn_save)
        root.addLayout(btn_row)

    # ── Load current values ───────────────────────────────────────────────────

    def _load_values(self):
        import config
        self._discord_webhook.setText(config.DISCORD_WEBHOOK_URL or "")
        self._discord_bot_token.setText(config.DISCORD_BOT_TOKEN or "")
        self._imap_accounts = [dict(a) for a in (config.IMAP_ACCOUNTS or [])]
        self._imap_refresh_list()

        self._backup_enabled.setChecked(config.BACKUP_ENABLED)
        self._backup_dir.setText(config.BACKUP_DIR or "backups")
        self._backup_max_keep.setValue(config.BACKUP_MAX_KEEP)
        self._auto_recurring.setChecked(config.AUTO_LOG_RECURRING)
        self._notify_recurring.setChecked(config.NOTIFY_DISCORD_ON_RECURRING)
        self._notify_monthly.setChecked(config.POST_MONTHLY_EXPENSE_SUMMARY)
        self._large_threshold.setValue(config.LARGE_EXPENSE_THRESHOLD_CENTS)

    # ── IMAP account helpers ──────────────────────────────────────────────────

    def _imap_refresh_list(self):
        self._imap_list.clear()
        for a in self._imap_accounts:
            label = a.get("label") or ""
            user  = a.get("user", "")
            text  = f"{label}  —  {user}" if label and label != user else user
            self._imap_list.addItem(text)

    def _imap_add(self):
        dlg = _ImapAccountDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            v = dlg.values()
            if v["user"]:
                self._imap_accounts.append(v)
                self._imap_refresh_list()

    def _imap_edit(self):
        row = self._imap_list.currentRow()
        if row < 0:
            return
        dlg = _ImapAccountDialog(self, self._imap_accounts[row])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._imap_accounts[row] = dlg.values()
            self._imap_refresh_list()

    def _imap_remove(self):
        row = self._imap_list.currentRow()
        if row < 0:
            return
        self._imap_accounts.pop(row)
        self._imap_refresh_list()

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self):
        import config
        from dotenv import set_key
        import configparser as _cp

        env_path = os.path.join(config.BASE_DIR, ".env")
        ini_path = os.path.join(config.BASE_DIR, "settings.ini")

        # ── Write secrets to .env ─────────────────────────────────────────────
        import json as _json
        secrets = {
            "DISCORD_WEBHOOK_URL": self._discord_webhook.text().strip(),
            "DISCORD_BOT_TOKEN":   self._discord_bot_token.text().strip(),
            "IMAP_ACCOUNTS":       _json.dumps(self._imap_accounts),
        }
        for key, val in secrets.items():
            set_key(env_path, key, val)
            os.environ[key] = val

        # ── Write non-secrets to settings.ini ────────────────────────────────
        ini = _cp.ConfigParser()
        ini.read(ini_path)

        def _set(section, key, value):
            if not ini.has_section(section):
                ini.add_section(section)
            ini.set(section, key, value)

        _set("backup", "enabled",   "true" if self._backup_enabled.isChecked() else "false")
        _set("backup", "directory", self._backup_dir.text().strip() or "backups")
        _set("backup", "max_keep",  str(self._backup_max_keep.value()))
        _set("expenses", "auto_log_recurring",
             "true" if self._auto_recurring.isChecked() else "false")
        _set("expenses", "notify_discord_on_recurring",
             "true" if self._notify_recurring.isChecked() else "false")
        _set("expenses", "large_expense_alert_threshold",
             str(self._large_threshold.value()))
        _set("notifications", "post_monthly_expense_summary",
             "true" if self._notify_monthly.isChecked() else "false")

        with open(ini_path, "w") as fh:
            ini.write(fh)

        # ── Patch live config module so changes take effect immediately ────────
        config.DISCORD_WEBHOOK_URL          = secrets["DISCORD_WEBHOOK_URL"]
        config.DISCORD_BOT_TOKEN            = secrets["DISCORD_BOT_TOKEN"]
        config.IMAP_ACCOUNTS                = list(self._imap_accounts)
        config.GMAIL_USER                   = self._imap_accounts[0]["user"] if self._imap_accounts else ""
        config.GMAIL_PASS                   = self._imap_accounts[0]["pass"] if self._imap_accounts else ""
        config.BACKUP_ENABLED               = self._backup_enabled.isChecked()
        config.BACKUP_DIR                   = self._backup_dir.text().strip() or "backups"
        config.BACKUP_MAX_KEEP              = self._backup_max_keep.value()
        config.AUTO_LOG_RECURRING           = self._auto_recurring.isChecked()
        config.NOTIFY_DISCORD_ON_RECURRING  = self._notify_recurring.isChecked()
        config.POST_MONTHLY_EXPENSE_SUMMARY = self._notify_monthly.isChecked()
        config.LARGE_EXPENSE_THRESHOLD_CENTS = self._large_threshold.value()

        QMessageBox.information(self, "Settings Saved", "Settings saved successfully.")


# ── Startup splash ────────────────────────────────────────────────────────────

class SplashScreen(QWidget):
    """Frameless loading screen shown while the initial email sync runs."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedSize(460, 230)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(44, 38, 44, 32)
        layout.setSpacing(0)

        logo = QLabel("Vanguard")
        logo.setStyleSheet(
            f"font-size: 20px; font-weight: 700; color: {theme.BLUE}; background: transparent;"
        )
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)

        layout.addSpacing(6)

        sub = QLabel("Syncing your orders…")
        sub.setStyleSheet(
            f"font-size: 11px; color: {theme.TEXT_SECONDARY}; background: transparent;"
        )
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(sub)

        layout.addStretch()

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)       # indeterminate pulsing animation
        self._bar.setFixedHeight(5)
        self._bar.setTextVisible(False)
        layout.addWidget(self._bar)

        layout.addSpacing(10)

        self._msg = QLabel("Starting up…")
        self._msg.setStyleSheet(
            f"font-size: 9px; color: {theme.TEXT_PRIMARY}; background: transparent; letter-spacing: 0.3px;"
        )
        self._msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._msg.setWordWrap(True)
        layout.addWidget(self._msg)

        self.setStyleSheet(f"""
            SplashScreen {{
                background: {theme.BG_BASE};
                border: 1px solid {theme.BG_ELEVATED};
                border-radius: 12px;
            }}
            QProgressBar {{
                border: none;
                border-radius: 2px;
                background: {theme.BG_ELEVATED};
            }}
            QProgressBar::chunk {{
                background: {theme.BLUE};
                border-radius: 2px;
            }}
        """)

        # Centre on primary screen
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            screen.x() + (screen.width()  - self.width())  // 2,
            screen.y() + (screen.height() - self.height()) // 2,
        )

    def set_message(self, msg: str):
        self._msg.setText(msg)
        QApplication.processEvents()


# ── Main Window ───────────────────────────────────────────────────────────────

# ── Email sync background worker ───────────────────────────────────────────────

class EmailSyncWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def run(self):
        try:
            import email_sync
            counts = email_sync.run_sync(progress_callback=self.progress.emit)
            self.finished.emit(counts)
        except Exception as e:
            self.progress.emit(f"Email sync error: {e}")
            self.finished.emit({"imported": 0, "updated": 0, "skipped": 0, "errors": 1})


class ManualScrapeWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)

    def __init__(self, retailer: str, days_back: int, accounts: list, parent=None):
        super().__init__(parent)
        self._retailer  = retailer
        self._days_back = days_back
        self._accounts  = accounts  # subset of config.IMAP_ACCOUNTS to scan

    def run(self):
        try:
            import email_sync
            counts = email_sync.run_scrape(
                self._retailer, self._days_back,
                progress_callback=self.progress.emit,
                accounts=self._accounts,
            )
            self.finished.emit(counts)
        except Exception as e:
            self.progress.emit(f"Scrape error: {e}")
            self.finished.emit({"imported": 0, "updated": 0, "skipped": 0, "errors": 1})


class ManualScrapeDialog(QDialog):
    """Dialog for selecting retailer + days-back before launching a manual scrape."""

    _RETAILERS_INBOUND = ["pokemon_center", "walmart", "target", "five_below", "topps", "nike", "bestbuy", "amazon", "shopify"]
    _RETAILERS_SALES   = ["ebay", "stockx"]
    _LABELS = {
        "pokemon_center": "Pokemon Center",
        "walmart":        "Walmart",
        "target":         "Target",
        "five_below":     "Five Below",
        "topps":          "Topps",
        "nike":           "Nike",
        "bestbuy":        "Best Buy",
        "amazon":         "Amazon",
        "shopify":        "Shopify (All Stores)",
        "ebay":           "eBay",
        "stockx":         "StockX",
    }

    scrape_requested = pyqtSignal(str, int)   # retailer, days_back

    def __init__(self, parent=None, retailers: Optional[List[str]] = None):
        super().__init__(parent)
        # Default to inbound-only when called from the orders tab
        self._active_retailers = retailers if retailers is not None else self._RETAILERS_INBOUND
        self.setWindowTitle("Manual Email Scrape")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._worker: Optional[ManualScrapeWorker] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── Header ────────────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet(
            f"background: {theme.BG_CARD}; border-bottom: 1px solid {theme.BG_ELEVATED};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(24, 18, 24, 16)
        hl.setSpacing(4)

        title_lbl = QLabel("Email Scrape")
        title_lbl.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {theme.TEXT_PRIMARY};"
        )
        is_sales = self._active_retailers == self._RETAILERS_SALES
        sub_text = ("Fetch sale emails from eBay / StockX and import into the tracker"
                    if is_sales else
                    "Fetch order emails and import into the tracker")
        sub_lbl = QLabel(sub_text)
        sub_lbl.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_SECONDARY};")
        hl.addWidget(title_lbl)
        hl.addWidget(sub_lbl)
        layout.addWidget(header)

        # ── Form body ─────────────────────────────────────────────────────────
        body = QWidget()
        body.setStyleSheet(f"background: {theme.BG_BASE};")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(24, 20, 24, 8)
        bl.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._retailer_combo = QComboBox()
        for key in self._active_retailers:
            self._retailer_combo.addItem(self._LABELS.get(key, key), key)
        form.addRow("Retailer:", self._retailer_combo)

        self._days_spin = QSpinBox()
        self._days_spin.setRange(1, 365)
        self._days_spin.setValue(30)
        self._days_spin.setSuffix(" days")
        form.addRow("Days back:", self._days_spin)

        bl.addLayout(form)

        # ── Account checkboxes ────────────────────────────────────────────────
        import config as _cfg
        self._acct_checkboxes: List[tuple] = []  # (QCheckBox, account_dict)
        accounts = _cfg.IMAP_ACCOUNTS or []
        if len(accounts) > 1:
            acct_lbl = QLabel("Accounts to scan:")
            acct_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 11px;")
            bl.addWidget(acct_lbl)
            accts_frame = QFrame()
            accts_frame.setStyleSheet(
                f"background: {theme.BG_CARD}; border: 1px solid {theme.BG_ELEVATED};"
                f" border-radius: 6px;"
            )
            af = QVBoxLayout(accts_frame)
            af.setContentsMargins(12, 8, 12, 8)
            af.setSpacing(6)
            for acct in accounts:
                label = acct.get("label") or acct.get("user", "")
                user  = acct.get("user", "")
                text  = f"{label}  ({user})" if label != user else user
                cb = QCheckBox(text)
                cb.setChecked(True)
                cb.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 12px;")
                af.addWidget(cb)
                self._acct_checkboxes.append((cb, acct))
            bl.addWidget(accts_frame)

        # Progress / result area
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: 11px; padding: 8px 12px;"
            f"background: {theme.BG_CARD}; border-radius: 6px;"
            f"border: 1px solid {theme.BG_ELEVATED};"
        )
        self._progress_label.setWordWrap(True)
        self._progress_label.hide()
        bl.addWidget(self._progress_label)

        self._result_label = QLabel("")
        self._result_label.setStyleSheet(
            f"color: {theme.GREEN}; font-size: 11px; font-weight: 600; padding: 8px 12px;"
            f"background: {theme.BG_CARD}; border-radius: 6px;"
            f"border: 1px solid {theme.BG_ELEVATED};"
        )
        self._result_label.setWordWrap(True)
        self._result_label.hide()
        bl.addWidget(self._result_label)

        layout.addWidget(body)

        # ── Footer buttons ────────────────────────────────────────────────────
        footer = QWidget()
        footer.setStyleSheet(
            f"background: {theme.BG_BASE}; border-top: 1px solid {theme.BG_ELEVATED};"
        )
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 14, 24, 18)
        fl.setSpacing(10)
        fl.addStretch()

        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)

        self._run_btn = QPushButton("Run Scrape")
        self._run_btn.setObjectName("btnAdd")
        self._run_btn.clicked.connect(self._run)

        fl.addWidget(self._close_btn)
        fl.addWidget(self._run_btn)
        layout.addWidget(footer)

    def _run(self):
        if self._worker and self._worker.isRunning():
            return
        retailer  = self._retailer_combo.currentData()
        days_back = self._days_spin.value()

        # Build the accounts list from checkboxes (or None if all selected / no multi-acct)
        if self._acct_checkboxes:
            selected = [acct for cb, acct in self._acct_checkboxes if cb.isChecked()]
            if not selected:
                QMessageBox.warning(self, "No Accounts", "Select at least one account to scan.")
                return
            accounts = selected
        else:
            accounts = None  # use all configured accounts

        self._run_btn.setEnabled(False)
        self._result_label.hide()
        self._progress_label.setText("Starting…")
        self._progress_label.show()

        self._worker = ManualScrapeWorker(retailer, days_back, accounts, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, msg: str):
        self._progress_label.setText(msg)

    def _on_finished(self, counts: dict):
        self._run_btn.setEnabled(True)
        self._progress_label.hide()
        imp = counts.get("imported", 0)
        upd = counts.get("updated",  0)
        skp = counts.get("skipped",  0)
        err = counts.get("errors",   0)
        color = theme.RED if err else theme.GREEN
        self._result_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 600; padding: 8px 12px;"
            f"background: {theme.BG_CARD}; border-radius: 6px;"
            f"border: 1px solid {theme.BG_ELEVATED};"
        )
        self._result_label.setText(
            f"Done — {imp} imported, {upd} updated, {skp} skipped"
            + (f", {err} error(s)" if err else "")
        )
        self._result_label.show()
        self.scrape_requested.emit(
            self._retailer_combo.currentData(), self._days_spin.value()
        )


class UpdateCheckWorker(QThread):
    """Background thread that checks GitHub Releases for a newer version."""
    update_available = pyqtSignal(str, str)   # (latest_version, download_url)

    # !! Replace with your actual GitHub username/repo !!
    _GITHUB_REPO = "ElmoVantage/vantage"

    def run(self):
        try:
            import requests
            from version import __version__ as current
            url = f"https://api.github.com/repos/{self._GITHUB_REPO}/releases/latest"
            resp = requests.get(url, timeout=5,
                                headers={"Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                return
            data = resp.json()
            latest = data.get("tag_name", "").lstrip("v")
            if not latest:
                return
            # Simple version comparison: split on "." and compare as ints
            def _ver(v):
                try:
                    return tuple(int(x) for x in v.split("."))
                except ValueError:
                    return (0,)
            if _ver(latest) > _ver(current):
                assets = data.get("assets", [])
                dl = next(
                    (a["browser_download_url"] for a in assets if a["name"].endswith(".zip")),
                    data.get("html_url", ""),
                )
                self.update_available.emit(latest, dl)
        except Exception:
            pass   # silently skip if offline or rate-limited


class TrackingRefreshWorker(QThread):
    finished = pyqtSignal(dict)

    def run(self):
        try:
            import tracking
            counts = tracking.refresh_all_shipped()
            self.finished.emit(counts)
        except Exception as e:
            print(f"[TrackingWorker] {e}")
            self.finished.emit({"refreshed": 0, "failed": 0, "skipped": 0})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vantage Tracker")
        self.resize(1600, 960)
        self._build_ui()
        self._setup_watcher()
        self._refresh_all()
        self._start_email_sync()
        self._check_return_reminders()
        self._reminder_timer = QTimer(self)
        self._reminder_timer.setInterval(60 * 60 * 1000)  # 1 hour
        self._reminder_timer.timeout.connect(self._check_return_reminders)
        self._reminder_timer.start()
        self._schedule_daily_delivery_report()
        self._check_for_updates()

    def _check_for_updates(self):
        worker = UpdateCheckWorker()
        worker.update_available.connect(self._on_update_available)
        worker.setParent(self)   # keep alive until finished
        worker.start()

    def _on_update_available(self, latest_version: str, url: str):
        self._update_banner.setText(
            f"⬆ Update available: v{latest_version} — "
            f"<a href='{url}' style='color:{theme.BLUE};'>Download</a>"
        )
        self._update_banner.setVisible(True)

    def _build_ui(self):
        # ── Central container ──────────────────────────────────────────────────
        central = QWidget()
        central.setStyleSheet(f"background: {theme.BG_BASE};")
        root_vl = QVBoxLayout(central)
        root_vl.setContentsMargins(0, 0, 0, 0)
        root_vl.setSpacing(0)
        self.setCentralWidget(central)

        # ── Brand strip ────────────────────────────────────────────────────────
        brand_bar = QWidget()
        brand_bar.setFixedHeight(38)
        brand_bar.setStyleSheet(
            f"background: {theme.BG_BASE};"
            f"border-bottom: 1px solid {theme.BG_ELEVATED};"
        )
        brand_hl = QHBoxLayout(brand_bar)
        brand_hl.setContentsMargins(20, 0, 20, 0)

        logo = QLabel("V")
        logo.setStyleSheet(
            f"font-size: 15px; font-weight: 800; letter-spacing: 3px; color: {theme.BLUE};"
        )
        brand_hl.addWidget(logo)

        sep_lbl = QLabel("·")
        sep_lbl.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: 13px; padding: 0 8px;")
        brand_hl.addWidget(sep_lbl)

        app_name = QLabel("Vanguard")
        app_name.setStyleSheet(
            f"font-size: 12px; font-weight: 500; color: {theme.TEXT_SECONDARY}; letter-spacing: 0.3px;"
        )
        brand_hl.addWidget(app_name)
        brand_hl.addStretch()

        # Update notification label (hidden by default, shown by update checker)
        self._update_banner = QLabel()
        self._update_banner.setOpenExternalLinks(True)
        self._update_banner.setStyleSheet(
            f"color: {theme.BLUE}; font-size: 11px; padding: 0 8px;"
        )
        self._update_banner.setVisible(False)
        brand_hl.addWidget(self._update_banner)

        root_vl.addWidget(brand_bar)

        # ── Tab widget ─────────────────────────────────────────────────────────
        tabs = QTabWidget()

        self.dashboard_tab      = DashboardTab()
        self.order_tab          = OrderTrackerTab()
        self.account_health_tab = AccountHealthTab()
        self.inventory_tab      = InventoryTab()
        self.sales_tab          = SalesTab()
        self.expenses_tab       = ExpensesTab()
        self.settings_tab       = SettingsTab()

        tabs.addTab(self.dashboard_tab,      "Dashboard")
        tabs.addTab(self.order_tab,          "Order Tracker")
        tabs.addTab(self.account_health_tab, "Account Health")
        tabs.addTab(self.inventory_tab,      "Inventory")
        tabs.addTab(self.sales_tab,          "Outbound Sales")
        tabs.addTab(self.expenses_tab,       "Expenses")
        tabs.addTab(self.settings_tab,       "Settings")

        # Cross-tab refresh
        self.order_tab.refresh_needed.connect(lambda: self.inventory_tab.load_data())
        self.order_tab.refresh_needed.connect(lambda: self.account_health_tab.load_data())
        self.order_tab.refresh_needed.connect(self._update_status_bar)
        self.order_tab.refresh_needed.connect(lambda: self.dashboard_tab.load_data())
        self.inventory_tab.refresh_needed.connect(self._update_status_bar)
        self.inventory_tab.refresh_needed.connect(lambda: self.dashboard_tab.load_data())
        self.sales_tab.refresh_needed.connect(lambda: self.inventory_tab.load_data())
        self.sales_tab.refresh_needed.connect(self._update_status_bar)
        self.sales_tab.refresh_needed.connect(lambda: self.dashboard_tab.load_data())
        self.expenses_tab.refresh_needed.connect(self._update_status_bar)
        self.expenses_tab.refresh_needed.connect(lambda: self.dashboard_tab.load_data())

        tabs.currentChanged.connect(self._on_tab_changed)

        root_vl.addWidget(tabs)
        self._tabs = tabs

        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet(
            f"QStatusBar {{ background: {theme.BG_BASE}; color: {theme.TEXT_SECONDARY};"
            f" font-size: 11px; border-top: 1px solid {theme.BG_ELEVATED}; padding: 0 8px; }}"
        )
        self.setStatusBar(self._status_bar)

    def _setup_watcher(self):
        self._watcher = QFileSystemWatcher([db.get_db_path()])
        self._debounce = QTimer()
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(600)
        self._watcher.fileChanged.connect(lambda _: self._debounce.start())
        self._debounce.timeout.connect(self._on_db_changed)

    def _on_db_changed(self):
        # Re-add path if watcher dropped it (SQLite replaces file on write)
        if db.get_db_path() not in self._watcher.files():
            self._watcher.addPath(db.get_db_path())
        self._refresh_all()

    def _on_tab_changed(self, index: int):
        self._refresh_active()

    def _refresh_active(self):
        idx = self._tabs.currentIndex()
        if idx == 0:
            self.dashboard_tab.load_data()
        elif idx == 1:
            self.order_tab.load_data()
        elif idx == 2:
            self.account_health_tab.load_data()
        elif idx == 3:
            self.inventory_tab.load_data()
        elif idx == 4:
            self.sales_tab.load_data()
        elif idx == 5:
            self.expenses_tab.load_data()
        self._update_status_bar()

    def _refresh_all(self):
        self.dashboard_tab.load_data()
        self.order_tab.load_data()
        self.account_health_tab.load_data()
        self.inventory_tab.load_data()
        self.sales_tab.load_data()
        self.expenses_tab.load_data()
        self._update_status_bar()

    def _update_status_bar(self):
        try:
            inv = db.get_inventory_report()
            exp = db.get_expense_totals()
            ts  = datetime.now().strftime("%H:%M")
            self._status_bar.showMessage(
                f"  Units: {inv['total_units']}  ·  "
                f"Value: {db.format_money(inv['total_value_cents'])}  ·  "
                f"Expenses MTD: {db.format_money(exp['month_cents'])}  ·  "
                f"YTD: {db.format_money(exp['year_cents'])}  ·  "
                f"Updated {ts}"
            )
        except Exception:
            pass

    def _check_return_reminders(self):
        try:
            due = db.get_due_return_reminders()
            for reminder in due:
                order_date = reminder["order_date"]
                try:
                    from datetime import date
                    days_elapsed = (date.today() - date.fromisoformat(order_date)).days
                except Exception:
                    days_elapsed = 25
                import webhooks
                webhooks.notify_return_reminder_due(
                    item_name    = reminder["item_name"],
                    order_date   = order_date,
                    days_elapsed = days_elapsed,
                )
                db.mark_reminder_notified(reminder["id"])
                print(f"[Reminders] Fired return reminder for '{reminder['item_name']}'")
        except Exception as exc:
            print(f"[Reminders] check error: {exc}")

    def _schedule_daily_delivery_report(self):
        """Schedule the next 7am daily delivery report webhook."""
        from datetime import datetime, timedelta
        now    = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        ms = int((target - now).total_seconds() * 1000)
        QTimer.singleShot(ms, self._send_daily_delivery_report)
        print(f"[DailyReport] Next delivery report scheduled for {target.strftime('%Y-%m-%d 07:00')}")

    def _send_daily_delivery_report(self):
        """Build today's delivery forecast and fire the Discord webhook."""
        try:
            from datetime import date
            import webhooks as _wh
            today = date.today().isoformat()

            orders  = db.get_inbound_orders()
            shipped = [
                o for o in orders
                if o["status"] == "shipped"
                and (o.get("estimated_delivery") or o.get("order_date") or "")[:10] == today
            ]

            # Group by normalized address (reuse the same logic as the calendar view)
            by_addr: dict = {}   # norm_key → {display_addr, pkg_count, items: {name: qty}}
            for o in shipped:
                raw_addr  = o.get("delivery_address") or ""
                disp_addr = _format_address_short(raw_addr) or raw_addr or "Unknown address"
                norm_key  = _normalize_addr_key(disp_addr)
                if norm_key not in by_addr:
                    by_addr[norm_key] = {"pkg_count": 0, "items": {}}
                    # Display address = first-seen variant (already short-formatted)
                    by_addr[norm_key]["display_addr"] = disp_addr
                by_addr[norm_key]["pkg_count"] += 1
                name = o.get("item_name") or "Unknown"
                by_addr[norm_key]["items"][name] = (
                    by_addr[norm_key]["items"].get(name, 0) + (o.get("quantity") or 1)
                )

            # Convert inner dict to sorted list for the webhook
            report = {
                info["display_addr"]: {
                    "pkg_count": info["pkg_count"],
                    "items":     list(info["items"].items()),
                }
                for info in by_addr.values()
            }
            _wh.notify_daily_delivery_report(today, report)
            print(f"[DailyReport] Sent delivery report for {today} ({len(shipped)} packages, {len(report)} addresses)")
        except Exception as exc:
            print(f"[DailyReport] error: {exc}")
        finally:
            self._schedule_daily_delivery_report()   # always reschedule for tomorrow

    def _start_email_sync(self):
        self._status_bar.showMessage("  Syncing emails in background…")
        self._sync_worker = EmailSyncWorker()
        self._sync_worker.progress.connect(
            lambda msg: self._status_bar.showMessage(f"  {msg}")
        )
        self._sync_worker.finished.connect(self._on_sync_finished)
        self._sync_worker.start()

    def _on_sync_finished(self, counts: dict):
        imported = counts.get("imported", 0)
        updated  = counts.get("updated", 0)
        errors   = counts.get("errors", 0)
        if imported or updated:
            self._refresh_all()
        msg = (
            f"  Email sync done — {imported} new order(s), {updated} updated"
            + (f", {errors} error(s)" if errors else "")
        )
        self._status_bar.showMessage(msg, 8000)
        QTimer.singleShot(8000, self._update_status_bar)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    db.startup()

    # Launch Discord bot as a background process — terminates when the app closes
    _bot_script = Path(__file__).parent / "discord_bot.py"
    _no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    _bot_proc = subprocess.Popen(
        [sys.executable, str(_bot_script)],
        creationflags=_no_window,
    )

    app = QApplication(sys.argv)
    app.aboutToQuit.connect(_bot_proc.terminate)
    app.setStyle("Fusion")

    # Load Inter from bundled fonts/ dir if present, fall back to Segoe UI
    from PyQt6.QtGui import QFontDatabase
    fonts_dir = Path(__file__).parent / "fonts"
    if fonts_dir.exists():
        for f in fonts_dir.glob("*.ttf"):
            QFontDatabase.addApplicationFont(str(f))
        for f in fonts_dir.glob("*.otf"):
            QFontDatabase.addApplicationFont(str(f))
    font_family = "Inter" if "Inter" in QFontDatabase.families() else "Segoe UI"
    font = QFont(font_family, 10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    app.setStyleSheet(APP_STYLE)

    def _auto_select_all(_old, new):
        if isinstance(new, (QLineEdit, QSpinBox, QDoubleSpinBox)):
            QTimer.singleShot(0, new.selectAll)
        elif isinstance(new, QDateEdit):
            QTimer.singleShot(0, new.selectAll)
    app.focusChanged.connect(_auto_select_all)

    # Set app icon (works both from source and from PyInstaller bundle)
    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # ── License gate ────────────────────────────────────────────────────────────
    import config as _cfg
    if not _cfg.DEV_MODE:
        licensed, reason = _license.is_licensed()
        if not licensed:
            # Map internal reason codes to user-facing messages
            initial_msg = {
                "no_key":  "",
                "expired": "Your subscription has expired. Please renew to continue.",
            }.get(reason, reason if reason not in ("ok", "cached") else "")

            while True:
                dlg = LicenseDialog(initial_msg)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    break          # key accepted — proceed
                # User closed the dialog without activating → quit
                sys.exit(0)

    splash = SplashScreen()
    splash.show()
    app.processEvents()

    window = MainWindow()
    window.setMinimumSize(1100, 680)

    worker = getattr(window, "_sync_worker", None)
    if worker and worker.isRunning():
        worker.progress.connect(splash.set_message)
        def _on_sync_done():
            splash.close()
            window.showNormal()
            window.raise_()
            window.activateWindow()
        worker.finished.connect(_on_sync_done)
    else:
        splash.close()
        window.showNormal()
        window.raise_()
        window.activateWindow()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
