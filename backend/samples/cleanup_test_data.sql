-- Run in Supabase SQL Editor after testing with the sample files.
-- Deleting the companies cascades to their opportunities and opportunity_changes.
delete from companies where name like '%(TEST)%';
delete from sync_runs where source = 'pipeline';
