"""Bounded sales reads for the founder connector, using its restricted database session."""

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from app import brief_v2, extractor, gmail, pipeline, timeline

MAX_LIMIT = 25
PROSPECT_STAGES = (1, 2, 3)
PILOT_STAGE = 4
MAX_EXCERPT = 350
SPEAKER_LINE = re.compile(r"^\s*(?:[-*•]\s+)?([^:\n]{2,100}):\s*(\S.*)$")
EMAIL_LABEL = re.compile(r"[^\s<>@]+@[^\s<>@]+")
ATTENDANCE_LINE = re.compile(r"^\s*[-*•# ]*(?:attendees?|participants?|asistentes?|participantes?|presentes)\b", re.I)
BUYER_ACTION = re.compile(
    r"^\s*[\"“]?(?:(?:yes|agreed|confirmed|sí|de acuerdo)[,!.:]\s+(?:let['’]s|sigamos con)"
    r"|(?:(?:yes|agreed|confirmed|great|perfect|sí|de acuerdo|perfecto)[,!.:]\s+)?"
    r"(?:(?:i|we)(?:\s+(?:will|commit to|agree(?:d)? to|confirm(?:ed)?|accept(?:ed)?)|['’]ll)"
    r"|(?:me|nos) compromet(?:o|emos|imos) a|(?:voy|vamos) a|confirmo|confirmamos|acordamos|aceptamos))"
    r"\s+(?!(?:not|no|never|try|consider|maybe|be unable|have to|decline|cancel|postpone|pause|wait|stop|"
    r"intentar|considerar|cancelar|posponer|esperar|rechazar)\b)\w", re.I,
)
CONDITIONAL_ACTION = re.compile(r"\b(?:if|unless|provided that|assuming|subject to|si|salvo|sujeto a)\b", re.I)
DEAL_FIELDS = """o.id, o.company_id, c.name as company, c.country, c.normalized_name,
    o.stage, o.stage_rank, o.owner_code, o.primary_buyer, o.last_progress, o.next_step"""
DEAL_TABLES = """public.opportunities o
    join public.companies c on c.id = o.company_id and c.workspace_id = o.workspace_id"""


def _limit(value: int, maximum: int = MAX_LIMIT) -> int:
    if not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _deal_output(row: dict) -> dict:
    return {
        "opportunity_id": str(row["id"]), "company": row["company"], "country": row["country"],
        "stage": row["stage"], "stage_rank": row["stage_rank"], "owner_code": row["owner_code"],
        "primary_buyer": (row["primary_buyer"] or "")[:250] or None,
        "last_progress": (row["last_progress"] or "")[:500] or None,
        "next_step": (row["next_step"] or "")[:500] or None,
    }


def search_pipeline(conn, workspace_id, query: str | None = None, owner_code: str | None = None,
                    stage: str | int | None = None, limit: int = 10) -> dict:
    """Search the active pipeline, placing exact normalized account names first."""
    _limit(limit)
    name = pipeline.normalize_name(query) if query else ""
    rows = conn.execute(
        f"""select {DEAL_FIELDS} from {DEAL_TABLES}
            where o.workspace_id = %(workspace)s and o.removed_at is null
              and (%(owner)s::text is null or o.owner_code = %(owner)s)
              and (%(stage)s::text is null or o.stage_rank::text = %(stage)s
                   or lower(btrim(o.stage)) = %(stage)s
                   or regexp_replace(lower(btrim(o.stage)), '^\\d+[.)-]\\s*', '') = %(stage)s)
              and strpos(c.normalized_name, %(name)s) > 0
            order by (c.normalized_name = %(name)s) desc,
                     (strpos(c.normalized_name, %(name)s) = 1) desc, c.normalized_name, o.id
            limit %(limit)s""",
        {"workspace": workspace_id, "owner": owner_code.strip().upper() if owner_code else None,
         "stage": str(stage).strip().lower() if stage is not None else None, "name": name, "limit": limit + 1},
    ).fetchall()
    return {"status": "ok", "opportunities": [_deal_output(row) for row in rows[:limit]],
            "truncated": len(rows) > limit}


