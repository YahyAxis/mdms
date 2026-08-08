"""
Studio Album Cluster Pre-Fetch Service
Executes Stage 1 album-level release group queries, tracklist position matching, and release tag distribution.
"""

from typing import List, Tuple, Optional, Dict, Any
from db import get_connection, db_transaction
from services.tax import normalize_tag_alias
from services.enrich.api import ExternalAPIService, calculate_similarity
from services.enrich.evidence import EvidenceBuffer

def enrich_album_cluster(
    cluster_items: List[Tuple[str, str, str, int, Optional[str]]],
    run_id: str
) -> Dict[str, Any]:
    """
    Pre-fetches official MusicBrainz release group metadata, tracklists, barcodes, and labels
    for an album cluster, applying release-level evidence to all cluster tracks.
    """
    if not cluster_items:
        return {"tracks_matched": 0, "tags_applied": 0}
    
    first_rec_id = cluster_items[0][0]
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT value FROM meta_evidence WHERE entity_id = %s AND field_name = 'artist' LIMIT 1", (first_rec_id,))
    art_row = cursor.fetchone()
    cursor.execute("SELECT value FROM meta_evidence WHERE entity_id = %s AND field_name = 'album' LIMIT 1", (first_rec_id,))
    alb_row = cursor.fetchone()

    art_name = art_row[0] if art_row else None
    alb_title = alb_row[0] if alb_row else None

    if not art_name or not alb_title:
        return {"tracks_matched": 0, "tags_applied": 0}

    try:
        rg = ExternalAPIService.query_musicbrainz_release_group(art_name, alb_title)
        if not rg or not isinstance(rg, dict) or not rg.get("id"):
            return {"tracks_matched": 0, "tags_applied": 0}

        tracklist_info = ExternalAPIService.query_musicbrainz_release_tracklist(rg["id"])
        if not tracklist_info or not isinstance(tracklist_info, dict):
            return {"tracks_matched": 0, "tags_applied": 0}

        official_tracks = tracklist_info.get("tracks", [])
        cluster_tags = tracklist_info.get("tags", [])
        first_rel_date = tracklist_info.get("first_release_date")

        tracks_matched = 0
        total_tags_applied = 0

        with db_transaction() as tx:
            for rec_id, fpath, local_title, lock_val, _ in cluster_items:
                tx.execute("SELECT track_number, disc_number FROM core_recordings WHERE id = %s", (rec_id,))
                trk_row = tx.fetchone()
                local_pos = trk_row[0] if trk_row else None

                best_match = None
                best_score = -1.0
                for mb_trk in official_tracks:
                    if not isinstance(mb_trk, dict):
                        continue
                    mb_title = mb_trk.get("title", "")
                    mb_pos = mb_trk.get("position")
                    
                    title_sim = calculate_similarity(local_title, mb_title)
                    pos_match = 1.0 if (local_pos and mb_pos and int(local_pos) == int(mb_pos)) else 0.0
                    score = (0.70 * title_sim) + (0.30 * pos_match)

                    if score > best_score:
                        best_score = score
                        best_match = mb_trk

                buf = EvidenceBuffer(rec_id, run_id)
                for idx, c_tag in enumerate(cluster_tags):
                    clean_norm = normalize_tag_alias(c_tag)
                    if clean_norm:
                        pos_w = round(0.75 ** idx, 4)
                        buf.add("genre", clean_norm, "REMOTE", "SRC_MUSICBRAINZ", confidence=0.80 * pos_w, token_index=idx)

                if first_rel_date:
                    buf.add("original_release_date", first_rel_date, "REMOTE", "SRC_MUSICBRAINZ", confidence=0.99)

                if best_match and best_score >= 0.40:
                    if best_match.get("recording_mbid"):
                        buf.add("musicbrainz_recording_id", best_match["recording_mbid"], "REMOTE", "SRC_MUSICBRAINZ", confidence=0.98)
                    if best_match.get("isrc"):
                        buf.add("isrc", best_match["isrc"], "REMOTE", "SRC_MUSICBRAINZ", confidence=0.99)
                    tracks_matched += 1

                tags_count = buf.commit(cursor=tx)
                total_tags_applied += tags_count

        return {"tracks_matched": tracks_matched, "tags_applied": total_tags_applied}
    except Exception:
        return {"tracks_matched": 0, "tags_applied": 0}