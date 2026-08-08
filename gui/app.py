"""
Audio Engineer's Workbench Main Shell
Features Tape-Reel Sidebar navigation, header status bar, collapsible bottom log drawer, 
and 6 workspace stack routing. Updated to initialize and lifecycle-manage the background crawler daemon.
"""

import sys
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget, 
    QStackedWidget, QLabel, QFrame, QPushButton, QSplitter
)
from PySide6.QtCore import Qt, QTimer, Slot

from config.settings import settings
from domain.events import signals, LogEvent
from gui.styles import MASTER_DARK_AMBER_QSS
from gui.widgets.console import RingBufferConsole

# Import the background crawler daemon
from services.crawl import BackgroundCrawlerDaemon

# Import All 6 Production Workspace Views (No Taxonomy)
from gui.views.analytics import AnalyticsWorkspace
from gui.views.catalog import CatalogWorkspace
from gui.views.ops import OperationsWorkspace
from gui.views.discover import DiscoveryWorkspace
from gui.views.research import ResearchWorkspace
from gui.views.system import SystemHealthWorkspace

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{settings.APP_NAME} v{settings.APP_VERSION} [Audio Workbench]")
        self.resize(1380, 850)
        self.setStyleSheet(MASTER_DARK_AMBER_QSS)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Header Bar
        header = QFrame()
        header.setStyleSheet("background-color: #16161a; border-bottom: 1px solid #222228;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 8)

        lbl_brand = QLabel(f"<b>MDMS WORKSTATION</b> <span style='color:#828a9a;'>v{settings.APP_VERSION}</span>")
        lbl_brand.setStyleSheet("font-size: 11pt; color: #f59e0b; font-family: 'Consolas', monospace;")
        header_layout.addWidget(lbl_brand)

        header_layout.addStretch()

        self.lbl_status_pill = QLabel("SYSTEM: ONLINE")
        self.lbl_status_pill.setStyleSheet("""
            background-color: #064e3b; color: #10b981; font-family: 'Consolas', monospace; 
            font-size: 8pt; font-weight: bold; padding: 2px 8px; border-radius: 2px; border: 1px solid #10b981;
        """)
        header_layout.addWidget(self.lbl_status_pill)

        main_layout.addWidget(header)

        # Main Workspace Splitter
        main_splitter = QSplitter(Qt.Orientation.Vertical)

        top_container = QWidget()
        workspace_layout = QHBoxLayout(top_container)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)

        # Tape-Reel Sidebar (Compacted list of 6 flat-ontology workspace destinations)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("TapeReelSidebar")
        self.sidebar.setFixedWidth(220)
        self.sidebar.addItems([
            "ANALYTICS CENTER",
            "CATALOG BROWSER",
            "OPERATIONS WORKBENCH",
            "DISCOVERY CRATE",
            "RESEARCH STUDIO",
            "SYSTEM HEALTH"
        ])
        self.sidebar.currentRowChanged.connect(self._switch_workspace)

        # Stack Initialization
        self.workspace_stack = QStackedWidget()

        # Instantiate 6 Active Workspaces
        self.analytics_view = AnalyticsWorkspace()
        self.catalog_view = CatalogWorkspace()
        self.ops_view = OperationsWorkspace()
        self.discover_view = DiscoveryWorkspace()
        self.research_view = ResearchWorkspace()
        self.system_view = SystemHealthWorkspace()

        self.workspace_stack.addWidget(self.analytics_view)  # Index 0
        self.workspace_stack.addWidget(self.catalog_view)    # Index 1
        self.workspace_stack.addWidget(self.ops_view)        # Index 2
        self.workspace_stack.addWidget(self.discover_view)   # Index 3
        self.workspace_stack.addWidget(self.research_view)   # Index 4
        self.workspace_stack.addWidget(self.system_view)     # Index 5

        workspace_layout.addWidget(self.sidebar)
        workspace_layout.addWidget(self.workspace_stack, stretch=1)

        main_splitter.addWidget(top_container)

        # Collapsible Bottom Console Drawer
        self.bottom_drawer = QFrame()
        self.bottom_drawer.setStyleSheet("background-color: #0c0c0e; border-top: 1px solid #222228;")
        drawer_layout = QVBoxLayout(self.bottom_drawer)
        drawer_layout.setContentsMargins(8, 4, 8, 4)

        drawer_header = QHBoxLayout()
        lbl_drawer_title = QLabel("LIVE SYSTEM EVENT STREAM")
        lbl_drawer_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a; font-weight: bold;")
        drawer_header.addWidget(lbl_drawer_title)
        drawer_header.addStretch()

        self.btn_toggle_drawer = QPushButton("▼ Collapse")
        self.btn_toggle_drawer.setFixedHeight(22)
        self.btn_toggle_drawer.setStyleSheet("font-size: 7.5pt; padding: 1px 8px; font-family: 'Consolas', monospace;")
        self.btn_toggle_drawer.clicked.connect(self._toggle_drawer)
        drawer_header.addWidget(self.btn_toggle_drawer)

        drawer_layout.addLayout(drawer_header)

        self.global_console = RingBufferConsole(max_blocks=2000)
        drawer_layout.addWidget(self.global_console)

        main_splitter.addWidget(self.bottom_drawer)
        main_splitter.setSizes([680, 170])

        main_layout.addWidget(main_splitter, stretch=1)

        signals.log_emitted.connect(self._on_log_emitted)
        QTimer.singleShot(10, lambda: self.sidebar.setCurrentRow(0))

        # Initialize and start the background crawler daemon thread
        self.crawler_daemon = BackgroundCrawlerDaemon()
        self.crawler_daemon.start()

    def _switch_workspace(self, index: int) -> None:
        if 0 <= index < self.workspace_stack.count():
            self.workspace_stack.setCurrentIndex(index)

    def _toggle_drawer(self) -> None:
        if self.global_console.isVisible():
            self.global_console.hide()
            self.btn_toggle_drawer.setText("▲ Expand Log")
        else:
            self.global_console.show()
            self.btn_toggle_drawer.setText("▼ Collapse")

    @Slot(LogEvent)
    def _on_log_emitted(self, event: LogEvent) -> None:
        self.global_console.append_log(event)

    def closeEvent(self, event) -> None:
        """Safely stops background threads when closing the application."""
        if hasattr(self, "crawler_daemon") and self.crawler_daemon:
            self.crawler_daemon.stop()
        super().closeEvent(event)