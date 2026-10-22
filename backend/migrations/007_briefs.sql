-- 007_briefs: every morning brief that was built, for history and "changed since last brief".

create table briefs (
    id           uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references workspaces(id) on delete cascade,
    for_date     date not null,
    subject      text not null,
    html         text not null,
    payload      jsonb not null,          -- the tables' rows, with evidence links
    recipients   text[] not null,
    created_at   timestamptz not null default now()
);

create index briefs_recent_idx on briefs (workspace_id, created_at desc);
alter table briefs enable row level security;
