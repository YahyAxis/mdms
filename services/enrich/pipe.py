"""
Stage B Canonical Enrichment Orchestration
Fetches MusicBrainz, AcoustID, Deezer, Discogs, and Last.fm evidence via parallel workers,
resolving metadata decisions directly into native database columns.
Updated to trigger automatic owned-candidate (ACQUIRED) sweeps upon completion.
"""

import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional, Set, Dict, Any

from config.settings import settings
from domain.models import Evidence, TelemetryEvent, generate_ulid
from domain.events import event_bus, LogEvent
from db import get_connection, db_transaction, close_thread_connection, ArtistRepo
from utils.net import _disk_cache
from utils.fpcalc import generate_chromaprint, ensure_fpcalc
from utils.profile import get_system_resource_telemetry
from utils.text import sanitize_artist_name
from services.tax import TaxonomyService, normalize_tag_alias
from services.resolve import SymbolicInferenceEngine, ResolutionPersistenceAdapter, get_current_field_value
from services.enrich.api import ExternalAPIService, _artist_cache
from services.enrich.evidence import EvidenceBuffer
from services.enrich.cluster import enrich_album_cluster
from services.enrich.donor import IntraAlbumEngine

ENRICHMENT_VERSION = "5.0.0"

def get_artist_repo():
    return ArtistRepo()

def update_acquired_candidates_from_library() -> int:
    """
    Sweeps active discovery candidates against newly enriched local recordings.
    If matches are found, updates their status to ACQUIRED to hide them from the crate.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT candidate_id, release_group_mbid, title, artist_name 
        FROM sys_discovery_candidates 
        WHERE state NOT IN ('ACQUIRED', 'IGNORED')
    """)
    candidates = cursor.fetchall()
    if not candidates:
        return 0

    cursor.execute("""
        SELECT r.musicbrainz_recording_id, r.title, COALESCE(a.name, ''), alb.release_group_mbid
        FROM core_recordings r
        LEFT JOIN core_artists a ON r.artist_id = a.id
        LEFT JOIN core_albums alb ON r.album_id = alb.id
    """)
    owned_rows = cursor.fetchall()

    owned_mbids = set()
    owned_fuzzy = set()

    for mb_rec_id, title, artist_name, rg_mbid in owned_rows:
        if mb_rec_id: owned_mbids.add(mb_rec_id)
        if rg_mbid: owned_mbids.add(rg_mbid)
        if title and artist_name:
            clean_t = "".join(c.lower() for c in str(title) if c.isalnum())
            clean_a = "".join(c.lower() for c in str(artist_name) if c.isalnum())
            owned_fuzzy.add((clean_t, clean_a))

    acquired_ids = []
    for cand_id, rg_mbid, title, artist in candidates:
        is_acquired = False
        if rg_mbid and rg_mbid in owned_mbids:
            is_acquired = True
        else:
            clean_ct = "".join(c.lower() for c in str(title) if c.isalnum())
            clean_ca = "".join(c.lower() for c in str(artist) if c.isalnum())
            if (clean_ct, clean_ca) in owned_fuzzy:
                is_acquired = True

        if is_acquired:
            acquired_ids.append(cand_id)

    if acquired_ids:
        with db_transaction() as tx:
            placeholders = ",".join("%s" for _ in acquired_ids)
            tx.execute(f"""
                UPDATE sys_discovery_candidates 
                SET state = 'ACQUIRED', updated_at = CURRENT_TIMESTAMP 
                WHERE candidate_id IN ({placeholders})
            """, acquired_ids)

    return len(acquired_ids)


