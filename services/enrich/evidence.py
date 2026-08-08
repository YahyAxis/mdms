"""
Evidence Buffer Infrastructure
Manages in-memory buffering, session deduplication, and batch insertion of evidence rows into meta_evidence.
"""

from typing import List, Tuple, Optional, Any, Dict, Set
from db import db_transaction

class EvidenceBuffer:
    def __init__(self, recording_id: str, run_id: str) -> None:
        self.recording_id = recording_id
        self.run_id = run_id
        self._rows: List[Tuple[str, str, str, str, str, str, Optional[str], float, str, Optional[str], int, str, float]] = []
        self._seen_keys: Set[Tuple[str, str, str, str]] = set()

    @property
    def count(self) -> int:
        return len(self._rows)

    def add(
        self,
        field_name: str,
        value: Optional[str],
        evidence_class: str,
        source_id: str,
        payload_hash: Optional[str] = None,
        confidence: float = 1.0,
        token_index: int = 0,
        positional_weight: float = 1.0
    ) -> None:
        if not value:
            return
        val_clean = str(value).strip()
        if not val_clean or val_clean.upper() in ("NONE", "NULL", "UNKNOWN"):
            return

        dedup_key = (self.recording_id, field_name, source_id, val_clean.casefold())
        if dedup_key in self._seen_keys:
            return
        self._seen_keys.add(dedup_key)

        self._rows.append((
            self.recording_id, field_name, val_clean, evidence_class,
            source_id, self.run_id, payload_hash, max(0.0, min(1.0, float(confidence))),
            "RESOLVER", val_clean, token_index, ";", max(0.0, min(1.0, float(positional_weight)))
        ))

    def add_many(self, evidence_items: List[Dict[str, Any]]) -> None:
        for item in evidence_items:
            self.add(
                field_name=item.get("field_name", ""),
                value=item.get("value"),
                evidence_class=item.get("evidence_class", "LOCAL"),
                source_id=item.get("source_id", "SRC_UNKNOWN"),
                payload_hash=item.get("payload_hash"),
                confidence=float(item.get("confidence", 1.0)),
                token_index=int(item.get("token_index", 0)),
                positional_weight=float(item.get("positional_weight", 1.0))
            )

    def commit(self, cursor: Optional[Any] = None) -> int:
        if not self._rows:
            return 0

        inserted_count = len(self._rows)
        sql = """
            INSERT INTO meta_evidence (
                entity_id, field_name, value, evidence_class, source_id, run_id, 
                payload_hash, confidence, origin_type, raw_value, token_index, 
                token_delimiter, positional_weight
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """

        if cursor:
            cursor.executemany(sql, self._rows)
        else:
            with db_transaction() as tx:
                tx.executemany(sql, self._rows)

        self.clear()
        return inserted_count

    def clear(self) -> None:
        self._rows.clear()
        self._seen_keys.clear()