"""
Modular Operations Workspace Package
Assembles Pipeline Control Rack and Metadata Repair Center into a tabbed workspace.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget
from gui.views.ops.pipeline import PipelineView
from gui.views.ops.repair import RepairView

class OperationsWorkspace(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.pipeline_view = PipelineView()
        self.repair_view = RepairView()

        self.tabs.addTab(self.pipeline_view, "[1] PIPELINE CONTROL RACK")
        self.tabs.addTab(self.repair_view, "[2] METADATA REPAIR CENTER")

# Alias for backward compatibility
OperationsView = OperationsWorkspace

__all__ = [
    "PipelineView",
    "RepairView",
    "OperationsWorkspace",
    "OperationsView",
]