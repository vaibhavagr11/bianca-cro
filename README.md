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

The first review cycle imports only sources dated within the preceding **14 days**. Later daily cycles add new source versions
to the same pending company card, preserving earlier bullets until Approve, Save & approve, or Ignore. The cutoff is persisted once;
pending bullets do not expire after 14 days. Clicking a source label highlights only the bullets linked to that exact source version.

The configured default destination is the [Pipeline tab of the designated source-of-truth Sheet](https://docs.google.com/spreadsheets/d/14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw/edit?gid=1869050409#gid=1869050409).

- [Live interface and approved sample-data preview](artifacts/README.md)
- [Deployment, Claude Enterprise connector, and n8n setup](docs/gsheet-updates.md)
- [Daily n8n workflow and scheduling setup](n8n/README.md)

This feature is disabled by default. It requires migrations through `014_review_daily_lineage.sql`, a configured Google service account,
and separate founder review credentials. Committing this code does not connect or modify a production Sheet or Claude account.

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
