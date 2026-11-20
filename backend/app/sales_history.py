"""Read-only summaries of observed pipeline changes and buyer activity."""

from bisect import bisect_left, bisect_right
from calendar import monthrange
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app import brief_v2, gmail, pipeline, sales_queries, timeline

HISTORY_FIELDS = ("stage", "owner_code", "country", "primary_buyer", "contacts_text", "next_step", "last_progress",
                  "created", "removed", "restored")
INGEST_SOURCES = ("pipeline", "gmail", "notes", "calendar")
MAX_VALUE = 500


def _month_start(today: date, months: int) -> date:
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    return date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))


def _timestamp(instant: datetime | None, timezone: ZoneInfo) -> str | None:
    return instant.astimezone(timezone).isoformat() if instant else None


def _ledger(conn, workspace_id) -> dict:
    return conn.execute(
        """with pipeline_runs as (
               select id, started_at, finished_at from public.sync_runs
               where workspace_id = %s and source = 'pipeline' and status = 'succeeded'
           )
           select (select id from pipeline_runs order by started_at, id limit 1) as initial_sync_id,
                  min(started_at) as first_sync_at, max(finished_at) as last_sync_at
           from pipeline_runs""",
        (workspace_id,),
    ).fetchone()


def _ledger_coverage(ledger: dict, start: date, end: date, timezone: ZoneInfo) -> dict:
    first, latest = ledger["first_sync_at"], ledger["last_sync_at"]
    return {"first_successful_sync_at": _timestamp(first, timezone),
            "last_successful_sync_at": _timestamp(latest, timezone),
            "starts_after_requested_date": first.astimezone(timezone).date() > start if first else None,
            "ends_before_requested_date": latest.astimezone(timezone).date() < end if latest else None,
            "basis": "Observed Sheet syncs. History between syncs or before the first sync is unknown."}


def get_deal_change_digest(conn, workspace_id, opportunity_id: UUID, months: int = 3,
                           limit: int = 10, offset: int = 0, *, as_of: datetime | None = None) -> dict:
    """Return the tracked changes to a deal over a rolling calendar-month window."""
    sales_queries._limit(limit)
    if not 1 <= months <= 60 or not 0 <= offset <= 2_147_483_647:
        raise ValueError("months or offset is outside the supported range")
    deal = conn.execute(
        f"select {sales_queries.DEAL_FIELDS}, o.removed_at from {sales_queries.DEAL_TABLES}"
        " where o.workspace_id = %s and o.id = %s", (workspace_id, opportunity_id),
    ).fetchone()
    if not deal:
        return {"status": "not_found"}

    _, timezone = sales_queries._workspace(conn, workspace_id)
    now = (as_of or timeline.utcnow()).astimezone(timezone)
    start = _month_start(now.date(), months)
    ledger = _ledger(conn, workspace_id)
    rows = conn.execute(
        """with matched as (
               select id, sync_run_id, field, old_value, new_value, detected_at
               from public.opportunity_changes
               where workspace_id = %s and opportunity_id = %s and field = any(%s)
                 and detected_at >= %s and detected_at <= %s
           )
           select totals.total, page.* from (select count(*) as total from matched) totals
           left join lateral (
               select * from matched order by detected_at desc, id desc limit %s offset %s
           ) page on true order by page.detected_at desc, page.id desc""",
        (workspace_id, opportunity_id, list(HISTORY_FIELDS), datetime.combine(start, time.min, timezone), now,
         limit, offset),
    ).fetchall()
    changes = []
    for row in rows:
        if row["id"] is None:
            continue
        old, new = row["old_value"], row["new_value"]
        event = ("initial_import" if row["field"] == "created" and row["sync_run_id"] == ledger["initial_sync_id"]
                 else row["field"])
        changes.append({"change_id": row["id"], "event": event, "field": row["field"],
                        "old_value": old[:MAX_VALUE] if old is not None else None,
                        "new_value": new[:MAX_VALUE] if new is not None else None,
                        "value_truncated": len(old or "") > MAX_VALUE or len(new or "") > MAX_VALUE,
                        "detected_at": _timestamp(row["detected_at"], timezone),
                        "sync_run_id": str(row["sync_run_id"])})
    count = rows[0]["total"]
    next_offset = offset + len(changes)
    return {"status": "ok", "opportunity": {**sales_queries._deal_output(deal),
                                            "removed_at": _timestamp(deal["removed_at"], timezone)},
            "period": {"start": start.isoformat(), "end": now.date().isoformat(), "timezone": timezone.key,
                       "as_of": now.isoformat()},
            "history_coverage": _ledger_coverage(ledger, start, now.date(), timezone),
            "changes": changes, "total_matches": count, "next_offset": next_offset if next_offset < count else None}


