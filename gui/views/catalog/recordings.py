"""
Recording Catalog Explorer View
Renders paginated recording tables with quality-based row tinting, format/quality filters, 
and high-density multi-card inspector panels. Refactored to import centralized UI components.
"""

from typing import List, Dict, Any, Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, 
    QLineEdit, QPushButton, QComboBox, QSplitter, QTableWidget, 
    QTableWidgetItem, QHeaderView, QAbstractItemView, QScrollArea
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor

from db import RecordingRepo, get_connection, db_transaction
from domain.models import Recording
from domain.events import signals, IngestionFinishedEvent
from gui.widgets.badges import QualityBadge, LockStateBadge
from gui.widgets.pagination import PaginationBar

# Centralized imports replacing locally duplicated widgets and formatters
from gui.widgets.common import format_duration, KeyValueRow, InspectorCard

class RecordingsView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rec_repo = RecordingRepo()
        self.current_recordings: List[Recording] = []
        self.selected_rec: Optional[Recording] = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # Filter Toolbar
        filter_bar = QFrame()
        filter_bar.setStyleSheet("background-color: #16161a; border-radius: 4px; border: 1px solid #222228;")
        filter_layout = QHBoxLayout(filter_bar)
        filter_layout.setContentsMargins(10, 6, 10, 6)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter Title, Artist, Album, or ISRC...")
        self.search_input.textChanged.connect(self._on_search_changed)

        self.combo_format = QComboBox()
        self.combo_format.addItems(["[ALL FORMATS]", "FLAC", "MP3", "WAV", "OGG", "M4A"])
        self.combo_format.currentIndexChanged.connect(self.reload_catalog)

        self.combo_quality = QComboBox()
        self.combo_quality.addItems(["[ALL QUALITY]", "100% STUDIO COMPLETE", "< 100% INCOMPLETE ONLY"])
        self.combo_quality.currentIndexChanged.connect(self.reload_catalog)

        filter_layout.addWidget(self.search_input, stretch=3)
        filter_layout.addWidget(self.combo_format, stretch=1)
        filter_layout.addWidget(self.combo_quality, stretch=1)
        main_layout.addWidget(filter_bar)

        # Main Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)

        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.rec_table = QTableWidget()
        self.rec_table.setColumnCount(8)
        self.rec_table.setHorizontalHeaderLabels(["ID", "Track Title", "Artist", "Album", "Genre", "Subgenre", "Format", "Quality"])
        self.rec_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rec_table.setColumnWidth(0, 85)
        self.rec_table.setColumnWidth(2, 140)
        self.rec_table.setColumnWidth(3, 140)
        self.rec_table.setColumnWidth(4, 110)
        self.rec_table.setColumnWidth(5, 110)
        self.rec_table.setColumnWidth(6, 75)
        self.rec_table.setColumnWidth(7, 75)
        self.rec_table.verticalHeader().hide()
        self.rec_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rec_table.itemSelectionChanged.connect(self._on_row_selected)
        self.rec_table.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)

        table_layout.addWidget(self.rec_table)

        self.pagination = PaginationBar(page_size=100)
        self.pagination.page_changed.connect(self._render_page)
        table_layout.addWidget(self.pagination)

        splitter.addWidget(table_container)

        # Right Inspector Panel
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        inspector_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(8, 0, 4, 0)
        inspector_layout.setSpacing(10)

        self.lbl_rec_title = QLabel("SELECT A RECORDING ON THE LEFT")
        self.lbl_rec_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 10.5pt; font-weight: bold; color: #f59e0b;")
        inspector_layout.addWidget(self.lbl_rec_title)

        card_meta = InspectorCard("Core Metadata Attributes")
        self.row_artist = KeyValueRow("Artist")
        self.row_album = KeyValueRow("Album")
        self.row_genre = KeyValueRow("Primary Genre")
        self.row_subgenre = KeyValueRow("Primary Subgenre")
        self.row_release_date = KeyValueRow("Release Date")
        self.row_orig_date = KeyValueRow("Original Date")
        self.row_quality = KeyValueRow("Quality Index")
        card_meta.add_row(self.row_artist)
        card_meta.add_row(self.row_album)
        card_meta.add_row(self.row_genre)
        card_meta.add_row(self.row_subgenre)
        card_meta.add_row(self.row_release_date)
        card_meta.add_row(self.row_orig_date)
        card_meta.add_row(self.row_quality)
        inspector_layout.addWidget(card_meta)

        card_specs = InspectorCard("Audio File Technical Specs")
        self.row_duration = KeyValueRow("Duration")
        self.row_format = KeyValueRow("Codec Format")
        self.row_bitrate = KeyValueRow("Bitrate")
        self.row_sample_rate = KeyValueRow("Sample Rate")
        self.row_filepath = KeyValueRow("File Path")
        card_specs.add_row(self.row_duration)
        card_specs.add_row(self.row_format)
        card_specs.add_row(self.row_bitrate)
        card_specs.add_row(self.row_sample_rate)
        card_specs.add_row(self.row_filepath)
        inspector_layout.addWidget(card_specs)

        card_ids = InspectorCard("Authority Identifiers")
        self.row_isrc = KeyValueRow("ISRC Code")
        self.row_mbid = KeyValueRow("MusicBrainz ID")
        self.row_acoustid = KeyValueRow("AcoustID")
        card_ids.add_row(self.row_isrc)
        card_ids.add_row(self.row_mbid)
        card_ids.add_row(self.row_acoustid)
        inspector_layout.addWidget(card_ids)

        card_locks = InspectorCard("Field Lock Controls")
        self.badge_title_lock = LockStateBadge("AUTOMATIC")
        self.badge_title_lock.state_clicked.connect(lambda st: self._update_lock("title", st))
        self.badge_artist_lock = LockStateBadge("AUTOMATIC")
        self.badge_artist_lock.state_clicked.connect(lambda st: self._update_lock("artist", st))
        
        lock_row1 = QHBoxLayout()
        lock_row1.addWidget(QLabel("Title Lock:"))
        lock_row1.addWidget(self.badge_title_lock)
        lock_row2 = QHBoxLayout()
        lock_row2.addWidget(QLabel("Artist Lock:"))
        lock_row2.addWidget(self.badge_artist_lock)

        card_locks.card_layout.addLayout(lock_row1)
        card_locks.card_layout.addLayout(lock_row2)
        inspector_layout.addWidget(card_locks)

        inspector_layout.addStretch()
        inspector_scroll.setWidget(inspector_panel)
        splitter.addWidget(inspector_scroll)

        splitter.setSizes([850, 420])
        main_layout.addWidget(splitter, stretch=1)

        signals.ingestion_finished.connect(self._on_ingestion_finished)

        self.reload_catalog()

    def _on_search_changed(self) -> None:
        self.reload_catalog()

    def reload_catalog(self) -> None:
        search_q = self.search_input.text().strip()
        fmt_q = self.combo_format.currentText()
        q_q = self.combo_quality.currentIndex()

        filters = {}
        if search_q:
            filters["search"] = search_q

        recordings = self.rec_repo.get_overview_catalog(filters)

        if fmt_q != "[ALL FORMATS]":
            recordings = [r for r in recordings if r.format.upper() == fmt_q.upper()]

        if q_q == 1:
            recordings = [r for r in recordings if r.quality_score >= 1.0]
        elif q_q == 2:
            recordings = [r for r in recordings if r.quality_score < 1.0]

        self.current_recordings = recordings
        self.pagination.set_total_items(len(recordings))
        self._render_page(self.pagination.current_page)

    def _render_page(self, page: int) -> None:
        page_size = self.pagination.page_size
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = self.current_recordings[start_idx:end_idx]

        self.rec_table.setRowCount(len(page_items))
        for r_idx, rec in enumerate(page_items):
            items = [
                QTableWidgetItem(rec.id[:8] + "..."),
                QTableWidgetItem(rec.title),
                QTableWidgetItem(rec.artist_name),
                QTableWidgetItem(rec.album_title),
                QTableWidgetItem(rec.primary_genre),
                QTableWidgetItem(rec.primary_subgenre),
                QTableWidgetItem(rec.format),
                QTableWidgetItem(f"{rec.quality_score * 100:.0f}%")
            ]

            if rec.quality_score >= 1.0:
                bg_col = QColor("#0a2318")
                fg_q = QColor("#10b981")
            elif rec.quality_score >= 0.80:
                bg_col = QColor("#0b1f2e")
                fg_q = QColor("#38bdf8")
            elif rec.quality_score >= 0.50:
                bg_col = QColor("#221503")
                fg_q = QColor("#f59e0b")
            else:
                bg_col = QColor("#280b0b")
                fg_q = QColor("#ef4444")

            items[7].setForeground(fg_q)

            for col_idx, item in enumerate(items):
                item.setBackground(bg_col)
                self.rec_table.setItem(r_idx, col_idx, item)

    def _on_row_selected(self) -> None:
        selected_rows = self.rec_table.selectedItems()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        page_size = self.pagination.page_size
        actual_idx = ((self.pagination.current_page - 1) * page_size) + row

        if actual_idx < len(self.current_recordings):
            rec = self.current_recordings[actual_idx]
            self.selected_rec = rec
            
            self.lbl_rec_title.setText(f"ID #{rec.id[:8]}... — {rec.title or 'Untitled Work'}")
            self.row_artist.set_value(rec.artist_name or "Unknown Artist")
            self.row_album.set_value(rec.album_title or "Unknown Album")
            self.row_genre.set_value(rec.primary_genre or "Unclassified")
            self.row_subgenre.set_value(rec.primary_subgenre or "Unclassified")
            self.row_release_date.set_value(rec.release_date or "N/A")
            self.row_orig_date.set_value(rec.original_release_date or "N/A")
            self.row_quality.set_value(f"{rec.quality_score * 100:.0f}%", "#10b981" if rec.quality_score >= 1.0 else "#f59e0b")

            self.row_duration.set_value(format_duration(rec.duration))
            self.row_format.set_value(rec.format or "FLAC")
            
            bitrate_str = f"{rec.bitrate // 1000} kbps" if rec.bitrate and rec.bitrate > 1000 else (f"{rec.bitrate} kbps" if rec.bitrate else "N/A")
            self.row_bitrate.set_value(bitrate_str)
            self.row_sample_rate.set_value(f"{float(rec.sample_rate or 0)/1000.0:.1f} kHz" if rec.sample_rate else "N/A")
            self.row_filepath.set_value(rec.filepath or "N/A")

            self.row_isrc.set_value(rec.isrc or "N/A", "#10b981" if rec.isrc else "#ef4444")
            self.row_mbid.set_value(rec.musicbrainz_recording_id or "N/A", "#38bdf8" if rec.musicbrainz_recording_id else "#ef4444")
            self.row_acoustid.set_value(rec.acoustid_id or "N/A", "#f59e0b" if rec.acoustid_id else "#ef4444")

            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT field_name, lock_state FROM meta_locks WHERE entity_id = %s", (rec.id,))
                lock_map = {r[0]: r[1] for r in cursor.fetchall()}
                self.badge_title_lock.set_state(lock_map.get("title", "AUTOMATIC"))
                self.badge_artist_lock.set_state(lock_map.get("artist", "AUTOMATIC"))
            except Exception:
                pass

    def _update_lock(self, field_name: str, lock_state: str) -> None:
        if not self.selected_rec:
            return
        with db_transaction() as tx:
            tx.execute("""
                INSERT INTO meta_locks (entity_id, field_name, lock_state)
                VALUES (%s, %s, %s)
                ON CONFLICT(entity_id, field_name) DO UPDATE SET lock_state = EXCLUDED.lock_state
            """, (self.selected_rec.id, field_name, lock_state))

    @Slot(IngestionFinishedEvent)
    def _on_ingestion_finished(self, event: IngestionFinishedEvent) -> None:
        self.reload_catalog()