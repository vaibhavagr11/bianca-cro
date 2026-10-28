"""Ingest endpoints called by n8n. Thin layer: auth, sync_runs bookkeeping, DB writes. Logic lives in pipeline.py."""

import hashlib
import hmac
import logging
from datetime import date

import psycopg
from fastapi import APIRouter, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app import db, gmail, google_calendar, notes
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

log = logging.getLogger("bianca_cro.ingest")
router = APIRouter(prefix="/ingest")


class PipelinePayload(BaseModel):
    rows: list[dict]


class GmailPayload(BaseModel):
    messages: list[dict]
    mailbox: str = "RG"  # whose inbox this batch came from (team owner code)


class NotesPayload(BaseModel):
    documentId: str
    content: str


class CalendarPayload(BaseModel):
    events: list[dict]


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


def store_gmail(conn: psycopg.Connection, workspace_id, kept: list, mailbox: str) -> tuple[int, int, int]:
    """Store one inbox's messages. (created, updated, duplicates_from_another_inbox).

    Gmail's message id and thread id differ per inbox. The same email already stored from another inbox is matched
    on its Message-ID (or, for older records without one, on sender + sent time + subject) and not stored twice;
    this inbox's thread is then linked to the stored thread, so later replies join the same conversation.
    """
    created = updated = duplicates = 0
    for m in sorted(kept, key=lambda m: m.occurred_at or ""):
        alias = conn.execute(
            "select canonical_thread_id from gmail_thread_aliases where workspace_id = %s and mailbox = %s"
            " and thread_id = %s", (workspace_id, mailbox, m.thread_id)).fetchone()
        thread = alias[0] if alias else m.thread_id
        mine = conn.execute("select 1 from source_records where workspace_id = %s and source = 'gmail'"
                            " and external_id = %s", (workspace_id, m.external_id)).fetchone()
        if mine is None:
            sender = ((m.participants.get("from") or [{}])[0].get("address") or "").lower()
            other = conn.execute(
                "select id, thread_id from source_records where workspace_id = %s and source = 'gmail'"
                " and external_id <> %s and ((%s::text is not null and meta->>'message_id' = %s)"
                "  or (occurred_at = %s::timestamptz and lower(participants->'from'->0->>'address') = %s"
                "      and coalesce(subject, '') = coalesce(%s, '')))"
                " order by created_at limit 1",
                (workspace_id, m.external_id, m.message_id, m.message_id, m.occurred_at, sender, m.subject),
            ).fetchone()
            if other is not None:
                conn.execute("update source_records set meta = jsonb_set(meta, '{mailboxes}',"
                             " (coalesce(meta->'mailboxes', '[\"RG\"]'::jsonb) || to_jsonb(%s::text))) where id = %s"
                             " and not coalesce(meta->'mailboxes', '[\"RG\"]'::jsonb) ? %s", (mailbox, other[0], mailbox))
                if other[1] and m.thread_id and other[1] != thread:
                    link_thread(conn, workspace_id, mailbox, m.thread_id, other[1])
                duplicates += 1
                continue
        meta = {"mailboxes": [mailbox], **({"message_id": m.message_id} if m.message_id else {})}
        row = conn.execute(
            "insert into source_records (workspace_id, source, external_id, thread_id, occurred_at,"
            " subject, participants, body, content_hash, meta)"
            " values (%s, 'gmail', %s, %s, %s, %s, %s, %s, %s, %s)"
            " on conflict (workspace_id, source, external_id) do update set"
            "  thread_id = excluded.thread_id, occurred_at = excluded.occurred_at,"
            "  subject = excluded.subject, participants = excluded.participants, body = excluded.body,"
            "  content_hash = excluded.content_hash, meta = source_records.meta || excluded.meta,"
            "  status = 'pending', classification = null"
            " where source_records.content_hash <> excluded.content_hash"
            " returning (xmax = 0) as inserted",
            (workspace_id, m.external_id, thread, m.occurred_at, m.subject,
             Jsonb(m.participants), m.body, m.content_hash, Jsonb(meta)),
        ).fetchone()
        if row is None:
            continue  # unchanged
        if row[0]:
            created += 1
        else:
            updated += 1
    return created, updated, duplicates


