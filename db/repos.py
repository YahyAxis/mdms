"""
Unified Native PostgreSQL Repositories
Provides high-throughput data access operations for Recordings, Artists, Albums, Evidence, Discovery, and Sonic features.
"""

import json
from typing import Optional, List, Dict, Any
from domain.models import (
    Recording, Artist, Album, Evidence, Decision, 
    DiscoveryCandidate, DiscoveryScore, generate_ulid
)
from domain.sonic import SonicFeature, AudioVector, SonicSimilarityResult
from db.core import get_connection, db_transaction
from utils.text import normalize_artist_name, sanitize_artist_name
from utils.geo import get_region_for_country

class RecordingRepo:
    def get_by_id(self, recording_id: str) -> Optional[Recording]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, artist_name, album_title, issue_release_date, original_release_date, 
                   duration, bitrate, sample_rate, format, filepath, isrc, musicbrainz_recording_id, 
                   acoustid_id, state, quality_score, primary_genre, primary_subgenre 
            FROM vw_recording_overview WHERE id = %s
        """, (recording_id,))
        r = cursor.fetchone()
        if not r:
            return None
        return Recording(
            id=r[0], title=r[1] or "Untitled", artist_name=r[2], album_title=r[3], 
            release_date=r[4], original_release_date=r[5], duration=r[6] or 0.0, 
            bitrate=r[7], sample_rate=r[8], format=r[9] or "FLAC", filepath=r[10] or "", 
            isrc=r[11], musicbrainz_recording_id=r[12], acoustid_id=r[13], state=r[14] or "PARSED", 
            quality_score=r[15] or 0.0, primary_genre=r[16] or "Unclassified", 
            primary_subgenre=r[17] or "Unclassified"
        )

    def save(self, recording: Recording) -> None:
        with db_transaction() as cursor:
            cursor.execute("""
                INSERT INTO core_recordings (
                    id, title, release_date, original_release_date, isrc, 
                    musicbrainz_recording_id, acoustid_id, track_number, 
                    disc_number, album_tracks_count, state, is_locked, genre, subgenre
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    title = EXCLUDED.title,
                    release_date = EXCLUDED.release_date,
                    original_release_date = EXCLUDED.original_release_date,
                    isrc = EXCLUDED.isrc,
                    musicbrainz_recording_id = EXCLUDED.musicbrainz_recording_id,
                    acoustid_id = EXCLUDED.acoustid_id,
                    state = EXCLUDED.state,
                    is_locked = EXCLUDED.is_locked,
                    genre = EXCLUDED.genre,
                    subgenre = EXCLUDED.subgenre
            """, (
                recording.id, recording.title, recording.release_date, 
                recording.original_release_date, recording.isrc, 
                recording.musicbrainz_recording_id, recording.acoustid_id, 
                recording.track_number, recording.disc_number, 
                recording.album_tracks_count, recording.state, recording.is_locked,
                recording.primary_genre, recording.primary_subgenre
            ))

    def get_overview_catalog(self, filters: Optional[Dict[str, Any]] = None) -> List[Recording]:
        conn = get_connection()
        cursor = conn.cursor()
        query = """
            SELECT id, title, artist_name, album_title, issue_release_date, original_release_date, 
                   duration, bitrate, sample_rate, format, filepath, isrc, musicbrainz_recording_id, 
                   acoustid_id, state, quality_score, primary_genre, primary_subgenre 
            FROM vw_recording_overview
        """
        params = []
        where_clauses = []
        if filters and filters.get("search"):
            where_clauses.append("(title ILIKE %s OR artist_name ILIKE %s OR album_title ILIKE %s)")
            p = f"%{filters['search']}%"
            params.extend([p, p, p])
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += " ORDER BY id ASC"
        cursor.execute(query, params)
        return [Recording(
            id=r[0], title=r[1] or "Untitled", artist_name=r[2], album_title=r[3], 
            release_date=r[4], original_release_date=r[5], duration=r[6] or 0.0, 
            bitrate=r[7], sample_rate=r[8], format=r[9] or "FLAC", filepath=r[10] or "", 
            isrc=r[11], musicbrainz_recording_id=r[12], acoustid_id=r[13], state=r[14] or "PARSED", 
            quality_score=r[15] or 0.0, primary_genre=r[16] or "Unclassified", 
            primary_subgenre=r[17] or "Unclassified"
        ) for r in cursor.fetchall()]

    def get_pending_enrichment_recordings(self) -> List[Recording]:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, artist_name, album_title, issue_release_date, original_release_date, 
                   duration, bitrate, sample_rate, format, filepath, isrc, musicbrainz_recording_id, 
                   acoustid_id, state, quality_score, primary_genre, primary_subgenre 
            FROM vw_recording_overview
            WHERE quality_score < 1.0 OR musicbrainz_recording_id IS NULL OR isrc IS NULL
            ORDER BY quality_score ASC, id ASC
        """)
        return [Recording(
            id=r[0], title=r[1] or "Untitled", artist_name=r[2], album_title=r[3], 
            release_date=r[4], original_release_date=r[5], duration=r[6] or 0.0, 
            bitrate=r[7], sample_rate=r[8], format=r[9] or "FLAC", filepath=r[10] or "", 
            isrc=r[11], musicbrainz_recording_id=r[12], acoustid_id=r[13], state=r[14] or "PARSED", 
            quality_score=r[15] or 0.0, primary_genre=r[16] or "Unclassified", 
            primary_subgenre=r[17] or "Unclassified"
        ) for r in cursor.fetchall()]


