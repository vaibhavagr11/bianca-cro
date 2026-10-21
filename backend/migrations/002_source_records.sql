-- 002_source_records: raw items from Gmail, Calendar and the meeting-notes doc, after filtering and redaction.
-- One row per source item. The AI classifier and extractors read from here.

create table source_records (
    id             uuid primary key default gen_random_uuid(),
    workspace_id   uuid not null references workspaces(id) on delete cascade,
    source         text not null check (source in ('gmail', 'calendar', 'notes')),
    external_id    text not null,          -- Gmail message id, Calendar event id, notes section key
    thread_id      text,                   -- Gmail thread id; groups messages for the classifier
    occurred_at    timestamptz,
    subject        text,
    participants   jsonb not null default '{}',  -- {"from": [{address, name}], "to": [...], "cc": [...]}
    body           text,                   -- cleaned: quoted replies removed, secrets redacted
    content_hash   text not null,          -- unchanged hash = skip on re-send
    status         text not null default 'pending'
                   check (status in ('pending', 'classified', 'failed')),
    classification jsonb,                  -- AI classifier output (sales-related, category, company, quote...)
    company_id     uuid references companies(id) on delete set null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (workspace_id, source, external_id)
);

create index source_records_thread_idx on source_records (workspace_id, source, thread_id);
create index source_records_pending_idx on source_records (workspace_id, status) where status = 'pending';
create index source_records_company_idx on source_records (company_id, occurred_at desc);

create trigger source_records_updated_at before update on source_records for each row execute function set_updated_at();
alter table source_records enable row level security;

-- Filtered-out items are counted, never stored.
alter table sync_runs add column rows_skipped integer;
