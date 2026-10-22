"""Processing endpoints called by n8n after ingestion. Each call handles a bounded batch and is safe to repeat."""

import logging

import anthropic
from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import classifier, db
from app.config import settings
from app.ingest import _fail, _workspace_id, require_token
from app.pipeline import normalize_name

log = logging.getLogger("bianca_cro.process")
router = APIRouter(prefix="/process")


@router.post("/classify")
def classify_threads(x_ingest_token: str | None = Header(default=None)) -> dict:
    """Classify up to classify_batch_threads pending units: Gmail threads and notes-doc meetings, newest first."""
    require_token(x_ingest_token)
    if settings.anthropic_api_key is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    workspace_header = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    )
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), default_headers=workspace_header
    )

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        system_prompt, company_ids = _context(conn, workspace_id)
        threads = [
            (r["source"], r["thread_id"])
            for r in conn.execute(
                "select source, thread_id from source_records where workspace_id = %s and status = 'pending'"
                " group by source, thread_id order by max(occurred_at) desc nulls last limit %s",
                (workspace_id, settings.classify_batch_threads),
            )
        ]
        run_id = conn.execute(
            "insert into sync_runs (workspace_id, source, rows_received) values (%s, 'classify', %s) returning id",
            (workspace_id, len(threads)),
        ).fetchone()["id"]
        conn.commit()

        counts = {"classified": 0, "commercial": 0, "linked_to_company": 0, "failed": 0}
        try:
            for source, thread_id in threads:
                records = conn.execute(
                    "select id, external_id, occurred_at::text, subject, participants, body, meta from source_records"
                    " where workspace_id = %s and source = %s and thread_id = %s",
                    (workspace_id, source, thread_id),
                ).fetchall()
                renderers = {"notes": lambda r: classifier.render_note(r[0]),
                             "calendar": lambda r: classifier.render_event(r[0])}
                thread_text = renderers.get(source, classifier.render_thread)(records)
                try:
                    result = classifier.classify(client, settings.anthropic_model, system_prompt, thread_text, source)
                except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                    # One bad thread doesn't stop the batch; it stays visible as failed.
                    log.warning("classify thread failed: %s", type(exc).__name__)
                    conn.execute(
                        "update source_records set status = 'failed' where workspace_id = %s and thread_id = %s"
                        " and source = %s",
                        (workspace_id, thread_id, source),
                    )
                    conn.commit()
                    counts["failed"] += 1
                    continue

                stored = classifier.check(result, thread_text, {k: v[0] for k, v in company_ids.items()})
                stored["model"] = settings.anthropic_model
                company_id = company_ids[normalize_name(stored["company"])][1] if stored["company"] else None
                discard = stored["category"] in classifier.DISCARD_TEXT_CATEGORIES
                if discard:
                    stored["evidence_quote"] = None  # keep only the label; the text is not sales data
                conn.execute(
                    "update source_records set status = 'classified', classification = %s, company_id = %s,"
                    " body = case when %s then null else body end"
                    " where workspace_id = %s and thread_id = %s and source = %s",
                    (Jsonb(stored), company_id, discard, workspace_id, thread_id, source),
                )
                conn.commit()
                counts["classified"] += 1
                counts["commercial"] += stored["is_commercial"]
                counts["linked_to_company"] += company_id is not None
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("classify run %s failed", run_id)
            raise HTTPException(status_code=500, detail="classify failed")

        remaining = conn.execute(
            "select count(distinct (source, thread_id)) as n from source_records"
            " where workspace_id = %s and status = 'pending'",
            (workspace_id,),
        ).fetchone()["n"]
        conn.execute(
            "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s, rows_skipped = %s"
            " where id = %s",
            (counts["classified"], counts["failed"], run_id),
        )
        conn.commit()

    log.info("classify run %s: %s remaining_threads=%s", run_id, counts, remaining)
    return {"sync_run_id": str(run_id), "threads": len(threads), **counts, "remaining_threads": remaining}


def _context(conn, workspace_id) -> tuple[str, dict[str, tuple[str, str]]]:
    """System prompt plus {normalized name: (display name, company id)} for exact-match checks."""
    ws = conn.execute("select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()
    team = conn.execute(
        "select owner_code, full_name, email from team_members where workspace_id = %s order by owner_code",
        (workspace_id,),
    ).fetchall()
    companies = conn.execute(
        "select c.id, c.name, c.normalized_name, c.country, o.stage, o.owner_code, o.contacts_text"
        " from companies c join opportunities o on o.company_id = c.id"
        " where c.workspace_id = %s and o.removed_at is null order by c.name",
        (workspace_id,),
    ).fetchall()
    prompt = classifier.build_system_prompt(companies, team, ws["internal_domains"])
    return prompt, {c["normalized_name"]: (c["name"], c["id"]) for c in companies}
