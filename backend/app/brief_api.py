"""POST /brief/build: assemble today's brief from the commercial memory. n8n sends the returned email."""

import json
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import anthropic
from fastapi import APIRouter, Header
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import brief, db, timeline
from app.config import settings
from app.gmail import domain
from app.ingest import _workspace_id, require_token
from app.process import _claude

log = logging.getLogger("bianca_cro.brief")


def _dumps(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
router = APIRouter(prefix="/brief")

FOUNDER_ORDER = ["RG", "NC"]  # sections in this order; NDL gets the small shared table
# Two founders are named Nicolás, so the brief uses these names, not first names.
DISPLAY = {"RG": ("Ramiro", "Ramiro Godfrid"), "NC": ("Nico", "Nico Casaux"), "NDL": ("Nico DL", "Nico de León")}


@router.post("/build")
def build_brief(dry_run: bool = False, x_ingest_token: str | None = Header(default=None)) -> dict:
    require_token(x_ingest_token)
    now = timeline.utcnow()
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
        since = last["created_at"] if last else now - timedelta(hours=24)

        deals = {str(d["company_id"]): d for d in conn.execute(
            "select o.id, o.company_id, c.name, o.stage, o.stage_rank, o.owner_code from opportunities o"
            " join companies c on c.id = o.company_id where o.workspace_id = %s and o.removed_at is null",
            (workspace_id,))}
        profiles = {str(r["opportunity_id"]): r["profile"] for r in conn.execute(
            "select distinct on (opportunity_id) opportunity_id, profile from opportunity_profiles"
            " where workspace_id = %s order by opportunity_id, generated_at desc", (workspace_id,))}

        cands: list[brief.Candidate] = []
        seq = iter(range(1, 10_000))

        def cid() -> str:
            return f"C{next(seq)}"

        # Dated follow-ups Bianca owes (commitments and next steps), overdue up to 30 days or due within 7.
        dated: list[brief.Row] = []
        for i in conn.execute(
                "select company_id, text, due_date, owner_name, source, unit_id, occurred_at, meta from insights i"
                " left join lateral (select meta from source_records r where r.id = i.source_record_ids[1]) m on true"
                " where i.workspace_id = %s and i.type in ('commitment', 'next_step') and i.owner_side = 'bianca'"
                " and i.status = 'open' and i.due_date between %s and %s",
                (workspace_id, today - timedelta(days=brief.OVERDUE_WINDOW_DAYS), today + timedelta(days=brief.WEEK_DAYS))):
            d = deals.get(str(i["company_id"]))
            if not d:
                continue
            when = i["occurred_at"].astimezone(tz)
            label = {"gmail": "Email", "notes": "Notes", "calendar": "Meeting"}.get(i["source"], i["source"])
            link = (timeline.GMAIL_THREAD_URL.format(i["unit_id"]) if i["source"] == "gmail"
                    else timeline.DOC_URL.format((i["meta"] or {}).get("doc_id")) if i["source"] == "notes"
                    and (i["meta"] or {}).get("doc_id") else None)
            dated.append(brief.Row(d["name"], i["text"], i["due_date"], i["owner_name"], d["stage"],
                                   f"{label} · {when:%b} {when.day}", link))
            dated[-1].owner_code = d["owner_code"]

        # Dated items hidden because the completion check found evidence they were done.
        hidden: dict[str, int] = {}
        for h in conn.execute(
                "select o.owner_code, count(*) as n from insights i join opportunities o on o.company_id = i.company_id"
                " where i.workspace_id = %s and i.type in ('commitment', 'next_step') and i.owner_side = 'bianca'"
                " and i.status = 'possibly_done' and i.due_date between %s and %s group by o.owner_code",
                (workspace_id, today - timedelta(days=brief.OVERDUE_WINDOW_DAYS), today + timedelta(days=brief.WEEK_DAYS))):
            hidden[h["owner_code"]] = h["n"]

        # Profile recommendations and blockers.
        for company_id, d in deals.items():
            prof = profiles.get(str(d["id"]))
            if not prof:
                continue
            for a in prof.get("recommended_actions") or []:
                score = {"high": 70, "medium": 50, "low": 30}.get(a.get("urgency"), 30)
                cands.append(brief.Candidate(cid(), d["name"], d["stage"], d["owner_code"], "recommended",
                                             "Recommended next move", f"{a['action']} (why: {a['why']})", score))
            if (d["stage_rank"] or 0) >= 1:
                for b in (prof.get("blockers") or [])[:2]:
                    cands.append(brief.Candidate(cid(), d["name"], d["stage"], d["owner_code"], "blocker",
                                                 "Blocker: " + b["text"][:70].rstrip() + ("…" if len(b["text"]) > 70 else ""),
                                                 f"Blocker: {b['text']}", 60))

        # Unanswered customer emails: thread's latest message is from outside Bianca.
        for t in conn.execute(
                "select distinct on (thread_id) thread_id, company_id, subject, occurred_at, participants"
                " from source_records where workspace_id = %s and source = 'gmail' and company_id is not null"
                " and (classification->>'is_commercial')::bool order by thread_id, occurred_at desc",
                (workspace_id,)):
            d = deals.get(str(t["company_id"]))
            senders = [p["address"] for p in (t["participants"] or {}).get("from") or []]
            if d and senders and all(domain(s) not in internal for s in senders):
                c = brief.unanswered_candidate(cid(), d["name"], d["stage"], d["owner_code"], t["subject"],
                                               t["occurred_at"], now, timeline.GMAIL_THREAD_URL.format(t["thread_id"]))
                if c:
                    cands.append(c)

        # Quiet active deals that still have open items.
        last_activity = {str(r["company_id"]): r["last"] for r in conn.execute(
            "select company_id, max(occurred_at) as last from source_records where workspace_id = %s"
            " and company_id is not null and occurred_at <= now() group by company_id", (workspace_id,))}
        for company_id, d in deals.items():
            prof = profiles.get(str(d["id"])) or {}
            open_items = len(prof.get("open_commitments") or []) + len(prof.get("blockers") or [])
            la = last_activity.get(company_id)
            c = brief.quiet_candidate(cid(), d["name"], d["stage"], d["stage_rank"], d["owner_code"],
                                      la.astimezone(tz).date() if la else None, open_items, today)
            if c:
                cands.append(c)

        # Meetings today and tomorrow (local time), not cancelled.
        start = datetime.combine(today, time.min, tz)
        meetings: list[brief.Meeting] = []
        for m in conn.execute(
                "select subject, occurred_at, participants, company_id, meta from source_records"
                " where workspace_id = %s and source = 'calendar' and occurred_at >= %s and occurred_at < %s"
                " and coalesce(meta->>'status', 'confirmed') <> 'cancelled' and (classification->>'is_commercial')::bool"
                " order by occurred_at", (workspace_id, start, start + timedelta(days=2))):
            d = deals.get(str(m["company_id"])) if m["company_id"] else None
            prof = profiles.get(str(d["id"])) if d else None
            context = ""
            if prof:
                opens = "; ".join(c["text"] for c in (prof.get("open_commitments") or [])[:2])
                context = f"{prof['summary']['text'][:300]} Open: {opens or 'none'}"
            codes = sorted({email_to_code[a["address"]] for a in (m["participants"] or {}).get("attendees") or []
                            if a.get("address") in email_to_code})
            meetings.append(brief.Meeting(m["occurred_at"].astimezone(tz), m["subject"] or "(no title)",
                                          d["name"] if d else None, d["stage"] if d else None, codes, context))

        # Changes since the last brief.
        changes: list[brief.Change] = []
        first_pipeline_run = conn.execute(
            "select id from sync_runs where workspace_id = %s and source = 'pipeline' and status = 'succeeded'"
            " order by started_at limit 1", (workspace_id,)).fetchone()
        for ch in conn.execute(
                "select c.name, o.owner_code, x.field, x.old_value, x.new_value, x.sync_run_id from opportunity_changes x"
                " join opportunities o on o.id = x.opportunity_id join companies c on c.id = o.company_id"
                " where x.workspace_id = %s and x.detected_at > %s and x.field in ('stage', 'created', 'removed', 'owner_code')"
                " order by x.detected_at desc", (workspace_id, since)):
            if ch["field"] == "created" and first_pipeline_run and ch["sync_run_id"] == first_pipeline_run["id"]:
                continue
            text = {"created": "New deal in pipeline", "removed": "Removed from pipeline"}.get(
                ch["field"], f"{'Stage' if ch['field'] == 'stage' else 'Owner'}: {ch['old_value']} → {ch['new_value']}")
            changes.append(brief.Change(ch["name"], text, ch["owner_code"]))
        for r in conn.execute(
                "select distinct on (source, thread_id) source, thread_id, subject, company_id, participants, meta"
                " from source_records where workspace_id = %s and company_id is not null and occurred_at > %s"
                " and occurred_at <= now() and (classification->>'is_commercial')::bool"
                " order by source, thread_id, occurred_at desc", (workspace_id, since)):
            d = deals.get(str(r["company_id"]))
            if not d:
                continue
            if r["source"] == "gmail":
                senders = [p["address"] for p in (r["participants"] or {}).get("from") or []]
                if senders and all(domain(s) not in internal for s in senders):
                    changes.append(brief.Change(d["name"], f"Customer replied: {(r['subject'] or '')[:60]}",
                                                d["owner_code"], timeline.GMAIL_THREAD_URL.format(r["thread_id"])))
            elif r["source"] == "notes":
                changes.append(brief.Change(d["name"], f"New meeting notes: {(r['subject'] or '')[:60]}", d["owner_code"],
                                            timeline.DOC_URL.format((r["meta"] or {}).get("doc_id", ""))))
        for r in conn.execute(
                "select subject, company_id from source_records where workspace_id = %s and source = 'calendar'"
                " and meta->>'status' = 'cancelled' and updated_at > %s and occurred_at >= now()",
                (workspace_id, since)):
            d = deals.get(str(r["company_id"])) if r["company_id"] else None
            changes.append(brief.Change(d["name"] if d else "-", f"Meeting cancelled: {(r['subject'] or '')[:60]}",
                                        d["owner_code"] if d else None))

    # Per-founder sections.
    names = {t["owner_code"]: t["full_name"] for t in team}
    client = _claude()
    sections, payload = [], {"sections": [], "ndl": [], "changes": []}
    for code in FOUNDER_ORDER:
        mine = [c for c in cands if c.owner_code == code]
        my_meetings = [m for m in meetings if code in m.attendees]
        try:
            plan = brief.plan_for(client, settings.anthropic_model, names.get(code, code), mine, my_meetings, today)
        except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
            log.warning("brief plan failed for %s: %s", code, type(exc).__name__)
            plan = brief.fallback_plan(mine, my_meetings)
        todo, week = brief.split_dated([r for r in dated if r.owner_code == code], today)
        todo += brief.priority_rows(brief.resolve_plan(plan, mine))
        preps = {p.meeting_index: p.prep for p in plan.preps}
        meet_rows = [(m, preps.get(n, "")) for n, m in enumerate(my_meetings)]
        short, full = DISPLAY.get(code, (code, names.get(code, code)))
        sections.append({"name": full, "short": short, "today": todo, "week": week, "meetings": meet_rows,
                         "hidden": hidden.get(code, 0)})
        payload["sections"].append({
            "owner": code,
            "today": [{"company": r.company, "what": r.what, "due": r.due, "why": r.why, "source": r.source}
                      for r in todo],
            "week": [{"company": r.company, "what": r.what, "due": r.due, "source": r.source} for r in week],
            "meetings": [{"at": m.at.isoformat(), "title": m.title, "deal": m.deal, "prep": p} for m, p in meet_rows],
        })
    ndl_today, _ = brief.split_dated([r for r in dated if r.owner_code == "NDL"], today)
    ndl_unanswered = [brief.Row(c.deal, c.detail, None, None, c.stage, c.source or "Email", c.link)
                      for c in cands if c.owner_code == "NDL" and c.kind == "unanswered_email"]
    ndl = (ndl_today + ndl_unanswered)[:brief.MAX_NDL]
    payload["ndl"] = [{"company": r.company, "what": r.what, "due": r.due} for r in ndl]
    payload["changes"] = [{"deal": c.deal, "text": c.text, "owner": c.owner_code} for c in changes[:brief.MAX_CHANGES]]

    html_body = brief.render(sections, ndl, changes, today, len(deals))
    text_body = brief.plain_text(sections, today)
    subj = brief.subject(sections, today)
    to = settings.brief_to or [t["email"] for t in team if t["receives_brief"]]
    if not dry_run:
        with db.connect() as conn:
            conn.execute("insert into briefs (workspace_id, for_date, subject, html, payload, recipients)"
                         " values (%s, %s, %s, %s, %s, %s)",
                         (workspace_id, today, subj, html_body, Jsonb(payload, dumps=_dumps), to))
    log.info("brief built for %s: candidates=%s meetings=%s changes=%s dry_run=%s",
             today, len(cands), len(meetings), len(changes), dry_run)
    return {"subject": subj, "html": html_body, "text": text_body, "to": ",".join(to), "for_date": today.isoformat(),
            "candidates": len(cands), "dry_run": dry_run}
