"""
External API Gateway Service
Provides external provider API lookups (MusicBrainz, AcoustID, Discogs, Deezer, iTunes, Last.fm, Wikidata)
with rate limiting, caching, health tracking, Lucene query sanitization, core title extraction, and multi-tier query relaxation.
"""

import re
import urllib.parse
import difflib
import unicodedata
from typing import List, Dict, Any, Optional, Tuple

from config.settings import settings
from utils.net import execute_http_request
from utils.text import sanitize_artist_name
from services.tax import BoundedLRUCache
from domain.events import event_bus, LogEvent

_artist_cache = BoundedLRUCache(maxsize=1000)

def sanitize_title_for_search(title: str) -> str:
    if not title:
        return ""
    cleaned = unicodedata.normalize("NFC", str(title)).strip()
    cleaned = cleaned.replace("’", "'").replace("`", "'")
    cleaned = cleaned.strip('"' + "'" + '“”‘’')
    cleaned = re.sub(r"[\–\—\-]", " ", cleaned)
    cleaned = re.sub(r"\bPart\s+I\s*[\-\–\—]?\s*V\b", "Part 1-5", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bPart\s+VI\s*[\-\–\—]?\s*IX\b", "Part 6-9", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"[\(\[\-]\s*(20\d\d|19\d\d)?\s*(remastered|remaster|deluxe edition|bonus track|anniversary edition|single version|album version|5\.1 mix|stereo|mono|digital remaster)\s*[\)\]]?",
        "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\s+(feat\.?|ft\.?)\s+.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace('/', ' ').replace('&', 'and')
    return re.sub(r"\s+", " ", cleaned).strip()

def extract_core_title(title: str) -> str:
    if not title:
        return ""
    clean = sanitize_title_for_search(title)
    clean_short = re.sub(r"\s*[\(\[\{].*?[\)\]\}]\s*", " ", clean).strip()
    clean_short = re.sub(r"\s*:\s*.*$", "", clean_short).strip()
    clean_short = re.sub(r"\.+$", "", clean_short).strip()
    return re.sub(r"\s+", " ", clean_short).strip()

def clean_for_lucene_query(text: str) -> str:
    if not text:
        return ""
    cleaned = str(text).replace("’", "'").replace("`", "'")
    cleaned = re.sub(r'[+\-&&||!(){}\[\]^"~*?:\\/]', " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()

def calculate_similarity(str1: Optional[str], str2: Optional[str]) -> float:
    if not str1 or not str2:
        return 0.0
    clean1 = "".join(c.lower() for c in str(str1) if c.isalnum())
    clean2 = "".join(c.lower() for c in str(str2) if c.isalnum())
    if not clean1 or not clean2:
        return 0.0
    if clean1 == clean2:
        return 1.0
    return difflib.SequenceMatcher(None, clean1, clean2).ratio()

def _extract_primary_artist_name(candidate: Dict[str, Any]) -> str:
    artist_credits = candidate.get("artist-credit", [])
    if artist_credits and isinstance(artist_credits, list) and isinstance(artist_credits[0], dict):
        credit = artist_credits[0]
        return credit.get("name") or credit.get("artist", {}).get("name") or ""
    return ""

class ExternalAPIService:
    @staticmethod
    def query_acoustid(
        duration: float,
        fingerprint: str,
        local_artist: Optional[str] = None,
        local_title: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        params = {
            "client": settings.ACOUSTID_CLIENT_KEY,
            "meta": "recordingids+recordings+releasegroups+compress",
            "duration": int(duration),
            "fingerprint": fingerprint
        }
        res_data, ctx = execute_http_request("ACOUSTID", "https://api.acoustid.org/v2/lookup", params=params)
        if not res_data or res_data.get("status") != "ok":
            return None, None, None

        results = res_data.get("results", [])
        if not results or not isinstance(results, list):
            return None, None, None

        clean_local_artist = sanitize_artist_name(local_artist or "")
        clean_local_title = extract_core_title(local_title or "")

        best_mbid_match = None
        best_mbid_score = -1.0

        best_acoustid_only = None
        best_acoustid_score = -1.0

        for res in results:
            if not isinstance(res, dict):
                continue
            raw_score = float(res.get("score", 0.0))
            acoustid_id = res.get("id")
            recs = res.get("recordings", [])

            if acoustid_id and raw_score > best_acoustid_score:
                best_acoustid_score = raw_score
                best_acoustid_only = acoustid_id

            if not recs:
                continue

            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                mb_rec_id = rec.get("id")
                if not mb_rec_id:
                    continue

                rec_title = rec.get("title") or ""
                rec_artists = rec.get("artists", [])
                art_name = rec_artists[0].get("name") if (rec_artists and isinstance(rec_artists, list) and isinstance(rec_artists[0], dict)) else ""

                art_sim = calculate_similarity(clean_local_artist, sanitize_artist_name(art_name)) if (clean_local_artist and art_name) else 0.85
                tit_sim = calculate_similarity(clean_local_title, extract_core_title(rec_title)) if (clean_local_title and rec_title) else 0.85

                composite_score = raw_score * (0.35 * art_sim + 0.35 * tit_sim + 0.30 * raw_score)

                if composite_score > best_mbid_score:
                    best_mbid_score = composite_score
                    best_mbid_match = (acoustid_id, mb_rec_id, ctx.endpoint_url)

        if best_mbid_match:
            return best_mbid_match

        if best_acoustid_only:
            return (best_acoustid_only, None, ctx.endpoint_url)

        return None, None, None

    @staticmethod
    def query_musicbrainz_recording(mb_rec_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/recording/{mb_rec_id}"
        params = {"inc": "isrcs+releases+artist-credits+tags+release-groups+url-rels+media", "fmt": "json"}
        res_data, ctx = execute_http_request("MUSICBRAINZ", endpoint, params=params)
        return res_data, ctx.endpoint_url if res_data else (None, None)

    @staticmethod
    def query_musicbrainz_by_isrc(isrc_code: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/isrc/{isrc_code}"
        params = {"inc": "artist-credits+releases+tags+release-groups", "fmt": "json"}
        res_data, ctx = execute_http_request("MUSICBRAINZ", endpoint, params=params)
        if res_data and isinstance(res_data.get("recordings"), list) and res_data["recordings"]:
            rec = res_data["recordings"][0]
            return rec, rec.get("id"), ctx.endpoint_url
        return None, None, None

    @staticmethod
    def query_musicbrainz_by_search(
        title: str,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        duration_sec: Optional[float] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        clean_title = sanitize_title_for_search(title)
        core_title = extract_core_title(title)
        art_clean = sanitize_artist_name(artist or "")

        lucene_title = clean_for_lucene_query(clean_title)
        lucene_core_title = clean_for_lucene_query(core_title)
        lucene_artist = clean_for_lucene_query(art_clean)

        if not lucene_title and not lucene_core_title:
            return None, None, None

        query_tiers = []

        # Tier 1: Core Short Title + Clean Sanitized Artist
        if lucene_core_title and lucene_artist and lucene_artist.lower() != "unknown artist":
            query_tiers.append(("Tier 1 (Core Title + Artist)", f'recording:"{lucene_core_title}" AND artist:"{lucene_artist}"'))

        # Tier 2: Full Title + Clean Artist
        if lucene_title and lucene_title != lucene_core_title and lucene_artist and lucene_artist.lower() != "unknown artist":
            query_tiers.append(("Tier 2 (Full Title + Artist)", f'recording:"{lucene_title}" AND artist:"{lucene_artist}"'))

        # Tier 3: Core Title Grouped
        if lucene_core_title and lucene_artist and lucene_artist.lower() != "unknown artist":
            query_tiers.append(("Tier 3 (Core Title Grouped)", f'recording:({lucene_core_title}) AND artist:({lucene_artist})'))

        # Tier 4: Broad Query Fallback
        if lucene_core_title and lucene_artist and lucene_artist.lower() != "unknown artist":
            query_tiers.append(("Tier 4 (Broad Terms)", f'"{lucene_core_title}" AND "{lucene_artist}"'))

        # Tier 5: Core Title Only
        if lucene_core_title:
            query_tiers.append(("Tier 5 (Core Title Only)", f'recording:"{lucene_core_title}"'))

        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/recording"

        for tier_label, query_str in query_tiers:
            res_data, ctx = execute_http_request("MUSICBRAINZ", endpoint, params={"query": query_str, "fmt": "json"})
            if not res_data or not isinstance(res_data.get("recordings"), list) or not res_data["recordings"]:
                continue

            candidates = res_data["recordings"]
            best_cand = None
            best_score = -1.0

            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                cand_title = cand.get("title", "")
                cand_art_name = _extract_primary_artist_name(cand)

                t_sim_full = calculate_similarity(clean_title, cand_title)
                t_sim_core = calculate_similarity(core_title, cand_title)
                t_sim = max(t_sim_full, t_sim_core)

                a_sim = calculate_similarity(art_clean, sanitize_artist_name(cand_art_name)) if art_clean else 0.85

                dur_penalty = 0.0
                cand_length_ms = cand.get("length")
                if duration_sec and cand_length_ms:
                    cand_dur_sec = float(cand_length_ms) / 1000.0
                    delta_sec = abs(duration_sec - cand_dur_sec)
                    if delta_sec > 60.0:
                        continue
                    elif delta_sec > 15.0:
                        dur_penalty = 0.10

                adj_score = (0.50 * t_sim + 0.50 * a_sim) - dur_penalty

                if t_sim >= 0.40 and a_sim >= 0.40 and adj_score > best_score:
                    best_score = adj_score
                    best_cand = cand

            if best_cand:
                return best_cand, best_cand.get("id"), ctx.endpoint_url

        return None, None, None

    @staticmethod
    def query_musicbrainz_release_group(artist_name: str, album_title: str) -> Optional[Dict[str, Any]]:
        clean_album = clean_for_lucene_query(extract_core_title(album_title))
        clean_artist = clean_for_lucene_query(sanitize_artist_name(artist_name or ""))
        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/release-group"
        query = f'releasegroup:"{clean_album}" AND artist:"{clean_artist}" AND primarytype:album'
        res_data, _ = execute_http_request("MUSICBRAINZ", endpoint, params={"query": query, "fmt": "json"})
        if res_data and isinstance(res_data.get("release-groups"), list) and res_data["release-groups"]:
            return res_data["release-groups"][0]
        return None

    @staticmethod
    def query_musicbrainz_release_tracklist(release_group_id: str) -> Optional[Dict[str, Any]]:
        endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/release-group/{release_group_id}"
        res_data, _ = execute_http_request("MUSICBRAINZ", endpoint, params={"inc": "releases+tags", "fmt": "json"})
        if not res_data or not isinstance(res_data.get("releases"), list) or not res_data["releases"]:
            return None

        rg_tags = [t.get("name") for t in res_data.get("tags", []) if isinstance(t, dict) and t.get("name")]
        target_rel_id = res_data["releases"][0].get("id")
        if not target_rel_id:
            return None

        rel_endpoint = f"{settings.MUSICBRAINZ_BASE_URL}/release/{target_rel_id}"
        rel_data, _ = execute_http_request("MUSICBRAINZ", rel_endpoint, params={"inc": "recordings+isrcs+artist-credits+labels+tags", "fmt": "json"})
        if not rel_data:
            return None

        tracks_list = []
        for media in rel_data.get("media", []):
            if isinstance(media, dict):
                for trk in media.get("tracks", []):
                    if isinstance(trk, dict):
                        rec = trk.get("recording", {})
                        isrcs = rec.get("isrcs", []) if isinstance(rec, dict) else []
                        tracks_list.append({
                            "position": trk.get("position"),
                            "title": trk.get("title") or rec.get("title"),
                            "recording_mbid": rec.get("id") if isinstance(rec, dict) else None,
                            "isrc": isrcs[0] if (isrcs and isinstance(isrcs, list)) else None
                        })

        rel_tags = [t.get("name") for t in rel_data.get("tags", []) if isinstance(t, dict) and t.get("name")]

        return {
            "first_release_date": res_data.get("first-release-date"),
            "tracks": tracks_list,
            "tags": list(set(rg_tags + rel_tags))
        }

    @staticmethod
    def query_wikidata_artist(artist_mbid: str) -> Optional[Dict[str, Any]]:
        cached = _artist_cache.get(artist_mbid)
        if cached is not None:
            return cached

        sparql_query = f"""
        SELECT ?item ?wikidataQID ?countryCode ?inception ?genderLabel ?artistTypeLabel WHERE {{
            ?item wdt:P434 "{artist_mbid}" .
            OPTIONAL {{ ?item wdt:P495/wdt:P297 ?countryCode . }}
            OPTIONAL {{ ?item wdt:P571 ?inception . }}
            OPTIONAL {{ ?item wdt:P21/rdfs:label ?genderLabel . FILTER(LANG(?genderLabel) = "en") }}
            OPTIONAL {{ ?item wdt:P31/rdfs:label ?artistTypeLabel . FILTER(LANG(?artistTypeLabel) = "en") }}
            BIND(STRAFTER(STR(?item), "http://www.wikidata.org/entity/") AS ?wikidataQID)
        }} LIMIT 1
        """
        params = {"query": sparql_query, "format": "json"}
        post_data = urllib.parse.urlencode(params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        res_data, ctx = execute_http_request(
            "WIKIDATA", settings.WIKIDATA_SPARQL_URL, method="POST", post_data=post_data, headers=headers
        )
        if not res_data or not isinstance(res_data, dict):
            _artist_cache.put(artist_mbid, None)
            return None

        bindings = res_data.get("results", {}).get("bindings", [])
        if bindings and isinstance(bindings, list) and isinstance(bindings[0], dict):
            b = bindings[0]
            inc_str = b.get("inception", {}).get("value", "") if isinstance(b.get("inception"), dict) else ""
            formed_yr = int(inc_str[:4]) if (inc_str and len(inc_str) >= 4 and inc_str[:4].isdigit()) else None
            res = {
                "wikidata_qid": b.get("wikidataQID", {}).get("value") if isinstance(b.get("wikidataQID"), dict) else None,
                "country": b.get("countryCode", {}).get("value") if isinstance(b.get("countryCode"), dict) else None,
                "gender": b.get("genderLabel", {}).get("value") if isinstance(b.get("genderLabel"), dict) else None,
                "artist_type": b.get("artistTypeLabel", {}).get("value") if isinstance(b.get("artistTypeLabel"), dict) else None,
                "formed_year": formed_yr,
                "payload_hash": ctx.endpoint_url
            }
            _artist_cache.put(artist_mbid, res)
            return res

        _artist_cache.put(artist_mbid, None)
        return None

    @staticmethod
    def query_deezer_isrc(artist: str, title: str) -> Optional[str]:
        clean_title = extract_core_title(title)
        query = f'artist:"{sanitize_artist_name(artist)}" track:"{clean_title}"'
        res_data, _ = execute_http_request("DEEZER", "https://api.deezer.com/search", params={"q": query})
        if res_data and isinstance(res_data.get("data"), list) and res_data["data"]:
            first = res_data["data"][0]
            if isinstance(first, dict):
                isrc = first.get("isrc")
                if isrc and len(str(isrc)) == 12:
                    return str(isrc).upper()
        return None

    @staticmethod
    def query_itunes_track(artist: str, title: str) -> Optional[Dict[str, Any]]:
        term = f"{sanitize_artist_name(artist)} {extract_core_title(title)}"
        res_data, ctx = execute_http_request("ITUNES", "https://itunes.apple.com/search", params={"term": term, "entity": "song", "limit": 1})
        if res_data and isinstance(res_data.get("results"), list) and res_data["results"]:
            best = res_data["results"][0]
            if isinstance(best, dict):
                rel_date = best.get("releaseDate", "")[:10] if best.get("releaseDate") else None
                return {
                    "album": best.get("collectionName"),
                    "genre": best.get("primaryGenreName"),
                    "release_date": rel_date,
                    "payload_hash": ctx.endpoint_url
                }
        return None

    @staticmethod
    def query_discogs_release(artist: str, album: str) -> Optional[Dict[str, Any]]:
        if not settings.DISCOGS_CONSUMER_KEY or not artist or not album:
            return None
        params = {
            "artist": sanitize_artist_name(artist),
            "release_title": extract_core_title(album),
            "type": "release",
            "key": settings.DISCOGS_CONSUMER_KEY,
            "secret": settings.DISCOGS_CONSUMER_SECRET
        }
        res_data, ctx = execute_http_request("DISCOGS", "https://api.discogs.com/database/search", params=params)
        if res_data and isinstance(res_data.get("results"), list) and res_data["results"]:
            best = res_data["results"][0]
            if isinstance(best, dict):
                barcodes = best.get("barcode", [])
                styles = (best.get("style") or []) if isinstance(best.get("style"), list) else []
                genres = (best.get("genre") or []) if isinstance(best.get("genre"), list) else []
                return {
                    "catno": best.get("catno"),
                    "barcode": str(barcodes[0]).strip() if (barcodes and isinstance(barcodes, list)) else None,
                    "styles": styles + genres,
                    "payload_hash": ctx.endpoint_url
                }
        return None

    @staticmethod
    def query_lastfm_tags(artist: str, title: str) -> List[Tuple[str, float]]:
        if not settings.LASTFM_API_KEY or not artist or not title:
            return []
        params = {
            "method": "track.gettoptags", "artist": sanitize_artist_name(artist),
            "track": extract_core_title(title),
            "api_key": settings.LASTFM_API_KEY, "format": "json"
        }
        res_data, _ = execute_http_request("LASTFM", "https://ws.audioscrobbler.com/2.0/", params=params)
        if not res_data or not isinstance(res_data, dict):
            return []
        toptags = res_data.get("toptags", {}).get("tag", []) if isinstance(res_data.get("toptags"), dict) else []
        if not isinstance(toptags, list):
            return []

        results = []
        for t in toptags[:8]:
            if isinstance(t, dict) and t.get("name"):
                try:
                    count_val = float(t.get("count", 50))
                except (ValueError, TypeError):
                    count_val = 50.0
                results.append((str(t["name"]), count_val))
        return results