"""Persistent founder review queue and recoverable, explicit Sheet approvals.

The queue consumes stable source-content identities, not brief dates or insight UUIDs.
No generated suggestion writes to Google until an authenticated founder approves it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation

from app import db
from app.config import settings
from app.pipeline import PipelineRow, TRACKED_FIELDS, is_working_client, normalize_name
from app.review_sheets import SheetsClient, SheetConflict, SheetError, SheetWorkingClient, SheetWriteUncertain
from app.review_content import Content, approved_lineage, proposal_content, source_version, validate_lineage


class ReviewConflict(ValueError):
    """A reviewed version changed, or another decision already owns the update."""


class ReviewNotFound(ValueError):
    """The requested proposal does not belong to this workspace."""


class ReviewUnavailable(RuntimeError):
    """The service cannot confirm a write. Pending intent remains available for recovery."""


SOURCE_NAMES = {"gmail": "Email", "notes": "Meeting notes", "calendar": "Calendar"}
CONTEXT_NAMES = {"need": "Customer need", "buying_signal": "Buying signal", "objection": "Objection",
                 "blocker": "Blocker"}
_BULLET = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+)")
MAX_CELL_CHARS = 45_000
ROUTING_VERSION = 1
ACTION_TYPES = {"commitment", "next_step", "blocker"}
CONTEXT_TYPES = {"need", "buying_signal", "objection"}
OUTSIDE_SALES_PIPELINE = "This update is outside the sales pipeline."


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _stamp(value: Any) -> str:
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value or "")


def _squash(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def _same_cells(a: dict, b: dict) -> bool:
    # Pipeline ingestion strips outer whitespace. Compare its view without discarding the live Sheet's bytes.
    return all(_cell(a.get(k)).strip() == _cell(b.get(k)).strip() for k in ("lastProgress", "nextStep", "clientContext"))


def _cells(last_progress: Any, next_step: Any, client_context: Any = "") -> dict:
    return {"lastProgress": _cell(last_progress), "nextStep": _cell(next_step),
            "clientContext": _cell(client_context)}


def clean_bullets(bullets: list[str], *, allow_empty: bool = False) -> list[str]:
    if not isinstance(bullets, list) or (not bullets and not allow_empty) or len(bullets) > 200:
        raise ValueError("Provide between 1 and 200 nonempty bullets.")
    clean = []
    for bullet in bullets:
        if not isinstance(bullet, str) or not bullet.strip():
            raise ValueError("Every bullet must contain text.")
        if "\n" in bullet or "\r" in bullet:
            raise ValueError("Use one bullet per line.")
        text = _BULLET.sub("", bullet.strip()).strip()
        if not text or "\x00" in text or len(text) > 4_000:
            raise ValueError("Every bullet must contain valid text.")
        clean.append(text)
    if len("\n".join("- " + x for x in clean)) > MAX_CELL_CHARS:
        raise ValueError("The proposed update is too long for one Sheet cell. Shorten the bullets.")
    return clean


def _source_key(version: dict) -> tuple:
    return version["source"], version["external_id"], version["version"]


def _instant(value: Any) -> datetime | None:
    """Source timestamps are UTC instants; an unknown initial date cannot prove recency."""
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _in_bootstrap_window(record: dict, state: dict) -> bool:
    occurred = _instant(record.get("occurred_at"))
    return occurred is not None and state["bootstrap_cutoff"] <= occurred <= state["initialized_at"]


def _cycle(conn, workspace_id, *, force: bool) -> tuple[dict, bool]:
    """Persist one initial source boundary and one resumable cycle per workspace day."""
    clock = conn.execute("select now() as now, timezone from workspaces where id = %s", (workspace_id,)).fetchone()
    if not clock:
        raise ReviewUnavailable("The review workspace is unavailable.")
    now = _instant(clock["now"])
    today = now.astimezone(ZoneInfo(clock["timezone"])).date()
    state = conn.execute("select * from review_queue_state where workspace_id = %s for update", (workspace_id,)).fetchone()
    if state is None:
        state = conn.execute(
            "insert into review_queue_state (workspace_id, initialized_at, bootstrap_cutoff, cycle_for_date, cycle_started_at)"
            " values (%s, %s, %s, %s, %s) returning *",
            (workspace_id, now, now - timedelta(days=14), today, now)).fetchone()
        # Snapshot every existing source version, even if unclassified or unlinked.
        # These exclusions are not approvals and never consume a future changed version.
        records = conn.execute(
            "select source, external_id, content_hash, occurred_at, subject, meta, created_at"
            " from source_records where workspace_id = %s and created_at <= %s", (workspace_id, now)).fetchall()
        excluded = []
        for record in records:
            if not _in_bootstrap_window(record, state):
                version = source_version(record)
                excluded.append({**version, "reason": "undated" if _instant(record.get("occurred_at")) is None
                                 else "outside_initial_window"})
        if excluded:
            conn.execute(
                "insert into review_bootstrap_exclusions (workspace_id, source, external_id, version, reason)"
                " select %s, item.source, item.external_id, item.version, item.reason"
                " from jsonb_to_recordset(%s) as item(source text, external_id text, version text, reason text)"
                " on conflict do nothing", (workspace_id, Jsonb(excluded)))
        return state, False
    if state["cycle_completed_at"] is None:
        return state, False  # Batch continuations must drain even across midnight.
    if not force and state["last_completed_on"] is not None and state["last_completed_on"] >= today:
        return state, True
    state = conn.execute(
        "update review_queue_state set cycle_for_date = %s, cycle_started_at = %s, cycle_completed_at = null"
        " where workspace_id = %s returning *", (today, now, workspace_id)).fetchone()
    return state, False


def _cycle_info(state: dict) -> dict:
    return {"forDate": state["cycle_for_date"].isoformat(),
            "bootstrap": {"initializedAt": _stamp(state["initialized_at"]),
                          "cutoff": _stamp(state["bootstrap_cutoff"]), "days": 14,
                          "completed": bool(state["bootstrap_completed"])}}


def _proposal_data(row: dict) -> dict:
    return {"id": str(row["id"]), "company": row["company_name"], "owner": row.get("owner_code") or "",
            "stage": row.get("stage") or "", "country": row.get("country") or "",
            "nextStepBullets": row.get("next_step_bullets") or [],
            "clientContextBullets": row.get("client_context_bullets") or [], "sources": row["sources"],
            "routingVersion": row.get("routing_version", 0),
            "lineageVersion": row.get("lineage_version", 0),
            "bulletLineage": row.get("bullet_lineage") or {"nextStep": [], "clientContext": []},
            "lastProgress": row["snapshot_last_progress"], "nextStep": row["snapshot_next_step"],
            "clientContext": _cell(row.get("snapshot_client_context")),
            "revision": row["revision"], "status": row["status"]}


def _current_content(conn, workspace_id, deal: dict, records: list[dict]) -> Content:
    insights = conn.execute(
        "select * from insights where workspace_id = %s and company_id = %s and opportunity_id = %s"
        " and source_record_ids && %s::uuid[] order by id",
        (workspace_id, deal["company_id"], deal["opportunity_id"], [r["id"] for r in records]),
    ).fetchall() if records else []
    return proposal_content(records, insights)


def _content_versions(sources: list[dict]) -> list[dict]:
    return sorted(({"source": s["source"], "external_id": s["externalId"], "version": s["version"]}
                   for s in sources), key=_source_key)


def build_proposals(conn, workspace_id, *, force: bool = False) -> dict:
    """Resume today's bounded build; only the initial import has a 14-day window."""
    conn.row_factory = dict_row
    conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"review-build:{workspace_id}",))
    state, skip = _cycle(conn, workspace_id, force=force)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "applying": 0, "remaining": 0}
    if skip:
        return {**counts, "done": True, "skipped": True, **_cycle_info(state)}
    deals = conn.execute(
        "select o.id as opportunity_id, o.company_id, c.name as company_name, o.owner_code, o.stage, c.country,"
        " o.last_progress, o.next_step, o.client_context from opportunities o join companies c on c.id = o.company_id"
        " where o.workspace_id = %s and o.removed_at is null order by c.name", (workspace_id,)).fetchall()
    receipts = {_source_key(r) + (str(r["company_id"]),) for r in conn.execute(
        "select company_id, source, external_id, version from review_source_receipts where workspace_id = %s", (workspace_id,))}
    exclusions = {_source_key(r) for r in conn.execute(
        "select source, external_id, version from review_bootstrap_exclusions where workspace_id = %s", (workspace_id,))}
    limit = max(1, settings.review_build_batch)
    for deal in deals:
        existing = conn.execute(
            "select * from review_proposals where workspace_id = %s and company_id = %s"
            " and status in ('pending', 'applying') for update", (workspace_id, deal["company_id"])).fetchone()
        if is_working_client(deal["stage"]):
            if existing and existing["status"] == "pending":
                if counts["created"] + counts["updated"] >= limit:
                    counts["remaining"] += 1
                    continue
                _supersede_pending(conn, existing["id"])
                counts["updated"] += 1
            continue
        if existing and existing["status"] == "applying":
            counts["applying"] += 1
            continue
        records = conn.execute(
            "select * from source_records where workspace_id = %s and company_id = %s and status = 'classified'"
            " and classification->>'is_commercial' = 'true' and created_at <= %s"
            " order by occurred_at nulls last, source, external_id",
            (workspace_id, deal["company_id"], state["cycle_started_at"])).fetchall()
        records = [r for r in records
                   if _source_key(source_version(r)) not in exclusions
                   and _source_key(source_version(r)) + (str(deal["company_id"]),) not in receipts
                   and (state["bootstrap_completed"] or _in_bootstrap_window(r, state))]
        # Re-extraction can change an insight's scope without changing its source version.
        next_steps, context, sources, lineage = _current_content(conn, workspace_id, deal, records)
        if not existing and not (next_steps or context):
            counts["unchanged"] += 1
            continue
        versions = _content_versions(sources)
        baseline = _cells(deal["last_progress"], deal["next_step"], deal.get("client_context"))
        data = {"company_name": deal["company_name"], "owner_code": deal["owner_code"], "stage": deal["stage"],
                "country": deal["country"], "bullets": next_steps + context, "sources": sources, "source_versions": versions,
                "next_step_bullets": next_steps, "client_context_bullets": context, "routing_version": ROUTING_VERSION,
                "snapshot_last_progress": baseline["lastProgress"], "snapshot_next_step": baseline["nextStep"],
                "snapshot_client_context": baseline["clientContext"], "observed_source_versions": versions,
                "bullet_lineage": lineage, "lineage_version": 1}
        if existing and (next_steps or context) and all(existing.get(k) == value for k, value in data.items()):
            counts["unchanged"] += 1
            continue
        if counts["created"] + counts["updated"] >= limit:
            counts["remaining"] += 1
            continue
        if not (next_steps or context):
            _supersede_pending(conn, existing["id"])
            counts["updated"] += 1
            continue
        if existing:
            conn.execute(
                "update review_proposals set company_name = %s, owner_code = %s, stage = %s, country = %s,"
                " bullets = %s, sources = %s, source_versions = %s, snapshot_last_progress = %s,"
                " snapshot_next_step = %s, snapshot_client_context = %s, next_step_bullets = %s,"
                " client_context_bullets = %s, routing_version = %s, observed_source_versions = %s,"
                " bullet_lineage = %s, lineage_version = %s, revision = revision + 1 where id = %s",
                (data["company_name"], data["owner_code"], data["stage"], data["country"], Jsonb(next_steps + context), Jsonb(sources),
                 Jsonb(versions), baseline["lastProgress"], baseline["nextStep"], baseline["clientContext"],
                 Jsonb(next_steps), Jsonb(context), ROUTING_VERSION, Jsonb(versions), Jsonb(lineage), 1, existing["id"]))
            counts["updated"] += 1
        else:
            conn.execute(
                "insert into review_proposals (workspace_id, company_id, opportunity_id, company_name, owner_code,"
                " stage, country, bullets, sources, source_versions, snapshot_last_progress, snapshot_next_step,"
                " snapshot_client_context, next_step_bullets, client_context_bullets, routing_version,"
                " observed_source_versions, bullet_lineage, lineage_version)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (workspace_id, deal["company_id"], deal["opportunity_id"], data["company_name"], data["owner_code"],
                 data["stage"], data["country"], Jsonb(next_steps + context), Jsonb(sources), Jsonb(versions),
                 baseline["lastProgress"], baseline["nextStep"], baseline["clientContext"],
                 Jsonb(next_steps), Jsonb(context), ROUTING_VERSION, Jsonb(versions), Jsonb(lineage), 1))
            counts["created"] += 1
    done = counts["remaining"] == 0
    if done:
        state = conn.execute(
            "update review_queue_state set cycle_completed_at = now(), last_completed_on = cycle_for_date,"
            " bootstrap_completed = true where workspace_id = %s returning *", (workspace_id,)).fetchone()
    return {**counts, "done": done, "skipped": False, **_cycle_info(state)}


