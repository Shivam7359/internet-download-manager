"""
IDM UI — Premium Dark Theme Stylesheet
=========================================
Global QSS stylesheet for the entire application.

Applies a premium dark theme with:
    • Gradient accent colors (blue-to-cyan)
    • Glassmorphism card effects
    • Smooth micro-animations
    • Consistent color tokens
    • Glow progress bars
    • Modern typography
"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication

# ── Color Tokens ──────────────────────────────────────────────────────────────
# Single source of truth — reference these in code if needed.
COLORS = {
    "bg_primary":       "#0D1117",
    "bg_secondary":     "#161B22",
    "bg_tertiary":      "#1C2333",
    "bg_elevated":      "#21262D",
    "bg_hover":         "#30363D",
    "border_default":   "#21262D",
    "border_subtle":    "#30363D",
    "border_active":    "#58A6FF",
    "text_primary":     "#E6EDF3",
    "text_secondary":   "#C9D1D9",
    "text_muted":       "#8B949E",
    "text_dimmed":      "#6E7681",
    "accent_blue":      "#58A6FF",
    "accent_blue_dark": "#1F6FEB",
    "accent_cyan":      "#79C0FF",
    "accent_green":     "#3FB950",
    "accent_green_dk":  "#238636",
    "accent_red":       "#F85149",
    "accent_red_dk":    "#DA3633",
    "accent_orange":    "#D29922",
    "accent_purple":    "#BC8CFF",
    "gradient_start":   "#1F6FEB",
    "gradient_end":     "#58A6FF",
}

DARK_THEME = """
/* ═══════════════════════════════════════════════════════════════════
   GLOBAL RESET & TYPOGRAPHY
   ═══════════════════════════════════════════════════════════════════ */

* {
    font-family: "Segoe UI", "Inter", "Roboto", -apple-system, sans-serif;
    outline: none;
}

QMainWindow, QWidget {
    background-color: #0D1117;
    color: #E6EDF3;
}

/* ═══════════════════════════════════════════════════════════════════
   MENU BAR
   ═══════════════════════════════════════════════════════════════════ */

QMenuBar {
    background-color: #0D1117;
    color: #C9D1D9;
    border-bottom: 1px solid #21262D;
    padding: 2px 6px;
    font-size: 12px;
    font-weight: 500;
}

QMenuBar::item {
    padding: 6px 14px;
    border-radius: 6px;
}

QMenuBar::item:selected {
    background-color: #1C2333;
    color: #E6EDF3;
}

QMenu {
    background-color: #161B22;
    color: #C9D1D9;
    border: 1px solid #30363D;
    border-radius: 10px;
    padding: 6px;
}

QMenu::item {
    padding: 8px 36px 8px 16px;
    border-radius: 6px;
    font-size: 12px;
}

QMenu::item:selected {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #1F6FEB, stop: 1 #58A6FF
    );
    color: #FFFFFF;
}

QMenu::separator {
    height: 1px;
    background: #21262D;
    margin: 6px 12px;
}

QMenu::icon {
    padding-left: 8px;
}

/* ═══════════════════════════════════════════════════════════════════
   TOOLBAR
   ═══════════════════════════════════════════════════════════════════ */

QToolBar {
    background-color: #0D1117;
    border-bottom: 1px solid #21262D;
    spacing: 6px;
    padding: 8px 12px;
}

QToolBar::separator {
    width: 1px;
    background: #21262D;
    margin: 4px 8px;
}

QToolButton {
    background-color: transparent;
    color: #C9D1D9;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 12px;
    font-weight: 600;
    min-height: 32px;
}

QToolButton:hover {
    background-color: #1C2333;
    border-color: #30363D;
    color: #E6EDF3;
}

QToolButton:pressed {
    background-color: #30363D;
}

QToolButton:checked {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 rgba(31,111,235,0.15), stop: 1 rgba(88,166,255,0.15)
    );
    border-color: #58A6FF;
    color: #58A6FF;
}

