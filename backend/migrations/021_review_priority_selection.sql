-- Priority selection is a presentation decision, not a decision on omitted evidence.
alter table review_proposals add column selection_snapshot jsonb
    check (selection_snapshot is null or jsonb_typeof(selection_snapshot) = 'object');

-- Keep one reusable result, including an empty result, per company. The fingerprint
-- includes the policy, evidence, current Sheet context and review date.
create table review_selection_cache (
    workspace_id uuid not null references workspaces(id) on delete cascade,
    company_id uuid not null references companies(id) on delete cascade,
    input_fingerprint text not null,
    snapshot jsonb not null check (jsonb_typeof(snapshot) = 'object'),
    updated_at timestamptz not null default now(),
    primary key (workspace_id, company_id)
);

-- A source can support both a displayed action and a deferred action. Only the
-- displayed candidates are consumed, so deciding one never retires the other.
create table review_candidate_receipts (
    workspace_id uuid not null references workspaces(id) on delete cascade,
    company_id uuid not null references companies(id) on delete cascade,
    candidate_key text not null,
    proposal_id uuid not null references review_proposals(id) on delete cascade,
    decided_at timestamptz not null default now(),
    primary key (workspace_id, company_id, candidate_key)
);
create index review_candidate_receipts_proposal on review_candidate_receipts (proposal_id);
alter table review_selection_cache enable row level security;
alter table review_candidate_receipts enable row level security;