def _account(conn, workspace_id, opportunity_id: UUID) -> dict | None:
    return conn.execute(
        f"select {DEAL_FIELDS} from {DEAL_TABLES} where o.workspace_id = %s and o.id = %s and o.removed_at is null",
        (workspace_id, opportunity_id),
    ).fetchone()


def _workspace(conn, workspace_id) -> tuple[set[str], ZoneInfo]:
    row = conn.execute(
        "select internal_domains, timezone from public.workspaces where id = %s", (workspace_id,),
    ).fetchone()
    return {domain.lower() for domain in row["internal_domains"]}, ZoneInfo(row["timezone"])


def _contacts(conn, workspace_id, company_ids: list) -> dict:
    contacts = defaultdict(list)
    for row in conn.execute(
        "select company_id, name, email from public.contacts where workspace_id = %s and company_id = any(%s)",
        (workspace_id, company_ids),
    ):
        contacts[row["company_id"]].append(row)
    return contacts


def _domains(conn, workspace_id, company_ids: list) -> dict:
    domains = defaultdict(set)
    for row in conn.execute(
        "select company_id, domain from public.company_domains where workspace_id = %s"
        " and company_id = any(%s) and trusted",
        (workspace_id, company_ids),
    ):
        domains[row["company_id"]].add(row["domain"].lower())
    return domains


def _email_addresses(participants: dict, fields: tuple[str, ...]) -> set[str]:
    return {person["address"].strip().lower() for field in fields for person in participants.get(field) or []
            if person.get("address") and "@" in person["address"]}


def _email_direction(row: dict, internal: set[str], contacts: set[str], domains: set[str]) -> str | None:
    participants = row["participants"] or {}
    senders = _email_addresses(participants, ("from",))
    recipients = _email_addresses(participants, ("to", "cc"))
    if not senders or brief_v2.is_automated(list(senders), row["subject"]):
        return None

    def from_bianca(address: str) -> bool:
        return gmail.domain(address) in internal

    def from_customer(address: str) -> bool:
        return (not from_bianca(address) and not brief_v2.AUTO_SENDER_RE.search(address)
                and (address in contacts or gmail.domain(address) in domains))

    if all(from_customer(sender) for sender in senders) and any(from_bianca(address) for address in recipients):
        return "inbound"
    if all(from_bianca(sender) for sender in senders) and any(from_customer(address) for address in recipients):
        return "outbound"
    return None


def _source(row: dict, timezone: ZoneInfo, aliases: dict, viewer_mailbox: str | None) -> dict:
    meta = row["meta"] or {}
    url = None
    if row["source"] == "gmail" and viewer_mailbox and row["thread_id"]:
        mailboxes = meta.get("mailboxes") or []
        if viewer_mailbox in mailboxes:
            thread_id = aliases.get(row["thread_id"])
            if thread_id is None and len(mailboxes) == 1:
                thread_id = row["thread_id"]
            if thread_id:
                url = timeline.GMAIL_THREAD_URL.format(quote(thread_id, safe=""))
    if row["source"] == "notes" and meta.get("doc_id"):
        url = timeline.DOC_URL.format(quote(meta["doc_id"], safe=""))
    occurred_at = row["occurred_at"]
    # Notes record dates, with a midnight database timestamp. Do not convert that midnight to another date.
    when = (occurred_at.date().isoformat() if row["source"] == "notes"
            else occurred_at.astimezone(timezone).isoformat()) if occurred_at else None
    return {"source": row["source"], "record_id": str(row["id"]), "title": (row["subject"] or "")[:250],
            "occurred_at": when, "date_estimated": bool(meta.get("date_inferred")), "url": url}


