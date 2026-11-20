# Daily Brief and Sales Pipeline Updates

Deploy the backend with migration `016_brief_verification` applied, then run the scheduled publishers from the successful Pipeline sync path.

1. Fetch the full configured Pipeline tab with headers, including every Working Client row. Once the fetch succeeds, post `{"rows":[...],"coverage_complete":true}` to `POST /ingest/pipeline`. Keep the returned `sync_run_id` from this execution. Configure the fetch and ingest nodes to stop on error.
2. Complete the remaining ingestion and wait for `POST /process/daily` to return `stage:"done"`.
3. Pass the saved ID as the `pipeline_sync_run_id` query parameter to `POST /brief/build?version=v2`. Send only after the build request succeeds. When action verification is enabled, also wait for `ready_to_send === true` as described in the [verification rollout](../README.md#daily-brief-verification-and-n8n-rollout). Use `dry_run=true` and disconnect the mail node for staging.
4. For the daily Hub queue, either call `POST /process/publish?version=v2` with the same receipt in place of step 3, or import the review-only `sales-pipeline-updates-daily.json` and call it from the same orchestration path. Map the saved ID to its `pipeline_sync_run_id` input. Set the backend URL and Header Auth credential for `X-Ingest-Token` on **Build the next review batch**.

The receipt must identify the latest Pipeline attempt, completed successfully with full coverage on the current workspace date. An old, failed, partial, or superseded receipt returns HTTP 409. Existing callers acquire this check when they begin passing the receipt. Test a failed ingest and a stale receipt in staging before switching the production schedule.

`POST /review/daily` also requires today's processing stage to be `done` with no pending source records. The first successful review cycle starts a persistent 14-day bootstrap window. The review-only subworkflow loops with the same receipt until `done:true`. Retrying resumes saved batches. After completion, another call on the same workspace date reports `skipped:true`.

An authorized operator can call `POST /review/build?force=true` for an intentional same-day repair after a full Sheet sync or deployment upgrade. Force continues or rebuilds the review cycle using its existing first-run cutoff and decisions.

See [workflow import](https://docs.n8n.io/workflows/export-import/) and [HTTP Request credentials](https://docs.n8n.io/integrations/builtin/credentials/httprequest/).

# Sheets proxy: the backend's only route to Google Sheets

n8n is the Google connector, so the backend never holds a Google credential. Import `sheets-proxy.json`; the backend
posts each Sheets call to its webhook and n8n makes it with the existing Google Sheets OAuth2 credential.

1. **Backend request** (Webhook): Header Auth credential, name `X-Bianca-Sheets-Secret`, value = `REVIEW_SHEETS_WEBHOOK_SECRET`.
2. **Read tab** and **Write cells**: select a Google Sheets OAuth2 credential whose account can edit the target spreadsheet.
3. **Check request** allows one spreadsheet ID (the staging copy as committed), a tab read and a cell-value batch update of at most 3 cells.
   For production, duplicate the workflow and change only that ID and the webhook path.
4. Activate it, then set `REVIEW_SHEETS_WEBHOOK_URL` (the Production URL of the webhook) and `REVIEW_SHEETS_WEBHOOK_SECRET` on the backend.

n8n returns `{"status": <Google status>, "body": <Google JSON>}`; the backend keeps all checks (row and header lookup,
before/after comparison, read-back) and treats an unclear write reply as "retry the same approval".