class ArtistRepo:
    def get_or_create(self, name: str, country: Optional[str] = None, region: Optional[str] = None, mbid: Optional[str] = None) -> str:
        """Dynamically retrieves or creates artists using fast O(1) indexed normalized_name matching."""
        raw_name = name.strip() if name and name.strip() else "Unknown Artist"
        clean_name = sanitize_artist_name(raw_name)
        norm_key = normalize_artist_name(raw_name)

        conn = get_connection()
        cursor = conn.cursor()

        if mbid:
            cursor.execute("SELECT id FROM core_artists WHERE musicbrainz_artist_id = %s", (mbid,))
            row = cursor.fetchone()
            if row:
                return row[0]

        # Fast O(1) index query instead of dynamic full table scan
        cursor.execute("SELECT id FROM core_artists WHERE normalized_name = %s", (norm_key,))
        row = cursor.fetchone()
        if row:
            return row[0]

        calc_region = region or get_region_for_country(country)
        new_id = generate_ulid()
        with db_transaction() as tx:
            tx.execute("""
                INSERT INTO core_artists (id, name, normalized_name, country, region, musicbrainz_artist_id) 
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (normalized_name) DO UPDATE SET name = EXCLUDED.name RETURNING id
            """, (new_id, clean_name, norm_key, country, calc_region, mbid))
            
            inserted_id_row = tx.fetchone()
            if inserted_id_row:
                return inserted_id_row[0]
                
        return new_id

    def update_demographics(self, artist_id: str, **kwargs: Any) -> None:
        valid_fields = {"country", "region", "formed_year", "ended_year", "artist_type", "gender", "sort_name", "musicbrainz_artist_id"}
        updates = []
        params = []

        country_val = kwargs.get("country")
        if country_val and "region" not in kwargs:
            kwargs["region"] = get_region_for_country(country_val)

        for k, v in kwargs.items():
            if k in valid_fields and v is not None:
                updates.append(f"{k} = %s")
                params.append(v)

        if not updates:
            return

        params.append(artist_id)
        query = f"UPDATE core_artists SET {', '.join(updates)} WHERE id = %s"
        with db_transaction() as cursor:
            cursor.execute(query, params)


