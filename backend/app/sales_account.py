"""Evidence-backed stakeholder, commitment, and meeting reads for the sales connector."""

from collections import defaultdict
from datetime import date, timedelta
from uuid import UUID

from app import brief_v2, extractor, gmail, sales_queries as queries, timeline

SCAN_LIMIT = 300
SOURCE_FIELDS = "id, company_id, source, thread_id, occurred_at, subject, participants, meta"


def _people(row: dict, fields: tuple[str, ...]):
    for field in fields:
        for person in (row["participants"] or {}).get(field) or []:
            address = (person.get("address") or "").strip().lower()
            if "@" in address and not brief_v2.AUTO_SENDER_RE.search(address):
                yield address, (person.get("name") or "").strip()[:160] or None, field


def _calendar_item(row, timezone, internal) -> dict:
    people = {}
    for address, name, _ in _people(row, ("attendees",)):
        if gmail.domain(address) not in internal:
            people.setdefault(address, {"name": name, "email": address})
    return {**queries._source(row, timezone, {}, None), "attendance": "unverified",
            "calendar_status": (row["meta"] or {}).get("status", "unknown"),
            "external_invitees": list(people.values())[:queries.MAX_LIMIT]}


def get_stakeholder_map(conn, workspace_id, opportunity_id: UUID, limit: int = 10,
                        viewer_mailbox: str | None = None) -> dict:
    """List recorded account contacts and observed participants, without inferring their deal roles."""
    queries._limit(limit)
    account = queries._account(conn, workspace_id, opportunity_id)
    if not account:
        return {"status": "not_found"}
    internal, timezone = queries._workspace(conn, workspace_id)
    company_id = account["company_id"]
    now = timeline.utcnow()
    contacts = conn.execute(
        """select name, email from public.contacts
           where workspace_id = %s and company_id = %s
           order by lower(coalesce(name, '')), lower(email), email limit %s""",
        (workspace_id, company_id, SCAN_LIMIT + 1),
    ).fetchall()
    observations = conn.execute(
        f"""select {SOURCE_FIELDS} from public.source_records
            where workspace_id = %s and company_id = %s and source in ('gmail', 'calendar')
              and status = 'classified' and classification->'is_commercial' = 'true'::jsonb
              and (source <> 'calendar' or coalesce(meta->>'status', '') not in ('cancelled', 'canceled'))
              and (source <> 'gmail' or occurred_at is null or occurred_at <= %s)
            order by occurred_at desc nulls last, id desc limit %s""",
        (workspace_id, company_id, now, SCAN_LIMIT + 1),
    ).fetchall()
    people = {}
    for contact in contacts[:SCAN_LIMIT]:
        address = contact["email"].strip().lower()
        if gmail.domain(address) in internal or brief_v2.AUTO_SENDER_RE.search(address):
            continue
        people.setdefault(address, {"email": address, "name": (contact["name"] or "")[:160] or None,
                                    "recorded_contact": True, "observations": {}})

    for row in observations[:SCAN_LIMIT]:
        if row["source"] == "gmail":
            senders = list(queries._email_addresses(row["participants"] or {}, ("from",)))
            if not senders or brief_v2.is_automated(senders, row["subject"]):
                continue
            fields = ("from", "to", "cc")
        else:
            fields = ("attendees",)
        for address, name, field in _people(row, fields):
            if gmail.domain(address) in internal:
                continue
            person = people.setdefault(address, {"email": address, "name": None,
                                                 "recorded_contact": False, "observations": {}})
            person["name"] = person["name"] or name
            person["observations"].setdefault(field, row)

    selected = sorted(people.values(), key=lambda p: (not p["recorded_contact"],
                                                     (p["name"] or p["email"]).casefold(), p["email"]))[:limit]
    aliases = queries._aliases(conn, workspace_id, viewer_mailbox,
                              [row for p in selected for row in p["observations"].values()])
    labels = {"from": "email_sender", "to": "email_recipient", "cc": "email_cc", "attendees": "scheduled_invitee"}
    for person in selected:
        person["observations"] = [
            {**queries._source(row, timezone, aliases, viewer_mailbox), "relationship": labels[field],
             **({"attendance": "unverified"} if field == "attendees" else {})}
            for field, row in person["observations"].items()
        ]

    primary = (account["primary_buyer"] or "").strip()
    return {"status": "ok", "opportunity": queries._deal_output(account), "stakeholders": selected,
            "primary_buyer": {"as_recorded": primary[:250], "source": "pipeline_sheet"} if primary else None,
            "truncated": len(people) > limit or max(len(contacts), len(observations)) > SCAN_LIMIT,
            "as_of": now.astimezone(timezone).isoformat()}


