-- Separate action/blocker suggestions from customer context. Legacy proposals are
-- rebuilt from typed insights; never infer routing from their flattened wording.
alter table opportunities add column client_context text;
alter table review_proposals add column snapshot_client_context text not null default '';
alter table review_proposals add column next_step_bullets jsonb not null default '[]'::jsonb
    check (jsonb_typeof(next_step_bullets) = 'array');
alter table review_proposals add column client_context_bullets jsonb not null default '[]'::jsonb
    check (jsonb_typeof(client_context_bullets) = 'array');
alter table review_proposals add column routing_version integer not null default 0 check (routing_version >= 0);
alter table review_decisions add column next_step_bullets jsonb not null default '[]'::jsonb
    check (jsonb_typeof(next_step_bullets) = 'array');
alter table review_decisions add column client_context_bullets jsonb not null default '[]'::jsonb
    check (jsonb_typeof(client_context_bullets) = 'array');
alter table review_decisions add column routing_version integer not null default 0 check (routing_version >= 0);
-- Bind every new durable write intent to the exact configured spreadsheet/tab.
-- Null legacy targets must be reconciled rather than silently retargeted.
alter table review_decisions add column sheet_target jsonb
    check (sheet_target is null or jsonb_typeof(sheet_target) = 'object');
