"""
Stage A Local Ingestion Engine
Handles parallel media file parsing, triple hashing, composite fast-pass cache matching,
live Watchdog directory scanning, and direct column metadata resolution.
"""

import os
import re
import json
import gzip
import time
import shutil
import hashlib
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ProcessPoolExecutor

import soundfile as sf
import mutagen
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

from config.settings import settings
from domain.models import (
    AudioAsset, Evidence, ParserResult, Recording, TelemetryEvent, generate_ulid
)
from domain.events import event_bus, LogEvent
from domain.exceptions import IngestionError
from db import get_connection, db_transaction, close_thread_connection
from utils.fpcalc import generate_chromaprint, suppress_c_stderr
from utils.profile import get_system_resource_telemetry
from utils.text import clean_path_seed, normalize_artist_name
from services.tax import normalize_tag_alias, TaxonomyService
from services.resolve import SymbolicInferenceEngine, ResolutionPersistenceAdapter, get_current_field_value

PARSER_VERSION = "5.0.0"

def restore_archived_files_to_input() -> int:
    """Restores files from archive and corrupted directories back to the input folder for re-ingestion."""
    valid_exts = (".flac", ".mp3", ".m4a", ".wav", ".ogg")
    restored_count = 0

    dirs_to_scan = [settings.ARCHIVE_DIR, settings.CORRUPTED_DIR]
    for src_dir in dirs_to_scan:
        if not os.path.exists(src_dir):
            continue

        for root, _, files in os.walk(src_dir):
            for f in files:
                if f.lower().endswith(valid_exts):
                    src_file = os.path.join(root, f)
                    try:
                        move_file_safe(src_file, settings.INPUT_DIR, f)
                        restored_count += 1
                    except Exception as ex:
                        event_bus.publish(LogEvent(f"[-] Failed to restore archived file {f}: {ex}", "WARNING"))

    return restored_count

class FastBootGuard:
    """Calculates active state hashes to determine if a clean, cached boot can be bypassed."""
    @staticmethod
    def compute_state_hash() -> str:
        try:
            with db_transaction() as cursor:
                cursor.execute("SELECT MAX(run_id) FROM sys_runs")
                last_run_row = cursor.fetchone()
                last_run = last_run_row[0] if last_run_row and last_run_row[0] else "NONE"
                
                cursor.execute("SELECT COUNT(*) FROM core_recordings")
                rec_count = cursor.fetchone()[0] or 0

            input_dir = str(settings.INPUT_DIR)
            input_files_count = 0
            dir_mtime = 0.0
            
            if os.path.exists(input_dir):
                st = os.stat(input_dir)
                dir_mtime = st.st_mtime
                input_files_count = len(os.listdir(input_dir))
            
            raw_str = f"{last_run}_{rec_count}_{input_files_count}_{dir_mtime:.2f}_{settings.get_config_hash()}"
            return hashlib.md5(raw_str.encode("utf-8")).hexdigest()
        except Exception:
            return generate_ulid()

    @staticmethod
    def is_clean_boot() -> bool:
        try:
            current_hash = FastBootGuard.compute_state_hash()
            with db_transaction() as tx:
                tx.execute("SELECT result_json FROM sys_fastpass_cache WHERE cache_key = 'last_maint_state'")
                cached = tx.fetchone()

                if cached and cached[0] == current_hash:
                    return True

                tx.execute("""
                    INSERT INTO sys_fastpass_cache (cache_key, result_json, created_at)
                    VALUES ('last_maint_state', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        result_json = EXCLUDED.result_json,
                        created_at = CURRENT_TIMESTAMP
                """, (current_hash,))
            return False
        except Exception:
            return False

