"""Evidence and open questions for founder sales decisions."""

import re
from collections import Counter, defaultdict
from datetime import timedelta
from uuid import UUID

from app import brief_v2, extractor, gmail, sales_queries as sales, timeline

EVIDENCE_DAYS = 180
MAX_REENGAGEMENT_LOOKBACK_DAYS = 730
MAX_QUIET_DAYS = 365
EVIDENCE_PER_DEAL = 100
ACTION_TYPES = ("commitment", "next_step")
BLOCKER_TYPES = ("blocker", "objection")
OUTLOOK_TYPES = (*ACTION_TYPES, *BLOCKER_TYPES, "need", "buying_signal")
TENTATIVE_ACTION = re.compile(
    r"\b(?:pending|depends? on|contingent on|after approval|might|maybe|probably|try to|"
    r"pendiente|depende|sujeto a|tal vez|quiz[aá]s|probablemente|intentar)\b", re.I,
)
QUALIFICATION = (
    ("budget", r"\b(?:budget|presupuesto|funding|financiamiento|funds|fondos)\b",
     "Is there a budget for this initiative, and who controls it?"),
    ("decision_maker", r"\b(?:decision[- ]makers?|decision owner|final decision|final approver|sign[- ]?off|"
     r"who decides|quien decide|decision final|toma la decision|responsable de la decision)\b",
     "Who makes the final decision, and who else needs to approve it?"),
    ("timing", r"\b(?:deadline|timeline|target date|decision date|decision by|by \w+ \d|"
     r"by \d|fecha|plazo|cronograma|calendario)\b",
     "When do you need a decision and when would you want to start?"),
    ("success_criteria", r"\b(?:success|criteria|kpis?|metrics?|baseline|threshold|exito|criterios?|"
     r"metricas?|linea base|umbral)\b",
     "What would count as success, and how will you measure it?"),
    ("buying_process", r"\b(?:procurement|legal review|security review|approval process|purchase order|"
     r"vendor onboarding|compras|revision legal|revision de seguridad|aprobaciones|orden de compra)\b",
     "What steps and approvals are required before you can move forward?"),
)
PILOT_CRITERIA = (
    ("scope_and_start", r"\b(?:pilot scope|pilot start|pilot runs?|start(?:ing)? (?:the |a )?pilot|"
     r"launch(?:ing)? (?:the |a )?pilot|alcance (?:del )?piloto|inicio (?:del )?piloto|"
     r"(?:empezar|iniciar|arrancar) (?:el |un )?piloto)\b",
     "What is in the pilot scope, and what needs to happen before it starts?"),
    QUALIFICATION[3],
    QUALIFICATION[1],
    ("decision_timing", r"\b(?:decision date|decision by|decide by|go[- ]no[- ]go|pilot review|pilot ends?|"
     r"pilot readout|fecha de decision|evaluacion (?:del )?piloto|fin (?:del )?piloto)\b",
     "When will you review pilot results and decide whether to proceed?"),
)


def _deals(conn, workspace_id, stages, owner_code, limit=None):
    owner = owner_code.strip().upper() if owner_code else None
    return conn.execute(
        f"""select {sales.DEAL_FIELDS} from {sales.DEAL_TABLES}
            where o.workspace_id = %s and o.removed_at is null and o.stage_rank = any(%s)
              and (%s::text is null or o.owner_code = %s)
            order by c.normalized_name, o.id limit %s""",
        (workspace_id, list(stages), owner, owner, limit),
    ).fetchall()


def _buyer_identities(conn, workspace_id, company_ids, internal):
    if not company_ids:
        return {}
    contacts = sales._contacts(conn, workspace_id, company_ids)
    domains = sales._domains(conn, workspace_id, company_ids)
    identities = {}
    for company_id in company_ids:
        external = [contact for contact in contacts[company_id]
                    if gmail.domain(contact["email"].lower()) not in internal
                    and not brief_v2.AUTO_SENDER_RE.search(contact["email"])]
        emails = {contact["email"].strip().lower() for contact in external}
        names = Counter(sales._speaker_key(contact["name"] or "") for contact in external)
        speakers = {name for name, count in names.items() if " " in name and count == 1}
        identities[company_id] = (emails, domains[company_id], speakers | emails)
    return identities