def _aliases(conn, workspace_id, viewer_mailbox: str | None, rows: list[dict]) -> dict:
    threads = sorted({row["thread_id"] for row in rows if row["source"] == "gmail" and row["thread_id"]})
    if not viewer_mailbox or not threads:
        return {}
    return {row["canonical_thread_id"]: row["thread_id"] for row in conn.execute(
        "select canonical_thread_id, min(thread_id) as thread_id from public.gmail_thread_aliases"
        " where workspace_id = %s and mailbox = %s and canonical_thread_id = any(%s) group by canonical_thread_id"
        " having count(*) = 1",
        (workspace_id, viewer_mailbox, threads),
    )}


def search_sales_evidence(conn, workspace_id, query: str | None = None, opportunity_id: UUID | None = None,
                          owner_code: str | None = None, stages: list[int] | None = None, source: str | None = None,
                          insight_type: str | None = None, evidence_kind: str | None = None,
                          since: date | None = None, until: date | None = None, limit: int = 10,
                          offset: int = 0, viewer_mailbox: str | None = None) -> dict:
    """Search commercial records and grounded insights with a stable evidence-level order."""
    _limit(limit)
    if not 0 <= offset <= 2_147_483_647:
        raise ValueError("offset must be between 0 and 2147483647")
    if since and until and since > until:
        raise ValueError("since must not be after until")
    if stages is not None and (not stages or any(stage not in range(6) for stage in stages)):
        raise ValueError("stages must contain pipeline stage ranks between 0 and 5")
    if source not in (None, "gmail", "notes", "calendar"):
        raise ValueError("invalid source")
    if evidence_kind not in (None, "source", "insight") or evidence_kind == "source" and insight_type:
        raise ValueError("an insight type can only filter insight evidence")
    _, timezone = _workspace(conn, workspace_id)
    now = timeline.utcnow()
    params = {"workspace": workspace_id, "opportunity": opportunity_id, "stages": stages,
              "owner": owner_code.strip().upper() if owner_code else None, "source": source, "insight_type": insight_type,
              "kind": evidence_kind, "query": (query or "").strip() or None, "now": now, "timezone": timezone.key,
              "since": since, "until": until, "limit": limit, "offset": offset, "excerpt_length": MAX_EXCERPT}
    rows = conn.execute(
        f"""with accounts as (
                select o.id as opportunity_id, o.company_id, c.name as company, o.stage, o.owner_code
                from {DEAL_TABLES}
                where o.workspace_id = %(workspace)s and o.removed_at is null
                  and (%(opportunity)s::uuid is null or o.id = %(opportunity)s)
                  and (%(owner)s::text is null or o.owner_code = %(owner)s)
                  and (%(stages)s::smallint[] is null or o.stage_rank = any(%(stages)s))
            ), candidates as (
                select a.opportunity_id, a.company, a.stage, a.owner_code, 'source'::text as kind,
                       r.id, r.source, r.thread_id, r.occurred_at, r.subject, r.meta,
                       null::uuid as insight_id, null::text as insight_type, null::text as insight_summary,
                       null::text as confidence,
                       case when %(query)s::text is null then left(r.body, %(excerpt_length)s)
                            when match.body_position > 0 then substring(r.body from greatest(1, match.body_position - 80)
                                                                        for %(excerpt_length)s) end as excerpt
                from accounts a join public.source_records r on r.company_id = a.company_id
                cross join lateral (select strpos(lower(r.body), lower(%(query)s::text)) as body_position) match
                where r.workspace_id = %(workspace)s and r.status = 'classified'
                  and r.classification->'is_commercial' = 'true'::jsonb
                  and %(insight_type)s::text is null and %(kind)s::text is distinct from 'insight'
                  and (%(source)s::text is null or r.source = %(source)s)
                  and (%(query)s::text is null or match.body_position > 0
                       or strpos(lower(r.subject), lower(%(query)s)) > 0)
                union all
                select a.opportunity_id, a.company, a.stage, a.owner_code, 'insight'::text as kind,
                       r.id, r.source, r.thread_id, r.occurred_at, r.subject, r.meta,
                       i.id, i.type, left(i.text, %(excerpt_length)s), i.confidence, left(i.quote, %(excerpt_length)s)
                from accounts a join public.insights i on i.opportunity_id = a.opportunity_id
                     and i.company_id = a.company_id
                join lateral (
                    select record.id, record.source, record.thread_id, record.occurred_at, record.subject, record.meta,
                           count(*) over () as supporting_records
                    from public.source_records record
                    where record.workspace_id = i.workspace_id and record.company_id = i.company_id
                      and record.source = i.source and record.id = any(i.source_record_ids)
                      and (i.origin_record_id is null or record.id = i.origin_record_id)
                      and record.status = 'classified' and record.classification->'is_commercial' = 'true'::jsonb
                      and strpos(regexp_replace(lower(record.body), '[[:space:]]+', ' ', 'g'),
                                 nullif(btrim(regexp_replace(lower(i.quote), '[[:space:]]+', ' ', 'g')), '')) > 0
                    order by record.id limit 1
                ) r on r.supporting_records = 1
                where i.workspace_id = %(workspace)s and i.status <> 'dismissed'
                  and (i.action_scope = 'deal_progression' or i.action_scope is null)
                  and %(kind)s::text is distinct from 'source'
                  and (%(insight_type)s::text is null or i.type = %(insight_type)s)
                  and (%(source)s::text is null or i.source = %(source)s)
                  and (%(query)s::text is null or strpos(lower(i.text), lower(%(query)s)) > 0
                       or strpos(lower(i.quote), lower(%(query)s)) > 0)
            ), dated as (
                select *, case when source = 'notes' then occurred_at::date
                               else (occurred_at at time zone %(timezone)s)::date end as event_date
                from candidates
            ), matches as (
                select * from dated
                where (%(since)s::date is null or event_date >= %(since)s)
                  and (%(until)s::date is null or event_date <= %(until)s)
                  and (occurred_at is null or occurred_at <= %(now)s
                       or (source = 'calendar' and kind = 'source' and %(until)s::date is not null))
            ), totals as (
                select count(*) as total_matches, count(distinct id) as distinct_source_records,
                       count(distinct opportunity_id) as distinct_opportunities,
                       count(*) filter (where kind = 'insight') as insight_matches,
                       count(*) filter (where kind = 'source') as source_matches
                from matches
            ), by_type as (
                select coalesce(jsonb_object_agg(insight_type, hits), '{{}}'::jsonb) as counts_by_insight_type
                from (select insight_type, count(*) as hits from matches where kind = 'insight' group by insight_type) grouped
            )
            select page.*, totals.*, by_type.counts_by_insight_type
            from totals cross join by_type left join lateral (
                select * from matches
                order by occurred_at desc nulls last, kind, id, insight_id nulls last
                limit %(limit)s offset %(offset)s
            ) page on true
            order by page.occurred_at desc nulls last, page.kind, page.id, page.insight_id nulls last""",
        params,
    ).fetchall()

    totals = rows[0]
    page = [row for row in rows if row["id"] is not None]
    aliases = _aliases(conn, workspace_id, viewer_mailbox, page)
    evidence = []
    for row in page:
        item = {**_source(row, timezone, aliases, viewer_mailbox), "kind": row["kind"],
                "opportunity_id": str(row["opportunity_id"]), "company": row["company"],
                "stage": row["stage"], "owner_code": row["owner_code"]}
        if row["kind"] == "insight":
            item.update(insight_id=str(row["insight_id"]), insight_type=row["insight_type"],
                        summary=row["insight_summary"], confidence=row["confidence"], quote=row["excerpt"])
        else:
            item["match_basis"] = ("filters" if params["query"] is None else "body" if row["excerpt"] else "title")
            if row["excerpt"]:
                item["excerpt"] = row["excerpt"]
        if row["source"] == "calendar":
            item["calendar_status"] = (row["meta"] or {}).get("status", "unknown")
            item["attendance"] = "unverified"
            item["future"] = bool(row["occurred_at"] and row["occurred_at"] > now)
        evidence.append(item)
    next_offset = offset + len(evidence)
    return {"status": "ok", "evidence": evidence, "total_matches": totals["total_matches"],
            "distinct_source_records": totals["distinct_source_records"],
            "distinct_opportunities": totals["distinct_opportunities"],
            "counts_by_kind": {"source": totals["source_matches"], "insight": totals["insight_matches"]},
            "counts_by_insight_type": totals["counts_by_insight_type"],
            "next_offset": next_offset if next_offset < totals["total_matches"] else None,
            "as_of": now.astimezone(timezone).isoformat()}


