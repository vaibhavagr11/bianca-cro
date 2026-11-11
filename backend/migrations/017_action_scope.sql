-- Scope is assigned by extraction. Older rows stay null ("not yet reviewed") and are shown until re-extracted.
-- origin_record_id comes from 015_insight_origins.
alter table insights
    add column action_scope text check (action_scope in ('deal_progression', 'delivery', 'out_of_scope'));