def _grounded_insights(conn, workspace_id, opportunity_id, owner, types, now, limit,
                       *, due_by=None, include_undated=True, open_only=False) -> list[dict]:
    """Retrieve a single quote-bearing commercial email or note for each insight."""
    return conn.execute(
        """select i.id as insight_id, i.opportunity_id, i.type, i.text, i.quote, i.confidence,
                  i.owner_side, i.owner_name, i.due_date, i.status as recorded_status, i.completion,
                  c.name as company, o.owner_code, r.id, r.company_id, r.source, r.thread_id, r.external_id,
                  r.occurred_at, r.subject, r.meta
           from public.insights i
           join public.opportunities o on o.id = i.opportunity_id and o.workspace_id = i.workspace_id
               and o.company_id = i.company_id
           join public.companies c on c.id = o.company_id and c.workspace_id = i.workspace_id
           join lateral (
               select record.id, record.company_id, record.source, record.thread_id, record.external_id,
                      record.occurred_at, record.subject, record.meta, count(*) over () as supporting_records
               from public.source_records record
               where record.workspace_id = i.workspace_id and record.company_id = i.company_id
                 and record.source = i.source and record.source in ('gmail', 'notes')
                 and record.id = any(i.source_record_ids)
                 and (i.origin_record_id is null or record.id = i.origin_record_id)
                 and record.status = 'classified' and record.classification->'is_commercial' = 'true'::jsonb
                 and strpos(regexp_replace(lower(record.body), '[[:space:]]+', ' ', 'g'),
                            nullif(btrim(regexp_replace(lower(i.quote), '[[:space:]]+', ' ', 'g')), '')) > 0
               order by record.id limit 1
           ) r on r.supporting_records = 1
           where i.workspace_id = %s and o.removed_at is null and i.type = any(%s)
             and i.status <> 'dismissed' and i.confidence in ('medium', 'high')
             and (not %s or i.status in ('open', 'possibly_done'))
             and (i.action_scope = 'deal_progression' or i.action_scope is null)
             and (%s::uuid is null or o.id = %s) and (%s::text is null or o.owner_code = %s)
             and (r.occurred_at is null or r.occurred_at <= %s)
             and (i.due_date is not null or %s)
             and (%s::date is null or i.due_date is null or i.due_date <= %s)
           order by (i.status = 'done'), i.due_date nulls last, r.occurred_at desc nulls last, i.id limit %s""",
        (workspace_id, list(types), open_only, opportunity_id, opportunity_id, owner, owner, now, include_undated,
         due_by, due_by, limit),
    ).fetchall()


def _insight_source(row, timezone, aliases, viewer_mailbox) -> dict:
    return {**queries._source(row, timezone, aliases, viewer_mailbox), "insight_id": str(row["insight_id"]),
            "type": row["type"], "summary": row["text"][:queries.MAX_EXCERPT],
            "quote": row["quote"][:queries.MAX_EXCERPT], "confidence": row["confidence"]}


def _action_item(row, timezone, aliases, viewer_mailbox) -> dict:
    return {"kind": "commitment" if row["type"] == "commitment" else "request_or_proposed_step",
            "opportunity_id": str(row["opportunity_id"]), "company": row["company"], "deal_owner": row["owner_code"],
            "responsible_side": row["owner_side"] or "unclear", "responsible_person": row["owner_name"],
            "due_date": row["due_date"], "recorded_status": row["recorded_status"],
            "evidence": _insight_source(row, timezone, aliases, viewer_mailbox)}