def _supersede_pending(conn, proposal_id) -> None:
    conn.execute("update review_proposals set status = 'superseded', revision = revision + 1"
                 " where id = %s and status = 'pending'", (proposal_id,))


def _activity(row: dict, company: str) -> dict:
    return {"id": str(row["id"]), "proposalId": str(row["proposal_id"]), "company": company,
            "kind": "approved" if row["action"] == "approve" else "ignored", "edited": row["edited"],
            "actor": row["actor"], "at": _stamp(row.get("completed_at") or row["created_at"]),
            "before": row["before_cells"], "after": row["after_cells"],
            "mirror_refreshed": row.get("mirror_refreshed"),
            "lineageVersion": row.get("lineage_version", 0),
            "bulletLineage": row.get("bullet_lineage") or {"nextStep": [], "clientContext": []}}


def workspace_view(conn, workspace_id) -> dict:
    conn.row_factory = dict_row
    try:
        statuses = SheetsClient().read_company_statuses()
    except SheetError as exc:
        raise ReviewUnavailable("Current sales status could not be confirmed from the Sheet. Refresh to try again.") from exc
    live_sales = {name for name, stage in statuses.items() if stage is not None and not is_working_client(stage)}
    eligible = [r for r in conn.execute(
        "select o.*, c.name, c.normalized_name, (select count(*) from review_proposals p where p.workspace_id = o.workspace_id"
        " and p.opportunity_id = o.id and p.status = 'approved') as approved from opportunities o"
        " join companies c on c.id = o.company_id where o.workspace_id = %s"
        " and (c.normalized_name = any(%s) or (o.removed_at is not null and c.normalized_name <> all(%s)))"
        " order by c.name", (workspace_id, sorted(live_sales), sorted(statuses))) if not is_working_client(r["stage"])]
    eligible_ids = [r["id"] for r in eligible if r["normalized_name"] in live_sales]
    historical_ids = [r["id"] for r in eligible]
    proposals = []
    for row in conn.execute(
            "select p.* from review_proposals p join opportunities o on o.id = p.opportunity_id"
            " where p.workspace_id = %s and p.opportunity_id = any(%s) and p.status in ('pending', 'applying')"
            " and (o.removed_at is null or p.status = 'applying') order by p.company_name", (workspace_id, eligible_ids)):
        item = _proposal_data(row)
        if row["status"] == "applying":
            intent = conn.execute(
                "select idempotency_key, revision, next_step_bullets, client_context_bullets, routing_version,"
                " sheet_target, error, bullet_lineage, lineage_version from review_decisions where workspace_id = %s"
                " and proposal_id = %s and status = 'applying'", (workspace_id, row["id"])).fetchone()
            if intent and row.get("routing_version") == ROUTING_VERSION and intent.get("routing_version") == ROUTING_VERSION:
                item["nextStepBullets"] = intent["next_step_bullets"]
                item["clientContextBullets"] = intent["client_context_bullets"]
                item["bulletLineage"] = intent.get("bullet_lineage") or {"nextStep": [], "clientContext": []}
                item["lineageVersion"] = intent.get("lineage_version", 0)
                if _target_is_pinned(intent.get("sheet_target")) and intent["sheet_target"] == _sheet_target():
                    item["retry"] = {"idempotency_key": intent["idempotency_key"], "revision": intent["revision"],
                                     "next_step_bullets": intent["next_step_bullets"],
                                     "client_context_bullets": intent["client_context_bullets"]}
                    item["error"] = intent.get("error") or "Sheet update awaiting confirmation. Retry the same approval."
                else:
                    item["error"] = "The saved approval targets a different Sheet configuration. An operator must reconcile it before retrying."
            else:
                item["error"] = "This approval predates column routing. An operator must reconcile its original Sheet write before it can be reviewed again."
        elif row.get("routing_version") != ROUTING_VERSION:
            item["error"] = "This proposal predates column routing. Rebuild the review queue before deciding."
        if row["status"] == "pending" and row.get("lineage_version") != 1:
            item["error"] = "This proposal predates source lineage. Rebuild the review queue before deciding."
        proposals.append(item)
    rows = [{"id": str(r["id"]), "company": r["name"], "owner": r["owner_code"] or "", "stage": r["stage"] or "",
             "lastProgress": _cell(r["last_progress"]), "nextStep": _cell(r["next_step"]),
             "clientContext": _cell(r.get("client_context")), "revision": r["approved"],
             "lastSyncedAt": _stamp(r.get("last_seen_at"))} for r in eligible if r["removed_at"] is None]
    activity = [_activity(r, r["company_name"]) for r in conn.execute(
        "select d.*, p.company_name from review_decisions d join review_proposals p on p.id = d.proposal_id"
        " where d.workspace_id = %s and p.opportunity_id = any(%s) and d.status = 'succeeded'"
        " order by d.completed_at desc limit 200", (workspace_id, historical_ids))]
    target = _sheet_target()
    sheet_url = None
    if _target_is_pinned(target):
        sheet_url = (f"https://docs.google.com/spreadsheets/d/{target['spreadsheet_id']}/edit"
                     f"?gid={target['sheet_id']}#gid={target['sheet_id']}")
    return {"proposals": proposals, "rows": rows, "activity": activity, "sheetUrl": sheet_url,
            "routingVersion": ROUTING_VERSION, "clientContextMode": settings.review_client_context_mode,
            "owners": [{"code": "RG", "name": "Ramiro"}, {"code": "NC", "name": "Nico"}, {"code": "NDL", "name": "Nico"}]}


