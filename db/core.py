"""
Database Core Engine & Transaction Infrastructure
Provides PostgreSQL raw connection management, transaction coordination,
schema initialisation, and administrative cleanup tasks.
Updated to guarantee unconditional transaction commits for schema baseline.
"""

import threading
from contextlib import contextmanager
from typing import Generator, Any
from sqlalchemy import create_engine, text
from config.settings import settings
from domain.exceptions import DatabaseError

_local = threading.local()
_engine = None
_SessionFactory = None

def get_postgres_engine():
    global _engine, _SessionFactory
    if _engine is None:
        _engine = create_engine(
            settings.POSTGRES_URL,
            pool_size=20,
            max_overflow=10,
            pool_timeout=30.0,
            pool_pre_ping=True
        )
    return _engine

def get_connection() -> Any:
    if not hasattr(_local, "pg_conn") or _local.pg_conn is None:
        try:
            engine = get_postgres_engine()
            _local.pg_conn = engine.raw_connection()
        except Exception as e:
            raise DatabaseError(f"Failed to connect to PostgreSQL database: {e}")

    try:
        if hasattr(_local.pg_conn, "get_transaction_status") and _local.pg_conn.get_transaction_status() == 3:
            _local.pg_conn.rollback()
    except Exception:
        pass

    return _local.pg_conn

def close_thread_connection() -> None:
    if hasattr(_local, "pg_conn") and _local.pg_conn is not None:
        try:
            _local.pg_conn.close()
        except Exception:
            pass
        _local.pg_conn = None

@contextmanager
def db_transaction() -> Generator[Any, None, None]:
    conn = get_connection()
    try:
        if hasattr(conn, "get_transaction_status") and conn.get_transaction_status() == 3:
            conn.rollback()
    except Exception:
        pass

    if not hasattr(_local, "tx_depth"):
        _local.tx_depth = 0

    _local.tx_depth += 1
    depth = _local.tx_depth
    cursor = conn.cursor()

    try:
        yield cursor
        if depth == 1:
            conn.commit()
    except Exception as e:
        try:
            if depth == 1:
                conn.rollback()
        except Exception:
            pass
        raise DatabaseError(f"PostgreSQL Transaction failed at depth {depth}: {e}") from e
    finally:
        _local.tx_depth -= 1

def merge_duplicate_recordings_by_mbid(target_recording_id: str, duplicate_recording_id: str) -> bool:
    if not target_recording_id or not duplicate_recording_id or target_recording_id == duplicate_recording_id:
        return False

    with db_transaction() as cursor:
        cursor.execute("SELECT id FROM core_recordings WHERE id = %s", (target_recording_id,))
        if not cursor.fetchone():
            return False

        cursor.execute("SELECT id FROM core_recordings WHERE id = %s", (duplicate_recording_id,))
        if not cursor.fetchone():
            return False

        cursor.execute("UPDATE core_assets SET recording_id = %s WHERE recording_id = %s", (target_recording_id, duplicate_recording_id))
        cursor.execute("UPDATE core_sonic_features SET recording_id = %s WHERE recording_id = %s", (target_recording_id, duplicate_recording_id))

        cursor.execute("""
            INSERT INTO meta_evidence (
                entity_id, field_name, value, evidence_class, source_id, run_id, 
                payload_hash, confidence, origin_type, raw_value, token_index, 
                token_delimiter, positional_weight
            )
            SELECT %s, field_name, value, evidence_class, source_id, run_id,
                   payload_hash, confidence, origin_type, raw_value, token_index,
                   token_delimiter, positional_weight
            FROM meta_evidence WHERE entity_id = %s
            ON CONFLICT DO NOTHING
        """, (target_recording_id, duplicate_recording_id))
        cursor.execute("DELETE FROM meta_evidence WHERE entity_id = %s", (duplicate_recording_id,))

        cursor.execute("UPDATE meta_decisions SET entity_id = %s WHERE entity_id = %s", (target_recording_id, duplicate_recording_id))
        cursor.execute("UPDATE meta_issues SET entity_id = %s WHERE entity_id = %s", (target_recording_id, duplicate_recording_id))
        cursor.execute("UPDATE meta_locks SET entity_id = %s WHERE entity_id = %s", (target_recording_id, duplicate_recording_id))

        cursor.execute("DELETE FROM meta_validation WHERE recording_id = %s", (duplicate_recording_id,))
        cursor.execute("DELETE FROM core_recordings WHERE id = %s", (duplicate_recording_id,))

        from services.resolve import ResolutionPersistenceAdapter
        ResolutionPersistenceAdapter.recalculate_quality_score(cursor, target_recording_id)

    return True