/* ═══════════════════════════════════════════════════════════════════
   SCROLL BARS — Slim & modern
   ═══════════════════════════════════════════════════════════════════ */

QScrollBar:vertical {
    background: transparent;
    width: 8px;
    border: none;
    margin: 2px 0;
}

QScrollBar::handle:vertical {
    background: #30363D;
    min-height: 40px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background: #484F58;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    height: 0px;
    background: transparent;
}

QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    border: none;
    margin: 0 2px;
}

QScrollBar::handle:horizontal {
    background: #30363D;
    min-width: 40px;
    border-radius: 4px;
}

QScrollBar::handle:horizontal:hover {
    background: #484F58;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    width: 0px;
    background: transparent;
}

/* ═══════════════════════════════════════════════════════════════════
   INPUT FIELDS — Elevated surface
   ═══════════════════════════════════════════════════════════════════ */

QLineEdit {
    background-color: #161B22;
    color: #E6EDF3;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 13px;
    selection-background-color: #1F6FEB;
    selection-color: #FFFFFF;
}

QLineEdit:focus {
    border-color: #58A6FF;
    background-color: #1C2333;
}

QLineEdit:disabled {
    background-color: #0D1117;
    color: #6E7681;
    border-color: #21262D;
}

QTextEdit {
    background-color: #161B22;
    color: #E6EDF3;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    selection-background-color: #1F6FEB;
}

QTextEdit:focus {
    border-color: #58A6FF;
}

QSpinBox, QDoubleSpinBox {
    background-color: #161B22;
    color: #E6EDF3;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13px;
    min-height: 34px;
}

QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #58A6FF;
    background-color: #1C2333;
}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    border: none;
    width: 20px;
}

QComboBox {
    background-color: #161B22;
    color: #E6EDF3;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 13px;
    min-width: 100px;
    min-height: 34px;
}

QComboBox:hover {
    border-color: #484F58;
    background-color: #1C2333;
}

QComboBox:focus {
    border-color: #58A6FF;
}

QComboBox::drop-down {
    border: none;
    width: 28px;
}

QComboBox QAbstractItemView {
    background-color: #161B22;
    color: #E6EDF3;
    border: 1px solid #30363D;
    border-radius: 8px;
    selection-background-color: #1F6FEB;
    selection-color: #FFFFFF;
    outline: none;
    padding: 4px;
}

/* ═══════════════════════════════════════════════════════════════════
   BUTTONS — Gradient accent for primary actions
   ═══════════════════════════════════════════════════════════════════ */

QPushButton {
    background-color: #21262D;
    color: #C9D1D9;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 9px 22px;
    font-size: 13px;
    font-weight: 600;
    min-height: 34px;
}

QPushButton:hover {
    background-color: #30363D;
    border-color: #484F58;
    color: #E6EDF3;
}

QPushButton:pressed {
    background-color: #484F58;
}

QPushButton:disabled {
    background-color: #161B22;
    color: #6E7681;
    border-color: #21262D;
}

QPushButton#primary {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #1F6FEB, stop: 1 #388BFD
    );
    color: #FFFFFF;
    border: none;
}

QPushButton#primary:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #388BFD, stop: 1 #58A6FF
    );
}

QPushButton#primary:pressed {
    background-color: #1158C7;
}

QPushButton#success {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #238636, stop: 1 #2EA043
    );
    color: #FFFFFF;
    border: none;
}

QPushButton#success:hover {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #2EA043, stop: 1 #3FB950
    );
}

QPushButton#danger {
    background-color: #DA3633;
    color: #FFFFFF;
    border: none;
}

QPushButton#danger:hover {
    background-color: #F85149;
}

/* ═══════════════════════════════════════════════════════════════════
   CHECKBOXES & RADIO — Custom indicators
   ═══════════════════════════════════════════════════════════════════ */