class CompositeFastPassCache:
    """Saves sub-millisecond local file re-parsing cycles by checking file metadata signature histories."""
    @staticmethod
    def compute_file_cache_key(filepath: str) -> str:
        try:
            st = os.stat(filepath)
            file_stat_str = f"{st.st_size}_{st.st_mtime}"
            with open(filepath, "rb") as f:
                header_bytes = f.read(65536)
        except Exception:
            file_stat_str = filepath
            header_bytes = b""

        raw_bytes = (
            file_stat_str.encode("utf-8") +
            header_bytes +
            settings.get_config_hash().encode("utf-8") +
            PARSER_VERSION.encode("utf-8")
        )
        return hashlib.md5(raw_bytes).hexdigest()

    @staticmethod
    def get_cached_result(cache_key: str) -> Optional[ParserResult]:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM sys_fastpass_cache WHERE cache_key = %s", (cache_key,))
            row = cursor.fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                asset_dict = data.get("asset")
                asset = AudioAsset(**asset_dict) if asset_dict else None
                evidence_list = [Evidence(**e) for e in data.get("evidence", [])]
                
                return ParserResult(
                    status="FAST_PASS_HIT",
                    filepath=data.get("filepath", ""),
                    filename=data.get("filename", ""),
                    asset=asset,
                    evidence=evidence_list,
                    payload_hash=data.get("payload_hash")
                )
        except Exception:
            pass
        return None

    @staticmethod
    def store_cached_result(cache_key: str, result: ParserResult) -> None:
        if StageAIngestionEngine._cancel_requested or result.status not in ("SUCCESS", "FAST_PASS_HIT") or not result.asset:
            return
        try:
            asset_dict = result.asset.__dict__ if result.asset else None
            evidence_dicts = [e.__dict__ for e in result.evidence]
            
            payload = {
                "filepath": result.filepath,
                "filename": result.filename,
                "asset": asset_dict,
                "evidence": evidence_dicts,
                "payload_hash": result.payload_hash
            }

            with db_transaction() as tx:
                tx.execute("""
                    INSERT INTO sys_fastpass_cache (cache_key, result_json, created_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        result_json = EXCLUDED.result_json,
                        created_at = CURRENT_TIMESTAMP
                """, (cache_key, json.dumps(payload, default=str)))
        except Exception:
            pass

class DeduplicationEvaluator:
    """Prevents file, stream, fingerprint, or exact metadata collisions prior to database entry."""
    @staticmethod
    def evaluate(
        asset: AudioAsset, 
        cursor: Any, 
        fingerprint: Optional[str] = None,
        title: Optional[str] = None,
        artist: Optional[str] = None
    ) -> Optional[str]:
        cursor.execute("SELECT asset_id FROM core_assets WHERE md5_file = %s", (asset.md5_file,))
        if cursor.fetchone():
            return "FILE_DUPLICATE"

        cursor.execute("SELECT asset_id FROM core_assets WHERE audio_stream_hash = %s", (asset.audio_stream_hash,))
        if cursor.fetchone():
            return "STREAM_DUPLICATE"

        if fingerprint and asset.duration > 0:
            cursor.execute("""
                SELECT a.asset_id FROM core_assets a
                JOIN meta_evidence e ON a.recording_id = e.entity_id
                WHERE e.field_name = 'fingerprint' AND e.value = %s AND ABS(a.duration - %s) <= 2.0
            """, (fingerprint, asset.duration))
            if cursor.fetchone():
                return "FINGERPRINT_DUPLICATE"

        if title and artist:
            norm_art = normalize_artist_name(artist)
            norm_title = str(title).strip().casefold()
            cursor.execute("""
                SELECT r.id FROM core_recordings r
                JOIN core_artists a ON r.artist_id = a.id
                WHERE LOWER(r.title) = %s
            """, (norm_title,))
            possible_recs = cursor.fetchall()
            for (rec_id,) in possible_recs:
                cursor.execute("SELECT name FROM core_artists WHERE id = (SELECT artist_id FROM core_recordings WHERE id = %s)", (rec_id,))
                art_row = cursor.fetchone()
                if art_row and normalize_artist_name(art_row[0]) == norm_art:
                    return "TITLE_ARTIST_DUPLICATE"

        return None

