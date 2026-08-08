"""
High-Density Audio Metric Card Widget
Renders KPI metrics with warm amber monospace typography and thin 3px left border accents.
"""

from typing import Optional
from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel

class MetricCard(QFrame):
    def __init__(
        self,
        title: str,
        value: str = "--",
        subtitle: Optional[str] = None,
        accent_color: str = "#f59e0b",
        parent=None
    ) -> None:
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self.lbl_title = QLabel(title.upper())
        self.lbl_title.setStyleSheet("color: #828a9a; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8pt; font-weight: 700;")
        layout.addWidget(self.lbl_title)

        self.lbl_value = QLabel(value)
        self.lbl_value.setStyleSheet("color: #f59e0b; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 14pt; font-weight: 700;")
        layout.addWidget(self.lbl_value)

        self.lbl_subtitle = QLabel(subtitle or "")
        self.lbl_subtitle.setStyleSheet("color: #94a3b8; font-size: 8pt;")
        if not subtitle:
            self.lbl_subtitle.hide()
        layout.addWidget(self.lbl_subtitle)

    def set_value(self, val: str, subtitle: Optional[str] = None) -> None:
        self.lbl_value.setText(str(val))
        if subtitle is not None:
            self.lbl_subtitle.setText(subtitle)
            self.lbl_subtitle.show()