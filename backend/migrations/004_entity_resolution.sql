-- 004_entity_resolution: people and email domains tied to companies, and how each record got its company.

-- External people seen in emails and meetings.
create table contacts (
    id            uuid primary key default gen_random_uuid(),
    workspace_id  uuid not null references workspaces(id) on delete cascade,
    company_id    uuid references companies(id) on delete set null,
    email         text not null,
    name          text,
    record_count  integer not null default 0,
    first_seen_at timestamptz,
    last_seen_at  timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (workspace_id, email)
);

-- Email domains that identify a company (e.g. kof.com.mx -> FEMSA Arg). Free-mail domains never go here.
create table company_domains (
    id             uuid primary key default gen_random_uuid(),
    workspace_id   uuid not null references workspaces(id) on delete cascade,
    company_id     uuid not null references companies(id) on delete cascade,
    domain         text not null,
    origin         text not null default 'learned' check (origin in ('learned', 'manual')),
    trusted        boolean not null default false,  -- used for rule-based linking
    evidence_count integer not null default 0,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (workspace_id, domain)
);

create index contacts_company_idx on contacts (company_id);
create index company_domains_company_idx on company_domains (company_id);
create trigger contacts_updated_at        before update on contacts        for each row execute function set_updated_at();
create trigger company_domains_updated_at before update on company_domains for each row execute function set_updated_at();
alter table contacts        enable row level security;
alter table company_domains enable row level security;

-- How the record's company_id was decided: 'classifier', 'rules', 'both' (agree), or 'conflict' (disagree; review).
alter table source_records add column link_method text;
alter table source_records add column rules_company_id uuid references companies(id) on delete set null;
