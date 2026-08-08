from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from domain.models import generate_ulid

@dataclass
class SonicFeature:
    recording_id: str
    bpm: Optional[float] = None
    beat_confidence: Optional[float] = None
    danceability: Optional[float] = None
    spectral_centroid: Optional[float] = None
    spectral_flux: Optional[float] = None
    spectral_rolloff: Optional[float] = None
    zero_crossing_rate: Optional[float] = None
    key_signature: Optional[str] = None
    lufs_loudness: Optional[float] = None
    dynamic_range: Optional[float] = None
    acousticness: Optional[float] = None
    instrumentalness: Optional[float] = None
    valence: Optional[float] = None
    extracted_at: Optional[str] = None

@dataclass
class AudioVector:
    recording_id: str
    dimensions: int = 512
    vector_data: bytes = b""
    model_name: str = "openl3_music_512"

@dataclass
class SonicSimilarityResult:
    target_recording_id: str
    matched_recording_id: str
    similarity_score: float
    distance_metric: str = "cosine"