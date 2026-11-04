# Sales Pipeline Updates

## Behavior

The founder interface preserves the approved Bianca layout, title **Sales Pipeline Updates**, and owners Ramiro (`RG`),
Nico Casaux (`NC`), and Nico de León (`NDL`). One card holds the context and next actions for one pipeline company.

- **Next Step** contains next steps, commitments, and blockers; **Client Context** contains customer needs, buying signals, and objections. Routing uses the stored insight type, not the wording of an editable bullet.
- **Approve** sends both reviewed lists. **Edit → Save & approve** uses separate editors for the founder's changes.
- If action bullets are present, they replace `Next step` and its **exact previous value replaces `Last progress`**, including an empty old Next step. The previous Last progress is overwritten, not appended.
- If context bullets are present, they are added to existing `Client Context`, deduplicating equivalent bullet lines. The existing text is preserved. An empty approved section leaves its corresponding cells unchanged, so context-only approval never rotates action cells.
- Before/after values for all three cells and the exact destination are stored in the durable audit.
- **Ignore** dismisses the card for all founders without writing any Sheet cell. It remains in the activity history.

`artifacts/sales-pipeline-updates.html` is the live app. It starts without company data and requires a configured connector
or authenticated access on the hosted backend. `sales-pipeline-updates-demo.html` is the separate fictional-data preview.
Real company data and credentials are never written to browser storage. Server records are shared by all reviewers.

