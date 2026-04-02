"""
theme.py — Single source of truth for all visual constants.

Edit this file to retheme the entire application.
Every widget that needs a color imports it from here.
"""

from PyQt6.QtGui import QColor

# ── Background layers ─────────────────────────────────────────────────────────
BG_BASE     = "#0e0e0f"    # surface
BG_CARD     = "#1a191b"    # surface-container
BG_ELEVATED = "#201f21"    # surface-container-high
BORDER      = "#484849"    # outline-variant

# ── Derived surface colors ────────────────────────────────────────────────────
BG_INPUT     = "#131213"   # input / combobox background
BG_HOVER     = "#242224"   # general hover state
SELECTION_BG = "#1a2a2e"   # table row selected (cyan tint)

# ── Text ──────────────────────────────────────────────────────────────────────
TEXT_PRIMARY   = "#f4f2f4"
TEXT_SECONDARY = "#adaaab"
TEXT_MUTED     = "#6b6869"

# ── Accent palette ────────────────────────────────────────────────────────────
BLUE   = "#99f7ff"   # primary cyan
TEAL   = "#00e2ee"   # primary-dim
GREEN  = "#4caf82"
ORANGE = "#f7a94f"
PURPLE = "#99f7ff"   # mapped to cyan (was purple #a68cff)
RED    = "#ff716c"

# ── PyQt6 QColor versions (for use in Python model/delegate code) ─────────────
QCOLOR_GREEN  = QColor(76,  175, 130)   # GREEN
QCOLOR_RED    = QColor(255, 113, 108)   # RED
QCOLOR_GOLD   = QColor(247, 169,  79)   # ORANGE

# ── Dashboard card accent cycle ───────────────────────────────────────────────
CARD_ACCENTS = [BLUE, ORANGE, GREEN, RED, PURPLE, TEAL]

# ── Font ──────────────────────────────────────────────────────────────────────
FONT_FAMILY = "Inter, Segoe UI, system-ui, sans-serif"
FONT_SIZE   = 10   # pt, applied as app default

# ── Full Qt stylesheet (generated from constants above) ───────────────────────
# Uses .replace() so the template stays readable without escaped braces.

