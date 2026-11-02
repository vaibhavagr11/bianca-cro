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
and **Ignore**. Approval replaces `Last progress` with the exact previous `Next step`, then writes the approved bullets
into `Next step`. Ignore never writes to the Sheet. Owners RG, NC, and NDL are supported.

- [Live interface and approved sample-data preview](artifacts/README.md)
- [Deployment, Claude Enterprise connector, and n8n setup](docs/gsheet-updates.md)

This feature is disabled by default. It requires migration `012_sheet_review.sql`, a configured Google service account,
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
