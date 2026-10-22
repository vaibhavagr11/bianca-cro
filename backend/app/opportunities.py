"""Read endpoints for deals: list, and the per-deal timeline. Same token as ingest for now."""

from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row

from app import db, timeline
from app.ingest import _workspace_id, require_token

router = APIRouter(prefix="/opportunities")


@router.get("")
def list_opportunities(x_ingest_token: str | None = Header(default=None)) -> list[dict]:
    """Active deals with how much linked activity each has, most active first."""
    require_token(x_ingest_token)
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        rows = conn.execute(
            "select o.id, c.name as company, o.stage, o.owner_code, count(s.id) as items,"
            " max(s.occurred_at) filter (where s.occurred_at <= now()) as last_activity_at"
            " from opportunities o join companies c on c.id = o.company_id"
            " left join source_records s on s.company_id = c.id and s.status = 'classified'"
            " where o.workspace_id = %s and o.removed_at is null"
            " group by o.id, c.name, o.stage, o.owner_code order by count(s.id) desc, c.name",
            (workspace_id,),
        ).fetchall()
    return [{**r, "id": str(r["id"]), "last_activity_at": timeline._iso(r["last_activity_at"])} for r in rows]


@router.get("/{opportunity_id}/timeline")
def opportunity_timeline(opportunity_id: str, x_ingest_token: str | None = Header(default=None)) -> dict:
    require_token(x_ingest_token)
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        deal = conn.execute(
            "select o.id, o.company_id, c.name as company, c.country, o.stage, o.owner_code, o.primary_buyer,"
            " o.contacts_text, o.last_progress, o.next_step, o.removed_at"
            " from opportunities o join companies c on c.id = o.company_id"
            " where o.workspace_id = %s and o.id::text = %s",
            (workspace_id, opportunity_id),
        ).fetchone()
        if deal is None:
            raise HTTPException(status_code=404, detail="opportunity not found")
        internal = {d.lower() for d in conn.execute(
            "select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()["internal_domains"]}
        records = conn.execute(
            "select id, source, external_id, thread_id, occurred_at, subject, participants, classification, meta"
            " from source_records where company_id = %s and status = 'classified'",
            (deal["company_id"],),
        ).fetchall()
        # "created" from the very first pipeline sync is the initial import, not news; later creations are real.
        changes = conn.execute(
            "select field, old_value, new_value, detected_at from opportunity_changes ch where opportunity_id = %s"
            " and not (field = 'created' and sync_run_id = (select id from sync_runs where workspace_id = %s"
            "  and source = 'pipeline' and status = 'succeeded' order by started_at limit 1))",
            (deal["id"], workspace_id),
        ).fetchall()
        contacts = conn.execute(
            "select name, email, record_count, last_seen_at from contacts where company_id = %s"
            " order by record_count desc, last_seen_at desc nulls last",
            (deal["company_id"],),
        ).fetchall()

    now = timeline.utcnow()
    items = timeline.build_timeline(records, changes, internal, now)
    return {
        "opportunity": {k: (str(v) if k in ("id", "company_id") else timeline._iso(v) if k == "removed_at" else v)
                        for k, v in deal.items()},
        **timeline.summarize(items, now),
        "contacts": [{**c, "last_seen_at": timeline._iso(c["last_seen_at"])} for c in contacts],
        "timeline": items,
    }