def link_thread(conn: psycopg.Connection, workspace_id, mailbox: str, inbox_thread: str, canonical: str) -> None:
    """Point this inbox's thread at the stored conversation, and move anything already filed under it."""
    conn.execute("insert into gmail_thread_aliases (workspace_id, mailbox, thread_id, canonical_thread_id)"
                 " values (%s, %s, %s, %s) on conflict do nothing", (workspace_id, mailbox, inbox_thread, canonical))
    conn.execute("update source_records set thread_id = %s where workspace_id = %s and source = 'gmail'"
                 " and thread_id = %s and coalesce(meta->'mailboxes', '[\"RG\"]'::jsonb) ? %s",
                 (canonical, workspace_id, inbox_thread, mailbox))
    conn.execute("update insights set unit_id = %s where workspace_id = %s and source = 'gmail' and unit_id = %s",
                 (canonical, workspace_id, inbox_thread))


@router.post("/gmail")
def ingest_gmail(payload: GmailPayload, x_ingest_token: str | None = Header(default=None)) -> dict:
    """Store filtered, cleaned Gmail messages as pending source_records. Re-sends are idempotent."""
    require_token(x_ingest_token)

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        internal = conn.execute("select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()[0]
        internal_domains = {d.lower() for d in internal}
        project_domains = {d.lower() for d in settings.project_team_domains}
        run_id = conn.execute(
            "insert into sync_runs (workspace_id, source, rows_received) values (%s, 'gmail', %s) returning id",
            (workspace_id, len(payload.messages)),
        ).fetchone()[0]
        conn.commit()

        try:
            kept, skipped = gmail.process(
                payload.messages, internal_domains, project_domains, set(settings.excluded_label_ids)
            )
            with conn.transaction():
                created, updated, duplicates = store_gmail(conn, workspace_id, kept, payload.mailbox.upper())
                if duplicates:
                    skipped["already_stored_from_other_inbox"] = duplicates
                counts = {
                    "created": created,
                    "updated": updated,
                    "unchanged": len(kept) - created - updated - duplicates,
                    "skipped": sum(skipped.values()),
                }
                conn.execute(
                    "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s,"
                    " rows_updated = %s, rows_unchanged = %s, rows_skipped = %s where id = %s",
                    (counts["created"], counts["updated"], counts["unchanged"], counts["skipped"], run_id),
                )
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("gmail sync %s failed", run_id)
            raise HTTPException(status_code=500, detail="gmail sync failed")

    log.info("gmail sync %s succeeded: received=%s %s skipped_by_reason=%s", run_id, len(payload.messages), counts, skipped)
    return {"sync_run_id": str(run_id), "received": len(payload.messages), **counts, "skipped_by_reason": skipped}


@router.post("/notes")
def ingest_notes(payload: NotesPayload, x_ingest_token: str | None = Header(default=None)) -> dict:
    """Split the meeting-notes doc into meetings and store those within the window as pending source_records."""
    require_token(x_ingest_token)
    today = date.today()
    sections = notes.split_sections(payload.content, today)
    recent = notes.recent_sections(sections, today, settings.notes_window_days)

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        run_id = conn.execute(
            "insert into sync_runs (workspace_id, source, rows_received) values (%s, 'notes', %s) returning id",
            (workspace_id, len(sections)),
        ).fetchone()[0]
        conn.commit()

        try:
            created = updated = 0
            with conn.transaction():
                for position, s in enumerate(recent):
                    body = notes.clean_body(s)
                    external_id = f"{payload.documentId}:{s.section_id}"
                    content_hash = hashlib.sha256(body.encode()).hexdigest()
                    meta = {"doc_id": payload.documentId, "date_inferred": s.date_inferred, "position": position}
                    row = conn.execute(
                        "insert into source_records (workspace_id, source, external_id, thread_id, occurred_at,"
                        " subject, body, content_hash, meta) values (%s, 'notes', %s, %s, %s, %s, %s, %s, %s)"
                        " on conflict (workspace_id, source, external_id) do update set"
                        "  occurred_at = excluded.occurred_at, subject = excluded.subject, body = excluded.body,"
                        "  content_hash = excluded.content_hash, meta = excluded.meta,"
                        "  status = 'pending', classification = null"
                        " where source_records.content_hash <> excluded.content_hash"
                        " returning (xmax = 0) as inserted",
                        (workspace_id, external_id, external_id, s.meeting_date, notes.redact_secrets(s.header)[:200],
                         body, content_hash, Jsonb(meta)),
                    ).fetchone()
                    if row is None:
                        continue
                    if row[0]:
                        created += 1
                    else:
                        updated += 1
                counts = {
                    "created": created,
                    "updated": updated,
                    "unchanged": len(recent) - created - updated,
                    "skipped": len(sections) - len(recent),
                }
                conn.execute(
                    "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s,"
                    " rows_updated = %s, rows_unchanged = %s, rows_skipped = %s where id = %s",
                    (counts["created"], counts["updated"], counts["unchanged"], counts["skipped"], run_id),
                )
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("notes sync %s failed", run_id)
            raise HTTPException(status_code=500, detail="notes sync failed")

    log.info("notes sync %s succeeded: sections=%s %s", run_id, len(sections), counts)
    return {"sync_run_id": str(run_id), "sections_in_doc": len(sections), "in_window": len(recent),
            "window_days": settings.notes_window_days, **counts}


@router.post("/calendar")
def ingest_calendar(payload: CalendarPayload, x_ingest_token: str | None = Header(default=None)) -> dict:
    """Store customer meetings from Google Calendar as pending source_records; apply cancellations."""
    require_token(x_ingest_token)

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        internal = conn.execute("select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()[0]
        internal_domains = {d.lower() for d in internal}
        project_domains = {d.lower() for d in settings.project_team_domains}
        run_id = conn.execute(
            "insert into sync_runs (workspace_id, source, rows_received) values (%s, 'calendar', %s) returning id",
            (workspace_id, len(payload.events)),
        ).fetchone()[0]
        conn.commit()

        try:
            kept, cancelled_ids, skipped = google_calendar.process(payload.events, internal_domains, project_domains)
            created = updated = 0
            with conn.transaction():
                for e in kept:
                    row = conn.execute(
                        "insert into source_records (workspace_id, source, external_id, thread_id, occurred_at,"
                        " subject, participants, body, content_hash, meta)"
                        " values (%s, 'calendar', %s, %s, %s, %s, %s, %s, %s, %s)"
                        " on conflict (workspace_id, source, external_id) do update set"
                        "  occurred_at = excluded.occurred_at, subject = excluded.subject,"
                        "  participants = excluded.participants, body = excluded.body,"
                        "  content_hash = excluded.content_hash, meta = excluded.meta,"
                        "  status = 'pending', classification = null"
                        " where source_records.content_hash <> excluded.content_hash"
                        " returning (xmax = 0) as inserted",
                        (workspace_id, e.external_id, e.external_id, e.starts_at, e.subject,
                         Jsonb(e.participants), e.body, e.content_hash, Jsonb(e.meta)),
                    ).fetchone()
                    if row is None:
                        continue
                    if row[0]:
                        created += 1
                    else:
                        updated += 1
                # Cancellations only touch meetings we already have; the classification stays.
                cancelled = conn.execute(
                    "update source_records set meta = meta || '{\"status\": \"cancelled\"}'::jsonb"
                    " where workspace_id = %s and source = 'calendar' and external_id = any(%s)"
                    " and meta->>'status' is distinct from 'cancelled'",
                    (workspace_id, cancelled_ids),
                ).rowcount
                counts = {
                    "created": created,
                    "updated": updated,
                    "unchanged": len(kept) - created - updated,
                    "cancelled": cancelled,
                    "skipped": sum(skipped.values()),
                }
                conn.execute(
                    "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s,"
                    " rows_updated = %s, rows_unchanged = %s, rows_skipped = %s where id = %s",
                    (counts["created"], counts["updated"], counts["unchanged"], counts["skipped"], run_id),
                )
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("calendar sync %s failed", run_id)
            raise HTTPException(status_code=500, detail="calendar sync failed")

    log.info("calendar sync %s succeeded: received=%s %s skipped_by_reason=%s",
             run_id, len(payload.events), counts, skipped)
    return {"sync_run_id": str(run_id), "received": len(payload.events), **counts, "skipped_by_reason": skipped}


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
