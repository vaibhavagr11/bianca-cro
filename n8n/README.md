# Daily Sales Pipeline Updates

Import `sales-pipeline-updates-daily.json` into n8n to schedule the review queue once per day. It is inactive and contains no credentials. It uses the existing stored/processed source data; keep the existing Google ingestion workflows.

1. Deploy the backend and matching UI, apply migrations through **014**, and sync the full configured Pipeline tab.
2. On **Build the next review batch**, confirm the actual backend URL and select a Header Auth credential whose header name is `X-Ingest-Token` and whose value is the existing ingest secret. Do not paste secrets into workflow JSON.
3. Set the workflow timezone to match `workspaces.timezone`. The supplied schedule is **09:00 America/Argentina/Buenos_Aires**, matching the repository seed. Change the time to run after all intended ingestion and the existing `/process/daily` loop finish.
4. Run manually against the test environment. The first successful queue cycle starts a persistent 14-day bootstrap window. The HTTP response returns `done:false` when more company batches remain; the workflow loops until `done:true`.
5. Publish/activate the workflow after verification. Repository code does not activate an n8n schedule by itself.

`POST /review/daily` requires today's processing stage to be `done` with no pending source records. A missing, incomplete, or timed-out upstream run returns 409 and stops the workflow. Finish processing and rerun; do not force past this error in the daily job. Network/server errors also stop the workflow so normal n8n error handling can notify its owner. Retrying the same cycle resumes saved batches; after completion, another normal call on the same workspace date reports `skipped:true`.

If the existing morning workflow already calls `/process/publish`, that endpoint also drives the same date-gated review builder. Prefer that existing daily scheduling path or this dedicated review-only workflow. The dedicated workflow neither builds another brief nor sends email, approves cards, or writes Sheet cells.

An authorized operator can call `POST /review/build?force=true` for an intentional same-day repair after a full Sheet sync or deployment upgrade. Force continues/rebuilds a review cycle; it **does not reset** the first-run cutoff, erase exclusions or decisions, or create a rolling 14-day filter. Do not add force to the normal scheduled job.

This workflow's JSON and connections are checked locally. Its credential selection, exact timing, import compatibility, and final execution must be verified in the actual n8n account.

References: [n8n Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/), [workflow JSON import](https://docs.n8n.io/workflows/export-import/), and [HTTP Request credentials](https://docs.n8n.io/integrations/builtin/credentials/httprequest/).

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
