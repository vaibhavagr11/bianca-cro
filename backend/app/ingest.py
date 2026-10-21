"""Ingest endpoints called by n8n. Thin layer: auth, sync_runs bookkeeping, DB writes. Logic lives in pipeline.py."""

import hmac
import logging

import psycopg
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import db
from app.config import settings
from app.pipeline import (
    TRACKED_FIELDS,
    ExistingDeal,
    PipelineValidationError,
    SyncPlan,
    SyncRefused,
    parse_rows,
    parse_stage,
    plan_sync,
)

log = logging.getLogger("ai_cro.ingest")
router = APIRouter(prefix="/ingest")


class PipelinePayload(BaseModel):
    rows: list[dict]


def require_token(token: str | None) -> None:
    expected = settings.ingest_token.get_secret_value()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid ingest token")


@router.post("/pipeline")
def ingest_pipeline(payload: PipelinePayload, x_ingest_token: str | None = Header(default=None)) -> dict:
    require_token(x_ingest_token)

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        run_id = conn.execute(
            "insert into sync_runs (workspace_id, source, rows_received) values (%s, 'pipeline', %s) returning id",
            (workspace_id, len(payload.rows)),
        ).fetchone()[0]
        conn.commit()  # the run is recorded even if everything below fails

        try:
            rows = parse_rows(payload.rows)
            with conn.transaction():
                plan = plan_sync(_load_existing(conn, workspace_id), rows)
                _apply(conn, workspace_id, run_id, plan)
                counts = {
                    "created": len(plan.creates),
                    "updated": len(plan.updates),
                    "unchanged": len(plan.unchanged),
                    "removed": len(plan.removals),
                }
                conn.execute(
                    "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s,"
                    " rows_updated = %s, rows_unchanged = %s, rows_removed = %s where id = %s",
                    (counts["created"], counts["updated"], counts["unchanged"], counts["removed"], run_id),
                )
        except PipelineValidationError as exc:
            _fail(conn, run_id, f"validation: {exc}")
            raise HTTPException(status_code=422, detail=str(exc))
        except SyncRefused as exc:
            _fail(conn, run_id, f"refused: {exc}")
            raise HTTPException(status_code=409, detail=f"sync refused: {exc}")
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("pipeline sync %s failed", run_id)
            raise HTTPException(status_code=500, detail="pipeline sync failed")

    log.info("pipeline sync %s succeeded: received=%s %s", run_id, len(payload.rows), counts)
    return {"sync_run_id": str(run_id), "received": len(payload.rows), **counts}


def _workspace_id(conn: psycopg.Connection):
    row = conn.execute("select id from workspaces where name = %s", (settings.workspace_name,)).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="workspace not configured")
    return row[0]


def _fail(conn: psycopg.Connection, run_id, error: str) -> None:
    conn.rollback()
    conn.execute("update sync_runs set status = 'failed', finished_at = now(), error = %s where id = %s", (error, run_id))
    conn.commit()


def _load_existing(conn: psycopg.Connection, workspace_id) -> list[ExistingDeal]:
    rows = conn.execute(
        "select o.id, c.normalized_name, o.owner_code, o.stage, c.country, o.primary_buyer, o.contacts_text,"
        " o.last_progress, o.next_step, o.row_hash, o.removed_at is not null"
        " from opportunities o join companies c on c.id = o.company_id where o.workspace_id = %s",
        (workspace_id,),
    ).fetchall()
    # Column order above matches TRACKED_FIELDS.
    return [
        ExistingDeal(str(r[0]), r[1], dict(zip(TRACKED_FIELDS, r[2:9])), r[9], r[10])
        for r in rows
    ]


def _apply(conn: psycopg.Connection, workspace_id, run_id, plan: SyncPlan) -> None:
    changes: list[tuple] = []  # (opportunity_id, field, old, new)

    for row in plan.creates:
        company_id = conn.execute(
            "insert into companies (workspace_id, name, normalized_name, country) values (%s, %s, %s, %s)"
            " on conflict (workspace_id, normalized_name) do update set name = excluded.name, country = excluded.country"
            " returning id",
            (workspace_id, row.company, row.normalized_name, row.fields["country"]),
        ).fetchone()[0]
        opp_id = conn.execute(
            "insert into opportunities (workspace_id, company_id, owner_code, stage, stage_rank, primary_buyer,"
            " contacts_text, last_progress, next_step, row_hash) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " returning id",
            (workspace_id, company_id, *_opp_values(row), row.row_hash),
        ).fetchone()[0]
        changes.append((opp_id, "created", None, row.company))

    for opp_id, row, field_changes, restored in plan.updates:
        conn.execute(
            "update opportunities set owner_code = %s, stage = %s, stage_rank = %s, primary_buyer = %s,"
            " contacts_text = %s, last_progress = %s, next_step = %s, row_hash = %s,"
            " last_seen_at = now(), removed_at = null where id = %s",
            (*_opp_values(row), row.row_hash, opp_id),
        )
        conn.execute(
            "update companies set name = %s, country = %s"
            " where id = (select company_id from opportunities where id = %s)",
            (row.company, row.fields["country"], opp_id),
        )
        if restored:
            changes.append((opp_id, "restored", None, row.company))
        changes.extend((opp_id, f, old, new) for f, old, new in field_changes)

    if plan.unchanged:
        conn.execute("update opportunities set last_seen_at = now() where id = any(%s::uuid[])", (plan.unchanged,))

    for deal in plan.removals:
        conn.execute("update opportunities set removed_at = now() where id = %s", (deal.opportunity_id,))
        changes.append((deal.opportunity_id, "removed", None, None))

    if changes:
        with conn.cursor() as cur:
            cur.executemany(
                "insert into opportunity_changes (workspace_id, opportunity_id, sync_run_id, field, old_value, new_value)"
                " values (%s, %s, %s, %s, %s, %s)",
                [(workspace_id, opp_id, run_id, f, old, new) for opp_id, f, old, new in changes],
            )


def _opp_values(row) -> tuple:
    f = row.fields
    return (f["owner_code"], f["stage"], parse_stage(f["stage"]), f["primary_buyer"],
            f["contacts_text"], f["last_progress"], f["next_step"])
