"""Verify a brief against a completed sync before returning a sendable email."""

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from uuid import UUID, uuid4

import anthropic
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import brief_verification, db, timeline
from app.config import settings
from app.ingest import _workspace_id

CHECKER = "brief-send-v1"


class VerificationPending(Exception):
    """An API response that must bypass rendering and persistence in the brief builder."""

    def __init__(self, response: dict):
        self.response = response


def _fail(reason: str, build_id: UUID | None = None) -> None:
    raise VerificationPending({"ready_to_send": False, "verification_status": "blocked",
                               "build_id": str(build_id) if build_id else None, "reason": reason})


def _batch_receipts(conn, workspace_id, batch_id: str) -> list[dict]:
    feeds = set(settings.brief_verification_feeds)
    if not feeds:
        raise HTTPException(409, "No required brief feeds are configured")
    cur = conn.cursor(row_factory=dict_row)
    rows = cur.execute(
        "select distinct on (feed_key) id, feed_key, status, coverage_complete, started_at, finished_at"
        " from sync_runs where workspace_id = %s and sync_batch_id = %s and feed_key = any(%s)"
        " order by feed_key, started_at desc, id desc", (workspace_id, batch_id, sorted(feeds)),
    ).fetchall()
    cutoff = timeline.utcnow() - timedelta(hours=settings.brief_verification_sync_hours)
    if {r["feed_key"] for r in rows} != feeds or any(
        r["status"] != "succeeded" or not r["coverage_complete"]
        or r["finished_at"] is None or r["started_at"] < cutoff for r in rows
    ):
        raise HTTPException(409, "Required source receipts are missing, incomplete, failed, or stale")
    # A newer execution for the same feed supersedes its receipt. Also detect a legacy sync
    # started after this cohort began, since it does not carry a feed key.
    newer = cur.execute(
        "select exists (select 1 from sync_runs new where new.workspace_id = %s"
        " and new.sync_batch_id is distinct from %s and ("
        " exists (select 1 from sync_runs prior where prior.id = any(%s)"
        " and prior.feed_key = new.feed_key and new.started_at >= prior.started_at)"
        " or (new.feed_key is null and (new.started_at >= %s or new.status = 'running')"
        " and new.source = any(%s)))) as found",
        (workspace_id, batch_id, [r["id"] for r in rows], min(r["started_at"] for r in rows),
         sorted({feed.split(":")[0] for feed in feeds})),
    ).fetchone()
    if newer["found"]:
        raise HTTPException(409, "A newer source sync superseded the supplied receipts")
    return rows


def validate_sync_batch(conn, workspace_id, batch_id: str):
    """Return the final completion time of a fresh, fully attested sync execution."""
    return max(r["finished_at"] for r in _batch_receipts(conn, workspace_id, batch_id))


def _check_processing(conn, workspace_id, batch_id: str, finished_at) -> None:
    cur = conn.cursor(row_factory=dict_row)
    result = cur.execute(
        "select exists (select 1 from daily_runs where workspace_id = %s and sync_batch_id = %s"
        " and stage = 'done' and started_at >= %s and finished_at is not null)"
        " and not exists (select 1 from source_records where workspace_id = %s and status = 'pending') as ready",
        (workspace_id, batch_id, finished_at, workspace_id),
    ).fetchone()
    if not result["ready"]:
        raise HTTPException(409, "Daily processing has not finished successfully for this sync")


# xmin is the row version visible in the current PostgreSQL snapshot. Hash IDs and versions rather
# than activity contents so a late update, deletion, relink or Done/Undo invalidates earlier verdicts.
_SNAPSHOT_TABLES = ("source_records", "insights", "contacts", "company_domains", "companies", "opportunities",
                    "opportunity_profiles", "team_members", "brief_actions", "daily_runs")