def _activity_rows(conn, workspace_id, company_id, source: str, as_of: datetime, limit: int,
                   *, before: tuple | None = None, explicit_date: bool = False) -> list[dict]:
    cutoff, cutoff_id = before or (None, None)
    return conn.execute(
        """select id, source, thread_id, occurred_at, subject, participants, meta from public.source_records
           where workspace_id = %s and company_id = %s and source = %s and status = 'classified'
             and classification->'is_commercial' = 'true'::jsonb and occurred_at <= %s
             and (%s::timestamptz is null or (occurred_at, id) < (%s, %s))
             and (source <> 'calendar' or coalesce(meta->>'status', '') not in ('cancelled', 'canceled'))
             and (not %s or meta->>'date_inferred' is distinct from 'true')
           order by occurred_at desc, id desc limit %s""",
        (workspace_id, company_id, source, as_of, cutoff, cutoff, cutoff_id, explicit_date, limit),
    ).fetchall()


def get_account_activity(conn, workspace_id, opportunity_id: UUID, limit: int = 10,
                         viewer_mailbox: str | None = None, *, as_of: datetime | None = None) -> dict:
    """Read recent customer mail, dated meeting notes, and calendar records separately."""
    _limit(limit)
    account = _account(conn, workspace_id, opportunity_id)
    if not account:
        return {"status": "not_found"}
    now = as_of or timeline.utcnow()
    internal, timezone = _workspace(conn, workspace_id)
    company_id = account["company_id"]
    contacts = {row["email"].lower() for row in _contacts(conn, workspace_id, [company_id])[company_id]}
    domains = _domains(conn, workspace_id, [company_id])[company_id]

    email = []
    before = None
    while (contacts or domains) and len(email) < limit:
        page = _activity_rows(conn, workspace_id, company_id, "gmail", now, 100, before=before)
        if not page:
            break
        for row in page:
            if direction := _email_direction(row, internal, contacts, domains):
                email.append({**row, "direction": direction})
                if len(email) == limit:
                    break
        before = (page[-1]["occurred_at"], page[-1]["id"])
        if len(page) < 100:
            break

    notes = _activity_rows(conn, workspace_id, company_id, "notes", now, limit)
    dated = next((row for row in notes if not (row["meta"] or {}).get("date_inferred")), None)
    if notes and dated is None:
        dated = next(iter(_activity_rows(conn, workspace_id, company_id, "notes", now, 1, explicit_date=True)), None)
    calendar = _activity_rows(conn, workspace_id, company_id, "calendar", now, limit)
    aliases = _aliases(conn, workspace_id, viewer_mailbox, email)

    def activity(row: dict) -> dict:
        item = _source(row, timezone, aliases, viewer_mailbox)
        if row["source"] == "gmail":
            item["direction"] = row["direction"]
        if row["source"] == "calendar":
            item["attendance"] = "unverified"
        return item

    recent = sorted(email + notes + calendar, key=lambda row: (row["occurred_at"], row["id"]), reverse=True)
    return {"status": "ok", "opportunity": _deal_output(account), "as_of": now.astimezone(timezone).isoformat(),
            "has_customer_email_identity": bool(contacts or domains),
            "last_customer_email": activity(email[0]) if email else None,
            "last_meeting_with_dated_notes": activity(dated) if dated else None,
            "last_recorded_note": activity(notes[0]) if notes else None,
            "last_scheduled_calendar_event": activity(calendar[0]) if calendar else None,
            "recent_activity": [activity(row) for row in recent[:limit]]}