def _buyer_activity(conn, workspace_id, company_ids: list, internal: set[str], timezone: ZoneInfo, now: datetime,
                    start: date | None = None, end: date | None = None):
    """Yield recorded direct human Gmail and dated notes explicitly attributed to a known buyer."""
    if not company_ids:
        return
    contacts = sales_queries._contacts(conn, workspace_id, company_ids)
    domains = sales_queries._domains(conn, workspace_id, company_ids)
    emails, speakers = {}, {}
    for company_id, rows in contacts.items():
        people = [row for row in rows if gmail.domain(row["email"].lower()) not in internal
                  and not brief_v2.AUTO_SENDER_RE.search(row["email"])]
        emails[company_id] = {sales_queries._speaker_key(row["email"]) for row in people}
        names = Counter(sales_queries._speaker_key(row["name"] or "") for row in people)
        speakers[company_id] = emails[company_id] | {name for name, count in names.items() if count == 1 and " " in name}

    with conn.cursor(name="sales_history_buyer_activity") as cursor:
        cursor.itersize = 100
        cursor.execute(
            """select id, company_id, source, thread_id, occurred_at, subject, participants, meta,
                      case when source = 'notes' then body end as body
               from public.source_records where workspace_id = %(workspace)s and company_id = any(%(companies)s)
                 and source in ('gmail', 'notes') and status = 'classified'
                 and classification->'is_commercial' = 'true'::jsonb and occurred_at <= %(now)s
                 and (%(start)s::date is null or case when source = 'notes' then occurred_at::date
                           else (occurred_at at time zone %(timezone)s)::date end >= %(start)s)
                 and (%(end)s::date is null or case when source = 'notes' then occurred_at::date
                           else (occurred_at at time zone %(timezone)s)::date end <= %(end)s)
               order by occurred_at desc, id desc""",
            {"workspace": workspace_id, "companies": company_ids, "now": now, "start": start, "end": end,
             "timezone": timezone.key},
        )
        for row in cursor:
            company_id = row["company_id"]
            if row["source"] == "gmail":
                direction = sales_queries._email_direction(row, internal, emails.get(company_id, set()), domains[company_id])
                if direction:
                    yield {**row, "touch_kind": f"human_email_{direction}",
                           "touch_date": row["occurred_at"].astimezone(timezone).date()}
            elif not (row["meta"] or {}).get("date_inferred"):
                speaker = next(sales_queries._speaker_lines(row["body"], speakers.get(company_id, set())), None)
                if speaker:
                    yield {**row, "touch_kind": "dated_note_with_buyer_attribution", "speaker": speaker[0],
                           "touch_date": row["occurred_at"].date()}


def _stage_entries(conn, workspace_id, deals: list[dict], now: datetime) -> dict:
    current_stage = {row["id"]: row["stage_rank"] for row in deals}
    observed = {}
    for row in conn.execute(
        """select opportunity_id, field, old_value, new_value, detected_at from public.opportunity_changes
           where workspace_id = %s and opportunity_id = any(%s) and detected_at <= %s
             and field in ('stage', 'removed', 'restored')
           order by detected_at desc, id desc""", (workspace_id, list(current_stage), now),
    ):
        opportunity_id = row["opportunity_id"]
        if opportunity_id in observed:
            continue
        if row["field"] != "stage":
            observed[opportunity_id] = None
            continue
        before, after = pipeline.parse_stage(row["old_value"]), pipeline.parse_stage(row["new_value"])
        if before == after and after is not None:
            continue  # A label edit within the same rank does not reset time in stage.
        observed[opportunity_id] = row["detected_at"] if after == current_stage[opportunity_id] else None
    return observed


