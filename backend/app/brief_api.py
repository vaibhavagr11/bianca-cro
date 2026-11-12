"""POST /brief/build: assemble today's brief from the commercial memory. n8n sends the returned email."""

import hashlib
import json
import logging
from datetime import datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import anthropic
from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app import actions, brief, brief_review, brief_v2, db, extractor, timeline
from app.brief_delivery import DeliveryGate, VerificationPending, create_gate
from app.brief_review import ActionSource
from app.brief_text import prepare_table_text
from app.config import settings
from app.gmail import domain
from app.ingest import _workspace_id, require_token
from app.pipeline import is_working_client
from app.pipeline_freshness import check_fresh_pipeline, require_fresh_pipeline
from app.process import _claude

log = logging.getLogger("bianca_cro.brief")


def _dumps(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
router = APIRouter(prefix="/brief")

FOUNDER_ORDER = ["RG", "NC"]  # sections in this order; NDL gets the small shared table
# Two founders are named Nicolás, so the brief uses these names, not first names.
DISPLAY = {"RG": ("Ramiro", "Ramiro Godfrid"), "NC": ("Nico", "Nico Casaux"), "NDL": ("Nico DL", "Nico de León")}


def _sales_deals(conn, workspace_id) -> tuple[dict[str, dict], set[str]]:
    active = {}
    working_clients = set()
    for deal in conn.execute(
            "select o.id, o.company_id, c.name, o.stage, o.stage_rank, o.owner_code, o.removed_at from opportunities o"
            " join companies c on c.id = o.company_id where o.workspace_id = %s order by o.id",
            (workspace_id,)):
        company_id = str(deal["company_id"])
        if is_working_client(deal["stage"]):
            working_clients.add(company_id)
        elif deal["removed_at"] is None:
            active[company_id] = deal
    return active, working_clients


def _working_client_record(record: dict, working_clients: set[str]) -> bool:
    # A rules link can identify the company even when the primary link is absent or disputed.
    return str(record["company_id"]) in working_clients or str(record.get("rules_company_id")) in working_clients


def _insight_rows(conn, workspace_id) -> list[dict]:
    return conn.execute(
        "select i.id, i.opportunity_id, i.type, i.text, i.quote, i.owner_side, i.owner_name, i.due_date, i.status,"
        " i.source, i.unit_id, i.occurred_at, i.created_at, i.company_id, i.source_record_ids, m.meta,"
        " i.action_scope, i.origin_record_id, i.origin_occurred_at, i.last_extracted_at, i.updated_at"
        " from insights i left join source_records m"
        " on m.id = coalesce(i.origin_record_id, i.source_record_ids[1]) where i.workspace_id = %s"
        " order by i.occurred_at desc nulls last, i.id",
        (workspace_id,),
    ).fetchall()


def _scoped_rows(rows: list[dict], deals: dict[str, dict]) -> list[dict]:
    """Sales items, plus not-yet-scoped ones; only delivery and out-of-scope work is hidden."""
    return [r for r in rows if r["action_scope"] in extractor.SALES_SCOPES
            and (deal := deals.get(str(r["company_id"]))) is not None
            and str(deal["id"]) == str(r["opportunity_id"])]


def _email_asks(rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    asks: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        if (r["source"] == "gmail" and r["origin_record_id"] and r["status"] == "open"
                and r["type"] == "next_step" and r["owner_side"] in ("bianca", "customer")):
            key = (str(r["origin_record_id"]), str(r["company_id"]), r["owner_side"])
            asks.setdefault(key, []).append(r)
    return asks


def _message_asks(asks: dict[tuple[str, str, str], list[dict]], message: dict, side: str) -> list[dict]:
    key = str(message["id"]), str(message["company_id"]), side
    # The subject can be inherited from an earlier email. The actual ask must be in the message body.
    return [r for r in asks.get(key, []) if extractor.quote_in_record(r["quote"], message, body_only=True)]


def _profile_sales_entry(entry: dict, profile: dict, sales_by_id: dict[str, dict], deal: dict) -> bool:
    """Every cited insight is a current sales (or not-yet-scoped) item. Profiles from before scopes still count."""
    refs = profile["profile"].get("refs") or {}
    evidence = [sales_by_id.get(str(refs.get(ref))) for ref in entry.get("evidence") or []]
    return bool(evidence) and all(r and str(r["opportunity_id"]) == str(deal["id"])
                                  and (r.get("updated_at") or r["created_at"]) <= profile["generated_at"]
                                  and (r["type"] not in ("commitment", "next_step") or r["status"] == "open")
                                  for r in evidence)


def _upcoming_meetings(conn, workspace_id, today, tz, deals, working_clients, email_to_code, sales) -> list[brief.Meeting]:
    # Preserve prep only when a qualifying action is supported by this particular meeting.
    prep: dict[str, str] = {}
    for r in sales:
        if (r["source"] == "calendar" and r["status"] == "open" and r["owner_side"] == "bianca"
                and r["type"] in ("commitment", "next_step")):
            prep.setdefault(str(r["origin_record_id"]), r["text"])
    start = datetime.combine(today, time.min, tz)
    meetings = []
    for m in conn.execute(
            "select id, subject, occurred_at, participants, company_id, rules_company_id from source_records"
            " where workspace_id = %s and source = 'calendar' and occurred_at >= %s and occurred_at < %s"
            " and coalesce(meta->>'status', 'confirmed') <> 'cancelled' and (classification->>'is_commercial')::bool"
            " order by occurred_at", (workspace_id, start, start + timedelta(days=2))):
        if _working_client_record(m, working_clients):
            continue
        deal = deals.get(str(m["company_id"]))
        codes = sorted({email_to_code[a["address"]] for a in (m["participants"] or {}).get("attendees") or []
                        if a.get("address") in email_to_code})
        meetings.append(brief.Meeting(m["occurred_at"].astimezone(tz), m["subject"] or "(no title)",
                                      deal["name"] if deal else None, deal["stage"] if deal else None, codes,
                                      context=prep.get(str(m["id"]), "")))
    return meetings


class BuildSyncReceipts(BaseModel):
    sync_run_ids: list[UUID] = Field(default_factory=list)


@router.post("/build")
def build_brief(dry_run: bool = False, version: str = "v1", x_ingest_token: str | None = Header(default=None),
                verification: Literal["off", "shadow", "required"] = "off",
                build_id: UUID | None = None, receipts: BuildSyncReceipts | None = None,
                pipeline_sync_run_id: UUID | None = None) -> dict:
    require_token(x_ingest_token)
    if pipeline_sync_run_id is not None:
        require_fresh_pipeline(pipeline_sync_run_id)
    if settings.brief_verification_required and not dry_run and verification != "required":
        raise HTTPException(status_code=409, detail="This brief requires action verification before sending.")
    if verification == "shadow" and not dry_run:
        raise HTTPException(status_code=422, detail="Shadow verification requires dry_run=true.")
    builder = build_brief_v2 if version == "v2" else build_brief_v1
    pipeline_args = {"pipeline_sync_run_id": pipeline_sync_run_id} if pipeline_sync_run_id is not None else {}
    if verification == "off":
        return builder(dry_run, **pipeline_args)
    try:
        gate = create_gate(receipts.sync_run_ids if receipts else [], build_id,
                           "v2" if version == "v2" else "v1", dry_run)
        return builder(dry_run, gate=gate, **pipeline_args)
    except VerificationPending as pending:
        return pending.response


def _finish(workspace_id, for_date, payload: dict, recipients: list[str], response: dict,
            dry_run: bool, gate: DeliveryGate | None, pipeline_sync_run_id: UUID | None = None) -> dict:
    if gate is not None:
        response.update(gate.ready_fields())
    if dry_run:
        if gate is not None:
            gate.finalize(None, response)
        if pipeline_sync_run_id is not None:
            require_fresh_pipeline(pipeline_sync_run_id)
        return response
    with db.connect() as conn:
        if pipeline_sync_run_id is not None:
            check_fresh_pipeline(conn, workspace_id, pipeline_sync_run_id)
        if gate is not None:
            gate.finalize(conn, response)
        conn.execute("insert into briefs (workspace_id, for_date, subject, html, payload, recipients)"
                     " values (%s, %s, %s, %s, %s, %s)",
                     (workspace_id, for_date, response["subject"], response["html"],
                      Jsonb(payload, dumps=_dumps), recipients))
    return response


def _recommendation_ref(opportunity_id, text: str) -> str:
    return f"{opportunity_id}:{hashlib.sha1(brief_v2.norm(text).encode()).hexdigest()[:12]}"


def _reply_to(subject: str | None) -> str:
    return f"Reply to the customer's email ({subject or 'no subject'})."


def build_brief_v1(dry_run: bool = False, since: datetime | None = None,
                   gate: DeliveryGate | None = None, pipeline_sync_run_id: UUID | None = None) -> dict:
    """The production brief. `since` overrides the change window for side-by-side replays only."""
    now = gate.as_of if gate else timeline.utcnow()
    sources: dict[str, ActionSource] = {}
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        ws = conn.execute("select timezone, internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()
        tz = ZoneInfo(ws["timezone"])
        today = now.astimezone(tz).date()
        internal = {d.lower() for d in ws["internal_domains"]} | {d.lower() for d in settings.project_team_domains}
        team = conn.execute("select owner_code, full_name, email, receives_brief from team_members"
                            " where workspace_id = %s", (workspace_id,)).fetchall()
        email_to_code = {t["email"].lower(): t["owner_code"] for t in team}
        last = conn.execute("select created_at from briefs where workspace_id = %s order by created_at desc limit 1",
                            (workspace_id,)).fetchone()
        since = since or (last["created_at"] if last else now - timedelta(hours=24))

        deals, working_clients = _sales_deals(conn, workspace_id)
        profiles = {str(r["opportunity_id"]): r for r in conn.execute(
            "select distinct on (opportunity_id) opportunity_id, profile, generated_at, profiler_version"
            " from opportunity_profiles"
            " where workspace_id = %s order by opportunity_id, generated_at desc", (workspace_id,))}
        sales = _scoped_rows(_insight_rows(conn, workspace_id), deals)
        sales_by_id = {str(r["id"]): r for r in sales}
        email_asks = _email_asks(sales)
        insight_origins = {str(r["id"]): r for r in conn.execute(
            "select id, origin_record_id, origin_occurred_at from insights where workspace_id = %s",
            (workspace_id,))} if gate else {}
        closed = actions.latest(conn, workspace_id) if gate else {}

        cands: list[brief.Candidate] = []
        seq = iter(range(1, 10_000))

        def cid() -> str:
            return f"C{next(seq)}"

        # Only agreed commitments with a firm deadline belong in the dated obligations.
        dated: list[brief.Row] = []
        hidden: dict[str, int] = {}
        first_due = today - timedelta(days=brief.OVERDUE_WINDOW_DAYS)
        last_due = today + timedelta(days=brief.WEEK_DAYS)
        for i in sales:
            if (i["type"] != "commitment" or i["owner_side"] != "bianca" or not i["due_date"]
                    or not first_due <= i["due_date"] <= last_due):
                continue
            d = deals[str(i["company_id"])]
            if i["status"] == "possibly_done" and not gate:   # verified builds re-check these instead
                hidden[d["owner_code"]] = hidden.get(d["owner_code"], 0) + 1
            if i["status"] not in (("open", "possibly_done") if gate else ("open",)):
                continue
            when = _event_day(i, tz) if i["occurred_at"] else None
            label = SOURCE_LABEL.get(i["source"], i["source"])
            link = _link_for(i["source"], i["unit_id"], i["meta"])
            ref = f"insight:{i['id']}" if gate else None
            if ref:
                sources[ref] = ActionSource.insight("commitment", i)
            dated.append(brief.Row(d["name"], i["text"], i["due_date"], i["owner_name"], d["stage"],
                                   f"{label} · {when:%b} {when.day}" if when else label, link,
                                   owner_code=d["owner_code"], verification_ref=ref))

        # Profile recommendations and blockers need current, sales-scoped evidence.
        open_sales_by_deal: dict[str, int] = {}
        for company_id, d in deals.items():
            prow = profiles.get(str(d["id"]))
            if not prow:
                continue
            prof = prow["profile"]
            for a in prof.get("recommended_actions") or []:
                if a.get("action_scope") not in extractor.SALES_SCOPES or not _profile_sales_entry(a, prow, sales_by_id, d):
                    continue
                score = {"high": 70, "medium": 50, "low": 30}.get(a.get("urgency"), 30)
                rec_id = _recommendation_ref(d["id"], a["action"])
                if closed.get(("rec", rec_id), ("",))[0] == "done":
                    continue
                ref = f"rec:{rec_id}" if gate else None
                if ref:
                    sources[ref] = brief_review.profile_source(prof, a, insight_origins)
                detail = a["action"]
                cands.append(brief.Candidate(cid(), d["name"], d["stage"], d["owner_code"], "recommended",
                                              "AI recommendation", detail, score, verification_ref=ref))
            open_sales_by_deal[company_id] = sum(_profile_sales_entry(e, prow, sales_by_id, d)
                                                for e in (prof.get("open_commitments") or []) + (prof.get("blockers") or []))
            if (d["stage_rank"] or 0) >= 1:
                for b in (prof.get("blockers") or [])[:2]:
                    if not _profile_sales_entry(b, prow, sales_by_id, d):
                        continue
                    cands.append(brief.Candidate(cid(), d["name"], d["stage"], d["owner_code"], "blocker",
                                                 "Blocker",
                                                 f"Blocker: {b['text']}", 60))

        # Unanswered customer emails: thread's latest message is from outside Bianca.
        seen_threads = set()
        for t in conn.execute(
                "select id, thread_id, company_id, rules_company_id, subject, body, occurred_at, participants"
                " from source_records where workspace_id = %s and source = 'gmail' and company_id is not null"
                " and (classification->>'is_commercial')::bool and occurred_at >= %s"
                " and occurred_at <= %s order by thread_id, occurred_at desc, id",
                (workspace_id, now - timedelta(days=brief.UNANSWERED_MAX_DAYS), now)):
            if _working_client_record(t, working_clients):
                continue
            d = deals.get(str(t["company_id"]))
            senders = [p["address"] for p in (t["participants"] or {}).get("from") or []]
            thread = t["thread_id"]
            if thread in seen_threads or brief_v2.is_automated(senders, t["subject"]):
                continue  # an out-of-office or bot: the thread's latest real message decides it
            seen_threads.add(thread)
            handled = closed.get(("thread", thread))
            if handled and handled[0] == "done" and handled[1] >= t["occurred_at"]:
                continue
            asks = _message_asks(email_asks, t, "bianca")
            if d and asks and senders and all(domain(s) not in internal for s in senders):
                c = brief.unanswered_candidate(cid(), d["name"], d["stage"], d["owner_code"], t["subject"],
                                               t["occurred_at"], now, timeline.GMAIL_THREAD_URL.format(t["thread_id"]))
                if c:
                    c.detail = "Customer asked: " + " · ".join(a["text"] for a in asks)
                    if gate:
                        c.verification_ref = f"thread:{thread}"
                        sources[c.verification_ref] = ActionSource("reply", str(t["id"]), t["occurred_at"])
                    cands.append(c)

        for i in sales:
            if (i["status"] != "open" or i["owner_side"] != "bianca" or i["type"] != "next_step"
                    or i["source"] == "gmail" or not i["occurred_at"]):
                continue
            recorded = _event_day(i, tz)
            if (today - recorded).days > brief_v2.UNDATED_WINDOW_DAYS:
                continue
            d = deals[str(i["company_id"])]
            label = SOURCE_LABEL.get(i["source"], i["source"])
            cands.append(brief.Candidate(cid(), d["name"], d["stage"], d["owner_code"], "next_step", "Next step",
                                         i["text"], 60, link=_link_for(i["source"], i["unit_id"], i["meta"]),
                                         source=f"{label} · {recorded:%b} {recorded.day}"))

        # Quiet active deals that still have open items.
        last_activity = {str(r["company_id"]): r["last"] for r in conn.execute(
            "select company_id, max(occurred_at) as last from source_records where workspace_id = %s"
            " and company_id is not null and occurred_at <= now() group by company_id", (workspace_id,))}
        for company_id, d in deals.items():
            la = last_activity.get(company_id)
            c = brief.quiet_candidate(cid(), d["name"], d["stage"], d["stage_rank"], d["owner_code"],
                                      la.astimezone(tz).date() if la else None, open_sales_by_deal.get(company_id, 0), today)
            if c:
                cands.append(c)

        # Meetings today and tomorrow (local time), not cancelled.
        meetings = _upcoming_meetings(conn, workspace_id, today, tz, deals, working_clients, email_to_code, sales)

        # Changes since the last brief.
        changes: list[brief.Change] = []
        first_pipeline_run = conn.execute(
            "select id from sync_runs where workspace_id = %s and source = 'pipeline' and status = 'succeeded'"
            " order by started_at limit 1", (workspace_id,)).fetchone()
        for ch in conn.execute(
                "select c.name, o.company_id, o.owner_code, x.field, x.old_value, x.new_value, x.sync_run_id"
                " from opportunity_changes x"
                " join opportunities o on o.id = x.opportunity_id join companies c on c.id = o.company_id"
                " where x.workspace_id = %s and x.detected_at > %s and x.field in ('stage', 'created', 'removed', 'owner_code')"
                " order by x.detected_at desc", (workspace_id, since)):
            if str(ch["company_id"]) in working_clients:
                continue
            if ch["field"] == "created" and first_pipeline_run and ch["sync_run_id"] == first_pipeline_run["id"]:
                continue
            text = {"created": "New deal in pipeline", "removed": "Removed from pipeline"}.get(
                ch["field"], f"{'Stage' if ch['field'] == 'stage' else 'Owner'}: {ch['old_value']} → {ch['new_value']}")
            changes.append(brief.Change(ch["name"], text, ch["owner_code"]))
        for r in conn.execute(
                "select distinct on (source, thread_id) source, thread_id, subject, company_id, rules_company_id,"
                " participants, meta"
                " from source_records where workspace_id = %s and company_id is not null and occurred_at > %s"
                " and occurred_at <= now() and (classification->>'is_commercial')::bool"
                " order by source, thread_id, occurred_at desc", (workspace_id, since)):
            d = deals.get(str(r["company_id"]))
            if not d or _working_client_record(r, working_clients):
                continue
            if r["source"] == "gmail":
                senders = [p["address"] for p in (r["participants"] or {}).get("from") or []]
                if senders and all(domain(s) not in internal for s in senders):
                    changes.append(brief.Change(d["name"], f"Customer replied: {r['subject'] or '(no subject)'}",
                                                d["owner_code"], timeline.GMAIL_THREAD_URL.format(r["thread_id"])))
            elif r["source"] == "notes":
                changes.append(brief.Change(d["name"], f"New meeting notes: {r['subject'] or '(no subject)'}", d["owner_code"],
                                            timeline.DOC_URL.format((r["meta"] or {}).get("doc_id", ""))))
        for r in conn.execute(
                "select subject, company_id, rules_company_id from source_records where workspace_id = %s and source = 'calendar'"
                " and meta->>'status' = 'cancelled' and updated_at > %s and occurred_at >= now()",
                (workspace_id, since)):
            if _working_client_record(r, working_clients):
                continue
            d = deals.get(str(r["company_id"]))
            changes.append(brief.Change(d["name"] if d else "-", f"Meeting cancelled: {r['subject'] or '(no subject)'}",
                                        d["owner_code"] if d else None))

    # Per-founder sections.
    names = {t["owner_code"]: t["full_name"] for t in team}
    client = None if gate else (_claude() if any(c.owner_code in FOUNDER_ORDER for c in cands) else None)
    sections, payload = [], {"sections": [], "ndl": [], "changes": []}
    for code in FOUNDER_ORDER:
        mine = [c for c in cands if c.owner_code == code]
        my_meetings = [m for m in meetings if code in m.attendees]
        if gate or not mine:
            plan = brief.fallback_plan(mine)   # source wording, ranked by score
        else:
            try:
                plan = brief.plan_for(client, settings.anthropic_model, names.get(code, code), mine, today)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                log.warning("brief plan failed for %s: %s", code, type(exc).__name__)
                plan = brief.fallback_plan(mine)
        todo, week = brief.split_dated([r for r in dated if r.owner_code == code], today)
        todo += brief.priority_rows(brief.resolve_plan(plan, mine))
        meet_rows = [(m, m.context) for m in my_meetings]
        short, full = DISPLAY.get(code, (code, names.get(code, code)))
        sections.append({"owner": code, "name": full, "short": short, "today": todo, "week": week,
                         "meetings": meet_rows, "hidden": 0 if gate else hidden.get(code, 0)})
    ndl_today, _ = brief.split_dated([r for r in dated if r.owner_code == "NDL"], today)
    ndl_unanswered = [brief.Row(c.deal, c.detail, None, None, c.stage, c.source or "Email", c.link,
                               verification_ref=c.verification_ref)
                      for c in cands if c.owner_code == "NDL" and c.kind == "unanswered_email"]
    ndl = (ndl_today + ndl_unanswered)[:brief.MAX_NDL]
    ndl_review = None
    if gate:
        companies = {d["name"]: key for key, d in deals.items()}
        ndl_review = brief_review.verify_v1(sections, ndl, companies, sources, gate)
        payload["verification"] = gate.audit
        payload["ndl_verification"] = ndl_review.sections()
    for s in sections:
        payload["sections"].append({
            "owner": s["owner"],
            "today": [{"company": r.company, "what": r.what, "due": r.due, "why": r.why, "source": r.source}
                      for r in s["today"]],
            "week": [{"company": r.company, "what": r.what, "due": r.due, "source": r.source} for r in s["week"]],
            "meetings": [{"at": m.at.isoformat(), "title": m.title, "deal": m.deal, "prep": p}
                         for m, p in s["meetings"]],
        })
        if gate:
            payload["sections"][-1]["verification"] = s["verification"].sections()
    payload["ndl"] = [{"company": r.company, "what": r.what, "due": r.due} for r in ndl]
    payload["changes"] = [{"deal": c.deal, "text": c.text, "owner": c.owner_code} for c in changes[:brief.MAX_CHANGES]]

    # Verified builds keep wording stable across polls: no model rewrite, only the deterministic cut.
    display = prepare_table_text(brief.table_text_fields(sections, ndl, changes), client, settings.anthropic_model)
    html_body = brief.render(sections, ndl, changes, today, len(deals), ndl_review, display=display)
    text_body = brief.plain_text(sections, today, ndl_review, display=display)
    subj = brief.subject(sections, today)
    to = settings.brief_to or [t["email"] for t in team if t["receives_brief"]]
    log.info("brief built for %s: candidates=%s meetings=%s changes=%s dry_run=%s",
             today, len(cands), len(meetings), len(changes), dry_run)
    response = {"subject": subj, "html": html_body, "text": text_body, "to": ",".join(to), "for_date": today.isoformat(),
                "candidates": len(cands), "dry_run": dry_run}
    if gate:
        response["payload"] = json.loads(_dumps(payload))
    return _finish(workspace_id, today, payload, to, response, dry_run, gate, pipeline_sync_run_id)


# ---------------------------------------------------------------- v2 (deal-grouped, owner-checked, stage-aware)

SOURCE_LABEL = {"gmail": "Email", "notes": "Notes", "calendar": "Meeting"}
NICO_INBOX_CONNECTED = True  # Ramiro's and Nico's Gmail/Calendar are both ingested


def _link_for(source: str, unit_id: str | None, meta: dict | None) -> str | None:
    if source == "gmail" and unit_id:
        return timeline.GMAIL_THREAD_URL.format(unit_id)
    if source == "notes" and (meta or {}).get("doc_id"):
        return timeline.DOC_URL.format(meta["doc_id"])
    return None


def _event_day(rec: dict, tz) -> "date":
    """The day the source event happened. Notes carry a date only (stored as midnight UTC), so no zone shift."""
    return rec["occurred_at"].date() if rec["source"] == "notes" else rec["occurred_at"].astimezone(tz).date()


def build_brief_v2(dry_run: bool = False, since: datetime | None = None,
                   gate: DeliveryGate | None = None, pipeline_sync_run_id: UUID | None = None) -> dict:
    """Build the sales brief with compact deal-level selection (see brief_v2).

    `since` overrides the change window for side-by-side replays only.
    """
    now = gate.as_of if gate else timeline.utcnow()
    sources: dict[str, ActionSource] = {}
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        ws = conn.execute("select timezone, internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()
        tz = ZoneInfo(ws["timezone"])
        today = now.astimezone(tz).date()
        internal = {d.lower() for d in ws["internal_domains"]} | {d.lower() for d in settings.project_team_domains}
        team = conn.execute("select owner_code, full_name, email, receives_brief from team_members"
                            " where workspace_id = %s", (workspace_id,)).fetchall()
        email_to_code = {t["email"].lower(): t["owner_code"] for t in team}
        team_by_name = {brief_v2.norm(t["full_name"]): t["owner_code"] for t in team}
        display = {code: DISPLAY[code][0] for code in DISPLAY}
        last = conn.execute("select created_at from briefs where workspace_id = %s order by created_at desc limit 1",
                            (workspace_id,)).fetchone()
        since = since or (last["created_at"] if last else now - timedelta(hours=24))

        deals, working_clients = _sales_deals(conn, workspace_id)
        profiles = {str(r["opportunity_id"]): r for r in conn.execute(
            "select distinct on (opportunity_id) opportunity_id, profile, generated_at, profiler_version"
            " from opportunity_profiles"
            " where workspace_id = %s order by opportunity_id, generated_at desc", (workspace_id,))}
        ins_rows = _insight_rows(conn, workspace_id)
        sales = _scoped_rows(ins_rows, deals)
        sales_by_id = {str(r["id"]): r for r in sales}
        email_asks = _email_asks(sales)
        # An email whose extracted items are all delivery or out-of-scope work does not move the sales conversation:
        # it neither answers our sales ask nor counts as our follow-up. An email with nothing extracted still does.
        email_scopes: dict[str, set] = {}
        for r in ins_rows:
            if r["source"] == "gmail" and r["origin_record_id"]:
                email_scopes.setdefault(str(r["origin_record_id"]), set()).add(r["action_scope"])
        not_sales = {mid for mid, scopes in email_scopes.items() if not scopes & set(extractor.SALES_SCOPES)}
        newest_insight: dict[str, datetime] = {}
        for r in ins_rows:
            k = str(r["opportunity_id"])
            if k not in newest_insight or r["last_extracted_at"] > newest_insight[k]:
                newest_insight[k] = r["last_extracted_at"]
        ins_by_id = {str(r["id"]): r for r in ins_rows}
        closed = actions.latest(conn, workspace_id)  # founders' "Mark as complete" clicks: (kind, ref) -> (action, at)
        eligible_statuses = ("open", "possibly_done") if gate else ("open",)
        bianca_actions = [r for r in sales if r["type"] in ("commitment", "next_step") and r["owner_side"] == "bianca"]
        open_items = [r for r in bianca_actions if r["status"] in eligible_statuses]
        # Who wrote the passage each open item quotes: Bianca, the customer, or the notes doc.
        recs = {str(r["id"]): r for r in conn.execute(
            "select id, subject, body, participants from source_records where id = any(%s)",
            ([rid for r in open_items for rid in r["source_record_ids"]],))}
        last_contact = {str(r["company_id"]): r["last"].astimezone(tz).date() for r in conn.execute(
            "select company_id, max(occurred_at) as last from source_records where workspace_id = %s"
            " and company_id is not null and occurred_at <= now() group by company_id", (workspace_id,))}
        hidden: dict[str, int] = {}
        for r in bianca_actions:
            if r["status"] == "possibly_done":
                code = deals[str(r["company_id"])]["owner_code"]
                hidden[code] = hidden.get(code, 0) + 1

        # An inbound last message may be unanswered; an outbound last message needs a verified reply expectation.
        unanswered: dict[str, brief_v2.Unanswered] = {}
        threads: dict[str, list[dict]] = {}
        unclassified_latest: dict[str, datetime] = {}
        for t in conn.execute(
                "select id, thread_id, company_id, rules_company_id, subject, body, occurred_at, participants,"
                " external_id, content_hash,"
                " classification is null as unclassified, classification->'reply_expectation' as reply_expectation"
                " from source_records where workspace_id = %s and source = 'gmail' and occurred_at <= %s"
                " and (classification is null or (company_id is not null and (classification->>'is_commercial')::bool))"
                " order by thread_id, occurred_at, external_id", (workspace_id, now)):
            if _working_client_record(t, working_clients):
                continue
            p = t["participants"] or {}
            frm = [x["address"] for x in p.get("from") or []]
            automated = brief_v2.is_automated(frm, t["subject"])
            if t["unclassified"]:
                if not automated:
                    unclassified_latest[t["thread_id"]] = t["occurred_at"]
                continue
            if str(t["id"]) in not_sales:
                continue
            threads.setdefault(t["thread_id"], []).append(
                {"at": t["occurred_at"], "from": frm, "to": p.get("to") or [], "cc": p.get("cc") or [],
                 "automated": automated, "row": t, "external_id": t["external_id"], "content_hash": t["content_hash"],
                 "reply_expectation": t["reply_expectation"]})
        from_customer = lambda m: bool(m["from"]) and all(domain(a) not in internal for a in m["from"])
        last_reply: dict[str, datetime] = {}         # deal -> the customer's latest real email, any thread
        last_ours: dict[str, datetime] = {}          # deal -> our latest email, any thread
        ours_sent: dict[str, list[datetime]] = {}    # deal -> every email we sent outside Bianca
        last_auto: dict[str, datetime] = {}          # deal -> latest out-of-office/bounce (they arrive as own threads)
        outside = lambda m: any(domain(x.get("address")) not in internal for x in m["to"] + m["cc"])
        for msgs in threads.values():
            for m in msgs:
                d = deals.get(str(m["row"]["company_id"]))
                if not d:
                    continue
                if m["automated"]:
                    if from_customer(m) and (d["name"] not in last_auto or m["at"] > last_auto[d["name"]]):
                        last_auto[d["name"]] = m["at"]
                    continue
                if not from_customer(m) and outside(m):
                    ours_sent.setdefault(d["name"], []).append(m["at"])
                latest = last_reply if from_customer(m) else last_ours
                if d["name"] not in latest or m["at"] > latest[d["name"]]:
                    latest[d["name"]] = m["at"]
        waiting: dict[str, brief_v2.Waiting] = {}
        for thread_id, msgs in threads.items():
            real = [m for m in msgs if not m["automated"]]
            if not real:
                continue
            t = real[-1]["row"]
            d = deals.get(str(t["company_id"]))
            handled = closed.get(("thread", thread_id))
            if not d or (handled and handled[0] == "done" and handled[1] >= t["occurred_at"]):
                continue  # a founder marked it handled after its latest real message
            if not from_customer(real[-1]):
                unclassified_at = unclassified_latest.get(thread_id)
                if unclassified_at is not None and unclassified_at >= t["occurred_at"]:
                    continue  # newer real mail can resolve or change the request before it is classified
                asks = _message_asks(email_asks, t, "customer")   # what we asked for, when extraction found it
                w = brief_v2.waiting_on_them(msgs, internal, now)
                if not w or (not gate and (last_reply.get(d["name"], t["occurred_at"]) > t["occurred_at"]
                                           or last_ours.get(d["name"], t["occurred_at"]) > t["occurred_at"])):
                    continue  # legacy mode uses a deal-wide activity shortcut
                sender = next((email_to_code.get(a.lower()) for a in w["last"]["from"]), None)
                sent = t["occurred_at"].astimezone(tz)
                replied = last_reply.get(d["name"])
                nudges = sum(1 for at in ours_sent.get(d["name"], []) if not replied or at > replied)  # every thread
                if d["name"] not in waiting or sent.date() > waiting[d["name"]].sent:
                    waiting[d["name"]] = brief_v2.Waiting(
                        d["name"], d["stage"], w["to"], sent.date(), w["days"], max(nudges, w["nudges"]), replied is not None,
                        w["auto_reply"] or last_auto.get(d["name"], t["occurred_at"]) > t["occurred_at"],
                        timeline.GMAIL_THREAD_URL.format(thread_id), f"Email · {sent:%b} {sent.day}",
                        thread_id, sender if sender in FOUNDER_ORDER else d["owner_code"], d["stage_rank"],
                        action=asks[0]["text"] if asks else None)
                    if gate:
                        sources[f"wait:{thread_id}"] = ActionSource("waiting", str(t["id"]), t["occurred_at"])
                continue
            asks = [r for r in _message_asks(email_asks, t, "bianca")
                    if (not r["owner_name"] or team_by_name.get(brief_v2.norm(r["owner_name"])) == d["owner_code"])]
            if not asks:
                continue
            c = brief.unanswered_candidate("x", d["name"], d["stage"], d["owner_code"], t["subject"],
                                           t["occurred_at"], now, timeline.GMAIL_THREAD_URL.format(thread_id))
            if c:
                days = max(1, (now - t["occurred_at"]).days)
                if d["name"] not in unanswered or days < unanswered[d["name"]].days:
                    sent = t["occurred_at"].astimezone(tz)
                    unanswered[d["name"]] = brief_v2.Unanswered(
                        t["subject"] or "", days, c.link, True, action=asks[0]["text"],
                        source=f"Email · {sent:%b} {sent.day}", thread_id=thread_id, item_id=str(asks[0]["id"]))
                    if gate:
                        sources[f"thread:{thread_id}"] = ActionSource("reply", str(t["id"]), t["occurred_at"])

        meetings = _upcoming_meetings(conn, workspace_id, today, tz, deals, working_clients, email_to_code, sales)

        # What changed: only records stored since the last brief can hold a development; each is then dated by
        # its source event time (email sent, meeting date), never by when we stored it.
        new_records = [record for record in conn.execute(
            "select id, source, thread_id, subject, body, company_id, rules_company_id, participants, meta,"
            " occurred_at, created_at"
            " from source_records where workspace_id = %s and company_id is not null and created_at > %s"
            " and (classification->>'is_commercial')::bool order by occurred_at", (workspace_id, since))
            if not _working_client_record(record, working_clients)]
        stage_changes = conn.execute(
            "select c.name, o.company_id, x.field, x.old_value, x.new_value, x.sync_run_id from opportunity_changes x"
            " join opportunities o on o.id = x.opportunity_id join companies c on c.id = o.company_id"
            " where x.workspace_id = %s and x.detected_at > %s and x.field in ('stage', 'created', 'removed', 'owner_code')"
            " order by x.detected_at desc", (workspace_id, since)).fetchall()
        first_pipeline_run = conn.execute(
            "select id from sync_runs where workspace_id = %s and source = 'pipeline' and status = 'succeeded'"
            " order by started_at limit 1", (workspace_id,)).fetchone()

    def note(d: dict, entry: dict | None, prof_row: dict | None, with_why: bool) -> brief_v2.Note | None:
        if not entry or not prof_row:
            return None
        prof = prof_row["profile"]
        refs = [r for r in entry.get("evidence") or [] if r.startswith("I")]
        resolved = [ins_by_id[i] for i in ((prof.get("refs") or {}).get(r) for r in refs) if i in ins_by_id]
        fresh = prof_row["generated_at"] >= newest_insight.get(str(d["id"]), prof_row["generated_at"])
        verified = bool(refs) and len(resolved) == len(refs) and fresh
        link = _link_for(resolved[0]["source"], resolved[0]["unit_id"], resolved[0]["meta"]) if resolved else None
        text = entry.get("action") or entry.get("text") or ""
        dated = [(r["origin_occurred_at"] or r["occurred_at"], r) for r in resolved
                 if r["origin_occurred_at"] or r["occurred_at"]]
        when, newest = max(dated, key=lambda pair: pair[0]) if dated else (None, None)
        if when and newest["source"] != "notes":
            when = when.astimezone(tz)  # notes carry the meeting's calendar date in UTC
        source = f"{SOURCE_LABEL.get(newest['source'], newest['source'])} · {when:%b} {when.day}" if newest else ""
        ref = _recommendation_ref(d["id"], text)
        if gate and with_why:
            sources[f"rec:{ref}"] = brief_review.profile_source(prof, entry, ins_by_id)
        return brief_v2.Note(text, entry.get("why") if with_why else None, verified, entry.get("basis"),
                             link if verified else None, frozenset(str(x["id"]) for x in resolved), source, ref)

    views: dict[str, brief_v2.DealView] = {}
    for company_id, d in deals.items():
        prow = profiles.get(str(d["id"]))
        prof = prow["profile"] if prow else {}
        blockers = [n for b in prof.get("blockers") or [] if _profile_sales_entry(b, prow, sales_by_id, d)
                    and (n := note(d, b, prow, False)) and n.verified]
        recs_ = [n for a in prof.get("recommended_actions") or [] if a.get("action_scope") in extractor.SALES_SCOPES
                 and _profile_sales_entry(a, prow, sales_by_id, d)
                 and (n := note(d, a, prow, True)) and n.verified
                 and closed.get(("rec", n.ref), ("",))[0] != "done"]
        views[d["name"]] = brief_v2.DealView(
            d["name"], d["stage"], d["stage_rank"], d["owner_code"], last_contact.get(company_id),
            blocker=blockers[0] if blockers else None,
            recommendation=recs_[0] if recs_ else None,
            recommendations=recs_,
            summary=note(d, prof.get("summary"), prow, False), unanswered=unanswered.get(d["name"]))

    bianca_names = {brief_v2.norm(t["full_name"]) for t in team} | {"bianca"}

    def owner_side_of(owner: str | None) -> str:
        o = brief_v2.norm((owner or "").split("(")[0])
        return "bianca" if o in bianca_names or o.startswith("bianca") else "customer"

    profile_side: dict[str, str] = {}   # insight id -> side of the open commitment that cites it ("none" if not listed)
    fresh_opps: set[str] = set()
    for d in deals.values():
        prow = profiles.get(str(d["id"]))
        if not prow or prow["generated_at"] < newest_insight.get(str(d["id"]), prow["generated_at"]):
            continue                    # no current profile: no second reading available
        fresh_opps.add(str(d["id"]))
        refs = prow["profile"].get("refs") or {}
        for oc in prow["profile"].get("open_commitments") or []:
            for ref in oc.get("evidence") or []:
                if ref in refs:
                    side = owner_side_of(oc.get("owner"))
                    if profile_side.get(refs[ref]) != "bianca":
                        profile_side[refs[ref]] = side

    def side_for(r: dict) -> str | None:
        if str(r["opportunity_id"]) not in fresh_opps:
            return None
        return profile_side.get(str(r["id"]), "none")

    def speaker(r: dict) -> str:
        if r["source"] != "gmail":
            return r["source"]
        quote = " ".join(r["quote"].split()).lower()
        record_ids = [r["origin_record_id"]] if r["origin_record_id"] else r["source_record_ids"]
        for rid in record_ids:
            rec = recs.get(str(rid))
            if rec and quote in " ".join(f"{rec['subject'] or ''} {rec['body'] or ''}".split()).lower():
                frm = [p["address"] for p in (rec["participants"] or {}).get("from") or []]
                if brief_v2.is_automated(frm, rec["subject"]):
                    return "notes"      # a meeting-notes bot (Gemini) wrote it, not the customer
                return "bianca" if frm and all(domain(a) in internal for a in frm) else "customer"
        return "unknown"

    founders = set(FOUNDER_ORDER)
    t1, t2 = settings.brief_today_overdue_days, settings.brief_stale_overdue_days
    items: list[brief_v2.Item] = []
    for r in open_items:
        d = deals[str(r["company_id"])]
        route, basis, delegated = brief_v2.resolve_owner(r["owner_name"], d["owner_code"], team_by_name, founders, display)
        timestamp = r["origin_occurred_at"] or r["occurred_at"]
        recorded = brief_v2.recorded_on(timestamp, tz)
        when = timestamp.astimezone(tz) if timestamp else None
        kind = brief_v2.item_kind(r["type"], r["due_date"], speaker(r), r["quote"], side_for(r))
        if gate:
            sources[f"insight:{r['id']}"] = ActionSource.insight("waiting" if kind == "proposed" else "commitment", r)
        items.append(brief_v2.Item(
            str(r["id"]), d["name"], r["text"], r["due_date"], recorded, r["owner_name"], route, basis, delegated,
            f"{SOURCE_LABEL.get(r['source'], r['source'])} · {when:%b} {when.day}" if when else r["source"],
            _link_for(r["source"], r["unit_id"], r["meta"]),
            brief_v2.bucket(r["due_date"], recorded, today, t1, t2), kind))

    # ---- classify every newly extracted insight by the event time of the record its quote comes from
    lookback = settings.brief_late_lookback_days
    records_by_id = {str(rec["id"]): rec for rec in new_records}
    events: list[tuple[dict, dict, str]] = []          # (insight, record, new|late|backfill|undated|scheduled)
    for r in sales:
        if not (r["last_extracted_at"] > since and r["type"] in brief_v2.CHANGE_TYPES):
            continue
        rec = records_by_id.get(str(r["origin_record_id"]))
        if rec is None:
            continue                                   # quote is in a record we already had: not a change
        reliable = not (rec["meta"] or {}).get("date_inferred")
        kind = brief_v2.classify_event(rec["occurred_at"], rec["created_at"], since, now, lookback, reliable)
        if kind != "seen":
            events.append((r, rec, kind))
    for r, _, kind in events:                          # late-recorded developments still count for prioritization
        if kind in ("new", "late"):
            views[deals[str(r["company_id"])]["name"]].developments += 1

    names = {t["owner_code"]: t["full_name"] for t in team}
    sections = []
    for code in FOUNDER_ORDER:
        short, full = DISPLAY.get(code, (code, names.get(code, code)))
        sections.append(brief_v2.build_section(
            code, full, short, views, items, [(m, m.context) for m in meetings if code in m.attendees], today, display,
            0 if gate else hidden.get(code, 0), settings.brief_critical_ahead_days, settings.brief_in_talks_target))
        sections[-1].waiting, sections[-1].more_waiting = brief_v2.pick_waiting(list(waiting.values()), code)

    ndl = [brief_v2.TodayRow(i.deal, views[i.deal].stage, "Due now", i.text, f"due {i.due:%b} {i.due.day}", "Nico DL",
                             i.link, i.id, i.source, f"insight:{i.id}")
           for i in items if i.route == "NDL" and i.kind == "owed" and i.bucket in ("today", "overdue")]
    ndl += [brief_v2.TodayRow(v.deal, v.stage, "Reply needed", brief_v2.reply_text(v.unanswered),
                              f"received {v.unanswered.days}d ago", "Nico DL", v.unanswered.link, v.unanswered.item_id,
                              v.unanswered.source, v.unanswered.completion_ref)
            for v in views.values() if v.owner == "NDL" and v.unanswered and brief_v2.reply_label(v.unanswered) == "reply_needed"]
    ndl = ndl[:brief.MAX_NDL]
    ndl_review = brief_review.verify_v2(sections, ndl, {d["name"]: key for key, d in deals.items()}, sources, gate) \
        if gate else None

    changes: list[brief_v2.ChangeRow] = []
    for ch in stage_changes:
        if str(ch["company_id"]) in working_clients:
            continue
        if ch["field"] == "created" and first_pipeline_run and ch["sync_run_id"] == first_pipeline_run["id"]:
            continue
        text = {"created": "New deal in the pipeline Sheet", "removed": "Removed from the pipeline Sheet"}.get(
            ch["field"], f"{'Stage' if ch['field'] == 'stage' else 'Owner'} changed in the Sheet: "
                         f"{ch['old_value']} → {ch['new_value']}")
        changes.append(brief_v2.ChangeRow(ch["name"], views[ch["name"]].rank if ch["name"] in views else None,
                                          text, "Sheet", None))
    explained: set[tuple[str, str]] = set()
    per_deal: dict[str, int] = {}
    order = {"blocker": 0, "buying_signal": 1, "objection": 2, "commitment": 3, "next_step": 4, "need": 5}
    for r, rec, kind in sorted(events, key=lambda e: (e[2] != "new", order[e[0]["type"]])):
        if kind not in ("new", "late"):
            continue
        d = deals[str(r["company_id"])]
        explained.add((r["source"], r["unit_id"]))
        if per_deal.get(d["name"], 0) >= 1:
            continue
        per_deal[d["name"]] = per_deal.get(d["name"], 0) + 1
        event = _event_day(rec, tz)
        q = " ".join(r["quote"].split()).lower()
        title_only = q in " ".join((rec["subject"] or "").split()).lower() and q not in " ".join((rec["body"] or "").split()).lower()
        frm = [x["address"] for x in (rec["participants"] or {}).get("from") or []]
        spk = (r["source"] if r["source"] != "gmail" else "notes" if brief_v2.is_automated(frm, None) else
               "bianca" if frm and all(domain(a) in internal for a in frm) else "customer")
        ikind = (brief_v2.item_kind(r["type"], r["due_date"], spk, r["quote"], side_for(r))
                 if r["type"] in ("commitment", "next_step") and r["owner_side"] == "bianca" else None)
        _, text = brief_v2.describe_change(r["type"], r["owner_side"], ikind, title_only, r["source"], r["text"],
                                           rec["subject"], event, spk)
        if gate and ikind == "owed" and not title_only:
            text = f"Commitment recorded: {brief_v2.first_sentence(r['text'], 120)}"
        changes.append(brief_v2.ChangeRow(d["name"], d["stage_rank"], text,
                                          f"{SOURCE_LABEL.get(r['source'], r['source'])} · {event:%b} {event.day}",
                                          _link_for(r["source"], r["unit_id"], r["meta"]), kind, event,
                                          title_only=title_only))
    seen: set[tuple[str, str]] = set()
    for rec in reversed(new_records):  # plain facts only where nothing was extracted, dated the same way
        key, d = (rec["source"], rec["thread_id"]), deals.get(str(rec["company_id"]))
        if not d or key in explained or key in seen or rec["source"] not in ("gmail", "notes"):
            continue
        seen.add(key)
        kind = brief_v2.classify_event(rec["occurred_at"], rec["created_at"], since, now, lookback,
                                       not (rec["meta"] or {}).get("date_inferred"))
        if kind not in ("new", "late"):
            continue
        if rec["source"] == "gmail":
            senders = [x["address"] for x in (rec["participants"] or {}).get("from") or []]
            if not senders or any(domain(a) in internal for a in senders) or brief_v2.is_automated(senders, rec["subject"]):
                continue  # ours, or an out-of-office/notification: nobody replied
            text = f"Customer replied (nothing new extracted): {rec['subject'] or '(no subject)'}"
        else:
            text = f"Meeting notes added (nothing new extracted): {rec['subject'] or '(no subject)'}"
        changes.append(brief_v2.ChangeRow(d["name"], d["stage_rank"], text, SOURCE_LABEL[rec["source"]],
                                          _link_for(rec["source"], rec["thread_id"], rec["meta"]), kind,
                                          _event_day(rec, tz), title_only=True))
    changes = brief_v2.select_changes(changes)
    backfill_events = [(r, rec) for r, rec, kind in events if kind == "backfill"]
    undated_events = sum(1 for _, _, kind in events if kind == "undated")
    backfill = None
    if backfill_events:
        dates = sorted(_event_day(rec, tz) for _, rec in backfill_events)
        backfill = (f"Not shown as new: {len(backfill_events)} older development(s) newly recorded "
                    f"(events from {dates[0]:%b} {dates[0].day} to {dates[-1]:%b} {dates[-1].day}, "
                    f"more than {lookback} days before the last brief).")
    if undated_events:
        backfill = (backfill + " " if backfill else "") + f"{undated_events} without a reliable event date."

    coverage = ("Sources: pipeline Sheet, Ramiro's and Nico's Gmail and Calendar, meeting notes. Not connected: "
                "WhatsApp, so missing activity is not proof nothing happened." if NICO_INBOX_CONNECTED else
                "Sources: pipeline Sheet, Ramiro's Gmail and Calendar, meeting notes. Not connected: Nico's inbox "
                "and calendar, WhatsApp, so missing activity is not proof nothing happened.")
    labels = brief_v2.completable(sections, ndl)  # ref -> (deal, text) for each row's "Mark as complete" link
    action_url = (lambda ref: actions.link(ref, *labels[ref])) if settings.brief_action_secret else None
    client = None if gate else _claude()   # verified builds keep wording stable: deterministic cut only
    display = prepare_table_text(brief_v2.table_text_fields(sections, ndl, changes), client, settings.anthropic_model)
    html_body = brief_v2.render(sections, ndl, changes, today, len(deals), coverage, not NICO_INBOX_CONNECTED,
                                backfill, action_url, ndl_review, display=display)
    text_body = brief_v2.plain_text(sections, today, ndl_review, display=display)
    subj = brief_v2.subject(sections, today)
    to = settings.brief_to or [t["email"] for t in team if t["receives_brief"]]
    payload = brief_v2.to_payload(sections, ndl, changes)
    if gate:
        payload["verification"] = gate.audit
        payload["ndl_verification"] = ndl_review.sections()
    payload["change_audit"] = [{"deal": deals[str(r["company_id"])]["name"], "type": r["type"], "text": r["text"],
                                "event": _event_day(rec, tz).isoformat(),
                                "stored": rec["created_at"].astimezone(tz).isoformat(), "class": kind}
                               for r, rec, kind in events]
    payload["change_cutoff"] = since.astimezone(tz).isoformat()
    log.info("brief v2 built for %s: items=%s changes=%s dry_run=%s", today, len(items), len(changes), dry_run)
    response = {"subject": subj, "html": html_body, "text": text_body, "to": ",".join(to), "for_date": today.isoformat(),
                "candidates": len(items), "dry_run": dry_run, "version": "v2", "payload": json.loads(_dumps(payload))}
    return _finish(workspace_id, today, payload, to, response, dry_run, gate, pipeline_sync_run_id)