def _completion_sources(conn, workspace_id, items: list[dict]) -> dict:
    keys = set()
    for row in items:
        review = row["completion"] or {}
        if row["recorded_status"] in ("possibly_done", "done") and review.get("record_id"):
            try:
                keys.add(UUID(str(review["record_id"])))
            except ValueError:
                continue
    if not keys:
        return {}
    return {str(row["id"]): row for row in conn.execute(
        f"""select {SOURCE_FIELDS}, body from public.source_records
            where workspace_id = %s and id = any(%s) and status = 'classified'
              and classification->'is_commercial' = 'true'::jsonb""", (workspace_id, list(keys)),
    )}


def _completion(row, records, timezone, aliases, viewer_mailbox, now) -> dict | None:
    review = row["completion"] or {}
    if row["recorded_status"] not in ("possibly_done", "done"):
        return None
    record = records.get(str(review.get("record_id")))
    result = {"manual_confirmation": {"by": review.get("by"), "at": review.get("at")}
              if row["recorded_status"] == "done" and review.get("via") == "brief" else None,
              "evidence": None}
    if not record or record["company_id"] != row["company_id"]:
        return result
    if not extractor.quote_in_record(review.get("quote") or "", record, body_only=True):
        return result
    if (review.get("state") == "event_passed" and record["id"] == row["id"]
            and row["due_date"] and row["due_date"] < now.astimezone(timezone).date()):
        state = "scheduled_date_passed"
    elif (review.get("state") in ("done", "possibly_done") and record["source"] != "calendar"
          and not (record["meta"] or {}).get("date_inferred")
          and not (row["meta"] or {}).get("date_inferred") and row["occurred_at"] and record["occurred_at"]
          and row["occurred_at"] < record["occurred_at"] <= now):
        state = "possible_completion"
    else:
        return result
    result["evidence"] = {**queries._source(record, timezone, aliases, viewer_mailbox), "assessment": state,
                          "quote": review["quote"][:queries.MAX_EXCERPT]}
    return result


def _waiting_replies(conn, workspace_id, opportunity_id, owner, internal, now) -> tuple[list[dict], bool]:
    threads = conn.execute(
        """select distinct on (r.company_id, r.thread_id) r.company_id, r.thread_id, r.occurred_at,
                  o.id as opportunity_id, o.owner_code, c.name as company
           from public.source_records r
           join public.opportunities o on o.workspace_id = r.workspace_id and o.company_id = r.company_id
           join public.companies c on c.workspace_id = r.workspace_id and c.id = r.company_id
           where r.workspace_id = %s and o.removed_at is null and r.source = 'gmail' and r.thread_id is not null
             and r.status = 'classified' and r.classification->'is_commercial' = 'true'::jsonb
             and r.classification->'reply_expectation'->>'state' = 'expected' and r.occurred_at between %s and %s
             and (%s::uuid is null or o.id = %s) and (%s::text is null or o.owner_code = %s)
           order by r.company_id, r.thread_id, r.occurred_at desc, r.id desc limit %s""",
        (workspace_id, now - timedelta(days=brief_v2.WAITING_DAYS[1] + 1), now, opportunity_id, opportunity_id,
         owner, owner, SCAN_LIMIT + 1),
    ).fetchall()
    if not threads:
        return [], False
    by_thread = {(row["company_id"], row["thread_id"]): row for row in threads[:SCAN_LIMIT]}
    thread_ids = list({thread for _, thread in by_thread})
    contacts = queries._contacts(conn, workspace_id, list({company for company, _ in by_thread}))
    domains = queries._domains(conn, workspace_id, list({company for company, _ in by_thread}))
    closed = {row["ref"]: row for row in conn.execute(
        """select distinct on (ref) ref, action, created_at from public.brief_actions
           where workspace_id = %s and kind = 'thread' and ref = any(%s)
           order by ref, created_at desc, id desc""", (workspace_id, thread_ids),
    )}
    messages = defaultdict(list)
    for row in conn.execute(
        f"""select {SOURCE_FIELDS}, external_id, content_hash, body,
                   classification->'reply_expectation' as reply_expectation
            from public.source_records
            where workspace_id = %s and source = 'gmail' and thread_id = any(%s) and occurred_at <= %s
              and status = 'classified' and classification->'is_commercial' = 'true'::jsonb
            order by occurred_at, external_id, id""", (workspace_id, thread_ids, now),
    ):
        key = (row["company_id"], row["thread_id"])
        if key not in by_thread:
            continue
        participants = row["participants"] or {}
        senders = list(queries._email_addresses(participants, ("from",)))
        messages[key].append({"at": row["occurred_at"], "from": senders, "to": participants.get("to") or [],
                              "cc": participants.get("cc") or [], "external_id": row["external_id"],
                              "content_hash": row["content_hash"], "reply_expectation": row["reply_expectation"],
                              "automated": brief_v2.is_automated(senders, row["subject"]), "row": row})
    waits = []
    for key, thread in messages.items():
        if not (waiting := brief_v2.waiting_on_them(thread, internal, now)):
            continue
        last = waiting["last"]["row"]
        company, thread_id = key
        if closed.get(thread_id, {}).get("action") == "done" and closed[thread_id]["created_at"] >= last["occurred_at"]:
            continue
        customer_addresses = {row["email"].lower() for row in contacts[company]}
        if queries._email_direction(last, internal, customer_addresses, domains[company]) != "outbound":
            continue
        expectation = waiting["last"]["reply_expectation"]
        evidence = next((message["row"] for message in thread if not message["automated"]
                         and message["external_id"] == expectation["evidence_message_id"]), None)
        quote = expectation.get("evidence_quote") or ""
        response = (expectation.get("response") or "").strip()
        if not evidence or not response or not extractor.quote_in_record(quote, evidence, body_only=True):
            continue
        waits.append({**by_thread[key], "last": last, "evidence": evidence, "quote": quote,
                      "response": response, "elapsed_days": waiting["days"]})
    return waits, len(threads) > SCAN_LIMIT