def _stall_evidence(conn, workspace_id, deal_ids: list, now: datetime, insight_type: str) -> dict:
    rows = conn.execute(
        """select distinct on (i.opportunity_id, i.type)
                  i.id as insight_id, i.opportunity_id, i.type, i.text, i.quote, i.status,
                  i.owner_side, i.owner_name, i.due_date, r.id, r.source, r.thread_id, r.subject, r.meta, r.occurred_at
           from public.insights i
           join public.opportunities o on o.id = i.opportunity_id and o.workspace_id = i.workspace_id
               and o.company_id = i.company_id
           join lateral (
               select record.id, record.source, record.thread_id, record.subject, record.meta, record.occurred_at,
                      count(*) over () as supporting_records
               from public.source_records record
               where record.workspace_id = i.workspace_id and record.company_id = i.company_id and record.source = i.source
                 and record.id = any(i.source_record_ids) and (i.origin_record_id is null or record.id = i.origin_record_id)
                 and record.status = 'classified' and record.classification->'is_commercial' = 'true'::jsonb
                 and strpos(regexp_replace(lower(record.body), '[[:space:]]+', ' ', 'g'),
                            nullif(btrim(regexp_replace(lower(i.quote), '[[:space:]]+', ' ', 'g')), '')) > 0
               order by record.id limit 1
           ) r on r.supporting_records = 1
           where i.workspace_id = %s and i.opportunity_id = any(%s)
             and (i.action_scope = 'deal_progression' or i.action_scope is null)
             and i.confidence in ('high', 'medium') and i.type = %s and i.status = 'open'
             and (r.occurred_at is null or r.occurred_at <= %s)
           order by i.opportunity_id, i.type, r.occurred_at desc nulls last, i.id""",
        (workspace_id, deal_ids, insight_type, now),
    )
    return {(row["opportunity_id"], row["type"]): row for row in rows}


def find_stalled_deals(conn, workspace_id, owner_code: str | None = None, quiet_days: int = 30,
                       limit: int = 10, viewer_mailbox: str | None = None, *, as_of: datetime | None = None) -> dict:
    """Find quiet active prospects while keeping missing contact evidence distinct."""
    sales_queries._limit(limit)
    if not 1 <= quiet_days <= 365:
        raise ValueError("quiet_days must be between 1 and 365")
    internal, timezone = sales_queries._workspace(conn, workspace_id)
    now = (as_of or timeline.utcnow()).astimezone(timezone)
    owner = owner_code.strip().upper() if owner_code else None
    deals = conn.execute(
        f"select {sales_queries.DEAL_FIELDS} from {sales_queries.DEAL_TABLES}"
        " where o.workspace_id = %s and o.removed_at is null and o.stage_rank = any(%s)"
        " and (%s::text is null or o.owner_code = %s)",
        (workspace_id, list(sales_queries.PROSPECT_STAGES), owner, owner),
    ).fetchall()
    latest = {}
    with closing(_buyer_activity(conn, workspace_id, [row["company_id"] for row in deals], internal, timezone, now)) as activity:
        for row in activity:
            previous = latest.get(row["company_id"])
            if previous is None or row["touch_date"] > previous["touch_date"]:
                latest[row["company_id"]] = row

    blockers = _stall_evidence(conn, workspace_id, [deal["id"] for deal in deals], now, "blocker") if deals else {}
    stalled, unknown = [], []
    quiet_count = no_touch_count = 0
    for deal in deals:
        touch = latest.get(deal["company_id"])
        age = (now.date() - touch["touch_date"]).days if touch else None
        quiet = age is not None and age >= quiet_days
        blocked = (deal["id"], "blocker") in blockers
        quiet_count += quiet
        no_touch_count += touch is None
        if blocked or quiet:
            stalled.append((deal, touch, age, blocked, quiet))
        elif not touch:
            unknown.append(deal)
    stalled.sort(key=lambda item: (not item[3], not item[4], -item[2] if item[2] is not None else 0,
                                   item[0]["normalized_name"], item[0]["id"]))
    unknown.sort(key=lambda deal: (deal["normalized_name"], deal["id"]))
    selected = stalled[:limit]
    unknown_selected = unknown[:max(0, limit - len(selected))]
    selected_deals = [item[0] for item in selected] + unknown_selected
    entries = _stage_entries(conn, workspace_id, selected_deals, now) if selected_deals else {}
    next_steps = _stall_evidence(conn, workspace_id, [item[0]["id"] for item in selected], now, "next_step") if selected else {}
    selected_blockers = [blockers[(deal["id"], "blocker")] for deal, _, _, blocked, _ in selected if blocked]
    aliases = sales_queries._aliases(conn, workspace_id, viewer_mailbox,
                                    [touch for _, touch, _, _, _ in selected if touch] + selected_blockers + list(next_steps.values()))

    def describe_insight(row: dict | None) -> dict | None:
        if not row:
            return None
        return {**sales_queries._source(row, timezone, aliases, viewer_mailbox), "insight_id": str(row["insight_id"]),
                "insight_type": row["type"], "summary": row["text"][:MAX_VALUE], "quote": row["quote"][:MAX_VALUE],
                "status": row["status"], "owner_side": row["owner_side"], "owner_name": row["owner_name"],
                "due_date": row["due_date"].isoformat() if row["due_date"] else None}

    items = []
    for deal, touch, age, blocked, quiet in selected:
        deal_id = deal["id"]
        entered = entries.get(deal_id)
        last_touch = None
        if touch:
            last_touch = {**sales_queries._source(touch, timezone, aliases, viewer_mailbox),
                          "touch_kind": touch["touch_kind"], "speaker": touch.get("speaker")}
        items.append({**sales_queries._deal_output(deal),
                      "stall_reasons": [reason for flag, reason in ((blocked, "open_blocker"), (quiet, "quiet")) if flag],
                      "touch_status": ("quiet" if quiet else "recent") if touch else "no_observed_buyer_touch",
                      "last_buyer_touch": last_touch, "quiet_days_observed": age,
                      "observed_stage_entry_at": _timestamp(entered, timezone),
                      "open_blocker": describe_insight(blockers.get((deal_id, "blocker"))),
                      "recorded_next_step": describe_insight(next_steps.get((deal_id, "next_step")))})
    return {"status": "ok", "deals": items, "eligible_deal_count": len(deals), "quiet_count": quiet_count,
            "stalled_count": len(stalled), "open_blocker_count": len(blockers),
            "no_observed_touch_count": no_touch_count, "unclassified_touch_deals": [
                {**sales_queries._deal_output(deal), "touch_status": "no_observed_buyer_touch",
                 "observed_stage_entry_at": _timestamp(entries.get(deal["id"]), timezone)} for deal in unknown_selected
            ], "truncated": len(stalled) + len(unknown) > limit,
            "quiet_days": quiet_days, "as_of": now.isoformat(),
            "basis": "Active Opening, In Talks and Proposal deals with recorded silence or an open blocker. Buyer touch "
                     "is direct human email or explicitly buyer-attributed dated notes. Missing evidence does not establish silence."}


