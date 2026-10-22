-- 005_insights: structured commercial intelligence extracted from emails, meetings and notes.
-- Every insight carries a verbatim quote and the record it came from.

create table insights (
    id                uuid primary key default gen_random_uuid(),
    workspace_id      uuid not null references workspaces(id) on delete cascade,
    company_id        uuid not null references companies(id) on delete cascade,
    opportunity_id    uuid references opportunities(id) on delete set null,
    source            text not null,             -- gmail / calendar / notes
    unit_id           text not null,             -- thread_id of the extracted unit (thread, meeting or note)
    source_record_ids uuid[] not null,           -- records in that unit
    type              text not null check (type in
                      ('need', 'buying_signal', 'objection', 'commitment', 'blocker', 'next_step')),
    text              text not null,             -- one-line statement in English
    quote             text not null,             -- verbatim from the source
    owner_side        text check (owner_side in ('bianca', 'customer', 'unclear')),
    owner_name        text,
    due_date          date,
    occurred_at       timestamptz,               -- when the source item happened
    status            text not null default 'open'
                      check (status in ('open', 'possibly_done', 'done', 'dismissed')),
    confidence        text not null check (confidence in ('high', 'medium', 'low')),
    extractor_version text not null,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

create index insights_company_idx on insights (company_id, type, occurred_at desc);
create index insights_unit_idx on insights (workspace_id, source, unit_id);
create trigger insights_updated_at before update on insights for each row execute function set_updated_at();
alter table insights enable row level security;

-- Which extractor version last processed each record (null = never).
alter table source_records add column extracted_version text;
