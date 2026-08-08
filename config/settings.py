"""
MDMS Configuration Settings
Loads environment configurations from .env, defines system directory paths, and builds config hashes.
"""

import os
import json
import hashlib
import multiprocessing
from pathlib import Path
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    BASE_DIR = Path(__file__).resolve().parent.parent
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=True)
    else:
        load_dotenv(override=True)
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"

@dataclass(frozen=True)
class Settings:
    APP_NAME: str = "Music Data Management System Workstation"
    APP_VERSION: str = "5.0.0"

    # PostgreSQL Configuration
    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "postgres")
    POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost")
    POSTGRES_PORT: int = int(os.getenv("POSTGRES_PORT", "5432"))
    POSTGRES_DB: str = os.getenv("POSTGRES_DB", "mdms_db")

    @property
    def POSTGRES_URL(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # Directory Paths
    INPUT_DIR: Path = DATA_DIR / "input"
    ARCHIVE_DIR: Path = DATA_DIR / "archive"
    CORRUPTED_DIR: Path = DATA_DIR / "corrupted"
    FEATURES_DIR: Path = DATA_DIR / "features"
    EXPORTS_DIR: Path = DATA_DIR / "exports"
    COVERS_DIR: Path = DATA_DIR / "covers"
    TAXONOMY_DIR: Path = DATA_DIR / "taxonomy"
    DISCOVERY_CACHE_DIR: Path = DATA_DIR / "discovery_cache"
    BIN_DIR: Path = BASE_DIR / ".bin"

    # API Keys & Egress Gateway
    MUSICBRAINZ_EMAIL: str = os.getenv("MDMS_MB_EMAIL", "developer@mdms.local")
    MUSICBRAINZ_BASE_URL: str = os.getenv("MUSICBRAINZ_BASE_URL", "https://musicbrainz.org/ws/2").rstrip('/')
    HTTP_PROXIES: str = os.getenv("HTTP_PROXIES", "")

    ACOUSTID_CLIENT_KEY: str = os.getenv("ACOUSTID_KEY", "mpiUlRlYaU")
    LASTFM_API_KEY: str = os.getenv("LASTFM_KEY", "")
    DISCOGS_CONSUMER_KEY: str = os.getenv("DISCOGS_KEY", "")
    DISCOGS_CONSUMER_SECRET: str = os.getenv("DISCOGS_SECRET", "")
    
    LISTENBRAINZ_TOKEN: str = os.getenv("LISTENBRAINZ_TOKEN", "")
    LISTENBRAINZ_BASE_URL: str = os.getenv("LISTENBRAINZ_BASE_URL", "https://api.listenbrainz.org/1").rstrip('/')
    WIKIDATA_SPARQL_URL: str = os.getenv("WIKIDATA_SPARQL_URL", "https://query.wikidata.org/sparql")

    MAX_WORKER_PROCESSES: int = max(4, multiprocessing.cpu_count())
    DB_TIMEOUT: float = 30.0
    ACQUISITION_SETTLE_TIMEOUT: float = 12.0
    RATE_LIMIT_INTERVAL: float = float(os.getenv("RATE_LIMIT_INTERVAL", "1.0"))

    # Cloudflare Edge Proxy Rate Limits
    RATE_LIMIT_MB_REPLENISHMENT: float = float(os.getenv("RATE_LIMIT_MB_REPLENISHMENT", "64.0"))
    RATE_LIMIT_MB_BURST: float = float(os.getenv("RATE_LIMIT_MB_BURST", "128.0"))

    def get_config_hash(self) -> str:
        config_dict = {
            "app_version": self.APP_VERSION, 
            "backend": "POSTGRES",
            "max_workers": self.MAX_WORKER_PROCESSES, 
            "rate_limit": self.RATE_LIMIT_INTERVAL,
            "mb_url": self.MUSICBRAINZ_BASE_URL,
            "lb_url": self.LISTENBRAINZ_BASE_URL
        }
        return hashlib.sha256(json.dumps(config_dict, sort_keys=True).encode("utf-8")).hexdigest()

    def ensure_directories(self) -> None:
        for path in [
            self.INPUT_DIR, self.ARCHIVE_DIR, self.CORRUPTED_DIR, 
            self.FEATURES_DIR, self.EXPORTS_DIR, self.COVERS_DIR, 
            self.TAXONOMY_DIR, self.DISCOVERY_CACHE_DIR, self.BIN_DIR
        ]:
            path.mkdir(parents=True, exist_ok=True)

settings = Settings()
settings.ensure_directories()