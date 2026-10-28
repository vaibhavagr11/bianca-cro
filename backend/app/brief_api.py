"""POST /brief/build: assemble today's brief from the commercial memory. n8n sends the returned email."""

import json
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import anthropic
from fastapi import APIRouter, Header
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import brief, brief_v2, db, timeline
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
def build_brief(dry_run: bool = False, version: str = "v1", x_ingest_token: str | None = Header(default=None)) -> dict:
    require_token(x_ingest_token)
    if version == "v2":
        return build_brief_v2(dry_run)
    return build_brief_v1(dry_run)


def build_brief_v1(dry_run: bool = False, since: datetime | None = None) -> dict:
    """The production brief. `since` overrides the change window for side-by-side replays only."""
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
        since = since or (last["created_at"] if last else now - timedelta(hours=24))

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


# ---------------------------------------------------------------- v2 (deal-grouped, owner-checked, stage-aware)

SOURCE_LABEL = {"gmail": "Email", "notes": "Notes", "calendar": "Meeting"}
NICO_INBOX_CONNECTED = False  # only Ramiro's Gmail/Calendar are ingested today


def _link_for(source: str, unit_id: str | None, meta: dict | None) -> str | None:
    if source == "gmail" and unit_id:
        return timeline.GMAIL_THREAD_URL.format(unit_id)
    if source == "notes" and (meta or {}).get("doc_id"):
        return timeline.DOC_URL.format(meta["doc_id"])
    return None


def _event_day(rec: dict, tz) -> "date":
    """The day the source event happened. Notes carry a date only (stored as midnight UTC), so no zone shift."""
    return rec["occurred_at"].date() if rec["source"] == "notes" else rec["occurred_at"].astimezone(tz).date()


