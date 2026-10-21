-- 001_init: workspaces, team, companies, and the mirrored sales pipeline.
-- Every table carries workspace_id so customer data stays isolated from day one.

create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- A customer account (e.g. Bianca AI).
create table workspaces (
    id               uuid primary key default gen_random_uuid(),
    name             text not null unique,
    internal_domains text[] not null default '{}',  -- e.g. {bianca-ai.com}; tells "us" from "them"
    timezone         text not null,                 -- IANA name; drives "today" in the morning brief
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

-- The customer's own team; owner_code matches the pipeline's Owner column (NC, RG, NDL).
create table team_members (
    id             uuid primary key default gen_random_uuid(),
    workspace_id   uuid not null references workspaces(id) on delete cascade,
    owner_code     text not null,
    full_name      text not null,
    email          text not null,
    receives_brief boolean not null default false,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (workspace_id, owner_code),
    unique (workspace_id, email)
);

-- One row per external company. Shared by every source (pipeline, Gmail, Calendar, notes).
create table companies (
    id              uuid primary key default gen_random_uuid(),
    workspace_id    uuid not null references workspaces(id) on delete cascade,
    name            text not null,       -- as written in the source
    normalized_name text not null,       -- lowercase, no accents, collapsed spaces; the match key
    country         text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (workspace_id, normalized_name)
);

-- Current state of each deal, mirrored read-only from the pipeline file. One deal per company for now.
create table opportunities (
    id            uuid primary key default gen_random_uuid(),
    workspace_id  uuid not null references workspaces(id) on delete cascade,
    company_id    uuid not null unique references companies(id) on delete cascade,
    owner_code    text,                  -- not a foreign key: the sheet may hold TBD or unknown codes
    stage         text,                  -- raw, e.g. "2. In Talks"
    stage_rank    smallint,              -- parsed number, e.g. 2; null if unparseable
    primary_buyer text,
    contacts_text text,                  -- raw "Nombre contacto (Rol)" cell; parsed into contacts later
    last_progress text,
    next_step     text,
    row_hash      text not null,         -- hash of the source row; unchanged hash = skip
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    removed_at    timestamptz,           -- set when the row disappears from the sheet; never hard-deleted
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- One row per sync attempt, successful or not. Makes failures visible.
create table sync_runs (
    id             uuid primary key default gen_random_uuid(),
    workspace_id   uuid not null references workspaces(id) on delete cascade,
    source         text not null,        -- e.g. 'pipeline'
    status         text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
    started_at     timestamptz not null default now(),
    finished_at    timestamptz,
    rows_received  integer,
    rows_created   integer,
    rows_updated   integer,
    rows_unchanged integer,
    rows_removed   integer,
    error          text                  -- error type/message only; never row contents
);

-- Field-level history of the pipeline. Feeds "what changed" in the morning brief.
create table opportunity_changes (
    id             bigint generated always as identity primary key,
    workspace_id   uuid not null references workspaces(id) on delete cascade,
    opportunity_id uuid not null references opportunities(id) on delete cascade,
    sync_run_id    uuid not null references sync_runs(id) on delete cascade,
    field          text not null,        -- e.g. 'stage', 'next_step', or 'created' / 'removed'
    old_value      text,
    new_value      text,
    detected_at    timestamptz not null default now()
);

create index opportunities_workspace_idx on opportunities (workspace_id);
create index opportunity_changes_recent_idx on opportunity_changes (workspace_id, detected_at desc);
create index sync_runs_recent_idx on sync_runs (workspace_id, source, started_at desc);

create trigger workspaces_updated_at    before update on workspaces    for each row execute function set_updated_at();
create trigger team_members_updated_at  before update on team_members  for each row execute function set_updated_at();
create trigger companies_updated_at     before update on companies     for each row execute function set_updated_at();
create trigger opportunities_updated_at before update on opportunities for each row execute function set_updated_at();

-- Lock down Supabase's auto-generated API: RLS on, no policies = no access via anon/authenticated keys.
-- Our backend connects as the database owner, which bypasses RLS.
alter table workspaces          enable row level security;
alter table team_members        enable row level security;
alter table companies           enable row level security;
alter table opportunities       enable row level security;
alter table sync_runs           enable row level security;
alter table opportunity_changes enable row level security;
