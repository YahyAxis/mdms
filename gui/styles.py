"""
Master Dark Amber "Audio Engineer's Workbench" QSS Stylesheet
Defines the visual theme, monospace technical typography, and rack-panel aesthetic.
"""

MASTER_DARK_AMBER_QSS = """
/* ============================================================================
   GLOBAL BASE & TYPOGRAPHY
   ============================================================================ */
QMainWindow, QDialog {
    background-color: #101012;
    color: #e2e8f0;
}

QWidget {
    background-color: #101012;
    color: #e2e8f0;
    font-family: 'Segoe UI', 'Inter', -apple-system, sans-serif;
    font-size: 9.5pt;
}

/* ============================================================================
   SIDEBAR ("TAPE REEL LABELS")
   ============================================================================ */
QListWidget#TapeReelSidebar {
    background-color: #16161a;
    border: none;
    border-right: 1px solid #222226;
    outline: none;
    font-size: 10pt;
    font-weight: 600;
    padding-top: 6px;
}

QListWidget#TapeReelSidebar::item {
    height: 42px;
    padding-left: 16px;
    color: #828a9a;
    border-left: 3px solid transparent;
}

QListWidget#TapeReelSidebar::item:hover {
    background-color: #1a1a1f;
    color: #cbd5e1;
}

QListWidget#TapeReelSidebar::item:selected {
    background-color: #1c1c22;
    color: #f59e0b;
    border-left: 3px solid #f59e0b;
}

/* ============================================================================
   BUTTONS & INPUTS
   ============================================================================ */
QPushButton {
    background-color: #1c1c22;
    color: #e2e8f0;
    border: 1px solid #2a2a32;
    border-radius: 3px;
    padding: 6px 14px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #24242c;
    border-color: #f59e0b;
    color: #ffffff;
}

QPushButton:pressed {
    background-color: #16161a;
    border-color: #d97706;
}

QPushButton:disabled {
    background-color: #141416;
    color: #475569;
    border-color: #1c1c22;
}

QPushButton#AmberPrimaryBtn {
    background-color: #f59e0b;
    color: #101012;
    border: none;
    font-weight: 700;
}

QPushButton#AmberPrimaryBtn:hover {
    background-color: #fbbf24;
}

QPushButton#AmberPrimaryBtn:pressed {
    background-color: #d97706;
}

QLineEdit, QComboBox, QSpinBox {
    background-color: #16161a;
    color: #e2e8f0;
    border: 1px solid #2a2a32;
    border-radius: 3px;
    padding: 5px 8px;
    font-family: 'Consolas', 'JetBrains Mono', monospace;
}

QLineEdit:focus, QComboBox:focus {
    border-color: #f59e0b;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

/* ============================================================================
   TABLES & HEADERS
   ============================================================================ */
QTableView, QTableWidget {
    background-color: #141418;
    gridline-color: #222228;
    color: #e2e8f0;
    border: 1px solid #222228;
    selection-background-color: #22222c;
    selection-color: #f59e0b;
    outline: none;
}

QHeaderView::section {
    background-color: #16161a;
    color: #828a9a;
    padding: 6px;
    font-family: 'Consolas', 'JetBrains Mono', monospace;
    font-size: 8.5pt;
    font-weight: 700;
    text-transform: uppercase;
    border: none;
    border-bottom: 1px solid #2a2a32;
    border-right: 1px solid #1a1a20;
}

/* ============================================================================
   FRAMES, PANELS & CARDS
   ============================================================================ */
QFrame#WorkbenchCard {
    background-color: #16161a;
    border: 1px solid #222228;
    border-radius: 4px;
}

QFrame#WorkbenchCardAccent {
    background-color: #16161a;
    border: 1px solid #222228;
    border-left: 3px solid #f59e0b;
    border-radius: 4px;
}

QTabWidget::pane {
    border: 1px solid #222228;
    background-color: #141418;
}

QTabBar::tab {
    background-color: #16161a;
    color: #828a9a;
    padding: 8px 16px;
    border: 1px solid #222228;
    border-bottom: none;
    font-weight: 600;
}

QTabBar::tab:selected {
    background-color: #141418;
    color: #f59e0b;
    border-top: 2px solid #f59e0b;
}

/* ============================================================================
   PROGRESS BARS & SCROLLBARS
   ============================================================================ */
QProgressBar {
    background-color: #1a1a20;
    border: 1px solid #222228;
    border-radius: 2px;
    text-align: center;
    color: #e2e8f0;
    font-family: 'Consolas', monospace;
    font-size: 8.5pt;
    font-weight: 700;
}

QProgressBar::chunk {
    background-color: #f59e0b;
}

QScrollBar:vertical {
    background-color: #101012;
    width: 10px;
    margin: 0px;
}

QScrollBar::handle:vertical {
    background-color: #26262e;
    min-height: 20px;
    border-radius: 2px;
}

QScrollBar::handle:vertical:hover {
    background-color: #f59e0b;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""