def build_brief_v2(dry_run: bool = False, since: datetime | None = None) -> dict:
    """Same data, safeguards and model calls as v1; compact deal-level selection (see brief_v2).

    `since` overrides the change window for side-by-side replays only.
    """
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
        team_by_name = {brief_v2.norm(t["full_name"]): t["owner_code"] for t in team}
        display = {code: DISPLAY[code][0] for code in DISPLAY}
        last = conn.execute("select created_at from briefs where workspace_id = %s order by created_at desc limit 1",
                            (workspace_id,)).fetchone()
        since = since or (last["created_at"] if last else now - timedelta(hours=24))

        deals = {str(d["company_id"]): d for d in conn.execute(
            "select o.id, o.company_id, c.name, o.stage, o.stage_rank, o.owner_code from opportunities o"
            " join companies c on c.id = o.company_id where o.workspace_id = %s and o.removed_at is null",
            (workspace_id,))}
        profiles = {str(r["opportunity_id"]): r for r in conn.execute(
            "select distinct on (opportunity_id) opportunity_id, profile, generated_at from opportunity_profiles"
            " where workspace_id = %s order by opportunity_id, generated_at desc", (workspace_id,))}
        ins_rows = conn.execute(
            "select i.id, i.opportunity_id, i.type, i.text, i.quote, i.owner_side, i.owner_name, i.due_date, i.status,"
            " i.source, i.unit_id, i.occurred_at, i.created_at, i.company_id, i.source_record_ids, m.meta from insights i"
            " left join lateral (select meta from source_records r where r.id = i.source_record_ids[1]) m on true"
            " where i.workspace_id = %s", (workspace_id,)).fetchall()
        newest_insight: dict[str, datetime] = {}
        for r in ins_rows:
            k = str(r["opportunity_id"])
            if r["created_at"] and (k not in newest_insight or r["created_at"] > newest_insight[k]):
                newest_insight[k] = r["created_at"]
        ins_by_id = {str(r["id"]): r for r in ins_rows}
        open_items = [r for r in ins_rows if r["type"] in ("commitment", "next_step") and r["owner_side"] == "bianca"
                      and r["status"] == "open" and str(r["company_id"]) in deals]
        # Who wrote the passage each open item quotes: Bianca, the customer, or the notes doc.
        recs = {str(r["id"]): r for r in conn.execute(
            "select id, body, participants from source_records where id = any(%s)",
            ([rid for r in open_items for rid in r["source_record_ids"]],))}
        last_contact = {str(r["company_id"]): r["last"].astimezone(tz).date() for r in conn.execute(
            "select company_id, max(occurred_at) as last from source_records where workspace_id = %s"
            " and company_id is not null and occurred_at <= now() group by company_id", (workspace_id,))}
        hidden = {h["owner_code"]: h["n"] for h in conn.execute(
            "select o.owner_code, count(*) as n from insights i join opportunities o on o.company_id = i.company_id"
            " where i.workspace_id = %s and i.type in ('commitment', 'next_step') and i.owner_side = 'bianca'"
            " and i.status = 'possibly_done' group by o.owner_code", (workspace_id,))}

        # Unanswered customer emails (v1 window), newest per deal, with whether the email asks for something.
        unanswered: dict[str, brief_v2.Unanswered] = {}
        for t in conn.execute(
                "select distinct on (thread_id) thread_id, company_id, subject, body, occurred_at, participants"
                " from source_records where workspace_id = %s and source = 'gmail' and company_id is not null"
                " and (classification->>'is_commercial')::bool order by thread_id, occurred_at desc", (workspace_id,)):
            d = deals.get(str(t["company_id"]))
            senders = [p["address"] for p in (t["participants"] or {}).get("from") or []]
            if not (d and senders and all(domain(s) not in internal for s in senders)):
                continue
            c = brief.unanswered_candidate("x", d["name"], d["stage"], d["owner_code"], t["subject"],
                                           t["occurred_at"], now, timeline.GMAIL_THREAD_URL.format(t["thread_id"]))
            if c:
                days = max(1, (now - t["occurred_at"]).days)
                if d["name"] not in unanswered or days < unanswered[d["name"]].days:
                    unanswered[d["name"]] = brief_v2.Unanswered(
                        t["subject"] or "", days, c.link, brief_v2.has_request(f"{t['subject'] or ''} {t['body'] or ''}"))

        start = datetime.combine(today, time.min, tz)
        meetings: list[brief.Meeting] = []
        for m in conn.execute(
                "select subject, occurred_at, participants, company_id from source_records"
                " where workspace_id = %s and source = 'calendar' and occurred_at >= %s and occurred_at < %s"
                " and coalesce(meta->>'status', 'confirmed') <> 'cancelled' and (classification->>'is_commercial')::bool"
                " order by occurred_at", (workspace_id, start, start + timedelta(days=2))):
            d = deals.get(str(m["company_id"])) if m["company_id"] else None
            prof = (profiles.get(str(d["id"])) or {}).get("profile") if d else None
            context = ""
            if prof:
                opens = "; ".join(c["text"] for c in (prof.get("open_commitments") or [])[:2])
                context = f"{prof['summary']['text'][:300]} Open: {opens or 'none'}"
            codes = sorted({email_to_code[a["address"]] for a in (m["participants"] or {}).get("attendees") or []
                            if a.get("address") in email_to_code})
            meetings.append(brief.Meeting(m["occurred_at"].astimezone(tz), m["subject"] or "(no title)",
                                          d["name"] if d else None, d["stage"] if d else None, codes, context))

        # What changed: only records stored since the last brief can hold a development; each is then dated by
        # its source event time (email sent, meeting date), never by when we stored it.
        new_records = conn.execute(
            "select id, source, thread_id, subject, body, company_id, participants, meta, occurred_at, created_at"
            " from source_records where workspace_id = %s and company_id is not null and created_at > %s"
            " and (classification->>'is_commercial')::bool order by occurred_at", (workspace_id, since)).fetchall()
        stage_changes = conn.execute(
            "select c.name, x.field, x.old_value, x.new_value, x.sync_run_id from opportunity_changes x"
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
        return brief_v2.Note(entry.get("action") or entry.get("text") or "", entry.get("why") if with_why else None,
                             verified, entry.get("basis"), link if verified else None)

    views: dict[str, brief_v2.DealView] = {}
    for company_id, d in deals.items():
        prow = profiles.get(str(d["id"]))
        prof = prow["profile"] if prow else {}
        blockers, recs_ = prof.get("blockers") or [], prof.get("recommended_actions") or []
        views[d["name"]] = brief_v2.DealView(
            d["name"], d["stage"], d["stage_rank"], d["owner_code"], last_contact.get(company_id),
            blocker=note(d, blockers[0], prow, False) if blockers else None,
            recommendation=note(d, recs_[0], prow, True) if recs_ else None,
            summary=note(d, prof.get("summary"), prow, False), unanswered=unanswered.get(d["name"]))

    def speaker(r: dict) -> str:
        if r["source"] != "gmail":
            return r["source"]
        quote = " ".join(r["quote"].split()).lower()
        for rid in r["source_record_ids"]:
            rec = recs.get(str(rid))
            if rec and quote in " ".join((rec["body"] or "").split()).lower():
                frm = [p["address"] for p in (rec["participants"] or {}).get("from") or []]
                return "bianca" if frm and all(domain(a) in internal for a in frm) else "customer"
        return "unknown"

    founders = set(FOUNDER_ORDER)
    t1, t2 = settings.brief_today_overdue_days, settings.brief_stale_overdue_days
    items: list[brief_v2.Item] = []
    for r in open_items:
        d = deals[str(r["company_id"])]
        route, basis, delegated = brief_v2.resolve_owner(r["owner_name"], d["owner_code"], team_by_name, founders, display)
        recorded = brief_v2.recorded_on(r["occurred_at"], tz)
        when = r["occurred_at"].astimezone(tz) if r["occurred_at"] else None
        items.append(brief_v2.Item(
            str(r["id"]), d["name"], r["text"], r["due_date"], recorded, r["owner_name"], route, basis, delegated,
            f"{SOURCE_LABEL.get(r['source'], r['source'])} · {when:%b} {when.day}" if when else r["source"],
            _link_for(r["source"], r["unit_id"], r["meta"]),
            brief_v2.bucket(r["due_date"], recorded, today, t1, t2),
            brief_v2.item_kind(r["type"], r["due_date"], speaker(r), r["quote"])))

    # ---- classify every newly extracted insight by the event time of the record its quote comes from
    lookback = settings.brief_late_lookback_days
    by_unit: dict[tuple[str, str], list[dict]] = {}
    for rec in new_records:
        by_unit.setdefault((rec["source"], rec["thread_id"]), []).append(rec)
    events: list[tuple[dict, dict, str]] = []          # (insight, record, new|late|backfill|undated|scheduled)
    for r in ins_rows:
        if not (r["created_at"] and r["created_at"] > since and r["type"] in brief_v2.CHANGE_TYPES
                and str(r["company_id"]) in deals):
            continue
        rec = next((x for x in by_unit.get((r["source"], r["unit_id"]), [])
                    if brief_v2.is_new_insight(r["quote"], f"{x['subject'] or ''} {x['body'] or ''}")), None)
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
            code, full, short, views, items, [(m, "") for m in meetings if code in m.attendees], today, display,
            hidden.get(code, 0), settings.brief_critical_ahead_days, settings.brief_in_talks_target))

    # ---- the existing planner call (unchanged contract), after selection: it only rewords the AI moves already
    # chosen and writes meeting prep lines. It cannot add, remove or reorder deals.
    client = _claude()
    for s in sections:
        moves = [(v, m) for v, m, _ in s.highlights] + list(s.customers)
        picked = [(v, m) for v, m in moves if m.tag == "AI recommendation" and not m.text.endswith("(profile not verified)")]
        ai = {f"C{n}": m for n, (_, m) in enumerate(picked, start=1)}
        cands = [brief.Candidate(f"C{n}", v.deal, v.stage or "", s.code, "recommended", "Recommended next move", m.text, 50)
                 for n, (v, m) in enumerate(picked, start=1)]
        my_meetings = [m for m, _ in s.meetings]
        try:
            plan = brief.plan_for(client, settings.anthropic_model, names.get(s.code, s.code), cands, my_meetings, today)
        except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
            log.warning("brief v2 plan failed for %s: %s", s.code, type(exc).__name__)
            plan = brief.fallback_plan(cands, my_meetings)
        for a in plan.actions:
            if a.candidate_id in ai and a.do_today.strip():
                ai[a.candidate_id].text = a.do_today.strip()
        preps = {p.meeting_index: p.prep for p in plan.preps}
        s.meetings = [(m, preps.get(n, "")) for n, m in enumerate(my_meetings)]

    ndl = [brief_v2.TodayRow(i.deal, views[i.deal].stage, "Due now", i.text, f"due {i.due:%b} {i.due.day}", "Nico DL",
                             i.link, i.id)
           for i in items if i.route == "NDL" and i.kind == "owed" and i.bucket in ("today", "overdue")]
    ndl += [brief_v2.TodayRow(v.deal, v.stage, "Reply needed", f"Customer asked: {v.unanswered.subject}",
                              f"received {v.unanswered.days}d ago", "Nico DL", v.unanswered.link)
            for v in views.values() if v.owner == "NDL" and v.unanswered and brief_v2.reply_label(v.unanswered) == "reply_needed"]
    ndl = ndl[:brief.MAX_NDL]

    changes: list[brief_v2.ChangeRow] = []
    for ch in stage_changes:
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
        side = ({"bianca": " (we owe)", "customer": " (they owe)"}.get(r["owner_side"] or "", "")
                if r["type"] in ("commitment", "next_step") else "")
        event = _event_day(rec, tz)
        changes.append(brief_v2.ChangeRow(d["name"], d["stage_rank"],
                                          f"{brief_v2.CHANGE_TYPES[r['type']]}{side}: {brief_v2.first_sentence(r['text'], 140)}",
                                          f"{SOURCE_LABEL.get(r['source'], r['source'])} · {event:%b} {event.day}",
                                          _link_for(r["source"], r["unit_id"], r["meta"]), kind, event))
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
            if not senders or any(domain(a) in internal for a in senders):
                continue
            text = f"Customer replied (nothing new extracted): {(rec['subject'] or '')[:60]}"
        else:
            text = f"Meeting notes added (nothing new extracted): {(rec['subject'] or '')[:60]}"
        changes.append(brief_v2.ChangeRow(d["name"], d["stage_rank"], text, SOURCE_LABEL[rec["source"]],
                                          _link_for(rec["source"], rec["thread_id"], rec["meta"]), kind,
                                          _event_day(rec, tz)))
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

    coverage = ("Sources: pipeline Sheet, Ramiro's Gmail and Calendar, meeting notes. Not connected: Nico's inbox "
                "and calendar, WhatsApp, so missing activity is not proof nothing happened.")
    html_body = brief_v2.render(sections, ndl, changes, today, len(deals), coverage, not NICO_INBOX_CONNECTED,
                                backfill)
    text_body = brief_v2.plain_text(sections, today)
    subj = brief_v2.subject(sections, today)
    to = settings.brief_to or [t["email"] for t in team if t["receives_brief"]]
    payload = brief_v2.to_payload(sections, ndl, changes)
    payload["change_audit"] = [{"deal": deals[str(r["company_id"])]["name"], "type": r["type"], "text": r["text"],
                                "event": _event_day(rec, tz).isoformat(),
                                "stored": rec["created_at"].astimezone(tz).isoformat(), "class": kind}
                               for r, rec, kind in events]
    payload["change_cutoff"] = since.astimezone(tz).isoformat()
    if not dry_run:
        with db.connect() as conn:
            conn.execute("insert into briefs (workspace_id, for_date, subject, html, payload, recipients)"
                         " values (%s, %s, %s, %s, %s, %s)",
                         (workspace_id, today, subj, html_body, Jsonb(payload, dumps=_dumps), to))
    log.info("brief v2 built for %s: items=%s changes=%s dry_run=%s", today, len(items), len(changes), dry_run)
    return {"subject": subj, "html": html_body, "text": text_body, "to": ",".join(to), "for_date": today.isoformat(),
            "candidates": len(items), "dry_run": dry_run, "version": "v2", "payload": json.loads(_dumps(payload))}