class UniversalMediaParser:
    """Performs target-independent extraction of audio headers, hashes, and embedded Vorbis/ID3 tags."""
    @staticmethod
    def calculate_file_hashes(filepath: str) -> Tuple[str, str]:
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                md5.update(chunk)
                sha256.update(chunk)
        return md5.hexdigest(), sha256.hexdigest()

    @staticmethod
    def calculate_pcm_stream_hash(filepath: str) -> Tuple[str, float, int, int, int]:
        try:
            stream_md5 = hashlib.md5()
            with suppress_c_stderr():
                with sf.SoundFile(filepath) as audio_file:
                    duration = float(len(audio_file)) / float(audio_file.samplerate)
                    samplerate = audio_file.samplerate
                    channels = audio_file.channels
                    bitrate = getattr(audio_file, "bitrate", 0)

                    for block in audio_file.blocks(blocksize=65536, dtype="int16"):
                        stream_md5.update(block.tobytes())

            return stream_md5.hexdigest(), duration, samplerate, channels, bitrate
        except Exception:
            file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            fallback_md5 = hashlib.md5()
            with open(filepath, "rb") as f:
                f.seek(min(file_size, 8192))
                for chunk in iter(lambda: f.read(65536), b""):
                    fallback_md5.update(chunk)
            return fallback_md5.hexdigest(), 0.0, 44100, 2, 0

    @staticmethod
    def parse_path_seeds(filepath: str) -> Dict[str, Any]:
        seeds = {}
        cleaned_stem = clean_path_seed(filepath)
        if cleaned_stem:
            seeds["title"] = cleaned_stem

        path_str = str(filepath).lower()
        disc_no = 1
        m_disc = re.search(r"\b(disc|cd|vol|volume|side|vinyl|vol\.)\s*([0-9a-f]+)", path_str)
        if m_disc:
            val = m_disc.group(2)
            if val.isdigit():
                disc_no = int(val)
            elif val in ('a', 'b', 'c', 'd'):
                disc_no = ord(val) - ord('a') + 1
        seeds["disc_number"] = disc_no

        return seeds

    @staticmethod
    def parse_native_tags(filepath: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        ext = os.path.splitext(filepath)[1].lower()
        extracted = {}
        raw_dump = {}

        try:
            with suppress_c_stderr():
                audio = mutagen.File(filepath)
            if audio is None:
                return extracted, raw_dump

            if hasattr(audio, 'info') and getattr(audio.info, 'bitrate', None):
                extracted["bitrate"] = int(audio.info.bitrate)

            try:
                for k, v in audio.items():
                    if str(k).startswith("APIC") or str(k).startswith("COVERART"):
                        continue
                    raw_dump[str(k)] = str(v)
            except Exception:
                raw_dump = {"info": "parsed"}

            def get_tag_val(keys: List[str]) -> Optional[str]:
                for k in keys:
                    try:
                        val = audio.get(k)
                        if val is not None:
                            if hasattr(val, 'text') and isinstance(val.text, (list, tuple)) and len(val.text) > 0:
                                v_str = " ; ".join(str(t).strip() for t in val.text if str(t).strip())
                            elif isinstance(val, (list, tuple)) and len(val) > 0:
                                v_str = " ; ".join(str(t).strip() for t in val if str(t).strip())
                            else:
                                v_str = str(val).strip()

                            if v_str and v_str.upper() not in ("NONE", "NULL", "UNKNOWN"):
                                return v_str
                    except Exception:
                        pass
                return None

            if ext in (".flac", ".ogg"):
                extracted["title"] = get_tag_val(["title", "TITLE"])
                extracted["artist"] = get_tag_val(["artist", "ARTIST"])
                extracted["album"] = get_tag_val(["album", "ALBUM"])
                extracted["release_date"] = get_tag_val(["date", "DATE", "year", "YEAR"])
                extracted["isrc"] = get_tag_val(["isrc", "ISRC"])
                extracted["genre"] = get_tag_val(["genre", "GENRE"])
                extracted["subgenre"] = get_tag_val(["subgenre", "SUBGENRE"])
                extracted["track_number"] = get_tag_val(["tracknumber", "TRACKNUMBER"])
                extracted["musicbrainz_recording_id"] = get_tag_val(["musicbrainz_trackid", "MUSICBRAINZ_TRACKID"])

            elif ext == ".mp3":
                extracted["title"] = get_tag_val(["TIT2", "title"])
                extracted["artist"] = get_tag_val(["TPE1", "artist"])
                extracted["album"] = get_tag_val(["TALB", "album"])
                extracted["release_date"] = get_tag_val(["TDRC", "TYER", "date"])
                extracted["isrc"] = get_tag_val(["TSRC", "isrc"])
                extracted["genre"] = get_tag_val(["TCON", "genre"])
                extracted["subgenre"] = get_tag_val(["TXXX:SUBGENRE", "TXXX:subgenre"])
                extracted["track_number"] = get_tag_val(["TRCK", "track_number"])
                extracted["musicbrainz_recording_id"] = get_tag_val(["TXXX:MUSICBRAINZ_TRACKID", "UFID:http://musicbrainz.org"])

        except Exception:
            pass

        return extracted, raw_dump

def process_file_worker(args: Tuple[str, str]) -> ParserResult:
    filepath, run_id = args
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()[1:].upper()

    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return ParserResult(status="CORRUPTED", filepath=filepath, filename=filename, error_msg="File not found")

    try:
        file_size = os.path.getsize(filepath)
        md5_f, sha256_f = UniversalMediaParser.calculate_file_hashes(filepath)
        pcm_hash, duration, samplerate, channels, bitrate = UniversalMediaParser.calculate_pcm_stream_hash(filepath)
        extracted_tags, raw_dump = UniversalMediaParser.parse_native_tags(filepath)
        path_seeds = UniversalMediaParser.parse_path_seeds(filepath)
        duration_fp, fingerprint = generate_chromaprint(filepath)

        effective_bitrate = extracted_tags.get("bitrate") or bitrate or 0

    except Exception as e:
        return ParserResult(status="CORRUPTED", filepath=filepath, filename=filename, error_msg=str(e))

    raw_payload_bytes = json.dumps(raw_dump, default=str).encode("utf-8")
    compressed_payload = gzip.compress(raw_payload_bytes)
    payload_hash = hashlib.sha256(raw_payload_bytes).hexdigest()

    asset = AudioAsset(
        asset_id=generate_ulid(), md5_file=md5_f, sha256_file=sha256_f,
        audio_stream_hash=pcm_hash or md5_f, duration=duration_fp or duration or 0.0,
        format=ext or "MP3", bitrate=effective_bitrate, sample_rate=samplerate or 44100,
        channels=channels or 2, file_size=file_size, state="PARSED"
    )

    evidence_list: List[Evidence] = []
    if fingerprint and duration_fp:
        evidence_list.append(Evidence(field_name="fingerprint", value=fingerprint, evidence_class="LOCAL", source_id="SRC_FPCALC", run_id=run_id, payload_hash=payload_hash))

    for field_name, val in extracted_tags.items():
        if field_name == "bitrate": continue
        if val and str(val).strip():
            raw_val_str = str(val).strip()
            if len(raw_val_str) > 2000:
                raw_val_str = raw_val_str[:2000]

            if field_name in ("genre", "subgenre"):
                segments = re.split(r"[;|\x00\r\n,/]+", raw_val_str)
                for idx, seg in enumerate(segments):
                    clean_norm = normalize_tag_alias(seg)
                    if clean_norm:
                        pos_weight = round(0.75 ** idx, 4)
                        evidence_list.append(Evidence(
                            field_name=field_name, value=clean_norm, evidence_class="LOCAL",
                            source_id="SRC_EMBEDDED_TAGS", run_id=run_id, payload_hash=payload_hash,
                            confidence=pos_weight, raw_value=seg.strip()[:2000], token_index=idx, positional_weight=pos_weight
                        ))
            else:
                evidence_list.append(Evidence(field_name=field_name, value=raw_val_str, evidence_class="LOCAL", source_id="SRC_EMBEDDED_TAGS", run_id=run_id, payload_hash=payload_hash))

    for field_name, val in path_seeds.items():
        if val and str(val).strip():
            evidence_list.append(Evidence(field_name=field_name, value=str(val).strip(), evidence_class="LOCAL", source_id="SRC_PATH_SEED", run_id=run_id, payload_hash=payload_hash))

    return ParserResult(
        status="SUCCESS", filepath=filepath, filename=filename,
        asset=asset, evidence=evidence_list,
        compressed_payload=compressed_payload, payload_hash=payload_hash
    )

def move_file_safe(src_path: str, target_dir: Path, base_filename: str) -> str:
    if not src_path or not os.path.exists(src_path):
        return ""
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_path = target_dir / base_filename
    if dest_path.exists():
        stem, ext = os.path.splitext(base_filename)
        dest_path = target_dir / f"{stem}_{generate_ulid()[:6]}{ext}"
    try:
        shutil.move(src_path, dest_path)
    except FileNotFoundError:
        return ""
    except OSError as e:
        if getattr(e, "errno", None) == 18:
            try:
                shutil.copy2(src_path, dest_path)
                os.unlink(src_path)
            except Exception:
                return ""
        else:
            raise e
    return str(dest_path)


class StageAIngestionEngine:
    _cancel_requested = False

    @classmethod
    def cancel_current(cls) -> None:
        cls._cancel_requested = True

    @staticmethod
    def force_reingest_library() -> int:
        restored = restore_archived_files_to_input()
        event_bus.publish(LogEvent(f"[+] Restored {restored} archived/corrupted file(s) back to data/input for re-ingestion.", "SUCCESS"))

        with db_transaction() as tx:
            tx.execute("DELETE FROM sys_fastpass_cache WHERE cache_key != 'last_maint_state'")

        return StageAIngestionEngine.run_stage_a()

    @staticmethod
    def run_stage_a(input_dir: Optional[str] = None, batch_size: int = 500) -> int:
        StageAIngestionEngine._cancel_requested = False
        start_time = time.time()
        target_dir = input_dir or str(settings.INPUT_DIR)
        valid_exts = (".flac", ".mp3", ".m4a", ".wav", ".ogg")
        files_to_process = [
            os.path.join(root, f)
            for root, _, files in os.walk(target_dir)
            for f in files if f.lower().endswith(valid_exts)
        ]

        total_files = len(files_to_process)
        if not files_to_process:
            event_bus.publish(LogEvent("[+] Stage A Ingestion: No files found in input directory."))
            return 0

        event_bus.publish(LogEvent(f"[*] Starting Stage A Local Ingestion pass on {total_files} file(s)..."))
        run_id = generate_ulid()
        with db_transaction() as cursor:
            cursor.execute("""
                INSERT INTO sys_runs (run_id, parser_version, config_hash)
                VALUES (%s, %s, %s)
            """, (run_id, PARSER_VERSION, settings.get_config_hash()))

        results: List[ParserResult] = []
        uncached_args: List[Tuple[str, str]] = []
        cache_keys: Dict[str, str] = {}

        for fp in files_to_process:
            if StageAIngestionEngine._cancel_requested:
                break
            ckey = CompositeFastPassCache.compute_file_cache_key(fp)
            cache_keys[fp] = ckey
            hit = CompositeFastPassCache.get_cached_result(ckey)
            if hit:
                results.append(hit)
            else:
                uncached_args.append((fp, run_id))

        if uncached_args and not StageAIngestionEngine._cancel_requested:
            with ProcessPoolExecutor(max_workers=settings.MAX_WORKER_PROCESSES) as executor:
                try:
                    for idx, res in enumerate(executor.map(process_file_worker, uncached_args)):
                        if StageAIngestionEngine._cancel_requested:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        results.append(res)
                        
                        if res.status in ("SUCCESS", "FAST_PASS_HIT") and res.filepath in cache_keys:
                            CompositeFastPassCache.store_cached_result(cache_keys[res.filepath], res)

                        processed = idx + 1
                        elapsed = max(0.001, time.time() - start_time)
                        tps = processed / elapsed
                        remaining = len(uncached_args) - processed
                        eta_sec = remaining / tps if tps > 0 else 0
                        eta_str = f"{int(eta_sec)}s left" if eta_sec > 0 else "Finishing..."
                        parse_pct = int((processed / len(uncached_args)) * 100)
                        total_pct = int(parse_pct * 0.50)
                        cpu_pct, ram_gb = get_system_resource_telemetry()

                        event_bus.publish(TelemetryEvent(
                            active_workers=settings.MAX_WORKER_PROCESSES,
                            queue_size=remaining,
                            throughput_tps=round(tps, 1),
                            cpu_pct=cpu_pct, ram_gb=ram_gb,
                            finish_est_str=f"Parsing ({eta_str})",
                            total_progress=total_pct, stage_progress=parse_pct, file_progress=100
                        ))
                except Exception as ex:
                    event_bus.publish(LogEvent(f"[-] Worker map error: {ex}", "ERROR"))

        imported_count = 0
        total_results = len(results)

        for i in range(0, total_results, batch_size):
            if StageAIngestionEngine._cancel_requested:
                break

            batch = results[i:i + batch_size]
            with db_transaction() as cursor:
                for res in batch:
                    if res.status not in ("SUCCESS", "FAST_PASS_HIT") or not res.asset:
                        try:
                            move_file_safe(res.filepath, settings.CORRUPTED_DIR, res.filename)
                        except Exception:
                            pass
                        continue

                    fp_val = next((e.value for e in res.evidence if e.field_name == "fingerprint"), None)
                    title_val = next((e.value for e in res.evidence if e.field_name == "title"), res.filename)
                    art_val = next((e.value for e in res.evidence if e.field_name == "artist"), None)

                    dup_type = DeduplicationEvaluator.evaluate(
                        res.asset, cursor, fingerprint=fp_val, title=title_val, artist=art_val
                    )
                    if dup_type:
                        try:
                            if os.path.exists(res.filepath):
                                os.remove(res.filepath)
                        except OSError:
                            pass
                        event_bus.publish(LogEvent(f"[*] Duplicate file ignored ({dup_type}): {res.filename}"))
                        continue

                    rec_id = generate_ulid()

                    cursor.execute("""
                        INSERT INTO sys_payloads (content_hash, payload_type, compressed_data, source)
                        VALUES (%s, 'PARSER_MUTAGEN', %s, 'Local Parser')
                        ON CONFLICT (content_hash) DO NOTHING
                    """, (res.payload_hash, res.compressed_payload))

                    cursor.execute("""
                        INSERT INTO core_recordings (id, title, state)
                        VALUES (%s, %s, 'PARSED')
                    """, (rec_id, res.filename))

                    cursor.execute("""
                        INSERT INTO core_assets (
                            asset_id, recording_id, md5_file, sha256_file, 
                            audio_stream_hash, duration, format, bitrate, 
                            sample_rate, channels, file_size, state
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PARSED')
                    """, (
                        res.asset.asset_id, rec_id, res.asset.md5_file, res.asset.sha256_file,
                        res.asset.audio_stream_hash, res.asset.duration, res.asset.format,
                        res.asset.bitrate, res.asset.sample_rate, res.asset.channels, res.asset.file_size
                    ))

                    archive_name = f"{res.asset.asset_id}.{res.asset.format.lower()}"
                    archive_path_str = move_file_safe(res.filepath, settings.ARCHIVE_DIR, archive_name)

                    cursor.execute("""
                        INSERT INTO core_asset_locations (asset_id, filepath, mounted_drive, is_available)
                        VALUES (%s, %s, 'LOCAL', 1)
                    """, (res.asset.asset_id, archive_path_str or res.filepath))

                    for ev in res.evidence:
                        cursor.execute("""
                            INSERT INTO meta_evidence (
                                entity_id, field_name, value, evidence_class, 
                                source_id, run_id, payload_hash, confidence, 
                                raw_value, token_index, positional_weight
                            ) VALUES (%s, %s, %s, 'LOCAL', %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                        """, (
                            rec_id, ev.field_name, ev.value, ev.source_id, 
                            run_id, res.payload_hash, ev.confidence, 
                            ev.raw_value or ev.value, ev.token_index, ev.positional_weight
                        ))

                    cursor.execute("""
                        INSERT INTO meta_validation (recording_id, quality_score)
                        VALUES (%s, 0.20)
                        ON CONFLICT(recording_id) DO NOTHING
                    """, (rec_id,))

                    embedded_genres = [
                        (e.value, e.confidence, e.source_id) 
                        for e in res.evidence if e.field_name in ("genre", "subgenre")
                    ]
                    if embedded_genres:
                        g_match = TaxonomyService.resolve_genres(embedded_genres, artist_name=art_val)
                        if g_match and g_match.primary_genre != "Unclassified":
                            cursor.execute("""
                                INSERT INTO meta_evidence (
                                    entity_id, field_name, value, evidence_class, source_id, run_id, 
                                    payload_hash, confidence, raw_value, token_index, positional_weight
                                ) VALUES (%s, 'genre', %s, 'DERIVED', 'SRC_TAXONOMY', %s, %s, %s, %s, 0, 1.0)
                                ON CONFLICT DO NOTHING
                            """, (rec_id, g_match.primary_genre, run_id, res.payload_hash, g_match.confidence, g_match.primary_genre))

                            if g_match.primary_subgenre:
                                cursor.execute("""
                                    INSERT INTO meta_evidence (
                                        entity_id, field_name, value, evidence_class, source_id, run_id, 
                                        payload_hash, confidence, raw_value, token_index, positional_weight
                                    ) VALUES (%s, 'subgenre', %s, 'DERIVED', 'SRC_TAXONOMY', %s, %s, %s, %s, 0, 1.0)
                                    ON CONFLICT DO NOTHING
                                """, (rec_id, g_match.primary_subgenre, run_id, res.payload_hash, g_match.confidence, g_match.primary_subgenre))

                    target_fields = ("title", "artist", "album", "genre", "subgenre", "release_date", "isrc", "musicbrainz_recording_id", "acoustid_id")
                    for field_name in target_fields:
                        cursor.execute("""
                            SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                                   payload_hash, confidence, origin_type, observed_at, raw_value, 
                                   token_index, token_delimiter, positional_weight
                            FROM meta_evidence WHERE entity_id = %s AND field_name = %s
                        """, (rec_id, field_name))
                        ev_rows = [Evidence(
                            id=r[0], entity_id=r[1], field_name=r[2], value=r[3], evidence_class=r[4],
                            source_id=r[5], run_id=r[6], payload_hash=r[7], confidence=r[8], origin_type=r[9],
                            observed_at=str(r[10]), raw_value=r[11], token_index=r[12], token_delimiter=r[13],
                            positional_weight=r[14]
                        ) for r in cursor.fetchall()]

                        if ev_rows:
                            try:
                                curr_val = get_current_field_value(cursor, rec_id, field_name)
                                decision = SymbolicInferenceEngine.resolve_field(rec_id, field_name, curr_val, ev_rows, run_id)
                                ResolutionPersistenceAdapter.apply_decision(decision)
                            except Exception as f_ex:
                                event_bus.publish(LogEvent(f"[-] Ingest resolution error on {field_name} for track {rec_id[:8]}: {f_ex}", "WARNING"))

                    imported_count += 1

            db_processed = min(total_results, i + len(batch))
            db_pct = int((db_processed / max(1, total_results)) * 100)
            total_pct = 50 + int(db_pct * 0.50)
            elapsed = max(0.001, time.time() - start_time)
            tps = db_processed / elapsed
            cpu_pct, ram_gb = get_system_resource_telemetry()

            event_bus.publish(TelemetryEvent(
                active_workers=settings.MAX_WORKER_PROCESSES,
                queue_size=total_results - db_processed,
                throughput_tps=round(tps, 1),
                cpu_pct=cpu_pct, ram_gb=ram_gb,
                finish_est_str="Resolving Metadata...",
                total_progress=total_pct, stage_progress=db_pct, file_progress=100
            ))

        with db_transaction() as cursor:
            cursor.execute("UPDATE sys_runs SET finished_at = CURRENT_TIMESTAMP WHERE run_id = %s", (run_id,))

        event_bus.publish(TelemetryEvent(0, 0, 0.0, 0.0, 0.0, "Idle", 100, 100, 100))
        return imported_count

class ThreadedWatcherHandler(PatternMatchingEventHandler):
    def __init__(self) -> None:
        super().__init__(patterns=["*.mp3", "*.flac", "*.wav", "*.m4a", "*.ogg"])

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        threading.Thread(target=self._process_file, args=(event.src_path,), daemon=True).start()

    def _process_file(self, file_path: str) -> None:
        # Wrap the filesystem thread operations cleanly inside connection pool close guards
        try:
            start_time = time.time()
            last_size = -1
            while time.time() - start_time < settings.ACQUISITION_SETTLE_TIMEOUT:
                try:
                    if os.path.exists(file_path):
                        with open(file_path, "rb") as f:
                            f.read(1024)
                        size = os.path.getsize(file_path)
                        if size == last_size and size > 0:
                            run_id = generate_ulid()
                            res = process_file_worker((file_path, run_id))
                            if res.status in ("SUCCESS", "FAST_PASS_HIT") and res.asset:
                                with db_transaction() as cursor:
                                    fp_val = next((e.value for e in res.evidence if e.field_name == "fingerprint"), None)
                                    title_val = next((e.value for e in res.evidence if e.field_name == "title"), res.filename)
                                    art_val = next((e.value for e in res.evidence if e.field_name == "artist"), None)

                                    if not DeduplicationEvaluator.evaluate(res.asset, cursor, fingerprint=fp_val, title=title_val, artist=art_val):
                                        rec_id = generate_ulid()
                                        cursor.execute("INSERT INTO core_recordings (id, title, state) VALUES (%s, %s, 'PARSED')", (rec_id, res.filename))
                                        cursor.execute("""
                                            INSERT INTO core_assets (asset_id, recording_id, md5_file, sha256_file, audio_stream_hash, duration, format, bitrate, sample_rate, channels, file_size, state)
                                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PARSED')
                                        """, (res.asset.asset_id, rec_id, res.asset.md5_file, res.asset.sha256_file, res.asset.audio_stream_hash, res.asset.duration, res.asset.format, res.asset.bitrate, res.asset.sample_rate, res.asset.channels, res.asset.file_size))
                                        
                                        archive_name = f"{res.asset.asset_id}.{res.asset.format.lower()}"
                                        archive_path_str = move_file_safe(file_path, settings.ARCHIVE_DIR, archive_name)
                                        cursor.execute("INSERT INTO core_asset_locations (asset_id, filepath, mounted_drive, is_available) VALUES (%s, %s, 'LOCAL', 1)", (res.asset.asset_id, archive_path_str or file_path))
                                        
                                        for ev in res.evidence:
                                            cursor.execute("""
                                                INSERT INTO meta_evidence (
                                                    entity_id, field_name, value, evidence_class, 
                                                    source_id, run_id, payload_hash, confidence, 
                                                    raw_value, token_index, positional_weight
                                                ) VALUES (%s, %s, %s, 'LOCAL', %s, %s, %s, %s, %s, %s, %s)
                                                ON CONFLICT DO NOTHING
                                            """, (
                                                rec_id, ev.field_name, ev.value, ev.source_id, 
                                                run_id, res.payload_hash, ev.confidence, 
                                                ev.raw_value or ev.value, ev.token_index, ev.positional_weight
                                            ))
                                        
                                        cursor.execute("INSERT INTO meta_validation (recording_id, quality_score) VALUES (%s, 0.20) ON CONFLICT DO NOTHING", (rec_id,))

                                        # Supply artist name context to watchdog resolver
                                        live_genres = [(e.value, e.confidence, e.source_id) for e in res.evidence if e.field_name in ("genre", "subgenre")]
                                        if live_genres:
                                            g_match = TaxonomyService.resolve_genres(live_genres, artist_name=art_val)
                                            if g_match:
                                                cursor.execute("UPDATE core_recordings SET genre = %s, subgenre = %s WHERE id = %s", (g_match.primary_genre, g_match.primary_subgenre, rec_id))

                                        for field_name in ("title", "artist", "album", "genre", "subgenre", "release_date", "isrc", "musicbrainz_recording_id", "acoustid_id"):
                                            cursor.execute("""
                                                SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                                                       payload_hash, confidence, origin_type, observed_at, raw_value, 
                                                       token_index, token_delimiter, positional_weight
                                                FROM meta_evidence WHERE entity_id = %s AND field_name = %s
                                            """, (rec_id, field_name))
                                            ev_rows = [Evidence(
                                                id=r[0], entity_id=r[1], field_name=r[2], value=r[3], evidence_class=r[4],
                                                source_id=r[5], run_id=r[6], payload_hash=r[7], confidence=r[8], origin_type=r[9],
                                                observed_at=str(r[10]), raw_value=r[11], token_index=r[12], token_delimiter=r[13],
                                                positional_weight=r[14]
                                            ) for r in cursor.fetchall()]

                                            if ev_rows:
                                                try:
                                                    curr_val = get_current_field_value(cursor, rec_id, field_name)
                                                    decision = SymbolicInferenceEngine.resolve_field(rec_id, field_name, curr_val, ev_rows, run_id)
                                                    ResolutionPersistenceAdapter.apply_decision(decision)
                                                except Exception as f_ex:
                                                    event_bus.publish(LogEvent(f"[-] Watchdog resolution error on {field_name}: {f_ex}", "WARNING"))

                                        event_bus.publish(LogEvent(f"[+] Live Watchdog ingested and resolved: {res.filename}", "SUCCESS"))
                            elif res.status == "CORRUPTED":
                                try:
                                    move_file_safe(file_path, settings.CORRUPTED_DIR, res.filename)
                                    event_bus.publish(LogEvent(f"[-] Quarantined corrupted file: {res.filename}", "WARNING"))
                                except Exception:
                                    pass
                            return
                        last_size = size
                except (PermissionError, OSError):
                    pass
                time.sleep(0.5)
        finally:
            close_thread_connection()

class WatchdogService:
    def __init__(self) -> None:
        self.observer: Optional[Observer] = None
        self.is_active = False

    def start(self) -> None:
        if not self.is_active:
            self.observer = Observer()
            self.observer.schedule(ThreadedWatcherHandler(), path=str(settings.INPUT_DIR), recursive=True)
            self.observer.start()
            self.is_active = True

    def stop(self) -> None:
        if self.is_active and self.observer:
            self.observer.stop()
            self.observer.join(timeout=5.0)
            self.is_active = False