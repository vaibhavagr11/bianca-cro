-- Keep the first logged time (created_at) and original record across re-extraction.
alter table insights
    add column origin_record_id uuid references source_records(id) on delete set null,
    add column origin_occurred_at timestamptz,
    add column last_extracted_at timestamptz;

-- Older extraction rows point to the whole thread. Recover an origin only when exactly one
-- source contains the quote. Timestamps inferred for undated notes are not evidence cutoffs.
with matched as (
    select i.id as insight_id, r.id as record_id, r.occurred_at,
           coalesce(r.meta->>'date_inferred', 'false') = 'true' as inferred,
           count(*) over (partition by i.id) as match_count
    from insights i
    join source_records r on r.id = any(i.source_record_ids) and r.workspace_id = i.workspace_id
    cross join lateral (select lower(btrim(regexp_replace(i.quote, '[[:space:]]+', ' ', 'g'))) as quote) q
    where q.quote <> '' and (
        strpos(lower(regexp_replace(coalesce(r.subject, ''), '[[:space:]]+', ' ', 'g')), q.quote) > 0
        or strpos(lower(regexp_replace(coalesce(r.body, ''), '[[:space:]]+', ' ', 'g')), q.quote) > 0
    )
)
update insights i
set origin_record_id = m.record_id,
    origin_occurred_at = case when not m.inferred then m.occurred_at end,
    occurred_at = case when not m.inferred then coalesce(m.occurred_at, i.occurred_at) else i.occurred_at end
from matched m where m.insight_id = i.id and m.match_count = 1;

update insights set last_extracted_at = created_at;
alter table insights
    alter column last_extracted_at set default now(),
    alter column last_extracted_at set not null;
