import re
import json
import hashlib
import unicodedata
import difflib
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Set, Tuple

from config.settings import settings
from domain.models import Evidence, Decision, generate_ulid
from db import db_transaction, get_connection, ArtistRepo, AlbumRepo
from utils.text import EDITION_NOISE_PATTERN, EDITION_SUFFIX_PATTERN

RESOLVER_VERSION = "5.0.0"

# Register genre and subgenre as native table columns
ALLOWED_RECORDING_COLUMNS = {
    "title", "artist_id", "album_id", "release_date",
    "original_release_date", "isrc", "musicbrainz_recording_id", "acoustid_id",
    "genre", "subgenre"
}

MBID_REGEX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

class EvidenceFamily(Enum):
    USER_OVERRIDE = "USER_OVERRIDE"
    CANONICAL_DB = "CANONICAL_DB"
    COMMERCIAL_DB = "COMMERCIAL_DB"
    AUDIO_ANALYSIS = "AUDIO_ANALYSIS"
    LOCAL_FILES = "LOCAL_FILES"
    DERIVED_TAXONOMY = "DERIVED_TAXONOMY"
    LOCAL_HINTS = "LOCAL_HINTS"
    COMMUNITY_TAGS = "COMMUNITY_TAGS"

SOURCE_FAMILY_MAP: Dict[str, EvidenceFamily] = {
    "SRC_USER": EvidenceFamily.USER_OVERRIDE,
    "SRC_MUSICBRAINZ": EvidenceFamily.CANONICAL_DB,
    "SRC_MUSICBRAINZ_ISRC": EvidenceFamily.CANONICAL_DB,
    "SRC_MUSICBRAINZ_SEARCH": EvidenceFamily.CANONICAL_DB,
    "SRC_WIKIDATA": EvidenceFamily.CANONICAL_DB,
    "SRC_DISCOGS": EvidenceFamily.COMMERCIAL_DB,
    "SRC_DEEZER": EvidenceFamily.COMMERCIAL_DB,
    "SRC_ITUNES": EvidenceFamily.COMMERCIAL_DB,
    "SRC_ACOUSTID": EvidenceFamily.AUDIO_ANALYSIS,
    "SRC_FPCALC": EvidenceFamily.AUDIO_ANALYSIS,
    "SRC_EMBEDDED_TAGS": EvidenceFamily.LOCAL_FILES,
    "SRC_PATH_SEED": EvidenceFamily.LOCAL_HINTS,
    "SRC_ALBUM_SIBLING": EvidenceFamily.LOCAL_HINTS,
    "SRC_LASTFM": EvidenceFamily.COMMUNITY_TAGS,
    "SRC_LISTENBRAINZ": EvidenceFamily.COMMUNITY_TAGS,
    "SRC_TAXONOMY": EvidenceFamily.DERIVED_TAXONOMY
}

FIELD_FAMILY_PRIORS: Dict[str, Dict[EvidenceFamily, float]] = {
    "title": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.CANONICAL_DB: 0.95, EvidenceFamily.LOCAL_FILES: 0.85, EvidenceFamily.COMMERCIAL_DB: 0.80, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "artist": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.CANONICAL_DB: 0.95, EvidenceFamily.LOCAL_FILES: 0.85, EvidenceFamily.COMMERCIAL_DB: 0.80, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "album": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.CANONICAL_DB: 0.95, EvidenceFamily.LOCAL_FILES: 0.85, EvidenceFamily.COMMERCIAL_DB: 0.80, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "genre": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.LOCAL_FILES: 0.85, EvidenceFamily.CANONICAL_DB: 0.80, EvidenceFamily.COMMUNITY_TAGS: 0.70, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "subgenre": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.LOCAL_FILES: 0.85, EvidenceFamily.CANONICAL_DB: 0.80, EvidenceFamily.COMMUNITY_TAGS: 0.70, EvidenceFamily.DERIVED_TAXONOMY: 0.40},
    "release_date": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.CANONICAL_DB: 0.95, EvidenceFamily.COMMERCIAL_DB: 0.85, EvidenceFamily.LOCAL_FILES: 0.60, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "original_release_date": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.CANONICAL_DB: 0.99, EvidenceFamily.COMMERCIAL_DB: 0.85, EvidenceFamily.LOCAL_FILES: 0.60, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "isrc": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.CANONICAL_DB: 0.99, EvidenceFamily.COMMERCIAL_DB: 0.95, EvidenceFamily.LOCAL_FILES: 0.90, EvidenceFamily.DERIVED_TAXONOMY: 0.50},
    "catalog_number": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.COMMERCIAL_DB: 0.95, EvidenceFamily.CANONICAL_DB: 0.80, EvidenceFamily.LOCAL_FILES: 0.60},
    "barcode": {EvidenceFamily.USER_OVERRIDE: 1.50, EvidenceFamily.COMMERCIAL_DB: 0.95, EvidenceFamily.CANONICAL_DB: 0.80, EvidenceFamily.LOCAL_FILES: 0.60}
}