QCheckBox {
    color: #C9D1D9;
    font-size: 13px;
    spacing: 10px;
    background: transparent;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid #30363D;
    border-radius: 4px;
    background: #161B22;
}

QCheckBox::indicator:hover {
    border-color: #58A6FF;
}

QCheckBox::indicator:checked {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #1F6FEB, stop: 1 #58A6FF
    );
    border-color: #58A6FF;
}

QRadioButton {
    color: #C9D1D9;
    font-size: 13px;
    spacing: 10px;
    background: transparent;
}

QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid #30363D;
    border-radius: 9px;
    background: #161B22;
}

QRadioButton::indicator:hover {
    border-color: #58A6FF;
}

QRadioButton::indicator:checked {
    background: #1F6FEB;
    border-color: #58A6FF;
}

/* ═══════════════════════════════════════════════════════════════════
   SLIDERS
   ═══════════════════════════════════════════════════════════════════ */

QSlider::groove:horizontal {
    height: 6px;
    background: #21262D;
    border-radius: 3px;
}

QSlider::handle:horizontal {
    width: 18px;
    height: 18px;
    margin: -6px 0;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #58A6FF, stop: 1 #79C0FF
    );
    border-radius: 9px;
    border: 2px solid #0D1117;
}

QSlider::handle:horizontal:hover {
    background: #79C0FF;
}

QSlider::sub-page:horizontal {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #1F6FEB, stop: 1 #58A6FF
    );
    border-radius: 3px;
}

/* ═══════════════════════════════════════════════════════════════════
   PROGRESS BAR — Glowing gradient
   ═══════════════════════════════════════════════════════════════════ */

QProgressBar {
    background-color: #161B22;
    border: 1px solid #21262D;
    border-radius: 6px;
    text-align: center;
    color: #E6EDF3;
    font-size: 11px;
    font-weight: 600;
    height: 22px;
}

QProgressBar::chunk {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #1F6FEB,
        stop: 0.5 #58A6FF,
        stop: 1 #79C0FF
    );
    border-radius: 5px;
}

/* ═══════════════════════════════════════════════════════════════════
   TABLE VIEW — Premium data grid
   ═══════════════════════════════════════════════════════════════════ */

QTableView, QTreeView, QListView {
    background-color: #0D1117;
    alternate-background-color: #111922;
    color: #E6EDF3;
    border: none;
    gridline-color: #161B22;
    selection-background-color: rgba(31, 111, 235, 0.2);
    selection-color: #E6EDF3;
    font-size: 12px;
}

QTableView::item, QTreeView::item, QListView::item {
    padding: 6px 10px;
    border-bottom: 1px solid #161B22;
}

QTableView::item:hover, QTreeView::item:hover, QListView::item:hover {
    background-color: rgba(88, 166, 255, 0.06);
}

QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {
    background-color: rgba(31, 111, 235, 0.18);
    border-left: 3px solid #58A6FF;
}

QHeaderView {
    background-color: #0D1117;
    border: none;
}

QHeaderView::section {
    background-color: #0D1117;
    color: #8B949E;
    border: none;
    border-bottom: 2px solid #21262D;
    padding: 10px 12px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

QHeaderView::section:hover {
    color: #E6EDF3;
    background-color: #161B22;
}

/* ═══════════════════════════════════════════════════════════════════
   GROUP BOXES — Glass card style
   ═══════════════════════════════════════════════════════════════════ */

QGroupBox {
    font-weight: 600;
    font-size: 13px;
    color: #58A6FF;
    border: 1px solid #21262D;
    border-radius: 10px;
    margin-top: 14px;
    padding: 22px 18px 18px;
    background-color: rgba(22, 27, 34, 0.6);
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 10px;
    background-color: #0D1117;
    border-radius: 4px;
}

/* ═══════════════════════════════════════════════════════════════════
   DIALOGS — Elevated surface
   ═══════════════════════════════════════════════════════════════════ */

QDialog {
    background-color: #161B22;
    color: #E6EDF3;
    border: 1px solid #21262D;
}

QMessageBox {
    background-color: #161B22;
}

QMessageBox QLabel {
    color: #E6EDF3;
    font-size: 13px;
}

QInputDialog, QFileDialog {
    background-color: #161B22;
}

/* ═══════════════════════════════════════════════════════════════════
   LABELS — Typography hierarchy
   ═══════════════════════════════════════════════════════════════════ */

QLabel {
    color: #C9D1D9;
    font-size: 12px;
    background: transparent;
}

QLabel#heading {
    font-size: 20px;
    font-weight: 700;
    color: #E6EDF3;
}

