-- Browser sign-in is separate from both Google data access and MCP credentials.
-- Identity bindings are deliberately never overwritten on a subsequent login.
create table hub_google_identities (
    workspace_id uuid not null references workspaces(id) on delete cascade,
    google_sub text not null check (length(google_sub) between 1 and 255),
    email text not null check (email = lower(email)),
    actor text not null check (actor in ('RG', 'NC', 'NDL', 'MG')),
    created_at timestamptz not null default now(),
    last_login_at timestamptz not null default now(),
    disabled_at timestamptz,
    primary key (workspace_id, google_sub),
    unique (workspace_id, email),
    unique (workspace_id, actor)
);

create table hub_sessions (
    token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
    workspace_id uuid not null,
    google_sub text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    foreign key (workspace_id, google_sub)
        references hub_google_identities(workspace_id, google_sub) on delete cascade,
    check (expires_at > created_at)
);
create index hub_sessions_identity on hub_sessions (workspace_id, google_sub);
create index hub_sessions_expiry on hub_sessions (expires_at);

create table hub_oauth_states (
    state_hash text primary key check (state_hash ~ '^[0-9a-f]{64}$'),
    browser_hash text not null check (browser_hash ~ '^[0-9a-f]{64}$'),
    workspace_id uuid not null references workspaces(id) on delete cascade,
    return_to text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    check (expires_at > created_at)
);
create index hub_oauth_states_expiry on hub_oauth_states (expires_at);

-- Only the backend's existing database role accesses these tables. No Data API
-- policies or public grants: neither anonymous nor signed-in API users can read
-- session hashes, OAuth state, or identity bindings.
alter table hub_google_identities enable row level security;
alter table hub_sessions enable row level security;
alter table hub_oauth_states enable row level security;
revoke all on hub_google_identities, hub_sessions, hub_oauth_states from public, anon, authenticated;