DEFAULT_FIELD_PRIORS: Dict[EvidenceFamily, float] = {
    EvidenceFamily.USER_OVERRIDE: 1.50,
    EvidenceFamily.CANONICAL_DB: 0.90,
    EvidenceFamily.COMMERCIAL_DB: 0.80,
    EvidenceFamily.AUDIO_ANALYSIS: 0.75,
    EvidenceFamily.LOCAL_FILES: 0.85,
    EvidenceFamily.DERIVED_TAXONOMY: 0.50,
    EvidenceFamily.LOCAL_HINTS: 0.40,
    EvidenceFamily.COMMUNITY_TAGS: 0.30
}

def get_current_field_value(cursor: Any, entity_id: str, field_name: str) -> Optional[str]:
    if field_name in ALLOWED_RECORDING_COLUMNS or field_name in ("title", "release_date", "original_release_date", "isrc", "musicbrainz_recording_id", "acoustid_id"):
        col = "title" if field_name == "title" else field_name
        cursor.execute(f"SELECT {col} FROM core_recordings WHERE id = %s", (entity_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return str(row[0])
    elif field_name == "artist":
        cursor.execute("""
            SELECT a.name FROM core_recordings r
            JOIN core_artists a ON r.artist_id = a.id
            WHERE r.id = %s
        """, (entity_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return str(row[0])
    elif field_name == "album":
        cursor.execute("""
            SELECT alb.title FROM core_recordings r
            JOIN core_albums alb ON r.album_id = alb.id
            WHERE r.id = %s
        """, (entity_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return str(row[0])
    
    cursor.execute("""
        SELECT value FROM meta_evidence 
        WHERE entity_id = %s AND field_name = %s AND value IS NOT NULL AND value != ''
        ORDER BY confidence DESC, id DESC LIMIT 1
    """, (entity_id, field_name))
    row = cursor.fetchone()
    return str(row[0]) if row and row[0] else None

class TitleNormalizer:
    @classmethod
    def normalize(cls, text: str) -> str:
        if not text:
            return ""
        norm = unicodedata.normalize("NFC", text).strip().casefold()
        norm = EDITION_NOISE_PATTERN.sub("", norm)
        return re.sub(r"\s+", " ", norm.strip('"' + "'" + '“”‘’')).strip()

class DateNormalizer:
    @classmethod
    def normalize(cls, text: str) -> str:
        if not text:
            return ""
        digits = "".join([c for c in text if c.isdigit()])
        if len(digits) >= 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        elif len(digits) >= 4:
            return digits[:4]
        return text.strip()

class ISRCNormalizer:
    ISRC_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$", re.IGNORECASE)

    @classmethod
    def normalize(cls, text: str) -> str:
        if not text or str(text).strip().upper() in ("NONE", "NULL", "UNKNOWN"):
            return ""
        clean = text.replace("-", "").strip().upper()
        return clean if cls.ISRC_PATTERN.match(clean) else ""

class MBIDNormalizer:
    @classmethod
    def normalize(cls, text: str) -> str:
        if not text:
            return ""
        m = MBID_REGEX.search(str(text))
        if m:
            return m.group(0).lower()
        clean = str(text).strip()
        return clean[:64]

class GenericNormalizer:
    @classmethod
    def normalize(cls, text: str) -> str:
        if not text:
            return ""
        norm = unicodedata.normalize("NFC", text).strip().casefold()
        return re.sub(r"\s+", " ", norm.strip('"' + "'" + '“”‘’')).strip()

class GenreNormalizer:
    @classmethod
    def normalize(cls, text: str) -> str:
        """Capitalizes each word and preserves formatting of hyphenated genre tokens cleanly."""
        if not text:
            return ""
        norm = unicodedata.normalize("NFC", text).strip()
        words = norm.split()
        title_words = []
        for w in words:
            # Handle styling of hyphenated words natively (e.g. singer-songwriter -> Singer-Songwriter)
            sub_words = w.split('-')
            title_sub = [sw.capitalize() for sw in sub_words]
            title_words.append("-".join(title_sub))
        
        norm_title = " ".join(title_words)
        return re.sub(r"\s+", " ", norm_title.strip('"' + "'" + '“”‘’')).strip()

class NormalizerRegistry:
    _normalizers = {
        "title": TitleNormalizer,
        "album": TitleNormalizer,
        "release_date": DateNormalizer,
        "original_release_date": DateNormalizer,
        "isrc": ISRCNormalizer,
        "musicbrainz_recording_id": MBIDNormalizer,
        "musicbrainz_release_id": MBIDNormalizer,
        "musicbrainz_artist_id": MBIDNormalizer,
        "genre": GenreNormalizer,
        "subgenre": GenreNormalizer
    }

    @classmethod
    def normalize(cls, field_name: str, value: str) -> str:
        norm_cls = cls._normalizers.get(field_name, GenericNormalizer)
        return norm_cls.normalize(value)

@dataclass
class RuleObservation:
    rule_name: str
    is_valid: bool
    detail: str

class BaseFieldRule:
    def evaluate(self, candidate: "CandidateNode", context: Dict[str, Any]) -> List[RuleObservation]:
        return []

class ArtistRule(BaseFieldRule):
    def evaluate(self, candidate: "CandidateNode", context: Dict[str, Any]) -> List[RuleObservation]:
        ref_artist = context.get("reference_artist")
        if not ref_artist:
            return []
        
        c1 = re.sub(r"\W+", "", candidate.normalized_value)
        c2 = re.sub(r"\W+", "", ref_artist.casefold())
        sim = difflib.SequenceMatcher(None, c1, c2).ratio() if (c1 and c2) else 1.0

        if sim < 0.40 and EvidenceFamily.USER_OVERRIDE not in candidate.supported_families:
            return [RuleObservation("ArtistRule", False, f"Severe artist contradiction against reference '{ref_artist}' (Similarity: {sim*100:.0f}%)")]
        return [RuleObservation("ArtistRule", True, "Artist similarity verified")]

class TitleRule(BaseFieldRule):
    def evaluate(self, candidate: "CandidateNode", context: Dict[str, Any]) -> List[RuleObservation]:
        if EDITION_SUFFIX_PATTERN.search(candidate.value):
            return [RuleObservation("TitleRule", True, "Contains edition suffix; canonical short form preferred")]
        return [RuleObservation("TitleRule", True, "Canonical title form")]

class ISRCRule(BaseFieldRule):
    ISRC_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$", re.IGNORECASE)

    def evaluate(self, candidate: "CandidateNode", context: Dict[str, Any]) -> List[RuleObservation]:
        clean = candidate.value.replace("-", "").strip()
        if self.ISRC_PATTERN.match(clean):
            return [RuleObservation("ISRCRule", True, "Valid ISO 3901 syntax")]
        candidate.is_invalidated = True
        return [RuleObservation("ISRCRule", False, "Fails ISO 3901 syntax validation")]

class SubgenreRule(BaseFieldRule):
    GENERIC_NAMES = {"rock", "jazz", "pop", "electronic", "classical", "metal", "hip hop", "r&b", "folk", "blues"}

    def evaluate(self, candidate: "CandidateNode", context: Dict[str, Any]) -> List[RuleObservation]:
        if candidate.normalized_value in self.GENERIC_NAMES:
            return [RuleObservation("SubgenreRule", False, "Generic root category penalized")]
        return [RuleObservation("SubgenreRule", True, "Specific subgenre boosted")]

class RuleRegistry:
    _rules: Dict[str, BaseFieldRule] = {
        "artist": ArtistRule(),
        "title": TitleRule(),
        "isrc": ISRCRule(),
        "subgenre": SubgenreRule()
    }

    @classmethod
    def get_rule(cls, field_name: str) -> Optional[BaseFieldRule]:
        return cls._rules.get(field_name)

@dataclass
class CandidateNode:
    value: str
    normalized_value: str
    supporting_evidence: List[Evidence] = field(default_factory=list)
    supported_families: Set[EvidenceFamily] = field(default_factory=set)
    observations: List[RuleObservation] = field(default_factory=list)
    is_invalidated: bool = False

    def get_trust_weight(self, field_name: str) -> float:
        if self.is_invalidated:
            return 0.00
        priors = FIELD_FAMILY_PRIORS.get(field_name, DEFAULT_FIELD_PRIORS)
        family_scores = []
        for fam in self.supported_families:
            base_prior = priors.get(fam, 0.30)
            fam_evidence = [
                e for e in self.supporting_evidence
                if SOURCE_FAMILY_MAP.get(e.source_id, EvidenceFamily.COMMUNITY_TAGS) == fam
            ]
            if fam_evidence:
                avg_conf = sum(
                    e.confidence * getattr(e, "positional_weight", 1.0)
                    for e in fam_evidence
                ) / len(fam_evidence)
            else:
                avg_conf = 1.0
            family_scores.append(base_prior * avg_conf)

        penalty = sum(0.30 for obs in self.observations if not obs.is_valid)
        return max(0.01, sum(family_scores) - penalty)

class SymbolicInferenceEngine:
    @staticmethod
    def compute_decision_hash(field_name: str, evidence_list: List[Evidence], selected_value: str) -> str:
        ev_tokens = sorted([f"{e.id}:{e.value}" for e in evidence_list if e.id is not None])
        raw_str = f"{field_name}|{'|'.join(ev_tokens)}|{selected_value}|{RESOLVER_VERSION}|{settings.get_config_hash()}"
        return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

    @staticmethod
    def resolve_field(
        entity_id: str,
        field_name: str,
        current_value: Optional[str],
        evidence_list: List[Evidence],
        run_id: str = ""
    ) -> Decision:
        conn = get_connection()
        cursor = conn.cursor()

        if current_value is None:
            current_value = get_current_field_value(cursor, entity_id, field_name)

        cursor.execute("SELECT lock_state FROM meta_locks WHERE entity_id = %s AND field_name = %s", (entity_id, field_name))
        lock_row = cursor.fetchone()
        lock_state = lock_row[0] if lock_row else "AUTOMATIC"

        if lock_state == "MANUAL":
            dec_hash = SymbolicInferenceEngine.compute_decision_hash(field_name, evidence_list, str(current_value or ""))
            return Decision(
                run_id=run_id, entity_id=entity_id, field_name=field_name,
                old_value=current_value, selected_value=current_value,
                reason="Field locked as MANUAL by user", decision_hash=dec_hash
            )

        if lock_state == "PROTECTED":
            has_user_evidence = any(e.source_id == "SRC_USER" for e in evidence_list)
            if not has_user_evidence:
                dec_hash = SymbolicInferenceEngine.compute_decision_hash(field_name, evidence_list, str(current_value or ""))
                return Decision(
                    run_id=run_id, entity_id=entity_id, field_name=field_name,
                    old_value=current_value, selected_value=current_value,
                    reason="Field is PROTECTED and no user evidence present", decision_hash=dec_hash
                )

        if not evidence_list:
            return Decision(
                run_id=run_id, entity_id=entity_id, field_name=field_name,
                old_value=current_value, selected_value=current_value,
                reason="No evidence facts available", decision_hash="NO_EVIDENCE"
            )

        candidate_map: Dict[str, CandidateNode] = {}
        for ev in evidence_list:
            if not ev.value or not str(ev.value).strip():
                continue
            raw_val = str(ev.value).strip()
            if raw_val.upper() in ("NONE", "NULL"):
                continue

            norm_val = NormalizerRegistry.normalize(field_name, raw_val)
            if not norm_val:
                continue

            # Force genre/subgenre candidate nodes to always use their beautifully formatted norm_val
            if norm_val not in candidate_map:
                candidate_map[norm_val] = CandidateNode(
                    value=norm_val if field_name in ("musicbrainz_recording_id", "musicbrainz_release_id", "musicbrainz_artist_id", "genre", "subgenre") else raw_val, 
                    normalized_value=norm_val
                )

            cand = candidate_map[norm_val]
            cand.supporting_evidence.append(ev)
            family = SOURCE_FAMILY_MAP.get(ev.source_id, EvidenceFamily.COMMUNITY_TAGS)
            cand.supported_families.add(family)

        if not candidate_map:
            dec_hash = SymbolicInferenceEngine.compute_decision_hash(field_name, evidence_list, "")
            return Decision(
                run_id=run_id, entity_id=entity_id, field_name=field_name,
                old_value=current_value, selected_value=current_value,
                reason="All evidence values were invalid or placeholders", decision_hash=dec_hash
            )

        rule = RuleRegistry.get_rule(field_name)
        ctx = {"entity_id": entity_id, "reference_artist": current_value if field_name == "artist" else None}
        for cand in candidate_map.values():
            if rule:
                cand.observations = rule.evaluate(cand, ctx)

        candidates = [c for c in candidate_map.values() if not c.is_invalidated]
        if not candidates:
            candidates = list(candidate_map.values())

        candidates.sort(key=lambda c: (-c.get_trust_weight(field_name), -len(c.supported_families)))

        selected = candidates[0]
        rejected = [c for c in candidate_map.values() if c.normalized_value != selected.normalized_value]
        dec_hash = SymbolicInferenceEngine.compute_decision_hash(field_name, evidence_list, selected.value)

        fams_str = ", ".join([f.value for f in selected.supported_families])
        reason_str = f"Selected '{selected.value}' (Trust weight: {selected.get_trust_weight(field_name):.2f}, Families: [{fams_str}])"

        rejected_json = json.dumps([{
            "value": r.value,
            "families": [f.value for f in r.supported_families],
            "invalidated": r.is_invalidated,
            "observations": [{"rule": obs.rule_name, "valid": obs.is_valid, "detail": obs.detail} for obs in r.observations]
        } for r in rejected])

        return Decision(
            run_id=run_id, entity_id=entity_id, field_name=field_name,
            old_value=current_value, selected_value=selected.value,
            reason=reason_str, rejected_values_json=rejected_json, decision_hash=dec_hash
        )

def purge_invalid_isrc_evidence(recording_id: Optional[str] = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()

    if recording_id:
        cursor.execute("""
            SELECT id, entity_id, value FROM meta_evidence
            WHERE field_name = 'isrc' AND entity_id = %s
        """, (recording_id,))
    else:
        cursor.execute("""
            SELECT id, entity_id, value FROM meta_evidence
            WHERE field_name = 'isrc'
        """)

    rows = cursor.fetchall()
    invalid_evidence_ids = []
    affected_entity_ids = set()

    for ev_id, ent_id, val in rows:
        norm_isrc = ISRCNormalizer.normalize(str(val) if val else "")
        if not norm_isrc:
            invalid_evidence_ids.append(ev_id)
            affected_entity_ids.add(ent_id)

    if not invalid_evidence_ids:
        return 0

    run_id = generate_ulid()

    with db_transaction() as tx:
        tx.executemany("DELETE FROM meta_evidence WHERE id = %s", [(eid,) for eid in invalid_evidence_ids])
        purged_count = len(invalid_evidence_ids)

        for ent_id in affected_entity_ids:
            tx.execute("""
                SELECT id, entity_id, field_name, value, evidence_class, source_id, run_id, 
                       payload_hash, confidence, origin_type, observed_at, raw_value, 
                       token_index, token_delimiter, positional_weight
                FROM meta_evidence WHERE entity_id = %s AND field_name = 'isrc'
            """, (ent_id,))
            ev_rows = [Evidence(
                id=r[0], entity_id=r[1], field_name=r[2], value=r[3], evidence_class=r[4],
                source_id=r[5], run_id=r[6], payload_hash=r[7], confidence=r[8], origin_type=r[9],
                observed_at=str(r[10]), raw_value=r[11], token_index=r[12], token_delimiter=r[13],
                positional_weight=r[14]
            ) for r in tx.fetchall()]

            curr_isrc = get_current_field_value(tx, ent_id, "isrc")
            decision = SymbolicInferenceEngine.resolve_field(ent_id, "isrc", curr_isrc, ev_rows, run_id)
            
            new_isrc = decision.selected_value if decision.decision_hash != "NO_EVIDENCE" else None
            tx.execute("UPDATE core_recordings SET isrc = %s WHERE id = %s", (new_isrc, ent_id))

            tx.execute("""
                UPDATE meta_issues SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP
                WHERE entity_id = %s AND issue_code = 'INVALID_ISRC_SYNTAX' AND status = 'OPEN'
            """, (ent_id,))

            if new_isrc:
                tx.execute("""
                    UPDATE meta_issues SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP
                    WHERE entity_id = %s AND issue_code = 'MISSING_ISRC' AND status = 'OPEN'
                """, (ent_id,))
            else:
                tx.execute("""
                    INSERT INTO meta_issues (entity_id, issue_code, severity, status)
                    VALUES (%s, 'MISSING_ISRC', 'WARNING', 'OPEN')
                    ON CONFLICT DO NOTHING
                """, (ent_id,))

            ResolutionPersistenceAdapter.recalculate_quality_score(tx, ent_id)

    return purged_count

class ResolutionPersistenceAdapter:
    @staticmethod
    def recalculate_quality_score(cursor: Any, recording_id: str) -> float:
        cursor.execute("""
            SELECT title, artist_id, album_id, release_date, isrc, musicbrainz_recording_id
            FROM core_recordings WHERE id = %s
        """, (recording_id,))
        row = cursor.fetchone()
        if not row:
            return 0.0

        title, art_id, alb_id, rel_date, isrc_code, mbid_code = row
        has_clean_title = bool(title and title.strip() and not str(title).lower().endswith(('.mp3', '.flac', '.wav', '.m4a', '.ogg')))
        
        parts = [
            0.20 if has_clean_title else (0.05 if title else 0.0),
            0.25 if art_id else 0.0,
            0.20 if alb_id else 0.0,
            0.15 if rel_date else 0.0,
            0.10 if (isrc_code and isrc_code != "NONE") else 0.0,
            0.10 if mbid_code else 0.0
        ]
        q_score = round(min(1.0, sum(parts)), 2)

        if q_score >= 1.0 and isrc_code and mbid_code:
            new_state = "COMPLETE"
        elif q_score >= 0.80:
            new_state = "ENRICHED"
        elif mbid_code or isrc_code:
            new_state = "IDENTIFIED"
        else:
            new_state = "PARSED"

        cursor.execute("UPDATE core_recordings SET state = %s WHERE id = %s", (new_state, recording_id))

        cursor.execute("""
            INSERT INTO meta_validation (recording_id, quality_score, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(recording_id) DO UPDATE SET
                quality_score = EXCLUDED.quality_score,
                updated_at = CURRENT_TIMESTAMP
        """, (recording_id, q_score))

        return q_score

    @staticmethod
    def apply_decision(decision: Decision, target_table: str = "core_recordings") -> bool:
        if decision.decision_hash == "NO_EVIDENCE" or not decision.selected_value:
            return False

        with db_transaction() as cursor:
            if target_table == "core_recordings":
                if decision.field_name in ALLOWED_RECORDING_COLUMNS or decision.field_name in ("title", "release_date", "original_release_date", "isrc", "musicbrainz_recording_id"):
                    
                    if decision.field_name in ("isrc", "musicbrainz_recording_id") and decision.selected_value:
                        col_check = "musicbrainz_recording_id" if decision.field_name == "musicbrainz_recording_id" else "isrc"
                        cursor.execute(f"SELECT id FROM core_recordings WHERE {col_check} = %s AND id != %s", (decision.selected_value, decision.entity_id))
                        conflict = cursor.fetchone()
                        if conflict:
                            conflicting_id = conflict[0]
                            if decision.field_name == "musicbrainz_recording_id":
                                from db.core import merge_duplicate_recordings_by_mbid
                                merged = merge_duplicate_recordings_by_mbid(target_recording_id=decision.entity_id, duplicate_recording_id=conflicting_id)
                                if merged:
                                    cursor.execute(f"SELECT id FROM core_recordings WHERE musicbrainz_recording_id = %s AND id != %s", (decision.selected_value, decision.entity_id))
                                    conflict = cursor.fetchone()

                            if conflict:
                                cursor.execute("""
                                    INSERT INTO meta_issues (entity_id, issue_code, severity, status)
                                    VALUES (%s, 'DUPLICATE_IDENTIFIER_COLLISION', 'WARNING', 'OPEN')
                                    ON CONFLICT DO NOTHING
                                """, (decision.entity_id,))
                                return False

                    if decision.field_name in ALLOWED_RECORDING_COLUMNS:
                        cursor.execute(f"UPDATE core_recordings SET {decision.field_name} = %s WHERE id = %s", (decision.selected_value, decision.entity_id))

                    if decision.field_name == "musicbrainz_recording_id":
                        cursor.execute("UPDATE meta_issues SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP WHERE entity_id = %s AND issue_code = 'MISSING_MBID' AND status = 'OPEN'", (decision.entity_id,))
                    elif decision.field_name == "isrc":
                        cursor.execute("UPDATE meta_issues SET status = 'RESOLVED', resolved_at = CURRENT_TIMESTAMP WHERE entity_id = %s AND issue_code = 'MISSING_ISRC' AND status = 'OPEN'", (decision.entity_id,))

                elif decision.field_name == "artist":
                    repo_art = ArtistRepo()
                    art_id = repo_art.get_or_create(decision.selected_value)
                    cursor.execute("UPDATE core_recordings SET artist_id = %s WHERE id = %s", (art_id, decision.entity_id))

                elif decision.field_name == "album":
                    cursor.execute("SELECT artist_id FROM core_recordings WHERE id = %s", (decision.entity_id,))
                    art_row = cursor.fetchone()
                    art_id = art_row[0] if art_row else None
                    repo_alb = AlbumRepo()
                    alb_id = repo_alb.get_or_create(decision.selected_value, artist_id=art_id)
                    cursor.execute("UPDATE core_recordings SET album_id = %s, release_id = %s WHERE id = %s", (alb_id, alb_id, decision.entity_id))

                ResolutionPersistenceAdapter.recalculate_quality_score(cursor, decision.entity_id)

            cursor.execute("""
                INSERT INTO meta_decisions (run_id, entity_id, field_name, old_value, selected_value, reason, rejected_values_json, decision_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                decision.run_id, decision.entity_id, decision.field_name,
                decision.old_value, decision.selected_value, decision.reason,
                decision.rejected_values_json, decision.decision_hash
            ))

        return True