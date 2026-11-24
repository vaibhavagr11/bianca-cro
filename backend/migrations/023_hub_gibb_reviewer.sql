-- Gibb is a distinct Hub reviewer, not an additional pipeline sales owner.
-- Preserve migration 022 and every existing Google identity/session binding.
alter table hub_google_identities
    drop constraint hub_google_identities_actor_check;
alter table hub_google_identities
    add constraint hub_google_identities_actor_check
    check (actor in ('RG', 'NC', 'NDL', 'MG', 'GA'));
