"""
Album Catalog Inspector View
Provides searchable album directory, specification inspector,
complete tracklist table, and bi-directional navigation signals.
"""

from typing import List, Dict, Any, Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QListWidget, QListWidgetItem, QTableWidget, 
    QTableWidgetItem, QHeaderView, QSplitter, QAbstractItemView, 
    QScrollArea, QPushButton
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor

from db.core import get_connection
from domain.events import signals, IngestionFinishedEvent

# Centralized imports replacing duplicated panel structures and time formatters
from gui.widgets.common import format_duration, KeyValueRow, InspectorCard

class AlbumsView(QWidget):
    view_album_tracks_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.albums_data: List[Dict[str, Any]] = []

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # Header Filter
        search_row = QHBoxLayout()
        lbl_s = QLabel("SEARCH ALBUM CATALOG:")
        lbl_s.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a; font-weight: bold;")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search album title or artist name...")
        self.search_input.textChanged.connect(self._filter_album_list)
        search_row.addWidget(lbl_s)
        search_row.addWidget(self.search_input, stretch=1)
        main_layout.addLayout(search_row)

        # Splitter: Left List | Right Details
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.album_list = QListWidget()
        self.album_list.setStyleSheet("""
            QListWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 9pt;
            }
            QListWidget::item { padding: 8px 12px; border-bottom: 1px solid #1a1a20; }
            QListWidget::item:selected { background-color: #1c1c22; color: #f59e0b; border-left: 3px solid #f59e0b; }
        """)
        self.album_list.currentRowChanged.connect(self._on_album_selected)
        splitter.addWidget(self.album_list)

        # Right Inspector Panel
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        inspector_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(8, 0, 4, 0)
        inspector_layout.setSpacing(10)

        header_title_row = QHBoxLayout()
        self.lbl_album_title = QLabel("SELECT AN ALBUM ON THE LEFT")
        self.lbl_album_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 10.5pt; font-weight: bold; color: #f59e0b;")
        header_title_row.addWidget(self.lbl_album_title)
        header_title_row.addStretch()

        self.btn_jump_tracks = QPushButton("[VIEW ALBUM TRACKS IN RECORDINGS EXPLORER]")
        self.btn_jump_tracks.setStyleSheet("""
            QPushButton {
                background-color: #1c1c22; color: #f59e0b; border: 1px solid #f59e0b;
                font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold; padding: 4px 10px;
            }
            QPushButton:hover { background-color: #f59e0b; color: #101012; }
        """)
        self.btn_jump_tracks.clicked.connect(self._on_jump_tracks_clicked)
        header_title_row.addWidget(self.btn_jump_tracks)

        inspector_layout.addLayout(header_title_row)

        card_specs = InspectorCard("Release Specifications & Identifiers")
        self.row_artist = KeyValueRow("Primary Artist", key_width=140)
        self.row_type = KeyValueRow("Album Type", key_width=140)
        self.row_rel_date = KeyValueRow("Issue Release Date", key_width=140)
        self.row_orig_date = KeyValueRow("Original Release Date", key_width=140)
        self.row_catno = KeyValueRow("Catalog Number", key_width=140)
        self.row_barcode = KeyValueRow("Barcode", key_width=140)
        self.row_runtime = KeyValueRow("Total Album Runtime", key_width=140)
        self.row_rg_mbid = KeyValueRow("Release Group MBID", key_width=140)
        card_specs.add_row(self.row_artist)
        card_specs.add_row(self.row_type)
        card_specs.add_row(self.row_rel_date)
        card_specs.add_row(self.row_orig_date)
        card_specs.add_row(self.row_catno)
        card_specs.add_row(self.row_barcode)
        card_specs.add_row(self.row_runtime)
        card_specs.add_row(self.row_rg_mbid)
        inspector_layout.addWidget(card_specs)

        # Complete Tracklist Table
        card_tracklist = InspectorCard("Complete Album Tracklist")
        self.table_tracklist = QTableWidget()
        self.table_tracklist.setColumnCount(6)
        self.table_tracklist.setHorizontalHeaderLabels(["Trk #", "Track Title", "Duration", "Format", "Bitrate", "Quality"])
        self.table_tracklist.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_tracklist.setColumnWidth(0, 55)
        self.table_tracklist.setColumnWidth(2, 75)
        self.table_tracklist.setColumnWidth(3, 70)
        self.table_tracklist.setColumnWidth(4, 90)
        self.table_tracklist.setColumnWidth(5, 75)
        self.table_tracklist.verticalHeader().hide()
        self.table_tracklist.setMinimumHeight(240)
        self.table_tracklist.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_tracklist.add_row(self.table_tracklist)
        inspector_layout.addWidget(card_tracklist)

        inspector_scroll.setWidget(inspector_panel)
        splitter.addWidget(inspector_scroll)

        splitter.setSizes([380, 890])
        main_layout.addWidget(splitter, stretch=1)

        signals.ingestion_finished.connect(self._on_ingestion_finished)

        self.reload_albums()

    def reload_albums(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT alb.id, alb.title, COALESCE(a.name, 'Unknown Artist'), 
                       alb.album_type, alb.release_date, alb.original_release_date, 
                       alb.catalog_number, alb.barcode, alb.release_group_mbid, 
                       COUNT(r.id) AS trk_cnt, COALESCE(SUM(f.duration), 0.0) AS tot_dur
                FROM core_albums alb
                LEFT JOIN core_artists a ON alb.artist_id = a.id
                LEFT JOIN core_recordings r ON alb.id = r.album_id
                LEFT JOIN core_assets f ON r.id = f.recording_id
                GROUP BY alb.id, alb.title, a.name, alb.album_type, alb.release_date, 
                         alb.original_release_date, alb.catalog_number, alb.barcode, alb.release_group_mbid
                ORDER BY alb.title ASC
            """)
            rows = cursor.fetchall()
            self.albums_data = [{
                "id": r[0], "title": r[1] or "Unknown Album", "artist": r[2], 
                "album_type": r[3], "release_date": r[4], "original_release_date": r[5], 
                "catalog_number": r[6], "barcode": r[7], "release_group_mbid": r[8], 
                "track_count": r[9], "total_duration": r[10]
            } for r in rows]

            self._filter_album_list()
        except Exception:
            pass

    def _filter_album_list(self) -> None:
        filter_text = self.search_input.text().strip().lower()
        self.album_list.clear()

        for alb in self.albums_data:
            if filter_text and filter_text not in alb["title"].lower() and filter_text not in alb["artist"].lower():
                continue
            item_text = f"{alb['title']} — {alb['artist']}  [{alb['track_count']:,} trks]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, alb)
            self.album_list.addItem(item)

        if self.album_list.count() > 0:
            self.album_list.setCurrentRow(0)

    def _on_album_selected(self, row: int) -> None:
        if row < 0 or row >= self.album_list.count():
            return
        item = self.album_list.item(row)
        alb = item.data(Qt.ItemDataRole.UserRole)
        if not alb:
            return

        self.lbl_album_title.setText(f"ALBUM // {alb['title'].upper()}")
        self.row_artist.set_value(alb["artist"] or "Unknown Artist")
        self.row_type.set_value(alb["album_type"] or "Album")
        self.row_rel_date.set_value(alb["release_date"] or "N/A")
        self.row_orig_date.set_value(alb["original_release_date"] or "N/A", "#10b981" if alb["original_release_date"] else "#f59e0b")
        self.row_catno.set_value(alb["catalog_number"] or "N/A")
        self.row_barcode.set_value(alb["barcode"] or "N/A")
        self.row_runtime.set_value(format_duration(alb["total_duration"]))
        self.row_rg_mbid.set_value(alb["release_group_mbid"] or "N/A", "#38bdf8" if alb["release_group_mbid"] else "#ef4444")

        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT r.track_number, r.title, f.duration, f.format, f.bitrate, COALESCE(v.quality_score, 0.0)
                FROM core_recordings r
                LEFT JOIN core_assets f ON r.id = f.recording_id
                LEFT JOIN meta_validation v ON r.id = v.recording_id
                WHERE r.album_id = %s
                ORDER BY r.disc_number ASC, COALESCE(r.track_number, 999) ASC, r.title ASC
            """, (alb["id"],))
            trk_rows = cursor.fetchall()

            self.table_tracklist.setRowCount(len(trk_rows))
            for r_idx, (t_no, t_title, dur, fmt, bit, q_val) in enumerate(trk_rows):
                bit_val = int(bit) if bit else 0
                bit_str = f"{bit_val // 1000} kbps" if bit_val > 1000 else (f"{bit_val} kbps" if bit_val > 0 else "N/A")
                
                q_num = float(q_val or 0.0)
                item_q = QTableWidgetItem(f"{q_num * 100:.0f}%")
                if q_num >= 1.0:
                    item_q.setForeground(QColor("#10b981"))
                elif q_num >= 0.8:
                    item_q.setForeground(QColor("#38bdf8"))
                else:
                    item_q.setForeground(QColor("#f59e0b"))

                self.table_tracklist.setItem(r_idx, 0, QTableWidgetItem(str(t_no) if t_no else "-"))
                self.table_tracklist.setItem(r_idx, 1, QTableWidgetItem(str(t_title or "Untitled")))
                self.table_tracklist.setItem(r_idx, 2, QTableWidgetItem(format_duration(dur)))
                self.table_tracklist.setItem(r_idx, 3, QTableWidgetItem(str(fmt or "FLAC")))
                self.table_tracklist.setItem(r_idx, 4, QTableWidgetItem(bit_str))
                self.table_tracklist.setItem(r_idx, 5, item_q)

        except Exception:
            pass

    def _on_jump_tracks_clicked(self) -> None:
        if self.album_list.currentRow() >= 0:
            item = self.album_list.currentItem()
            alb = item.data(Qt.ItemDataRole.UserRole) if item else None
            if alb and alb.get("title"):
                self.view_album_tracks_requested.emit(alb["title"])

    @Slot(IngestionFinishedEvent)
    def _on_ingestion_finished(self, event: IngestionFinishedEvent) -> None:
        self.reload_albums()