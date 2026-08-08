"""
MDMS Database Package Entry Point
Exposes core database connection helpers and unified native PostgreSQL repositories.
"""

from db.core import (
    get_connection,
    close_thread_connection,
    db_transaction,
    get_postgres_engine,
    init_db_schema,
    wipe_database_clean,
    merge_duplicate_recordings_by_mbid
)

from db.repos import (
    RecordingRepo,
    ArtistRepo,
    AlbumRepo,
    EvidenceRepo,
    DiscoveryRepo,
    SonicRepo,
    UnitOfWork
)

__all__ = [
    "get_connection",
    "close_thread_connection",
    "db_transaction",
    "get_postgres_engine",
    "init_db_schema",
    "wipe_database_clean",
    "merge_duplicate_recordings_by_mbid",
    "RecordingRepo",
    "ArtistRepo",
    "AlbumRepo",
    "EvidenceRepo",
    "DiscoveryRepo",
    "SonicRepo",
    "UnitOfWork"
]