def list_commitments_and_followups(conn, workspace_id, opportunity_id: UUID | None = None,
                                   owner_code: str | None = None, due_by: date | None = None,
                                   include_undated: bool = True, limit: int = 10,
                                   viewer_mailbox: str | None = None) -> dict:
    """Read commitments, open requests, and expected replies with evidence and recorded completion state."""
    queries._limit(limit)
    if opportunity_id and not queries._account(conn, workspace_id, opportunity_id):
        return {"status": "not_found"}
    internal, timezone = queries._workspace(conn, workspace_id)
    now = timeline.utcnow()
    owner = owner_code.strip().upper() if owner_code else None
    rows = _grounded_insights(conn, workspace_id, opportunity_id, owner, ("commitment", "next_step"), now,
                             limit + 1, due_by=due_by, include_undated=include_undated)
    waits, scan_truncated = _waiting_replies(conn, workspace_id, opportunity_id, owner, internal, now) if include_undated else ([], False)
    complete = _completion_sources(conn, workspace_id, rows)
    aliases = queries._aliases(conn, workspace_id, viewer_mailbox,
                              rows + list(complete.values()) + [row for wait in waits for row in (wait["last"], wait["evidence"])])

    items = []
    for row in rows:
        item = _action_item(row, timezone, aliases, viewer_mailbox)
        item["completion"] = _completion(row, complete, timezone, aliases, viewer_mailbox, now)
        items.append((row["occurred_at"], item))
    for wait in waits:
        proof = wait["evidence"]
        last = wait["last"]
        reply = {"expected_response": wait["response"][:queries.MAX_EXCERPT], "elapsed_days": wait["elapsed_days"],
                 "last_outbound": queries._source(last, timezone, aliases, viewer_mailbox)}
        matching_item = next((item for _, item in items if item["kind"] == "request_or_proposed_step"
                              and item["responsible_side"] == "customer" and item["recorded_status"] == "open"
                              and item["evidence"]["record_id"] == str(proof["id"])
                              and extractor.quote_in_record(wait["quote"], {"body": item["evidence"]["quote"]}, body_only=True)), None)
        if matching_item is not None:
            matching_item["reply_expectation"] = reply
            continue
        items.append((last["occurred_at"], {
            "kind": "awaiting_customer_reply", "opportunity_id": str(wait["opportunity_id"]),
            "company": wait["company"], "deal_owner": wait["owner_code"], "responsible_side": "customer",
            "responsible_person": None, "due_date": None, "recorded_status": "awaiting_in_captured_thread",
            "evidence": {**queries._source(proof, timezone, aliases, viewer_mailbox), "quote": wait["quote"][:queries.MAX_EXCERPT]},
            "reply_expectation": reply, "completion": None,
        }))
    items.sort(key=lambda entry: (entry[1]["recorded_status"] == "done", entry[1]["due_date"] or date.max,
                                  -entry[0].timestamp() if entry[0] else float("inf"), entry[1]["opportunity_id"],
                                  entry[1]["evidence"]["record_id"]))
    return {"status": "ok", "items": [item for _, item in items[:limit]],
            "truncated": len(items) > limit or scan_truncated, "as_of": now.astimezone(timezone).isoformat(),
            "reply_scope": "Expected replies in classified Gmail threads 7–60 days after the last outbound email."}


