"""
Audio Fidelity & Consensus Label Analytics Center
Provides high-density metrics, codec matrices, resolved tag profiles,
geographic artist profiles, decades timelines, and an export manager.
Loads all database metrics asynchronously in background threads to ensure 100% GUI responsiveness.
"""

import os
import json
import csv
import threading
from typing import List, Tuple, Dict, Any, Optional
from collections import defaultdict
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, 
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, 
    QHeaderView, QScrollArea, QAbstractItemView, QComboBox, QTabWidget, 
    QLineEdit, QPlainTextEdit
)
from PySide6.QtCore import Qt, Slot, Signal
from PySide6.QtGui import QColor, QFont

from config.settings import settings
from db.core import get_connection, close_thread_connection
from domain.events import event_bus, LogEvent
from services.tax import TaxonomyService
from utils.geo import get_region_for_country
from gui.widgets.metric import MetricCard

class AnalyticsBarRow(QWidget):
    def __init__(self, label: str, count: int, total: int, color: str = "#f59e0b", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        pct = int((count / total) * 100) if total > 0 else 0

        lbl_name = QLabel(label)
        lbl_name.setFixedWidth(180)
        lbl_name.setToolTip(f"{label}: {count:,} / {total:,} ({pct}%)")
        lbl_name.setStyleSheet("font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt; color: #cbd5e1;")

        bar = QProgressBar()
        bar.setFixedHeight(12)
        bar.setValue(pct)
        bar.setTextVisible(False)
        bar.setToolTip(f"{count:,} of {total:,} items ({pct}%)")
        bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #121216;
                border: 1px solid #222228;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 1px;
            }}
        """)

        lbl_val = QLabel(f"{count:,} ({pct}%)")
        lbl_val.setFixedWidth(95)
        lbl_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl_val.setStyleSheet(f"font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt; color: {color}; font-weight: bold;")

        layout.addWidget(lbl_name)
        layout.addWidget(bar, stretch=1)
        layout.addWidget(lbl_val)

class AnalyticsCard(QFrame):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WorkbenchCardAccent")
        self.setStyleSheet("""
            QFrame#WorkbenchCardAccent {
                background-color: #16161a;
                border: 1px solid #222228;
                border-left: 3px solid #f59e0b;
                border-radius: 4px;
            }
        """)

        self.card_layout = QVBoxLayout(self)
        self.card_layout.setContentsMargins(12, 10, 12, 10)
        self.card_layout.setSpacing(6)

        lbl_title = QLabel(title.upper())
        lbl_title.setStyleSheet("font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt; font-weight: bold; color: #828a9a; border-bottom: 1px solid #222228; padding-bottom: 4px;")
        self.card_layout.addWidget(lbl_title)
        
        self._row_widgets: List[QWidget] = []

    def add_row(self, row_widget: QWidget) -> None:
        self._row_widgets.append(row_widget)
        self.card_layout.addWidget(row_widget)

    def clear_rows(self) -> None:
        for w in self._row_widgets:
            w.deleteLater()
        self._row_widgets.clear()

class AnalyticsWorkspace(QWidget):
    # Signal to securely post loaded statistics back to main thread
    analytics_loaded = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Title Header Bar
        header_row = QHBoxLayout()
        header = QLabel("ANALYTICS // LIBRARY AUDIO FIDELITY & DATA EXPORTER")
        header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 11pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(header)
        header_row.addStretch()

        self.combo_scope = QComboBox()
        self.combo_scope.addItems(["SCOPE: FULL LIBRARY", "SCOPE: INCOMPLETE ONLY (<1.0)"])
        self.combo_scope.setFixedWidth(210)
        self.combo_scope.currentIndexChanged.connect(self.reload_analytics)
        header_row.addWidget(self.combo_scope)

        self.btn_refresh = QPushButton("[REFRESH ANALYTICS]")
        self.btn_refresh.setFixedWidth(160)
        self.btn_refresh.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        self.btn_refresh.clicked.connect(self.reload_analytics)
        header_row.addWidget(self.btn_refresh)

        main_layout.addLayout(header_row)

        # Global KPI Header Cards Row
        kpi_row = QHBoxLayout()
        self.card_tracks = MetricCard("TOTAL TRACKS", "--", "Active catalog items")
        self.card_playtime = MetricCard("TOTAL PLAYTIME", "--", "Cumulative duration")
        self.card_storage = MetricCard("STORAGE FOOTPRINT", "--", "Physical asset size")
        self.card_lossless = MetricCard("LOSSLESS RATIO", "--", "FLAC / WAV formats", "#10b981")
        self.card_quality = MetricCard("AVG QUALITY SCORE", "--", "Meta-validation index")

        kpi_row.addWidget(self.card_tracks)
        kpi_row.addWidget(self.card_playtime)
        kpi_row.addWidget(self.card_storage)
        kpi_row.addWidget(self.card_lossless)
        kpi_row.addWidget(self.card_quality)
        main_layout.addLayout(kpi_row)

        # Analytics Tab Container
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=1)

        self._build_tab_overview()
        self._build_tab_taxonomy()
        self._build_tab_audio_dsp()
        self._build_tab_demographics()
        self._build_tab_data_vault()
        self._build_tab_export()

        # Connect the asynchronous load handler
        self.analytics_loaded.connect(self._on_analytics_loaded)

        self.reload_analytics()

    def _create_tab_scroll_area(self) -> Tuple[QWidget, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 12, 4, 12)
        layout.setSpacing(12)
        scroll.setWidget(content)
        return scroll, layout

    # =========================================================================
    # TAB 1: OVERVIEW & METRICS
    # =========================================================================
    def _build_tab_overview(self) -> None:
        scroll, layout = self._create_tab_scroll_area()

        grid_row1 = QHBoxLayout()
        self.card_fidelity = AnalyticsCard("Audio Codec & Format Distribution")
        self.card_q_density = AnalyticsCard("Quality Score Frequency Distribution")
        grid_row1.addWidget(self.card_fidelity)
        grid_row1.addWidget(self.card_q_density)
        layout.addLayout(grid_row1)

        grid_row2 = QHBoxLayout()
        self.card_audio_specs = AnalyticsCard("Sample Rate & Fidelity Spectrum")
        self.card_authority_cov = AnalyticsCard("Authority Identifier Coverage Rates")
        grid_row2.addWidget(self.card_audio_specs)
        grid_row2.addWidget(self.card_authority_cov)
        layout.addLayout(grid_row2)

        grid_row3 = QHBoxLayout()
        self.card_lifecycle = AnalyticsCard("Catalog Lifecycle Entity State Matrix")
        self.card_completeness_deficit = AnalyticsCard("Metadata Field Completeness Deficit Ratios")
        grid_row3.addWidget(self.card_lifecycle)
        grid_row3.addWidget(self.card_completeness_deficit)
        layout.addLayout(grid_row3)

        self.tabs.addTab(scroll, "[1] OVERVIEW & METRICS")

    # =========================================================================
    # TAB 2: FLAT GENRE DISTRIBUTION
    # =========================================================================
    def _build_tab_taxonomy(self) -> None:
        scroll, layout = self._create_tab_scroll_area()

        grid_row1 = QHBoxLayout()
        self.card_genres = AnalyticsCard("Primary Resolved Genre Distribution")
        self.card_subgenres = AnalyticsCard("Top Style Subgenre Distribution")
        grid_row1.addWidget(self.card_genres)
        grid_row1.addWidget(self.card_subgenres)
        layout.addLayout(grid_row1)

        grid_row2 = QHBoxLayout()
        self.card_tax_depth = AnalyticsCard("Label Resolution Summary")
        self.card_evidence_sources = AnalyticsCard("Evidence Provenance Source Breakdown")
        grid_row2.addWidget(self.card_tax_depth)
        grid_row2.addWidget(self.card_evidence_sources)
        layout.addLayout(grid_row2)

        # Raw Tag Frequencies Card (Dynamic evidence query instead of dynamic database table)
        self.card_unmapped_digest = AnalyticsCard("Raw Genre Tag Frequencies in Evidence")
        
        unmapped_header = QHBoxLayout()
        lbl_info = QLabel("MOST FREQUENTLY OCCURRING RAW TAGS IN COLLECTED EVIDENCE:")
        lbl_info.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        unmapped_header.addWidget(lbl_info)
        unmapped_header.addStretch()

        self.btn_auto_expand = QPushButton("[AUTO-EXPANSION ARCHIVED]")
        self.btn_auto_expand.setFixedHeight(24)
        self.btn_auto_expand.setEnabled(False)
        self.btn_auto_expand.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #94a3b8; font-family: 'Consolas', monospace; 
                font-size: 8pt; font-weight: bold; border: 1px solid #334155; padding: 2px 12px; border-radius: 2px;
            }
        """)
        self.btn_auto_expand.clicked.connect(self._on_auto_expand_clicked)
        unmapped_header.addWidget(self.btn_auto_expand)
        self.card_unmapped_digest.card_layout.addLayout(unmapped_header)

        layout.addWidget(self.card_unmapped_digest)

        self.tabs.addTab(scroll, "[2] GENRE CHANNELS")

    # =========================================================================
    # TAB 3: AUDIO SPECTRUM & DSP
    # =========================================================================
    def _build_tab_audio_dsp(self) -> None:
        scroll, layout = self._create_tab_scroll_area()

        grid_row1 = QHBoxLayout()
        self.card_bitrates = AnalyticsCard("Bitrate Density Distribution Bins")
        self.card_channels = AnalyticsCard("Audio Channel Configuration Matrix")
        grid_row1.addWidget(self.card_bitrates)
        grid_row1.addWidget(self.card_channels)
        layout.addLayout(grid_row1)

        grid_row2 = QHBoxLayout()
        self.card_sample_rates = AnalyticsCard("Detailed Sample Rate Distribution")
        self.card_sonic_cov = AnalyticsCard("Sonic Features & DSP Audio Analysis Coverage")
        grid_row2.addWidget(self.card_sample_rates)
        grid_row2.addWidget(self.card_sonic_cov)
        layout.addLayout(grid_row2)

        self.tabs.addTab(scroll, "[3] AUDIO SPECTRUM & DSP")

    # =========================================================================
    # TAB 4: DEMOGRAPHICS & ERA
    # =========================================================================
    def _build_tab_demographics(self) -> None:
        scroll, layout = self._create_tab_scroll_area()

        grid_row1 = QHBoxLayout()
        self.card_regions = AnalyticsCard("Geographic Artist Regional Profile")
        self.card_countries = AnalyticsCard("Top Artist Country Origins (Top 12)")
        grid_row1.addWidget(self.card_regions)
        grid_row1.addWidget(self.card_countries)
        layout.addLayout(grid_row1)

        grid_row2 = QHBoxLayout()
        self.card_decades = AnalyticsCard("Original Release Decades Timeline")
        self.card_artist_types = AnalyticsCard("Artist Type & Gender Profile")
        grid_row2.addWidget(self.card_decades)
        grid_row2.addWidget(self.card_artist_types)
        layout.addLayout(grid_row2)

        self.tabs.addTab(scroll, "[4] DEMOGRAPHICS & ERA")

    # =========================================================================
    # TAB 5: GATHERED DATA VAULT
    # =========================================================================
    def _build_tab_data_vault(self) -> None:
        scroll, layout = self._create_tab_scroll_area()

        vault_kpi = QHBoxLayout()
        self.card_total_facts = MetricCard("EVIDENCE FACTS", "--", "meta_evidence rows", "#10b981")
        self.card_total_payloads = MetricCard("API BLOB PAYLOADS", "--", "sys_payloads archives", "#38bdf8")
        self.card_total_decisions = MetricCard("DECISIONS LOGGED", "--", "meta_decisions history", "#f59e0b")
        self.card_total_issues = MetricCard("OPEN ISSUES", "--", "meta_issues queue", "#ef4444")

        vault_kpi.addWidget(self.card_total_facts)
        vault_kpi.addWidget(self.card_total_payloads)
        vault_kpi.addWidget(self.card_total_decisions)
        vault_kpi.addWidget(self.card_total_issues)
        layout.addLayout(vault_kpi)

        grid_row = QHBoxLayout()
        self.card_fact_sources = AnalyticsCard("Evidence Provenance Facts by Provider")
        grid_row.addWidget(self.card_fact_sources)
        layout.addLayout(grid_row)

        self.card_fact_explorer = AnalyticsCard("Searchable Gathered Fact Vault (Live meta_evidence Inspector)")
        
        search_fact_row = QHBoxLayout()
        lbl_fact_s = QLabel("SEARCH GATHERED FACTS:")
        lbl_fact_s.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a; font-weight: bold;")
        self.search_fact_input = QLineEdit()
        self.search_fact_input.setPlaceholderText("Search fact value, field name, source ID, or entity ID...")
        self.search_fact_input.textChanged.connect(self._reload_fact_explorer)
        search_fact_row.addWidget(lbl_fact_s)
        search_fact_row.addWidget(self.search_fact_input, stretch=1)
        self.card_fact_explorer.card_layout.addLayout(search_fact_row)

        self.table_fact_explorer = QTableWidget()
        self.table_fact_explorer.setColumnCount(6)
        self.table_fact_explorer.setHorizontalHeaderLabels(["Entity ID", "Field Name", "Fact / Value String", "Class", "Source Provider", "Confidence"])
        self.table_fact_explorer.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table_fact_explorer.setColumnWidth(0, 100)
        self.table_fact_explorer.setColumnWidth(1, 110)
        self.table_fact_explorer.setColumnWidth(3, 85)
        self.table_fact_explorer.setColumnWidth(4, 150)
        self.table_fact_explorer.setColumnWidth(5, 90)
        self.table_fact_explorer.verticalHeader().hide()
        self.table_fact_explorer.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_fact_explorer.setMinimumHeight(280)
        self.table_fact_explorer.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        self.card_fact_explorer.add_row(self.table_fact_explorer)
        layout.addWidget(self.card_fact_explorer)

        self.tabs.addTab(scroll, "[5] GATHERED DATA VAULT")

    # =========================================================================
    # TAB 6: DATA EXPORT & REPORTS
    # =========================================================================
    def _build_tab_export(self) -> None:
        scroll, layout = self._create_tab_scroll_area()

        card_export_controls = AnalyticsCard("Catalog & Taxonomy Data Exporter Rack")
        
        btn_layout = QHBoxLayout()
        btn_csv = QPushButton("[EXPORT CATALOG CSV]")
        btn_json = QPushButton("[EXPORT GENRE ASSOCIATIONS JSON]")
        btn_txt = QPushButton("[EXPORT RAW EVIDENCE TAGS TXT]")
        btn_md = QPushButton("[GENERATE MARKDOWN REPORT]")

        for btn in (btn_csv, btn_json, btn_txt, btn_md):
            btn.setFixedHeight(32)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #1c1c22; color: #e2e8f0; border: 1px solid #2a2a32;
                    font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold; padding: 4px 10px;
                }
                QPushButton:hover { background-color: #24242c; border-color: #f59e0b; color: #f59e0b; }
            """)
            btn_layout.addWidget(btn)

        card_export_controls.card_layout.addLayout(btn_layout)
        layout.addWidget(card_export_controls)

        btn_csv.clicked.connect(self._export_catalog_csv)
        btn_json.clicked.connect(self._export_taxonomy_json)
        btn_txt.clicked.connect(self._export_unmapped_txt)
        btn_md.clicked.connect(self._generate_markdown_report)

        card_preview = AnalyticsCard("Exported Data Live Text Console")
        self.preview_export_text = QPlainTextEdit()
        self.preview_export_text.setReadOnly(True)
        self.preview_export_text.setMinimumHeight(320)
        self.preview_export_text.setFont(QFont("Consolas", 8.5))
        self.preview_export_text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0c0c0e; color: #f59e0b; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace;
            }
        """)
        card_preview.add_row(self.preview_export_text)
        layout.addWidget(card_preview)

        self.tabs.addTab(scroll, "[6] DATA EXPORT & REPORTS")

    # =========================================================================
    # ASYNCHRONOUS DATA RELOAD WORKER
    # =========================================================================
    def reload_analytics(self) -> None:
        """Launches the background thread worker to compute database statistics asynchronously."""
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("[LOADING DATA...]")
        
        # Display Loading placeholders in GUI indicators
        for card in (self.card_tracks, self.card_playtime, self.card_storage, self.card_lossless, self.card_quality):
            card.set_value("Loading...")

        is_incomplete_only = (self.combo_scope.currentIndex() == 1)

        def worker():
            try:
                conn = get_connection()
                cursor = conn.cursor()

                rec_where = "WHERE v.quality_score < 1.0" if is_incomplete_only else ""
                rec_join_val = "LEFT JOIN meta_validation v ON r.id = v.recording_id "
                full_rec_join = (rec_join_val + rec_where) if is_incomplete_only else ""

                # 1. Total Tracks
                if is_incomplete_only:
                    cursor.execute("SELECT COUNT(*) FROM core_recordings r LEFT JOIN meta_validation v ON r.id = v.recording_id WHERE v.quality_score < 1.0")
                else:
                    cursor.execute("SELECT COUNT(*) FROM core_recordings")
                total_tracks = cursor.fetchone()[0] or 0

                # 2. Lossless counts
                cursor.execute("""
                    SELECT COUNT(*), COALESCE(SUM(a.file_size), 0), COALESCE(SUM(a.duration), 0)
                    FROM core_assets a 
                    JOIN core_recordings r ON a.recording_id = r.id
                    """ + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "UPPER(a.format) IN ('FLAC', 'WAV')")
                lossless_row = cursor.fetchone()
                lossless_tracks = lossless_row[0] or 0

                # 3. Overall storage & playtime
                cursor.execute("SELECT COALESCE(SUM(a.file_size), 0), COALESCE(SUM(a.duration), 0) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join)
                tot_row = cursor.fetchone()
                total_bytes = tot_row[0] or 0
                total_seconds = float(tot_row[1] or 0.0)

                # 4. Averages
                cursor.execute("SELECT AVG(v.quality_score) FROM meta_validation v JOIN core_recordings r ON v.recording_id = r.id " + (rec_where if is_incomplete_only else ""))
                avg_q_row = cursor.fetchone()
                avg_q = float(avg_q_row[0]) if avg_q_row and avg_q_row[0] else 0.0

                # 5. Format Distribution
                cursor.execute("SELECT UPPER(a.format), COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + " GROUP BY UPPER(a.format) ORDER BY COUNT(*) DESC")
                format_counts = cursor.fetchall()

                # 6. Quality Score distribution
                cursor.execute("SELECT COUNT(*) FROM meta_validation v JOIN core_recordings r ON v.recording_id = r.id WHERE v.quality_score >= 1.0 " + ("AND v.quality_score < 1.0" if is_incomplete_only else ""))
                q100 = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM meta_validation v JOIN core_recordings r ON v.recording_id = r.id WHERE v.quality_score >= 0.80 AND v.quality_score < 1.0")
                q80 = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM meta_validation v JOIN core_recordings r ON v.recording_id = r.id WHERE v.quality_score >= 0.50 AND v.quality_score < 0.80")
                q50 = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM meta_validation v JOIN core_recordings r ON v.recording_id = r.id WHERE v.quality_score < 0.50")
                q0 = cursor.fetchone()[0] or 0

                # 7. Sample rates
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "a.sample_rate >= 88200")
                hires_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "a.sample_rate BETWEEN 44100 AND 48000 AND UPPER(a.format) IN ('FLAC', 'WAV')")
                cd_lossless_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(a.bitrate >= 280000 OR (a.bitrate >= 280 AND a.bitrate <= 1000)) AND UPPER(a.format) NOT IN ('FLAC', 'WAV')")
                high_lossy_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "((a.bitrate < 280000 AND a.bitrate < 280) OR a.bitrate IS NULL) AND UPPER(a.format) NOT IN ('FLAC', 'WAV')")
                low_lossy_cnt = cursor.fetchone()[0] or 0

                # 8. Authority coverage
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "r.isrc IS NOT NULL AND r.isrc != '' AND r.isrc != 'NONE'")
                isrc_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "r.musicbrainz_recording_id IS NOT NULL AND r.musicbrainz_recording_id != ''")
                mbid_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "r.acoustid_id IS NOT NULL AND r.acoustid_id != ''")
                acoustid_cnt = cursor.fetchone()[0] or 0

                # 9. State matrix
                cursor.execute("SELECT state, COUNT(*) FROM core_recordings r " + full_rec_join + " GROUP BY state")
                state_rows = cursor.fetchall()

                # 10. Deficits
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(r.isrc IS NULL OR r.isrc = '' OR r.isrc = 'NONE')")
                no_isrc = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(r.musicbrainz_recording_id IS NULL OR r.musicbrainz_recording_id = '')")
                no_mbid = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(r.album_id IS NULL OR r.album_id = '')")
                no_album = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(r.release_date IS NULL OR r.release_date = '')")
                no_date = cursor.fetchone()[0] or 0

                # 11. Genre statistics
                genre_where = "WHERE quality_score < 1.0" if is_incomplete_only else ""
                cursor.execute(f"SELECT primary_genre, COUNT(*) FROM vw_recording_overview {genre_where} GROUP BY primary_genre ORDER BY COUNT(*) DESC")
                genre_rows = cursor.fetchall()

                subgenre_where = "WHERE quality_score < 1.0 AND primary_subgenre IS NOT NULL AND primary_subgenre != 'Unclassified'" if is_incomplete_only else "WHERE primary_subgenre IS NOT NULL AND primary_subgenre != 'Unclassified'"
                cursor.execute(f"SELECT primary_subgenre, COUNT(*) FROM vw_recording_overview {subgenre_where} GROUP BY primary_subgenre ORDER BY COUNT(*) DESC")
                sub_rows = cursor.fetchall()

                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "r.subgenre IS NOT NULL AND r.subgenre != 'Unclassified'")
                deep_sub_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "r.genre IS NOT NULL AND r.genre != 'Unclassified'")
                root_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_recordings r " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(r.genre IS NULL OR r.genre = 'Unclassified') AND (r.subgenre IS NULL OR r.subgenre = 'Unclassified')")
                raw_tag_cnt = cursor.fetchone()[0] or 0

                cursor.execute("SELECT source_id, COUNT(*) FROM meta_evidence GROUP BY source_id ORDER BY COUNT(*) DESC LIMIT 8")
                source_rows = cursor.fetchall()

                cursor.execute("""
                    SELECT value, COUNT(*) as occurrence_count 
                    FROM meta_evidence 
                    WHERE field_name IN ('genre', 'subgenre') AND source_id != 'SRC_TAXONOMY'
                    GROUP BY value 
                    ORDER BY occurrence_count DESC LIMIT 12
                """)
                unmapped_rows = cursor.fetchall()

                # 12. Bitrates & Channels
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "UPPER(a.format) IN ('FLAC', 'WAV')")
                flac_pcm = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(a.bitrate >= 280000 OR (a.bitrate >= 280 AND a.bitrate <= 1000)) AND UPPER(a.format) NOT IN ('FLAC', 'WAV')")
                b320 = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "(a.bitrate BETWEEN 220000 AND 279999 OR (a.bitrate BETWEEN 220 AND 279)) AND UPPER(a.format) NOT IN ('FLAC', 'WAV')")
                b256 = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "((a.bitrate < 220000 AND a.bitrate < 220) OR a.bitrate IS NULL) AND UPPER(a.format) NOT IN ('FLAC', 'WAV')")
                blow = cursor.fetchone()[0] or 0

                cursor.execute("SELECT a.channels, COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + " GROUP BY a.channels ORDER BY COUNT(*) DESC")
                ch_rows = cursor.fetchall()

                # 13. Detailed sample rates & DSP coverage
                cursor.execute("SELECT a.sample_rate, COUNT(*) FROM core_assets a JOIN core_recordings r ON a.recording_id = r.id " + full_rec_join + " GROUP BY a.sample_rate ORDER BY COUNT(*) DESC LIMIT 5")
                sr_rows = cursor.fetchall()

                cursor.execute("SELECT COUNT(*) FROM core_sonic_features sf JOIN core_recordings r ON sf.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "sf.bpm IS NOT NULL")
                bpm_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_sonic_features sf JOIN core_recordings r ON sf.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "sf.lufs_loudness IS NOT NULL")
                lufs_cnt = cursor.fetchone()[0] or 0
                cursor.execute("SELECT COUNT(*) FROM core_sonic_features sf JOIN core_recordings r ON sf.recording_id = r.id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "sf.key_signature IS NOT NULL AND sf.key_signature != ''")
                key_cnt = cursor.fetchone()[0] or 0

                # 14. Geographics & Decades
                cursor.execute("SELECT ar.country FROM core_artists ar JOIN core_recordings r ON ar.id = r.artist_id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "ar.country IS NOT NULL AND ar.country != ''")
                country_data_rows = cursor.fetchall()
                region_counts: Dict[str, int] = defaultdict(int)
                country_counts: Dict[str, int] = defaultdict(int)

                for (c_code,) in country_data_rows:
                    reg = get_region_for_country(c_code)
                    region_counts[reg] += 1
                    country_counts[c_code.upper()] += 1

                sorted_regions = sorted(region_counts.items(), key=lambda x: x[1], reverse=True)[:6]
                total_artists = sum(region_counts.values()) or 1
                sorted_countries = sorted(country_counts.items(), key=lambda x: x[1], reverse=True)[:12]

                cursor.execute("""
                    SELECT 
                        CASE 
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) < 1970 THEN '< 1970s'
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) BETWEEN 1970 AND 1979 THEN '1970s'
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) BETWEEN 1980 AND 1989 THEN '1980s'
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) BETWEEN 1990 AND 1999 THEN '1990s'
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) BETWEEN 2000 AND 2009 THEN '2000s'
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) BETWEEN 2010 AND 2019 THEN '2010s'
                            WHEN CAST(SUBSTR(COALESCE(original_release_date, release_date), 1, 4) AS INT) >= 2020 THEN '2020s'
                            ELSE 'Unknown Era'
                        END AS decade,
                        COUNT(*)
                    FROM core_recordings r
                    """ + full_rec_join + """
                    GROUP BY decade ORDER BY decade ASC
                """)
                decade_rows = cursor.fetchall()

                cursor.execute("SELECT ar.artist_type, COUNT(*) FROM core_artists ar JOIN core_recordings r ON ar.id = r.artist_id " + full_rec_join + (" AND " if is_incomplete_only else " WHERE ") + "ar.artist_type IS NOT NULL GROUP BY ar.artist_type")
                art_type_rows = cursor.fetchall()

                # Package the results
                statistics = {
                    "total_tracks": total_tracks, "lossless_tracks": lossless_tracks,
                    "total_bytes": total_bytes, "total_seconds": total_seconds, "avg_q": avg_q,
                    "format_counts": format_counts, "q100": q100, "q80": q80, "q50": q50, "q0": q0,
                    "hires_cnt": hires_cnt, "cd_lossless_cnt": cd_lossless_cnt,
                    "high_lossy_cnt": high_lossy_cnt, "low_lossy_cnt": low_lossy_cnt,
                    "isrc_cnt": isrc_cnt, "mbid_cnt": mbid_cnt, "acoustid_cnt": acoustid_cnt,
                    "state_rows": state_rows, "no_isrc": no_isrc, "no_mbid": no_mbid,
                    "no_album": no_album, "no_date": no_date, "genre_rows": genre_rows,
                    "sub_rows": sub_rows, "deep_sub_cnt": deep_sub_cnt, "root_cnt": root_cnt,
                    "raw_tag_cnt": raw_tag_cnt, "source_rows": source_rows, "unmapped_rows": unmapped_rows,
                    "flac_pcm": flac_pcm, "b320": b320, "b256": b256, "blow": blow, "ch_rows": ch_rows,
                    "sr_rows": sr_rows, "bpm_cnt": bpm_cnt, "lufs_cnt": lufs_cnt, "key_cnt": key_cnt,
                    "sorted_regions": sorted_regions, "total_artists": total_artists,
                    "sorted_countries": sorted_countries, "decade_rows": decade_rows, "art_type_rows": art_type_rows
                }

                self.analytics_loaded.emit(statistics)
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Analytics worker error: {ex}", "ERROR"))
                # Send empty dictionary to unblock the refresh button states
                self.analytics_loaded.emit({})
            finally:
                close_thread_connection()

        threading.Thread(target=worker, daemon=True).start()

    @Slot(dict)
    def _on_analytics_loaded(self, data: dict) -> None:
        """Invoked securely in the GUI thread once metric thread processing finishes."""
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("[REFRESH ANALYTICS]")

        if not data:
            return

        total_tracks = data["total_tracks"]
        total_seconds = data["total_seconds"]
        total_bytes = data["total_bytes"]
        lossless_tracks = data["lossless_tracks"]
        avg_q = data["avg_q"]
        total_artists = data["total_artists"]

        size_gb = total_bytes / (1024 ** 3)
        size_str = f"{size_gb:.2f} GB" if size_gb < 1024 else f"{size_gb / 1024:.2f} TB"

        playtime_hours = total_seconds / 3600.0
        playtime_str = f"{playtime_hours:.1f} hrs" if playtime_hours < 48 else f"{playtime_hours / 24.0:.1f} days"
        lossless_pct = (lossless_tracks / total_tracks * 100) if total_tracks > 0 else 0.0

        self.card_tracks.set_value(f"{total_tracks:,}", "Scoped recordings")
        self.card_playtime.set_value(playtime_str, f"{int(total_seconds):,} seconds total")
        self.card_storage.set_value(size_str, f"{total_bytes / (1024**2):,.0f} MB media data")
        self.card_lossless.set_value(f"{lossless_pct:.1f}%", f"{lossless_tracks:,} FLAC/WAV assets")
        self.card_quality.set_value(f"{avg_q * 100:.1f}%", "Completeness score")

        # --- TAB 1: OVERVIEW METRICS ---
        self.card_fidelity.clear_rows()
        if not data["format_counts"]:
            self.card_fidelity.add_row(AnalyticsBarRow("No assets in scope", 0, 1, "#475569"))
        else:
            for fmt, cnt in data["format_counts"]:
                fmt_str = fmt or "UNKNOWN"
                color = "#10b981" if fmt_str in ("FLAC", "WAV") else "#f59e0b"
                self.card_fidelity.add_row(AnalyticsBarRow(f"Format: {fmt_str}", cnt, total_tracks, color))

        self.card_q_density.clear_rows()
        self.card_q_density.add_row(AnalyticsBarRow("100% Studio Complete", data["q100"], total_tracks, "#10b981"))
        self.card_q_density.add_row(AnalyticsBarRow("80-99% High Quality", data["q80"], total_tracks, "#38bdf8"))
        self.card_q_density.add_row(AnalyticsBarRow("50-79% Partial Quality", data["q50"], total_tracks, "#f59e0b"))
        self.card_q_density.add_row(AnalyticsBarRow("< 50% Critical Repair", data["q0"], total_tracks, "#ef4444"))

        self.card_audio_specs.clear_rows()
        self.card_audio_specs.add_row(AnalyticsBarRow("Hi-Res Audio (>=88.2k)", data["hires_cnt"], total_tracks, "#10b981"))
        self.card_audio_specs.add_row(AnalyticsBarRow("CD Lossless (44.1k/48k)", data["cd_lossless_cnt"], total_tracks, "#38bdf8"))
        self.card_audio_specs.add_row(AnalyticsBarRow("High Bitrate (>=320k)", data["high_lossy_cnt"], total_tracks, "#f59e0b"))
        self.card_audio_specs.add_row(AnalyticsBarRow("Standard Lossy (<320k)", data["low_lossy_cnt"], total_tracks, "#ef4444"))

        self.card_authority_cov.clear_rows()
        self.card_authority_cov.add_row(AnalyticsBarRow("ISO 3901 ISRC Code", data["isrc_cnt"], total_tracks, "#10b981"))
        self.card_authority_cov.add_row(AnalyticsBarRow("MusicBrainz MBID", data["mbid_cnt"], total_tracks, "#38bdf8"))
        self.card_authority_cov.add_row(AnalyticsBarRow("AcoustID Fingerprint", data["acoustid_cnt"], total_tracks, "#f59e0b"))

        self.card_lifecycle.clear_rows()
        for st_val, st_cnt in data["state_rows"]:
            self.card_lifecycle.add_row(AnalyticsBarRow(f"State: {st_val or 'NEW'}", st_cnt, total_tracks, "#38bdf8"))

        self.card_completeness_deficit.clear_rows()
        self.card_completeness_deficit.add_row(AnalyticsBarRow("Missing ISRC Code", data["no_isrc"], total_tracks, "#ef4444"))
        self.card_completeness_deficit.add_row(AnalyticsBarRow("Missing MusicBrainz MBID", data["no_mbid"], total_tracks, "#ef4444"))
        self.card_completeness_deficit.add_row(AnalyticsBarRow("Missing Album Relation", data["no_album"], total_tracks, "#f59e0b"))
        self.card_completeness_deficit.add_row(AnalyticsBarRow("Missing Release Date", data["no_date"], total_tracks, "#f59e0b"))

        # --- TAB 2: FLAT GENRE DISTRIBUTION ---
        self.card_genres.clear_rows()
        if not data["genre_rows"]:
            self.card_genres.add_row(AnalyticsBarRow("No data in scope", 0, 1, "#475569"))
        else:
            for g_name, g_cnt in data["genre_rows"]:
                self.card_genres.add_row(AnalyticsBarRow(str(g_name or "Unclassified"), g_cnt, total_tracks, "#f59e0b"))

        self.card_subgenres.clear_rows()
        if not data["sub_rows"]:
            self.card_subgenres.add_row(AnalyticsBarRow("Unclassified", total_tracks, total_tracks if total_tracks > 0 else 1, "#475569"))
        else:
            for sub_name, sub_cnt in data["sub_rows"]:
                self.card_subgenres.add_row(AnalyticsBarRow(str(sub_name), sub_cnt, total_tracks, "#10b981"))

        self.card_tax_depth.clear_rows()
        self.card_tax_depth.add_row(AnalyticsBarRow("Resolved Subgenre", data["deep_sub_cnt"], total_tracks, "#10b981"))
        self.card_tax_depth.add_row(AnalyticsBarRow("Resolved Primary Genre", data["root_cnt"], total_tracks, "#38bdf8"))
        self.card_tax_depth.add_row(AnalyticsBarRow("Unclassified Tracks", data["raw_tag_cnt"], total_tracks, "#f59e0b"))

        self.card_evidence_sources.clear_rows()
        for src_id, src_cnt in data["source_rows"]:
            self.card_evidence_sources.add_row(AnalyticsBarRow(str(src_id or "SRC_UNKNOWN"), src_cnt, total_tracks, "#38bdf8"))

        self.card_unmapped_digest.clear_rows()
        if not data["unmapped_rows"]:
            self.card_unmapped_digest.add_row(AnalyticsBarRow("No raw evidence tags found", 0, 1, "#10b981"))
        else:
            for r_tag, cnt in data["unmapped_rows"]:
                color = "#10b981" if cnt >= 5 else "#f59e0b"
                self.card_unmapped_digest.add_row(AnalyticsBarRow(f"Tag: {r_tag}", cnt, max(1, data["unmapped_rows"][0][1]), color))

        # --- TAB 3: AUDIO SPECTRUM & DSP ---
        self.card_bitrates.clear_rows()
        self.card_bitrates.add_row(AnalyticsBarRow("Lossless PCM / FLAC", data["flac_pcm"], total_tracks, "#10b981"))
        self.card_bitrates.add_row(AnalyticsBarRow("High Quality (320 kbps)", data["b320"], total_tracks, "#38bdf8"))
        self.card_bitrates.add_row(AnalyticsBarRow("Medium Quality (256 kbps)", data["b256"], total_tracks, "#f59e0b"))
        self.card_bitrates.add_row(AnalyticsBarRow("Standard Lossy (<220 kbps)", data["blow"], total_tracks, "#ef4444"))

        self.card_channels.clear_rows()
        if not data["ch_rows"]:
            self.card_channels.add_row(AnalyticsBarRow("No assets in scope", 0, 1, "#475569"))
        else:
            for ch_cnt_val, ch_track_cnt in data["ch_rows"]:
                ch_label = "Stereo (2.0ch)" if ch_cnt_val == 2 else ("Mono (1.0ch)" if ch_cnt_val == 1 else f"Multichannel ({ch_cnt_val}ch)")
                self.card_channels.add_row(AnalyticsBarRow(ch_label, ch_track_cnt, total_tracks, "#38bdf8"))

        self.card_sample_rates.clear_rows()
        if not data["sr_rows"]:
            self.card_sample_rates.add_row(AnalyticsBarRow("No assets in scope", 0, 1, "#475569"))
        else:
            for sr_val, sr_cnt in data["sr_rows"]:
                sr_label = f"{float(sr_val or 0) / 1000.0:.1f} kHz"
                color = "#10b981" if (sr_val or 0) >= 88200 else "#38bdf8"
                self.card_sample_rates.add_row(AnalyticsBarRow(sr_label, sr_cnt, total_tracks, color))

        self.card_sonic_cov.clear_rows()
        self.card_sonic_cov.add_row(AnalyticsBarRow("BPM Tempo Extracted", data["bpm_cnt"], total_tracks, "#10b981"))
        self.card_sonic_cov.add_row(AnalyticsBarRow("LUFS Loudness Analyzed", data["lufs_cnt"], total_tracks, "#38bdf8"))
        self.card_sonic_cov.add_row(AnalyticsBarRow("Key Signature Identified", data["key_cnt"], total_tracks, "#f59e0b"))

        # --- TAB 4: DEMOGRAPHICS & ERA ---
        self.card_regions.clear_rows()
        if not data["sorted_regions"]:
            self.card_regions.add_row(AnalyticsBarRow("No artist regions", 0, 1, "#475569"))
        else:
            for reg_name, reg_cnt in data["sorted_regions"]:
                self.card_regions.add_row(AnalyticsBarRow(reg_name, reg_cnt, total_artists, "#38bdf8"))

        self.card_countries.clear_rows()
        if not data["sorted_countries"]:
            self.card_countries.add_row(AnalyticsBarRow("No artist countries", 0, 1, "#475569"))
        else:
            for c_name, c_cnt in data["sorted_countries"]:
                self.card_countries.add_row(AnalyticsBarRow(f"Country: {c_name}", c_cnt, total_artists, "#f59e0b"))

        self.card_decades.clear_rows()
        for dec, cnt in data["decade_rows"]:
            if dec:
                self.card_decades.add_row(AnalyticsBarRow(dec, cnt, total_tracks, "#f59e0b"))

        self.card_artist_types.clear_rows()
        if not data["art_type_rows"]:
            self.card_artist_types.add_row(AnalyticsBarRow("No demographic data", 0, 1, "#475569"))
        else:
            for a_type, a_cnt in data["art_type_rows"]:
                self.card_artist_types.add_row(AnalyticsBarRow(f"Type: {a_type}", a_cnt, total_artists, "#10b981"))

        # --- TAB 5: GATHERED DATA VAULT ---
        self._reload_data_vault_tab()

    def _reload_data_vault_tab(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM meta_evidence")
            tot_facts = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM sys_payloads")
            tot_payloads = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM meta_decisions")
            tot_decisions = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM meta_issues WHERE status = 'OPEN'")
            tot_issues = cursor.fetchone()[0] or 0

            self.card_total_facts.set_value(f"{tot_facts:,}", "Total meta_evidence facts")
            self.card_total_payloads.set_value(f"{tot_payloads:,}", "Compressed API JSON blobs")
            self.card_total_decisions.set_value(f"{tot_decisions:,}", "Applied resolver decisions")
            self.card_total_issues.set_value(f"{tot_issues:,}", "Open issue queue count")

            self.card_fact_sources.clear_rows()
            cursor.execute("SELECT source_id, COUNT(*) FROM meta_evidence GROUP BY source_id ORDER BY COUNT(*) DESC")
            for src_id, src_cnt in cursor.fetchall():
                self.card_fact_sources.add_row(AnalyticsBarRow(str(src_id or "SRC_UNKNOWN"), src_cnt, tot_facts, "#38bdf8"))

            self._reload_fact_explorer()

        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] Data Vault refresh error: {ex}", "ERROR"))

    def _reload_fact_explorer(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            filter_text = self.search_fact_input.text().strip()
            if filter_text:
                q = "%" + filter_text + "%"
                cursor.execute("""
                    SELECT entity_id, field_name, value, evidence_class, source_id, confidence 
                    FROM meta_evidence 
                    WHERE (entity_id LIKE %s OR field_name LIKE %s OR value LIKE %s OR source_id LIKE %s)
                    ORDER BY id DESC LIMIT 150
                """, (q, q, q, q))
            else:
                cursor.execute("""
                    SELECT entity_id, field_name, value, evidence_class, source_id, confidence 
                    FROM meta_evidence 
                    ORDER BY id DESC LIMIT 150
                """)

            rows = cursor.fetchall()
            self.table_fact_explorer.setRowCount(len(rows))
            for r_idx, (e_id, f_name, val, e_class, src_id, conf) in enumerate(rows):
                item_eid = QTableWidgetItem(str(e_id[:8]) + "...")
                item_fname = QTableWidgetItem(str(f_name))
                item_val = QTableWidgetItem(str(val or ""))
                item_class = QTableWidgetItem(str(e_class or "LOCAL"))
                item_src = QTableWidgetItem(str(src_id or ""))
                item_conf = QTableWidgetItem(f"{float(conf or 1.0) * 100:.0f}%")

                item_fname.setForeground(QColor("#f59e0b"))
                item_src.setForeground(QColor("#38bdf8"))

                self.table_fact_explorer.setItem(r_idx, 0, item_eid)
                self.table_fact_explorer.setItem(r_idx, 1, item_fname)
                self.table_fact_explorer.setItem(r_idx, 2, item_val)
                self.table_fact_explorer.setItem(r_idx, 3, item_class)
                self.table_fact_explorer.setItem(r_idx, 4, item_src)
                self.table_fact_explorer.setItem(r_idx, 5, item_conf)

        except Exception:
            pass

    def _on_auto_expand_clicked(self) -> None:
        event_bus.publish(LogEvent("[*] Dynamic Auto-Expansion is deactivated in Closed Flat-String Consensus mode.", "INFO"))

    # =========================================================================
    # TAB 6: DATA EXPORT HELPERS
    # =========================================================================
    def _export_catalog_csv(self) -> None:
        try:
            settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            export_file = settings.EXPORTS_DIR / "catalog_export.csv"

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, artist_name, album_title, primary_genre, primary_subgenre, 
                       quality_score, format, bitrate, sample_rate, isrc, musicbrainz_recording_id 
                FROM vw_recording_overview ORDER BY id ASC
            """)
            rows = cursor.fetchall()

            headers = ["id", "title", "artist", "album", "genre", "subgenre", "quality_score", "format", "bitrate", "sample_rate", "isrc", "musicbrainz_recording_id"]
            
            with open(export_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)

            preview_lines = [f"CATALOG CSV EXPORT SUCCESS: {export_file}\nTotal Records Exported: {len(rows):,}\n"]
            preview_lines.append(",".join(headers))
            for r in rows[:15]:
                preview_lines.append(",".join(str(x) for x in r))

            self.preview_export_text.setPlainText("\n".join(preview_lines))
            event_bus.publish(LogEvent(f"[+] Catalog CSV exported successfully to {export_file}", "SUCCESS"))
        except Exception as ex:
            self.preview_export_text.setPlainText(f"EXPORT ERROR: {ex}")

    def _export_taxonomy_json(self) -> None:
        try:
            settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            export_file = settings.EXPORTS_DIR / "taxonomy_export.json"

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT genre, subgenre, COUNT(*) as count 
                FROM core_recordings 
                WHERE genre IS NOT NULL AND genre != 'Unclassified'
                GROUP BY genre, subgenre 
                ORDER BY count DESC
            """)
            rows = cursor.fetchall()
            
            associations = {}
            for genre, subgenre, count in rows:
                if genre not in associations:
                    associations[genre] = []
                if subgenre and subgenre != "Unclassified":
                    associations[genre].append(f"{subgenre} ({count} tracks)")

            payload = {
                "resolved_genre_associations": associations,
                "engine_status": "Flat Consensus Mode"
            }

            json_str = json.dumps(payload, indent=2)
            with open(export_file, "w", encoding="utf-8") as f:
                f.write(json_str)

            self.preview_export_text.setPlainText(f"GENRE ASSOCIATIONS JSON EXPORT SUCCESS: {export_file}\n\n" + json_str[:2000] + "\n...")
            event_bus.publish(LogEvent(f"[+] Flat genre associations exported successfully to {export_file}", "SUCCESS"))
        except Exception as ex:
            self.preview_export_text.setPlainText(f"EXPORT ERROR: {ex}")

    def _export_unmapped_txt(self) -> None:
        try:
            settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            export_file = settings.EXPORTS_DIR / "unmapped_tags.txt"

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT value, COUNT(*) as occurrence_count 
                FROM meta_evidence 
                WHERE field_name IN ('genre', 'subgenre') AND source_id != 'SRC_TAXONOMY'
                GROUP BY value 
                ORDER BY occurrence_count DESC
            """)
            rows = cursor.fetchall()

            lines = [f"# MDMS RAW EVIDENCE TAGS REPORT - Total: {len(rows):,} tags"]
            for tag, cnt in rows:
                lines.append(f"{tag}\t{cnt}")

            txt_out = "\n".join(lines)
            with open(export_file, "w", encoding="utf-8") as f:
                f.write(txt_out)

            self.preview_export_text.setPlainText(f"RAW TAGS TXT EXPORT SUCCESS: {export_file}\n\n" + "\n".join(lines[:30]))
            event_bus.publish(LogEvent(f"[+] Raw Tags TXT exported successfully to {export_file}", "SUCCESS"))
        except Exception as ex:
            self.preview_export_text.setPlainText(f"EXPORT ERROR: {ex}")

    def _generate_markdown_report(self) -> None:
        try:
            settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            export_file = settings.EXPORTS_DIR / "library_summary_report.md"

            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM core_recordings")
            tot_trks = cursor.fetchone()[0] or 0

            cursor.execute("SELECT AVG(quality_score) FROM meta_validation")
            avg_q = (cursor.fetchone()[0] or 0.0) * 100.0

            cursor.execute("SELECT UPPER(format), COUNT(*) FROM core_assets GROUP BY UPPER(format)")
            fmts = cursor.fetchall()

            cursor.execute("SELECT primary_genre, COUNT(*) FROM vw_recording_overview GROUP BY primary_genre ORDER BY COUNT(*) DESC LIMIT 5")
            top_genres = cursor.fetchall()

            md_lines = [
                "# MDMS WORKSTATION // AUDIO LIBRARY SUMMARY REPORT",
                "--------------------------------------------------",
                f"- **Total Library Tracks**: {tot_trks:,}",
                f"- **Average Quality Index**: {avg_q:.1f}%",
                "",
                "## AUDIO CODEC BREAKDOWN",
            ]
            for fmt, cnt in fmts:
                pct = (cnt / tot_trks * 100) if tot_trks > 0 else 0
                md_lines.append(f"- **{fmt or 'UNKNOWN'}**: {cnt:,} tracks ({pct:.1f}%)")

            md_lines.extend(["", "## TOP PRIMARY ROOT GENRES"])
            for g, cnt in top_genres:
                pct = (cnt / tot_trks * 100) if tot_trks > 0 else 0
                md_lines.append(f"- **{g or 'Unclassified'}**: {cnt:,} tracks ({pct:.1f}%)")

            md_out = "\n".join(md_lines)
            with open(export_file, "w", encoding="utf-8") as f:
                f.write(md_out)

            self.preview_export_text.setPlainText(f"MARKDOWN REPORT GENERATED: {export_file}\n\n" + md_out)
            event_bus.publish(LogEvent(f"[+] Library Markdown report saved to {export_file}", "SUCCESS"))
        except Exception as ex:
            self.preview_export_text.setPlainText(f"EXPORT ERROR: {ex}")