def _sheet_target() -> dict:
    return {"spreadsheet_id": settings.review_spreadsheet_id, "sheet_tab": settings.review_sheet_tab,
            "sheet_id": settings.review_sheet_id}


def _target_is_pinned(target: dict | None) -> bool:
    return (isinstance(target, dict) and isinstance(target.get("spreadsheet_id"), str)
            and bool(target["spreadsheet_id"]) and isinstance(target.get("sheet_tab"), str)
            and bool(target["sheet_tab"]) and type(target.get("sheet_id")) is int and target["sheet_id"] >= 0)


def _request_hash(proposal_id: str, action: str, revision: int, next_steps: list[str], context: list[str]) -> str:
    return _hash({"proposal_id": str(proposal_id), "action": action, "revision": revision,
                  "next_step_bullets": next_steps, "client_context_bullets": context,
                  "routing_version": ROUTING_VERSION})


def _append_context(existing: str, bullets: list[str]) -> str:
    """Preserve existing bytes and append only genuinely new normalized bullet lines."""
    seen = {_squash(_BULLET.sub("", line.strip())) for line in existing.splitlines() if line.strip()}
    additions = []
    for bullet in bullets:
        key = _squash(bullet)
        if key not in seen:
            seen.add(key)
            additions.append("- " + bullet)
    if not additions:
        return existing
    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    return existing + separator + "\n".join(additions)