def _buyer_attribution(row, internal, identity):
    emails, domains, speakers = identity
    if row["source"] == "gmail":
        if sales._email_direction(row, internal, emails, domains) == "inbound":
            return "direct_customer_email"
    elif any(extractor.quote_in_record(row["quote"], {"body": excerpt}, body_only=True)
             for _, excerpt in sales._speaker_lines(row["body"], speakers)):
        return "named_customer_speaker_in_notes"
    return None


def _grounded_rows(conn, workspace_id, deals, identities, internal, since, until, types=OUTLOOK_TYPES):
    """Resolve each insight to exactly one visible, dated record before attribution."""
    grouped = defaultdict(list)
    if not deals:
        return grouped, set()
    rows = conn.execute(
        """with ranked as (
            select i.id as insight_id, i.opportunity_id, i.company_id, i.type, i.text, i.quote,
                   i.owner_side, i.owner_name, i.due_date, i.status as insight_status, i.action_scope,
                   i.origin_record_id, r.id, r.source, r.thread_id, r.occurred_at, r.subject, r.participants,
                   r.meta, r.body, row_number() over (partition by i.opportunity_id
                                      order by r.occurred_at desc, i.id) as deal_position
            from public.insights i join public.opportunities o
              on o.id = i.opportunity_id and o.workspace_id = i.workspace_id and o.company_id = i.company_id
            join lateral (
                select record.id, record.source, record.thread_id, record.occurred_at, record.subject,
                       record.participants, record.meta, case when record.source = 'notes' then record.body end as body,
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
            where i.workspace_id = %s and i.opportunity_id = any(%s) and o.removed_at is null
              and i.type = any(%s) and i.status <> 'dismissed' and i.confidence in ('high', 'medium')
              and (i.action_scope = 'deal_progression' or i.action_scope is null)
              and r.source in ('gmail', 'notes') and r.occurred_at between %s and %s
              and r.meta->>'date_inferred' is distinct from 'true'
        ) select * from ranked where deal_position <= %s
          order by opportunity_id, deal_position""",
        (workspace_id, [deal["id"] for deal in deals], list(types), since, until, EVIDENCE_PER_DEAL + 1),
    )
    truncated = set()
    seen = set()
    for row in rows:
        opportunity_id = row["opportunity_id"]
        if row["deal_position"] > EVIDENCE_PER_DEAL:
            truncated.add(opportunity_id)
            continue
        key = (opportunity_id, row["id"], *extractor.insight_key(row["type"], row["quote"]))
        if key in seen:
            continue
        seen.add(key)
        row["buyer_attribution"] = _buyer_attribution(row, internal, identities[row["company_id"]])
        grouped[opportunity_id].append(row)
    return grouped, truncated


def _citation(row, timezone, aliases, viewer_mailbox):
    return {
        **sales._source(row, timezone, aliases, viewer_mailbox),
        "insight_id": str(row["insight_id"]), "insight_type": row["type"],
        "summary": row["text"][:sales.MAX_EXCERPT], "quote": row["quote"][:sales.MAX_EXCERPT],
        "quote_truncated": len(row["quote"]) > sales.MAX_EXCERPT,
        "buyer_attribution": row["buyer_attribution"], "recorded_status": row["insight_status"],
        "commercial_scope": row["action_scope"] or "legacy_scope_unverified",
        "recorded_owner_side": row["owner_side"], "recorded_owner_name": (row["owner_name"] or "")[:150] or None,
        "extracted_due_date": row["due_date"].isoformat() if row["due_date"] else None,
    }


def _topic_evidence(rows, topics):
    return {key: [row for row in rows if row["buyer_attribution"]
                  and re.search(pattern, sales.pipeline.normalize_name(row["quote"]), re.I)][:3]
            for key, pattern, _ in topics}


def _topics(matches, topics, cite):
    return {key: {"status": "buyer_evidence_available" if matches[key] else "unknown",
                  "buyer_evidence": [cite(row) for row in matches[key]], "question_if_unresolved": question}
            for key, _, question in topics}


def _explicit_buyer_action(row):
    return (row["type"] in ACTION_TYPES and row["buyer_attribution"] is not None and sales._is_buyer_action(row)
            and not TENTATIVE_ACTION.search(row["quote"]))


def _confirmed_buyer_action(row):
    return row["action_scope"] == "deal_progression" and _explicit_buyer_action(row)


def _pending_actions(rows):
    return [row for row in rows if row["type"] in ACTION_TYPES
            and row["insight_status"] in ("open", "possibly_done")]


def _blockers(rows):
    return [row for row in rows if row["type"] in BLOCKER_TYPES and row["insight_status"] == "open"][:5]


