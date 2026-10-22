-- 006_opportunity_profiles: Claude-written deal profiles built only from cited insights. One row per generation.

create table opportunity_profiles (
    id               uuid primary key default gen_random_uuid(),
    workspace_id     uuid not null references workspaces(id) on delete cascade,
    opportunity_id   uuid not null references opportunities(id) on delete cascade,
    profile          jsonb not null,
    insight_ids      uuid[] not null,      -- the insights the profile was built from
    profiler_version text not null,
    model            text not null,
    generated_at     timestamptz not null default now()
);

create index opportunity_profiles_latest_idx on opportunity_profiles (opportunity_id, generated_at desc);
alter table opportunity_profiles enable row level security;
