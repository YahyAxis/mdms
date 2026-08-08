"""
Pipeline Execution & Control Rack View
Renders compact execution cards, 5 telemetry progress bars, live RingBuffer console,
sys_runs history, force re-ingest, and database wipe controls with rich per-track live telemetry.
"""

import time
import threading
from typing import List, Optional, Any
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QMessageBox
)
from PySide6.QtCore import Slot, Qt, QTimer
from PySide6.QtGui import QColor

try:
    import psutil
    TOTAL_SYSTEM_RAM_GB = psutil.virtual_memory().total / (1024 ** 3)
except Exception:
    TOTAL_SYSTEM_RAM_GB = 16.0

from config.settings import settings
from domain.models import TelemetryEvent
from domain.events import LogEvent, IngestionFinishedEvent, event_bus, signals
from db.core import get_connection, db_transaction, wipe_database_clean
from services.ingest import StageAIngestionEngine, WatchdogService
from services.enrich import EnrichmentEngine
from services.tax import TaxonomyService
from gui.widgets.console import RingBufferConsole
from gui.widgets.metric import MetricCard

class CompactBarMeter(QWidget):
    def __init__(self, title: str, color: str = "#f59e0b", parent=None) -> None:
        super().__init__(parent)
        self.base_color = color
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(6)

        self.lbl_title = QLabel(title.upper())
        self.lbl_title.setFixedWidth(110)
        self.lbl_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a; font-weight: bold;")

        self.bar = QProgressBar()
        self.bar.setFixedHeight(12)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #121216; border: 1px solid #222228; border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: {color}; border-radius: 1px;
            }}
        """)

        self.lbl_val = QLabel("0%")
        self.lbl_val.setFixedWidth(65)
        self.lbl_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_val.setStyleSheet(f"font-family: 'Consolas', monospace; font-size: 8.5pt; color: {color}; font-weight: bold;")

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.bar, stretch=1)
        layout.addWidget(self.lbl_val)

    def set_progress(self, pct: int, text_override: Optional[str] = None, alert_high: bool = False) -> None:
        val = max(0, min(100, int(pct)))
        self.bar.setValue(val)
        self.lbl_val.setText(text_override if text_override else f"{val}%")

        if alert_high and val >= 85:
            color = "#ef4444"
        elif alert_high and val >= 70:
            color = "#f59e0b"
        else:
            color = self.base_color

        self.bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #121216; border: 1px solid #222228; border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: {color}; border-radius: 1px;
            }}
        """)
        self.lbl_val.setStyleSheet(f"font-family: 'Consolas', monospace; font-size: 8.5pt; color: {color}; font-weight: bold;")