QLabel#subheading {
    font-size: 14px;
    font-weight: 600;
    color: #C9D1D9;
}

QLabel#muted {
    font-size: 11px;
    color: #8B949E;
}

QLabel#accent {
    color: #58A6FF;
    font-weight: 600;
}

QLabel#success {
    color: #3FB950;
    font-weight: 600;
}

QLabel#danger {
    color: #F85149;
    font-weight: 600;
}

/* ═══════════════════════════════════════════════════════════════════
   TOOLTIPS — Floating cards
   ═══════════════════════════════════════════════════════════════════ */

QToolTip {
    background-color: #1C2333;
    color: #E6EDF3;
    border: 1px solid #30363D;
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 12px;
}

/* ═══════════════════════════════════════════════════════════════════
   TABS — Modern underline style
   ═══════════════════════════════════════════════════════════════════ */

QTabWidget::pane {
    border: 1px solid #21262D;
    border-radius: 8px;
    background: #0D1117;
    padding: 8px;
}

QTabBar::tab {
    background: transparent;
    color: #8B949E;
    padding: 10px 20px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
    font-weight: 600;
}

QTabBar::tab:selected {
    color: #E6EDF3;
    border-bottom-color: #58A6FF;
}

QTabBar::tab:hover:!selected {
    color: #C9D1D9;
    background: rgba(88, 166, 255, 0.06);
    border-bottom-color: #30363D;
}

/* ═══════════════════════════════════════════════════════════════════
   STATUS BAR
   ═══════════════════════════════════════════════════════════════════ */

QStatusBar {
    background-color: #0D1117;
    border-top: 1px solid #21262D;
    color: #8B949E;
    font-size: 11px;
    padding: 4px 12px;
}

QStatusBar QLabel {
    font-size: 11px;
    color: #8B949E;
    padding: 0 8px;
}

/* ═══════════════════════════════════════════════════════════════════
   SPLITTER
   ═══════════════════════════════════════════════════════════════════ */

QSplitter::handle {
    background-color: #21262D;
}

QSplitter::handle:horizontal {
    width: 1px;
}

QSplitter::handle:vertical {
    height: 1px;
}

QSplitter::handle:hover {
    background-color: #58A6FF;
}

/* ═══════════════════════════════════════════════════════════════════
   FRAME CARDS — Glassmorphism panels
   ═══════════════════════════════════════════════════════════════════ */

QFrame#card {
    background-color: rgba(22, 27, 34, 0.8);
    border: 1px solid #21262D;
    border-radius: 12px;
    padding: 16px;
}

QFrame#card:hover {
    border-color: #30363D;
}

QFrame#separator {
    background: #21262D;
    max-height: 1px;
    min-height: 1px;
}

/* ═══════════════════════════════════════════════════════════════════
   SYSTEM TRAY
   ═══════════════════════════════════════════════════════════════════ */

QSystemTrayIcon QMenu {
    background-color: #161B22;
    color: #C9D1D9;
    border: 1px solid #30363D;
    border-radius: 10px;
}
"""


def apply_dark_theme(app: QApplication) -> None:
    """Apply the premium dark theme stylesheet to a QApplication instance."""
    app.setStyleSheet(DARK_THEME)
