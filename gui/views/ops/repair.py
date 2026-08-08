"""
Metadata Issue Repair Center View
Provides real-time open issues queue, issue code and severity filtering,
field-level lock controls, manual evidence overrides, and album sibling donor imputer.
Refactored to import centralized UI components.
"""

from typing import List, Dict, Any, Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QLineEdit, 
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QSplitter, 
    QAbstractItemView, QScrollArea, QComboBox
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor

from db import get_connection, db_transaction
from domain.models import Evidence, Decision
from domain.events import event_bus, LogEvent
from services.resolve import SymbolicInferenceEngine, ResolutionPersistenceAdapter, purge_invalid_isrc_evidence, get_current_field_value
from services.enrich import IntraAlbumEngine
from gui.widgets.badges import SeverityBadge, LockStateBadge, QualityBadge

# Centralized panel import replacing duplicate local definitions
from gui.widgets.common import InspectorCard

class RepairView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.selected_issue: Optional[Dict[str, Any]] = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        header_row = QHBoxLayout()
        header = QLabel("OPERATIONS // METADATA ISSUE REPAIR CENTER")
        header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 11pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(header)
        header_row.addStretch()

        btn_sweep_issues = QPushButton("[GENERATE LOW-QUALITY ISSUES]")
        btn_sweep_issues.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_sweep_issues.clicked.connect(self._run_issue_generator_sweep)
        header_row.addWidget(btn_sweep_issues)

        btn_purge_isrc = QPushButton("[PURGE INVALID ISRC EVIDENCE]")
        btn_purge_isrc.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_purge_isrc.clicked.connect(self._on_purge_isrc_clicked)
        header_row.addWidget(btn_purge_isrc)

        btn_refresh = QPushButton("[REFRESH QUEUE]")
        btn_refresh.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_refresh.clicked.connect(self.reload_issues_queue)
        header_row.addWidget(btn_refresh)

        main_layout.addLayout(header_row)

        queue_filter_row = QHBoxLayout()
        lbl_q = QLabel("OPEN METADATA ISSUES QUEUE (vw_open_issues_queue):")
        lbl_q.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a; font-weight: bold;")
        queue_filter_row.addWidget(lbl_q)
        queue_filter_row.addStretch()

        self.combo_code_filter = QComboBox()
        self.combo_code_filter.addItems(["[ALL ISSUE CODES]", "LOW_QUALITY_INDEX", "MISSING_MBID", "MISSING_ISRC", "INVALID_ISRC_SYNTAX", "DUPLICATE_IDENTIFIER_COLLISION"])
        self.combo_code_filter.setFixedWidth(230)
        self.combo_code_filter.currentIndexChanged.connect(self.reload_issues_queue)
        queue_filter_row.addWidget(self.combo_code_filter)

        self.combo_sev_filter = QComboBox()
        self.combo_sev_filter.addItems(["[ALL SEVERITIES]", "CRITICAL", "WARNING", "INFO"])
        self.combo_sev_filter.setFixedWidth(160)
        self.combo_sev_filter.currentIndexChanged.connect(self.reload_issues_queue)
        queue_filter_row.addWidget(self.combo_sev_filter)

        main_layout.addLayout(queue_filter_row)

        splitter = QSplitter(Qt.Orientation.Vertical)

        queue_container = QWidget()
        queue_layout = QVBoxLayout(queue_container)
        queue_layout.setContentsMargins(0, 0, 0, 0)

        self.table_issues = QTableWidget()
        self.table_issues.setColumnCount(6)
        self.table_issues.setHorizontalHeaderLabels(["Issue ID", "Entity ID", "Track Title", "Issue Code", "Severity", "Created UTC"])
        self.table_issues.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table_issues.setColumnWidth(0, 75)
        self.table_issues.setColumnWidth(1, 95)
        self.table_issues.setColumnWidth(3, 210)
        self.table_issues.setColumnWidth(4, 95)
        self.table_issues.setColumnWidth(5, 140)
        self.table_issues.verticalHeader().hide()
        self.table_issues.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_issues.itemSelectionChanged.connect(self._on_issue_selected)
        self.table_issues.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        queue_layout.addWidget(self.table_issues)
        splitter.addWidget(queue_container)

        workbench_splitter = QSplitter(Qt.Orientation.Horizontal)

        edit_panel = QFrame()
        edit_panel.setObjectName("WorkbenchCardAccent")
        edit_panel.setStyleSheet("QFrame#WorkbenchCardAccent { background-color: #16161a; border: 1px solid #222228; border-left: 3px solid #f59e0b; border-radius: 4px; padding: 10px; }")
        edit_layout = QVBoxLayout(edit_panel)

        header_edit_row = QHBoxLayout()
        self.lbl_edit_title = QLabel("SELECT AN ISSUE FROM QUEUE ABOVE")
        self.lbl_edit_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 10pt; font-weight: bold; color: #f59e0b;")
        header_edit_row.addWidget(self.lbl_edit_title, stretch=1)

        self.quality_badge = QualityBadge(0.0)
        header_edit_row.addWidget(self.quality_badge)
        edit_layout.addLayout(header_edit_row)

        self.input_title = QLineEdit()
        self.input_artist = QLineEdit()
        self.input_album = QLineEdit()
        self.input_genre = QLineEdit()
        self.input_subgenre = QLineEdit()
        self.input_isrc = QLineEdit()

        self.badge_title = LockStateBadge("AUTOMATIC")
        self.badge_title.state_clicked.connect(lambda st: self._update_lock("title", st))
        self.badge_artist = LockStateBadge("AUTOMATIC")
        self.badge_artist.state_clicked.connect(lambda st: self._update_lock("artist", st))

        form_grid = QVBoxLayout()
        for f_label, inp, badge in (("Title:", self.input_title, self.badge_title), ("Artist:", self.input_artist, self.badge_artist)):
            row = QHBoxLayout()
            lbl = QLabel(f_label)
            lbl.setFixedWidth(70)
            lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a;")
            row.addWidget(lbl)
            row.addWidget(inp, stretch=1)
            row.addWidget(badge)
            form_grid.addLayout(row)

        for f_label, inp in (("Album:", self.input_album), ("Genre:", self.input_genre), ("Subgenre:", self.input_subgenre), ("ISRC Code:", self.input_isrc)):
            row = QHBoxLayout()
            lbl = QLabel(f_label)
            lbl.setFixedWidth(70)
            lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a;")
            row.addWidget(lbl)
            row.addWidget(inp, stretch=1)
            form_grid.addLayout(row)

        edit_layout.addLayout(form_grid)

        btn_edit_row = QHBoxLayout()
        self.btn_apply_override = QPushButton("[APPLY MANUAL OVERRIDE]")
        self.btn_apply_override.setObjectName("AmberPrimaryBtn")
        self.btn_apply_override.clicked.connect(self._apply_manual_override)

        self.btn_resolve_issue = QPushButton("[MARK ISSUE RESOLVED]")
        self.btn_resolve_issue.clicked.connect(self._mark_issue_resolved)

        btn_edit_row.addWidget(self.btn_apply_override)
        btn_edit_row.addWidget(self.btn_resolve_issue)
        edit_layout.addLayout(btn_edit_row)

        workbench_splitter.addWidget(edit_panel)

        sibling_panel = QFrame()
        sibling_panel.setObjectName("WorkbenchCardAccent")
        sibling_panel.setStyleSheet("QFrame#WorkbenchCardAccent { background-color: #16161a; border: 1px solid #222228; border-left: 3px solid #38bdf8; border-radius: 4px; padding: 10px; }")
        sibling_layout = QVBoxLayout(sibling_panel)

        lbl_sib_title = QLabel("ALBUM SIBLING CONTEXT & EVIDENCE IMPUTER")
        lbl_sib_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 9.5pt; font-weight: bold; color: #38bdf8;")
        sibling_layout.addWidget(lbl_sib_title)

        self.table_siblings = QTableWidget()
        self.table_siblings.setColumnCount(4)
        self.table_siblings.setHorizontalHeaderLabels(["Trk #", "Sibling Title", "Artist", "Quality"])
        self.table_siblings.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_siblings.setColumnWidth(0, 45)
        self.table_siblings.setColumnWidth(2, 120)
        self.table_siblings.setColumnWidth(3, 65)
        self.table_siblings.verticalHeader().hide()
        self.table_siblings.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', monospace; font-size: 8pt;
            }
            QTableWidget::item { padding: 3px; color: #cbd5e1; }
        """)
        sibling_layout.addWidget(self.table_siblings)

        self.btn_impute_donor = QPushButton("[COPY EVIDENCE FROM TOP DONOR TRACK]")
        self.btn_impute_donor.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold; color: #38bdf8; border: 1px solid #38bdf8;")
        self.btn_impute_donor.clicked.connect(self._impute_from_donor)
        sibling_layout.addWidget(self.btn_impute_donor)

        workbench_splitter.addWidget(sibling_panel)
        workbench_splitter.setSizes([600, 500])

        splitter.addWidget(workbench_splitter)
        splitter.setSizes([260, 480])

        main_layout.addWidget(splitter, stretch=1)

        self.reload_issues_queue()

    def reload_issues_queue(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            code_filter = self.combo_code_filter.currentText()
            sev_filter = self.combo_sev_filter.currentText()

            where_clauses = []
            params = []

            if code_filter != "[ALL ISSUE CODES]":
                where_clauses.append("issue_code = %s")
                params.append(code_filter)

            if sev_filter != "[ALL SEVERITIES]":
                where_clauses.append("severity = %s")
                params.append(sev_filter)

            sql = "SELECT id, entity_id, recording_title, issue_code, severity, created_at FROM vw_open_issues_queue"
            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)
            sql += " ORDER BY created_at DESC"

            cursor.execute(sql, params)
            rows = cursor.fetchall()

            self.table_issues.setRowCount(len(rows))
            for r_idx, (i_id, e_id, title, code, sev, created) in enumerate(rows):
                self.table_issues.setItem(r_idx, 0, QTableWidgetItem(f"#{i_id}"))
                self.table_issues.setItem(r_idx, 1, QTableWidgetItem(e_id[:8] + "..."))
                self.table_issues.setItem(r_idx, 2, QTableWidgetItem(title))
                
                item_code = QTableWidgetItem(code)
                item_code.setForeground(QColor("#f59e0b"))
                self.table_issues.setItem(r_idx, 3, item_code)

                item_sev = QTableWidgetItem(sev)
                if sev == "CRITICAL": item_sev.setForeground(QColor("#ef4444"))
                elif sev == "WARNING": item_sev.setForeground(QColor("#f59e0b"))
                else: item_sev.setForeground(QColor("#38bdf8"))
                self.table_issues.setItem(r_idx, 4, item_sev)

                self.table_issues.setItem(r_idx, 5, QTableWidgetItem(str(created)[:16]))

            if len(rows) > 0 and self.table_issues.currentRow() < 0:
                self.table_issues.selectRow(0)

        except Exception:
            pass

    def _on_issue_selected(self) -> None:
        selected = self.table_issues.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        
        with db_transaction() as cursor:
            code_filter = self.combo_code_filter.currentText()
            sev_filter = self.combo_sev_filter.currentText()
            where_clauses, params = [], []
            if code_filter != "[ALL ISSUE CODES]":
                where_clauses.append("issue_code = %s")
                params.append(code_filter)
            if sev_filter != "[ALL SEVERITIES]":
                where_clauses.append("severity = %s")
                params.append(sev_filter)

            sql = "SELECT id, entity_id, recording_title, issue_code, severity FROM vw_open_issues_queue"
            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)
            sql += " ORDER BY created_at DESC LIMIT 1 OFFSET %s"
            params.append(row)

            cursor.execute(sql, params)
            r = cursor.fetchone()
            if not r:
                return

            self.selected_issue = {"issue_id": r[0], "entity_id": r[1], "title": r[2], "issue_code": r[3], "severity": r[4]}
            self.lbl_edit_title.setText(f"REPAIR ENTITY // {r[1][:8]}... [{r[3]}]")

            cursor.execute("""
                SELECT r.title, COALESCE(a.name, ''), COALESCE(alb.title, ''), r.isrc, r.album_id,
                       COALESCE(r.genre, ''), COALESCE(r.subgenre, ''), COALESCE(v.quality_score, 0.0)
                FROM core_recordings r
                LEFT JOIN core_artists a ON r.artist_id = a.id
                LEFT JOIN core_albums alb ON r.album_id = alb.id
                LEFT JOIN meta_validation v ON r.id = v.recording_id
                WHERE r.id = %s
            """, (r[1],))
            rec_data = cursor.fetchone()

            if rec_data:
                self.input_title.setText(rec_data[0] or "")
                self.input_artist.setText(rec_data[1] or "")
                self.input_album.setText(rec_data[2] or "")
                self.input_isrc.setText(rec_data[3] or "")
                self.input_genre.setText(rec_data[5] or "")
                self.input_subgenre.setText(rec_data[6] or "")
                self.quality_badge.set_score(float(rec_data[7]))

                album_id = rec_data[4]
                if album_id:
                    self._load_siblings(album_id, r[1])

            cursor.execute("SELECT field_name, lock_state FROM meta_locks WHERE entity_id = %s", (r[1],))
            locks = {l[0]: l[1] for l in cursor.fetchall()}
            self.badge_title.set_state(locks.get("title", "AUTOMATIC"))
            self.badge_artist.set_state(locks.get("artist", "AUTOMATIC"))

    def _load_siblings(self, album_id: str, current_rec_id: str) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT r.track_number, r.title, COALESCE(a.name, ''), COALESCE(v.quality_score, 0.0)
                FROM core_recordings r
                LEFT JOIN core_artists a ON r.artist_id = a.id
                LEFT JOIN meta_validation v ON r.id = v.recording_id
                WHERE r.album_id = %s AND r.id != %s
                ORDER BY r.track_number ASC
            """, (album_id, current_rec_id))
            rows = cursor.fetchall()

            self.table_siblings.setRowCount(len(rows))
            for r_idx, (t_no, title, art, q) in enumerate(rows):
                item_q = QTableWidgetItem(f"{q * 100:.0f}%")
                if q >= 1.0: item_q.setForeground(QColor("#10b981"))
                else: item_q.setForeground(QColor("#f59e0b"))

                self.table_siblings.setItem(r_idx, 0, QTableWidgetItem(str(t_no) if t_no else "-"))
                self.table_siblings.setItem(r_idx, 1, QTableWidgetItem(str(title or "Untitled")))
                self.table_siblings.setItem(r_idx, 2, QTableWidgetItem(str(art)))
                self.table_siblings.setItem(r_idx, 3, item_q)
        except Exception:
            pass

    def _apply_manual_override(self) -> None:
        if not self.selected_issue:
            return
        e_id = self.selected_issue["entity_id"]
        run_id = f"manual-repair-{e_id[:6]}"

        ev_list = [
            Evidence(entity_id=e_id, field_name="title", value=self.input_title.text().strip(), evidence_class="LOCAL", source_id="SRC_USER", confidence=1.00),
            Evidence(entity_id=e_id, field_name="artist", value=self.input_artist.text().strip(), evidence_class="LOCAL", source_id="SRC_USER", confidence=1.00),
            Evidence(entity_id=e_id, field_name="album", value=self.input_album.text().strip(), evidence_class="LOCAL", source_id="SRC_USER", confidence=1.00),
            Evidence(entity_id=e_id, field_name="isrc", value=self.input_isrc.text().strip(), evidence_class="LOCAL", source_id="SRC_USER", confidence=1.00),
            Evidence(entity_id=e_id, field_name="genre", value=self.input_genre.text().strip(), evidence_class="LOCAL", source_id="SRC_USER", confidence=1.00),
            Evidence(entity_id=e_id, field_name="subgenre", value=self.input_subgenre.text().strip(), evidence_class="LOCAL", source_id="SRC_USER", confidence=1.00),
        ]

        with db_transaction() as tx:
            for ev in ev_list:
                if ev.value:
                    tx.execute("""
                        INSERT INTO meta_evidence (
                            entity_id, field_name, value, evidence_class, source_id, run_id, 
                            confidence, origin_type, raw_value
                        ) VALUES (%s, %s, %s, 'LOCAL', 'SRC_USER', %s, 1.00, 'RESOLVER', %s)
                        ON CONFLICT DO NOTHING
                    """, (ev.entity_id, ev.field_name, ev.value, run_id, ev.value))

                    curr_val = get_current_field_value(tx, e_id, ev.field_name)
                    dec = SymbolicInferenceEngine.resolve_field(e_id, ev.field_name, curr_val, [ev], run_id)
                    ResolutionPersistenceAdapter.apply_decision(dec)

        event_bus.publish(LogEvent(f"[+] Applied manual user overrides to recording {e_id[:8]}...", "SUCCESS"))
        self.reload_issues_queue()

    def _run_issue_generator_sweep(self) -> None:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT r.id, COALESCE(v.quality_score, 0.0), r.musicbrainz_recording_id, r.isrc
            FROM core_recordings r
            LEFT JOIN meta_validation v ON r.id = v.recording_id
            WHERE v.quality_score < 0.80 OR r.musicbrainz_recording_id IS NULL OR r.isrc IS NULL
        """)
        rows = cursor.fetchall()

        issues_created = 0
        with db_transaction() as tx:
            for rec_id, q_score, mbid, isrc_val in rows:
                if q_score < 0.50:
                    tx.execute("""
                        INSERT INTO meta_issues (entity_id, issue_code, severity, status)
                        VALUES (%s, 'LOW_QUALITY_INDEX', 'CRITICAL', 'OPEN')
                        ON CONFLICT DO NOTHING
                    """, (rec_id,))
                    issues_created += 1

                if not mbid:
                    tx.execute("""
                        INSERT INTO meta_issues (entity_id, issue_code, severity, status)
                        VALUES (%s, 'MISSING_MBID', 'WARNING', 'OPEN')
                        ON CONFLICT DO NOTHING
                    """, (rec_id,))
                    issues_created += 1

                if not isrc_val:
                    tx.execute("""
                        INSERT INTO meta_issues (entity_id, issue_code, severity, status)
                        VALUES (%s, 'MISSING_ISRC', 'WARNING', 'OPEN')
                        ON CONFLICT DO NOTHING
                    """, (rec_id,))
                    issues_created += 1

        event_bus.publish(LogEvent(f"[+] Automated issue sweep completed: Generated {issues_created} open issue candidate(s).", "SUCCESS"))
        self.reload_issues_queue()

    def _mark_issue_resolved(self) -> None:
        if not self.selected_issue:
            return
        i_id = self.selected_issue["issue_id"]
        with db_transaction() as tx:
            tx.execute("UPDATE meta_issues SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP WHERE id = %s", (i_id,))
        event_bus.publish(LogEvent(f"[+] Issue #{i_id} marked resolved.", "SUCCESS"))
        self.reload_issues_queue()

    def _on_purge_isrc_clicked(self) -> None:
        count = purge_invalid_isrc_evidence()
        event_bus.publish(LogEvent(f"[+] Purged {count} invalid ISRC evidence rows.", "SUCCESS"))
        self.reload_issues_queue()

    def _impute_from_donor(self) -> None:
        if not self.selected_issue:
            return
        e_id = self.selected_issue["entity_id"]
        stats = IntraAlbumEngine.propagate_from_donor(e_id, f"impute-{e_id[:6]}")
        event_bus.publish(LogEvent(f"[+] Donor evidence imputed across {stats.get('siblings_updated', 0)} sibling track(s).", "SUCCESS"))

    def _update_lock(self, field_name: str, lock_state: str) -> None:
        if not self.selected_issue:
            return
        e_id = self.selected_issue["entity_id"]
        with db_transaction() as tx:
            tx.execute("""
                INSERT INTO meta_locks (entity_id, field_name, lock_state)
                VALUES (%s, %s, %s)
                ON CONFLICT(entity_id, field_name) DO UPDATE SET lock_state = EXCLUDED.lock_state
            """, (e_id, field_name, lock_state))