def _consume_sources(conn, proposal: dict) -> None:
    for version in proposal["source_versions"]:
        conn.execute(
            "insert into review_source_receipts (workspace_id, company_id, source, external_id, version, proposal_id)"
            " values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
            (proposal["workspace_id"], proposal["company_id"], version["source"], version["external_id"],
             version["version"], proposal["id"]))


def _mirror_approval(conn, proposal: dict, before: dict, after: dict) -> bool:
    """Refresh the pipeline cache only if a newer source sync has not replaced the reviewed cells."""
    row = conn.execute(
        "select o.*, c.name, c.country from opportunities o join companies c on c.id = o.company_id"
        " where o.workspace_id = %s and o.id = %s for update of o",
        (proposal["workspace_id"], proposal["opportunity_id"])).fetchone()
    if not row or row["removed_at"] is not None:
        return False
    current = _cells(row["last_progress"], row["next_step"], row.get("client_context"))
    if not (_same_cells(current, before) or _same_cells(current, after)):
        return False
    # Match the ingest parser's canonical whitespace/null handling so the next full sync stays idempotent.
    fields = {k: (str(row[k]).strip() or None) if row.get(k) is not None else None for k in TRACKED_FIELDS}
    fields["last_progress"] = after["lastProgress"].strip() or None
    fields["next_step"] = after["nextStep"].strip() or None
    fields["client_context"] = after["clientContext"].strip() or None
    pipeline_row = PipelineRow(row["name"], normalize_name(row["name"]), fields)
    conn.execute("update opportunities set last_progress = %s, next_step = %s, client_context = %s, row_hash = %s where id = %s",
                 (fields["last_progress"], fields["next_step"], fields["client_context"], pipeline_row.row_hash, row["id"]))
    changes = [(field, row.get(field), fields[field]) for field in ("last_progress", "next_step", "client_context") if row.get(field) != fields[field]]
    if changes:
        run = conn.execute(
            "insert into sync_runs (workspace_id, source, status, rows_received, rows_updated, finished_at)"
            " values (%s, 'sheet_review', 'succeeded', 1, 1, now()) returning id", (proposal["workspace_id"],)).fetchone()
        for field, old, new in changes:
            conn.execute(
                "insert into opportunity_changes (workspace_id, opportunity_id, sync_run_id, field, old_value, new_value)"
                " values (%s, %s, %s, %s, %s, %s)",
                (proposal["workspace_id"], proposal["opportunity_id"], run["id"], field, old, new))
    return True


