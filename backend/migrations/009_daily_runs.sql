-- 009_daily_runs: the morning processing run (classify -> resolve -> extract -> completion check -> profiles),
-- advanced one small step per call so each request stays short. One row per workspace per day; a finished run
-- starts over when a later sync brings new items.

create table daily_runs (
    id           uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references workspaces(id) on delete cascade,
    for_date     date not null,
    stage        text not null default 'classify'
                 check (stage in ('classify', 'resolve', 'extract', 'reconcile', 'profile', 'done', 'timed_out')),
    queue        text[] not null default '{}',   -- opportunity ids still to check / re-profile in this stage
    touched      text[] not null default '{}',   -- opportunity ids that got new insights in this run
    steps        integer not null default 0,
    log          jsonb not null default '[]',    -- one short entry per step: counts only, never content
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    unique (workspace_id, for_date)
);

alter table daily_runs enable row level security;
