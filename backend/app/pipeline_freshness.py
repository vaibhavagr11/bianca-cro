"""Check the Pipeline receipt supplied by a scheduled publisher."""

from uuid import UUID

from fastapi import HTTPException

from app import db
from app.ingest import _workspace_id


def require_fresh_pipeline(sync_run_id: UUID) -> None:
    with db.connect() as conn:
        check_fresh_pipeline(conn, _workspace_id(conn), sync_run_id)


def check_fresh_pipeline(conn, workspace_id, sync_run_id: UUID) -> None:
    latest = conn.execute(
        "select s.id, s.status, s.coverage_complete,"
        " (s.finished_at at time zone w.timezone)::date = (now() at time zone w.timezone)::date"
        " from sync_runs s join workspaces w on w.id = s.workspace_id"
        " where s.workspace_id = %s and s.source = 'pipeline'"
        " order by s.started_at desc, s.id desc limit 1", (workspace_id,),
    ).fetchone()
    if latest != (sync_run_id, "succeeded", True, True):
        raise HTTPException(409, "A current, successful full Pipeline sync is required. Sync the Sheet and retry.")