def _speaker_key(label: str) -> str:
    clean = label.strip(" *_`\t")
    email = clean.strip("<>").strip()
    return email.lower() if EMAIL_LABEL.fullmatch(email) else pipeline.normalize_name(clean)


def _speaker_lines(body: str | None, speakers: set[str]):
    for line in (body or "").splitlines():
        match = SPEAKER_LINE.match(line)
        if match and _speaker_key(match[1]) in speakers:
            yield match[1].strip(" *_`\t"), match[2]


def _unattributed_lines(body: str | None, speakers: set[str]):
    names = [re.compile(rf"(?<!\w){re.escape(name)}(?!\w)") for name in sorted(speakers) if " " in name]
    emails = [re.compile(rf"(?<![\w.+@-]){re.escape(email)}(?![\w+@-]|\.[\w])")
              for email in sorted(speakers) if EMAIL_LABEL.fullmatch(email)]
    for line in (body or "").splitlines():
        if ATTENDANCE_LINE.match(line):
            continue
        if (any(pattern.search(pipeline.normalize_name(line)) for pattern in names)
                or any(pattern.search(line.lower()) for pattern in emails)):
            yield line.strip()


def find_stakeholder_evidence(conn, workspace_id, opportunity_id: UUID, stakeholder: str, limit: int = 10,
                              since: date | None = None, until: date | None = None) -> dict:
    """Find verbatim meeting-note excerpts explicitly labeled with this stakeholder's name."""
    _limit(limit)
    if since and until and since > until:
        raise ValueError("since must not be after until")
    name = _speaker_key(stakeholder)
    if not name:
        raise ValueError("stakeholder must contain a name or email")
    account = _account(conn, workspace_id, opportunity_id)
    if not account:
        return {"status": "not_found"}
    company_id = account["company_id"]
    contacts = _contacts(conn, workspace_id, [company_id])[company_id]
    short_name = " " not in name and "@" not in name
    if short_name:
        named = [contact for contact in contacts if name in _speaker_key(contact["name"] or "").split()]
        if len(named) != 1:
            return {"status": "ambiguous_stakeholder", "stakeholder": stakeholder,
                    "message": "Use the person's full name or email address."}
    else:
        named = [contact for contact in contacts
                 if name in {_speaker_key(contact["name"] or ""), _speaker_key(contact["email"])}]
    if len(named) > 1:
        return {"status": "ambiguous_stakeholder", "stakeholder": stakeholder,
                "message": "More than one contact shares this name. Use their email address."}
    speakers = {name}
    if named:
        contact_name = _speaker_key(named[0]["name"] or "")
        if contact_name and sum(_speaker_key(c["name"] or "") == contact_name for c in contacts) == 1:
            speakers.add(contact_name)
        speakers.add(_speaker_key(named[0]["email"]))
        if short_name:
            speakers.discard(name)  # A first-name search may resolve a contact but cannot prove authorship of a short label.

    _, timezone = _workspace(conn, workspace_id)
    rows = conn.execute(
        """select id, source, occurred_at, subject, meta, body from public.source_records
           where workspace_id = %s and company_id = %s and source = 'notes' and status = 'classified'
             and classification->'is_commercial' = 'true'::jsonb
             and (occurred_at is null or occurred_at <= now())
             and (%s::date is null or occurred_at >= %s::date)
             and (%s::date is null or occurred_at < %s::date + interval '1 day')
           order by occurred_at desc nulls last, id""",
        (workspace_id, company_id, since, since, until, until),
    )
    evidence = []
    account_context = []
    for row in rows:
        excerpts = [{"speaker_label": label, "quote": excerpt[:MAX_EXCERPT],
                     "quote_truncated": len(excerpt) > MAX_EXCERPT}
                    for label, excerpt in _speaker_lines(row["body"], speakers)]
        if excerpts:
            evidence.append({**_source(row, timezone, {}, None), "excerpts": excerpts[:2]})
            if len(evidence) > limit:
                break
        elif len(account_context) < min(5, limit):
            mention = next(_unattributed_lines(row["body"], speakers), None)
            if mention:
                account_context.append({**_source(row, timezone, {}, None), "quote": mention[:MAX_EXCERPT],
                                        "quote_truncated": len(mention) > MAX_EXCERPT,
                                        "attribution": "Account context only. Speaker not verified for this stakeholder."})
    return {"status": "ok" if evidence else "insufficient_evidence", "opportunity": _deal_output(account),
            "stakeholder": named[0]["name"] if named and named[0]["name"] else stakeholder,
            "evidence": evidence[:limit], "truncated": len(evidence) > limit,
            "account_context": account_context if not evidence else [],
            "attribution": "Explicit speaker labels in stored meeting notes"}


