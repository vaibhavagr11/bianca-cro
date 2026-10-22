-- 008_completion: evidence that an open commitment / next step was completed (set by the completion check).
-- {"state": "done"|"possibly_done"|"event_passed", "record_id", "quote", "reason", "checked_at", "checker_version"}
alter table insights add column completion jsonb;
