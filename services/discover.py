"""
Discovery Backend and DSU Authority Graph Service.
"""

import json
import math
import time
import hashlib
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import List, Dict, Any, Optional, Set, Tuple

from config.settings import settings
from db.core import get_connection, db_transaction
from domain.models import (
    DiscoveryCandidate, DiscoveryScore, CandidateState, 
    MatchMethod, AuthorityEntityType, generate_ulid
)
from domain.events import event_bus, LogEvent
from utils.text import calculate_string_similarity, slugify_text

class DisjointSetUnion:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, i: str) -> str:
        if i not in self.parent:
            self.parent[i] = i
            return i
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: str, j: str) -> None:
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            self.parent[root_i] = root_j

def build_authority_dsu_clusters() -> Dict[str, str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT mbid, discogs_id, wikidata_qid FROM sys_authority_map WHERE is_active = 1")
    rows = cursor.fetchall()

    dsu = DisjointSetUnion()
    for mbid, discogs_id, wiki_qid in rows:
        primary_id = mbid or (f"discogs:{discogs_id}" if discogs_id else (f"wiki:{wiki_qid}" if wiki_qid else None))
        if not primary_id:
            continue
        if mbid: dsu.union(primary_id, mbid)
        if discogs_id: dsu.union(primary_id, f"discogs:{discogs_id}")
        if wiki_qid: dsu.union(primary_id, f"wiki:{wiki_qid}")

    return {k: dsu.find(k) for k in dsu.parent}


class LocalLibraryIndexCache:
    def __init__(self) -> None:
        self.mbids: Set[str] = set()
        self.barcodes: Set[str] = set()
        self.catalog_nos: Set[str] = set()
        self.titles_by_prefix: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    def reload(self) -> None:
        self.mbids.clear()
        self.barcodes.clear()
        self.catalog_nos.clear()
        self.titles_by_prefix.clear()

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT musicbrainz_recording_id FROM core_recordings WHERE musicbrainz_recording_id IS NOT NULL AND musicbrainz_recording_id != ''")
        self.mbids.update(r[0] for r in cursor.fetchall() if r[0])

        cursor.execute("SELECT release_group_mbid, musicbrainz_release_id, barcode, catalog_number FROM core_albums")
        for rg_mbid, rel_mbid, bc, cat in cursor.fetchall():
            if rg_mbid: self.mbids.add(str(rg_mbid))
            if rel_mbid: self.mbids.add(str(rel_mbid))
            if bc: self.barcodes.add(str(bc).strip())
            if cat: self.catalog_nos.add(str(cat).strip())

        # Index individual track/recording titles
        cursor.execute("SELECT title, artist_name FROM vw_recording_overview")
        for t_title, a_name in cursor.fetchall():
            if t_title:
                clean_t = "".join(c.lower() for c in str(t_title) if c.isalnum())
                prefix = clean_t[:3] if len(clean_t) >= 3 else clean_t
                self.titles_by_prefix[prefix].append((str(a_name or ""), str(t_title)))

        # Aligns album titles into the exact same fuzzy checker to prevent candidate duplications
        cursor.execute("""
            SELECT alb.title, COALESCE(a.name, 'Unknown Artist') 
            FROM core_albums alb
            LEFT JOIN core_artists a ON alb.artist_id = a.id
        """)
        for a_title, a_name in cursor.fetchall():
            if a_title:
                clean_t = "".join(c.lower() for c in str(a_title) if c.isalnum())
                prefix = clean_t[:3] if len(clean_t) >= 3 else clean_t
                self.titles_by_prefix[prefix].append((str(a_name or ""), str(a_title)))

    def is_exact_owned(
        self,
        mbid: Optional[str] = None,
        barcode: Optional[str] = None,
        catalog_no: Optional[str] = None
    ) -> bool:
        if mbid and mbid in self.mbids:
            return True
        if barcode and barcode.strip() in self.barcodes:
            return True
        if catalog_no and catalog_no.strip() in self.catalog_nos:
            return True
        return False

    def is_fuzzy_owned(self, title: str, artist: str, threshold: float = 0.85) -> bool:
        clean_t = "".join(c.lower() for c in str(title) if c.isalnum())
        prefix = clean_t[:3] if len(clean_t) >= 3 else clean_t
        candidates = self.titles_by_prefix.get(prefix, [])

        for lib_artist, lib_title in candidates:
            t_sim = calculate_string_similarity(title, lib_title)
            if t_sim >= threshold:
                a_sim = calculate_string_similarity(artist, lib_artist)
                if a_sim >= 0.70:
                    return True
        return False


def compute_graph_confidence(
    mbid: Optional[str],
    discogs_id: Optional[str],
    wikidata_qid: Optional[str],
    lastfm_name: Optional[str]
) -> float:
    identifiers = [mbid, discogs_id, wikidata_qid, lastfm_name]
    valid_count = sum(1 for item in identifiers if item and str(item).strip())
    return round(min(1.0, max(0.20, valid_count * 0.25)), 2)

def calculate_genre_relevance_score(genre: str, subgenre: str, cursor: Any) -> float:
    if not genre or genre == "Unclassified":
        return 0.40
    cursor.execute("SELECT COUNT(*) FROM vw_recording_overview WHERE primary_genre = %s", (genre,))
    root_cnt = cursor.fetchone()[0] or 0
    if root_cnt == 0:
        return 0.30
    
    sub_cnt = 0
    if subgenre and subgenre != "Unclassified":
        cursor.execute("SELECT COUNT(*) FROM vw_recording_overview WHERE primary_subgenre = %s", (subgenre,))
        sub_cnt = cursor.fetchone()[0] or 0

    affinity_score = min(1.0, 0.40 + (root_cnt / 100.0) * 0.40 + (sub_cnt / 20.0) * 0.20)
    return round(affinity_score, 2)

def calculate_subgenre_deficit_score(subgenre: str, cursor: Any) -> float:
    if not subgenre or subgenre == "Unclassified":
        return 0.50
    cursor.execute("SELECT COUNT(*) FROM vw_recording_overview WHERE primary_subgenre = %s", (subgenre,))
    count = cursor.fetchone()[0] or 0
    if count == 0:
        return 1.00
    elif count < 5:
        return 0.85
    elif count < 20:
        return 0.65
    return 0.35

def calculate_artist_saturation_penalty(artist_name: str, cursor: Any) -> float:
    if not artist_name or artist_name.lower() == "unknown artist":
        return 1.00
    cursor.execute("SELECT COUNT(*) FROM vw_recording_overview WHERE LOWER(artist_name) = LOWER(%s)", (artist_name.strip(),))
    count = cursor.fetchone()[0] or 0
    if count >= 30:
        return 0.40
    elif count >= 15:
        return 0.60
    elif count >= 5:
        return 0.80
    return 1.00

def calculate_user_fatigue_decay(candidate_id: str, cursor: Any) -> float:
    cursor.execute("SELECT computed_at FROM sys_discovery_scores WHERE candidate_id = %s", (candidate_id,))
    row = cursor.fetchone()
    if not row or not row[0]:
        return 1.00
    return 0.90

def generate_seed_candidates_if_empty(cursor: Any) -> int:
    cursor.execute("SELECT COUNT(*) FROM sys_discovery_candidates WHERE state IN ('NEW', 'QUEUED')")
    count = cursor.fetchone()[0] or 0
    if count >= 10:
        return 0

    sample_seeds = [
        ("Selected Ambient Works 85-92", "Aphex Twin", "sub_ambient", "Electronic", "Ambient"),
        ("Loveless", "My Bloody Valentine", "sub_shoegaze", "Rock", "Shoegaze"),
        ("Dots and Loops", "Stereolab", "sub_post_rock", "Rock", "Post-Rock"),
        ("Untrue", "Burial", "sub_idm", "Electronic", "IDM"),
        ("Dummy", "Portishead", "sub_trip_hop", "Electronic", "Trip Hop"),
        ("Souvlaki", "Slowdive", "sub_shoegaze", "Rock", "Shoegaze"),
        ("Geogaddi", "Boards of Canada", "sub_idm", "Electronic", "IDM"),
        ("Laughing Stock", "Talk Talk", "sub_post_rock", "Rock", "Post-Rock")
    ]

    seeded = 0
    for title, artist, sub_id, main_g, sub_g in sample_seeds:
        c_id = f"cand_{hashlib.md5((title + artist).encode('utf-8')).hexdigest()[:10]}"
        cursor.execute("""
            INSERT INTO sys_discovery_candidates (
                candidate_id, title, artist_name, primary_genre, primary_subgenre, state, final_ccs
            ) VALUES (%s, %s, %s, %s, %s, 'QUEUED', 0.85)
            ON CONFLICT (candidate_id) DO NOTHING
        """, (c_id, title, artist, main_g, sub_g))
        seeded += 1

    return seeded


class CompositeCandidateScorer:
    STRATEGY_PRESETS = {
        "Balanced Curator": {"rel": 0.35, "coll": 0.25, "graph": 0.20, "def": 0.20},
        "Completionist": {"rel": 0.50, "coll": 0.15, "graph": 0.25, "def": 0.10},
        "Genre Explorer": {"rel": 0.20, "coll": 0.20, "graph": 0.20, "def": 0.40},
        "Crate Digger": {"rel": 0.15, "coll": 0.45, "graph": 0.15, "def": 0.25}
    }

    @staticmethod
    def score_candidate(
        cand: DiscoveryCandidate,
        v_rel: float,
        v_graph: float,
        v_def: float,
        p_sat: float,
        delta_fatigue: float = 1.0,
        user_strategy: str = "Balanced Curator",
        custom_weights: Optional[Dict[str, float]] = None
    ) -> Tuple[float, DiscoveryScore]:
        if custom_weights and isinstance(custom_weights, dict):
            raw_weights = custom_weights
        else:
            raw_weights = CompositeCandidateScorer.STRATEGY_PRESETS.get(
                user_strategy, CompositeCandidateScorer.STRATEGY_PRESETS["Balanced Curator"]
            )

        w_sum = sum(raw_weights.values()) or 1.0
        norm_w = {k: v / w_sum for k, v in raw_weights.items()}

        v_coll = 0.78

        raw_ccs = (
            (norm_w.get("rel", 0.35) * v_rel) +
            (norm_w.get("coll", 0.25) * v_coll) +
            (norm_w.get("graph", 0.20) * v_graph) +
            (norm_w.get("def", 0.20) * v_def)
        ) * p_sat * delta_fatigue

        final_ccs = round(min(1.0, max(0.0, raw_ccs)), 3)
        margin_delta = round(0.04 + (1.0 - v_graph) * 0.10, 3)

        explanation = {
            "active_strategy": user_strategy,
            "vector_weights": norm_w,
            "scores": {"v_rel": v_rel, "v_coll": v_coll, "v_graph": v_graph, "v_def": v_def},
            "penalties": {"p_sat": p_sat, "delta_fatigue": delta_fatigue},
            "uncertainty_margin": margin_delta
        }

        score_obj = DiscoveryScore(
            candidate_id=cand.candidate_id,
            v_rel=v_rel, v_coll=v_coll, v_graph=v_graph, v_def=v_def,
            p_sat=p_sat, delta_fatigue=delta_fatigue, provider_factor=1.0,
            active_strategy=user_strategy, explanation_json=json.dumps(explanation)
        )

        return final_ccs, score_obj


class DiscoveryEnginePipeline:
    def __init__(self) -> None:
        self.library_index = LocalLibraryIndexCache()

    def run_pipeline(
        self,
        strategy: str = "Balanced Curator",
        custom_weights: Optional[Dict[str, float]] = None,
        limit: int = 50
    ) -> List[Tuple[DiscoveryCandidate, DiscoveryScore]]:
        self.library_index.reload()
        conn = get_connection()
        cursor = conn.cursor()

        with db_transaction() as seed_tx:
            generate_seed_candidates_if_empty(seed_tx)

        cursor.execute("""
            SELECT candidate_id, release_group_mbid, title, artist_name, primary_genre, primary_subgenre, state 
            FROM sys_discovery_candidates 
            WHERE state NOT IN ('IGNORED', 'ACQUIRED')
              AND (state != 'SNOOZED' OR snoozed_until IS NULL OR snoozed_until <= CURRENT_TIMESTAMP)
        """)
        raw_candidates = cursor.fetchall()

        scored_results: List[Tuple[DiscoveryCandidate, DiscoveryScore]] = []

        with db_transaction() as tx:
            for c_row in raw_candidates:
                cand_id, rg_mbid, title, artist, p_gen, p_sub, curr_state = c_row

                if self.library_index.is_exact_owned(mbid=rg_mbid):
                    tx.execute("UPDATE sys_discovery_candidates SET state = 'ACQUIRED', updated_at = CURRENT_TIMESTAMP WHERE candidate_id = %s", (cand_id,))
                    continue

                if self.library_index.is_fuzzy_owned(title, artist):
                    tx.execute("UPDATE sys_discovery_candidates SET state = 'ACQUIRED', updated_at = CURRENT_TIMESTAMP WHERE candidate_id = %s", (cand_id,))
                    continue

                v_rel = calculate_genre_relevance_score(p_gen, p_sub, tx)
                v_graph = compute_graph_confidence(rg_mbid, None, None, artist)
                v_def = calculate_subgenre_deficit_score(p_sub, tx)
                p_sat = calculate_artist_saturation_penalty(artist, tx)
                delta_fatigue = calculate_user_fatigue_decay(cand_id, tx)

                cand_state = CandidateState.NEW
                if curr_state:
                    try:
                        cand_state = CandidateState(curr_state)
                    except ValueError:
                        cand_state = CandidateState.__members__.get(curr_state, CandidateState.NEW)

                cand_obj = DiscoveryCandidate(
                    candidate_id=cand_id,
                    title=title,
                    artist_name=artist,
                    release_group_mbid=rg_mbid,
                    primary_genre=p_gen or "Unclassified",
                    primary_subgenre=p_sub or "Unclassified",
                    state=cand_state
                )

                final_ccs, score_obj = CompositeCandidateScorer.score_candidate(
                    cand_obj, v_rel=v_rel, v_graph=v_graph, v_def=v_def, p_sat=p_sat, 
                    delta_fatigue=delta_fatigue, user_strategy=strategy, custom_weights=custom_weights
                )
                cand_obj.final_ccs = final_ccs
                cand_obj.overall_confidence = v_graph

                tx.execute("""
                    UPDATE sys_discovery_candidates 
                    SET final_ccs = %s, overall_confidence = %s, updated_at = CURRENT_TIMESTAMP 
                    WHERE candidate_id = %s
                """, (final_ccs, v_graph, cand_id))

                tx.execute("""
                    INSERT INTO sys_discovery_scores (
                        candidate_id, v_rel, v_coll, v_graph, v_def, p_sat, 
                        delta_fatigue, provider_factor, active_strategy, explanation_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(candidate_id) DO UPDATE SET 
                        v_rel = EXCLUDED.v_rel, v_coll = EXCLUDED.v_coll, 
                        v_graph = EXCLUDED.v_graph, v_def = EXCLUDED.v_def, 
                        p_sat = EXCLUDED.p_sat, delta_fatigue = EXCLUDED.delta_fatigue, 
                        provider_factor = EXCLUDED.provider_factor, 
                        active_strategy = EXCLUDED.active_strategy, 
                        explanation_json = EXCLUDED.explanation_json, 
                        computed_at = CURRENT_TIMESTAMP
                """, (
                    score_obj.candidate_id, score_obj.v_rel, score_obj.v_coll, score_obj.v_graph, 
                    score_obj.v_def, score_obj.p_sat, score_obj.delta_fatigue, score_obj.provider_factor, 
                    score_obj.active_strategy, score_obj.explanation_json
                ))

                scored_results.append((cand_obj, score_obj))

        scored_results.sort(key=lambda x: x[0].final_ccs, reverse=True)
        return scored_results[:limit]

def rescore_active_candidates(
    strategy: str = "Balanced Curator",
    custom_weights: Optional[Dict[str, float]] = None,
    limit: int = 50
) -> List[Tuple[DiscoveryCandidate, DiscoveryScore]]:
    pipeline = DiscoveryEnginePipeline()
    return pipeline.run_pipeline(strategy=strategy, custom_weights=custom_weights, limit=limit)

def mark_candidate_state(candidate_id: str, new_state: CandidateState) -> bool:
    if not candidate_id:
        return False
    try:
        with db_transaction() as tx:
            tx.execute("""
                UPDATE sys_discovery_candidates 
                SET state = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE candidate_id = %s
            """, (new_state.value, candidate_id))
        event_bus.publish(LogEvent(f"[+] Discovery candidate {candidate_id[:8]} state updated to {new_state.value}.", "SUCCESS"))
        return True
    except Exception as ex:
        event_bus.publish(LogEvent(f"[-] Failed to update candidate state: {ex}", "ERROR"))
        return False

def snooze_candidate(candidate_id: str, days: int = 14) -> bool:
    if not candidate_id:
        return False
    try:
        snooze_until_dt = datetime.now(timezone.utc) + timedelta(days=days)
        snooze_until_str = snooze_until_dt.strftime("%Y-%m-%d %H:%M:%S%z")
        with db_transaction() as tx:
            tx.execute("""
                UPDATE sys_discovery_candidates 
                SET state = 'SNOOZED', snoozed_until = %s, updated_at = CURRENT_TIMESTAMP 
                WHERE candidate_id = %s
            """, (snooze_until_str, candidate_id))
        event_bus.publish(LogEvent(f"[+] Candidate {candidate_id[:8]} snoozed for {days} days until {snooze_until_str}.", "SUCCESS"))
        return True
    except Exception as ex:
        event_bus.publish(LogEvent(f"[-] Failed to snooze candidate: {ex}", "ERROR"))
        return False