def _result(intent: dict, proposal: dict, mirror_refreshed: bool | None = None) -> dict:
    data = {"proposalId": str(proposal["id"]), "company": proposal["company_name"],
            "status": "approved" if intent["action"] == "approve" else "ignored",
            "revision": proposal["revision"], "activity": _activity(intent, proposal["company_name"]),
            "idempotency_key": intent["idempotency_key"]}
    refreshed = intent.get("mirror_refreshed", mirror_refreshed)
    if refreshed is not None:
        data["mirror_refreshed"] = refreshed
    return data


def _require_current_content(conn, workspace_id, proposal: dict) -> None:
    # Extraction locks source rows before changing their insights. Keep the displayed evidence stable until
    # the new decision is durable. Revalidate the original card even when the founder supplies edited text.
    by_version = {_source_key(source_version(r)): r for r in conn.execute(
        "select * from source_records where workspace_id = %s and company_id = %s and status = 'classified'"
        " and classification->>'is_commercial' = 'true' order by id for share", (workspace_id, proposal["company_id"]))}
    expected = [_source_key(v) for v in proposal.get("observed_source_versions", [])]
    if any(key not in by_version for key in expected):
        raise ReviewConflict("The source material changed since this card was generated. Refresh the review queue before deciding.")
    next_steps, context, sources, lineage = _current_content(conn, workspace_id, proposal,
                                                            [by_version[key] for key in expected])
    if (not (next_steps or context) or next_steps != proposal["next_step_bullets"]
            or context != proposal["client_context_bullets"] or lineage != proposal["bullet_lineage"]
            or _content_versions(sources) != proposal["source_versions"]):
        raise ReviewConflict("The sales evidence changed since this card was generated. Refresh the review queue before deciding.")