class EnrichmentEngine:
    _cancel_requested = False

    @classmethod
    def cancel_current(cls) -> None:
        cls._cancel_requested = True

    @staticmethod
    def _enrich_single_track_worker(rec_item: Tuple[str, str, str, int, Optional[str]], run_id: str, is_full_mode: bool) -> Dict[str, Any]:
        if EnrichmentEngine._cancel_requested:
            return {"success": False, "rec_id": rec_item[0], "facts_added": 0}

        rec_id, filepath, current_title, is_locked, alb_id = rec_item
        conn = get_connection()
        cursor = conn.cursor()

        trace_prefix = f"[Trk {rec_id[:8]}]"

        try:
            ev_buffer = EvidenceBuffer(rec_id, run_id)

            cursor.execute("SELECT duration FROM core_assets WHERE recording_id = %s LIMIT 1", (rec_id,))
            dur_row = cursor.fetchone()
            local_duration = float(dur_row[0]) if (dur_row and dur_row[0]) else None

            cursor.execute("SELECT value FROM meta_evidence WHERE entity_id = %s AND field_name = 'artist' LIMIT 1", (rec_id,))
            art_row = cursor.fetchone()
            cursor.execute("SELECT value FROM meta_evidence WHERE entity_id = %s AND field_name = 'album' LIMIT 1", (rec_id,))
            alb_row = cursor.fetchone()

            search_artist = sanitize_artist_name(art_row[0]) if art_row and art_row[0] else None
            search_album = alb_row[0] if alb_row else None

            cursor.execute("""
                SELECT value FROM meta_evidence 
                WHERE entity_id = %s AND field_name = 'musicbrainz_recording_id' 
                  AND source_id IN ('SRC_MUSICBRAINZ', 'SRC_MUSICBRAINZ_ISRC', 'SRC_MUSICBRAINZ_SEARCH', 'SRC_ACOUSTID', 'SRC_USER')
                  AND confidence >= 0.80
                ORDER BY confidence DESC LIMIT 1
            """, (rec_id,))
            mb_trust_row = cursor.fetchone()
            has_trusted_mbid = bool(mb_trust_row and mb_trust_row[0])
            mb_rec_id = mb_trust_row[0] if has_trusted_mbid else None

            cursor.execute("""
                SELECT value FROM meta_evidence 
                WHERE entity_id = %s AND field_name = 'isrc' 
                  AND source_id IN ('SRC_MUSICBRAINZ', 'SRC_MUSICBRAINZ_ISRC', 'SRC_DEEZER', 'SRC_USER')
                  AND confidence >= 0.80
                ORDER BY confidence DESC LIMIT 1
            """, (rec_id,))
            isrc_trust_row = cursor.fetchone()
            has_trusted_isrc = bool(isrc_trust_row and isrc_trust_row[0])
            isrc_val = isrc_trust_row[0] if has_trusted_isrc else None

            cursor.execute("SELECT COUNT(*) FROM meta_evidence WHERE entity_id = %s AND field_name = 'catalog_number'", (rec_id,))
            has_catno = cursor.fetchone()[0] > 0

            # 1. AcoustID Chromaprint
            if not has_trusted_mbid and filepath and os.path.exists(filepath) and not EnrichmentEngine._cancel_requested:
                duration, fingerprint = generate_chromaprint(filepath)
                if fingerprint and duration:
                    acoustid_id, mb_from_acoustid, fp_payload_hash = ExternalAPIService.query_acoustid(duration, fingerprint, search_artist, current_title)
                    if acoustid_id: 
                        ev_buffer.add("acoustid_id", acoustid_id, "REMOTE", "SRC_ACOUSTID", fp_payload_hash, confidence=0.85)
                        event_bus.publish(LogEvent(f"{trace_prefix} [AcoustID] Hit ID [{acoustid_id[:8]}...] (Linked MBID: {mb_from_acoustid or 'None'})", "INFO"))

                    if mb_from_acoustid and not mb_rec_id: 
                        mb_rec_id = mb_from_acoustid
                        ev_buffer.add("musicbrainz_recording_id", mb_from_acoustid, "REMOTE", "SRC_ACOUSTID", fp_payload_hash, confidence=0.85)

            # 2. ISRC Lookup via MusicBrainz
            if not mb_rec_id and isrc_val and not EnrichmentEngine._cancel_requested:
                mb_rec_data, isrc_mbid, isrc_hash = ExternalAPIService.query_musicbrainz_by_isrc(isrc_val)
                if isrc_mbid:
                    mb_rec_id = isrc_mbid
                    ev_buffer.add("musicbrainz_recording_id", isrc_mbid, "REMOTE", "SRC_MUSICBRAINZ_ISRC", isrc_hash, confidence=0.99)
                    event_bus.publish(LogEvent(f"{trace_prefix} [ISRC Match] Hit MBID [{isrc_mbid[:8]}...] via ISRC {isrc_val}", "SUCCESS"))

            # 3. Title Search via MusicBrainz
            if not mb_rec_id and current_title and not EnrichmentEngine._cancel_requested:
                search_data, search_mbid, search_hash = ExternalAPIService.query_musicbrainz_by_search(current_title, search_artist, search_album, local_duration)
                if search_mbid:
                    mb_rec_id = search_mbid
                    ev_buffer.add("musicbrainz_recording_id", search_mbid, "REMOTE", "SRC_MUSICBRAINZ_SEARCH", search_hash, confidence=0.90)
                    event_bus.publish(LogEvent(f"{trace_prefix} [MB Search] Matched MBID [{search_mbid[:8]}...] for '{current_title}'", "SUCCESS"))

            # 4. Deezer ISRC Backfill
            if not isrc_val and search_artist and current_title and not EnrichmentEngine._cancel_requested:
                deezer_isrc = ExternalAPIService.query_deezer_isrc(search_artist, current_title)
                if deezer_isrc:
                    isrc_val = deezer_isrc
                    ev_buffer.add("isrc", deezer_isrc, "REMOTE", "SRC_DEEZER", confidence=0.95)

            # 5. iTunes Search
            if not mb_rec_id and search_artist and current_title and not EnrichmentEngine._cancel_requested:
                itunes_data = ExternalAPIService.query_itunes_track(search_artist, current_title)
                if itunes_data:
                    it_hash = itunes_data.get("payload_hash")
                    if itunes_data.get("album"): ev_buffer.add("album", itunes_data["album"], "REMOTE", "SRC_ITUNES", it_hash, confidence=0.88)
                    if itunes_data.get("release_date"): ev_buffer.add("release_date", itunes_data["release_date"], "REMOTE", "SRC_ITUNES", it_hash, confidence=0.88)

            # 6. Discogs Search
            if not has_catno and search_artist and search_album and not EnrichmentEngine._cancel_requested:
                discogs_data = ExternalAPIService.query_discogs_release(search_artist, search_album)
                if discogs_data:
                    dc_hash = discogs_data.get("payload_hash")
                    if discogs_data.get("catno"): ev_buffer.add("catalog_number", discogs_data["catno"], "REMOTE", "SRC_DISCOGS", dc_hash, confidence=0.90)
                    if discogs_data.get("barcode"): ev_buffer.add("barcode", discogs_data["barcode"], "REMOTE", "SRC_DISCOGS", dc_hash, confidence=0.90)

            # 7. MusicBrainz Details & Wikidata
            if mb_rec_id and not EnrichmentEngine._cancel_requested:
                mb_data, mb_hash = ExternalAPIService.query_musicbrainz_recording(mb_rec_id)
                if mb_data:
                    if mb_data.get("title"): ev_buffer.add("title", mb_data["title"], "REMOTE", "SRC_MUSICBRAINZ", mb_hash, confidence=0.95)
                    artist_credits = mb_data.get("artist-credit", [])
                    if artist_credits and isinstance(artist_credits[0], dict):
                        art_obj = artist_credits[0].get("artist", {})
                        mb_artist_name = artist_credits[0].get("name") or art_obj.get("name")
                        mb_artist_mbid = art_obj.get("id")
                        mb_artist_country = art_obj.get("country")

                        if mb_artist_name: ev_buffer.add("artist", mb_artist_name, "REMOTE", "SRC_MUSICBRAINZ", mb_hash, confidence=0.95)
                        if mb_artist_country: ev_buffer.add("country", mb_artist_country, "REMOTE", "SRC_MUSICBRAINZ", mb_hash, confidence=0.95)
                        if mb_artist_mbid:
                            ev_buffer.add("musicbrainz_artist_id", mb_artist_mbid, "REMOTE", "SRC_MUSICBRAINZ", mb_hash, confidence=0.99)
                            
                            wiki_data = ExternalAPIService.query_wikidata_artist(mb_artist_mbid)
                            repo_art = get_artist_repo()
                            art_id = repo_art.get_or_create(mb_artist_name or search_artist or "Unknown Artist", country=mb_artist_country or (wiki_data.get("country") if wiki_data else None), mbid=mb_artist_mbid)
                            
                            country_val = mb_artist_country or (wiki_data.get("country") if wiki_data else None)
                            formed_val = wiki_data.get("formed_year") if wiki_data else None
                            
                            repo_art.update_demographics(
                                art_id,
                                country=country_val,
                                formed_year=formed_val,
                                gender=wiki_data.get("gender") if wiki_data else None,
                                artist_type=wiki_data.get("artist_type") if wiki_data else None
                            )

                    releases = mb_data.get("releases", [])
                    if releases and isinstance(releases[0], dict):
                        best_rel = releases[0]
                        ev_buffer.add("album", best_rel.get("title"), "REMOTE", "SRC_MUSICBRAINZ", mb_hash, confidence=0.95)
                        ev_buffer.add("release_date", best_rel.get("date"), "REMOTE", "SRC_MUSICBRAINZ", mb_hash, confidence=0.95)

            # 8. Last.fm Community Tags
            effective_artist = search_artist or "Unknown Artist"
            if effective_artist != "Unknown Artist" and current_title and not EnrichmentEngine._cancel_requested:
                lastfm_tags = ExternalAPIService.query_lastfm_tags(effective_artist, current_title)
                for idx, (tag_name, tag_count) in enumerate(lastfm_tags):
                    conf_val = min(1.0, max(0.10, tag_count / 100.0))
                    clean_norm = normalize_tag_alias(tag_name)
                    if clean_norm:
                        pos_w = round(0.75 ** idx, 4)
                        ev_buffer.add("genre", clean_norm, "REMOTE", "SRC_LASTFM", confidence=conf_val * 0.85 * pos_w, token_index=idx)

            facts_added = ev_buffer.commit()

            cursor.execute("SELECT field_name FROM meta_locks WHERE entity_id = %s AND lock_state = 'MANUAL'", (rec_id,))
            manual_locked = {r[0] for r in cursor.fetchall()}

            if "genre" not in manual_locked and "subgenre" not in manual_locked:
                is_compilation = False
                if alb_id:
                    cursor.execute("SELECT COUNT(DISTINCT artist_id) FROM core_recordings WHERE album_id = %s", (alb_id,))
                    artist_cnt = cursor.fetchone()[0] or 0
                    art_check = (search_artist or "").lower()
                    is_compilation = (artist_cnt >= 3) or ("various" in art_check) or ("v.a." in art_check)

                if alb_id and not is_compilation:
                    cursor.execute("""
                        SELECT e.value, 
                               e.confidence * (CASE WHEN e.entity_id = %s THEN 1.6 ELSE 1.0 END), 
                               e.source_id 
                        FROM meta_evidence e 
                        JOIN core_recordings r ON e.entity_id = r.id 
                        WHERE r.album_id = %s AND e.field_name IN ('genre', 'subgenre') AND e.value IS NOT NULL AND e.value != ''
                    """, (rec_id, alb_id))
                else:
                    cursor.execute("""
                        SELECT value, confidence, source_id FROM meta_evidence 
                        WHERE entity_id = %s AND field_name IN ('genre', 'subgenre') AND value IS NOT NULL AND value != ''
                    """, (rec_id,))

                raw_evidence = cursor.fetchall()
                if raw_evidence:
                    genre_match = TaxonomyService.resolve_genres([(r[0], r[1], r[2]) for r in raw_evidence], artist_name=search_artist)
                    if genre_match and genre_match.primary_genre != "Unclassified":
                        buf_tax = EvidenceBuffer(rec_id, run_id)
                        buf_tax.add("genre", genre_match.primary_genre, "DERIVED", "SRC_TAXONOMY", confidence=genre_match.confidence)
                        buf_tax.add("subgenre", genre_match.primary_subgenre, "DERIVED", "SRC_TAXONOMY", confidence=genre_match.confidence)
                        buf_tax.commit()

            cursor.execute("""
                SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                       payload_hash, confidence, origin_type, observed_at, raw_value, 
                       token_index, token_delimiter, positional_weight 
                FROM meta_evidence WHERE entity_id = %s
            """, (rec_id,))

            evidence_by_field: Dict[str, List[Evidence]] = defaultdict(list)
            for r in cursor.fetchall():
                ev = Evidence(
                    id=r[0], entity_id=r[1], field_name=r[2], value=r[3], evidence_class=r[4],
                    source_id=r[5], run_id=r[6], payload_hash=r[7], confidence=r[8], origin_type=r[9],
                    observed_at=str(r[10]), raw_value=r[11], token_index=r[12], token_delimiter=r[13],
                    positional_weight=r[14]
                )
                evidence_by_field[r[2]].append(ev)

            target_fields = ("title", "artist", "album", "genre", "subgenre", "release_date", "isrc", "musicbrainz_recording_id", "acoustid_id")
            for field_name in target_fields:
                if field_name in manual_locked:
                    continue

                ev_rows = evidence_by_field.get(field_name, [])
                if ev_rows:
                    try:
                        curr_val = get_current_field_value(cursor, rec_id, field_name)
                        decision = SymbolicInferenceEngine.resolve_field(rec_id, field_name, curr_val, ev_rows, run_id)
                        ResolutionPersistenceAdapter.apply_decision(decision)
                    except Exception as field_ex:
                        event_bus.publish(LogEvent(f"{trace_prefix} Error resolving field {field_name}: {field_ex}", "WARNING"))

            cursor.execute("SELECT musicbrainz_recording_id FROM core_recordings WHERE id = %s", (rec_id,))
            final_mb_row = cursor.fetchone()
            final_mbid = final_mb_row[0] if (final_mb_row and final_mb_row[0]) else mb_rec_id

            cursor.execute("SELECT quality_score FROM meta_validation WHERE recording_id = %s", (rec_id,))
            q_row = cursor.fetchone()
            q_score = float(q_row[0]) if q_row and q_row[0] else 0.0

            return {
                "success": True,
                "rec_id": rec_id,
                "title": current_title,
                "artist": search_artist or "Unknown Artist",
                "mbid": final_mbid,
                "isrc": isrc_val,
                "facts_added": facts_added,
                "quality_score": q_score
            }
        except Exception as e:
            event_bus.publish(LogEvent(f"{trace_prefix} Enrichment failed: {e}", "ERROR"))
            return {"success": False, "rec_id": rec_id, "facts_added": 0, "quality_score": 0.0}
        finally:
            close_thread_connection()

    @staticmethod
    def run_enrichment_pipeline(mode: str = "1") -> int:
        EnrichmentEngine._cancel_requested = False
        ensure_fpcalc()
        start_time = time.time()
        run_id = generate_ulid()

        conn = get_connection()
        cursor = conn.cursor()

        with db_transaction() as tx:
            tx.execute("INSERT INTO sys_runs (run_id, parser_version, config_hash) VALUES (%s, %s, %s)", (run_id, ENRICHMENT_VERSION, settings.get_config_hash()))

        is_full_mode = (mode == "2")
        if is_full_mode:
            _artist_cache.clear()
            if _disk_cache:
                try:
                    _disk_cache.clear()
                except Exception:
                    pass

            cursor.execute("""
                SELECT r.id, loc.filepath, r.title, r.is_locked, r.album_id 
                FROM core_recordings r 
                LEFT JOIN core_assets a ON r.id = a.recording_id 
                LEFT JOIN core_asset_locations loc ON a.asset_id = loc.asset_id 
                WHERE loc.is_available = 1
            """)
        else:
            cursor.execute("""
                SELECT r.id, loc.filepath, r.title, r.is_locked, r.album_id 
                FROM core_recordings r 
                LEFT JOIN core_assets a ON r.id = a.recording_id 
                LEFT JOIN core_asset_locations loc ON a.asset_id = loc.asset_id 
                LEFT JOIN meta_validation v ON r.id = v.recording_id
                WHERE (v.quality_score IS NULL OR v.quality_score < 1.0 OR r.musicbrainz_recording_id IS NULL OR r.isrc IS NULL) AND loc.is_available = 1
            """)

        recordings = cursor.fetchall()
        total = len(recordings)

        if not recordings:
            event_bus.publish(LogEvent("[+] Canonical Enrichment: All tracks are fully enriched.", "SUCCESS"))
            return 0

        event_bus.publish(LogEvent(f"[*] Starting Stage 1 Studio Album Cluster Pre-Fetch across {total} track(s)..."))

        album_clusters = defaultdict(list)
        for r_item in recordings:
            album_clusters[r_item[4] or "SINGLE"].append(r_item)

        total_clusters = len(album_clusters)
        for c_idx, (alb_key, items) in enumerate(album_clusters.items()):
            if EnrichmentEngine._cancel_requested:
                break
            if items[0][4]:
                enrich_album_cluster(items, run_id)
            
            c_pct = int(((c_idx + 1) / total_clusters) * 100)
            cpu_pct, ram_gb = get_system_resource_telemetry()
            event_bus.publish(TelemetryEvent(
                active_workers=4, queue_size=total_clusters - (c_idx + 1), throughput_tps=0.0,
                cpu_pct=cpu_pct, ram_gb=ram_gb, finish_est_str="Stage 1 Pre-Fetch",
                total_progress=int(c_pct * 0.15), stage_progress=c_pct, file_progress=100,
                current_track_title=f"Cluster {c_idx+1}/{total_clusters}"
            ))

        if EnrichmentEngine._cancel_requested:
            event_bus.publish(LogEvent("[-] Enrichment pass cancelled by user.", "WARNING"))
            return 0

        event_bus.publish(LogEvent(f"[*] Starting Stage 2 Parallel Track Cascade on {total} track(s)..."))

        processed_count = 0
        total_facts_gathered = 0
        donor_queue: Set[str] = set()

        concurrency = max(1, settings.MAX_WORKER_PROCESSES)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_rec = {
                executor.submit(EnrichmentEngine._enrich_single_track_worker, rec_item, run_id, is_full_mode): rec_item
                for rec_item in recordings
            }

            for idx, future in enumerate(as_completed(future_to_rec)):
                if EnrichmentEngine._cancel_requested:
                    event_bus.publish(LogEvent("[-] Track cascade cancelled by user.", "WARNING"))
                    break

                rec_item = future_to_rec[future]
                rec_id = rec_item[0]
                processed = idx + 1
                elapsed = max(0.001, time.time() - start_time)
                tps = processed / elapsed
                remaining = total - processed
                eta_sec = remaining / tps if tps > 0 else 0
                eta_str = f"{int(eta_sec)}s left" if eta_sec > 0 else "Finishing..."
                pct = int((processed / total) * 100)
                total_pct = min(99, 15 + int(pct * 0.80))
                cpu_pct, ram_gb = get_system_resource_telemetry()

                try:
                    res_info = future.result()
                    if res_info and res_info.get("success"):
                        processed_count += 1
                        f_count = res_info.get("facts_added", 0)
                        total_facts_gathered += f_count
                        q_score = res_info.get("quality_score", 0.0)
                        track_t = res_info.get("title") or rec_item[2] or "Untitled"
                        art_t = res_info.get("artist") or "Unknown"
                        mb_id_str = res_info.get("mbid") or "N/A"

                        if q_score >= 1.0:
                            donor_queue.add(rec_id)

                        mb_display = mb_id_str[:8] + "..." if mb_id_str != "N/A" else "N/A"
                        event_bus.publish(LogEvent(
                            f"[+] [{processed}/{total}] Enriched '{track_t}' ({art_t}) -> MBID [{mb_display}] | Quality: {q_score*100:.0f}% | Facts: +{f_count}",
                            "SUCCESS" if q_score >= 0.8 else "INFO"
                        ))

                        event_bus.publish(TelemetryEvent(
                            active_workers=concurrency, queue_size=remaining, throughput_tps=round(tps, 1),
                            cpu_pct=cpu_pct, ram_gb=ram_gb, finish_est_str=f"{int(eta_sec)}s left",
                            total_progress=total_pct, stage_progress=pct, file_progress=100,
                            current_track_title=f"{track_t} ({art_t})", facts_count=total_facts_gathered
                        ))
                except Exception as ex:
                    event_bus.publish(LogEvent(f"[-] Track {rec_id[:8]} cascade exception: {ex}", "ERROR"))

        if donor_queue and not EnrichmentEngine._cancel_requested:
            event_bus.publish(LogEvent(f"[*] Running Sibling Propagation across donor track clusters..."))
            
            donor_list = list(donor_queue)
            placeholders = ",".join("%s" for _ in donor_list)
            cursor.execute(f"""
                SELECT album_id, id FROM core_recordings 
                WHERE id IN ({placeholders}) AND album_id IS NOT NULL
            """, donor_list)

            album_donors: Dict[str, str] = {}
            for alb_id_d, rec_id_d in cursor.fetchall():
                if alb_id_d not in album_donors:
                    album_donors[alb_id_d] = rec_id_d

            total_siblings_updated = 0
            for alb_id_d, donor_id in album_donors.items():
                if EnrichmentEngine._cancel_requested:
                    break
                stats = IntraAlbumEngine.propagate_from_donor(donor_id, run_id)
                total_siblings_updated += stats.get("siblings_updated", 0)

            event_bus.publish(LogEvent(f"[+] Sibling propagation complete: Updated {total_siblings_updated} sibling track(s).", "SUCCESS"))

        # Sweep recently enriched local tracks against pending candidates, flagging matching nodes as owned (ACQUIRED)
        try:
            acquired_count = update_acquired_candidates_from_library()
            if acquired_count > 0:
                event_bus.publish(LogEvent(f"[+] Post-Enrichment Sync: Sweep completed. Marked {acquired_count} candidate(s) as ACQUIRED (owned).", "SUCCESS"))
        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] Post-Enrichment candidate sync encountered error: {ex}", "WARNING"))

        with db_transaction() as tx:
            tx.execute("UPDATE sys_runs SET finished_at = CURRENT_TIMESTAMP WHERE run_id = %s", (run_id,))

        event_bus.publish(TelemetryEvent(0, 0, 0.0, 0.0, 0.0, "Idle", 100, 100, 100))
        event_bus.publish(LogEvent(f"[+] Canonical Enrichment complete: Processed {processed_count} track(s), gathered {total_facts_gathered} facts in {time.time() - start_time:.2f}s.", "SUCCESS"))
        return processed_count