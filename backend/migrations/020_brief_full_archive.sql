-- Full Daily Brief snapshots for the authenticated Hub reader. Older archives stay limited.
alter table briefs
    add column full_html text,
    add column full_payload jsonb;
