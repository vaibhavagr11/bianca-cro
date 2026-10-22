-- 003_source_meta: per-source extras (e.g. notes doc id, whether the meeting date was inferred).
alter table source_records add column meta jsonb not null default '{}';