def _period_feeds(conn, workspace_id, start: datetime, stop: datetime) -> dict:
    feeds = {source: [] for source in INGEST_SOURCES}
    for row in conn.execute(
        """select source, feed_key, count(*) as attempts, count(*) filter (where status = 'succeeded') as successes,
                  count(*) filter (where status = 'succeeded' and coverage_complete is true) as verified_receipts
           from public.sync_runs
           where workspace_id = %s and started_at >= %s and started_at < %s and source = any(%s)
           group by source, feed_key order by source, feed_key nulls last""",
        (workspace_id, start, stop, list(INGEST_SOURCES)),
    ):
        feeds[row["source"]].append({key: row[key] for key in ("feed_key", "attempts", "successes", "verified_receipts")})
    summaries = {}
    for source, rows in feeds.items():
        successes = sum(row["successes"] for row in rows)
        verified = sum(row["verified_receipts"] for row in rows)
        summaries[source] = {"feeds": rows, "successful_syncs": successes, "verified_receipts": verified,
                             "coverage_status": "no_successful_sync_recorded" if not successes else
                             "unattested_syncs_only" if not verified else "some_attested_syncs"}
    return summaries


def _stage_movement(old_value: str | None, new_value: str | None) -> str:
    before, after = pipeline.parse_stage(old_value), pipeline.parse_stage(new_value)
    if before not in range(6) or after not in range(6):
        return "unranked_stage_edits"
    if before == after:
        return "stage_label_edits"
    if after == 0:
        return "paused"
    if before == 0:
        return "resumed"
    return "advanced" if after > before else "regressed"


