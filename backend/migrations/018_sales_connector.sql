-- The sales connector connects as a provisioned login that inherits this read role.
-- Keep credentials and login-to-workspace bindings outside the migration.
do $$
declare
    reader pg_catalog.pg_roles%rowtype;
begin
    if not exists (select 1 from pg_catalog.pg_roles where rolname = 'bianca_sales_reader') then
        create role bianca_sales_reader nologin inherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
    end if;

    select * into reader from pg_catalog.pg_roles where rolname = 'bianca_sales_reader';
    if reader.rolcanlogin or reader.rolsuper or reader.rolcreatedb or reader.rolcreaterole
       or reader.rolreplication or reader.rolbypassrls
       or exists (select 1 from pg_catalog.pg_auth_members where member = reader.oid) then
        raise exception 'bianca_sales_reader must be an unprivileged, standalone grant role';
    end if;
end;
$$;

create schema bianca_sales_private;
revoke all on schema bianca_sales_private from public;

create table bianca_sales_private.workspace_bindings (
    login_role_oid oid primary key,
    workspace_id uuid not null references public.workspaces (id) on delete cascade
);
revoke all on bianca_sales_private.workspace_bindings from public;

-- SESSION_USER is the authenticated Postgres login even after SET ROLE. Use its OID so
-- dropping a login and creating another with the same name doesn't carry its access over.
create function bianca_sales_private.bound_workspace_id() returns uuid
    language sql stable security definer set search_path = ''
    as $$
        select binding.workspace_id
        from bianca_sales_private.workspace_bindings binding
        join pg_catalog.pg_roles login_role on login_role.oid = binding.login_role_oid
        where login_role.rolname = session_user
    $$;
revoke all on function bianca_sales_private.bound_workspace_id() from public;
grant execute on function bianca_sales_private.bound_workspace_id() to bianca_sales_reader;

grant usage on schema public to bianca_sales_reader;

grant select (id, internal_domains, timezone) on public.workspaces to bianca_sales_reader;
grant select (id, workspace_id, name, normalized_name, country) on public.companies to bianca_sales_reader;
grant select (id, workspace_id, company_id, owner_code, stage, stage_rank, removed_at, primary_buyer,
              last_progress, next_step) on public.opportunities to bianca_sales_reader;
grant select (workspace_id, company_id, name, email) on public.contacts to bianca_sales_reader;
grant select (workspace_id, domain, trusted, company_id) on public.company_domains to bianca_sales_reader;
grant select (id, workspace_id, source, external_id, thread_id, occurred_at, subject, participants,
              meta, body, company_id, status, classification) on public.source_records to bianca_sales_reader;
grant select (workspace_id, mailbox, thread_id, canonical_thread_id)
    on public.gmail_thread_aliases to bianca_sales_reader;
grant select (id, workspace_id, company_id, opportunity_id, source, type, text, quote, owner_side, confidence, status,
              action_scope, origin_record_id, origin_occurred_at, source_record_ids)
    on public.insights to bianca_sales_reader;
grant select (workspace_id, source, status, started_at, finished_at, feed_key, coverage_complete)
    on public.sync_runs to bianca_sales_reader;

-- These tables already have RLS enabled. Pair a permissive read policy with a restrictive
-- policy so permissive policies for other uses cannot expand the connector's scope.
create policy bianca_sales_read on public.workspaces for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.companies for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.opportunities for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.contacts for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.company_domains for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.sync_runs for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.source_records for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.gmail_thread_aliases for select to bianca_sales_reader using (true);
create policy bianca_sales_read on public.insights for select to bianca_sales_reader using (true);

create policy bianca_sales_workspace on public.workspaces as restrictive for select to bianca_sales_reader
    using (id = (select bianca_sales_private.bound_workspace_id()));
create policy bianca_sales_workspace on public.companies as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id()));
create policy bianca_sales_workspace on public.opportunities as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id()));
create policy bianca_sales_workspace on public.contacts as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id()));
create policy bianca_sales_workspace on public.company_domains as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id()));
create policy bianca_sales_workspace on public.sync_runs as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id())
           and source in ('pipeline', 'gmail', 'calendar', 'notes'));

-- Keep pending and failed records private, as well as records the classifier marks non-sales.
create policy bianca_sales_workspace on public.source_records as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id())
           and status = 'classified'
           and classification->'is_commercial' = 'true'::jsonb
           and classification->>'category' in ('customer', 'prospect', 'partner', 'internal_sales'));

create policy bianca_sales_workspace on public.gmail_thread_aliases as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id())
           and exists (
               select 1 from public.source_records record
               where record.workspace_id = gmail_thread_aliases.workspace_id
                 and record.source = 'gmail' and record.thread_id = gmail_thread_aliases.canonical_thread_id
           ));

-- An insight can carry a quote from an older classification. Its evidence must still be
-- visible. For legacy insights without an origin, require visibility of every candidate.
create policy bianca_sales_workspace on public.insights as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id()) and (
        (origin_record_id is not null and exists (
            select 1 from public.source_records record
            where record.id = insights.origin_record_id and record.workspace_id = insights.workspace_id
        ))
        or (origin_record_id is null and cardinality(source_record_ids) > 0 and not exists (
            select 1 from unnest(insights.source_record_ids) as candidate(id)
            where not exists (
                select 1 from public.source_records record
                where record.id = candidate.id and record.workspace_id = insights.workspace_id
            )
        ))
    ));
