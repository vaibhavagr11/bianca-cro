import logging

from fastapi import FastAPI, HTTPException

from app import db, ingest, opportunities, process

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bianca_cro")

app = FastAPI(title="Bianca CRO", version="0.1.0")
app.include_router(ingest.router)
app.include_router(process.router)
app.include_router(opportunities.router)


@app.get("/health")
def health() -> dict:
    """Liveness: the app process is up."""
    return {"status": "ok"}


@app.get("/health/db")
def health_db() -> dict:
    """Readiness: the app can reach Postgres."""
    try:
        with db.connect() as conn:
            conn.execute("select 1")
    except Exception as exc:
        # Log the error type only; connection errors can echo the host/user.
        log.error("db health check failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"status": "ok", "database": "reachable"}