The default destination is [the designated Pipeline tab](https://docs.google.com/spreadsheets/d/14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw/edit?gid=1869050409#gid=1869050409): spreadsheet `14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw`, tab `Pipeline`, numeric sheet ID `1869050409`. Overrides must deliberately name another authorized environment, including its sheet ID. The service checks the numeric tab ID as well as the title.

## How it connects

```mermaid
flowchart LR
    Google[Google sources] --> n8n
    n8n --> Ingest[Existing ingestion and daily processing]
    Ingest --> Memory[(Supabase PostgreSQL)]
    Memory --> Publish[POST /process/publish]
    Publish --> Brief[Daily Email Brief]
    Publish --> Queue[Persistent company review queue]
    Queue --> Connector[Bianca Review MCP connector]
    Connector --> Artifact[Claude Enterprise Artifact]
    Artifact -->|Approve or edited approval| Writer[Backend Sheet writer]
    Writer --> Sheet[Source-of-truth Pipeline tab]
```

The queue reuses the commercial classifications and quotation-validated insights already extracted for the brief.
It does not run a second summarization model, reuse the brief watermark, or apply the brief's four-company display cap.
Every active pipeline company with unreviewed linked commercial Gmail, meeting-note, or calendar records can get a card.
Unknown/unlinked companies first need an authoritative pipeline row and entity resolution in the existing ingestion flow.

The first review cycle imports only records whose `occurred_at` falls inside the inclusive interval from initialization minus
14 days through initialization. The database persists that boundary and the original versions excluded as undated or outside
the interval. All initially stored source versions are considered for exclusion, including records not yet classified or linked;
the older backlog therefore cannot leak back in when those records are classified later. Sources are retained in Supabase;
this window controls the review queue, not the existing brief's ingestion.

After bootstrap, there is no rolling date cutoff. New records and changed source-content versions become eligible during
later daily checks, including a newly arrived historical item or a real update to an excluded old version. The unchanged
initially excluded version stays excluded. Re-extracting the same text with new insight UUIDs does not create new source content.

While a company card is pending, each daily build appends new distinct bullets, preserves earlier bullets and evidence snapshots,
and unions supporting sources when the same bullet has multiple citations. Unreviewed bullets do not age out after 14 days or
disappear merely because an extraction's task status changes. A newer revision invalidates an older editor snapshot. Only Approve,
Save & approve, or Ignore closes the card and consumes its accumulated versions; new content after that can create another card.
Calendar cancellation status is part of source identity. Title-only or otherwise unextracted updates say that an email,
note, or scheduled/cancelled meeting was recorded and ask the founder to review it; they do not invent buying intent.

## Daily cycles and source lineage

Migration 014 adds workspace review state, bootstrap exclusions, current observed source versions, and per-bullet lineage on
proposals/decisions. Initialization, exclusions, and a build batch commit together. Failed attempts roll back; retries resume the
persisted cutoff and unfinished cycle. Batches can continue across requests and midnight. Once a cycle completes, normal calls
on the same workspace calendar date return `done:true, skipped:true`. The next date starts another cycle. The workspace timezone
controls the date, independently of the viewer's browser timezone.

The graph is explicit: every source has an ID derived from source type, external identity, and content version. Each aligned
`bulletLineage.nextStep[]` or `.clientContext[]` entry contains `bulletId`, the exact `text`, and `sourceIds`. A source edge is
created only when the supporting passage is validated against that record, or when that record supplies its own conservative
fallback. Duplicate bullets retain the union of their real supporting source IDs. Two emails on the same date remain distinct.
Historical versions retain captured metadata/quotes, so an expanded pending card does not silently reattribute old text to a newer version.

Clicking a source button highlights only its linked bullets across both destinations, with a pressed state and announced match count.
Selection is local to its company card and survives refresh while the source still exists. Missing or malformed metadata shows an
explicit tracing-unavailable state rather than guessing from labels. Founder-edited text gets no original source edges unless it
matches an original bullet in the same destination; manual edits are recorded as `founder_edit` in the decision lineage.

## Deploy the backend

1. Check out this branch and install the locked Python dependencies with `uv sync` in `backend/`.
2. Apply migrations with `uv run python -m scripts.migrate`. Migration 012 adds proposals, durable decisions, and consumed-source receipts; migration 013 adds Client Context, typed destination lists, and durable target binding; migration 014 adds daily/bootstrap state and exact source lineage.
   Existing domain tables and the new tables retain RLS with backend-only access.
3. Enable the Google Sheets API in the service account's Google Cloud project. Share the intended spreadsheet with that
   service-account email as **Editor**. The application reads the configured tab and writes only changed values among Client Context, Last progress, and Next step.
4. Configure server secrets and settings below, then set `REVIEW_ENABLED=true`. Set `PUBLIC_BASE_URL` to the real HTTPS backend.
   Never place the ingest token, founder tokens, or service-account JSON in an artifact or committed file.
5. Open `/review/app` on the backend for the same-origin hosted version. Sign in with an assigned founder token; it is held
   only in page memory. Each API request authenticates separately. `/review/demo` contains fictional data only.

| Setting | Purpose |
| --- | --- |
| `REVIEW_ENABLED` | Defaults to `false`; gates all review and publish routes. |
| `REVIEW_TOKENS` | JSON mapping of `RG`, `NC`, `NDL` to **distinct**, random bearer tokens of at least 32 characters. Configure only enabled reviewers. |
| `REVIEW_SPREADSHEET_ID` | Defaults to `14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw`; check existing environment overrides. |
| `REVIEW_SHEET_TAB` | Exact tab name, default `Pipeline`. |
| `REVIEW_SHEET_ID` | Numeric tab ID, default `1869050409`; validates that the named tab is the intended destination. |
| `REVIEW_CLIENT_CONTEXT_MODE` | Default `append`, matching the approved behavior: preserve context and add distinct bullets. `replace` is an explicit deployment override, not the founders' selected default. |
| `REVIEW_GOOGLE_SERVICE_ACCOUNT_JSON` | Full service-account credential as one JSON environment secret. |
| `REVIEW_BUILD_BATCH` | Changed companies per build call, default `20`; repeat while `done=false`. |
| `REVIEW_ALLOWED_ORIGINS` | Explicit Origin allowlist, default `["https://claude.ai"]`; the configured backend origin is also accepted. Server MCP calls may omit Origin. |

Generate separate tokens with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and store them securely.
The existing `INGEST_TOKEN` remains for n8n's ingestion/build endpoints. It cannot authorize a founder decision.

### Upgrade from the original review format

Deploy the API, live HTML, connector schema, and migrations through 014 together. After migration, sync the **full Pipeline tab including Client Context from the new target spreadsheet**, complete source processing, and run `/review/daily` until `done=true`. To intentionally repair or rebuild on an already completed day, use `/review/build?force=true` and drain all batches. Force never resets initialization or exclusions. Pending legacy cards are rebuilt from eligible typed insights with real lineage; an old card with no eligible material can be superseded. Do not infer categories or edges from old flat text. Existing `REVIEW_SPREADSHEET_ID` environment values override the new code default and must be updated deliberately.

Old `bullets`-only approval requests are rejected; the new payload requires `next_step_bullets` and `client_context_bullets`, either of which may be empty while the combined request must be nonempty. Old applying requests without typed routing/target binding require operator reconciliation. They are never replayed into a new destination. New intents bind spreadsheet ID, tab name, and numeric tab ID; changing configuration cannot silently redirect an in-flight write.

Previously saved typed applying approvals remain recoverable even if they predate lineage; their retry payload and target remain authoritative. Their source buttons show tracing unavailable instead of fabricating edges. Do not delete `review_queue_state` or exclusions as a routine deployment/reset step: that would create another first-run window.

## Connect the Enterprise Artifact

The backend provides a stateless Streamable HTTP MCP endpoint at `https://<backend>/review/mcp`, with:

- `bianca_review_workspace` — read shared pending cards, mirrored pipeline cells, and recent decisions.
- `bianca_approve_update` — apply the explicitly reviewed original/edited bullets.
- `bianca_ignore_update` — dismiss a proposal without a Sheet write.

An authorized Enterprise administrator adds a custom remote connector with that endpoint and an `Authorization: Bearer <token>`
request header. Use the viewing founder's **individual** credential/connection. **A shared request-header credential attributes
every action to the founder owning that token; it does not prove the viewer's Enterprise identity.** If the organization's
connector configuration cannot isolate per-founder headers, create separately access-controlled founder connectors or put an
OAuth identity gateway in front of these endpoints before enabling individual audit attribution. OAuth/SSO is not implemented here.

Enable artifacts and artifact connectors in the organization's settings. Each founder connects and grants access to the review
tools. Publish the live HTML privately using [the account-specific publishing instructions](../artifacts/claude-publish-prompt.md).
The exact Claude JavaScript connector bridge is generated/validated inside the actual account; the shipped client exposes the
small `window.biancaConnector.callTool(name, args)` integration point and otherwise shows a connection-required state.
It never guesses an undocumented native bridge, embeds credentials, or directly calls an external Render URL from the artifact.

This branch supplies the backend, tools, UI, and setup instructions; it does **not** publish or authorize an artifact in an
Enterprise account. Validate the account's actual connector behavior using a designated test Sheet before production activation.

Official references: [Claude connector-backed artifacts](https://code.claude.com/docs/en/artifacts#pull-live-data-with-mcp-connectors),
[custom remote connector setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

## n8n: generate the brief and review queue in parallel

Keep the existing four ingestion paths and `/process/daily` loop. After the loop returns both `done=true` and `stage="done"`,
replace its brief-build call with `POST /process/publish?version=v2`, authenticated with `X-Ingest-Token`.
This endpoint independently runs the existing brief builder and the review builder in two workers. It rejects a daily run that
is absent, incomplete, `timed_out`, or still has pending source records; existing `/process/daily` and `/brief/build` behavior is unchanged.

The response has independent `brief` and `review` results:

```json
{
  "brief": {"ok": true, "result": {"subject": "...", "html": "...", "text": "...", "to": "..."}},
  "review": {"ok": true, "result": {"created": 6, "updated": 0, "remaining": 0, "done": true}}
}
```

In n8n:

1. Send the email only when `brief.ok` is true, using `brief.result.subject`, `.html`, `.text`, and `.to`.
2. If `review.ok` is true but `review.result.done` is false, repeat `POST /review/build` until it returns `done=true`.
3. If one branch fails, retry **that branch only**: `/brief/build?version=v2` or `/review/build`. Repeating `/process/publish`
   builds another brief and advances its history again; do not use it as the retry for a failed review build.
4. Continue the full Pipeline-sheet sync on its usual schedule; a successful approval also refreshes the backend mirror when
   its reviewed baseline is still current. The mirror now includes Client Context. If `mirror_refreshed=false`, trigger the existing **full** pipeline sync before relying
   on its cache. Never send a partial pipeline snapshot: the existing ingest treats absent companies as removed.

You can choose `version=v1` explicitly if the live email workflow still uses v1. Review generation remains independent.
No n8n workflow or production email settings are changed merely by committing this branch.

For a dedicated once-daily review check, import [the supplied n8n workflow](../n8n/sales-pipeline-updates-daily.json) and follow
[its setup instructions](../n8n/README.md). It calls `POST /review/daily`, requires completed upstream processing, and drains batches
without generating another brief. Its default schedule is 09:00 America/Argentina/Buenos_Aires; set the real workspace timezone,
backend URL, Header Auth credential, and a time after ingestion before activation. The JSON is inactive by default and contains no secrets.
The existing daily publisher or this workflow can drive the same date-gated builder; browser Refresh does not run the schedule.

`POST /review/build?force=true` is an ingest-authenticated manual repair path, not a normal scheduler setting. Approvals do not
trigger a new build. New same-day arrivals after a completed cycle wait for the next normal daily cycle unless an operator explicitly rebuilds.

## Approval and recovery

The server scopes every lookup to the configured workspace, verifies the card revision, and serializes decisions for that
proposal. It resolves the current Sheet row by normalized `Empresa` and resolves `Last progress`/`Next step` by their headers
on every attempt, together with `Client Context`. Missing/duplicate companies or headers, a mismatched numeric tab ID, formulas in review cells, and changed reviewed values stop the write.
Fixed before/after values and the request hash are committed **before** the Google write is attempted. Changed cells are written as literal
strings in a single atomic `spreadsheets.batchUpdate` and then read back across all three review cells. Only changed cells are included in the write; other cells and formatting are untouched.

If a response is lost, the card remains `applying`, with the exact retry payload. Retry that saved approval: the writer recognizes
the desired final cell values and never rotates them a second time. Another founder cannot replace that in-flight decision.
A completed request returns the original result when retried with its original key and payload. Ignore consumes the reviewed
source versions but never initializes Google credentials or makes a Sheets request.

If the Sheet changed before the first write, resync the Pipeline tab, rebuild proposals with `/review/build?force=true` if today's cycle already completed, and review the new revision.
If an earlier write might have succeeded and a later human edit makes both snapshots differ, the saved intent stays applying.
An operator must reconcile the recorded before/after values with the Sheet before retrying; the service will not discard an
uncertain write and rotate again. Preserve audit records. Do not edit them to pretend an unverified write succeeded.

**Google Sheets has no cell-level compare-and-swap.** The read/check and atomic write are separate requests. Human edits,
sorting, or another external writer in the narrow interval can race them. Backend locks prevent competing approvals through
this service, but cannot lock the Google UI. Avoid concurrent sorting/manual editing of these cells during approval and
validate this workflow on a test Sheet. The readback and durable audit help detect/recover uncertain outcomes; they do not
claim to eliminate that provider limitation. [Google batch update behavior](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)

## Verification

```sh
cd backend
uv run pytest
cd ..
node tests-frontend/verify-live.cjs
node tests-frontend/verify-demo.cjs
```

Tests use dummy database credentials and mocked Google/Claude connections. They cover review transport/auth, source receipts,
revision changes, NDL, first-run cutoff/exclusions, daily gates and resumable batches, accumulated pending cards, per-bullet source graphs, typed category routing, context append/deduplication, context-only preservation of action cells, exact cell replacement, Ignore, durable retries/target binding, stale data, moved headers/rows, literal writes, and
frontend error recovery. Live Google credentials, the production database, and the Enterprise runtime bridge are not exercised
by these tests. Browser appearance needs verification in the deployment environment.