def _require_live_sales_status(sheets: SheetsClient, company: str) -> None:
    status = sheets.read_company_statuses().get(normalize_name(company))
    if status is None or is_working_client(status):
        raise ReviewConflict(OUTSIDE_SALES_PIPELINE)


def _prepare(conn, workspace_id, proposal_id: str, actor: str, action: str, revision: int,
             idempotency_key: str, next_step_bullets: list[str] | None,
             client_context_bullets: list[str] | None, sheets: SheetsClient) -> tuple[dict, dict, bool]:
    proposal = conn.execute(
        "select p.*, o.removed_at, o.stage as current_stage, c.name as current_company_name"
        " from review_proposals p join opportunities o on o.id = p.opportunity_id join companies c on c.id = o.company_id"
        " where p.workspace_id = %s and p.id::text = %s for update of p", (workspace_id, proposal_id)).fetchone()
    if not proposal:
        raise ReviewNotFound("This company update was not found.")
    excluded = is_working_client(proposal["current_stage"])
    if excluded and proposal["status"] != "applying":
        raise ReviewConflict(OUTSIDE_SALES_PIPELINE)
    if proposal.get("routing_version") != ROUTING_VERSION:
        raise ReviewConflict("This proposal predates column routing. Rebuild pending proposals; reconcile legacy applying approvals before retrying.")
    next_steps: list[str] = []
    context: list[str] = []
    if action == "approve":
        next_steps = clean_bullets(proposal["next_step_bullets"] if next_step_bullets is None else next_step_bullets,
                                   allow_empty=True)
        context = clean_bullets(proposal["client_context_bullets"] if client_context_bullets is None else client_context_bullets,
                               allow_empty=True)
        if not next_steps and not context:
            raise ValueError("Provide at least one Next Step or Client Context bullet.")
    request_hash = _request_hash(proposal_id, action, revision, next_steps, context)
    intent = conn.execute(
        "select * from review_decisions where workspace_id = %s and idempotency_key = %s for update",
        (workspace_id, idempotency_key)).fetchone()
    if intent:
        if intent.get("routing_version") != ROUTING_VERSION:
            raise ReviewConflict("This saved approval predates column routing and requires operator reconciliation.")
        if str(intent["proposal_id"]) != str(proposal["id"]) or intent["request_hash"] != request_hash:
            raise ReviewConflict("This approval key belongs to a different decision. Reload the review queue.")
        if intent["status"] == "succeeded":
            _require_live_sales_status(sheets, proposal["current_company_name"])
            return proposal, intent, True
        if intent["status"] != "applying" or proposal["status"] != "applying":
            raise ReviewConflict("This attempt could not be applied. Resync the Pipeline sheet and reload before approving again.")
        if not _target_is_pinned(intent.get("sheet_target")) or intent["sheet_target"] != _sheet_target():
            raise ReviewConflict("The saved approval targets a different Sheet configuration. An operator must reconcile it before retrying.")
        return proposal, intent, False
    if proposal["status"] != "pending":
        raise ReviewConflict("This update is already reviewed or being applied. Reload the review queue.")
    if proposal["revision"] != revision:
        raise ReviewConflict("This company has a newer update. Reload it before deciding.")
    if proposal["removed_at"] is not None:
        raise ReviewConflict("This company is no longer in the active Pipeline sheet.")
    if proposal.get("lineage_version") != 1:
        raise ReviewConflict("This proposal predates source lineage. Rebuild the review queue before deciding.")
    if not validate_lineage(proposal["next_step_bullets"], proposal["client_context_bullets"],
                            proposal["sources"], proposal["bullet_lineage"]):
        raise ReviewConflict("This proposal has invalid source lineage. Rebuild the review queue before deciding.")
    _require_current_content(conn, workspace_id, proposal)
    before = _cells(proposal["snapshot_last_progress"], proposal["snapshot_next_step"], proposal.get("snapshot_client_context"))
    after = dict(before)
    target = None
    if action == "approve":
        target = _sheet_target()
        if not _target_is_pinned(target):
            raise ReviewUnavailable("Pin REVIEW_SPREADSHEET_ID, REVIEW_SHEET_TAB, and a numeric REVIEW_SHEET_ID before approving.")
        live = sheets.read_company(proposal["current_company_name"])
        if is_working_client(live.status):
            raise ReviewConflict(OUTSIDE_SALES_PIPELINE)
        exact_live = _cells(live.last_progress, live.next_step, live.client_context)
        if not _same_cells(before, exact_live):
            raise ReviewConflict("The Sheet row changed since this card was generated. Resync the Pipeline sheet and reload before approving.")
        before, after = exact_live, dict(exact_live)
        if next_steps:
            after.update(lastProgress=before["nextStep"], nextStep="\n".join("- " + b for b in next_steps))
        if context:
            after["clientContext"] = (_append_context(before["clientContext"], context)
                                      if settings.review_client_context_mode == "append"
                                      else "\n".join("- " + b for b in context))
        if context and len(after["clientContext"]) > 50_000:
            raise ValueError("The resulting Client Context exceeds one Sheet cell. Shorten the context or update the existing cell first.")
    else:
        _require_live_sales_status(sheets, proposal["current_company_name"])
    decision_lineage = approved_lineage(proposal, next_steps, context) if action == "approve" else {"nextStep": [], "clientContext": []}
    edited = action == "approve" and (next_steps != proposal["next_step_bullets"] or context != proposal["client_context_bullets"])
    intent = conn.execute(
        "insert into review_decisions (workspace_id, proposal_id, actor, action, revision, idempotency_key, request_hash,"
        " bullets, before_cells, after_cells, edited, status, next_step_bullets, client_context_bullets, routing_version, sheet_target, bullet_lineage, lineage_version)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning *",
        (workspace_id, proposal["id"], actor, action, revision, idempotency_key, request_hash, Jsonb(next_steps + context),
         Jsonb(before), Jsonb(after), edited, "applying" if action == "approve" else "succeeded",
         Jsonb(next_steps), Jsonb(context), ROUTING_VERSION, Jsonb(target) if target is not None else None,
         Jsonb(decision_lineage), 1)).fetchone()
    if action == "ignore":
        intent = conn.execute("update review_decisions set completed_at = now() where id = %s returning *",
                              (intent["id"],)).fetchone()
        conn.execute("update review_proposals set status = 'ignored', decided_by = %s, decided_at = now() where id = %s",
                     (actor, proposal["id"]))
        _consume_sources(conn, proposal)
        return proposal, intent, True
    conn.execute("update review_proposals set status = 'applying' where id = %s", (proposal["id"],))
    return proposal, intent, False