_SNAPSHOT_QUERIES = [
    f"select '{table}:' || id::text || ':' || xmin::text as version from {table} where workspace_id = %(workspace)s"
    for table in _SNAPSHOT_TABLES
] + [
    "select 'workspace:' || id::text || ':' || xmin::text from workspaces where id = %(workspace)s",
    "select 'sync:' || id::text || ':' || xmin::text from sync_runs where workspace_id = %(workspace)s"
    " and (started_at >= %(since)s or status = 'running')",
]
_SNAPSHOT_SQL = ("select md5(coalesce(string_agg(version, ',' order by version), '')) as snapshot from ("
                 + " union all ".join(_SNAPSHOT_QUERIES) + ") versions")


def _snapshot(conn, workspace_id, since) -> str:
    return conn.cursor(row_factory=dict_row).execute(
        _SNAPSHOT_SQL, {"workspace": workspace_id, "since": since},
    ).fetchone()["snapshot"]


def _action_digest(actions: list[brief_verification.VerificationAction]) -> str:
    payload = json.dumps([asdict(a) for a in sorted(actions, key=lambda a: a.key)], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _client():
    if settings.anthropic_api_key is None:
        return None
    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value(), default_headers=headers,
                               timeout=35, max_retries=0)


class DeliveryGate:
    def __init__(self, workspace_id, batch_id: UUID, build_id: UUID, dry_run: bool,
                 as_of, since, snapshot: str):
        self.workspace_id = workspace_id
        self.batch_id = batch_id
        self.build_id = build_id
        self.dry_run = dry_run
        self.as_of = as_of
        self.since = since
        self.snapshot = snapshot
        self.audit: dict = {}

    def _ensure_current(self, conn) -> None:
        try:
            latest = validate_sync_batch(conn, self.workspace_id, str(self.batch_id))
            _check_processing(conn, self.workspace_id, str(self.batch_id), latest)
        except HTTPException as exc:
            _fail(str(exc.detail), self.build_id)
        if _snapshot(conn, self.workspace_id, self.since) != self.snapshot:
            _fail("Activity or founder decisions changed. Start a new build against current receipts.", self.build_id)

    def _progress(self, checked: int, total: int) -> None:
        raise VerificationPending({
            "ready_to_send": False, "verification_status": "pending", "build_id": str(self.build_id),
            "checked": checked, "total": total,
        })

    def __call__(self, actions: list[brief_verification.VerificationAction]) -> dict:
        # Keep the session lock across model I/O, without a database transaction open.
        # A duplicate poll can wait for the same build without buying a second Claude call.
        with db.connect() as lock_conn:
            lock_conn.autocommit = True
            if not lock_conn.execute(
                "select pg_try_advisory_lock(hashtextextended(%s, 0))", (f"brief:{self.build_id}",),
            ).fetchone()[0]:
                self._progress(0, len(actions))
            return self._advance(actions)

    def _advance(self, actions: list[brief_verification.VerificationAction]) -> dict:
        digest = _action_digest(actions)
        with db.connect() as conn:
            self._ensure_current(conn)
            run = conn.cursor(row_factory=dict_row).execute(
                "select * from brief_verification_runs where id = %s and workspace_id = %s",
                (self.build_id, self.workspace_id),
            ).fetchone()
            if run["action_digest"] and run["action_digest"] != digest:
                _fail("Selected actions changed. Start a new build.", self.build_id)
            if run["response"] is not None:
                raise VerificationPending(run["response"])
            verdicts = dict(run["verdicts"])
            missing = [a for a in actions if a.key not in verdicts]
            # Keep each request bounded even when several founders have rows on the same deal.
            deal = [a for a in missing if a.company_id == missing[0].company_id] if missing else []
            prepared = brief_verification.load(conn, self.workspace_id, deal, as_of=self.as_of) if deal else None

        input_tokens, output_tokens, budget_used = run["input_tokens"], run["output_tokens"], run["budget_used"]
        if prepared is not None:
            client = _client()
            try:
                report = brief_verification.verify(
                    prepared, client, settings.anthropic_model,
                    token_budget=max(0, settings.brief_verification_token_budget - budget_used),
                )
            finally:
                if client is not None:
                    client.close()
            verdicts.update({key: asdict(verdict) for key, verdict in report.verdicts.items()})
            input_tokens += report.input_tokens
            output_tokens += report.output_tokens
            budget_used += report.budget_used
        # Every action receives a verdict, including any incomplete model response.
        for action in deal:
            verdicts.setdefault(action.key, {"state": "unclear", "reason": "missing_verdict",
                                             "evidence_ids": [], "method": "review"})

        with db.connect() as conn:
            self._ensure_current(conn)
            conn.execute(
                "update brief_verification_runs set action_digest = %s, verdicts = %s, input_tokens = %s,"
                " output_tokens = %s, budget_used = %s where id = %s and workspace_id = %s",
                (digest, Jsonb(verdicts), input_tokens, output_tokens, budget_used, self.build_id, self.workspace_id),
            )
        if any(action.key not in verdicts for action in actions):
            self._progress(len(verdicts), len(actions))

        self.audit = {"checker": CHECKER, "sync_batch_id": str(self.batch_id), "as_of": self.as_of.isoformat(),
                      "verdicts": verdicts, "input_tokens": input_tokens, "output_tokens": output_tokens,
                      "budget_used": budget_used}
        return {key: brief_verification.Verdict(**value) for key, value in verdicts.items()}

    def ready_fields(self) -> dict:
        return {"ready_to_send": not self.dry_run, "verification_status": "preview" if self.dry_run else "verified",
                "build_id": str(self.build_id)}

    def finalize(self, conn, response: dict) -> None:
        if conn is None:
            with db.connect() as preview_conn:
                self._ensure_current(preview_conn)
            return
        cur = conn.cursor(row_factory=dict_row)
        saved = cur.execute(
            "select response from brief_verification_runs where id = %s and workspace_id = %s for update",
            (self.build_id, self.workspace_id),
        ).fetchone()
        self._ensure_current(conn)
        if saved["response"] is not None:
            raise VerificationPending(saved["response"])
        cur.execute("update brief_verification_runs set response = %s where id = %s and workspace_id = %s",
                    (Jsonb(response, dumps=lambda value: json.dumps(value, default=str)),
                     self.build_id, self.workspace_id))


