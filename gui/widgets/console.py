"""
Memory-Bounded RingBuffer Console Widget
Provides high-performance, color-highlighted log output bounded to 2,000 blocks.
"""

from typing import Optional
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit
from PySide6.QtGui import QTextCursor, QTextCharFormat, QColor, QFont
from domain.events import LogEvent

class RingBufferConsole(QWidget):
    def __init__(self, max_blocks: int = 2000, parent=None) -> None:
        super().__init__(parent)
        self.max_blocks = max_blocks

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMaximumBlockCount(self.max_blocks)
        self.editor.setFont(QFont("Consolas", 8))
        self.editor.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0c0c0e;
                color: #e2e8f0;
                border: 1px solid #222228;
                selection-background-color: #22222c;
                selection-color: #f59e0b;
            }
        """)

        layout.addWidget(self.editor)

    def append_log(self, event_or_msg: LogEvent | str, level: Optional[str] = None) -> None:
        if isinstance(event_or_msg, LogEvent):
            msg = event_or_msg.message
            lvl = event_or_msg.level
        else:
            msg = str(event_or_msg)
            lvl = level or "INFO"

        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        if lvl == "SUCCESS" or "[+]" in msg:
            fmt.setForeground(QColor("#10b981"))  # Signal Green
        elif lvl == "WARNING" or "[-]" in msg:
            fmt.setForeground(QColor("#f59e0b"))  # Warm Amber
        elif lvl == "ERROR" or "[!]" in msg:
            fmt.setForeground(QColor("#ef4444"))  # Studio Red
        else:
            fmt.setForeground(QColor("#cbd5e1"))  # Light Muted Slate

        cursor.insertText(f"{msg}\n", fmt)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def clear_console(self) -> None:
        self.editor.clear()