def _is_buyer_action(row: dict) -> bool:
    return (row["owner_side"] == "customer" and row["origin_record_id"] == row["id"]
            and bool(BUYER_ACTION.search(row["quote"])) and not CONDITIONAL_ACTION.search(row["quote"]))


def rank_warm_prospects(conn, workspace_id, owner_code: str | None = None, limit: int = 3,
                        include_pilots: bool = False, lookback_days: int = 60,
                        viewer_mailbox: str | None = None, *, as_of: datetime | None = None) -> dict:
    """Rank active prospects using citable buyer signals and recent open blockers."""
    _limit(limit, 10)
    if not 1 <= lookback_days <= 180:
        raise ValueError("lookback_days must be between 1 and 180")
    now = as_of or timeline.utcnow()
    since = now - timedelta(days=lookback_days)
    owner = owner_code.strip().upper() if owner_code else None
    internal, timezone = _workspace(conn, workspace_id)
    stages = [*PROSPECT_STAGES, PILOT_STAGE] if include_pilots else list(PROSPECT_STAGES)
    deals = {row["id"]: row for row in conn.execute(
        f"""select {DEAL_FIELDS} from {DEAL_TABLES}
            where o.workspace_id = %s and o.removed_at is null and o.stage_rank = any(%s)
              and (%s::text is null or o.owner_code = %s)""",
        (workspace_id, stages, owner, owner),
    )}
    company_ids = [row["company_id"] for row in deals.values()]
    contacts = _contacts(conn, workspace_id, company_ids)
    domains = _domains(conn, workspace_id, company_ids)
    rows = conn.execute(
        """select i.id as insight_id, i.opportunity_id, i.company_id, i.type, i.text, i.quote, i.owner_side,
                  i.origin_record_id, i.confidence, r.id, r.source, r.thread_id, r.occurred_at, r.subject,
                  r.participants, r.meta, r.body
           from public.insights i join public.source_records r
             on r.workspace_id = i.workspace_id and r.company_id = i.company_id and r.source = i.source
             and r.id = any(i.source_record_ids) and (i.origin_record_id is null or r.id = i.origin_record_id)
           where i.workspace_id = %s and i.opportunity_id = any(%s)
             and (i.action_scope = 'deal_progression' or i.action_scope is null)
             and i.confidence in ('high', 'medium')
             and (i.type = 'buying_signal' and i.status <> 'dismissed'
                  or i.type = 'blocker' and i.status = 'open'
                  or i.type in ('commitment', 'next_step') and i.status <> 'dismissed' and i.owner_side = 'customer'
                     and i.origin_record_id is not null and i.action_scope = 'deal_progression')
             and r.status = 'classified' and r.classification->'is_commercial' = 'true'::jsonb
             and r.source in ('gmail', 'notes') and r.body is not null
             and (i.origin_record_id is null or r.occurred_at between %s and %s)
           order by i.id, r.occurred_at desc nulls last, r.id""",
        (workspace_id, list(deals), since, now),
    )

    matches = defaultdict(list)
    for row in rows:
        if (deals[row["opportunity_id"]]["company_id"] == row["company_id"]
                and extractor.quote_in_record(row["quote"], row, body_only=True)):
            matches[row["insight_id"]].append(row)

    signals = defaultdict(dict)
    buyer_actions = defaultdict(dict)
    blockers = defaultdict(dict)
    for supported in matches.values():
        if len(supported) != 1:
            continue  # A legacy quote in several records cannot support a unique source or date.
        row = supported[0]
        occurred_at = row["occurred_at"]
        if not occurred_at or not since <= occurred_at <= now or (row["meta"] or {}).get("date_inferred"):
            continue
        kind = row["type"]
        if kind in ("commitment", "next_step") and not _is_buyer_action(row):
            continue
        account_contacts = contacts[row["company_id"]]
        if row["source"] == "gmail":
            direction = _email_direction(row, internal, {c["email"].lower() for c in account_contacts},
                                         domains[row["company_id"]])
            if direction != "inbound" and (kind != "blocker" or direction is None):
                continue
        elif kind != "blocker":
            buyer_contacts = [c for c in account_contacts if gmail.domain(c["email"].lower()) not in internal
                              and not brief_v2.AUTO_SENDER_RE.search(c["email"])]
            name_counts = Counter(_speaker_key(c["name"] or "") for c in buyer_contacts)
            names = {name for name, count in name_counts.items() if " " in name and count == 1}
            names.update(_speaker_key(c["email"]) for c in buyer_contacts)
            if not any(extractor.quote_in_record(row["quote"], {"body": excerpt}, body_only=True)
                       for _, excerpt in _speaker_lines(row["body"], names)):
                continue
        target = {"buying_signal": signals, "blocker": blockers}.get(kind, buyer_actions)
        # Count distinct source events, rather than duplicate extractions or several lines from the same event.
        previous = target[row["opportunity_id"]].get(row["id"])
        if not previous or (previous["confidence"] == "medium" and row["confidence"] == "high"):
            target[row["opportunity_id"]][row["id"]] = row

    positives = {key: buyer_actions[key] | signals[key] for key in deals if buyer_actions[key] or signals[key]}
    newest = {key: max(row["occurred_at"] for row in group.values()) for key, group in positives.items()}
    ordered = sorted(newest, key=lambda key: (-len(positives[key]), len(blockers[key]), -deals[key]["stage_rank"],
                                              -newest[key].timestamp(), deals[key]["normalized_name"], key))
    selected = ordered[:limit]
    supporting = {key: [sorted(group[key].values(), key=lambda row: (row["occurred_at"], row["id"]), reverse=True)[:3]
                        for group in (signals, buyer_actions, blockers)] for key in selected}
    aliases = _aliases(conn, workspace_id, viewer_mailbox,
                       [row for groups in supporting.values() for group in groups for row in group])

    def evidence(row: dict) -> dict:
        return {**_source(row, timezone, aliases, viewer_mailbox), "insight_id": str(row["insight_id"]),
                "insight_type": row["type"], "summary": row["text"][:MAX_EXCERPT], "quote": row["quote"][:MAX_EXCERPT]}

    ranking = []
    for rank, key in enumerate(selected, 1):
        signal_rows, action_rows, negative = supporting[key]
        ranking.append({"rank": rank, **_deal_output(deals[key]), "buyer_engagement_event_count": len(positives[key]),
                        "buying_signal_count": len(signals[key]), "buyer_action_count": len(buyer_actions[key]),
                        "open_blocker_count": len(blockers[key]), "buying_signals": [evidence(r) for r in signal_rows],
                        "buyer_actions": [evidence(r) for r in action_rows],
                        "open_blockers": [evidence(r) for r in negative]})
    return {"status": "ok" if ranking else "insufficient_evidence", "prospects": ranking,
            "eligible_deal_count": len(deals), "deals_with_buying_evidence": len(newest),
            "include_pilots": include_pilots, "lookback_days": lookback_days, "as_of": now.astimezone(timezone).isoformat(),
            "ranking_rule": "More distinct recent records with buyer signals or confirmed buyer actions, fewer recent "
                            "open blockers, later stage, then more recent buyer evidence and company name."}
