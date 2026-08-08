"""
Discovery Background Crawler Daemon
"""

import time
import json
import hashlib
import urllib.parse
import threading
from typing import List, Dict, Any, Optional, Tuple

from config.settings import settings
from db.core import get_connection, db_transaction, close_thread_connection
from domain.events import event_bus, LogEvent, CrawlerTelemetryEvent
from services.discover import LocalLibraryIndexCache
from utils.net import execute_http_request, get_circuit_breaker_stats

# Exclude bootlegs and concert recordings from discovery candidates
LIVE_ALBUM_BLOCKLIST = [
    "live", "bootleg", "concert", "tour", "recorded live at", 
    "in session", "performance", "unplugged", "box set", "compilation"
]

class BackgroundCrawlerDaemon(threading.Thread):
    def __init__(self) -> None:
        super().__init__()
        self.daemon = True
        self._stop_requested = threading.Event()
        self._suspend_crawling = threading.Event()
        
        self.library_index = LocalLibraryIndexCache()
        self.total_crawled_count = 0
        self.active_seed_name = "IDLE"

        event_bus.subscribe(CrawlerTelemetryEvent, self._handle_telemetry)

    def stop(self) -> None:
        self._stop_requested.set()

    def suspend(self) -> None:
        self._suspend_crawling.set()
        self.active_seed_name = "SUSPENDED (Ingest Active)"

    def resume(self) -> None:
        self._suspend_crawling.clear()
        self.active_seed_name = "IDLE"

    def run(self) -> None:
        event_bus.publish(LogEvent("[+] Discovery Crawler Daemon: Service started successfully.", "SUCCESS"))
        
        while not self._stop_requested.is_set():
            if self._suspend_crawling.is_set():
                time.sleep(2.0)
                continue

            try:
                self._harvest_local_seeds_to_frontier()
                
                # Fetch next seed with PENDING status from frontier
                seed = self._acquire_next_frontier_seed()
                if not seed:
                    self.active_seed_name = "IDLE (No Pending Seeds)"
                    self._emit_telemetry(0)
                    time.sleep(10.0)
                    continue

                seed_id, seed_name, seed_type = seed
                
                # Dynamic Resolution: If seed is a dummy name, resolve it to an official MusicBrainz Artist ID
                if seed_id.startswith("name:"):
                    event_bus.publish(LogEvent(f"[*] Discovery Crawler: Resolving manual artist name seed '{seed_name}'..."))
                    resolved_mbid = self._resolve_artist_name_to_mbid(seed_name)
                    if resolved_mbid:
                        with db_transaction() as tx:
                            tx.execute("DELETE FROM sys_crawl_frontier WHERE seed_id = %s", (seed_id,))
                            tx.execute("""
                                INSERT INTO sys_crawl_frontier (seed_id, entity_name, entity_type, priority, state)
                                VALUES (%s, %s, 'ARTIST', 1.5, 'ACTIVE')
                                ON CONFLICT (seed_id) DO UPDATE SET state = 'ACTIVE'
                            """, (resolved_mbid, seed_name))
                        seed_id = resolved_mbid
                    else:
                        event_bus.publish(LogEvent(f"[-] Discovery Crawler: Failed to resolve MusicBrainz ID for '{seed_name}'.", "WARNING"))
                        self._mark_seed_cooldown(seed_id, "FAILED")
                        continue

                self.active_seed_name = f"Crawling: {seed_name} ({seed_type})"
                event_bus.publish(LogEvent(f"[*] Discovery Crawler: Crawling seed '{seed_name}'..."))

                candidates = []

                # Task 1: MusicBrainz Discography Expansion (Cloudflare Proxy)
                try:
                    mb_cands = self._crawl_musicbrainz_discography(seed_id, seed_name)
                    candidates.extend(mb_cands)
                except Exception as e:
                    event_bus.publish(LogEvent(f"[-] Discovery Crawler: MusicBrainz crawl failed: {e}", "WARNING"))

                # Task 2: Wikidata SPARQL Genre-Network Harvester
                try:
                    wiki_cands = self._crawl_wikidata_genre_network(seed_id, seed_name)
                    candidates.extend(wiki_cands)
                except Exception as e:
                    event_bus.publish(LogEvent(f"[-] Discovery Crawler: Wikidata crawl failed: {e}", "WARNING"))

                # Task 3: Last.fm Similarity Harvesting (If API Key is active)
                if settings.LASTFM_API_KEY:
                    try:
                        lfm_cands = self._crawl_lastfm_similarity(seed_name)
                        candidates.extend(lfm_cands)
                    except Exception as e:
                        event_bus.publish(LogEvent(f"[-] Discovery Crawler: Last.fm crawl failed: {e}", "WARNING"))

                accepted_count = 0
                if candidates:
                    self.library_index.reload()
                    accepted_count = self._process_and_insert_candidates(candidates)

                self._mark_seed_cooldown(seed_id, "COOLDOWN")
                self.total_crawled_count += accepted_count
                event_bus.publish(LogEvent(f"[+] Discovery Crawler: Crawled '{seed_name}' successfully. Found +{accepted_count} candidate(s).", "SUCCESS"))

                pending_size = self._get_pending_queue_size()
                self._emit_telemetry(pending_size)

            except Exception as e:
                event_bus.publish(LogEvent(f"[-] Discovery Crawler: Loop encountered error: {e}", "WARNING"))
                time.sleep(5.0)
            finally:
                close_thread_connection()

            # Enforce cooperative throttle interval
            time.sleep(settings.ACQUISITION_SETTLE_TIMEOUT)

    def _harvest_local_seeds_to_frontier(self) -> None:
        """Saves high-quality locally owned artists into the frontier, falling back to classic seeds on fresh setups."""
        try:
            with db_transaction() as tx:
                # 1. Reset any stuck 'ACTIVE' seeds back to 'PENDING'
                tx.execute("UPDATE sys_crawl_frontier SET state = 'PENDING' WHERE state = 'ACTIVE'")

                # 2. Query high-quality local artists with resolved MBIDs
                tx.execute("""
                    SELECT a.musicbrainz_artist_id, a.name 
                    FROM core_artists a
                    JOIN core_recordings r ON a.id = r.artist_id
                    JOIN meta_validation v ON r.id = v.recording_id
                    WHERE a.musicbrainz_artist_id IS NOT NULL AND v.quality_score >= 0.80
                    GROUP BY a.musicbrainz_artist_id, a.name
                    HAVING COUNT(r.id) >= 3
                """)
                local_seeds = tx.fetchall()

                for mbid, name in local_seeds:
                    tx.execute("""
                        INSERT INTO sys_crawl_frontier (seed_id, entity_name, entity_type, priority, state)
                        VALUES (%s, %s, 'ARTIST', 1.0, 'PENDING')
                        ON CONFLICT (seed_id) DO NOTHING
                    """, (mbid, name))

                # 3. Check for any active (PENDING/ACTIVE) seeds in the queue
                tx.execute("SELECT COUNT(*) FROM sys_crawl_frontier WHERE state IN ('PENDING', 'ACTIVE')")
                active_count = tx.fetchone()[0] or 0

                if active_count == 0:
                    tx.execute("""
                        SELECT DISTINCT musicbrainz_artist_id, name 
                        FROM core_artists 
                        WHERE musicbrainz_artist_id IS NOT NULL AND musicbrainz_artist_id != ''
                    """)
                    fallback_seeds = tx.fetchall()
                    
                    if not fallback_seeds:
                        # Database cold start fallback: Seed famous high-trust progressive artists
                        fallback_seeds = [
                            ('7624c4b4-13df-46b9-913a-afec893085a6', 'Aphex Twin'),
                            ('090cd75b-9fe3-4416-ab8c-7f8d386f78ee', 'Stereolab'),
                            ('82c357ca-9274-4ec2-9e90-58ab62bc6d79', 'Burial'),
                            ('d00b14f8-1c14-4e48-afcf-6c7be219965d', 'Boards of Canada'),
                            ('1985397e-1282-4115-9c8a-7e10884df002', 'Talk Talk')
                        ]
                        
                    for mbid, name in fallback_seeds:
                        tx.execute("""
                            INSERT INTO sys_crawl_frontier (seed_id, entity_name, entity_type, priority, state)
                            VALUES (%s, %s, 'ARTIST', 1.0, 'PENDING')
                            ON CONFLICT (seed_id) DO NOTHING
                        """, (mbid, name))
        except Exception as ex:
            event_bus.publish(LogEvent(f"[-] Discovery Crawler: Seed harvest failed: {ex}", "WARNING"))

    def _acquire_next_frontier_seed(self) -> Optional[Tuple[str, str, str]]:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT seed_id, entity_name, entity_type 
                FROM sys_crawl_frontier 
                WHERE state = 'PENDING' 
                ORDER BY priority DESC, created_at ASC LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                with db_transaction() as tx:
                    tx.execute("UPDATE sys_crawl_frontier SET state = 'ACTIVE', updated_at = CURRENT_TIMESTAMP WHERE seed_id = %s", (row[0],))
                return row
        except Exception:
            pass
        return None

    def _mark_seed_cooldown(self, seed_id: str, status: str) -> None:
        try:
            with db_transaction() as tx:
                tx.execute("""
                    UPDATE sys_crawl_frontier 
                    SET state = %s, last_crawled_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP 
                    WHERE seed_id = %s
                """, (status, seed_id))
        except Exception:
            pass

    def _get_pending_queue_size(self) -> int:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sys_crawl_frontier WHERE state = 'PENDING'")
            return cursor.fetchone()[0] or 0
        except Exception:
            return 0

    def _resolve_artist_name_to_mbid(self, artist_name: str) -> Optional[str]:
        """Queries MusicBrainz via Cloudflare Worker Proxy to search and resolve an artist's MBID by their name."""
        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/artist"
        # MusicBrainz search API Lucene syntax query parameter
        params = {"query": f'artist:"{artist_name}"', "fmt": "json"}
        res_data, _ = execute_http_request("MUSICBRAINZ", endpoint, params=params)
        
        if res_data and isinstance(res_data.get("artists"), list) and res_data["artists"]:
            return res_data["artists"][0].get("id")
        return None

    def _crawl_musicbrainz_discography(self, artist_mbid: str, artist_name: str) -> List[Dict[str, Any]]:
        """Queries MusicBrainz via Cloudflare Worker Proxy to discover outstanding release groups."""
        if not artist_mbid or len(artist_mbid) != 36:
            return []
        
        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/release-group"
        params = {"artist": artist_mbid, "type": "album", "fmt": "json"}
        res_data, _ = execute_http_request("MUSICBRAINZ", endpoint, params=params)
        
        if not res_data or not isinstance(res_data.get("release-groups"), list):
            return []

        candidates = []
        for rg in res_data["release-groups"]:
            title = rg.get("title")
            rg_mbid = rg.get("id")
            if title and rg_mbid:
                candidates.append({
                    "title": title,
                    "artist_name": artist_name,
                    "mbid": rg_mbid,
                    "genre": "Discography"
                })
        return candidates

    def _crawl_wikidata_genre_network(self, artist_mbid: str, artist_name: str) -> List[Dict[str, Any]]:
        """Queries Wikidata SPARQL to harvest release groups from artists sharing genres (aligned with performer P175 property)."""
        sparql_query = f"""
        SELECT DISTINCT ?releaseGroup ?title ?similarArtistLabel ?genreLabel WHERE {{
            ?artist wdt:P434 "{artist_mbid}" .
            ?artist wdt:P136 ?genre .
            ?similarArtist wdt:P136 ?genre .
            ?releaseGroup wdt:P175 ?similarArtist ;
                          wdt:P31 wd:Q482994 ;
                          rdfs:label ?title .
            ?similarArtist rdfs:label ?similarArtistLabel .
            ?genre rdfs:label ?genreLabel .
            FILTER(LANG(?title) = "en")
            FILTER(LANG(?similarArtistLabel) = "en")
            FILTER(LANG(?genreLabel) = "en")
        }} LIMIT 40
        """
        params = {"query": sparql_query, "format": "json"}
        post_data = urllib.parse.urlencode(params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        res_data, _ = execute_http_request(
            "WIKIDATA", settings.WIKIDATA_SPARQL_URL, method="POST", post_data=post_data, headers=headers
        )
        if not res_data or not isinstance(res_data, dict):
            return []

        bindings = res_data.get("results", {}).get("bindings", [])
        candidates = []
        for b in bindings:
            rg_uri = b.get("releaseGroup", {}).get("value", "")
            rg_mbid = rg_uri.split("/")[-1] if "/" in rg_uri else None
            title = b.get("title", {}).get("value", "")
            art_name = b.get("similarArtistLabel", {}).get("value", "")
            genre = b.get("genreLabel", {}).get("value", "Unclassified")

            if title and art_name:
                candidates.append({
                    "title": title,
                    "artist_name": art_name,
                    "mbid": rg_mbid,
                    "genre": genre
                })
        return candidates

    def _crawl_lastfm_similarity(self, artist_name: str) -> List[Dict[str, Any]]:
        """Queries Last.fm to discover similar artists and harvests their top-ranked release groups."""
        if not settings.LASTFM_API_KEY:
            return []
        
        endpoint = "http://ws.audioscrobbler.com/2.0/"
        params = {
            "method": "artist.getsimilar",
            "artist": artist_name,
            "api_key": settings.LASTFM_API_KEY,
            "format": "json",
            "limit": 5
        }
        res_data, _ = execute_http_request("LASTFM", endpoint, params=params)
        
        if not res_data or not isinstance(res_data, dict):
            return []
        
        similar_artists = res_data.get("similarartists", {}).get("artist", [])
        if not isinstance(similar_artists, list):
            return []

        candidates = []
        # Limit to top 3 similar artists to prevent API transaction thrashing
        for art in similar_artists[:3]:
            name = art.get("name")
            if not name:
                continue
            
            # For each similar artist, fetch their top-ranked albums
            album_params = {
                "method": "artist.gettopalbums",
                "artist": name,
                "api_key": settings.LASTFM_API_KEY,
                "format": "json",
                "limit": 4
            }
            album_data, _ = execute_http_request("LASTFM", endpoint, params=album_params)
            
            if album_data and isinstance(album_data, dict):
                albums = album_data.get("topalbums", {}).get("album", [])
                if isinstance(albums, list):
                    for alb in albums:
                        alb_title = alb.get("name")
                        mbid = alb.get("mbid")
                        if alb_title and alb_title.lower() != "(null)":
                            candidates.append({
                                "title": alb_title,
                                "artist_name": name,
                                "mbid": mbid if (mbid and len(mbid) == 36) else None,
                                "genre": "Similar"
                            })
        return candidates

    def _process_and_insert_candidates(self, candidates: List[Dict[str, Any]]) -> int:
        accepted_count = 0
        conn = get_connection()
        cursor = conn.cursor()

        # 1. Fetch local library genre representation stats to dynamically apply consensus weights
        library_genres = {}
        try:
            cursor.execute("""
                SELECT LOWER(genre), COUNT(*) 
                FROM core_recordings 
                WHERE genre IS NOT NULL AND genre != 'Unclassified'
                GROUP BY LOWER(genre)
            """)
            library_genres = {r[0]: r[1] for r in cursor.fetchall()}
        except Exception:
            pass

        max_library_count = max(library_genres.values()) if library_genres else 1

        # 2. Query dynamic user targeted crawl genre filter from database fastpass cache
        target_genre_filter = None
        try:
            cursor.execute("SELECT result_json FROM sys_fastpass_cache WHERE cache_key = 'active_crawl_target_genre'")
            row = cursor.fetchone()
            if row and row[0]:
                target_genre_filter = str(row[0]).strip().lower()
        except Exception:
            pass

        for cand in candidates:
            title = cand["title"]
            artist = cand["artist_name"]
            genre = cand["genre"]
            mbid = cand["mbid"]

            # Gate 1: Check Live-Album Blocklist
            title_lower = title.lower()
            if any(term in title_lower for term in LIVE_ALBUM_BLOCKLIST):
                continue

            # Gate 2: Apply dynamic target genre keyword checks if active
            if target_genre_filter and target_genre_filter not in genre.lower() and target_genre_filter not in title_lower:
                continue

            # Gate 3: Check Local Catalog Ownership
            if self.library_index.is_exact_owned(mbid=mbid):
                continue
            if self.library_index.is_fuzzy_owned(title, artist):
                continue

            # Gate 4: Calculate personalized genre representation weight ("Check and Weight")
            affinity_count = library_genres.get(genre.lower(), 0)
            affinity_factor = 1.0 + (affinity_count / max_library_count) * 0.20 # Up to a 1.20x Composite Score boost

            weighted_ccs = round(min(1.0, 0.80 * affinity_factor), 3)

            # Gate 5: Insert Candidate into Discovery Table
            try:
                cand_id = f"cand_{hashlib.md5((title + artist).encode('utf-8')).hexdigest()[:10]}"
                with db_transaction() as tx:
                    tx.execute("""
                        INSERT INTO sys_discovery_candidates (
                            candidate_id, title, artist_name, release_group_mbid, primary_genre, state, final_ccs
                        ) VALUES (%s, %s, %s, %s, %s, 'NEW', %s)
                        ON CONFLICT (candidate_id) DO NOTHING
                    """, (cand_id, title, artist, mbid, genre, weighted_ccs))
                accepted_count += 1
            except Exception:
                pass

        return accepted_count

    def _emit_telemetry(self, pending_size: int) -> None:
        try:
            stats = get_circuit_breaker_stats()
            event_bus.publish(CrawlerTelemetryEvent(
                active_seed=self.active_seed_name,
                pending_queue_size=pending_size,
                total_crawled_count=self.total_crawled_count,
                active_throttle_rate_sec=settings.ACQUISITION_SETTLE_TIMEOUT,
                circuit_breaker_states_json=json.dumps(stats)
            ))
        except Exception:
            pass

    def _handle_telemetry(self, event: Any) -> None:
        pass