def wipe_database_clean(restore_files: bool = True) -> int:
    restored_count = 0
    if restore_files:
        from services.ingest import restore_archived_files_to_input
        restored_count = restore_archived_files_to_input()

    tables = [
        "sys_runs", "sys_sources", "sys_payloads", "sys_fastpass_cache", 
        "core_artists", "core_albums", "core_recordings", "core_assets", 
        "core_asset_locations", "core_sonic_features", "meta_evidence", 
        "meta_decisions", "meta_locks", "meta_validation", "meta_issues", 
        "sys_authority_map", "sys_discovery_candidates", "sys_discovery_scores",
        "sys_crawl_frontier"
    ]
    with db_transaction() as cursor:
        for tbl in tables:
            cursor.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE;")
        cursor.execute("DROP VIEW IF EXISTS vw_recording_overview CASCADE;")
        cursor.execute("DROP VIEW IF EXISTS vw_open_issues_queue CASCADE;")
    
    init_db_schema()
    return restored_count

def init_db_schema() -> None:
    engine = get_postgres_engine()
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        except Exception:
            pass

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS sys_runs (
            run_id VARCHAR(32) PRIMARY KEY,
            parser_version VARCHAR(32),
            resolver_version VARCHAR(32),
            taxonomy_version VARCHAR(32),
            fingerprint_version VARCHAR(32),
            git_commit VARCHAR(64),
            config_hash VARCHAR(64),
            started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP WITH TIME ZONE
        );

        CREATE TABLE IF NOT EXISTS sys_payloads (
            content_hash VARCHAR(64) PRIMARY KEY,
            payload_type VARCHAR(64),
            compressed_data BYTEA,
            source VARCHAR(64),
            checksum VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sys_fastpass_cache (
            cache_key VARCHAR(64) PRIMARY KEY,
            result_json TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sys_authority_map (
            id VARCHAR(32) PRIMARY KEY,
            entity_type VARCHAR(32) NOT NULL DEFAULT 'RELEASE_GROUP',
            mbid VARCHAR(64),
            discogs_id INT,
            wikidata_qid VARCHAR(32),
            lastfm_name TEXT,
            confidence DOUBLE PRECISION DEFAULT 0.0,
            match_method VARCHAR(32) DEFAULT 'EXACT_MBID',
            evidence_json TEXT DEFAULT '{}',
            evidence_hash VARCHAR(64) DEFAULT '',
            is_active INT DEFAULT 1,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS core_artists (
            id VARCHAR(32) PRIMARY KEY,
            name TEXT NOT NULL,
            normalized_name TEXT,
            sort_name TEXT,
            country VARCHAR(10),
            region TEXT,
            formed_year INT,
            ended_year INT,
            artist_type TEXT,
            gender VARCHAR(32),
            aliases TEXT,
            musicbrainz_artist_id TEXT UNIQUE,
            state VARCHAR(32) DEFAULT 'NEW',
            is_locked INT DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS core_albums (
            id VARCHAR(32) PRIMARY KEY,
            artist_id VARCHAR(32) REFERENCES core_artists(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            album_type VARCHAR(64) DEFAULT 'Album',
            release_date VARCHAR(32),
            original_release_date VARCHAR(32),
            remaster_year INT,
            catalog_number TEXT,
            barcode TEXT,
            musicbrainz_release_id TEXT,
            release_group_mbid TEXT,
            state VARCHAR(32) DEFAULT 'NEW',
            is_locked INT DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS core_recordings (
            id VARCHAR(32) PRIMARY KEY,
            song_id VARCHAR(32),
            release_id VARCHAR(32),
            album_id VARCHAR(32) REFERENCES core_albums(id) ON DELETE SET NULL,
            artist_id VARCHAR(32) REFERENCES core_artists(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            release_date VARCHAR(32),
            original_release_date VARCHAR(32),
            isrc TEXT,
            musicbrainz_recording_id TEXT,
            acoustid_id TEXT,
            track_number INT,
            disc_number INT DEFAULT 1,
            album_tracks_count INT,
            state VARCHAR(32) DEFAULT 'PARSED',
            is_locked INT DEFAULT 0,
            genre TEXT,
            subgenre TEXT
        );

        CREATE TABLE IF NOT EXISTS core_assets (
            asset_id VARCHAR(32) PRIMARY KEY,
            recording_id VARCHAR(32) REFERENCES core_recordings(id) ON DELETE CASCADE,
            md5_file VARCHAR(64),
            sha256_file VARCHAR(64),
            audio_stream_hash VARCHAR(64),
            duration DOUBLE PRECISION,
            format VARCHAR(16),
            bitrate INT,
            sample_rate INT,
            channels INT,
            file_size BIGINT,
            state VARCHAR(32) DEFAULT 'NEW'
        );

        CREATE TABLE IF NOT EXISTS core_asset_locations (
            id BIGSERIAL PRIMARY KEY,
            asset_id VARCHAR(32) REFERENCES core_assets(asset_id) ON DELETE CASCADE,
            filepath TEXT NOT NULL,
            mounted_drive VARCHAR(32) DEFAULT 'LOCAL',
            is_available INT DEFAULT 1,
            last_seen TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (asset_id) REFERENCES core_assets(asset_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS core_sonic_features (
            recording_id VARCHAR(32) PRIMARY KEY REFERENCES core_recordings(id) ON DELETE CASCADE,
            bpm DOUBLE PRECISION,
            beat_confidence DOUBLE PRECISION,
            danceability DOUBLE PRECISION,
            spectral_centroid DOUBLE PRECISION,
            spectral_flux DOUBLE PRECISION,
            spectral_rolloff DOUBLE PRECISION,
            zero_crossing_rate DOUBLE PRECISION,
            key_signature VARCHAR(32),
            lufs_loudness DOUBLE PRECISION,
            dynamic_range DOUBLE PRECISION,
            acousticness DOUBLE PRECISION,
            instrumentalness DOUBLE PRECISION,
            valence DOUBLE PRECISION,
            extracted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS meta_evidence (
            id BIGSERIAL PRIMARY KEY,
            entity_id VARCHAR(32) NOT NULL,
            field_name TEXT NOT NULL,
            value TEXT,
            evidence_class VARCHAR(32) DEFAULT 'LOCAL',
            source_id TEXT NOT NULL,
            run_id VARCHAR(32),
            payload_hash TEXT,
            confidence DOUBLE PRECISION DEFAULT 1.0,
            origin_type VARCHAR(32) DEFAULT 'RESOLVER',
            observed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            raw_value TEXT,
            token_index INT DEFAULT 0,
            token_delimiter VARCHAR(8),
            positional_weight DOUBLE PRECISION DEFAULT 1.0
        );

        CREATE TABLE IF NOT EXISTS meta_decisions (
            id BIGSERIAL PRIMARY KEY,
            run_id VARCHAR(32),
            entity_id VARCHAR(32) NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            selected_value TEXT,
            reason TEXT,
            rejected_values_json TEXT,
            decision_hash TEXT,
            applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS meta_locks (
            entity_id VARCHAR(32) NOT NULL,
            field_name VARCHAR(64) NOT NULL,
            lock_state VARCHAR(32) DEFAULT 'AUTOMATIC',
            PRIMARY KEY (entity_id, field_name)
        );

        CREATE TABLE IF NOT EXISTS meta_validation (
            recording_id VARCHAR(32) PRIMARY KEY REFERENCES core_recordings(id) ON DELETE CASCADE,
            quality_score DOUBLE PRECISION DEFAULT 0.0,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS meta_issues (
            id BIGSERIAL PRIMARY KEY,
            entity_id VARCHAR(32) NOT NULL,
            issue_code VARCHAR(64) NOT NULL,
            severity VARCHAR(32) DEFAULT 'WARNING',
            status VARCHAR(32) DEFAULT 'OPEN',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP WITH TIME ZONE
        );

        CREATE TABLE IF NOT EXISTS sys_discovery_candidates (
            candidate_id VARCHAR(32) PRIMARY KEY,
            authority_id VARCHAR(32),
            entity_type VARCHAR(32) NOT NULL DEFAULT 'RELEASE_GROUP',
            title TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            release_group_mbid TEXT,
            release_year INT,
            primary_genre VARCHAR(64) NOT NULL DEFAULT 'Unclassified',
            primary_subgenre VARCHAR(64) NOT NULL DEFAULT 'Unclassified',
            secondary_genres_json TEXT NOT NULL DEFAULT '[]',
            state VARCHAR(32) NOT NULL DEFAULT 'NEW',
            final_ccs DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            overall_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            snoozed_until TIMESTAMP WITH TIME ZONE,
            acquired_asset_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sys_discovery_scores (
            candidate_id VARCHAR(32) PRIMARY KEY,
            v_rel DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            v_coll DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            v_graph DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            v_def DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            p_sat DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            delta_fatigue DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            provider_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            active_strategy VARCHAR(64) NOT NULL DEFAULT 'Balanced Curator',
            explanation_json TEXT NOT NULL,
            computed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sys_crawl_frontier (
            seed_id VARCHAR(64) PRIMARY KEY,
            entity_name TEXT NOT NULL,
            entity_type VARCHAR(32) NOT NULL DEFAULT 'ARTIST',
            priority DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            state VARCHAR(32) NOT NULL DEFAULT 'PENDING',
            last_crawled_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_frontier_state_priority ON sys_crawl_frontier (state, priority DESC);
        """))

        cursor = conn.connection.cursor()
        views_stale = False

        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'core_recordings' AND column_name = 'genre'")
        if not cursor.fetchone():
            conn.execute(text("ALTER TABLE core_recordings ADD COLUMN IF NOT EXISTS genre TEXT;"))
            conn.execute(text("ALTER TABLE core_recordings ADD COLUMN IF NOT EXISTS subgenre TEXT;"))
            views_stale = True

        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'core_artists' AND column_name = 'normalized_name'")
        if not cursor.fetchone():
            conn.execute(text("ALTER TABLE core_artists ADD COLUMN IF NOT EXISTS normalized_name TEXT;"))
            
            cursor.execute("SELECT id, name FROM core_artists WHERE normalized_name IS NULL")
            null_rows = cursor.fetchall()
            if null_rows:
                from utils.text import normalize_artist_name
                with conn.connection.cursor() as update_cur:
                    for a_id, a_name in null_rows:
                        norm = normalize_artist_name(a_name)
                        update_cur.execute("UPDATE core_artists SET normalized_name = %s WHERE id = %s", (norm, a_id))
            
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_artists_normalized_name ON core_artists (normalized_name);"))
            views_stale = True

        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.views 
                WHERE table_schema = 'public' AND table_name = 'vw_recording_overview'
            )
        """)
        views_exist = cursor.fetchone()[0]

        if not views_exist or views_stale:
            conn.execute(text("DROP VIEW IF EXISTS vw_recording_overview CASCADE;"))
            conn.execute(text("DROP VIEW IF EXISTS vw_open_issues_queue CASCADE;"))

            conn.execute(text("ALTER TABLE meta_evidence DROP CONSTRAINT IF EXISTS unq_meta_evidence CASCADE;"))
            conn.execute(text("ALTER TABLE meta_evidence DROP CONSTRAINT IF EXISTS meta_evidence_entity_id_field_name_source_id_value_key CASCADE;"))
            conn.execute(text("DROP INDEX IF EXISTS unq_meta_evidence CASCADE;"))

            conn.execute(text("ALTER TABLE core_recordings ALTER COLUMN musicbrainz_recording_id TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_recordings ALTER COLUMN acoustid_id TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_recordings ALTER COLUMN isrc TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_albums ALTER COLUMN catalog_number TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_albums ALTER COLUMN barcode TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_albums ALTER COLUMN musicbrainz_release_id TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_albums ALTER COLUMN release_group_mbid TYPE TEXT;"))
            conn.execute(text("ALTER TABLE core_artists ALTER COLUMN musicbrainz_artist_id TYPE TEXT;"))
            conn.execute(text("ALTER TABLE meta_evidence ALTER COLUMN source_id TYPE TEXT;"))
            conn.execute(text("ALTER TABLE meta_evidence ALTER COLUMN payload_hash TYPE TEXT;"))

            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_evidence_unique 
                ON meta_evidence (entity_id, field_name, source_id, md5(value));
            """))

            conn.execute(text("""
            CREATE VIEW vw_recording_overview AS
            SELECT 
                r.id, 
                r.title, 
                COALESCE(a.name, 'Unknown Artist') AS artist_name, 
                COALESCE(alb.title, 'Unknown Album') AS album_title, 
                r.release_date AS issue_release_date,
                COALESCE(alb.original_release_date, r.original_release_date, r.release_date) AS original_release_date,
                f.duration, 
                f.bitrate, 
                f.sample_rate,
                f.channels,
                f.format, 
                loc.filepath, 
                r.isrc, 
                r.musicbrainz_recording_id, 
                r.acoustid_id, 
                r.state, 
                COALESCE(v.quality_score, 0.0) AS quality_score,
                COALESCE(r.genre, 'Unclassified') AS primary_genre,
                COALESCE(r.subgenre, 'Unclassified') AS primary_subgenre
            FROM core_recordings r
            LEFT JOIN core_artists a ON r.artist_id = a.id
            LEFT JOIN core_albums alb ON r.album_id = alb.id
            LEFT JOIN core_assets f ON r.id = f.recording_id
            LEFT JOIN core_asset_locations loc ON f.asset_id = loc.asset_id AND loc.is_available = 1
            LEFT JOIN meta_validation v ON r.id = v.recording_id;
            """))

            conn.execute(text("""
            CREATE VIEW vw_open_issues_queue AS
            SELECT 
                i.id, 
                i.entity_id, 
                COALESCE(r.title, 'Untitled Work') AS recording_title, 
                i.issue_code, 
                i.severity, 
                i.status, 
                i.created_at
            FROM meta_issues i
            LEFT JOIN core_recordings r ON i.entity_id = r.id
            WHERE i.status = 'OPEN';
            """))

        # Unconditionally commit all DDL schema creations to guarantee sys_crawl_frontier commits to disk
        conn.connection.commit()