def prepare_sales_meeting(conn, workspace_id, opportunity_id: UUID, within_days: int = 14,
                          limit: int = 8, viewer_mailbox: str | None = None) -> dict:
    """Find the next recorded meeting and grounded themes to review before it."""
    queries._limit(limit)
    if not 1 <= within_days <= 14:
        raise ValueError("within_days must be between 1 and 14")
    account = queries._account(conn, workspace_id, opportunity_id)
    if not account:
        return {"status": "not_found"}
    internal, timezone = queries._workspace(conn, workspace_id)
    now = timeline.utcnow()
    horizon = now + timedelta(days=within_days)
    event = conn.execute(
        f"""select {SOURCE_FIELDS} from public.source_records
            where workspace_id = %s and company_id = %s and source = 'calendar'
              and status = 'classified' and classification->'is_commercial' = 'true'::jsonb
              and occurred_at > %s and occurred_at <= %s
              and coalesce(meta->>'status', '') not in ('cancelled', 'canceled')
            order by occurred_at, id limit 1""", (workspace_id, account["company_id"], now, horizon),
    ).fetchone()
    rows = _grounded_insights(conn, workspace_id, opportunity_id, None,
                             ("need", "objection", "blocker", "buying_signal"), now, limit + 1)
    actions = _grounded_insights(conn, workspace_id, opportunity_id, None,
                                ("commitment", "next_step"), now, limit + 1, open_only=True)
    aliases = queries._aliases(conn, workspace_id, viewer_mailbox, rows[:limit] + actions[:limit])
    themes = [_insight_source(row, timezone, aliases, viewer_mailbox) for row in rows[:limit]]
    open_actions = [_action_item(row, timezone, aliases, viewer_mailbox) for row in actions[:limit]]
    unknowns = []
    questions = []
    if not account["primary_buyer"]:
        unknowns.append("The pipeline does not name a primary buyer.")
        questions.append({"question": "Who owns the next decision and who needs to approve it?"})
    if not account["next_step"]:
        unknowns.append("The pipeline does not record a next step.")
        questions.append({"question": "What next step, owner, and timing should we agree on?"})
    blocker = next((row for row in rows[:limit] if row["type"] in ("blocker", "objection")
                    and row["recorded_status"] == "open"), None)
    if blocker:
        questions.append({"question": "Does this recorded concern still affect the next decision, and what would resolve it?",
                          "based_on": str(blocker["insight_id"])})
    if not themes:
        unknowns.append("No grounded themes were found in the captured notes or email.")
        questions.append({"question": "What outcome should we aim to validate in this conversation?"})
    if not event:
        unknowns.append("No qualifying scheduled event was found in the requested Calendar window.")
    return {"status": "ok", "opportunity": queries._deal_output(account),
            "meeting": _calendar_item(event, timezone, internal) if event else None, "prior_themes": themes,
            "open_actions": open_actions, "open_actions_truncated": len(actions) > limit,
            "unknowns": unknowns, "proposed_questions": questions, "themes_truncated": len(rows) > limit,
            "as_of": now.astimezone(timezone).isoformat(), "calendar_through": horizon.astimezone(timezone).isoformat()}