def _stage_observations(conn, workspace_id, now: datetime) -> dict:
    """Stage states explicitly supported by the ledger, independent of current stage."""
    histories = defaultdict(lambda: ([], []))
    last_rank = {}
    for row in conn.execute(
        """select opportunity_id, field, new_value, detected_at from public.opportunity_changes
           where workspace_id = %s and detected_at <= %s and field in ('created', 'removed', 'restored', 'stage')
           order by detected_at, id""", (workspace_id, now),
    ):
        deal_id, field = row["opportunity_id"], row["field"]
        if field == "created":
            last_rank[deal_id] = None  # The creation event records the company, with no stage value.
        elif field == "stage":
            rank = pipeline.parse_stage(row["new_value"])
            last_rank[deal_id] = rank if rank in range(6) else None
        state = "removed" if field == "removed" else last_rank.get(deal_id)
        times, states = histories[deal_id]
        if not states or state != states[-1]:
            times.append(row["detected_at"].timestamp())
            states.append(state)
    return histories


def _stage_at_touch(history: tuple[list, list], row: dict, timezone: ZoneInfo):
    times, states = history
    if row["source"] == "notes":
        # Notes date the meeting without a time. Its ordering against same-day edits is unknown.
        midnight = datetime.combine(row["touch_date"], time.min, timezone)
        following = datetime.combine(row["touch_date"] + timedelta(days=1), time.min, timezone)
        index = bisect_left(times, midnight.timestamp())
        if index != bisect_left(times, following.timestamp()):
            return None
    else:
        index = bisect_right(times, row["occurred_at"].timestamp())
    return states[index - 1] if index else None


