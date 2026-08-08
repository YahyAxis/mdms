import time
import os
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def generate_ulid() -> str:
    now_ms = int(time.time() * 1000)
    time_chars = []
    for _ in range(10):
        time_chars.append(CROCKFORD_BASE32[now_ms & 31])
        now_ms >>= 5
    time_str = "".join(reversed(time_chars))

    rand_bytes = os.urandom(10)
    rand_chars = []
    for b in rand_bytes:
        rand_chars.append(CROCKFORD_BASE32[b & 31])
    return time_str + "".join(rand_chars)

class JobState(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

class PipelineState(Enum):
    IDLE = "IDLE"
    RUNNING_INGESTION = "RUNNING_INGESTION"
    RUNNING_ENRICHMENT = "RUNNING_ENRICHMENT"
    RUNNING_REPLAY = "RUNNING_REPLAY"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"

class Severity(Enum):
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"

class CandidateState(Enum):
    NEW = "NEW"
    QUEUED = "QUEUED"
    HUNTING = "HUNTING"
    ACQUIRED = "ACQUIRED"
    SNOOZED = "SNOOZED"
    IGNORED = "IGNORED"

class AuthorityEntityType(Enum):
    ARTIST = "ARTIST"
    RELEASE_GROUP = "RELEASE_GROUP"
    RECORDING = "RECORDING"

class MatchMethod(Enum):
    EXACT_MBID = "EXACT_MBID"
    TRANSITIVE_CLOSURE = "TRANSITIVE_CLOSURE"
    FUZZY_CONSENSUS = "FUZZY_CONSENSUS"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

@dataclass
class GenreMatch:
    primary_genre: str = "Unclassified"
    primary_subgenre: str = "Unclassified"
    secondary_subgenres: List[str] = field(default_factory=list)
    confidence: float = 0.0

@dataclass
class AudioAsset:
    asset_id: str = field(default_factory=generate_ulid)
    recording_id: Optional[str] = None
    md5_file: str = ""
    sha256_file: str = ""
    audio_stream_hash: str = ""
    duration: float = 0.0
    format: str = "FLAC"
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    file_size: int = 0
    state: str = "NEW"

@dataclass
class AssetLocation:
    id: Optional[int] = None
    asset_id: str = ""
    filepath: str = ""
    mounted_drive: str = "LOCAL"
    is_available: int = 1
    last_seen: Optional[str] = None

@dataclass
class Recording:
    id: str = field(default_factory=generate_ulid)
    song_id: Optional[str] = None
    release_id: Optional[str] = None
    artist_id: Optional[str] = None
    album_id: Optional[str] = None
    title: str = "Untitled Work"
    release_date: Optional[str] = None
    original_release_date: Optional[str] = None
    isrc: Optional[str] = None
    musicbrainz_recording_id: Optional[str] = None
    acoustid_id: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: int = 1
    album_tracks_count: Optional[int] = None
    state: str = "NEW"
    is_locked: int = 0
    artist_name: str = "Unknown Artist"
    album_title: str = "Unknown Album"
    primary_genre: str = "Unclassified"
    primary_subgenre: str = "Unclassified"
    quality_score: float = 0.0
    filepath: str = ""
    duration: float = 0.0
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    format: str = "FLAC"

@dataclass
class Artist:
    id: str = field(default_factory=generate_ulid)
    name: str = "Unknown Artist"
    sort_name: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    formed_year: Optional[int] = None
    ended_year: Optional[int] = None
    artist_type: Optional[str] = None
    gender: Optional[str] = None
    aliases: Optional[str] = None
    musicbrainz_artist_id: Optional[str] = None
    state: str = "NEW"
    is_locked: int = 0

@dataclass
class Album:
    id: str = field(default_factory=generate_ulid)
    artist_id: Optional[str] = None
    title: str = "Unknown Album"
    album_type: str = "Album"
    release_date: Optional[str] = None
    original_release_date: Optional[str] = None
    remaster_year: Optional[int] = None
    catalog_number: Optional[str] = None
    barcode: Optional[str] = None
    musicbrainz_release_id: Optional[str] = None
    release_group_mbid: Optional[str] = None
    state: str = "NEW"
    is_locked: int = 0

@dataclass
class Evidence:
    id: Optional[int] = None
    entity_id: str = ""
    field_name: str = ""
    value: Optional[str] = None
    evidence_class: str = "LOCAL"
    source_id: str = ""
    run_id: str = ""
    payload_hash: Optional[str] = None
    confidence: float = 1.0
    origin_type: str = "RESOLVER"
    observed_at: Optional[str] = None
    raw_value: Optional[str] = None
    token_index: int = 0
    token_delimiter: Optional[str] = ";"
    positional_weight: float = 1.0

@dataclass
class Decision:
    id: Optional[int] = None
    run_id: str = ""
    entity_id: str = ""
    field_name: str = ""
    old_value: Optional[str] = None
    selected_value: Optional[str] = None
    reason: str = ""
    rejected_values_json: Optional[str] = None
    decision_hash: str = ""
    applied_at: Optional[str] = None

@dataclass
class IssueDetail:
    id: int
    entity_id: str
    recording_title: str
    issue_code: str
    severity: str
    status: str
    created_at: str

@dataclass
class Cohort:
    id: Optional[int] = None
    name: str = ""
    description: str = ""
    song_count: int = 0
    total_hours: float = 0.0
    artist_count: int = 0
    avg_quality: float = 0.0
    created: Optional[str] = None

@dataclass
class AuthorityMapping:
    id: str = field(default_factory=generate_ulid)
    entity_type: AuthorityEntityType = AuthorityEntityType.RELEASE_GROUP
    mbid: Optional[str] = None
    discogs_id: Optional[str] = None
    wikidata_qid: Optional[str] = None
    lastfm_name: Optional[str] = None
    confidence: float = 0.0
    match_method: MatchMethod = MatchMethod.EXACT_MBID
    evidence_json: str = "{}"
    evidence_hash: str = ""
    is_active: bool = True

@dataclass
class DiscoveryCandidate:
    candidate_id: str = field(default_factory=generate_ulid)
    authority_id: Optional[str] = None
    entity_type: str = "RELEASE_GROUP"
    title: str = ""
    artist_name: str = ""
    release_group_mbid: Optional[str] = None
    release_year: Optional[int] = None
    primary_genre: str = "Unclassified"
    primary_subgenre: str = "Unclassified"
    secondary_genres: List[str] = field(default_factory=list)
    state: CandidateState = CandidateState.NEW
    final_ccs: float = 0.0
    overall_confidence: float = 0.0

@dataclass(frozen=True)
class DiscoveryScore:
    candidate_id: str
    v_rel: float = 0.0
    v_coll: float = 0.0
    v_graph: float = 0.0
    v_def: float = 0.0
    p_sat: float = 1.0
    delta_fatigue: float = 1.0
    provider_factor: float = 1.0
    active_strategy: str = "Balanced Curator"
    explanation_json: str = "{}"

@dataclass(frozen=True)
class TelemetryEvent:
    active_workers: int
    queue_size: int
    throughput_tps: float
    cpu_pct: float
    ram_gb: float
    finish_est_str: str
    total_progress: int = 0
    stage_progress: int = 0
    file_progress: int = 0
    current_track_title: str = ""
    facts_count: int = 0
    cache_hits: int = 0

@dataclass(frozen=True)
class ParserResult:
    status: str
    filepath: str
    filename: str
    asset: Optional[AudioAsset] = None
    evidence: List[Evidence] = field(default_factory=list)
    compressed_payload: Optional[bytes] = None
    payload_hash: Optional[str] = None
    error_msg: Optional[str] = None

@dataclass(frozen=True)
class DuplicatePair:
    asset_a_id: str
    asset_b_id: str
    recording_title: str
    artist_name: str
    format_a: str
    format_b: str
    bitrate_a: int
    bitrate_b: int
    sample_rate_a: int
    sample_rate_b: int
    size_a_mb: float
    size_b_mb: float
    path_a: str
    path_b: str
    dup_type: str

@dataclass(frozen=True)
class JobRunDetail:
    run_id: str
    parser_version: str
    config_hash: str
    started_at: str
    finished_at: str
    status: str
    duration_str: str