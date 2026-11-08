-- Scope is assigned by extraction. Older rows remain unknown until their evidence is reviewed.
alter table insights
    add column action_scope text check (action_scope in ('deal_progression', 'delivery', 'out_of_scope')),
    add column origin_record_id uuid references source_records(id) on delete set null;
