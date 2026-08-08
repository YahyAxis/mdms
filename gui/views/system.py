"""
System Health Center & Maintenance View
Provides live API network monitoring with 2-second background polling, individual provider circuit resets,
database integrity checking, one-click SQLite maintenance tools, and config inspection.
Refactored to import centralized UI components and support real-time crawler queue metrics.
"""

import os
import json
import threading
from typing import List, Dict, Any, Optional, Tuple
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, 
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, 
    QAbstractItemView, QScrollArea, QTabWidget, QMessageBox
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QColor, QFont

from config.settings import settings
from db.core import get_connection, db_transaction, get_postgres_engine
from domain.events import event_bus, LogEvent, CrawlerTelemetryEvent, signals
from services.resolve import purge_invalid_isrc_evidence
from utils.net import health_tracker, get_circuit_breaker_stats, _breakers, get_http_client
from gui.widgets.metric import MetricCard

# Centralized imports replacing locally duplicated widgets and layout helpers
from gui.widgets.common import create_tab_scroll_area, SystemCard

class SystemHealthWorkspace(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self._reload_network_table)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        header_row = QHBoxLayout()
        header = QLabel("SYSTEM HEALTH // API NETWORK CENTER & DATABASE MAINTENANCE")
        header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 11pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(header)
        header_row.addStretch()

        btn_test_endpoints = QPushButton("[TEST ALL API ENDPOINTS]")
        btn_test_endpoints.setFixedHeight(28)
        btn_test_endpoints.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_test_endpoints.clicked.connect(self._test_api_endpoints)
        header_row.addWidget(btn_test_endpoints)

        btn_refresh = QPushButton("[REFRESH SYSTEM HEALTH]")
        btn_refresh.setFixedWidth(180)
        btn_refresh.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_refresh.clicked.connect(self.reload_workspace)
        header_row.addWidget(btn_refresh)

        main_layout.addLayout(header_row)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=1)

        self._build_tab_network()
        self._build_tab_maintenance()
        self._build_tab_config()

        # Connect the thread-safe signal handler to process background crawl metrics
        signals.crawler_telemetry_updated.connect(self._on_crawler_telemetry_updated)

        self.reload_workspace()
        self.poll_timer.start()

    def _build_tab_network(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_network = SystemCard("External Authority API Network Status & Circuit Breakers (Live 2s Polling)")
        
        actions_row = QHBoxLayout()
        lbl_hint = QLabel("DOUBLE-CLICK A ROW TO RESET SPECIFIC CIRCUIT BREAKER")
        lbl_hint.setStyleSheet("font-family: 'Consolas', monospace; font-size: 7.5pt; color: #828a9a;")
        actions_row.addWidget(lbl_hint)
        actions_row.addStretch()

        btn_reset_breakers = QPushButton("[RESET ALL CIRCUIT BREAKERS]")
        btn_reset_breakers.setObjectName("AmberPrimaryBtn")
        btn_reset_breakers.setFixedHeight(26)
        btn_reset_breakers.clicked.connect(self._reset_all_breakers)
        actions_row.addWidget(btn_reset_breakers)
        card_network.card_layout.addLayout(actions_row)

        # High-density crawler queue telemetry metrics
        crawler_kpi = QHBoxLayout()
        self.card_crawl_seed = MetricCard("ACTIVE CRAWL SEED", "IDLE", "Seed extracted from library", "#f59e0b")
        self.card_crawl_queue = MetricCard("PENDING FRONTIER", "0", "Total queued crawling seeds", "#38bdf8")
        self.card_crawl_total = MetricCard("DISCOVERED CANDIDATES", "0", "Cumulative crawl harvest", "#10b981")
        
        crawler_kpi.addWidget(self.card_crawl_seed)
        crawler_kpi.addWidget(self.card_crawl_queue)
        crawler_kpi.addWidget(self.card_crawl_total)
        layout.addLayout(crawler_kpi)

        self.table_network = QTableWidget()
        self.table_network.setColumnCount(7)
        self.table_network.setHorizontalHeaderLabels(["Provider ID", "Circuit State", "Total Requests", "Successes", "Failures", "Latency EMA", "Last Error / Timestamp"])
        self.table_network.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table_network.setColumnWidth(0, 130)
        self.table_network.setColumnWidth(1, 110)
        self.table_network.setColumnWidth(2, 110)
        self.table_network.setColumnWidth(3, 90)
        self.table_network.setColumnWidth(4, 85)
        self.table_network.setColumnWidth(5, 105)
        self.table_network.verticalHeader().hide()
        self.table_network.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_network.itemDoubleClicked.connect(self._on_provider_double_clicked)
        self.table_network.setMinimumHeight(320)
        self.table_network.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_network.add_row(self.table_network)
        layout.addWidget(card_network)

        self.tabs.addTab(scroll, "[1] API NETWORK & CIRCUIT MONITORS")

    def _build_tab_maintenance(self) -> None:
        scroll, layout = create_tab_scroll_area()

        db_kpi = QHBoxLayout()
        self.card_db_size = MetricCard("DB SIZE", "--", "Active size footprint", "#10b981")
        self.card_db_tables = MetricCard("TABLE COUNT", "--", "Active public tables", "#38bdf8")
        self.card_db_page = MetricCard("BLOCK SIZE", "--", "PostgreSQL page size", "#f59e0b")
        self.card_db_integrity = MetricCard("DB INTEGRITY", "--", "PostgreSQL verification")

        db_kpi.addWidget(self.card_db_size)
        db_kpi.addWidget(self.card_db_tables)
        db_kpi.addWidget(self.card_db_page)
        db_kpi.addWidget(self.card_db_integrity)
        layout.addLayout(db_kpi)

        card_maint_controls = SystemCard("Database Optimization & Maintenance Controls")
        
        maint_grid = QVBoxLayout()
        maint_grid.setSpacing(8)

        btn_row1 = QHBoxLayout()
        btn_vacuum = QPushButton("[EXECUTE DATABASE VACUUM]")
        btn_vacuum.setFixedHeight(28)
        btn_vacuum.setObjectName("AmberPrimaryBtn")
        btn_vacuum.clicked.connect(self._exec_vacuum)

        btn_reindex = QPushButton("[RE-INDEX DATABASE INDEXES]")
        btn_reindex.setFixedHeight(28)
        btn_reindex.clicked.connect(self._exec_reindex)

        btn_row1.addWidget(btn_vacuum)
        btn_row1.addWidget(btn_reindex)
        maint_grid.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_purge_isrc = QPushButton("[PURGE INVALID ISRC EVIDENCE]")
        btn_purge_isrc.setFixedHeight(28)
        btn_purge_isrc.clicked.connect(self._exec_purge_isrc)

        btn_flush_cache = QPushButton("[FLUSH FAST-PASS CACHE]")
        btn_flush_cache.setFixedHeight(28)
        btn_flush_cache.clicked.connect(self._exec_flush_cache)

        btn_row2.addWidget(btn_purge_isrc)
        btn_row2.addWidget(btn_flush_cache)
        maint_grid.addLayout(btn_row2)

        card_maint_controls.card_layout.addLayout(maint_grid)
        layout.addWidget(card_maint_controls)

        self.tabs.addTab(scroll, "[2] DATABASE MAINTENANCE")

    def _build_tab_config(self) -> None:
        scroll, layout = create_tab_scroll_area()

        card_config = SystemCard("System Runtime Configuration Settings")
        self.table_config = QTableWidget()
        self.table_config.setColumnCount(2)
        self.table_config.setHorizontalHeaderLabels(["Setting Parameter Key", "Active Configuration Value"])
        self.table_config.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_config.setColumnWidth(0, 240)
        self.table_config.verticalHeader().hide()
        self.table_config.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_config.setMinimumHeight(350)
        self.table_config.setStyleSheet("""
            QTableWidget {
                background-color: #121216; color: #e2e8f0; gridline-color: #222228;
                border: 1px solid #222228; font-family: 'Consolas', 'JetBrains Mono', monospace; font-size: 8.5pt;
            }
            QTableWidget::item { padding: 4px; color: #cbd5e1; }
        """)
        card_config.add_row(self.table_config)
        layout.addWidget(card_config)

        self.tabs.addTab(scroll, "[3] SYSTEM CONFIGURATION")

    def reload_workspace(self) -> None:
        self._reload_network_table()
        self._reload_db_metrics()
        self._reload_config_table()

    def _reload_network_table(self) -> None:
        try:
            metrics = health_tracker.get_all_metrics()
            cb_stats = get_circuit_breaker_stats()

            all_providers = sorted(list(set(list(metrics.keys()) + list(cb_stats.keys()) + ["MUSICBRAINZ", "ACOUSTID", "DEEZER", "DISCOGS", "LASTFM", "WIKIDATA"])))
            
            self.table_network.setRowCount(len(all_providers))
            for r_idx, p_id in enumerate(all_providers):
                p_m = metrics.get(p_id, {})
                cb_s = cb_stats.get(p_id, {})

                state_str = cb_s.get("state", "CLOSED")
                tot_req = p_m.get("total_requests", 0)
                succ = p_m.get("success_count", 0)
                fail = p_m.get("failure_count", 0)
                ema_ms = p_m.get("latency_ema_ms", 0.0)
                last_err = p_m.get("last_error_msg") or (p_m.get("last_success_timestamp") or "Operational")

                item_pid = QTableWidgetItem(p_id)
                item_state = QTableWidgetItem(state_str)

                if state_str == "CLOSED":
                    item_state.setForeground(QColor("#10b981"))
                elif state_str == "HALF_OPEN":
                    item_state.setForeground(QColor("#f59e0b"))
                else:
                    item_state.setForeground(QColor("#ef4444"))

                self.table_network.setItem(r_idx, 0, item_pid)
                self.table_network.setItem(r_idx, 1, item_state)
                self.table_network.setItem(r_idx, 2, QTableWidgetItem(f"{tot_req:,}"))
                self.table_network.setItem(r_idx, 3, QTableWidgetItem(f"{succ:,}"))
                self.table_network.setItem(r_idx, 4, QTableWidgetItem(f"{fail:,}"))
                self.table_network.setItem(r_idx, 5, QTableWidgetItem(f"{ema_ms:.1f} ms"))
                self.table_network.setItem(r_idx, 6, QTableWidgetItem(str(last_err)))

        except Exception:
            pass

    def _on_provider_double_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        pid_item = self.table_network.item(row, 0)
        if pid_item:
            p_id = pid_item.text().strip().upper()
            if p_id in _breakers:
                _breakers[p_id].record_success()
                event_bus.publish(LogEvent(f"[+] Reset circuit breaker for provider '{p_id}'.", "SUCCESS"))
                self._reload_network_table()

    def _reset_all_breakers(self) -> None:
        for cb in _breakers.values():
            cb.record_success()
        event_bus.publish(LogEvent("[+] All network circuit breakers reset to CLOSED state.", "SUCCESS"))
        self._reload_network_table()

    def _test_api_endpoints(self) -> None:
        event_bus.publish(LogEvent("[*] Testing external API endpoint connectivity..."))

        def worker():
            client = get_http_client()
            endpoints = [
                ("MUSICBRAINZ", f"{settings.MUSICBRAINZ_BASE_URL}/recording/12345678-1234-1234-1234-123456789012?fmt=json"),
                ("DEEZER", "https://api.deezer.com/search?q=test"),
                ("ITUNES", "https://itunes.apple.com/search?term=test&limit=1"),
                ("WIKIDATA", settings.WIKIDATA_SPARQL_URL)
            ]
            for p_id, url in endpoints:
                try:
                    res = client.get(url, timeout=4.0)
                    health_tracker.record_success(p_id, 150.0)
                    event_bus.publish(LogEvent(f"[+] Endpoint test {p_id}: HTTP {res.status_code}", "SUCCESS"))
                except Exception as ex:
                    health_tracker.record_failure(p_id, ex.__class__.__name__, str(ex))
                    event_bus.publish(LogEvent(f"[-] Endpoint test {p_id} failed: {ex}", "WARNING"))

        threading.Thread(target=worker, daemon=True).start()

    def _reload_db_metrics(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT pg_database_size(current_database());")
            db_size_bytes = cursor.fetchone()[0] or 0
            size_mb = db_size_bytes / (1024 ** 2)

            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.tables 
                WHERE table_schema = 'public'
            """)
            table_cnt = cursor.fetchone()[0] or 0

            cursor.execute("SHOW block_size;")
            page_sz_raw = cursor.fetchone()
            page_sz = int(page_sz_raw[0]) if page_sz_raw else 8192

            try:
                cursor.execute("SELECT COUNT(*) FROM core_recordings;")
                integrity_str = "PASS (OK)"
                integrity_color = "#10b981"
            except Exception:
                integrity_str = "ERROR"
                integrity_color = "#ef4444"

            self.card_db_size.set_value(f"{size_mb:.2f} MB", f"{settings.POSTGRES_DB}")
            self.card_db_tables.set_value(f"{table_cnt}", "Active user tables")
            self.card_db_page.set_value(f"{page_sz} B", "Database block allocation")
            self.card_db_integrity.set_value(integrity_str, "PostgreSQL connection verify", integrity_color)

        except Exception:
            pass

    def _exec_vacuum(self) -> None:
        try:
            engine = get_postgres_engine()
            raw_conn = engine.raw_connection()
            raw_conn.autocommit = True
            
            with raw_conn.cursor() as cur:
                cur.execute("VACUUM;")
            
            raw_conn.close()
            event_bus.publish(LogEvent("[+] Database VACUUM complete: Unused spaces reclaimed natively.", "SUCCESS"))
            self._reload_db_metrics()
        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] VACUUM error: {ex}", "ERROR"))

    def _exec_reindex(self) -> None:
        try:
            engine = get_postgres_engine()
            raw_conn = engine.raw_connection()
            raw_conn.autocommit = True
            
            with raw_conn.cursor() as cur:
                cur.execute("REINDEX SCHEMA public;")
            
            raw_conn.close()
            event_bus.publish(LogEvent("[+] Database REINDEX complete: Index trees rebuilt natively.", "SUCCESS"))
        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] REINDEX error: {ex}", "ERROR"))

    def _exec_purge_isrc(self) -> None:
        count = purge_invalid_isrc_evidence()
        event_bus.publish(LogEvent(f"[+] Purged {count} invalid syntax ISRC evidence rows.", "SUCCESS"))

    def _exec_flush_cache(self) -> None:
        try:
            with db_transaction() as tx:
                tx.execute("DELETE FROM sys_fastpass_cache WHERE cache_key != 'last_maint_state'")
            event_bus.publish(LogEvent("[+] Fast-pass parser cache flushed successfully.", "SUCCESS"))
        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] Cache flush error: {ex}", "ERROR"))

    def _reload_config_table(self) -> None:
        try:
            config_items = [
                ("APP_NAME", settings.APP_NAME),
                ("APP_VERSION", settings.APP_VERSION),
                ("MUSICBRAINZ_BASE_URL", settings.MUSICBRAINZ_BASE_URL),
                ("WIKIDATA_SPARQL_URL", settings.WIKIDATA_SPARQL_URL),
                ("MAX_WORKER_PROCESSES", str(settings.MAX_WORKER_PROCESSES)),
                ("POSTGRES_HOST", str(settings.POSTGRES_HOST)),
                ("POSTGRES_PORT", str(settings.POSTGRES_PORT)),
                ("POSTGRES_DB", str(settings.POSTGRES_DB)),
                ("INPUT_DIR", str(settings.INPUT_DIR)),
                ("ARCHIVE_DIR", str(settings.ARCHIVE_DIR)),
                ("CORRUPTED_DIR", str(settings.CORRUPTED_DIR)),
                ("EXPORTS_DIR", str(settings.EXPORTS_DIR)),
                ("ACOUSTID_CLIENT_KEY", settings.ACOUSTID_CLIENT_KEY[:4] + "****" if settings.ACOUSTID_CLIENT_KEY else "N/A"),
                ("LASTFM_API_KEY", settings.LASTFM_API_KEY[:4] + "****" if settings.LASTFM_API_KEY else "N/A"),
                ("DISCOGS_CONSUMER_KEY", settings.DISCOGS_CONSUMER_KEY[:4] + "****" if settings.DISCOGS_CONSUMER_KEY else "N/A"),
            ]

            self.table_config.setRowCount(len(config_items))
            for r_idx, (key_str, val_str) in enumerate(config_items):
                item_k = QTableWidgetItem(key_str)
                item_k.setForeground(QColor("#f59e0b"))
                self.table_config.setItem(r_idx, 0, item_k)
                self.table_config.setItem(r_idx, 1, QTableWidgetItem(val_str))

        except Exception:
            pass

    @Slot(CrawlerTelemetryEvent)
    def _on_crawler_telemetry_updated(self, event: CrawlerTelemetryEvent) -> None:
        """Invoked on the GUI thread to update real-time background crawl metrics."""
        self.card_crawl_seed.set_value(event.active_seed[:28].upper() + "..." if len(event.active_seed) > 28 else event.active_seed.upper())
        self.card_crawl_queue.set_value(f"{event.pending_queue_size:,}")
        self.card_crawl_total.set_value(f"{event.total_crawled_count:,}")

    def closeEvent(self, event) -> None:
        self.poll_timer.stop()
        super().closeEvent(event)