-- Give the founder connector read access to recorded pipeline changes and follow-up state.
grant select (id, workspace_id, opportunity_id, sync_run_id, field, old_value, new_value, detected_at)
    on public.opportunity_changes to bianca_sales_reader;
grant select (id) on public.sync_runs to bianca_sales_reader;
grant select (owner_name, due_date, completion) on public.insights to bianca_sales_reader;
grant select (content_hash) on public.source_records to bianca_sales_reader;
grant select (id, workspace_id, kind, ref, action, created_at) on public.brief_actions to bianca_sales_reader;

-- Both tables already enforce row-level security. Restrictive policies preserve the workspace scope
-- even if another feature adds a permissive policy.
create policy bianca_sales_read on public.opportunity_changes for select to bianca_sales_reader using (true);
create policy bianca_sales_workspace on public.opportunity_changes as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id())
           and field in ('created', 'removed', 'restored', 'owner_code', 'stage', 'country',
                         'primary_buyer', 'contacts_text', 'last_progress', 'next_step'));

create policy bianca_sales_read on public.brief_actions for select to bianca_sales_reader using (true);
create policy bianca_sales_workspace on public.brief_actions as restrictive for select to bianca_sales_reader
    using (workspace_id = (select bianca_sales_private.bound_workspace_id())
           and kind = 'thread'
           and exists (
               select 1 from public.source_records record
               where record.workspace_id = brief_actions.workspace_id
                 and record.source = 'gmail' and record.thread_id = brief_actions.ref
           ));
