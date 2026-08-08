"""
Network Transport & Edge Gateway
Provides httpx connection pooling, token bucket rate limiting, circuit breakers,
single-flight request coalescing, provider health tracking, and sys_payloads eviction.
"""

import os
import json
import gzip
import time
import hashlib
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple
from concurrent.futures import Future

import httpx

try:
    import h2
    HAS_H2 = True
except ImportError:
    HAS_H2 = False

from config.settings import settings
from domain.exceptions import CircuitBreakerOpenError
from db import db_transaction, get_connection

try:
    import diskcache
    HAS_DISKCACHE = True
except ImportError:
    HAS_DISKCACHE = False

_disk_cache = diskcache.Cache(str(settings.DISCOVERY_CACHE_DIR)) if HAS_DISKCACHE else None

def evict_old_payloads(max_total_payloads: int = 5000) -> int:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sys_payloads")
        total_cnt = cursor.fetchone()[0] or 0

        if total_cnt <= max_total_payloads:
            return 0

        excess = total_cnt - max_total_payloads
        with db_transaction() as tx:
            tx.execute("""
                DELETE FROM sys_payloads 
                WHERE content_hash IN (
                    SELECT content_hash FROM sys_payloads 
                    ORDER BY created_at ASC LIMIT %s
                )
            """, (excess,))
        return excess
    except Exception:
        return 0

@dataclass
class ProviderMetrics:
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    latency_ema_ms: float = 0.0
    last_error_type: Optional[str] = None
    last_error_msg: Optional[str] = None
    last_success_timestamp: Optional[str] = None

class ProviderHealthTracker:
    def __init__(self) -> None:
        self.metrics: Dict[str, ProviderMetrics] = defaultdict(ProviderMetrics)
        self._lock = threading.Lock()

    def record_success(self, provider_id: str, latency_ms: float) -> None:
        with self._lock:
            m = self.metrics[provider_id.upper()]
            m.total_requests += 1
            m.success_count += 1
            if m.latency_ema_ms == 0.0:
                m.latency_ema_ms = latency_ms
            else:
                m.latency_ema_ms = (0.2 * latency_ms) + (0.8 * m.latency_ema_ms)
            m.last_success_timestamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

    def record_failure(self, provider_id: str, error_type: str, error_msg: str) -> None:
        with self._lock:
            m = self.metrics[provider_id.upper()]
            m.total_requests += 1
            m.failure_count += 1
            m.last_error_type = error_type
            m.last_error_msg = error_msg

    def get_provider_metrics(self, provider_id: str) -> ProviderMetrics:
        with self._lock:
            return self.metrics[provider_id.upper()]

    def get_all_metrics(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                p: {
                    "total_requests": m.total_requests,
                    "success_count": m.success_count,
                    "failure_count": m.failure_count,
                    "latency_ema_ms": round(m.latency_ema_ms, 1),
                    "last_error_type": m.last_error_type,
                    "last_error_msg": m.last_error_msg,
                    "last_success_timestamp": m.last_success_timestamp
                } for p, m in self.metrics.items()
            }

health_tracker = ProviderHealthTracker()

class CircuitState:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 10, recovery_timeout: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = CircuitState.CLOSED
        self._lock = threading.RLock()

    def check_call(self) -> None:
        with self._lock:
            now = time.time()
            if self.state == CircuitState.OPEN:
                if now - self.last_failure_time > self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker OPEN for service. Retry in {int(self.recovery_timeout - (now - self.last_failure_time))}s."
                    )

    def record_success(self) -> None:
        with self._lock:
            self.failure_count = 0
            self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            cooldown_remaining = max(0.0, self.recovery_timeout - (now - self.last_failure_time)) if self.state == CircuitState.OPEN else 0.0
            return {
                "state": self.state,
                "failure_count": self.failure_count,
                "cooldown_remaining_sec": round(cooldown_remaining, 1)
            }

