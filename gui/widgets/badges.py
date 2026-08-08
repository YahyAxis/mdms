"""
Monospace Status Badges
Provides technical badges for Quality Scores (0-100%), Lock States (AUTO/PROT/MANU), and Issue Severity.
"""

from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal

class QualityBadge(QWidget):
    def __init__(self, score: float = 0.0, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl)
        
        self.set_score(score)

    def set_score(self, score: float) -> None:
        pct = int(score * 100) if score <= 1.0 else int(score)
        pct = max(0, min(100, pct))
        
        if pct >= 90:
            bg, fg = "#064e3b", "#10b981"  # Emerald Green
        elif pct >= 70:
            bg, fg = "#451a03", "#f59e0b"  # Warm Amber
        else:
            bg, fg = "#450a0a", "#ef4444"  # Studio Red

        self.lbl.setText(f"{pct}%")
        self.lbl.setStyleSheet(f"""
            QLabel {{
                background-color: {bg};
                color: {fg};
                font-family: 'Consolas', 'JetBrains Mono', monospace;
                font-size: 8.5pt;
                font-weight: 700;
                padding: 2px 8px;
                border-radius: 2px;
                border: 1px solid {fg};
            }}
        """)

class LockStateBadge(QWidget):
    state_clicked = Signal(str)

    def __init__(self, state: str = "AUTOMATIC", is_interactive: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.state = state.upper()
        self.is_interactive = is_interactive

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self.is_interactive:
            self.lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.lbl)

        self.set_state(self.state)

    def set_state(self, state: str) -> None:
        self.state = state.upper()
        if "MANU" in self.state:
            text = "[MANUAL]"
            bg, fg = "#450a0a", "#ef4444"
        elif "PROT" in self.state:
            text = "[PROTECTED]"
            bg, fg = "#451a03", "#f59e0b"
        else:
            text = "[AUTOMATIC]"
            bg, fg = "#082f49", "#38bdf8"

        self.lbl.setText(text)
        self.lbl.setStyleSheet(f"""
            QLabel {{
                background-color: {bg};
                color: {fg};
                font-family: 'Consolas', 'JetBrains Mono', monospace;
                font-size: 8pt;
                font-weight: 700;
                padding: 2px 6px;
                border-radius: 2px;
                border: 1px solid {fg};
            }}
        """)

    def mousePressEvent(self, event) -> None:
        if self.is_interactive:
            next_state = "PROTECTED" if self.state == "AUTOMATIC" else ("MANUAL" if self.state == "PROTECTED" else "AUTOMATIC")
            self.set_state(next_state)
            self.state_clicked.emit(next_state)
        super().mousePressEvent(event)

class SeverityBadge(QWidget):
    def __init__(self, severity: str = "INFO", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl)

        self.set_severity(severity)

    def set_severity(self, severity: str) -> None:
        sev = severity.upper()
        if sev in ("CRITICAL", "ERROR"):
            bg, fg = "#450a0a", "#ef4444"
        elif sev == "WARNING":
            bg, fg = "#451a03", "#f59e0b"
        else:
            bg, fg = "#1e293b", "#94a3b8"

        self.lbl.setText(sev)
        self.lbl.setStyleSheet(f"""
            QLabel {{
                background-color: {bg};
                color: {fg};
                font-family: 'Consolas', 'JetBrains Mono', monospace;
                font-size: 8pt;
                font-weight: 700;
                padding: 2px 6px;
                border-radius: 2px;
            }}
        """)