def _upcoming_events(conn, workspace_id, deals, now):
    events = defaultdict(list)
    if deals:
        rows = conn.execute(
            """select * from (
                   select company_id, id, source, thread_id, occurred_at, subject, meta,
                          row_number() over (partition by company_id order by occurred_at, id) as position
                   from public.source_records
                   where workspace_id = %s and company_id = any(%s) and source = 'calendar' and status = 'classified'
                     and classification->'is_commercial' = 'true'::jsonb
                     and occurred_at > %s and occurred_at <= %s
                     and coalesce(meta->>'status', '') not in ('cancelled', 'canceled')
               ) scheduled where position <= 3 order by company_id, position""",
            (workspace_id, [deal["company_id"] for deal in deals], now, now + timedelta(days=90)),
        )
        for row in rows:
            events[row["company_id"]].append(row)
    return events


def _scheduled(row, timezone):
    return {**sales._source(row, timezone, {}, None), "attendance": "unverified",
            "calendar_status": (row["meta"] or {}).get("status", "unknown")}


def get_qualification_gaps(conn, workspace_id, opportunity_id: UUID, viewer_mailbox: str | None = None) -> dict:
    """Return buyer evidence and a discovery question for each qualification dimension."""
    deal = sales._account(conn, workspace_id, opportunity_id)
    if not deal:
        return {"status": "not_found"}
    now = timeline.utcnow()
    internal, timezone = sales._workspace(conn, workspace_id)
    identities = _buyer_identities(conn, workspace_id, [deal["company_id"]], internal)
    rows, truncated = _grounded_rows(conn, workspace_id, [deal], identities, internal,
                                     now - timedelta(days=EVIDENCE_DAYS), now)
    matches = _topic_evidence(rows[opportunity_id], QUALIFICATION)
    aliases = sales._aliases(conn, workspace_id, viewer_mailbox, [row for group in matches.values() for row in group])

    def cite(row):
        return _citation(row, timezone, aliases, viewer_mailbox)

    return {"status": "ok", "opportunity": sales._deal_output(deal),
            "qualification": _topics(matches, QUALIFICATION, cite),
            "as_of": now.astimezone(timezone).isoformat(), "lookback_days": EVIDENCE_DAYS,
            "evidence_truncated": opportunity_id in truncated,
            "interpretation": "Use each buyer quote to establish what they actually stated. A matching topic may "
                              "contain an unanswered question, a condition, or a negative answer."}


def assess_next_milestone(conn, workspace_id, opportunity_id: UUID, viewer_mailbox: str | None = None) -> dict:
    """Return buyer commitments, proposed next steps, blockers and scheduled events separately."""
    deal = sales._account(conn, workspace_id, opportunity_id)
    if not deal:
        return {"status": "not_found"}
    now = timeline.utcnow()
    internal, timezone = sales._workspace(conn, workspace_id)
    identities = _buyer_identities(conn, workspace_id, [deal["company_id"]], internal)
    rows, truncated = _grounded_rows(conn, workspace_id, [deal], identities, internal,
                                     now - timedelta(days=EVIDENCE_DAYS), now, (*ACTION_TYPES, *BLOCKER_TYPES))
    pending = _pending_actions(rows[opportunity_id])
    confirmed = [row for row in pending if _confirmed_buyer_action(row)]
    agreed = [row for row in confirmed if row["insight_status"] == "open"][:5]
    possible = [row for row in confirmed if row["insight_status"] == "possibly_done"][:5]
    other_actions = [row for row in pending if row["action_scope"] == "deal_progression"
                     and not _confirmed_buyer_action(row)][:5]
    legacy = [row for row in pending if row["action_scope"] is None and row["type"] == "commitment"
              and _explicit_buyer_action(row)][:5]
    blockers = _blockers(rows[opportunity_id])
    aliases = sales._aliases(conn, workspace_id, viewer_mailbox, agreed + other_actions + possible + legacy + blockers)

    def cite(row):
        return _citation(row, timezone, aliases, viewer_mailbox)

    events = _upcoming_events(conn, workspace_id, [deal], now)
    state = "buyer_agreement_observed" if agreed else "proposal_or_pending_action" if other_actions else "unknown"
    candidate = next(iter(agreed or other_actions), None)
    scenario = {
        "candidate_insight_id": str(candidate["insight_id"]) if candidate else None,
        "open_blocker_insight_ids": [str(row["insight_id"]) for row in blockers],
        "if_the_deal_can_move": (
            "If the relevant blockers clear, confirm the buyer carries out the cited agreed action." if agreed
            else "If the buyer confirms the cited proposed action, agree on an owner and timing." if candidate
            else "Ask the buyer what decision or action would move the deal forward."
        ),
        "if_a_blocker_remains": (
            "Ask what will clear the cited blocker and who can take responsibility for it." if blockers else None
        ),
        "what_to_verify": "Confirm who owns the next action, its timing, and which blockers need to clear.",
    }
    return {"status": "ok", "opportunity": sales._deal_output(deal), "milestone_state": state,
            "agreed_buyer_actions": [cite(row) for row in agreed],
            "proposed_or_other_pending_actions": [cite(row) for row in other_actions],
            "buyer_actions_possibly_completed": [cite(row) for row in possible],
            "legacy_buyer_commitments_with_unverified_scope": [cite(row) for row in legacy],
            "open_blockers_and_objections": [cite(row) for row in blockers],
            "scheduled_events": [_scheduled(row, timezone) for row in events[deal["company_id"]]],
            "conditional_scenario": scenario,
            "as_of": now.astimezone(timezone).isoformat(), "lookback_days": EVIDENCE_DAYS,
            "evidence_truncated": opportunity_id in truncated,
            "interpretation": "Build conditional scenarios from the cited buyer actions and blockers. A calendar "
                              "entry shows a schedule. Any suggested move remains a proposal until accepted."}


