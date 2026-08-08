"""
Job Audit Inspector Dialog Panel
Provides rollback automation and raw compressed payload viewing.
Refactored to import centralized UI components.
"""

import json
import gzip
from typing import List, Dict, Any, Optional, Tuple
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, 
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, 
    QHeaderView, QSplitter, QAbstractItemView, QScrollArea, 
    QTabWidget, QPlainTextEdit, QMessageBox
)
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QFont

from config.settings import settings
from db import get_connection, db_transaction
from domain.events import event_bus, LogEvent
from services.resolve import ResolutionPersistenceAdapter, ALLOWED_RECORDING_COLUMNS
from gui.widgets.metric import MetricCard

# Centralized imports replacing locally duplicated widgets and layout helpers
from gui.widgets.common import create_tab_scroll_area, AuditCard

class JobAuditInspectorDialog(QDialog):
    def __init__(self, run_id: str, parent=None) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.setWindowTitle(f"JOB AUDIT INSPECTOR // RUN ID [{run_id[:8]}...]")
        self.resize(1100, 720)
        self.setStyleSheet("""
            QDialog { background-color: #101012; color: #e2e8f0; }
            QWidget { font-family: 'Segoe UI', sans-serif; font-size: 9pt; }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        header_row = QHBoxLayout()
        lbl_header = QLabel(f"EXECUTION RUN INSPECTOR // ID [{run_id}]")
        lbl_header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 11pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(lbl_header)
        header_row.addStretch()

        btn_close = QPushButton("[CLOSE AUDITOR]")
        btn_close.setFixedWidth(140)
        btn_close.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_close.clicked.connect(self.accept)
        header_row.addWidget(btn_close)

        main_layout.addLayout(header_row)

        kpi_row = QHBoxLayout()
        self.card_facts_cnt = MetricCard("FACTS GATHERED", "--", "meta_evidence rows", "#10b981")
        self.card_decisions_cnt = MetricCard("DECISIONS APPLIED", "--", "meta_decisions rows", "#38bdf8")
        self.card_payloads_cnt = MetricCard("RAW BLOB PAYLOADS", "--", "sys_payloads items", "#f59e0b")
        self.card_version = MetricCard("ENGINE VERSION", "5.0.0", "Parser & Resolver")

        kpi_row.addWidget(self.card_facts_cnt)
        kpi_row.addWidget(self.card_decisions_cnt)
        kpi_row.addWidget(self.card_payloads_cnt)
        kpi_row.addWidget(self.card_version)
        main_layout.addLayout(kpi_row)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=1)

        self._build_tab_diff()
        self._build_tab_payloads()
        self._build_tab_rollback()

        self._reload_dialog_data()

    def _build_tab_diff(self) -> None:
        scroll, layout = create_tab_scroll_area()

        splitter = QSplitter(Qt.Orientation.Horizontal)

        card_ev = AuditCard("Evidence Facts Inserted During Run")
        self.table_evidence = QTableWidget()
        self.table_evidence.setColumnCount(4)
        self.table_evidence.setHorizontalHeaderLabels(["Field Name", "Fact Value", "Source Provider", "Confidence"])
        self.table_evidence.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_evidence.setColumnWidth(0, 95)
        self.table_evidence.setColumnWidth(2, 130)
        self.table_evidence.setColumnWidth(3, 75)
        self.table_evidence.verticalHeader().hide()
        self.table_evidence.setMinimumHeight(350)
        self.table_evidence.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_ev.add_row(self.table_evidence)
        splitter.addWidget(card_ev)

        card_dec = AuditCard("Field Decision Overrides Applied During Run")
        self.table_decisions = QTableWidget()
        self.table_decisions.setColumnCount(4)
        self.table_decisions.setHorizontalHeaderLabels(["Field Name", "Winning Value", "Old Value", "Resolver Reason"])
        self.table_decisions.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_decisions.setColumnWidth(0, 95)
        self.table_decisions.setColumnWidth(2, 110)
        self.table_decisions.setColumnWidth(3, 200)
        self.table_decisions.verticalHeader().hide()
        self.table_decisions.setMinimumHeight(350)
        self.table_decisions.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_dec.add_row(self.table_decisions)
        splitter.addWidget(card_dec)

        splitter.setSizes([500, 500])
        layout.addWidget(splitter)

        self.tabs.addTab(scroll, "[1] EVIDENCE & DECISIONS DIFF")

    def _build_tab_payloads(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_payloads = AuditCard("Decompressed API Response JSON Payload Archives (sys_payloads)")
        
        search_row = QHBoxLayout()
        lbl_p = QLabel("FILTER PAYLOADS:")
        lbl_p.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #828a9a; font-weight: bold;")
        self.search_payload_input = QLineEdit()
        self.search_payload_input.setPlaceholderText("Filter raw payload JSON text...")
        self.search_payload_input.textChanged.connect(self._reload_payloads_view)
        search_row.addWidget(lbl_p)
        search_row.addWidget(self.search_payload_input, stretch=1)
        card_payloads.card_layout.addLayout(search_row)

        self.preview_payload_text = QPlainTextEdit()
        self.preview_payload_text.setReadOnly(True)
        self.preview_payload_text.setMinimumHeight(360)
        self.preview_payload_text.setFont(QFont("Consolas", 8.5))
        self.preview_payload_text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0c0c0e; color: #38bdf8; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace;
            }
        """)
        card_payloads.add_row(self.preview_payload_text)
        layout.addWidget(card_payloads)

        self.tabs.addTab(scroll, "[2] RAW API PAYLOAD BLOBS")

    def _build_tab_rollback(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_rollback = AuditCard("Atomic Single-Run Rollback Engine")
        
        lbl_warn = QLabel(
            "ROLLBACK WARNING: Executing a rollback will undo all field decisions applied during this specific run.\n"
            "Previous metadata values will be safely restored. Fields locked with MANUAL status will be preserved."
        )
        lbl_warn.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; color: #ef4444;")
        lbl_warn.setWordWrap(True)
        card_rollback.add_row(lbl_warn)

        btn_rollback = QPushButton("[ROLLBACK ALL FIELD DECISIONS FOR THIS RUN]")
        btn_rollback.setFixedHeight(32)
        btn_rollback.setStyleSheet("""
            QPushButton {
                background-color: #450a0a; color: #ef4444; border: 1px solid #ef4444;
                font-family: 'Consolas', monospace; font-weight: bold; padding: 4px 12px;
            }
            QPushButton:hover { background-color: #ef4444; color: #101012; }
        """)
        btn_rollback.clicked.connect(self._exec_single_run_rollback)
        card_rollback.add_row(btn_rollback)
        layout.addWidget(card_rollback)

        card_llm = AuditCard("AI-Ready Diagnostic Markdown Report Generator (~5k Tokens)")
        
        btn_llm = QPushButton("[GENERATE AI-READY DIAGNOSTIC REPORT]")
        btn_llm.setObjectName("AmberPrimaryBtn")
        btn_llm.setFixedHeight(30)
        btn_llm.clicked.connect(self._generate_llm_report)
        card_llm.add_row(btn_llm)

        self.preview_llm_text = QPlainTextEdit()
        self.preview_llm_text.setReadOnly(True)
        self.preview_llm_text.setMinimumHeight(280)
        self.preview_llm_text.setFont(QFont("Consolas", 8.5))
        self.preview_llm_text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0c0c0e; color: #f59e0b; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace;
            }
        """)
        card_llm.add_row(self.preview_llm_text)
        layout.addWidget(card_llm)

        self.tabs.addTab(scroll, "[3] ROLLBACK ENGINE & LLM REPORT")

    def _reload_dialog_data(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM meta_evidence WHERE run_id = %s", (self.run_id,))
            facts_cnt = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM meta_decisions WHERE run_id = %s", (self.run_id,))
            dec_cnt = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM sys_payloads")
            payloads_cnt = cursor.fetchone()[0] or 0

            self.card_facts_cnt.set_value(f"{facts_cnt:,}", "Inserted in run")
            self.card_decisions_cnt.set_value(f"{dec_cnt:,}", "Overrides applied")
            self.card_payloads_cnt.set_value(f"{payloads_cnt:,}", "Archive blobs")

            cursor.execute("""
                SELECT field_name, value, source_id, confidence 
                FROM meta_evidence WHERE run_id = %s ORDER BY id DESC LIMIT 200
            """, (self.run_id,))
            ev_rows = cursor.fetchall()
            self.table_evidence.setRowCount(len(ev_rows))
            for r_idx, (fn, val, src, conf) in enumerate(ev_rows):
                item_fn = QTableWidgetItem(str(fn))
                item_src = QTableWidgetItem(str(src))
                item_src.setForeground(QColor("#38bdf8"))

                self.table_evidence.setItem(r_idx, 0, item_fn)
                self.table_evidence.setItem(r_idx, 1, QTableWidgetItem(str(val or "")))
                self.table_evidence.setItem(r_idx, 2, item_src)
                self.table_evidence.setItem(r_idx, 3, QTableWidgetItem(f"{float(conf or 1.0)*100:.0f}%"))

            cursor.execute("""
                SELECT field_name, selected_value, old_value, reason 
                FROM meta_decisions WHERE run_id = %s ORDER BY id DESC LIMIT 200
            """, (self.run_id,))
            dec_rows = cursor.fetchall()
            self.table_decisions.setRowCount(len(dec_rows))
            for r_idx, (fn, sel_v, old_v, reas) in enumerate(dec_rows):
                item_fn = QTableWidgetItem(str(fn))
                item_fn.setForeground(QColor("#f59e0b"))

                self.table_decisions.setItem(r_idx, 0, item_fn)
                self.table_decisions.setItem(r_idx, 1, QTableWidgetItem(str(sel_v or "")))
                self.table_decisions.setItem(r_idx, 2, QTableWidgetItem(str(old_v or "N/A")))
                self.table_decisions.setItem(r_idx, 3, QTableWidgetItem(str(reas or "")))

            self._reload_payloads_view()

        except Exception:
            pass

    def _reload_payloads_view(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT content_hash, payload_type, compressed_data FROM sys_payloads LIMIT 5")
            p_rows = cursor.fetchall()

            preview_lines = []
            filter_text = self.search_payload_input.text().strip().lower()

            for c_hash, p_type, c_data in p_rows:
                try:
                    raw_json = gzip.decompress(c_data).decode("utf-8")
                    parsed_obj = json.loads(raw_json)
                    pretty_str = json.dumps(parsed_obj, indent=2)

                    if filter_text and filter_text not in pretty_str.lower() and filter_text not in p_type.lower():
                        continue

                    preview_lines.append(f"// PAYLOAD [{p_type}] HASH: {c_hash[:12]}...")
                    preview_lines.append(pretty_str[:800] + "\n...")
                    preview_lines.append("-" * 60)
                except Exception:
                    pass

            if not preview_lines:
                preview_lines = ["No matching decompressed JSON payload blobs found."]

            self.preview_payload_text.setPlainText("\n".join(preview_lines))

        except Exception:
            pass

    def _exec_single_run_rollback(self) -> None:
        reply = QMessageBox.warning(
            self, "CONFIRM SINGLE-RUN ROLLBACK",
            f"Are you sure you want to rollback all field decisions applied during run {self.run_id[:8]}...?\n\nThis will safely restore previous field values while respecting user MANUAL locks.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT entity_id, field_name, old_value 
                    FROM meta_decisions WHERE run_id = %s
                """, (self.run_id,))
                dec_rows = cursor.fetchall()

                rolled_back_count = 0
                with db_transaction() as tx:
                    for e_id, f_name, old_v in dec_rows:
                        tx.execute("SELECT lock_state FROM meta_locks WHERE entity_id = %s AND field_name = %s", (e_id, f_name))
                        l_row = tx.fetchone()
                        if l_row and l_row[0] == 'MANUAL':
                            continue

                        if f_name in ALLOWED_RECORDING_COLUMNS:
                            tx.execute(f"UPDATE core_recordings SET {f_name} = %s WHERE id = %s", (old_v, e_id))
                            ResolutionPersistenceAdapter.recalculate_quality_score(tx, e_id)
                            rolled_back_count += 1

                    tx.execute("DELETE FROM meta_decisions WHERE run_id = %s", (self.run_id,))

                event_bus.publish(LogEvent(f"[+] Successfully rolled back {rolled_back_count} decision(s) for run {self.run_id[:8]}...", "SUCCESS"))
                self._reload_dialog_data()

            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Rollback error: {ex}", "ERROR"))

    def _generate_llm_report(self) -> None:
        try:
            settings.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            export_file = settings.EXPORTS_DIR / f"run_audit_{self.run_id[:8]}.md"

            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT run_id, parser_version, config_hash, started_at, finished_at FROM sys_runs WHERE run_id = %s", (self.run_id,))
            run_meta = cursor.fetchone()

            cursor.execute("SELECT field_name, selected_value, old_value, reason FROM meta_decisions WHERE run_id = %s LIMIT 30", (self.run_id,))
            dec_samples = cursor.fetchall()

            md_lines = [
                f"# MDMS AUDIT REPORT // RUN ID [{self.run_id}]",
                "--------------------------------------------------",
                f"- **Engine Version** : {run_meta[1] if run_meta else '5.0.0'}",
                f"- **Config Hash**    : {run_meta[2] if run_meta else 'N/A'}",
                f"- **Started UTC**    : {run_meta[3] if run_meta else 'N/A'}",
                f"- **Finished UTC**   : {run_meta[4] if run_meta else 'Running'}",
                "",
                "## SAMPLE FIELD DECISIONS APPLIED",
            ]

            for fn, sel_v, old_v, reas in dec_samples:
                md_lines.append(f"- **Field `{fn}`**: Selected '{sel_v}' (was '{old_v}') | Reason: {reas}")

            md_out = "\n".join(md_lines)
            with open(export_file, "w", encoding="utf-8") as f:
                f.write(md_out)

            self.preview_llm_text.setPlainText(f"LLM MARKDOWN REPORT GENERATED: {export_file}\n\n" + md_out)
            event_bus.publish(LogEvent(f"[+] AI-ready LLM diagnostic report saved to {export_file}", "SUCCESS"))

        except Exception as ex:
            self.preview_llm_text.setPlainText(f"REPORT GENERATION ERROR: {ex}")