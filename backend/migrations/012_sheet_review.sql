-- Founder review of proposed Google Sheet updates. Independent of daily-brief history.
create table review_proposals (
    id                     uuid primary key default gen_random_uuid(),
    workspace_id           uuid not null references workspaces(id) on delete cascade,
    company_id             uuid not null references companies(id) on delete cascade,
    opportunity_id         uuid not null references opportunities(id) on delete cascade,
    company_name           text not null,
    owner_code             text,
    stage                  text,
    country                text,
    bullets                jsonb not null check (jsonb_typeof(bullets) = 'array'),
    sources                jsonb not null check (jsonb_typeof(sources) = 'array'),
    source_versions        jsonb not null check (jsonb_typeof(source_versions) = 'array'),
    snapshot_last_progress text not null default '',
    snapshot_next_step     text not null default '',
    revision               integer not null default 1 check (revision > 0),
    status                 text not null default 'pending'
                           check (status in ('pending', 'applying', 'approved', 'ignored', 'superseded')),
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    decided_at             timestamptz,
    decided_by             text
);
create unique index review_proposals_one_active_company
    on review_proposals (workspace_id, company_id) where status in ('pending', 'applying');
create index review_proposals_workspace_status on review_proposals (workspace_id, status, updated_at desc);
create trigger review_proposals_updated_at before update on review_proposals
    for each row execute function set_updated_at();

-- A durable, fixed before/after pair is committed BEFORE the external write. A retry must reuse it.
create table review_decisions (
    id              uuid primary key default gen_random_uuid(),
    workspace_id    uuid not null references workspaces(id) on delete cascade,
    proposal_id     uuid not null references review_proposals(id) on delete cascade,
    actor           text not null,
    action          text not null check (action in ('approve', 'ignore')),
    revision        integer not null,
    idempotency_key text not null,
    request_hash    text not null,
    bullets         jsonb not null check (jsonb_typeof(bullets) = 'array'),
    before_cells    jsonb not null,
    after_cells     jsonb not null,
    edited          boolean not null default false,
    mirror_refreshed boolean,
    write_attempts  integer not null default 0 check (write_attempts >= 0),
    status          text not null check (status in ('applying', 'succeeded', 'conflict', 'failed')),
    error           text,
    created_at      timestamptz not null default now(),
    completed_at    timestamptz,
    unique (workspace_id, idempotency_key)
);
create unique index review_decisions_one_applying on review_decisions (proposal_id) where status = 'applying';
create index review_decisions_workspace_recent on review_decisions (workspace_id, created_at desc);

-- Content identities survive insight re-extraction (which replaces insight UUIDs).
create table review_source_receipts (
    workspace_id uuid not null references workspaces(id) on delete cascade,
    company_id   uuid not null references companies(id) on delete cascade,
    source       text not null,
    external_id  text not null,
    version      text not null,
    proposal_id  uuid not null references review_proposals(id) on delete cascade,
    decided_at   timestamptz not null default now(),
    primary key (workspace_id, company_id, source, external_id, version)
);
alter table review_proposals enable row level security;
alter table review_decisions enable row level security;
alter table review_source_receipts enable row level security;