def _recent_contact(conn, workspace_id, company_ids, identities, internal, since, now):
    if not company_ids:
        return set(), {}
    rows = conn.execute(
        """select id, source, company_id, occurred_at, subject, participants, meta, thread_id,
                  case when source = 'notes' then body end as body
           from public.source_records where workspace_id = %s and company_id = any(%s)
             and source in ('gmail', 'notes', 'calendar') and status = 'classified'
             and classification->'is_commercial' = 'true'::jsonb and occurred_at between %s and %s
             and (source <> 'calendar' or coalesce(meta->>'status', '') not in ('cancelled', 'canceled'))
           order by occurred_at desc, id""",
        (workspace_id, company_ids, since, now),
    )
    verified = set()
    unverified = {}
    for row in rows:
        company_id = row["company_id"]
        if company_id in verified:
            continue
        emails, domains, speakers = identities[company_id]
        if row["source"] == "gmail":
            if sales._email_direction(row, internal, emails, domains):
                verified.add(company_id)
        elif (row["source"] == "notes" and not (row["meta"] or {}).get("date_inferred")
              and next(sales._speaker_lines(row["body"], speakers), None)):
            verified.add(company_id)
        else:
            unverified.setdefault(company_id, row)
    return verified, unverified


def find_reengagement_candidates(conn, workspace_id, owner_code: str | None = None, quiet_days: int = 30,
                                 lookback_days: int = 180, limit: int = 10,
                                 viewer_mailbox: str | None = None) -> dict:
    """Find paused or opening deals with historical buyer evidence and no observed recent direct contact."""
    sales._limit(limit)
    if not 1 <= quiet_days <= MAX_QUIET_DAYS or not quiet_days < lookback_days <= MAX_REENGAGEMENT_LOOKBACK_DAYS:
        raise ValueError("quiet_days must be 1–365 and lookback_days must be longer, up to 730")
    now = timeline.utcnow()
    since, quiet_since = now - timedelta(days=lookback_days), now - timedelta(days=quiet_days)
    internal, timezone = sales._workspace(conn, workspace_id)
    deals = _deals(conn, workspace_id, (0, 1), owner_code)
    identities = _buyer_identities(conn, workspace_id, [deal["company_id"] for deal in deals], internal)
    rows, truncated = _grounded_rows(conn, workspace_id, deals, identities, internal,
                                     since, quiet_since, ("buying_signal", *ACTION_TYPES))
    prior = {}
    for opportunity_id, evidence in rows.items():
        signals = {}
        for row in evidence:
            if row["buyer_attribution"] and (row["type"] == "buying_signal" or _confirmed_buyer_action(row)):
                signals.setdefault(row["id"], row)
        if signals:
            prior[opportunity_id] = list(signals.values())[:3]
    with_evidence = [deal for deal in deals if deal["id"] in prior]
    verified, unverified = _recent_contact(conn, workspace_id, [deal["company_id"] for deal in with_evidence],
                                          identities, internal, quiet_since, now)
    eligible = [deal for deal in with_evidence if deal["company_id"] not in verified]
    eligible.sort(key=lambda deal: (-prior[deal["id"]][0]["occurred_at"].timestamp(), deal["normalized_name"], deal["id"]))
    selected = eligible[:limit]
    aliases = sales._aliases(conn, workspace_id, viewer_mailbox, [row for deal in selected for row in prior[deal["id"]]])
    candidates = []
    for deal in selected:
        company_id = deal["company_id"]
        context = unverified.get(company_id)
        candidates.append({
            **sales._deal_output(deal),
            "prior_buyer_evidence": [_citation(row, timezone, aliases, viewer_mailbox) for row in prior[deal["id"]]],
            "recent_direct_contact": "none_observed_in_synced_records",
            "has_customer_email_identity": bool(identities[company_id][0] or identities[company_id][1]),
            "recent_unverified_activity": (sales._source(context, timezone, {}, None) if context else None),
            "proposed_question": "Is the priority we discussed still active, and what would make a restart useful?",
        })
    return {"status": "ok" if candidates else "insufficient_evidence", "candidates": candidates,
            "as_of": now.astimezone(timezone).isoformat(), "quiet_days": quiet_days, "lookback_days": lookback_days,
            "quiet_since": quiet_since.astimezone(timezone).isoformat(), "eligible_deals_checked": len(deals),
            "truncated": len(eligible) > limit, "evidence_truncated": bool(truncated),
            "coverage_note": "Silence is limited to synced, account-linked records. Missing mailbox coverage, "
                             "unlinked messages, undated notes, and calendar attendance may affect the result."}


