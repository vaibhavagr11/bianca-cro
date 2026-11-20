# Bianca CRO

Commercial intelligence for Bianca AI's founders. It reads Bianca's pipeline file, Gmail, Google Calendar
and meeting notes, keeps track of deals and commitments, and sends a daily morning brief.

## Layout

```
backend/   FastAPI service (Python 3.12, managed with uv)
```

n8n (Cloud) fetches data from Google and posts it to the backend; all business logic lives in `backend/`.

## Sales Pipeline Updates

The founder review application groups proposed changes by company and supports **Approve**, **Edit → Save & approve**,
and **Ignore**. Next steps, commitments, and blockers go to `Next step`; customer needs, buying signals, and objections
go to `Client Context`. Action approval replaces `Last progress` with the exact previous `Next step`, then writes the
approved action bullets into `Next step`. Context approval adds distinct reviewed bullets to the existing `Client Context`.
An empty section leaves its cells unchanged. Ignore never writes to the Sheet. Owners RG, NC, and NDL are supported.

The review queue uses the Daily Brief's action scope. Evidence-backed deal progression and legacy insights with blank
scope can appear. Delivery and out-of-scope insights stay out of both writable sections. A requested deadline stays
with the request. Only an agreed commitment is shown as owed.

The first review cycle imports only sources dated within the preceding **14 days**. Later builds refresh each pending
card from unreviewed evidence. The initial cutoff is persisted once. An earlier bullet remains eligible while its evidence
still qualifies. Clicking a source label highlights the bullets linked to that exact source version.

The configured default destination is the [Pipeline tab of the designated source-of-truth Sheet](https://docs.google.com/spreadsheets/d/14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw/edit?gid=1869050409#gid=1869050409).

- [Live interface and approved sample-data preview](artifacts/README.md)
- [Deployment, Claude Enterprise connector, and n8n setup](docs/gsheet-updates.md)
- [Daily n8n workflow and scheduling setup](n8n/README.md)

This feature is disabled by default. Enabling it requires the optional review migrations, a configured Google Sheets
connection, and separate founder review credentials. Apply the review migrations with `uv run python -m scripts.migrate --sheet-review`.

For changes to Daily Brief wording or delivery, see the [Daily Brief review handoff](DAILY_BRIEF_REVIEW.md).

## Bianca Sales in Claude

The optional `/sales/mcp` connector lets founders ask Claude about live pipeline data through the existing backend.
See [the Bianca Sales setup guide](docs/bianca-sales-connector.md) for the staging database, founder OAuth, and shared Claude Project.

## Run the backend locally

```bash
cd backend
cp .env.example .env        # then paste your Supabase Session pooler URL into DATABASE_URL
uv sync                     # creates .venv and installs dependencies
uv run uvicorn app.main:app --reload --reload-dir app
```

- http://127.0.0.1:8000/health — app is up
- http://127.0.0.1:8000/health/db — app can reach Supabase
- http://127.0.0.1:8000/docs — interactive API docs

## Database migrations

```bash
cd backend
uv run python -m scripts.migrate --status   # what's applied / pending
uv run python -m scripts.migrate --seed     # apply pending migrations, (re)run seeds
```

### Rolling out sales scope for the Daily Brief

After applying `017_action_scope`, run the authenticated `POST /process/extract` with the active
`opportunity_ids` and `"backfill_scope": true` until `remaining_units` reaches zero. Collect
`touched_opportunity_ids` across batches and pass them to `POST /process/profile` in batches.
Check both brief versions with `POST /brief/build?version=v1&dry_run=true` and `version=v2` before
resuming scheduled sends. Legacy actions with blank scope remain eligible while they await re-extraction.

## Ingest endpoints (called by n8n)

`POST /ingest/pipeline` with header `X-Ingest-Token` and body `{"rows": [ {sheet row}, ... ]}` — the
whole Pipeline tab in one call. Try it with made-up data:

```bash
cd backend
curl -s -X POST http://127.0.0.1:8000/ingest/pipeline \
  -H "Content-Type: application/json" -H "X-Ingest-Token: $(grep INGEST_TOKEN .env | cut -d= -f2-)" \
  --data @samples/pipeline_sample.json
```

## Tests

```bash
cd backend
uv run pytest
```

## Rules

- Secrets only in `backend/.env` (git-ignored). Never log email, note or deal contents.
- No Bianca data files in the repo.

## Daily Brief verification and n8n rollout

The verified builder works for both `v1` and `v2`. It checks the actual action rows after selection.
It uses real replies and action-specific evidence from stored activity, including other Gmail threads.
Unclear rows go to **Needs verification**, and only still-owed actions stay in the action sections.
No automatic verdict changes a founder's Done/Undo decision. Audit fields include the checked sync,
the evidence record IDs, verdicts, and model token use. Email bodies and evidence quotes are not logged.

1. Apply the new migrations. Keep `BRIEF_VERIFICATION_REQUIRED=false` while testing the workflow.
2. Generate one UUID as `sync_batch_id` per n8n execution. Fetch all pages for each required source
   before posting it to its existing ingest endpoint. Add `sync_batch_id` and
   `coverage_complete: true` to the JSON body for each complete fetch. Include `mailbox: "RG"`
   or `"NC"` for Gmail and Calendar, and `includes_sent: true` for Gmail only once Sent mail
   is covered. A successful empty fetch still needs its receipt. Do not attest a partial fetch,
   a search that omitted Sent mail, or an upstream failure. Keep each response's `sync_run_id`.
3. Call `POST /process/daily?sync_batch_id=<uuid>` until `stage` is `done`. Stop on
   `timed_out` or any failure. `done: true` by itself also means the run timed out.
4. For a no-send preview, call `POST /brief/build?version=v2&verification=shadow&dry_run=true`.
   For live enforcement, use `POST /brief/build?version=v2&verification=required`.
   Send JSON `{"sync_run_ids":["<uuid>", "..."]}` and the existing ingest token. The receipts
   must be the latest for each configured feed and share the same `sync_batch_id`.
5. While the response has `verification_status: "pending"`, repeat the same build request with
   the returned `build_id` as a query parameter. Each call checks a bounded batch. If the
   status is `blocked`, stop and resolve the reason before starting a new build. Only the live
   branch should send, and only when `ready_to_send` is exactly `true`. Use `build_id` as the
   workflow's delivery deduplication key. A preview never sets `ready_to_send`.
6. Review a preview alongside the original output using founder-reviewed examples. Track stale rows
   caught, open rows incorrectly suppressed, unclear rows, token usage, and time to a ready brief.
   After n8n is switched to the verified contract, set `BRIEF_VERIFICATION_REQUIRED=true` to
   prevent non-dry-run builds from bypassing the check.

The default required feeds are `["gmail:RG","gmail:NC","calendar:RG","calendar:NC","notes","pipeline"]`.
`BRIEF_VERIFICATION_FEEDS` can override that JSON list for a deployment. Receipts expire after
`BRIEF_VERIFICATION_SYNC_HOURS` (default 6), and `BRIEF_VERIFICATION_TOKEN_BUDGET` sets the model
budget per build (default 24,000). The check is limited to stored activity. WhatsApp is outside it.
