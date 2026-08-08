"""
Artist Directory & Demographic Profile View
Provides searchable artist listings, demographic profiles,
and discography & attributed track inspector tables.
"""

from typing import List, Dict, Any, Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QListWidget, QListWidgetItem, QTableWidget, 
    QTableWidgetItem, QHeaderView, QSplitter, QAbstractItemView, QScrollArea
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor

from db import get_connection
from domain.events import signals, IngestionFinishedEvent
from utils.geo import get_region_for_country

# Centralized imports replacing duplicated KeyValueRow and Card layout classes
from gui.widgets.common import KeyValueRow, InspectorCard

class ArtistsView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.artists_data: List[Dict[str, Any]] = []

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        search_row = QHBoxLayout()
        lbl_s = QLabel("SEARCH ARTIST DIRECTORY:")
        lbl_s.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a; font-weight: bold;")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search artist name, country, or region...")
        self.search_input.textChanged.connect(self._filter_artist_list)
        search_row.addWidget(lbl_s)
        search_row.addWidget(self.search_input, stretch=1)
        main_layout.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.artist_list = QListWidget()
        self.artist_list.setStyleSheet("""
            QListWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 9pt;
            }
            QListWidget::item { padding: 8px 12px; border-bottom: 1px solid #1a1a20; }
            QListWidget::item:selected { background-color: #1c1c22; color: #f59e0b; border-left: 3px solid #f59e0b; }
        """)
        self.artist_list.currentRowChanged.connect(self._on_artist_selected)
        splitter.addWidget(self.artist_list)

        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        inspector_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(8, 0, 4, 0)
        inspector_layout.setSpacing(10)

        self.lbl_artist_title = QLabel("SELECT AN ARTIST ON THE LEFT")
        self.lbl_artist_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 10.5pt; font-weight: bold; color: #f59e0b;")
        inspector_layout.addWidget(self.lbl_artist_title)

        card_demo = InspectorCard("Demographic Profile & Origins")
        self.row_country = KeyValueRow("Country Code")
        self.row_region = KeyValueRow("Mapped Region")
        self.row_formed = KeyValueRow("Formed / Inception")
        self.row_type = KeyValueRow("Artist Type")
        self.row_gender = KeyValueRow("Gender")
        self.row_mbid = KeyValueRow("MusicBrainz Artist ID")
        card_demo.add_row(self.row_country)
        card_demo.add_row(self.row_region)
        card_demo.add_row(self.row_formed)
        card_demo.add_row(self.row_type)
        card_demo.add_row(self.row_gender)
        card_demo.add_row(self.row_mbid)
        inspector_layout.addWidget(card_demo)

        card_disco = InspectorCard("Album Discography")
        self.table_disco = QTableWidget()
        self.table_disco.setColumnCount(3)
        self.table_disco.setHorizontalHeaderLabels(["Album Title", "Release Date", "Track Count"])
        self.table_disco.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table_disco.setColumnWidth(1, 120)
        self.table_disco.setColumnWidth(2, 95)
        self.table_disco.verticalHeader().hide()
        self.table_disco.setFixedHeight(160)
        self.table_disco.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_disco.add_row(self.table_disco)
        inspector_layout.addWidget(card_disco)

        card_tracks = InspectorCard("Attributed Catalog Tracks")
        self.table_tracks = QTableWidget()
        self.table_tracks.setColumnCount(4)
        self.table_tracks.setHorizontalHeaderLabels(["Track Title", "Album Title", "Primary Genre", "Quality"])
        self.table_tracks.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table_tracks.setColumnWidth(1, 140)
        self.table_tracks.setColumnWidth(2, 110)
        self.table_tracks.setColumnWidth(3, 75)
        self.table_tracks.verticalHeader().hide()
        self.table_tracks.setMinimumHeight(180)
        self.table_tracks.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_tracks.add_row(self.table_tracks)
        inspector_layout.addWidget(card_tracks)

        inspector_scroll.setWidget(inspector_panel)
        splitter.addWidget(inspector_scroll)

        splitter.setSizes([350, 920])
        main_layout.addWidget(splitter, stretch=1)

        signals.ingestion_finished.connect(self._on_ingestion_finished)

        self.reload_artists()

    def reload_artists(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT a.id, a.name, a.country, a.formed_year, a.artist_type, a.gender, a.musicbrainz_artist_id, COUNT(r.id) AS trk_cnt
                FROM core_artists a
                LEFT JOIN core_recordings r ON a.id = r.artist_id
                GROUP BY a.id, a.name, a.country, a.formed_year, a.artist_type, a.gender, a.musicbrainz_artist_id
                ORDER BY trk_cnt DESC, a.name ASC
            """)
            rows = cursor.fetchall()
            self.artists_data = [{
                "id": r[0], "name": r[1] or "Unknown Artist", "country": r[2], 
                "formed_year": r[3], "artist_type": r[4], "gender": r[5], 
                "mbid": r[6], "track_count": r[7]
            } for r in rows]

            self._filter_artist_list()
        except Exception:
            pass

    def _filter_artist_list(self) -> None:
        filter_text = self.search_input.text().strip().lower()
        self.artist_list.clear()

        for art in self.artists_data:
            if filter_text and filter_text not in art["name"].lower() and filter_text not in str(art["country"]).lower():
                continue
            item_text = f"{art['name']}  [{art['track_count']:,} trks]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, art)
            self.artist_list.addItem(item)

        if self.artist_list.count() > 0:
            self.artist_list.setCurrentRow(0)

    def _on_artist_selected(self, row: int) -> None:
        if row < 0 or row >= self.artist_list.count():
            return
        item = self.artist_list.item(row)
        art = item.data(Qt.ItemDataRole.UserRole)
        if not art:
            return

        self.lbl_artist_title.setText(f"ARTIST // {art['name'].upper()}")
        self.row_country.set_value(str(art["country"] or "N/A").upper())
        self.row_region.set_value(get_region_for_country(art["country"]), "#38bdf8")
        self.row_formed.set_value(str(art["formed_year"]) if art["formed_year"] else "N/A")
        self.row_type.set_value(str(art["artist_type"] or "N/A"))
        self.row_gender.set_value(str(art["gender"] or "N/A"))
        self.row_mbid.set_value(str(art["mbid"] or "N/A"), "#38bdf8" if art["mbid"] else "#ef4444")

        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT alb.title, COALESCE(alb.original_release_date, alb.release_date), COUNT(r.id)
                FROM core_albums alb
                LEFT JOIN core_recordings r ON alb.id = r.album_id
                WHERE alb.artist_id = %s
                GROUP BY alb.id, alb.title, alb.original_release_date, alb.release_date ORDER BY alb.original_release_date ASC
            """, (art["id"],))
            alb_rows = cursor.fetchall()

            self.table_disco.setRowCount(len(alb_rows))
            for r_idx, (a_title, a_date, a_cnt) in enumerate(alb_rows):
                self.table_disco.setItem(r_idx, 0, QTableWidgetItem(str(a_title or "Unknown Album")))
                self.table_disco.setItem(r_idx, 1, QTableWidgetItem(str(a_date or "N/A")))
                self.table_disco.setItem(r_idx, 2, QTableWidgetItem(f"{a_cnt:,}"))

            cursor.execute("""
                SELECT r.title, COALESCE(alb.title, 'Unknown Album'), 
                       COALESCE(r.genre, 'Unclassified'),
                       COALESCE(v.quality_score, 0.0)
                FROM core_recordings r
                LEFT JOIN core_albums alb ON r.album_id = alb.id
                LEFT JOIN meta_validation v ON r.id = v.recording_id
                WHERE r.artist_id = %s ORDER BY r.title ASC LIMIT 100
            """, (art["id"],))
            trk_rows = cursor.fetchall()

            self.table_tracks.setRowCount(len(trk_rows))
            for r_idx, (t_title, alb_title, genre_str, q_val) in enumerate(trk_rows):
                item_q = QTableWidgetItem(f"{float(q_val or 0.0) * 100:.0f}%")
                if float(q_val or 0.0) >= 1.0:
                    item_q.setForeground(QColor("#10b981"))
                elif float(q_val or 0.0) >= 0.8:
                    item_q.setForeground(QColor("#38bdf8"))
                else:
                    item_q.setForeground(QColor("#f59e0b"))

                self.table_tracks.setItem(r_idx, 0, QTableWidgetItem(str(t_title or "Untitled")))
                self.table_tracks.setItem(r_idx, 1, QTableWidgetItem(str(alb_title or "Unknown Album")))
                self.table_tracks.setItem(r_idx, 2, QTableWidgetItem(str(genre_str or "Unclassified")))
                self.table_tracks.setItem(r_idx, 3, item_q)

        except Exception:
            pass

    @Slot(IngestionFinishedEvent)
    def _on_ingestion_finished(self, event: IngestionFinishedEvent) -> None:
        self.reload_artists()