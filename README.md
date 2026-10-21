# AI CRO

Always-on commercial intelligence for founder-led sales. First design partner: Bianca AI.

## Layout

```
backend/   FastAPI service (Python 3.12, managed with uv)
```

n8n (Cloud) fetches data from Google and posts it to the backend; all business logic lives in `backend/`.

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

## Tests

```bash
cd backend
uv run pytest
```

## Rules

- Secrets only in `backend/.env` (git-ignored). Never log customer content.
- No customer data files in the repo.
