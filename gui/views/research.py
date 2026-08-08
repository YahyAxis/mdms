"""
Scientific Research Studio & Field Provenance Explorer
Provides SQL cohort dataset building, Train/Validation/Test ML dataset splitting,
cross-tab provenance jumping, and complete field-level provenance audit timelines.
Refactored to import centralized UI components.
"""

import os
import csv
import json
import random
from typing import List, Dict, Any, Optional, Tuple
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, 
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, 
    QHeaderView, QSplitter, QAbstractItemView, QScrollArea, 
    QTabWidget, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont

from config.settings import settings
from db import get_connection
from domain.events import event_bus, LogEvent
from gui.widgets.metric import MetricCard

# Centralized imports replacing locally duplicated widgets and layout helpers
from gui.widgets.common import create_tab_scroll_area, ResearchCard

class ResearchWorkspace(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.current_cohort_rows: List[Tuple[Any, ...]] = []
        self.provenance_tracks: List[Tuple[str, str, str]] = []

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        header_row = QHBoxLayout()
        header = QLabel("RESEARCH STUDIO // DATASET COHORTS & FIELD PROVENANCE TIMELINE")
        header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 11pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(header)
        header_row.addStretch()

        btn_refresh = QPushButton("[REFRESH RESEARCH DATA]")
        btn_refresh.setFixedWidth(180)
        btn_refresh.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_refresh.clicked.connect(self.reload_workspace)
        header_row.addWidget(btn_refresh)

        main_layout.addLayout(header_row)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=1)

        self._build_tab_cohort_builder()
        self._build_tab_ml_exporter()
        self._build_tab_provenance()

        self.reload_workspace()

    def _build_tab_cohort_builder(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_rules = ResearchCard("Dataset Cohort Filter Rule Generator")
        rules_row = QHBoxLayout()
        
        lbl_g = QLabel("Genre Keyword:")
        lbl_g.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.input_genre_filter = QLineEdit()
        self.input_genre_filter.setPlaceholderText("Filter genre (e.g. Rock, Electronic)...")
        self.input_genre_filter.textChanged.connect(self._reload_cohort_preview)

        lbl_q = QLabel("Min Quality:")
        lbl_q.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.spin_min_q = QDoubleSpinBox()
        self.spin_min_q.setRange(0.0, 1.0)
        self.spin_min_q.setSingleStep(0.1)
        self.spin_min_q.setValue(0.5)
        self.spin_min_q.setStyleSheet("font-family: 'Consolas', monospace;")
        self.spin_min_q.valueChanged.connect(self._reload_cohort_preview)

        lbl_dec = QLabel("Era Decade:")
        lbl_dec.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.combo_decade_filter = QComboBox()
        self.combo_decade_filter.addItems(["[ALL DECADES]", "< 1970s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s"])
        self.combo_decade_filter.currentIndexChanged.connect(self._reload_cohort_preview)

        lbl_fmt = QLabel("Format:")
        lbl_fmt.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.combo_fmt_filter = QComboBox()
        self.combo_fmt_filter.addItems(["[ALL FORMATS]", "FLAC", "MP3", "WAV"])
        self.combo_fmt_filter.currentIndexChanged.connect(self._reload_cohort_preview)

        rules_row.addWidget(lbl_g)
        rules_row.addWidget(self.input_genre_filter, stretch=1)
        rules_row.addWidget(lbl_q)
        rules_row.addWidget(self.spin_min_q)
        rules_row.addWidget(lbl_dec)
        rules_row.addWidget(self.combo_decade_filter)
        rules_row.addWidget(lbl_fmt)
        rules_row.addWidget(self.combo_fmt_filter)

        card_rules.card_layout.addLayout(rules_row)
        layout.addWidget(card_rules)

        cohort_kpi = QHBoxLayout()
        self.card_c_tracks = MetricCard("COHORT TRACKS", "--", "Matched dataset items", "#10b981")
        self.card_c_time = MetricCard("TOTAL DURATION", "--", "Dataset playback hours", "#38bdf8")
        self.card_c_artists = MetricCard("UNIQUE ARTISTS", "--", "Distinct artist count", "#f59e0b")
        self.card_c_quality = MetricCard("AVG QUALITY INDEX", "--", "Cohort completeness")

        cohort_kpi.addWidget(self.card_c_tracks)
        cohort_kpi.addWidget(self.card_c_time)
        cohort_kpi.addWidget(self.card_c_artists)
        cohort_kpi.addWidget(self.card_c_quality)
        layout.addLayout(cohort_kpi)

        card_preview = ResearchCard("Matched Research Cohort Preview Table")
        
        btn_jump_row = QHBoxLayout()
        btn_jump_prov = QPushButton("[INSPECT PROVENANCE FOR SELECTED TRACK]")
        btn_jump_prov.setStyleSheet("""
            QPushButton {
                background-color: #1c1c22; color: #f59e0b; border: 1px solid #f59e0b;
                font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold; padding: 3px 10px;
            }
            QPushButton:hover { background-color: #f59e0b; color: #101012; }
        """)
        btn_jump_prov.clicked.connect(self._jump_cohort_to_provenance)
        btn_jump_row.addStretch()
        btn_jump_row.addWidget(btn_jump_prov)
        card_preview.card_layout.addLayout(btn_jump_row)

        self.table_cohort_preview = QTableWidget()
        self.table_cohort_preview.setColumnCount(7)
        self.table_cohort_preview.setHorizontalHeaderLabels(["Track ID", "Track Title", "Artist Name", "Album Title", "Primary Genre", "Format", "Quality"])
        self.table_cohort_preview.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_cohort_preview.setColumnWidth(0, 95)
        self.table_cohort_preview.setColumnWidth(2, 140)
        self.table_cohort_preview.setColumnWidth(3, 140)
        self.table_cohort_preview.setColumnWidth(4, 110)
        self.table_cohort_preview.setColumnWidth(5, 75)
        self.table_cohort_preview.setColumnWidth(6, 75)
        self.table_cohort_preview.verticalHeader().hide()
        self.table_cohort_preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_cohort_preview.itemDoubleClicked.connect(self._jump_cohort_to_provenance)
        self.table_cohort_preview.setMinimumHeight(280)
        self.table_cohort_preview.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_preview.add_row(self.table_cohort_preview)
        layout.addWidget(card_preview)

        self.tabs.addTab(scroll, "[1] RESEARCH COHORT BUILDER")

    def _build_tab_ml_exporter(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_split_controls = ResearchCard("Machine Learning Split Ratios & Exporter")
        split_row = QHBoxLayout()
        
        lbl_tr = QLabel("Train %:")
        lbl_tr.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.spin_train_pct = QSpinBox()
        self.spin_train_pct.setRange(40, 90)
        self.spin_train_pct.setValue(70)
        self.spin_train_pct.valueChanged.connect(self._validate_split_ratios)

        lbl_val = QLabel("Val %:")
        lbl_val.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.spin_val_pct = QSpinBox()
        self.spin_val_pct.setRange(5, 30)
        self.spin_val_pct.setValue(15)
        self.spin_val_pct.valueChanged.connect(self._validate_split_ratios)

        lbl_te = QLabel("Test %:")
        lbl_te.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a;")
        self.spin_test_pct = QSpinBox()
        self.spin_test_pct.setRange(5, 30)
        self.spin_test_pct.setValue(15)
        self.spin_test_pct.valueChanged.connect(self._validate_split_ratios)

        self.lbl_split_sum = QLabel("TOTAL: 100% [VALID]")
        self.lbl_split_sum.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #10b981; font-weight: bold;")

        self.combo_export_fmt = QComboBox()
        self.combo_export_fmt.addItems(["CSV FORMAT", "JSON FORMAT"])

        btn_export_split = QPushButton("[GENERATE ML SPLITS & EXPORT]")
        btn_export_split.setObjectName("AmberPrimaryBtn")
        btn_export_split.setFixedHeight(28)
        btn_export_split.clicked.connect(self._export_ml_splits)

        split_row.addWidget(lbl_tr)
        split_row.addWidget(self.spin_train_pct)
        split_row.addWidget(lbl_val)
        split_row.addWidget(self.spin_val_pct)
        split_row.addWidget(lbl_te)
        split_row.addWidget(self.spin_test_pct)
        split_row.addWidget(self.lbl_split_sum)
        split_row.addWidget(self.combo_export_fmt)
        split_row.addWidget(btn_export_split)

        card_split_controls.card_layout.addLayout(split_row)
        layout.addWidget(card_split_controls)

        card_preview = ResearchCard("Exported ML Dataset Splits Live Preview Console")
        self.preview_split_text = QPlainTextEdit()
        self.preview_split_text.setReadOnly(True)
        self.preview_split_text.setMinimumHeight(320)
        self.preview_split_text.setFont(QFont("Consolas", 8.5))
        self.preview_split_text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0c0c0e; color: #10b981; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace;
            }
        """)
        card_preview.add_row(self.preview_split_text)
        layout.addWidget(card_preview)

        self.tabs.addTab(scroll, "[2] ML SPLIT EXPORTER")

    def _build_tab_provenance(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_select = ResearchCard("Select Recording for Data Lineage & Provenance Inspection")
        search_prov_row = QHBoxLayout()
        lbl_ps = QLabel("SEARCH TRACK:")
        lbl_ps.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a; font-weight: bold;")
        self.search_prov_input = QLineEdit()
        self.search_prov_input.setPlaceholderText("Type track title, artist, or recording ID...")
        self.search_prov_input.textChanged.connect(self._search_provenance_tracks)

        self.combo_track_select = QComboBox()
        self.combo_track_select.setFixedWidth(300)
        self.combo_track_select.currentIndexChanged.connect(self._reload_provenance_timeline)

        search_prov_row.addWidget(lbl_ps)
        search_prov_row.addWidget(self.search_prov_input, stretch=1)
        search_prov_row.addWidget(self.combo_track_select)
        card_select.card_layout.addLayout(search_prov_row)
        layout.addWidget(card_select)

        self.card_active_decisions = ResearchCard("Current Active Field Decisions (meta_decisions Winners)")
        self.table_active_decisions = QTableWidget()
        self.table_active_decisions.setColumnCount(5)
        self.table_active_decisions.setHorizontalHeaderLabels(["Field Name", "Selected Winning Value", "Previous Value", "Resolver Reason", "Applied UTC"])
        self.table_active_decisions.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_active_decisions.setColumnWidth(0, 110)
        self.table_active_decisions.setColumnWidth(2, 140)
        self.table_active_decisions.setColumnWidth(3, 260)
        self.table_active_decisions.setColumnWidth(4, 140)
        self.table_active_decisions.verticalHeader().hide()
        self.table_active_decisions.setFixedHeight(180)
        self.table_active_decisions.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        self.card_active_decisions.add_row(self.table_active_decisions)
        layout.addWidget(self.card_active_decisions)

        self.card_fact_history = ResearchCard("Historical Fact Lineage Audit Trail (Winning Facts vs Superseded Evidence)")
        self.table_fact_history = QTableWidget()
        self.table_fact_history.setColumnCount(7)
        self.table_fact_history.setHorizontalHeaderLabels(["Field Name", "Gathered Fact Value", "Source Provider", "Confidence", "Status Badge", "Run ID", "Observed UTC"])
        self.table_fact_history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_fact_history.setColumnWidth(0, 110)
        self.table_fact_history.setColumnWidth(2, 150)
        self.table_fact_history.setColumnWidth(3, 85)
        self.table_fact_history.setColumnWidth(4, 120)
        self.table_fact_history.setColumnWidth(5, 95)
        self.table_fact_history.setColumnWidth(6, 140)
        self.table_fact_history.verticalHeader().hide()
        self.table_fact_history.setMinimumHeight(240)
        self.table_fact_history.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        self.card_fact_history.add_row(self.table_fact_history)
        layout.addWidget(self.card_fact_history)

        self.tabs.addTab(scroll, "[3] FIELD PROVENANCE TIMELINE")

    def reload_workspace(self) -> None:
        self._reload_cohort_preview()
        self._search_provenance_tracks()

    def _validate_split_ratios(self) -> None:
        tot = self.spin_train_pct.value() + self.spin_val_pct.value() + self.spin_test_pct.value()
        if tot == 100:
            self.lbl_split_sum.setText("TOTAL: 100% [VALID]")
            self.lbl_split_sum.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #10b981; font-weight: bold;")
        else:
            self.lbl_split_sum.setText(f"TOTAL: {tot}% [INVALID SUM]")
            self.lbl_split_sum.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #ef4444; font-weight: bold;")

    def _jump_cohort_to_provenance(self) -> None:
        selected = self.table_cohort_preview.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        if row < len(self.current_cohort_rows):
            rec_id = self.current_cohort_rows[row][0]
            self.tabs.setCurrentIndex(2)
            self.search_prov_input.setText(rec_id[:8])

    def _reload_cohort_preview(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            genre_q = self.input_genre_filter.text().strip()
            min_q = self.spin_min_q.value()
            fmt_q = self.combo_fmt_filter.currentText()
            dec_q = self.combo_decade_filter.currentText()

            where_clauses = ["quality_score >= %s"]
            params: List[Any] = [min_q]

            if genre_q:
                where_clauses.append("(primary_genre ILIKE %s OR primary_subgenre ILIKE %s)")
                params.extend([f"%{genre_q}%", f"%{genre_q}%"])

            if fmt_q != "[ALL FORMATS]":
                where_clauses.append("UPPER(format) = %s")
                params.append(fmt_q.upper())

            if dec_q != "[ALL DECADES]":
                if dec_q == "< 1970s":
                    where_clauses.append("CAST(SUBSTR(COALESCE(original_release_date, issue_release_date), 1, 4) AS INTEGER) < 1970")
                elif "2020s" in dec_q:
                    where_clauses.append("CAST(SUBSTR(COALESCE(original_release_date, issue_release_date), 1, 4) AS INTEGER) >= 2020")
                else:
                    try:
                        start_yr = int(dec_q[:4])
                        where_clauses.append("CAST(SUBSTR(COALESCE(original_release_date, issue_release_date), 1, 4) AS INTEGER) BETWEEN %s AND %s")
                        params.extend([start_yr, start_yr + 9])
                    except ValueError:
                        pass

            sql = "SELECT id, title, artist_name, album_title, primary_genre, format, quality_score, duration, bitrate, sample_rate, channels, isrc, musicbrainz_recording_id FROM vw_recording_overview WHERE " + " AND ".join(where_clauses) + " ORDER BY id ASC LIMIT 200"
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            self.current_cohort_rows = rows

            tot_cnt = len(rows)
            tot_sec = sum(float(r[7] or 0.0) for r in rows)
            tot_hours = tot_sec / 3600.0
            unique_artists = len(set(r[2] for r in rows if r[2]))
            avg_q = (sum(float(r[6] or 0.0) for r in rows) / tot_cnt * 100.0) if tot_cnt > 0 else 0.0

            self.card_c_tracks.set_value(f"{tot_cnt:,}", "Cohort size")
            self.card_c_time.set_value(f"{tot_hours:.1f} hrs", f"{int(tot_sec):,} seconds")
            self.card_c_artists.set_value(f"{unique_artists:,}", "Attributed artists")
            self.card_c_quality.set_value(f"{avg_q:.1f}%", "Cohort completeness")

            self.table_cohort_preview.setRowCount(len(rows))
            for r_idx, r in enumerate(rows):
                item_q = QTableWidgetItem(f"{float(r[6] or 0.0) * 100:.0f}%")
                if float(r[6] or 0.0) >= 1.0:
                    item_q.setForeground(QColor("#10b981"))
                else:
                    item_q.setForeground(QColor("#f59e0b"))

                self.table_cohort_preview.setItem(r_idx, 0, QTableWidgetItem(r[0][:8] + "..."))
                self.table_cohort_preview.setItem(r_idx, 1, QTableWidgetItem(str(r[1] or "Untitled")))
                self.table_cohort_preview.setItem(r_idx, 2, QTableWidgetItem(str(r[2] or "Unknown")))
                self.table_cohort_preview.setItem(r_idx, 3, QTableWidgetItem(str(r[3] or "Unknown")))
                self.table_cohort_preview.setItem(r_idx, 4, QTableWidgetItem(str(r[4] or "Unclassified")))
                self.table_cohort_preview.setItem(r_idx, 5, QTableWidgetItem(str(r[5] or "FLAC")))
                self.table_cohort_preview.setItem(r_idx, 6, item_q)

        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] Cohort preview reload error: {ex}", "ERROR"))

    def _export_ml_splits(self) -> None:
        if not self.current_cohort_rows:
            self.preview_split_text.setPlainText("ERROR: Current research cohort is empty. Adjust cohort filters first.")
            return

        tot_pct = self.spin_train_pct.value() + self.spin_val_pct.value() + self.spin_test_pct.value()
        if tot_pct != 100:
            self.preview_split_text.setPlainText(f"ERROR: Train/Val/Test ratios sum to {tot_pct}%, which is invalid. Ratios must sum to exactly 100%.")
            return

        try:
            settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            fmt_type = self.combo_export_fmt.currentText()

            train_pct = self.spin_train_pct.value() / 100.0
            val_pct = self.spin_val_pct.value() / 100.0
            
            shuffled = list(self.current_cohort_rows)
            random.shuffle(shuffled)

            total = len(shuffled)
            train_cut = int(total * train_pct)
            val_cut = train_cut + int(total * val_pct)

            train_set = shuffled[:train_cut]
            val_set = shuffled[train_cut:val_cut]
            test_set = shuffled[val_cut:]

            ext = "csv" if "CSV" in fmt_type else "json"
            train_file = settings.EXPORTS_DIR / f"ml_cohort_train.{ext}"
            val_file = settings.EXPORTS_DIR / f"ml_cohort_val.{ext}"
            test_file = settings.EXPORTS_DIR / f"ml_cohort_test.{ext}"

            headers = ["id", "title", "artist", "album", "genre", "format", "quality_score", "duration", "bitrate", "sample_rate", "channels", "isrc", "musicbrainz_recording_id"]

            if "CSV" in fmt_type:
                for path, data_set in ((train_file, train_set), (val_file, val_set), (test_file, test_set)):
                    with open(path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(headers)
                        for r in data_set:
                            writer.writerow(r[:13])
            else:
                for path, data_set in ((train_file, train_set), (val_file, val_set), (test_file, test_set)):
                    dict_list = [dict(zip(headers, r[:13])) for r in data_set]
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(dict_list, f, indent=2)

            preview_msg = (
                f"ML DATASET SPLIT EXPORT SUCCESS ({fmt_type})\n"
                f"--------------------------------------------------\n"
                f"- Total Cohort Items : {total:,}\n"
                f"- Train Split ({self.spin_train_pct.value()}%) : {len(train_set):,} items -> {train_file.name}\n"
                f"- Val Split ({self.spin_val_pct.value()}%)   : {len(val_set):,} items -> {val_file.name}\n"
                f"- Test Split ({self.spin_test_pct.value()}%)  : {len(test_set):,} items -> {test_file.name}\n\n"
                f"SAMPLE TRAIN ITEM JSON:\n" + json.dumps(dict(zip(headers, train_set[0][:13])) if train_set else {}, indent=2)
            )

            self.preview_split_text.setPlainText(preview_msg)
            event_bus.publish(LogEvent(f"[+] Exported ML dataset splits to {settings.EXPORTS_DIR}", "SUCCESS"))

        except Exception as ex:
            self.preview_split_text.setPlainText(f"ML SPLIT EXPORT ERROR: {ex}")

    def _search_provenance_tracks(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            filter_text = self.search_prov_input.text().strip()
            if filter_text:
                q = "%" + filter_text + "%"
                cursor.execute("""
                    SELECT r.id, r.title, COALESCE(a.name, 'Unknown Artist')
                    FROM core_recordings r
                    LEFT JOIN core_artists a ON r.artist_id = a.id
                    WHERE r.id ILIKE %s OR r.title ILIKE %s OR a.name ILIKE %s
                    ORDER BY r.title ASC LIMIT 50
                """, (q, q, q))
            else:
                cursor.execute("""
                    SELECT r.id, r.title, COALESCE(a.name, 'Unknown Artist')
                    FROM core_recordings r
                    LEFT JOIN core_artists a ON r.artist_id = a.id
                    ORDER BY r.title ASC LIMIT 50
                """)

            rows = cursor.fetchall()
            self.provenance_tracks = rows
            
            self.combo_track_select.blockSignals(True)
            self.combo_track_select.clear()
            for rec_id, title, art_name in rows:
                self.combo_track_select.addItem(f"{title} — {art_name} [{rec_id[:8]}]", rec_id)
            self.combo_track_select.blockSignals(False)

            if self.combo_track_select.count() > 0:
                self._reload_provenance_timeline()
            else:
                self.table_active_decisions.setRowCount(0)
                self.table_fact_history.setRowCount(0)

        except Exception:
            pass

    def _reload_provenance_timeline(self) -> None:
        if self.combo_track_select.count() == 0:
            return

        target_rec_id = self.combo_track_select.currentData()
        if not target_rec_id:
            return

        try:
            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT title, artist_name, album_title, primary_genre, primary_subgenre, 
                       issue_release_date, original_release_date, isrc, musicbrainz_recording_id 
                FROM vw_recording_overview WHERE id = %s
            """, (target_rec_id,))
            rec_entity = cursor.fetchone()

            winning_values: Dict[str, str] = {}
            target_title = "Untitled Work"
            if rec_entity:
                target_title = rec_entity[0] or "Untitled Work"
                winning_values["title"] = str(rec_entity[0] or "").strip().casefold()
                winning_values["artist"] = str(rec_entity[1] or "").strip().casefold()
                winning_values["album"] = str(rec_entity[2] or "").strip().casefold()
                winning_values["genre"] = str(rec_entity[3] or "").strip().casefold()
                winning_values["subgenre"] = str(rec_entity[4] or "").strip().casefold()
                winning_values["release_date"] = str(rec_entity[5] or "").strip().casefold()
                winning_values["original_release_date"] = str(rec_entity[6] or "").strip().casefold()
                winning_values["isrc"] = str(rec_entity[7] or "").strip().casefold()
                winning_values["musicbrainz_recording_id"] = str(rec_entity[8] or "").strip().casefold()

            self.card_active_decisions.card_layout.itemAt(0).widget().setText(f"ACTIVE FIELD DECISIONS // {target_title.upper()} [{target_rec_id[:8]}...]")

            cursor.execute("""
                SELECT field_name, selected_value, old_value, reason, applied_at
                FROM meta_decisions WHERE entity_id = %s
                ORDER BY applied_at DESC
            """, (target_rec_id,))
            dec_rows = cursor.fetchall()

            self.table_active_decisions.setRowCount(len(dec_rows))
            for r_idx, (fn, sel_v, old_v, reas, app_at) in enumerate(dec_rows):
                item_fn = QTableWidgetItem(str(fn))
                item_fn.setForeground(QColor("#f59e0b"))

                self.table_active_decisions.setItem(r_idx, 0, item_fn)
                self.table_active_decisions.setItem(r_idx, 1, QTableWidgetItem(str(sel_v or "N/A")))
                self.table_active_decisions.setItem(r_idx, 2, QTableWidgetItem(str(old_v or "N/A")))
                self.table_active_decisions.setItem(r_idx, 3, QTableWidgetItem(str(reas or "")))
                self.table_active_decisions.setItem(r_idx, 4, QTableWidgetItem(str(app_at)[:19]))

            cursor.execute("""
                SELECT field_name, value, source_id, confidence, run_id, observed_at
                FROM meta_evidence WHERE entity_id = %s
                ORDER BY observed_at DESC
            """, (target_rec_id,))
            ev_rows = cursor.fetchall()
            self.table_fact_history.setRowCount(len(ev_rows))
            for r_idx, (fn, val, src, conf, r_id, obs_at) in enumerate(ev_rows):
                val_clean = str(val or "").strip().casefold()
                winning_target = winning_values.get(fn, "")
                is_winning = (winning_target == val_clean and val_clean != "")

                item_fn = QTableWidgetItem(str(fn))
                item_src = QTableWidgetItem(str(src))
                item_src.setForeground(QColor("#38bdf8"))

                item_status = QTableWidgetItem("WINNING FACT" if is_winning else "SUPERSEDED")
                if is_winning:
                    item_status.setForeground(QColor("#10b981"))
                else:
                    item_status.setForeground(QColor("#64748b"))

                self.table_fact_history.setItem(r_idx, 0, item_fn)
                self.table_fact_history.setItem(r_idx, 1, QTableWidgetItem(str(val or "")))
                self.table_fact_history.setItem(r_idx, 2, item_src)
                self.table_fact_history.setItem(r_idx, 3, QTableWidgetItem(f"{float(conf or 1.0) * 100:.0f}%"))
                self.table_fact_history.setItem(r_idx, 4, item_status)
                self.table_fact_history.setItem(r_idx, 5, QTableWidgetItem(str(r_id[:8]) + "..." if r_id else "N/A"))
                self.table_fact_history.setItem(r_idx, 6, QTableWidgetItem(str(obs_at)[:19]))

        except Exception:
            pass