class PipelineView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.watchdog_service = WatchdogService()
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # Header Title Bar & Maintenance Controls
        header_row = QHBoxLayout()
        header = QLabel("OPERATIONS // PIPELINE EXECUTION & MAINTENANCE RACK")
        header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 10.5pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(header)
        header_row.addStretch()

        self.btn_reingest = QPushButton("[FORCE RE-INGEST LIBRARY]")
        self.btn_reingest.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        self.btn_reingest.clicked.connect(self._on_force_reingest_clicked)
        header_row.addWidget(self.btn_reingest)

        self.btn_wipe = QPushButton("[WIPE DATABASE CLEAN]")
        self.btn_wipe.setStyleSheet("""
            QPushButton {
                background-color: #450a0a; color: #ef4444; border: 1px solid #ef4444;
                font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold; padding: 3px 10px;
            }
            QPushButton:hover { background-color: #ef4444; color: #101012; }
        """)
        self.btn_wipe.clicked.connect(self._on_wipe_database_clicked)
        header_row.addWidget(self.btn_wipe)

        main_layout.addLayout(header_row)

        # Compact Execution Cards Row
        cards_row = QHBoxLayout()

        # Stage A Card
        card_ingest = QFrame()
        card_ingest.setObjectName("WorkbenchCardAccent")
        card_ingest.setStyleSheet("QFrame#WorkbenchCardAccent { background-color: #16161a; border: 1px solid #222228; border-left: 3px solid #f59e0b; border-radius: 4px; padding: 8px; }")
        ingest_layout = QVBoxLayout(card_ingest)
        ingest_layout.setSpacing(4)

        lbl_a_title = QLabel("STAGE A: LOCAL FILE INGESTION")
        lbl_a_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; font-weight: bold; color: #e2e8f0;")
        lbl_a_desc = QLabel("Scans data/input, triple hashes, extracts Chromaprint fingerprints & embedded ID3/Vorbis tags.")
        lbl_a_desc.setStyleSheet("color: #828a9a; font-size: 8pt;")
        lbl_a_desc.setWordWrap(True)

        btn_row_a = QHBoxLayout()
        self.btn_run_ingest = QPushButton("[RUN INGESTION]")
        self.btn_run_ingest.setObjectName("AmberPrimaryBtn")
        self.btn_run_ingest.setFixedHeight(26)
        self.btn_run_ingest.clicked.connect(self._start_ingestion_thread)

        self.btn_watchdog = QPushButton("[TOGGLE WATCHDOG]")
        self.btn_watchdog.setCheckable(True)
        self.btn_watchdog.setFixedHeight(26)
        self.btn_watchdog.clicked.connect(self._toggle_watchdog)

        btn_row_a.addWidget(self.btn_run_ingest)
        btn_row_a.addWidget(self.btn_watchdog)

        ingest_layout.addWidget(lbl_a_title)
        ingest_layout.addWidget(lbl_a_desc)
        ingest_layout.addLayout(btn_row_a)

        # Stage B Card
        card_enrich = QFrame()
        card_enrich.setObjectName("WorkbenchCardAccent")
        card_enrich.setStyleSheet("QFrame#WorkbenchCardAccent { background-color: #16161a; border: 1px solid #222228; border-left: 3px solid #f59e0b; border-radius: 4px; padding: 8px; }")
        enrich_layout = QVBoxLayout(card_enrich)
        enrich_layout.setSpacing(4)

        lbl_b_title = QLabel("STAGE B: CANONICAL ENRICHMENT")
        lbl_b_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; font-weight: bold; color: #e2e8f0;")
        lbl_b_desc = QLabel("Executes MusicBrainz, AcoustID, Deezer, Discogs, Last.fm & Wikidata SPARQL cascade.")
        lbl_b_desc.setStyleSheet("color: #828a9a; font-size: 8pt;")
        lbl_b_desc.setWordWrap(True)

        btn_row_b = QHBoxLayout()
        self.btn_run_enrich_pending = QPushButton("[ENRICH PENDING]")
        self.btn_run_enrich_pending.setObjectName("AmberPrimaryBtn")
        self.btn_run_enrich_pending.setFixedHeight(26)
        self.btn_run_enrich_pending.clicked.connect(lambda: self._start_enrichment_thread("1"))

        self.btn_run_enrich_full = QPushButton("[RE-ENRICH ALL]")
        self.btn_run_enrich_full.setFixedHeight(26)
        self.btn_run_enrich_full.clicked.connect(lambda: self._start_enrichment_thread("2"))

        btn_row_b.addWidget(self.btn_run_enrich_pending)
        btn_row_b.addWidget(self.btn_run_enrich_full)

        enrich_layout.addWidget(lbl_b_title)
        enrich_layout.addWidget(lbl_b_desc)
        enrich_layout.addLayout(btn_row_b)

        # Stage C Card
        card_tax = QFrame()
        card_tax.setObjectName("WorkbenchCardAccent")
        card_tax.setStyleSheet("QFrame#WorkbenchCardAccent { background-color: #16161a; border: 1px solid #222228; border-left: 3px solid #38bdf8; border-radius: 4px; padding: 8px; }")
        tax_layout = QVBoxLayout(card_tax)
        tax_layout.setSpacing(4)

        lbl_c_title = QLabel("STAGE C: TAXONOMY RE-CLASSIFICATION")
        lbl_c_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8.5pt; font-weight: bold; color: #e2e8f0;")
        lbl_c_desc = QLabel("Re-evaluates DAG ontology, alias mappings, and MiniLM semantic embeddings across library.")
        lbl_c_desc.setStyleSheet("color: #828a9a; font-size: 8pt;")
        lbl_c_desc.setWordWrap(True)

        btn_row_c = QHBoxLayout()
        self.btn_run_tax = QPushButton("[RE-CLASSIFY TAXONOMY]")
        self.btn_run_tax.setFixedHeight(26)
        self.btn_run_tax.clicked.connect(self._start_taxonomy_thread)
        btn_row_c.addWidget(self.btn_run_tax)

        tax_layout.addWidget(lbl_c_title)
        tax_layout.addWidget(lbl_c_desc)
        tax_layout.addLayout(btn_row_c)

        cards_row.addWidget(card_ingest)
        cards_row.addWidget(card_enrich)
        cards_row.addWidget(card_tax)
        main_layout.addLayout(cards_row)

        # Compact Metric Strip
        metric_strip = QHBoxLayout()
        self.card_workers = MetricCard("WORKERS", str(settings.MAX_WORKER_PROCESSES), "Thread pool")
        self.card_tps = MetricCard("THROUGHPUT", "0.0 trk/s", "Processing speed", "#10b981")
        self.card_facts = MetricCard("FACTS GATHERED", "0", "meta_evidence rows", "#38bdf8")
        self.card_eta = MetricCard("ETA TIMER", "IDLE", "Finish state")

        metric_strip.addWidget(self.card_workers)
        metric_strip.addWidget(self.card_tps)
        metric_strip.addWidget(self.card_facts)
        metric_strip.addWidget(self.card_eta)

        self.btn_cancel = QPushButton("[CANCEL JOB]")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setFixedHeight(38)
        self.btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #450a0a; color: #ef4444; border: 1px solid #ef4444;
                font-family: 'Consolas', monospace; font-weight: bold; padding: 4px 12px;
            }
            QPushButton:hover { background-color: #ef4444; color: #101012; }
        """)
        self.btn_cancel.clicked.connect(self._cancel_active_job)
        metric_strip.addWidget(self.btn_cancel)

        main_layout.addLayout(metric_strip)

        # Telemetry Bar Rack (5 Sleek Compact Progress Bars)
        telemetry_rack = QFrame()
        telemetry_rack.setStyleSheet("background-color: #16161a; border-radius: 4px; border: 1px solid #222228; padding: 6px 10px;")
        rack_layout = QVBoxLayout(telemetry_rack)
        rack_layout.setSpacing(3)

        self.meter_total = CompactBarMeter("Total Batch", "#10b981")
        self.meter_stage = CompactBarMeter("Active Stage", "#f59e0b")
        self.meter_file = CompactBarMeter("File Stream", "#38bdf8")
        self.meter_cpu = CompactBarMeter("CPU Load", "#38bdf8")
        self.meter_ram = CompactBarMeter("RAM Memory", "#a855f7")

        rack_layout.addWidget(self.meter_total)
        rack_layout.addWidget(self.meter_stage)
        rack_layout.addWidget(self.meter_file)
        rack_layout.addWidget(self.meter_cpu)
        rack_layout.addWidget(self.meter_ram)

        main_layout.addWidget(telemetry_rack)

        # Bottom Splitter: Console | History Table
        bottom_splitter = QSplitter(Qt.Orientation.Horizontal)

        left_box = QWidget()
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("<b>OPERATIONS LOG CONSOLE STREAM</b>"))

        self.log_console = RingBufferConsole(max_blocks=2000)
        left_layout.addWidget(self.log_console)

        right_box = QWidget()
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("<b>EXECUTION RUN HISTORY (sys_runs)</b>"))

        self.history_table = QTableWidget()
        self.history_table.setColumnCount(4)
        self.history_table.setHorizontalHeaderLabels(["Run ID", "Engine Version", "Started UTC", "Finished UTC"])
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history_table.itemDoubleClicked.connect(self._on_history_run_double_clicked)
        self.history_table.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        right_layout.addWidget(self.history_table)

        bottom_splitter.addWidget(left_box)
        bottom_splitter.addWidget(right_box)
        bottom_splitter.setSizes([600, 450])

        main_layout.addWidget(bottom_splitter, stretch=1)

        signals.log_emitted.connect(self._on_log_emitted)
        signals.telemetry_updated.connect(self._on_telemetry_updated)
        signals.ingestion_finished.connect(self._on_ingestion_finished)

        if self.watchdog_service.is_active:
            self.btn_watchdog.setChecked(True)
            self.btn_watchdog.setText("[WATCHDOG OBSERVER: ACTIVE]")
            self.btn_watchdog.setStyleSheet("background-color: #064e3b; color: #10b981; font-weight: bold; border: 1px solid #10b981;")

        self.reload_history_table()

    def reload_history_table(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT run_id, parser_version, started_at, finished_at FROM sys_runs ORDER BY started_at DESC LIMIT 20")
            rows = cursor.fetchall()
            self.history_table.setRowCount(len(rows))
            for r_idx, r in enumerate(rows):
                item_id = QTableWidgetItem(r[0][:8] + "...")
                item_id.setData(Qt.ItemDataRole.UserRole, r[0])
                item_ver = QTableWidgetItem(str(r[1] or "5.0.0"))
                item_start = QTableWidgetItem(str(r[2] or "")[:19])
                item_end = QTableWidgetItem(str(r[3] or "RUNNING")[:19])

                if r[3]:
                    item_end.setForeground(QColor("#10b981"))
                else:
                    item_end.setForeground(QColor("#f59e0b"))

                self.history_table.setItem(r_idx, 0, item_id)
                self.history_table.setItem(r_idx, 1, item_ver)
                self.history_table.setItem(r_idx, 2, item_start)
                self.history_table.setItem(r_idx, 3, item_end)
        except Exception:
            pass

    def _on_history_run_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        id_item = self.history_table.item(row, 0)
        if id_item:
            full_run_id = id_item.data(Qt.ItemDataRole.UserRole)
            if full_run_id:
                from gui.dialogs.audit import JobAuditInspectorDialog
                dialog = JobAuditInspectorDialog(full_run_id, self)
                dialog.exec()

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.btn_run_ingest.setEnabled(enabled)
        self.btn_run_enrich_pending.setEnabled(enabled)
        self.btn_run_enrich_full.setEnabled(enabled)
        self.btn_run_tax.setEnabled(enabled)
        self.btn_reingest.setEnabled(enabled)
        self.btn_wipe.setEnabled(enabled)
        self.btn_cancel.setEnabled(not enabled)

    def _start_ingestion_thread(self) -> None:
        self._set_controls_enabled(False)
        event_bus.publish(LogEvent("[*] Launching Stage A local ingestion pass..."))

        def worker():
            try:
                start_time = time.time()
                count = StageAIngestionEngine.run_stage_a()
                elapsed = time.time() - start_time
                event_bus.publish(IngestionFinishedEvent(imported_count=count, duration_sec=elapsed))
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Ingestion worker error: {ex}", "ERROR"))
                event_bus.publish(IngestionFinishedEvent(imported_count=0, duration_sec=0.0))

        threading.Thread(target=worker, daemon=True).start()

    def _start_enrichment_thread(self, mode: str) -> None:
        self._set_controls_enabled(False)
        mode_label = "Pending Tracks" if mode == "1" else "Full Library Re-Enrichment"
        event_bus.publish(LogEvent(f"[*] Launching Stage B Canonical Enrichment ({mode_label})..."))

        def worker():
            try:
                start_time = time.time()
                count = EnrichmentEngine.run_enrichment_pipeline(mode)
                elapsed = time.time() - start_time
                event_bus.publish(IngestionFinishedEvent(imported_count=count, duration_sec=elapsed))
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Enrichment worker error: {ex}", "ERROR"))
                event_bus.publish(IngestionFinishedEvent(imported_count=0, duration_sec=0.0))

        threading.Thread(target=worker, daemon=True).start()

    def _start_taxonomy_thread(self) -> None:
        self._set_controls_enabled(False)
        event_bus.publish(LogEvent("[*] Launching library taxonomy re-classification pass..."))

        def worker():
            try:
                start_time = time.time()
                count = TaxonomyService.reclassify_all_library_taxonomies()
                elapsed = time.time() - start_time
                event_bus.publish(LogEvent(f"[+] Taxonomy re-classification complete: Processed {count} recording(s) in {elapsed:.2f}s.", "SUCCESS"))
                event_bus.publish(IngestionFinishedEvent(imported_count=count, duration_sec=elapsed))
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Taxonomy re-classification worker error: {ex}", "ERROR"))
                event_bus.publish(IngestionFinishedEvent(imported_count=0, duration_sec=0.0))

        threading.Thread(target=worker, daemon=True).start()

    def _on_force_reingest_clicked(self) -> None:
        reply = QMessageBox.question(
            self, "Confirm Force Re-Ingest",
            "Are you sure you want to restore all archived/corrupted audio files back to data/input/ and force re-ingest the entire library?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._set_controls_enabled(False)
            event_bus.publish(LogEvent("[*] Restoring archived files to data/input/ and launching force re-ingest..."))

            def worker():
                try:
                    start_time = time.time()
                    count = StageAIngestionEngine.force_reingest_library()
                    elapsed = time.time() - start_time
                    event_bus.publish(IngestionFinishedEvent(imported_count=count, duration_sec=elapsed))
                except Exception as ex:
                    event_bus.publish(LogEvent(f"[-] Re-ingest worker error: {ex}", "ERROR"))
                    event_bus.publish(IngestionFinishedEvent(imported_count=0, duration_sec=0.0))

            threading.Thread(target=worker, daemon=True).start()

    def _on_wipe_database_clicked(self) -> None:
        reply = QMessageBox.critical(
            self, "CONFIRM FULL DATABASE PURGE",
            "WARNING: This will permanently wipe all library recordings, metadata facts, decisions, and authority mappings.\n\nAll archived/corrupted audio files will be automatically moved back to data/input/ ready for re-ingestion.\n\nAre you absolutely sure you want to purge the database clean?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                restored = wipe_database_clean(restore_files=True)
                event_bus.publish(LogEvent(f"[+] DATABASE PURGED CLEAN: Restored {restored} audio file(s) back to data/input/. Schema re-initialized successfully.", "SUCCESS"))
                event_bus.publish(IngestionFinishedEvent(imported_count=0, duration_sec=0.0))
                self.reload_history_table()
                self.meter_total.set_progress(0)
                self.meter_stage.set_progress(0)
                self.meter_file.set_progress(0)
                self.meter_cpu.set_progress(0)
                self.meter_ram.set_progress(0)
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Database wipe error: {ex}", "ERROR"))

    def _cancel_active_job(self) -> None:
        StageAIngestionEngine.cancel_current()
        EnrichmentEngine.cancel_current()
        self.btn_cancel.setEnabled(False)
        event_bus.publish(LogEvent("[*] Requesting pipeline job cancellation...", "WARNING"))

    @Slot(TelemetryEvent)
    def _on_telemetry_updated(self, event: TelemetryEvent) -> None:
        self.meter_total.set_progress(event.total_progress)
        self.meter_stage.set_progress(event.stage_progress)
        self.meter_file.set_progress(event.file_progress)
        self.meter_cpu.set_progress(int(event.cpu_pct), f"{event.cpu_pct:.1f}%", alert_high=True)

        ram_pct = int((event.ram_gb / TOTAL_SYSTEM_RAM_GB) * 100) if TOTAL_SYSTEM_RAM_GB > 0 else 0
        self.meter_ram.set_progress(ram_pct, f"{event.ram_gb:.2f} GB", alert_high=True)

        self.card_tps.set_value(f"{event.throughput_tps:.1f} trk/s")
        self.card_facts.set_value(f"{event.facts_count:,}", "Gathered facts")
        self.card_eta.set_value(event.finish_est_str)

    @Slot(IngestionFinishedEvent)
    def _on_ingestion_finished(self, event: IngestionFinishedEvent) -> None:
        self._set_controls_enabled(True)
        self.meter_total.set_progress(100)
        self.meter_stage.set_progress(100)
        self.meter_file.set_progress(100)
        self.card_tps.set_value("0.0 trk/s")
        self.card_eta.set_value("Complete")
        self.reload_history_table()

    @Slot(LogEvent)
    def _on_log_emitted(self, event: LogEvent) -> None:
        self.log_console.append_log(event)

    def _toggle_watchdog(self, checked: bool) -> None:
        if checked:
            self.watchdog_service.start()
            self.btn_watchdog.setText("[WATCHDOG OBSERVER: ACTIVE]")
            self.btn_watchdog.setStyleSheet("background-color: #064e3b; color: #10b981; font-weight: bold; border: 1px solid #10b981;")
            event_bus.publish(LogEvent("[+] Watchdog observer started on data/input."))
        else:
            self.watchdog_service.stop()
            self.btn_watchdog.setText("[TOGGLE WATCHDOG]")
            self.btn_watchdog.setStyleSheet("")
            event_bus.publish(LogEvent("[*] Watchdog observer stopped."))

    def closeEvent(self, event: Any) -> None:
        if self.watchdog_service.is_active:
            self.watchdog_service.stop()
        super().closeEvent(event)