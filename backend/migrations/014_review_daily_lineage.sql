-- One-time initial 14-day import, resumable daily cycles, and exact source lineage.
create table review_queue_state (
    workspace_id uuid primary key references workspaces(id) on delete cascade,
    initialized_at timestamptz not null,
    bootstrap_cutoff timestamptz not null,
    bootstrap_completed boolean not null default false,
    cycle_for_date date not null,
    cycle_started_at timestamptz not null,
    cycle_completed_at timestamptz,
    last_completed_on date,
    check (bootstrap_cutoff <= initialized_at)
);
alter table review_queue_state enable row level security;

-- Excluding an old initial version is not an approval/dismissal. A later changed
-- version or a newly arrived historical item can be reviewed after bootstrap.
create table review_bootstrap_exclusions (
    workspace_id uuid not null references workspaces(id) on delete cascade,
    source text not null check (source in ('gmail', 'notes', 'calendar')),
    external_id text not null,
    version text not null,
    reason text not null check (reason in ('undated', 'outside_initial_window')),
    primary key (workspace_id, source, external_id, version)
);
alter table review_bootstrap_exclusions enable row level security;

alter table review_proposals add column observed_source_versions jsonb not null default '[]'::jsonb
    check (jsonb_typeof(observed_source_versions) = 'array');
alter table review_proposals add column bullet_lineage jsonb not null default '{"nextStep":[],"clientContext":[]}'::jsonb
    check (jsonb_typeof(bullet_lineage) = 'object');
alter table review_proposals add column lineage_version integer not null default 0 check (lineage_version >= 0);
alter table review_decisions add column bullet_lineage jsonb not null default '{"nextStep":[],"clientContext":[]}'::jsonb
    check (jsonb_typeof(bullet_lineage) = 'object');
alter table review_decisions add column lineage_version integer not null default 0 check (lineage_version >= 0);
