-- Pending tracks that still have the acoustid_searched flag
SELECT COUNT(*) FROM core_recordings r
LEFT JOIN core_assets a ON r.id = a.recording_id
LEFT JOIN core_asset_locations loc ON a.asset_id = loc.asset_id
LEFT JOIN meta_validation v ON r.id = v.recording_id
WHERE (v.quality_score IS NULL OR v.quality_score < 1.0 
       OR r.musicbrainz_recording_id IS NULL OR r.isrc IS NULL)
  AND loc.is_available = 1
  AND EXISTS (
    SELECT 1 FROM meta_evidence e 
    WHERE e.entity_id = r.id AND e.field_name = 'acoustid_searched'
  );

-- Pending tracks that already have *any* MBID in evidence
SELECT COUNT(*) FROM core_recordings r
LEFT JOIN core_assets a ON r.id = a.recording_id
LEFT JOIN core_asset_locations loc ON a.asset_id = loc.asset_id
LEFT JOIN meta_validation v ON r.id = v.recording_id
WHERE (v.quality_score IS NULL OR v.quality_score < 1.0 
       OR r.musicbrainz_recording_id IS NULL OR r.isrc IS NULL)
  AND loc.is_available = 1
  AND EXISTS (
    SELECT 1 FROM meta_evidence e 
    WHERE e.entity_id = r.id AND e.field_name = 'musicbrainz_recording_id'
  );