def _record_error(conn, proposal: dict, intent: dict, message: str, *, conflict: bool) -> None:
    if conflict:
        conn.execute("update review_decisions set status = 'conflict', error = %s, completed_at = now() where id = %s",
                     (message, intent["id"]))
        conn.execute("update review_proposals set status = 'pending' where id = %s and status = 'applying'",
                     (proposal["id"],))
    else:
        conn.execute("update review_decisions set error = %s where id = %s", (message, intent["id"]))
    conn.commit()


class _LazySheets:
    """Validate stored decisions before opening the live Sheet."""

    def __init__(self):
        self.client = None

    def _get(self):
        if self.client is None:
            self.client = SheetsClient()
        return self.client

    def read_company(self, *args):
        return self._get().read_company(*args)

    def read_company_statuses(self):
        return self._get().read_company_statuses()

    def apply_update(self, *args, **kwargs):
        return self._get().apply_update(*args, **kwargs)


def decide(workspace_id, proposal_id: str, actor: str, action: str, revision: int, idempotency_key: str,
           next_step_bullets: list[str] | None = None, client_context_bullets: list[str] | None = None) -> dict:
    """An explicit founder decision. Fixed intents and locks make retries recoverable, not a second rotation."""
    if action not in ("approve", "ignore") or actor not in ("RG", "NC", "NDL"):
        raise ValueError("A valid founder and decision are required.")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("A valid proposal revision is required.")
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128 or idempotency_key.strip() != idempotency_key:
        raise ValueError("Provide an approval key between 8 and 128 characters.")
    # A new connection per request releases this session lock even on an exception or process disconnect.
    with db.connect() as conn:
        conn.row_factory = dict_row
        locked = conn.execute("select pg_try_advisory_lock(hashtextextended(%s, 0)) as locked",
                              (f"review-apply:{workspace_id}:{proposal_id}",)).fetchone()["locked"]
        conn.commit()
        if not locked:
            raise ReviewConflict("This update is already being processed. Retry after it finishes.")
        sheets = _LazySheets()
        try:
            proposal, intent, finished = _prepare(conn, workspace_id, proposal_id, actor, action, revision,
                                                  idempotency_key, next_step_bullets, client_context_bullets, sheets)
        except SheetConflict as exc:
            raise ReviewConflict(str(exc)) from exc
        except SheetError as exc:
            raise ReviewUnavailable("The Sheet could not be read. Nothing was changed; retry when the connection is restored.") from exc
        except UniqueViolation as exc:
            raise ReviewConflict("This approval key is already in use. Reload the review queue before deciding.") from exc
        conn.commit()  # Durable before/after values must survive before any Google write.
        if finished:
            return _result(intent, proposal)
        before, after = intent["before_cells"], intent["after_cells"]
        prior_attempts = intent["write_attempts"]
        intent = conn.execute(
            "update review_decisions set write_attempts = write_attempts + 1 where id = %s returning *",
            (intent["id"],)).fetchone()
        conn.commit()  # A process interruption after this point may follow an accepted Google write.
        excluded = is_working_client(proposal["current_stage"])
        try:
            live = sheets.apply_update(proposal["current_company_name"], before["lastProgress"], before["nextStep"],
                                       after["lastProgress"], after["nextStep"],
                                       expected_client_context=before["clientContext"], new_client_context=after["clientContext"],
                                       allow_write=not excluded, recovering=bool(prior_attempts))
        except SheetWorkingClient as exc:
            _record_error(conn, proposal, intent, OUTSIDE_SALES_PIPELINE, conflict=not prior_attempts)
            raise ReviewConflict(OUTSIDE_SALES_PIPELINE) from exc
        except SheetConflict as exc:
            if prior_attempts:
                message = ("A previous Sheet write is unconfirmed and the row has changed. Keep this saved approval; "
                           "reconcile its before/after values with the Sheet before retrying. Nothing else was written.")
                _record_error(conn, proposal, intent, message, conflict=False)
                raise ReviewUnavailable(message) from exc
            message = "The Sheet changed while this update was waiting. Resync the Pipeline sheet and review it again."
            _record_error(conn, proposal, intent, message, conflict=True)
            raise ReviewConflict(message) from exc
        except (SheetWriteUncertain, SheetError) as exc:
            # Even a failed read on a recovery attempt must not erase a previous uncertain write's intent.
            message = "Sheet update not confirmed. Retry this saved approval to check and finish the same update."
            _record_error(conn, proposal, intent, message, conflict=False)
            raise ReviewUnavailable(message) from exc
        except Exception as exc:
            # An unexpected client failure may follow a successful write. Keep the immutable intent.
            message = "Sheet update not confirmed. Retry this saved approval to check and finish the same update."
            _record_error(conn, proposal, intent, message, conflict=False)
            raise ReviewUnavailable(message) from exc
        try:
            mirror_refreshed = _mirror_approval(conn, proposal, before, after)
            intent = conn.execute(
                "update review_decisions set status = 'succeeded', error = null, completed_at = now(), mirror_refreshed = %s"
                " where id = %s returning *", (mirror_refreshed, intent["id"])).fetchone()
            conn.execute("update review_proposals set status = 'approved', bullets = %s, next_step_bullets = %s,"
                         " client_context_bullets = %s, bullet_lineage = %s, lineage_version = %s, decided_by = %s, decided_at = now() where id = %s",
                         (Jsonb(intent["bullets"]), Jsonb(intent["next_step_bullets"]),
                          Jsonb(intent["client_context_bullets"]), Jsonb(intent.get("bullet_lineage") or {"nextStep": [], "clientContext": []}),
                          intent.get("lineage_version", 0), intent["actor"], proposal["id"]))
            _consume_sources(conn, proposal)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            # The earlier committed intent remains applying. A retry recognizes Google's exact after-values.
            raise ReviewUnavailable("The Sheet write needs confirmation. Retry the saved approval to finish recording it.") from exc
        if excluded or is_working_client(live.status):
            raise ReviewConflict(OUTSIDE_SALES_PIPELINE)
        return _result(intent, proposal, mirror_refreshed)
