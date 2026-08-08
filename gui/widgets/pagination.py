"""
Catalog Pagination Controller Widget
Standardized page controller for 100-item batch navigation across catalog views.
"""

from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel
from PySide6.QtCore import Signal, Qt

class PaginationBar(QWidget):
    page_changed = Signal(int)

    def __init__(self, page_size: int = 100, parent=None) -> None:
        super().__init__(parent)
        self.page_size = page_size
        self.current_page = 1
        self.total_pages = 1
        self.total_items = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)

        self.btn_first = QPushButton("« First")
        self.btn_prev = QPushButton("‹ Prev")
        self.lbl_info = QLabel("Page 1 of 1 (0 items)")
        self.btn_next = QPushButton("Next ›")
        self.btn_last = QPushButton("Last »")

        self.lbl_info.setStyleSheet("font-family: 'Consolas', monospace; color: #828a9a; font-size: 8.5pt;")

        for btn in (self.btn_first, self.btn_prev, self.btn_next, self.btn_last):
            btn.setFixedWidth(65)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #16161a;
                    color: #e2e8f0;
                    border: 1px solid #2a2a32;
                    font-family: 'Consolas', monospace;
                    font-size: 8pt;
                    padding: 3px 6px;
                }
                QPushButton:hover { border-color: #f59e0b; color: #f59e0b; }
            """)

        self.btn_first.clicked.connect(lambda: self.go_to_page(1))
        self.btn_prev.clicked.connect(lambda: self.go_to_page(self.current_page - 1))
        self.btn_next.clicked.connect(lambda: self.go_to_page(self.current_page + 1))
        self.btn_last.clicked.connect(lambda: self.go_to_page(self.total_pages))

        layout.addWidget(self.btn_first)
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.lbl_info, stretch=1, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.btn_next)
        layout.addWidget(self.btn_last)

        self.update_controls()

    def set_total_items(self, total: int) -> None:
        self.total_items = max(0, total)
        self.total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages
        self.update_controls()

    def go_to_page(self, page: int) -> None:
        target = max(1, min(self.total_pages, page))
        if target != self.current_page:
            self.current_page = target
            self.update_controls()
            self.page_changed.emit(self.current_page)

    def update_controls(self) -> None:
        self.lbl_info.setText(f"Page {self.current_page} of {self.total_pages} ({self.total_items:,} items)")
        self.btn_first.setEnabled(self.current_page > 1)
        self.btn_prev.setEnabled(self.current_page > 1)
        self.btn_next.setEnabled(self.current_page < self.total_pages)
        self.btn_last.setEnabled(self.current_page < self.total_pages)