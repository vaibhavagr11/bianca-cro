import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app import actions, brief_api, daily, db, ingest, opportunities, process, review_api
from app.config import Settings, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bianca_cro")

def health() -> dict:
    """Liveness: the app process is up."""
    return {"status": "ok"}


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


def create_app(config: Settings = settings) -> FastAPI:
    transport = None
    lifespan = None
    if config.sales_mcp_enabled:
        from app.sales_mcp import create_sales_transport

        transport, session_manager = create_sales_transport(config)

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            async with session_manager.run():
                yield

    app = FastAPI(title="Bianca CRO", version="0.1.0", lifespan=lifespan)
    app.include_router(ingest.router)
    app.include_router(process.router)
    app.include_router(daily.router)
    app.include_router(opportunities.router)
    app.include_router(brief_api.router)
    app.include_router(actions.router)
    app.include_router(review_api.router)
    app.include_router(review_api.publish_router)
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/health/db", health_db, methods=["GET"])
    if transport is not None:
        if config.sales_mcp_supabase_client_id is not None:
            from app.sales_consent import create_sales_consent_router

            app.include_router(create_sales_consent_router(config))
        # The SDK owns both /sales/mcp and its root-level OAuth discovery route.
        app.mount("/", transport)
    return app


app = create_app()