class TokenBucketRateLimiter:
    def __init__(self, fill_rate: float, capacity: float) -> None:
        self.fill_rate = float(fill_rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                delta = now - self.last_update
                self.tokens = min(self.capacity, self.tokens + delta * self.fill_rate)
                self.last_update = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                sleep_time = (tokens - self.tokens) / self.fill_rate
                time.sleep(max(0.001, sleep_time))

    def cooldown(self, seconds: float) -> None:
        with self._lock:
            self.last_update = time.monotonic() + max(0.5, float(seconds))


# Rate limit variables updated directly from centralized config settings
mb_replenish = settings.RATE_LIMIT_MB_REPLENISHMENT
mb_burst = settings.RATE_LIMIT_MB_BURST

mb_rate_limiter = TokenBucketRateLimiter(fill_rate=mb_replenish, capacity=mb_burst)
acoustid_rate_limiter = TokenBucketRateLimiter(fill_rate=10.0, capacity=20.0)
deezer_rate_limiter = TokenBucketRateLimiter(fill_rate=15.0, capacity=30.0)
lastfm_rate_limiter = TokenBucketRateLimiter(fill_rate=10.0, capacity=20.0)
discogs_rate_limiter = TokenBucketRateLimiter(fill_rate=2.0, capacity=5.0)
wikidata_rate_limiter = TokenBucketRateLimiter(fill_rate=5.0, capacity=10.0)

mb_circuit_breaker = CircuitBreaker(failure_threshold=12, recovery_timeout=20.0)
acoustid_circuit_breaker = CircuitBreaker()
deezer_circuit_breaker = CircuitBreaker()
lastfm_circuit_breaker = CircuitBreaker()
discogs_circuit_breaker = CircuitBreaker()
wikidata_circuit_breaker = CircuitBreaker()

_limiters = {
    "MUSICBRAINZ": mb_rate_limiter,
    "ACOUSTID": acoustid_rate_limiter,
    "DEEZER": deezer_rate_limiter,
    "LASTFM": lastfm_rate_limiter,
    "DISCOGS": discogs_rate_limiter,
    "WIKIDATA": wikidata_rate_limiter,
}

_breakers = {
    "MUSICBRAINZ": mb_circuit_breaker,
    "ACOUSTID": acoustid_circuit_breaker,
    "DEEZER": deezer_circuit_breaker,
    "LASTFM": lastfm_circuit_breaker,
    "DISCOGS": discogs_circuit_breaker,
    "WIKIDATA": wikidata_circuit_breaker,
}

def get_circuit_breaker_stats() -> Dict[str, Dict[str, Any]]:
    return {p_id: cb.get_stats() for p_id, cb in _breakers.items()}

_in_flight_requests: Dict[str, Future] = {}
_in_flight_lock = threading.Lock()

_http_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()

def get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        with _client_lock:
            if _http_client is None:
                proxy = settings.HTTP_PROXIES if settings.HTTP_PROXIES else None
                _http_client = httpx.Client(
                    http2=HAS_H2,
                    proxy=proxy,
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    headers={
                        "User-Agent": f"MDMS/{settings.APP_VERSION} ({settings.MUSICBRAINZ_EMAIL})",
                        "Accept-Encoding": "gzip, deflate"
                    }
                )
    return _http_client

def close_http_client() -> None:
    global _http_client
    with _client_lock:
        if _http_client is not None:
            try:
                _http_client.close()
            except Exception:
                pass
            _http_client = None

@dataclass(frozen=True)
class FetchContext:
    provider_id: str
    endpoint_url: str
    http_status_code: int
    latency_ms: float
    cache_status: str
    timestamp_utc: str

def generate_query_hash(provider_id: str, endpoint_url: str, params: Optional[Dict[str, Any]] = None) -> str:
    param_str = ""
    if params:
        sorted_p = sorted([(k, str(v)) for k, v in params.items() if k not in ("api_key", "cb", "token", "secret")])
        param_str = "&".join(f"{k}={v}" for k, v in sorted_p)
    raw_str = f"{provider_id}:{endpoint_url.lower().strip()}:{param_str}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

def execute_http_request(
    provider_id: str,
    endpoint_url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    method: str = "GET",
    post_data: Optional[bytes] = None,
    ttl_seconds: int = 86400 * 7,
    max_retries: int = 3
) -> Tuple[Optional[Dict[str, Any]], FetchContext]:
    provider_key = provider_id.upper()
    query_hash = generate_query_hash(provider_key, endpoint_url, params)
    now_utc = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

    start_time = time.time()
    elapsed_ms = 0.0
    status_code = 500
    cache_status = "NETWORK_MISS"
    res_data = None
    caught_exception = None

    if _disk_cache:
        try:
            if query_hash in _disk_cache:
                cached_item = _disk_cache[query_hash]
                ctx = FetchContext(
                    provider_id=provider_key, endpoint_url=endpoint_url,
                    http_status_code=304, latency_ms=1.0, cache_status="FRESH_HIT",
                    timestamp_utc=now_utc
                )
                health_tracker.record_success(provider_key, 1.0)
                return cached_item, ctx
        except Exception:
            pass

    for attempt in range(max_retries):
        is_producer = False
        with _in_flight_lock:
            if query_hash in _in_flight_requests:
                query_future = _in_flight_requests[query_hash]
            else:
                query_future = Future()
                _in_flight_requests[query_hash] = query_future
                is_producer = True

        if not is_producer:
            try:
                return query_future.result(timeout=12.0)
            except Exception as ex:
                elapsed_ms = (time.time() - start_time) * 1000.0
                ctx = FetchContext(
                    provider_id=provider_key, endpoint_url=endpoint_url,
                    http_status_code=500, latency_ms=elapsed_ms, cache_status="SINGLE_FLIGHT_ERROR",
                    timestamp_utc=now_utc
                )
                health_tracker.record_failure(provider_key, ex.__class__.__name__, str(ex))
                return None, ctx

        breaker = _breakers.get(provider_key)
        limiter = _limiters.get(provider_key)
        client = get_http_client()

        target_url = endpoint_url
        if provider_key == "MUSICBRAINZ" and attempt >= 2 and "musicbrainz-gateway" in target_url:
            target_url = target_url.replace("https://musicbrainz-gateway.yahya-ess123456.workers.dev/ws/2", "https://musicbrainz.org/ws/2")

        try:
            if breaker:
                try:
                    breaker.check_call()
                except CircuitBreakerOpenError:
                    if attempt < max_retries - 1:
                        time.sleep(1.5)
                        with _in_flight_lock:
                            _in_flight_requests.pop(query_hash, None)
                        continue
                    raise

            if limiter:
                limiter.acquire()

            req_headers = {}
            if headers:
                req_headers.update(headers)

            if method.upper() == "POST":
                response = client.post(target_url, params=params, content=post_data, headers=req_headers)
            else:
                response = client.get(target_url, params=params, headers=req_headers)

            status_code = response.status_code

            if status_code in (429, 503) and attempt < max_retries - 1:
                retry_after = response.headers.get("Retry-After")
                try:
                    cooldown_sec = float(retry_after) if retry_after else 1.5
                except ValueError:
                    cooldown_sec = 1.5
                if limiter:
                    limiter.cooldown(cooldown_sec)
                time.sleep(cooldown_sec)
                with _in_flight_lock:
                    _in_flight_requests.pop(query_hash, None)
                continue

            response.raise_for_status()
            res_data = response.json()

            elapsed_ms = (time.time() - start_time) * 1000.0
            if breaker:
                breaker.record_success()

            health_tracker.record_success(provider_key, elapsed_ms)

            if _disk_cache and res_data:
                try:
                    _disk_cache.set(query_hash, res_data, expire=ttl_seconds)
                except Exception:
                    pass

            try:
                raw_bytes = json.dumps(res_data).encode("utf-8")
                compressed = gzip.compress(raw_bytes)
                c_hash = hashlib.sha256(raw_bytes).hexdigest()

                with db_transaction() as cursor:
                    cursor.execute("""
                        INSERT INTO sys_payloads (content_hash, payload_type, compressed_data, source, checksum)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (c_hash, f"API_{provider_key}", compressed, provider_key, c_hash))
            except Exception:
                pass

            break

        except Exception as ex:
            caught_exception = ex
            elapsed_ms = (time.time() - start_time) * 1000.0
            if breaker:
                breaker.record_failure()
            health_tracker.record_failure(provider_key, ex.__class__.__name__, str(ex))
            if isinstance(ex, httpx.HTTPStatusError):
                status_code = ex.response.status_code

            if attempt < max_retries - 1:
                time.sleep(1.0)
                with _in_flight_lock:
                    _in_flight_requests.pop(query_hash, None)
                continue

        finally:
            elapsed_ms = (time.time() - start_time) * 1000.0
            ctx = FetchContext(
                provider_id=provider_key, endpoint_url=target_url,
                http_status_code=status_code, latency_ms=elapsed_ms,
                cache_status=cache_status, timestamp_utc=now_utc
            )
            with _in_flight_lock:
                f = _in_flight_requests.pop(query_hash, None)
                if f and is_producer:
                    if res_data is not None:
                        f.set_result((res_data, ctx))
                    else:
                        err = caught_exception or httpx.HTTPError(f"Request failed with status {status_code}")
                        f.set_exception(err)

    return res_data, ctx