"""
Discovery Crate GUI
"""

import json
import hashlib
import threading
import urllib.parse
from typing import List, Dict, Any, Optional, Tuple
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, 
    QPushButton, QComboBox, QSlider, QScrollArea, QSplitter, 
    QPlainTextEdit, QLineEdit, QProgressBar
)
from PySide6.QtCore import Qt, Slot, Signal, QTimer
from PySide6.QtGui import QColor, QFont

from db.core import get_connection, db_transaction, close_thread_connection
from domain.models import CandidateState, DiscoveryCandidate, DiscoveryScore
from domain.events import event_bus, LogEvent, CrawlerTelemetryEvent, signals
from services.discover import (
    DiscoveryEnginePipeline, rescore_active_candidates, 
    mark_candidate_state, snooze_candidate, CompositeCandidateScorer
)
from gui.widgets.metric import MetricCard

class CandidateCard(QFrame):
    action_triggered = Signal()

    def __init__(
        self,
        candidate: DiscoveryCandidate,
        score_obj: DiscoveryScore,
        parent=None
    ) -> None:
        super().__init__(parent)
        self.candidate = candidate
        self.score_obj = score_obj

        self.setObjectName("WorkbenchCardAccent")
        self.setStyleSheet("""
            QFrame#WorkbenchCardAccent {
                background-color: #16161a;
                border: 1px solid #222228;
                border-left: 3px solid #f59e0b;
                border-radius: 4px;
            }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 4, 6, 4)
        main_layout.setSpacing(2)

        # Horizontal row layout
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        exp_data = json.loads(score_obj.explanation_json or "{}") if score_obj.explanation_json else {}
        margin = exp_data.get("uncertainty_margin", 0.040)
        
        lbl_ccs = QLabel(f"{candidate.final_ccs:.3f}")
        lbl_ccs.setToolTip(f"Composite Candidate Score (Margin: +/- {margin:.3f})")
        lbl_ccs.setFixedWidth(46)
        lbl_ccs.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_ccs.setStyleSheet("""
            QLabel {
                background-color: #064e3b; color: #10b981; 
                font-family: 'Consolas', 'JetBrains Mono', monospace; 
                font-size: 7.5pt; font-weight: bold; padding: 1px;
                border-radius: 2px; border: 1px solid #10b981;
            }
        """)
        row_layout.addWidget(lbl_ccs)

        title_str = (candidate.title or "Untitled").upper()
        if candidate.release_year:
            title_str += f" ({candidate.release_year})"

        lbl_info = QLabel(f"<span style='color:#f59e0b; font-weight:bold;'>{candidate.artist_name}</span> — <span style='color:#e2e8f0;'>{title_str}</span>")
        lbl_info.setStyleSheet("font-family: 'Segoe UI', sans-serif; font-size: 8.5pt;")
        row_layout.addWidget(lbl_info)

        lbl_genre = QLabel(f"[{candidate.primary_genre} / {candidate.primary_subgenre}]")
        lbl_genre.setStyleSheet("font-family: 'Consolas', monospace; font-size: 7.5pt; color: #828a9a;")
        row_layout.addWidget(lbl_genre)

        row_layout.addStretch()

        self.btn_toggle_anlrg = QPushButton("[ANLRG]")
        self.btn_toggle_anlrg.setFixedSize(52, 18)
        self.btn_toggle_anlrg.setToolTip("Toggle advanced scoring breakdown matrix")
        self.btn_toggle_anlrg.setStyleSheet("""
            QPushButton {
                background-color: #121216; color: #828a9a; border: 1px solid #222228;
                font-family: 'Consolas', monospace; font-size: 7pt; font-weight: bold; border-radius: 2px;
            }
            QPushButton:hover { color: #f59e0b; border-color: #f59e0b; }
        """)
        self.btn_toggle_anlrg.clicked.connect(self._toggle_anlrg)
        row_layout.addWidget(self.btn_toggle_anlrg)

        # 5. Micro-Action Buttons
        btn_accept = QPushButton("ACC")
        btn_accept.setFixedSize(32, 18)
        btn_accept.setToolTip("Accept and queue this recommendation")
        btn_accept.clicked.connect(self._on_accept)

        btn_snooze = QPushButton("SNZ")
        btn_snooze.setFixedSize(32, 18)
        btn_snooze.setToolTip("Snooze this release for 14 days")
        btn_snooze.clicked.connect(self._on_snooze)

        btn_ignore = QPushButton("IGN")
        btn_ignore.setFixedSize(32, 18)
        btn_ignore.setToolTip("Ignore this candidate")
        btn_ignore.clicked.connect(self._on_ignore)

        btn_owned = QPushButton("OWN")
        btn_owned.setFixedSize(32, 18)
        btn_owned.setToolTip("Mark this release as already acquired")
        btn_owned.clicked.connect(self._on_mark_owned)

        button_style = """
            QPushButton {
                font-family: 'Consolas', monospace;
                font-size: 7.5pt;
                font-weight: bold;
                border-radius: 2px;
            }
        """
        btn_accept.setStyleSheet(button_style + "background-color: #f59e0b; color: #101012; border: none;")
        btn_snooze.setStyleSheet(button_style + "color: #38bdf8; border: 1px solid #38bdf8; background-color: transparent;")
        btn_ignore.setStyleSheet(button_style + "background-color: #450a0a; color: #ef4444; border: 1px solid #ef4444;")
        btn_owned.setStyleSheet(button_style + "color: #10b981; border: 1px solid #10b981; background-color: transparent;")

        row_layout.addWidget(btn_accept)
        row_layout.addWidget(btn_snooze)
        row_layout.addWidget(btn_ignore)
        row_layout.addWidget(btn_owned)

        main_layout.addLayout(row_layout)

        # Conditional drop-down breakdown panel
        self.anlrg_container = QFrame()
        self.anlrg_container.setStyleSheet("background-color: #0c0c0e; border: 1px solid #222228; padding: 4px; border-radius: 2px;")
        anlrg_layout = QVBoxLayout(self.anlrg_container)
        anlrg_layout.setSpacing(2)

        anlrg_layout.addWidget(self._create_mini_score_bar("Relevance", score_obj.v_rel, "#10b981"))
        anlrg_layout.addWidget(self._create_mini_score_bar("Collector", score_obj.v_coll, "#38bdf8"))
        anlrg_layout.addWidget(self._create_mini_score_bar("Graph Conf", score_obj.v_graph, "#f59e0b"))
        anlrg_layout.addWidget(self._create_mini_score_bar("Deficit Fill", score_obj.v_def, "#a855f7"))

        lbl_pen = QLabel(f"PROMO PENALTIES: Saturation={score_obj.p_sat:.2f} | Fatigue={score_obj.delta_fatigue:.2f} | MBID: {candidate.release_group_mbid or 'N/A'}")
        lbl_pen.setStyleSheet("font-family: 'Consolas', monospace; font-size: 7pt; color: #828a9a;")
        anlrg_layout.addWidget(lbl_pen)

        self.anlrg_container.hide()
        main_layout.addWidget(self.anlrg_container)

    def _create_mini_score_bar(self, label: str, score_val: float, color: str) -> QWidget:
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 1, 0, 1)
        l.setSpacing(6)
        
        lbl = QLabel(f"{label.upper()}:")
        lbl.setFixedWidth(85)
        lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 7pt; color: #828a9a; font-weight: bold;")
        
        bar = QProgressBar()
        bar.setFixedHeight(5)
        bar.setValue(int(score_val * 100))
        bar.setTextVisible(False)
        bar.setStyleSheet(f"QProgressBar {{ background-color: #121216; border: 1px solid #222228; border-radius: 1px; }} QProgressBar::chunk {{ background-color: {color}; border-radius: 1px; }}")

        val_lbl = QLabel(f"{score_val:.2f}")
        val_lbl.setFixedWidth(35)
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val_lbl.setStyleSheet(f"font-family: 'Consolas', monospace; font-size: 7pt; color: {color}; font-weight: bold;")

        l.addWidget(lbl)
        l.addWidget(bar, stretch=1)
        l.addWidget(val_lbl)
        return w

    def _toggle_anlrg(self) -> None:
        if self.anlrg_container.isVisible():
            self.anlrg_container.hide()
            self.btn_toggle_anlrg.setText("[ANLRG]")
        else:
            self.anlrg_container.show()
            self.btn_toggle_anlrg.setText("[HIDE]")

    def _on_accept(self) -> None:
        mark_candidate_state(self.candidate.candidate_id, CandidateState.QUEUED)
        try:
            conn = get_connection()
            with db_transaction() as tx:
                tx.execute("""
                    UPDATE sys_crawl_frontier 
                    SET priority = priority + 0.25, updated_at = CURRENT_TIMESTAMP 
                    WHERE seed_id = (
                        SELECT musicbrainz_artist_id FROM core_recordings 
                        WHERE LOWER(artist_name) = LOWER(%s) LIMIT 1
                    )
                """, (self.candidate.artist_name,))
        except Exception:
            pass
        self.action_triggered.emit()

    def _on_snooze(self) -> None:
        snooze_candidate(self.candidate.candidate_id, days=14)
        try:
            conn = get_connection()
            with db_transaction() as tx:
                tx.execute("""
                    UPDATE sys_crawl_frontier 
                    SET priority = GREATEST(0.1, priority - 0.20), updated_at = CURRENT_TIMESTAMP 
                    WHERE seed_id = (
                        SELECT musicbrainz_artist_id FROM core_recordings 
                        WHERE LOWER(artist_name) = LOWER(%s) LIMIT 1
                    )
                """, (self.candidate.artist_name,))
        except Exception:
            pass
        self.action_triggered.emit()

    def _on_ignore(self) -> None:
        mark_candidate_state(self.candidate.candidate_id, CandidateState.IGNORED)
        try:
            conn = get_connection()
            with db_transaction() as tx:
                tx.execute("""
                    UPDATE sys_crawl_frontier 
                    SET priority = GREATEST(0.1, priority - 0.40), updated_at = CURRENT_TIMESTAMP 
                    WHERE seed_id = (
                        SELECT musicbrainz_artist_id FROM core_recordings 
                        WHERE LOWER(artist_name) = LOWER(%s) LIMIT 1
                    )
                """, (self.candidate.artist_name,))
        except Exception:
            pass
        self.action_triggered.emit()

    def _on_mark_owned(self) -> None:
        mark_candidate_state(self.candidate.candidate_id, CandidateState.ACQUIRED)
        self.action_triggered.emit()


class DiscoveryWorkspace(QWidget):
    pipeline_finished = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.pipeline = DiscoveryEnginePipeline()
        self.cached_results: List[Tuple[DiscoveryCandidate, DiscoveryScore]] = []

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(80)
        self.debounce_timer.timeout.connect(self._exec_slider_rescore)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Header Title Bar
        header_row = QHBoxLayout()
        header = QLabel("DISCOVERY // RECOMMENDATION CRATE & STRATEGY STUDIO")
        header.setStyleSheet("font-family: 'Consolas', monospace; font-size: 11pt; font-weight: bold; color: #f59e0b;")
        header_row.addWidget(header)
        header_row.addStretch()

        self.lbl_crawler_status = QLabel("CRAWLER STATUS: IDLE")
        self.lbl_crawler_status.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #828a9a; border-right: 1px solid #222228; padding-right: 10px;")
        header_row.addWidget(self.lbl_crawler_status)

        self.input_crawl_artist = QLineEdit()
        self.input_crawl_artist.setPlaceholderText("Enter Artist to Crawl (e.g. Nirvana)...")
        self.input_crawl_artist.setFixedWidth(200)
        header_row.addWidget(self.input_crawl_artist)

        self.btn_crawl_artist = QPushButton("[CRAWL]")
        self.btn_crawl_artist.setFixedHeight(28)
        self.btn_crawl_artist.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        self.btn_crawl_artist.clicked.connect(self._on_crawl_artist_clicked)
        header_row.addWidget(self.btn_crawl_artist)

        self.input_target_genre = QLineEdit()
        self.input_target_genre.setPlaceholderText("Filter Crawl Genre...")
        self.input_target_genre.setFixedWidth(140)
        self.input_target_genre.textChanged.connect(self._on_target_genre_changed)
        header_row.addWidget(self.input_target_genre)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search title, artist, or genre...")
        self.search_input.setFixedWidth(180)
        self.search_input.textChanged.connect(self._apply_text_filter)
        header_row.addWidget(self.search_input)

        self.combo_genre_filter = QComboBox()
        self.combo_genre_filter.setFixedWidth(150)
        self.combo_genre_filter.addItem("[ALL GENRES]")
        self.combo_genre_filter.currentIndexChanged.connect(self._apply_text_filter)
        header_row.addWidget(self.combo_genre_filter)

        self.combo_state_filter = QComboBox()
        self.combo_state_filter.addItems(["[STATE: ACTIVE CRATE]", "[STATE: QUEUED / ACCEPTED]", "[STATE: SNOOZED]", "[STATE: ACQUIRED]"])
        self.combo_state_filter.setFixedWidth(200)
        self.combo_state_filter.currentIndexChanged.connect(self.reload_crate)
        header_row.addWidget(self.combo_state_filter)

        self.combo_preset = QComboBox()
        self.combo_preset.addItems(["Balanced Curator", "Completionist", "Genre Explorer", "Crate Digger"])
        self.combo_preset.setFixedWidth(170)
        self.combo_preset.currentTextChanged.connect(self._on_preset_changed)
        header_row.addWidget(self.combo_preset)

        btn_accept_5 = QPushButton("[ACCEPT TOP 5]")
        btn_accept_5.setFixedHeight(28)
        btn_accept_5.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        btn_accept_5.clicked.connect(self._on_accept_top_5)
        header_row.addWidget(btn_accept_5)

        # Refresh button to reload the crate list manually
        self.btn_refresh = QPushButton("[REFRESH]")
        self.btn_refresh.setFixedHeight(28)
        self.btn_refresh.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold;")
        self.btn_refresh.clicked.connect(self.reload_crate)
        header_row.addWidget(self.btn_refresh)

        self.btn_run_pipeline = QPushButton("[RUN DISCOVERY PIPELINE]")
        self.btn_run_pipeline.setObjectName("AmberPrimaryBtn")
        self.btn_run_pipeline.setFixedHeight(28)
        self.btn_run_pipeline.clicked.connect(self.reload_crate)
        header_row.addWidget(self.btn_run_pipeline)

        main_layout.addLayout(header_row)

        kpi_row = QHBoxLayout()
        self.card_crate_cnt = MetricCard("CRATE CANDIDATES", "--", "Active recommendations")
        self.card_top_score = MetricCard("TOP_CCS_SCORE", "--", "Peak composite index", "#10b981")
        self.card_active_strat = MetricCard("ACTIVE STRATEGY", "Balanced Curator", "Vector preset", "#38bdf8")
        self.card_snoozed_cnt = MetricCard("SNOOZED RELEASES", "--", "14-day hold queue", "#f59e0b")

        kpi_row.addWidget(self.card_crate_cnt)
        kpi_row.addWidget(self.card_top_score)
        kpi_row.addWidget(self.card_active_strat)
        kpi_row.addWidget(self.card_snoozed_cnt)
        main_layout.addLayout(kpi_row)

        sliders_card = QFrame()
        sliders_card.setObjectName("WorkbenchCardAccent")
        sliders_card.setStyleSheet("QFrame#WorkbenchCardAccent { background-color: #16161a; border: 1px solid #222228; border-left: 3px solid #f59e0b; border-radius: 4px; padding: 8px 12px; }")
        sliders_layout = QVBoxLayout(sliders_card)
        sliders_layout.setSpacing(4)

        lbl_s_title = QLabel("STRATEGY VECTOR WEIGHT SLIDERS (DEBOUNCED REAL-TIME RE-SCORING)")
        lbl_s_title.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; font-weight: bold; color: #828a9a;")
        sliders_layout.addWidget(lbl_s_title)

        sliders_grid = QHBoxLayout()
        
        self.slider_rel = QSlider(Qt.Orientation.Horizontal)
        self.slider_coll = QSlider(Qt.Orientation.Horizontal)
        self.slider_graph = QSlider(Qt.Orientation.Horizontal)
        self.slider_def = QSlider(Qt.Orientation.Horizontal)

        self.lbl_val_rel = QLabel("35%")
        self.lbl_val_coll = QLabel("25%")
        self.lbl_val_graph = QLabel("20%")
        self.lbl_val_def = QLabel("20%")

        for s_lbl_str, slider, val_lbl in (
            ("Relevance:", self.slider_rel, self.lbl_val_rel),
            ("Collector:", self.slider_coll, self.lbl_val_coll),
            ("Graph:", self.slider_graph, self.lbl_val_graph),
            ("Deficit:", self.slider_def, self.lbl_val_def)
        ):
            box = QHBoxLayout()
            lbl = QLabel(s_lbl_str)
            lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #cbd5e1;")
            slider.setRange(5, 70)
            slider.setValue(25)
            slider.setStyleSheet("QSlider::handle:horizontal { background-color: #f59e0b; width: 10px; border-radius: 2px; }")
            val_lbl.setFixedWidth(35)
            val_lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 8pt; color: #f59e0b; font-weight: bold;")
            
            box.addWidget(lbl)
            box.addWidget(slider, stretch=1)
            box.addWidget(val_lbl)
            sliders_grid.addLayout(box)

            slider.valueChanged.connect(self._on_slider_moved)

        sliders_layout.addLayout(sliders_grid)
        main_layout.addWidget(sliders_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        scroll_content = QWidget()
        self.cards_layout = QVBoxLayout(scroll_content)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(6)

        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll, stretch=1)

        self.pipeline_finished.connect(self._on_pipeline_finished)

        signals.crawler_telemetry_updated.connect(self._on_crawler_telemetry_updated)

        self._sync_sliders_to_preset("Balanced Curator")
        self.reload_crate()

        self._load_cached_target_genre_filter()

    def _sync_sliders_to_preset(self, preset_name: str) -> None:
        preset_weights = CompositeCandidateScorer.STRATEGY_PRESETS.get(preset_name, {})
        if preset_weights:
            for s in (self.slider_rel, self.slider_coll, self.slider_graph, self.slider_def):
                s.blockSignals(True)

            self.slider_rel.setValue(int(preset_weights.get("rel", 0.35) * 100))
            self.slider_coll.setValue(int(preset_weights.get("coll", 0.25) * 100))
            self.slider_graph.setValue(int(preset_weights.get("graph", 0.20) * 100))
            self.slider_def.setValue(int(preset_weights.get("def", 0.20) * 100))

            for s in (self.slider_rel, self.slider_coll, self.slider_graph, self.slider_def):
                s.blockSignals(False)

            self.lbl_val_rel.setText(f"{self.slider_rel.value()}%")
            self.lbl_val_coll.setText(f"{self.slider_coll.value()}%")
            self.lbl_val_graph.setText(f"{self.slider_graph.value()}%")
            self.lbl_val_def.setText(f"{self.slider_def.value()}%")

    def _on_preset_changed(self, preset_name: str) -> None:
        self.card_active_strat.set_value(preset_name)
        self._sync_sliders_to_preset(preset_name)
        self.reload_crate()

    def _on_slider_moved(self) -> None:
        self.lbl_val_rel.setText(f"{self.slider_rel.value()}%")
        self.lbl_val_coll.setText(f"{self.slider_coll.value()}%")
        self.lbl_val_graph.setText(f"{self.slider_graph.value()}%")
        self.lbl_val_def.setText(f"{self.slider_def.value()}%")
        self.debounce_timer.start()

    def _exec_slider_rescore(self) -> None:
        def worker():
            try:
                custom_w = {
                    "rel": self.slider_rel.value() / 100.0,
                    "coll": self.slider_coll.value() / 100.0,
                    "graph": self.slider_graph.value() / 100.0,
                    "def": self.slider_def.value() / 100.0,
                }
                results = rescore_active_candidates(strategy=self.combo_preset.currentText(), custom_weights=custom_w, limit=250)
                self.pipeline_finished.emit(results)
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Slider rescore error: {ex}", "ERROR"))
                self.pipeline_finished.emit([])
            finally:
                close_thread_connection()

        threading.Thread(target=worker, daemon=True).start()

    def _on_accept_top_5(self) -> None:
        if not self.cached_results:
            return
        for cand, _ in self.cached_results[:5]:
            mark_candidate_state(cand.candidate_id, CandidateState.QUEUED)
        event_bus.publish(LogEvent("[+] Accepted top 5 discovery candidate releases.", "SUCCESS"))
        self.reload_crate()

    def reload_crate(self) -> None:
        state_idx = self.combo_state_filter.currentIndex()
        self.btn_run_pipeline.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.btn_run_pipeline.setText("[RUNNING PIPELINE...]")

        def worker():
            try:
                custom_w = {
                    "rel": self.slider_rel.value() / 100.0,
                    "coll": self.slider_coll.value() / 100.0,
                    "graph": self.slider_graph.value() / 100.0,
                    "def": self.slider_def.value() / 100.0,
                }

                if state_idx == 0:
                    results = self.pipeline.run_pipeline(
                        strategy=self.combo_preset.currentText(),
                        custom_weights=custom_w,
                        limit=250
                    )
                else:
                    target_state = "QUEUED" if state_idx == 1 else ("SNOOZED" if state_idx == 2 else "ACQUIRED")
                    results = self._fetch_candidates_by_state(target_state)

                self.pipeline_finished.emit(results)
            except Exception as ex:
                event_bus.publish(LogEvent(f"[-] Discovery pipeline error: {ex}", "ERROR"))
                self.pipeline_finished.emit([])
            finally:
                close_thread_connection()

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_candidates_by_state(self, state_name: str) -> List[Tuple[DiscoveryCandidate, DiscoveryScore]]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.candidate_id, c.release_group_mbid, c.title, c.artist_name, 
                   c.primary_genre, c.primary_subgenre, c.state, c.final_ccs, c.release_year,
                   s.v_rel, s.v_coll, s.v_graph, s.v_def, s.p_sat, s.delta_fatigue, s.active_strategy, s.explanation_json
            FROM sys_discovery_candidates c
            LEFT JOIN sys_discovery_scores s ON c.candidate_id = s.candidate_id
            WHERE c.state = %s
            ORDER BY c.final_ccs DESC LIMIT 250
        """, (state_name,))

        results = []
        for r in cursor.fetchall():
            cand = DiscoveryCandidate(
                candidate_id=r[0], release_group_mbid=r[1], title=r[2],
                artist_name=r[3], primary_genre=r[4] or "Unclassified",
                primary_subgenre=r[5] or "Unclassified",
                state=CandidateState(r[6]) if r[6] in CandidateState.__members__ else CandidateState.NEW,
                final_ccs=float(r[7] or 0.0), release_year=r[8]
            )
            score = DiscoveryScore(
                candidate_id=r[0], v_rel=float(r[9] or 0.8), v_coll=float(r[10] or 0.75),
                v_graph=float(r[11] or 0.8), v_def=float(r[12] or 0.7), p_sat=float(r[13] or 1.0),
                delta_fatigue=float(r[14] or 1.0), active_strategy=str(r[15] or "Balanced Curator"),
                explanation_json=str(r[16] or "{}")
            )
            results.append((cand, score))
        return results

    def _on_pipeline_finished(self, results: List[Tuple[DiscoveryCandidate, DiscoveryScore]]) -> None:
        self.btn_run_pipeline.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.btn_run_pipeline.setText("[RUN DISCOVERY PIPELINE]")
        self.cached_results = results
        self._update_kpi_cards(results)
        
        # Populate the dynamic genre filtering dropdown list
        self._populate_present_genres(results)
        
        self._apply_text_filter()

    def _populate_present_genres(self, results: List[Tuple[DiscoveryCandidate, DiscoveryScore]]) -> None:
        """Extracts unique genres present in active cards and populates the filter dropdown."""
        self.combo_genre_filter.blockSignals(True)
        
        current_selection = self.combo_genre_filter.currentText()
        
        self.combo_genre_filter.clear()
        self.combo_genre_filter.addItem("[ALL GENRES]")
        
        unique_genres = sorted(list(set(c.primary_genre for c, _ in results if c.primary_genre)))
        for g in unique_genres:
            self.combo_genre_filter.addItem(g)
            
        idx = self.combo_genre_filter.findText(current_selection)
        if idx >= 0:
            self.combo_genre_filter.setCurrentIndex(idx)
        else:
            self.combo_genre_filter.setCurrentIndex(0)
            
        self.combo_genre_filter.blockSignals(False)

    def _update_kpi_cards(self, results: List[Tuple[DiscoveryCandidate, DiscoveryScore]]) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sys_discovery_candidates WHERE state = 'SNOOZED'")
            snoozed_cnt = cursor.fetchone()[0] or 0

            top_score = results[0][0].final_ccs if results else 0.0

            self.card_crate_cnt.set_value(f"{len(results):,}", "Active items in crate")
            self.card_top_score.set_value(f"{top_score:.3f}", "Highest CCS candidate")
            self.card_snoozed_cnt.set_value(f"{snoozed_cnt:,}", "In 14-day hold queue")
        except Exception:
            pass

    def _apply_text_filter(self) -> None:
        filter_text = self.search_input.text().strip().lower()
        selected_genre = self.combo_genre_filter.currentText()

        filtered = []
        for c, s in self.cached_results:
            text_match = not filter_text or (
                filter_text in c.title.lower() or 
                filter_text in c.artist_name.lower() or 
                filter_text in c.primary_genre.lower() or 
                filter_text in c.primary_subgenre.lower()
            )
            
            genre_match = (selected_genre == "[ALL GENRES]") or (c.primary_genre == selected_genre)
            
            if text_match and genre_match:
                filtered.append((c, s))
                
        self._render_candidate_cards(filtered)

    def _clear_cards_layout(self) -> None:
        while self.cards_layout.count() > 0:
            item = self.cards_layout.takeAt(0)
            if item:
                widget = item.widget()
                if widget:
                    widget.deleteLater()

    def _render_candidate_cards(self, results: List[Tuple[DiscoveryCandidate, DiscoveryScore]]) -> None:
        self._clear_cards_layout()

        if not results:
            placeholder = QFrame()
            p_layout = QVBoxLayout(placeholder)
            p_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl = QLabel("NO CANDIDATES FOUND FOR SELECTED FILTER\nRun the Discovery Pipeline to generate new recommendations.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-family: 'Consolas', monospace; font-size: 9.5pt; color: #828a9a;")
            p_layout.addWidget(lbl)
            self.cards_layout.addWidget(placeholder)
            return

        for cand, score_obj in results:
            card = CandidateCard(cand, score_obj)
            card.action_triggered.connect(self.reload_crate)
            self.cards_layout.addWidget(card)

        self.cards_layout.addStretch()

    def _on_crawl_artist_clicked(self) -> None:
        """
        Dual-purpose Crawl trigger:
        1. If an artist name is typed, resolves and crawls that artist.
        2. If the artist is blank but a genre/tag filter is typed, harvests and crawls that genre!
        """
        artist_name = self.input_crawl_artist.text().strip()
        target_genre = self.input_target_genre.text().strip()

        if artist_name:
            try:
                conn = get_connection()
                with db_transaction() as tx:
                    dummy_id = f"name:{hashlib.md5(artist_name.lower().encode('utf-8')).hexdigest()[:12]}"
                    tx.execute("""
                        INSERT INTO sys_crawl_frontier (seed_id, entity_name, entity_type, priority, state)
                        VALUES (%s, %s, 'ARTIST', 1.5, 'PENDING')
                        ON CONFLICT (seed_id) DO UPDATE SET 
                            state = 'PENDING', 
                            priority = 1.5, 
                            updated_at = CURRENT_TIMESTAMP
                    """, (dummy_id, artist_name))
                
                self.input_crawl_artist.clear()
                event_bus.publish(LogEvent(f"[+] Crawler Queue: Added manual artist seed '{artist_name}' to frontier.", "SUCCESS"))
                self.reload_crate()
            except Exception as e:
                event_bus.publish(LogEvent(f"[-] Failed to enqueue crawl seed: {e}", "WARNING"))
        elif target_genre:
            event_bus.publish(LogEvent(f"[*] Discovery Crate: Searching Wikidata for popular '{target_genre}' artists to seed..."))
            threading.Thread(target=self._harvest_genre_seeds_background, args=(target_genre,), daemon=True).start()
        else:
            event_bus.publish(LogEvent("[-] Crawler: Enter an artist name or a crawl genre filter first.", "WARNING"))

    def _harvest_genre_seeds_background(self, genre_name: str) -> None:
        """Queries Wikidata SPARQL in a background thread to find popular artist seeds for a specific genre."""
        try:
            # SPARQL query matching music performers (P175) that match the user's genre string
            sparql_query = f"""
            SELECT DISTINCT ?artist ?artistLabel ?artistMBID WHERE {{
                ?artist wdt:P136 ?genre .
                ?genre rdfs:label ?genreLabel .
                ?artist wdt:P434 ?artistMBID .
                FILTER(LCASE(?genreLabel) = "{genre_name.lower().strip()}")
                FILTER(LANG(?genreLabel) = "en")
                ?artist rdfs:label ?artistLabel .
                FILTER(LANG(?artistLabel) = "en")
            }} LIMIT 15
            """
            params = {"query": sparql_query, "format": "json"}
            post_data = urllib.parse.urlencode(params).encode("utf-8")
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            from utils.net import execute_http_request
            res_data, _ = execute_http_request(
                "WIKIDATA", "https://query.wikidata.org/sparql", method="POST", post_data=post_data, headers=headers
            )
            if not res_data or not isinstance(res_data, dict):
                event_bus.publish(LogEvent(f"[-] Crawler: Failed to harvest genre seeds. Wikidata endpoint is busy.", "WARNING"))
                return

            bindings = res_data.get("results", {}).get("bindings", [])
            if not bindings:
                event_bus.publish(LogEvent(f"[-] Crawler: No popular artists found on Wikidata for genre '{genre_name}'. Check spelling.", "WARNING"))
                return

            added_count = 0
            with db_transaction() as tx:
                for b in bindings:
                    mbid = b.get("artistMBID", {}).get("value")
                    name = b.get("artistLabel", {}).get("value")
                    if mbid and name:
                        tx.execute("""
                            INSERT INTO sys_crawl_frontier (seed_id, entity_name, entity_type, priority, state)
                            VALUES (%s, %s, 'ARTIST', 1.2, 'PENDING')
                            ON CONFLICT (seed_id) DO UPDATE SET 
                                state = 'PENDING', 
                                priority = 1.2, 
                                updated_at = CURRENT_TIMESTAMP
                        """, (mbid, name))
                        added_count += 1

            if added_count > 0:
                event_bus.publish(LogEvent(f"[+] Crawler Queue: Harvested and enqueued {added_count} active artist seed(s) for genre '{genre_name}'!", "SUCCESS"))
                self.reload_crate()
            else:
                event_bus.publish(LogEvent(f"[-] Crawler: Zero new artist seeds enqueued for '{genre_name}'.", "WARNING"))

        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] Crawler: Background genre seeding failed: {ex}", "WARNING"))

    def _on_target_genre_changed(self, text: str) -> None:
        """Saves user targeted crawl genre dynamically inside sys_fastpass_cache for background daemon consumption."""
        try:
            conn = get_connection()
            with db_transaction() as tx:
                tx.execute("""
                    INSERT INTO sys_fastpass_cache (cache_key, result_json, created_at)
                    VALUES ('active_crawl_target_genre', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (cache_key) DO UPDATE SET 
                        result_json = EXCLUDED.result_json,
                        created_at = CURRENT_TIMESTAMP
                """, (text.strip(),))
        except Exception:
            pass

    def _load_cached_target_genre_filter(self) -> None:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM sys_fastpass_cache WHERE cache_key = 'active_crawl_target_genre'")
            row = cursor.fetchone()
            if row and row[0]:
                self.input_target_genre.blockSignals(True)
                self.input_target_genre.setText(str(row[0]).strip())
                self.input_target_genre.blockSignals(False)
        except Exception:
            pass

    @Slot(CrawlerTelemetryEvent)
    def _on_crawler_telemetry_updated(self, event: CrawlerTelemetryEvent) -> None:
        """Invoked on the GUI thread to update background crawl status indicators."""
        self.lbl_crawler_status.setText(f"CRAWLER: {event.active_seed.upper()} [FRONTIER: {event.pending_queue_size}]")
