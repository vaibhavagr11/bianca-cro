"""The morning processing run, one short step per call: n8n calls POST /process/daily until it says done.

Stages: classify new items -> link companies -> extract insights -> fresh profiles for touched deals.
Legacy calls also run reconciliation. Verified builds check completion before sending instead.
"""

import logging
from datetime import timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import db, process, timeline
from app.ingest import _workspace_id, require_token

log = logging.getLogger("bianca_cro.daily")
router = APIRouter(prefix="/process")

FINAL = ("done", "timed_out")
BATCH = {"reconcile": 3, "profile": 2}  # deals per call; each is one Claude call of up to ~40 s
MAX_RUN = timedelta(minutes=50)


def take(queue: list[str], stage: str) -> tuple[list[str], list[str]]:
    """Split the next batch of deals off the queue."""
    n = BATCH[stage]
    return queue[:n], queue[n:]


@router.post("/daily")
def daily_step(x_ingest_token: str | None = Header(default=None), sync_batch_id: UUID | None = None) -> dict:
    """Advance today's run by one step. Safe to call again after it is done."""
    require_token(x_ingest_token)
    with db.connect() as lock_conn:
        workspace_id = _workspace_id(lock_conn)
        lock_conn.commit()
        lock_conn.autocommit = True
        lock_key = f"bianca-cro/daily/{workspace_id}"
        if not lock_conn.execute("select pg_try_advisory_lock(hashtextextended(%s, 0))", (lock_key,)).fetchone()[0]:
            raise HTTPException(status_code=409, detail="A daily processing step is already running")
        try:
            return _daily_step(workspace_id, x_ingest_token, sync_batch_id)
        finally:
            lock_conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,))


def _daily_step(workspace_id, x_ingest_token: str | None, sync_batch_id: UUID | None) -> dict:
    with db.connect() as conn:
        conn.row_factory = dict_row
        latest_sync = None
        if sync_batch_id is not None:
            from app.brief_delivery import validate_sync_batch

            latest_sync = validate_sync_batch(conn, workspace_id, str(sync_batch_id))
        tz = ZoneInfo(conn.execute("select timezone from workspaces where id = %s", (workspace_id,)).fetchone()["timezone"])
        today = timeline.utcnow().astimezone(tz).date()
        run = conn.execute(
            "insert into daily_runs (workspace_id, for_date, sync_batch_id) values (%s, %s, %s)"
            " on conflict (workspace_id, for_date) do nothing returning *",
            (workspace_id, today, sync_batch_id),
        ).fetchone()
        if run is None:
            run = conn.execute(
                "select * from daily_runs where workspace_id = %s and for_date = %s",
                (workspace_id, today),
            ).fetchone()
        if sync_batch_id is None and run["sync_batch_id"] is not None:
            raise HTTPException(status_code=409, detail="Use the current sync batch to advance today's run")
        pending = conn.execute("select exists (select 1 from source_records where workspace_id = %s"
                               " and status = 'pending')", (workspace_id,)).fetchone()["exists"]
        if sync_batch_id is not None and (sync_batch_id != run["sync_batch_id"] or latest_sync > run["started_at"]):
            run = conn.execute(
                "update daily_runs set sync_batch_id = %s, stage = 'classify', queue = '{}', touched = '{}',"
                " started_at = clock_timestamp(), finished_at = null, steps = 0, log = '[]'::jsonb"
                " where id = %s returning *", (sync_batch_id, run["id"]),
            ).fetchone()
        elif run["stage"] in FINAL and pending:
            # A sync after today's run finished brought new items: run the stages again for them.
            run = conn.execute(
                "update daily_runs set stage = 'classify', queue = '{}', touched = '{}', started_at = now(),"
                " finished_at = null where id = %s returning *", (run["id"],)).fetchone()
        conn.commit()
        opportunity_ids = [r["id"] for r in conn.execute(
            "select id::text as id from opportunities where workspace_id = %s and removed_at is null",
            (workspace_id,))]

    if run["stage"] in FINAL:
        return _summary(run, done=True)

    stage, queue, touched = run["stage"], list(run["queue"]), list(run["touched"])
    if timeline.utcnow() - run["started_at"] > MAX_RUN:
        stage, entry = "timed_out", {"stage": run["stage"], "note": "run took too long; stopped"}
    elif stage == "classify":
        res = process.classify_threads(x_ingest_token=x_ingest_token)
        entry = {"stage": stage, **{k: res[k] for k in ("threads", "commercial", "linked_to_company", "failed",
                                                       "remaining_threads")}}
        if res["remaining_threads"] == 0:
            stage = "resolve"
    elif stage == "resolve":
        res = process.resolve_entities(x_ingest_token=x_ingest_token)
        entry = {"stage": stage, "records": res["records"], "link_methods": res["link_methods"]}
        stage = "extract"
    elif stage == "extract":
        res = process.extract_insights(process.ExtractRequest(opportunity_ids=opportunity_ids),
                                       x_ingest_token=x_ingest_token)
        touched = set(touched) | set(res.get("touched_opportunity_ids") or [])
        entry = {"stage": stage, **{k: v for k, v in res.items() if k != "touched_opportunity_ids"}}
        if res["remaining_units"] == 0:
            with db.connect() as conn:
                touched.update(r[0] for r in conn.execute(
                    "select distinct opportunity_id::text from insights where workspace_id = %s"
                    " and opportunity_id is not null and last_extracted_at >= %s", (workspace_id, run["started_at"])))
        touched = sorted(touched)
        if res["remaining_units"] == 0:
            stage, queue = ("profile" if sync_batch_id is not None else "reconcile"), touched
    elif stage == "reconcile":
        batch, queue = take(queue, stage)
        res = process.reconcile(batch) if batch else {}
        entry = {"stage": stage, "deals": len(batch), "marked": res.get("marked", 0)}
        if not queue:
            stage, queue = "profile", touched
    else:  # profile
        batch, queue = take(queue, stage)
        res = process.build_profiles(process.ExtractRequest(opportunity_ids=batch),
                                     x_ingest_token=x_ingest_token) if batch else {"profiles": []}
        entry = {"stage": stage, "deals": len(batch), "failed": sum(1 for p in res["profiles"] if p.get("failed"))}
        if not queue:
            stage = "done"

    with db.connect() as conn:
        conn.row_factory = dict_row
        run = conn.execute(
            "update daily_runs set stage = %s, queue = %s, touched = %s, steps = steps + 1, log = log || %s,"
            " finished_at = case when %s then now() end where id = %s returning *",
            (stage, queue, touched, Jsonb([entry]), stage in FINAL, run["id"])).fetchone()
    log.info("daily step %s: %s", run["steps"], entry)
    return _summary(run, done=stage in FINAL, did=entry)


def _summary(run: dict, done: bool, did: dict | None = None) -> dict:
    return {"done": done, "stage": run["stage"], "steps": run["steps"], "did": did,
            "deals_with_new_insights": len(run["touched"]), "for_date": run["for_date"].isoformat(),
            "sync_batch_id": str(run["sync_batch_id"]) if run["sync_batch_id"] else None}