_STYLE = """
/* ── Base ─────────────────────────────────────────────────────────────── */
QMainWindow, QDialog {
    background: $BG_BASE;
    font-family: $FONT_FAMILY;
}
QWidget { font-family: $FONT_FAMILY; }

/* ── Tab bar ──────────────────────────────────────────────────────────── */
QTabWidget::pane {
    border: none;
    border-top: 1px solid $BG_ELEVATED;
    background: $BG_BASE;
}
QTabBar {
    background: $BG_BASE;
    border-bottom: 1px solid $BG_ELEVATED;
}
QTabBar::tab {
    background: transparent;
    color: $TEXT_SECONDARY;
    padding: 13px 22px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.6px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
    text-transform: uppercase;
}
QTabBar::tab:selected {
    color: $BLUE;
    border-bottom: 2px solid $BLUE;
}
QTabBar::tab:hover:!selected {
    color: $TEXT_PRIMARY;
    border-bottom: 2px solid $BORDER;
}

/* ── Tables ───────────────────────────────────────────────────────────── */
QTableView {
    background: $BG_CARD;
    color: $TEXT_PRIMARY;
    gridline-color: transparent;
    selection-background-color: $SELECTION_BG;
    selection-color: #ffffff;
    alternate-background-color: $BG_BASE;
    border: none;
    font-size: 12px;
}
QTableView QHeaderView::section {
    background: $BG_BASE;
    color: $TEXT_SECONDARY;
    padding: 9px 12px;
    border: none;
    border-bottom: 1px solid $BG_ELEVATED;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
QHeaderView { background: $BG_BASE; border: none; }
QTableView QAbstractButton { background: $BG_BASE; border: none; }
QTableView::item { padding: 7px 12px; border: none; }
QTableView::item:selected { background: $SELECTION_BG; }
QTableView::item:hover:!selected { background: $BG_ELEVATED; }

/* ── Inputs ───────────────────────────────────────────────────────────── */
QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QDateEdit {
    background: $BG_INPUT;
    color: $TEXT_PRIMARY;
    border: 1px solid $BG_ELEVATED;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    selection-background-color: $BORDER;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTextEdit:focus, QDateEdit:focus {
    border-color: $BLUE;
    background: $BG_ELEVATED;
}
QComboBox {
    background: $BG_INPUT;
    color: $TEXT_PRIMARY;
    border: 1px solid $BG_ELEVATED;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}
QComboBox:focus { border-color: $BLUE; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $TEXT_SECONDARY;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: $BG_INPUT;
    color: $TEXT_PRIMARY;
    border: 1px solid $BG_ELEVATED;
    border-radius: 6px;
    selection-background-color: $SELECTION_BG;
    outline: none;
    padding: 2px;
}

/* ── Buttons ──────────────────────────────────────────────────────────── */
QPushButton {
    background: $BG_ELEVATED;
    color: $TEXT_PRIMARY;
    border: 1px solid $BORDER;
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton:hover {
    background: $BG_CARD;
    border-color: $BORDER;
    color: #ffffff;
}
QPushButton:pressed { background: $BORDER; }
QPushButton#btnAdd {
    background: $BG_ELEVATED;
    color: $BLUE;
    border: 1px solid $BLUE;
    font-weight: 700;
    padding: 7px 18px;
}
QPushButton#btnAdd:hover { background: $BG_CARD; border-color: $TEAL; color: $TEAL; }
QPushButton#btnAdd:pressed { background: $BORDER; color: $TEXT_PRIMARY; }
QPushButton#btnDelete {
    background: $RED;
    color: #ffffff;
    border: none;
    font-weight: 700;
    padding: 7px 18px;
}
QPushButton#btnDelete:hover { background: #e05252; }

/* ── Labels ───────────────────────────────────────────────────────────── */
QLabel { color: $TEXT_PRIMARY; background: transparent; }
QLabel#lblHeader {
    font-size: 15px;
    font-weight: 700;
    color: $BLUE;
}

/* ── Checkboxes ───────────────────────────────────────────────────────── */
QCheckBox { color: $TEXT_PRIMARY; spacing: 7px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border-radius: 4px;
    border: 1px solid $BORDER;
    background: $BG_INPUT;
}
QCheckBox::indicator:checked {
    background: $BLUE;
    border-color: $BLUE;
}
QCheckBox::indicator:hover { border-color: $BLUE; }

/* ── GroupBox ─────────────────────────────────────────────────────────── */
QGroupBox {
    border: 1px solid $BG_ELEVATED;
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 6px;
    color: $TEXT_SECONDARY;
    font-size: 11px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}

/* ── Menus ────────────────────────────────────────────────────────────── */
QMenu {
    background: $BG_CARD;
    color: $TEXT_PRIMARY;
    border: 1px solid $BORDER;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 7px 18px; border-radius: 4px; }
QMenu::item:selected { background: $BG_ELEVATED; color: #ffffff; }
QMenu::separator { height: 1px; background: $BG_ELEVATED; margin: 4px 8px; }

/* ── Status bar ───────────────────────────────────────────────────────── */
QStatusBar {
    background: $BG_BASE;
    color: $TEXT_SECONDARY;
    font-size: 11px;
    border-top: 1px solid $BG_ELEVATED;
}

/* ── Form ─────────────────────────────────────────────────────────────── */
QDialog { background: $BG_BASE; }
QFormLayout QLabel { color: $TEXT_PRIMARY; min-width: 130px; }

/* ── Calendar popup ───────────────────────────────────────────────────── */
QCalendarWidget {
    background: $BG_CARD;
    color: $TEXT_PRIMARY;
}
QCalendarWidget QWidget#qt_calendar_navigationbar {
    background: $BG_ELEVATED;
    color: $TEXT_PRIMARY;
    padding: 4px;
}
QCalendarWidget QToolButton {
    background: transparent;
    color: $TEXT_PRIMARY;
    font-size: 12px;
    font-weight: 600;
    border: none;
    border-radius: 4px;
    padding: 4px 8px;
}
QCalendarWidget QToolButton:hover { background: rgba(255,255,255,0.15); }
QCalendarWidget QSpinBox {
    background: transparent;
    color: $TEXT_PRIMARY;
    border: none;
    font-size: 12px;
    font-weight: 600;
    selection-background-color: $BORDER;
}
QCalendarWidget QSpinBox::up-button,
QCalendarWidget QSpinBox::down-button { width: 0; height: 0; }
QCalendarWidget QAbstractItemView {
    background: $BG_CARD;
    color: $TEXT_PRIMARY;
    selection-background-color: $BG_ELEVATED;
    selection-color: $BLUE;
    outline: none;
    border: none;
}
QCalendarWidget QAbstractItemView:disabled { color: $TEXT_MUTED; }

/* ── Scrollbars ───────────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: $BG_BASE; width: 8px; border-radius: 4px; margin: 0;
}
QScrollBar::handle:vertical {
    background: $BG_ELEVATED; border-radius: 4px; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: $BORDER; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal {
    background: $BG_BASE; height: 8px; border-radius: 4px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: $BG_ELEVATED; border-radius: 4px; min-width: 28px;
}
QScrollBar::handle:horizontal:hover { background: $BORDER; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
"""

APP_STYLE = (
    _STYLE
    .replace("$BG_BASE",     BG_BASE)
    .replace("$BG_CARD",     BG_CARD)
    .replace("$BG_ELEVATED", BG_ELEVATED)
    .replace("$BG_INPUT",    BG_INPUT)
    .replace("$BG_HOVER",    BG_HOVER)
    .replace("$BORDER",      BORDER)
    .replace("$SELECTION_BG",SELECTION_BG)
    .replace("$TEXT_PRIMARY",   TEXT_PRIMARY)
    .replace("$TEXT_SECONDARY", TEXT_SECONDARY)
    .replace("$TEXT_MUTED",     TEXT_MUTED)
    .replace("$BLUE",   BLUE)
    .replace("$TEAL",   TEAL)
    .replace("$GREEN",  GREEN)
    .replace("$ORANGE", ORANGE)
    .replace("$PURPLE", PURPLE)
    .replace("$RED",    RED)
    .replace("$FONT_FAMILY", FONT_FAMILY)
)
