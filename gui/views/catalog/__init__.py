"""
Modular Catalog Workspace Package
Assembles Recordings Explorer, Artist Directory, and Album Catalog sub-views into a tabbed workspace.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget
from gui.views.catalog.recordings import RecordingsView
from gui.views.catalog.artists import ArtistsView
from gui.views.catalog.albums import AlbumsView

class CatalogWorkspace(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.recordings_view = RecordingsView()
        self.artists_view = ArtistsView()
        self.albums_view = AlbumsView()

        self.tabs.addTab(self.recordings_view, "[1] RECORDINGS EXPLORER")
        self.tabs.addTab(self.artists_view, "[2] ARTIST DIRECTORY")
        self.tabs.addTab(self.albums_view, "[3] ALBUM CATALOG")

        # Bi-directional navigation: AlbumsView -> RecordingsView filter jump
        self.albums_view.view_album_tracks_requested.connect(self._on_view_album_tracks)

    def _on_view_album_tracks(self, album_title: str) -> None:
        self.tabs.setCurrentIndex(0)
        self.recordings_view.search_input.setText(album_title)

__all__ = [
    "RecordingsView",
    "ArtistsView",
    "AlbumsView",
    "CatalogWorkspace",
]