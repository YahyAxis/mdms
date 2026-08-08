"""Initial database schema baseline

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-07-25 21:50:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS sys_runs (
        run_id TEXT PRIMARY KEY,
        parser_version TEXT,
        resolver_version TEXT,
        taxonomy_version TEXT,
        fingerprint_version TEXT,
        git_commit TEXT,
        config_hash TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS sys_payloads (
        content_hash TEXT PRIMARY KEY,
        payload_type TEXT,
        compressed_data BLOB,
        source TEXT,
        checksum TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS sys_fastpass_cache (
        cache_key TEXT PRIMARY KEY,
        result_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS sys_unmapped_tags (
        raw_tag TEXT PRIMARY KEY,
        provider_id TEXT DEFAULT 'EXTERNAL_API',
        occurrence_count INTEGER DEFAULT 1,
        last_seen_utc TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS core_artists (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        sort_name TEXT,
        country TEXT,
        region TEXT,
        formed_year INTEGER,
        ended_year INTEGER,
        artist_type TEXT,
        gender TEXT,
        aliases TEXT,
        musicbrainz_artist_id TEXT UNIQUE,
        state TEXT DEFAULT 'NEW',
        is_locked INTEGER DEFAULT 0
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS core_albums (
        id TEXT PRIMARY KEY,
        artist_id TEXT,
        title TEXT NOT NULL,
        album_type TEXT DEFAULT 'Album',
        release_date TEXT,
        original_release_date TEXT,
        remaster_year INTEGER,
        catalog_number TEXT,
        barcode TEXT,
        musicbrainz_release_id TEXT,
        release_group_mbid TEXT,
        state TEXT DEFAULT 'NEW',
        is_locked INTEGER DEFAULT 0,
        FOREIGN KEY (artist_id) REFERENCES core_artists(id) ON DELETE SET NULL
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS core_recordings (
        id TEXT PRIMARY KEY,
        song_id TEXT,
        release_id TEXT,
        album_id TEXT,
        artist_id TEXT,
        title TEXT NOT NULL,
        release_date TEXT,
        original_release_date TEXT,
        isrc TEXT,
        musicbrainz_recording_id TEXT,
        acoustid_id TEXT,
        track_number INTEGER,
        disc_number INTEGER DEFAULT 1,
        album_tracks_count INTEGER,
        state TEXT DEFAULT 'NEW',
        is_locked INTEGER DEFAULT 0,
        FOREIGN KEY (album_id) REFERENCES core_albums(id) ON DELETE SET NULL,
        FOREIGN KEY (artist_id) REFERENCES core_artists(id) ON DELETE SET NULL
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS core_assets (
        asset_id TEXT PRIMARY KEY,
        recording_id TEXT,
        md5_file TEXT,
        sha256_file TEXT,
        audio_stream_hash TEXT,
        duration REAL,
        format TEXT,
        bitrate INTEGER,
        sample_rate INTEGER,
        channels INTEGER,
        file_size INTEGER,
        state TEXT DEFAULT 'NEW',
        FOREIGN KEY (recording_id) REFERENCES core_recordings(id) ON DELETE CASCADE
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS core_asset_locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id TEXT,
        filepath TEXT NOT NULL,
        mounted_drive TEXT DEFAULT 'LOCAL',
        is_available INTEGER DEFAULT 1,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (asset_id) REFERENCES core_assets(asset_id) ON DELETE CASCADE
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS core_sonic_features (
        recording_id TEXT PRIMARY KEY,
        bpm REAL,
        beat_confidence REAL,
        danceability REAL,
        spectral_centroid REAL,
        spectral_flux REAL,
        spectral_rolloff REAL,
        zero_crossing_rate REAL,
        key_signature TEXT,
        lufs_loudness REAL,
        dynamic_range REAL,
        acousticness REAL,
        instrumentalness REAL,
        valence REAL,
        extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (recording_id) REFERENCES core_recordings(id) ON DELETE CASCADE
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS meta_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        value TEXT,
        evidence_class TEXT DEFAULT 'LOCAL',
        source_id TEXT NOT NULL,
        run_id TEXT,
        payload_hash TEXT,
        confidence REAL DEFAULT 1.0,
        origin_type TEXT DEFAULT 'RESOLVER',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        raw_value TEXT,
        token_index INTEGER DEFAULT 0,
        token_delimiter TEXT,
        positional_weight REAL DEFAULT 1.0,
        UNIQUE(entity_id, field_name, source_id, value)
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS meta_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        entity_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        old_value TEXT,
        selected_value TEXT,
        reason TEXT,
        rejected_values_json TEXT,
        decision_hash TEXT,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS meta_locks (
        entity_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        lock_state TEXT DEFAULT 'AUTOMATIC',
        PRIMARY KEY (entity_id, field_name)
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS meta_validation (
        recording_id TEXT PRIMARY KEY,
        quality_score REAL DEFAULT 0.0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (recording_id) REFERENCES core_recordings(id) ON DELETE CASCADE
    );
    """)

    op.execute("""
    CREATE TABLE IF NOT EXISTS meta_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id TEXT NOT NULL,
        issue_code TEXT NOT NULL,
        severity TEXT DEFAULT 'WARNING',
        status TEXT DEFAULT 'OPEN',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP
    );
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS meta_issues;")
    op.execute("DROP TABLE IF EXISTS meta_validation;")
    op.execute("DROP TABLE IF EXISTS meta_locks;")
    op.execute("DROP TABLE IF EXISTS meta_decisions;")
    op.execute("DROP TABLE IF EXISTS meta_evidence;")
    op.execute("DROP TABLE IF EXISTS core_sonic_features;")
    op.execute("DROP TABLE IF EXISTS core_asset_locations;")
    op.execute("DROP TABLE IF EXISTS core_assets;")
    op.execute("DROP TABLE IF EXISTS core_recordings;")
    op.execute("DROP TABLE IF EXISTS core_albums;")
    op.execute("DROP TABLE IF EXISTS core_artists;")
    op.execute("DROP TABLE IF EXISTS sys_unmapped_tags;")
    op.execute("DROP TABLE IF EXISTS sys_fastpass_cache;")
    op.execute("DROP TABLE IF EXISTS sys_payloads;")
    op.execute("DROP TABLE IF EXISTS sys_runs;")