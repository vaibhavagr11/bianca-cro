-- 011_brief_actions: "Mark as complete" from the brief. Each click is kept (who, what, when) and doubles as a
-- person-graded label for the evals. An insight closed here also gets insights.status = 'done'; email threads and
-- AI suggestions have no status of their own, so the brief reads their latest action from this table.

create table brief_actions (
    id           uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references workspaces(id) on delete cascade,
    kind         text not null check (kind in ('insight', 'thread', 'rec')),
    ref          text not null,              -- insight id, Gmail thread id, or suggestion ref (deal id:text hash)
    action       text not null check (action in ('done', 'undone')),
    actor        text,                       -- team owner code the clicker picked on the confirm page
    created_at   timestamptz not null default now()
);

create index brief_actions_ref on brief_actions (workspace_id, kind, ref, created_at desc);
alter table brief_actions enable row level security;
