# ==========================================================================================
# FILE: gui/widgets/common.py
# ==========================================================================================
"""
Common UI Widgets and Helper Functions
Centralizes duplicated layout cards, scroll areas, duration formatters, and key-value widgets.
"""

from typing import Optional, Any, Tuple, List
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QFrame, QScrollArea
from PySide6.QtCore import Qt

def format_duration(sec: Optional[float]) -> str:
    """Consistently formats duration from seconds to MM:SS or H:MM:SS."""
    if not sec or sec <= 0:
        return "--:--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def create_tab_scroll_area() -> Tuple[QScrollArea, QVBoxLayout]:
    """Generates a standardized scrollable tab container with proper spacing."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(4, 12, 4, 12)
    layout.setSpacing(12)
    scroll.setWidget(content)
    return scroll, layout

class KeyValueRow(QWidget):
    """Sleek horizontal line widget displaying a key-value label pair."""
    def __init__(self, key: str, default_val: str = "N/A", key_width: int = 130, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)

        self.key_lbl = QLabel(f"{key.upper()}:")
        self.key_lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a; font-weight: bold;")
        self.key_lbl.setFixedWidth(key_width)

        self.val_lbl = QLabel(default_val)
        self.val_lbl.setStyleSheet("font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt; color: #e2e8f0; font-weight: bold;")
        self.val_lbl.setWordWrap(True)

        layout.addWidget(self.key_lbl)
        layout.addWidget(self.val_lbl, stretch=1)

    def set_value(self, value: Any, color_override: Optional[str] = None) -> None:
        val_str = str(value) if value is not None and str(value).strip() != "" else "N/A"
        self.val_lbl.setText(val_str)
        if color_override:
            self.val_lbl.setStyleSheet(f"font-family: 'Consolas', monospace; font-size: 8.5pt; color: {color_override}; font-weight: bold;")
        else:
            self.val_lbl.setStyleSheet("font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt; color: #e2e8f0; font-weight: bold;")

class MasterWorkbenchCard(QFrame):
    """MASTER UI Container with a warm amber left border and professional typography."""
    def __init__(self, title: str, accent_color: str = "#f59e0b", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WorkbenchCardAccent")
        self.setStyleSheet(f"""
            QFrame#WorkbenchCardAccent {{
                background-color: #16161a;
                border: 1px solid #222228;
                border-left: 3px solid {accent_color};
                border-radius: 4px;
            }}
        """)
        self.card_layout = QVBoxLayout(self)
        self.card_layout.setContentsMargins(12, 10, 12, 10)
        self.card_layout.setSpacing(6)

        self.lbl_title = QLabel(title.upper())
        self.lbl_title.setStyleSheet("font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt; font-weight: bold; color: #828a9a; border-bottom: 1px solid #222228; padding-bottom: 4px;")
        self.card_layout.addWidget(self.lbl_title)
        self._row_widgets: List[QWidget] = []

    def add_row(self, row_widget: QWidget) -> None:
        self._row_widgets.append(row_widget)
        self.card_layout.addWidget(row_widget)

    def clear_rows(self) -> None:
        for w in self._row_widgets:
            try:
                w.deleteLater()
            except Exception:
                pass
        self._row_widgets.clear()

# Backward compatible legacy aliases to avoid code modifications in views
class InspectorCard(MasterWorkbenchCard):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, "#f59e0b", parent)

class AnalyticsCard(MasterWorkbenchCard):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, "#f59e0b", parent)

class AuditCard(MasterWorkbenchCard):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, "#f59e0b", parent)

class ResearchCard(MasterWorkbenchCard):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, "#f59e0b", parent)

class SystemCard(MasterWorkbenchCard):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(title, "#f59e0b", parent)