def compare_pipeline_periods(conn, workspace_id, start: date, end: date, prior_start: date | None = None,
                             prior_end: date | None = None, viewer_mailbox: str | None = None,
                             *, as_of: datetime | None = None) -> dict:
    """Compare historical windows using recorded movements and distinct buyer accounts."""
    span = (end - start).days + 1
    if not 1 <= span <= 366:
        raise ValueError("period must contain between 1 and 366 days")
    if (prior_start is None) != (prior_end is None):
        raise ValueError("prior_start and prior_end are required together")
    if prior_start is None:
        if (start - date.min).days < span:
            raise ValueError("the default prior period would precede the supported calendar")
        prior_start, prior_end = start - timedelta(days=span), start - timedelta(days=1)
    if prior_end >= start or not 1 <= (prior_end - prior_start).days + 1 <= 366:
        raise ValueError("prior period must precede the current period and contain between 1 and 366 days")
    internal, timezone = sales_queries._workspace(conn, workspace_id)
    now = (as_of or timeline.utcnow()).astimezone(timezone)
    if end > now.date():
        raise ValueError("end must be on or before today's date in the workspace")
    ledger = _ledger(conn, workspace_id)
    accounts = list(conn.execute(
        f"select o.id, o.company_id, c.name as company from {sales_queries.DEAL_TABLES} where o.workspace_id = %s",
        (workspace_id,),
    ))
    by_opportunity = {row["id"]: row for row in accounts}
    by_company = {row["company_id"]: row for row in accounts}
    histories = _stage_observations(conn, workspace_id, now)

    def period(first: date, last: date) -> dict:
        begin = datetime.combine(first, time.min, timezone)
        stop = datetime.combine(last + timedelta(days=1), time.min, timezone)
        movements = Counter({key: 0 for key in ("advanced", "regressed", "paused", "resumed", "stage_label_edits",
                                               "unranked_stage_edits", "new_rows", "removed_rows", "restored_rows",
                                               "initial_import_rows", "owner_changes", "prospect_advances")})
        changed_deals = set()
        advancing_prospects = set()
        seen = set()
        stage_examples = []
        for row in conn.execute(
            """select id, opportunity_id, field, old_value, new_value, sync_run_id, detected_at
               from public.opportunity_changes
               where workspace_id = %s and detected_at >= %s and detected_at < %s and detected_at <= %s
                 and field in ('stage', 'created', 'removed', 'restored', 'owner_code')
               order by detected_at desc, id desc""",
            (workspace_id, begin, stop, now),
        ):
            identity = (row["opportunity_id"], row["sync_run_id"], row["field"], row["old_value"], row["new_value"])
            if identity in seen:
                continue
            seen.add(identity)
            if row["field"] == "stage":
                movement = _stage_movement(row["old_value"], row["new_value"])
                movements[movement] += 1
                if movement == "advanced" and pipeline.parse_stage(row["old_value"]) in sales_queries.PROSPECT_STAGES:
                    movements["prospect_advances"] += 1
                    advancing_prospects.add(row["opportunity_id"])
                if movement in ("advanced", "regressed", "paused", "resumed"):
                    changed_deals.add(row["opportunity_id"])
                    if len(stage_examples) < 3 and row["opportunity_id"] in by_opportunity:
                        stage_examples.append({"change_id": row["id"], "opportunity_id": str(row["opportunity_id"]),
                                               "company": by_opportunity[row["opportunity_id"]]["company"],
                                               "movement": movement, "old_value": row["old_value"][:MAX_VALUE],
                                               "new_value": row["new_value"][:MAX_VALUE],
                                               "detected_at": _timestamp(row["detected_at"], timezone)})
            elif row["field"] == "created":
                key = "initial_import_rows" if row["sync_run_id"] == ledger["initial_sync_id"] else "new_rows"
                movements[key] += 1
            else:
                movements[{"removed": "removed_rows", "restored": "restored_rows", "owner_code": "owner_changes"}[row["field"]]] += 1
        contacts, inbound, notes = set(), set(), set()
        prospect_contacts, prospect_inbound, unknown_stage = set(), set(), set()
        contact_examples = []
        for row in _buyer_activity(conn, workspace_id, list(by_company), internal, timezone, now, first, last):
            company_id = row["company_id"]
            deal_id = by_company[company_id]["id"]
            observed_stage = _stage_at_touch(histories[deal_id], row, timezone)
            if company_id not in contacts and len(contact_examples) < 3:
                contact_examples.append({**row, "observed_stage": observed_stage})
            contacts.add(company_id)
            if row["touch_kind"] == "human_email_inbound":
                inbound.add(company_id)
            if row["touch_kind"] == "dated_note_with_buyer_attribution":
                notes.add(company_id)
            if observed_stage in sales_queries.PROSPECT_STAGES:
                prospect_contacts.add(company_id)
                if row["touch_kind"] == "human_email_inbound":
                    prospect_inbound.add(company_id)
            elif observed_stage is None:
                unknown_stage.add(company_id)
        aliases = sales_queries._aliases(conn, workspace_id, viewer_mailbox, contact_examples)
        buyer_examples = [{**sales_queries._source(row, timezone, aliases, viewer_mailbox),
                           "company": by_company[row["company_id"]]["company"],
                           "opportunity_id": str(by_company[row["company_id"]]["id"]),
                           "touch_kind": row["touch_kind"], "observed_stage": row["observed_stage"]}
                          for row in contact_examples]
        return {"start": first.isoformat(), "end": last.isoformat(), "days": (last - first).days + 1,
                "partial_current_day": last == now.date(),
                "metrics": {**movements, "distinct_deals_with_stage_movement": len(changed_deals),
                            "distinct_advancing_prospects": len(advancing_prospects),
                            "distinct_accounts_with_buyer_touch": len(contacts),
                            "distinct_accounts_with_inbound_buyer_email": len(inbound),
                            "distinct_accounts_with_dated_buyer_notes": len(notes),
                            "distinct_prospect_accounts_with_buyer_touch": len(prospect_contacts),
                            "distinct_prospect_accounts_with_inbound_buyer_email": len(prospect_inbound),
                            "distinct_accounts_with_buyer_touch_unknown_stage": len(unknown_stage)},
                "stage_movement_examples": stage_examples, "buyer_touch_examples": buyer_examples,
                "history_coverage": _ledger_coverage(ledger, first, last, timezone),
                "source_coverage": _period_feeds(conn, workspace_id, begin, min(stop, now))}

    current, prior = period(start, end), period(prior_start, prior_end)
    return {"status": "ok", "timezone": timezone.key, "as_of": now.isoformat(), "current": current, "prior": prior,
            "deltas": {key: value - prior["metrics"][key] for key, value in current["metrics"].items()},
            "basis": "Stage changes use Sheet detection time and count recorded events. Buyer touch counts distinct "
                     "accounts with a mirrored opportunity row. Prospect counts use stage ranks 1–3 at the recorded "
                     "event, and historical touches without an observed stage are tracked separately. Receipts alone "
                     "do not establish continuous coverage."}