class AlbumRepo:
    def get_or_create(self, title: str, artist_id: Optional[str] = None, **kwargs: Any) -> str:
        clean_title = title.strip() if title and title.strip() else "Unknown Album"
        mb_release_id = kwargs.get("musicbrainz_release_id")
        release_group_mbid = kwargs.get("release_group_mbid")
        orig_date = kwargs.get("original_release_date")

        conn = get_connection()
        cursor = conn.cursor()

        if release_group_mbid:
            cursor.execute("SELECT id FROM core_albums WHERE release_group_mbid = %s", (release_group_mbid,))
            row = cursor.fetchone()
            if row:
                return row[0]

        if artist_id:
            cursor.execute("SELECT id FROM core_albums WHERE artist_id = %s AND LOWER(title) = LOWER(%s)", (artist_id, clean_title))
            row = cursor.fetchone()
            if row:
                return row[0]

        new_id = generate_ulid()
        with db_transaction() as tx:
            tx.execute("""
                INSERT INTO core_albums (id, title, artist_id, original_release_date, musicbrainz_release_id, release_group_mbid) 
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (new_id, clean_title, artist_id, orig_date, mb_release_id, release_group_mbid))
        return new_id


class EvidenceRepo:
    def add_batch(self, evidence_list: List[Evidence]) -> None:
        if not evidence_list:
            return
        rows = [
            (
                e.entity_id, e.field_name, e.value, e.evidence_class, 
                e.source_id, e.run_id, e.payload_hash, e.confidence, 
                e.origin_type, e.raw_value or e.value, e.token_index, 
                e.token_delimiter or ";", e.positional_weight
            ) for e in evidence_list if e.value and e.value.strip()
        ]
        with db_transaction() as cursor:
            cursor.executemany("""
                INSERT INTO meta_evidence (
                    entity_id, field_name, value, evidence_class, source_id, run_id, 
                    payload_hash, confidence, origin_type, raw_value, token_index, 
                    token_delimiter, positional_weight
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, rows)

    def get_for_entity(self, entity_id: str, field_name: Optional[str] = None) -> List[Evidence]:
        conn = get_connection()
        cursor = conn.cursor()
        if field_name:
            cursor.execute("""
                SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                       payload_hash, confidence, origin_type, observed_at, raw_value, 
                       token_index, token_delimiter, positional_weight
                FROM meta_evidence WHERE entity_id = %s AND field_name = %s
            """, (entity_id, field_name))
        else:
            cursor.execute("""
                SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                       payload_hash, confidence, origin_type, observed_at, raw_value, 
                       token_index, token_delimiter, positional_weight
                FROM meta_evidence WHERE entity_id = %s
            """, (entity_id,))
        return [Evidence(
            id=r[0], entity_id=r[1], field_name=r[2], value=r[3], evidence_class=r[4],
            source_id=r[5], run_id=r[6], payload_hash=r[7], confidence=r[8], origin_type=r[9],
            observed_at=str(r[10]), raw_value=r[11], token_index=r[12], token_delimiter=r[13],
            positional_weight=r[14]
        ) for r in cursor.fetchall()]


class DiscoveryRepo:
    def save_candidates(self, candidates: List[DiscoveryCandidate], scores: List[DiscoveryScore]) -> None:
        score_map = {s.candidate_id: s for s in scores}
        with db_transaction() as tx:
            for c in candidates:
                tx.execute("""
                    INSERT INTO sys_discovery_candidates (
                        candidate_id, authority_id, entity_type, title, artist_name, 
                        release_group_mbid, release_year, primary_genre, primary_subgenre, 
                        secondary_genres_json, state, final_ccs, overall_confidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(candidate_id) DO UPDATE SET 
                        final_ccs = EXCLUDED.final_ccs,
                        overall_confidence = EXCLUDED.overall_confidence,
                        primary_genre = EXCLUDED.primary_genre,
                        primary_subgenre = EXCLUDED.primary_subgenre
                """, (
                    c.candidate_id, c.authority_id, c.entity_type, c.title, c.artist_name,
                    c.release_group_mbid, c.release_year, c.primary_genre, c.primary_subgenre,
                    json.dumps(c.secondary_genres), c.state.value, c.final_ccs, c.overall_confidence
                ))

                if c.candidate_id in score_map:
                    s = score_map[c.candidate_id]
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
                            explanation_json = EXCLUDED.explanation_json, 
                            computed_at = CURRENT_TIMESTAMP
                    """, (
                        s.candidate_id, s.v_rel, s.v_coll, s.v_graph, s.v_def, s.p_sat,
                        s.delta_fatigue, s.provider_factor, s.active_strategy, s.explanation_json
                    ))


class SonicRepo:
    def save_features(self, features: SonicFeature) -> None:
        with db_transaction() as tx:
            tx.execute("""
                INSERT INTO core_sonic_features (
                    recording_id, bpm, beat_confidence, danceability, spectral_centroid, 
                    spectral_flux, spectral_rolloff, zero_crossing_rate, key_signature, 
                    lufs_loudness, dynamic_range, acousticness, instrumentalness, valence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(recording_id) DO UPDATE SET
                    bpm = EXCLUDED.bpm,
                    beat_confidence = EXCLUDED.beat_confidence,
                    danceability = EXCLUDED.danceability,
                    spectral_centroid = EXCLUDED.spectral_centroid,
                    spectral_flux = EXCLUDED.spectral_flux,
                    spectral_rolloff = EXCLUDED.spectral_rolloff,
                    zero_crossing_rate = EXCLUDED.zero_crossing_rate,
                    key_signature = EXCLUDED.key_signature,
                    lufs_loudness = EXCLUDED.lufs_loudness,
                    dynamic_range = EXCLUDED.dynamic_range,
                    acousticness = EXCLUDED.acousticness,
                    instrumentalness = EXCLUDED.instrumentalness,
                    valence = EXCLUDED.valence,
                    extracted_at = CURRENT_TIMESTAMP
            """, (
                features.recording_id, features.bpm, features.beat_confidence, 
                features.danceability, features.spectral_centroid, features.spectral_flux, 
                features.spectral_rolloff, features.zero_crossing_rate, features.key_signature, 
                features.lufs_loudness, features.dynamic_range, features.acousticness, 
                features.instrumentalness, features.valence
            ))


class UnitOfWork:
    def __init__(self) -> None:
        self.recordings = RecordingRepo()
        self.artists = ArtistRepo()
        self.albums = AlbumRepo()
        self.evidence = EvidenceRepo()
        self.discovery = DiscoveryRepo()
        self.sonic = SonicRepo()

    def __enter__(self) -> "UnitOfWork":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass