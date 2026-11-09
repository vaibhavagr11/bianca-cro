-- A receipt attests a complete feed for one n8n execution, including a successful empty fetch.
alter table sync_runs
    add column sync_batch_id uuid,
    add column feed_key text,
    add column coverage_complete boolean not null default false;

create index sync_runs_verification_idx on sync_runs (workspace_id, sync_batch_id, feed_key, started_at desc)
    where sync_batch_id is not null;

alter table daily_runs add column sync_batch_id uuid;

-- One resumable verification run per selected build. Draft runs contain verdicts and IDs, never email excerpts.
create table brief_verification_runs (
    id uuid primary key,
    workspace_id uuid not null references workspaces(id) on delete cascade,
    sync_batch_id uuid not null,
    version text not null check (version in ('v1', 'v2')),
    dry_run boolean not null,
    as_of timestamptz not null,
    snapshot text not null,
    action_digest text,
    verdicts jsonb not null default '{}',
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    budget_used integer not null default 0,
    response jsonb
);

alter table brief_verification_runs enable row level security;