def assess_pilot_readiness(conn, workspace_id, opportunity_id: UUID | None = None, owner_code: str | None = None,
                           limit: int = 10, viewer_mailbox: str | None = None) -> dict:
    """Show the observed pilot criteria, open actions and evidence gaps for pilot-stage deals."""
    sales._limit(limit)
    if opportunity_id is not None:
        deal = sales._account(conn, workspace_id, opportunity_id)
        if not deal or owner_code and deal["owner_code"] != owner_code.strip().upper():
            return {"status": "not_found"}
        if deal["stage_rank"] != sales.PILOT_STAGE:
            return {"status": "outside_pilot_stage", "opportunity": sales._deal_output(deal)}
        deals = [deal]
    else:
        deals = _deals(conn, workspace_id, (sales.PILOT_STAGE,), owner_code, limit + 1)
    selected = deals[:limit]
    now = timeline.utcnow()
    internal, timezone = sales._workspace(conn, workspace_id)
    identities = _buyer_identities(conn, workspace_id, [deal["company_id"] for deal in selected], internal)
    rows, truncated = _grounded_rows(conn, workspace_id, selected, identities, internal,
                                     now - timedelta(days=EVIDENCE_DAYS), now)
    events = _upcoming_events(conn, workspace_id, selected, now)
    pilots = []
    for deal in selected:
        evidence = rows[deal["id"]]
        matches = _topic_evidence(evidence, PILOT_CRITERIA)
        blockers = _blockers(evidence)
        actions = _pending_actions(evidence)[:5]
        aliases = sales._aliases(conn, workspace_id, viewer_mailbox,
                                 blockers + actions + [row for group in matches.values() for row in group])

        def cite(row):
            return _citation(row, timezone, aliases, viewer_mailbox)

        assessment = ("open_blockers_recorded" if blockers else "needs_confirmation" if not all(matches.values())
                      else "buyer_evidence_available_for_all_criteria")
        pilots.append({**sales._deal_output(deal), "assessment": assessment,
                       "criteria": _topics(matches, PILOT_CRITERIA, cite),
                       "open_blockers_and_objections": [cite(row) for row in blockers],
                       "pending_actions": [{**cite(row), "buyer_agreement_observed": _confirmed_buyer_action(row)}
                                           for row in actions],
                       "scheduled_events": [_scheduled(row, timezone) for row in events[deal["company_id"]]],
                       "evidence_truncated": deal["id"] in truncated})
    return {"status": "ok" if pilots else "insufficient_evidence", "pilots": pilots,
            "as_of": now.astimezone(timezone).isoformat(), "lookback_days": EVIDENCE_DAYS,
            "truncated": len(deals) > limit,
            "interpretation": "Pilot is the pipeline stage. Read the cited buyer statements to assess each "
                              "criterion and state which approvals, dates or measurements remain uncertain."}