def create_gate(sync_run_ids: list, build_id: UUID | str | None, version: str, dry_run: bool) -> DeliveryGate:
    supplied = set(str(i) for i in sync_run_ids)
    build_id = UUID(str(build_id)) if build_id else uuid4()
    if not supplied:
        _fail("Provide the current sync run IDs for all required sources.", build_id)
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        cur = conn.cursor(row_factory=dict_row)
        batches = cur.execute(
            "select distinct sync_batch_id from sync_runs where workspace_id = %s and id = any(%s)",
            (workspace_id, [UUID(i) for i in supplied]),
        ).fetchall()
        if len(batches) != 1 or batches[0]["sync_batch_id"] is None:
            _fail("Source receipts must belong to one sync execution.", build_id)
        batch_id = batches[0]["sync_batch_id"]
        try:
            receipts = _batch_receipts(conn, workspace_id, str(batch_id))
            _check_processing(conn, workspace_id, str(batch_id), max(r["finished_at"] for r in receipts))
        except HTTPException as exc:
            _fail(str(exc.detail), build_id)
        if {str(r["id"]) for r in receipts} != supplied:
            _fail("Supply the latest receipt for every required source in this sync execution.", build_id)

        since = min(r["started_at"] for r in receipts)
        snapshot = _snapshot(conn, workspace_id, since)
        now = timeline.utcnow()
        cur.execute(
            "insert into brief_verification_runs (id, workspace_id, sync_batch_id, version, dry_run, as_of, snapshot)"
            " values (%s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
            (build_id, workspace_id, batch_id, version, dry_run, now, snapshot),
        )
        run = cur.execute("select * from brief_verification_runs where id = %s and workspace_id = %s",
                          (build_id, workspace_id)).fetchone()
        if run is None or run["sync_batch_id"] != batch_id or run["version"] != version or run["dry_run"] != dry_run:
            _fail("Build ID belongs to another verification request.", build_id)
        if snapshot != run["snapshot"]:
            _fail("Activity or founder decisions changed. Start a new build.", build_id)
        if run["response"] is not None:
            raise VerificationPending(run["response"])
        return DeliveryGate(workspace_id, batch_id, build_id, dry_run, run["as_of"], since, snapshot)
