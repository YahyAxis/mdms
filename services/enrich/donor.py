"""
Intra-Album Sibling Metadata Propagation.
"""

from typing import Dict, Any, Optional, Set
from db import get_connection, db_transaction
from domain.models import Evidence
from services.enrich.evidence import EvidenceBuffer
from services.resolve import SymbolicInferenceEngine, ResolutionPersistenceAdapter, get_current_field_value

class IntraAlbumEngine:
    @staticmethod
    def propagate_from_donor(
        donor_recording_id: str,
        run_id: str,
        min_donor_quality: float = 1.0
    ) -> Dict[str, int]:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT quality_score FROM meta_validation WHERE recording_id = %s", (donor_recording_id,))
        q_row = cursor.fetchone()
        if not q_row or float(q_row[0]) < min_donor_quality:
            return {"siblings_updated": 0, "fields_propagated": 0}

        # Query native genre and subgenre columns directly from the core recordings schema
        cursor.execute("""
            SELECT r.album_id, r.artist_id, COALESCE(a.name, ''), COALESCE(alb.title, ''),
                   r.release_date, alb.original_release_date, alb.musicbrainz_release_id,
                   (SELECT value FROM meta_evidence WHERE entity_id = r.id AND field_name = 'country' LIMIT 1),
                   (SELECT value FROM meta_evidence WHERE entity_id = r.id AND field_name = 'catalog_number' LIMIT 1),
                   (SELECT value FROM meta_evidence WHERE entity_id = r.id AND field_name = 'barcode' LIMIT 1),
                   r.genre, r.subgenre
            FROM core_recordings r
            LEFT JOIN core_artists a ON r.artist_id = a.id
            LEFT JOIN core_albums alb ON r.album_id = alb.id
            WHERE r.id = %s
        """, (donor_recording_id,))
        donor = cursor.fetchone()

        if not donor or not donor[0]:
            return {"siblings_updated": 0, "fields_propagated": 0}

        album_id, artist_id, donor_art_name, donor_alb_title = donor[0], donor[1], donor[2], donor[3]
        donor_rel_date, donor_orig_date, mb_release_id = donor[4], donor[5], donor[6]
        donor_country, donor_catno, donor_barcode, donor_genre, donor_subgenre = donor[7], donor[8], donor[9], donor[10], donor[11]

        if not donor_genre or donor_genre == "Unclassified":
            return {"siblings_updated": 0, "fields_propagated": 0}

        cursor.execute("SELECT COUNT(DISTINCT artist_id) FROM core_recordings WHERE album_id = %s", (album_id,))
        artist_count = cursor.fetchone()[0] or 0
        art_lower = donor_art_name.lower()
        alb_lower = donor_alb_title.lower()
        is_compilation = (artist_count >= 3) or ("various" in art_lower) or ("v.a." in art_lower) or ("compilation" in alb_lower)

        cursor.execute("""
            SELECT r.id FROM core_recordings r
            LEFT JOIN meta_validation v ON r.id = v.recording_id
            WHERE r.album_id = %s AND r.id != %s AND COALESCE(v.quality_score, 0.0) < 1.0
        """, (album_id, donor_recording_id))
        siblings = cursor.fetchall()

        siblings_updated = 0
        total_fields_propagated = 0

        with db_transaction() as tx:
            for sib_row in siblings:
                sib_id = sib_row[0]

                tx.execute("SELECT field_name FROM meta_locks WHERE entity_id = %s AND lock_state IN ('MANUAL', 'PROTECTED')", (sib_id,))
                locked_fields = {r[0] for r in tx.fetchall()}

                buf = EvidenceBuffer(sib_id, run_id)
                fields_added_set: Set[str] = set()

                def _try_add(f_name: str, val: Optional[str], conf: float) -> None:
                    if f_name not in locked_fields and val:
                        buf.add(f_name, val, "DERIVED", "SRC_ALBUM_SIBLING", confidence=conf)
                        fields_added_set.add(f_name)

                _try_add("album", donor_alb_title, 0.92)
                _try_add("release_date", donor_rel_date, 0.92)
                _try_add("original_release_date", donor_orig_date, 0.99)
                _try_add("country", donor_country, 0.92)
                _try_add("catalog_number", donor_catno, 0.92)
                _try_add("barcode", donor_barcode, 0.92)
                _try_add("musicbrainz_release_id", mb_release_id, 0.92)
                _try_add("genre", donor_genre, 0.85)
                if donor_subgenre and donor_subgenre != "Unclassified":
                    _try_add("subgenre", donor_subgenre, 0.85)

                if not is_compilation and donor_art_name:
                    _try_add("artist", donor_art_name, 0.92)

                fields_added = buf.commit(cursor=tx)
                if fields_added > 0:
                    total_fields_propagated += fields_added
                    for field_name in fields_added_set:
                        tx.execute("""
                            SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                                   payload_hash, confidence, origin_type, observed_at, raw_value, 
                                   token_index, token_delimiter, positional_weight 
                            FROM meta_evidence WHERE entity_id = %s AND field_name = %s
                        """, (sib_id, field_name))
                        ev_rows = [Evidence(
                            id=r[0], entity_id=r[1], field_name=r[2], value=r[3], evidence_class=r[4],
                            source_id=r[5], run_id=r[6], payload_hash=r[7], confidence=r[8], origin_type=r[9],
                            observed_at=str(r[10]), raw_value=r[11], token_index=r[12], token_delimiter=r[13],
                            positional_weight=r[14]
                        ) for r in tx.fetchall()]

                        if ev_rows:
                            curr_v = get_current_field_value(tx, sib_id, field_name)
                            decision = SymbolicInferenceEngine.resolve_field(sib_id, field_name, curr_v, ev_rows, run_id)
                            ResolutionPersistenceAdapter.apply_decision(decision)

                    siblings_updated += 1

        return {"siblings_updated": siblings_updated, "fields_propagated": total_fields_propagated}

    @staticmethod
    def propagate_album_cluster(album_id: str, run_id: str) -> Dict[str, int]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r.id, COALESCE(v.quality_score, 0.0) AS q
            FROM core_recordings r
            LEFT JOIN meta_validation v ON r.id = v.recording_id
            WHERE r.album_id = %s
            ORDER BY q DESC LIMIT 1
        """, (album_id,))
        top_row = cursor.fetchone()

        if not top_row or float(top_row[1]) < 0.90:
            return {"siblings_updated": 0, "fields_propagated": 0}

        return IntraAlbumEngine.propagate_from_donor(top_row[0], run_id, min_donor_quality=float(top_row[1]))
