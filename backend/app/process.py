"""Processing endpoints called by n8n after ingestion. Each call handles a bounded batch and is safe to repeat."""

import logging
from datetime import timedelta

import anthropic
from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel
from psycopg.types.json import Jsonb

from app import brief_v2, classifier, db, extractor, gmail, opportunities, profiler, reconciler, resolution, timeline
from app.config import settings
from app.ingest import _fail, _workspace_id, require_token
from app.pipeline import normalize_name

log = logging.getLogger("bianca_cro.process")
router = APIRouter(prefix="/process")


@router.post("/classify")
def classify_threads(x_ingest_token: str | None = Header(default=None)) -> dict:
    """Classify a bounded batch of pending units or existing Gmail waits that need a reply expectation."""
    require_token(x_ingest_token)
    client = _claude()

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        system_prompt, company_ids = _context(conn, workspace_id)
        ws = conn.execute("select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()
        internal = {d.lower() for d in ws["internal_domains"] + settings.project_team_domains}
        threads = [
            (r["source"], r["thread_id"])
            for r in conn.execute(
                "select source, thread_id from source_records where workspace_id = %s and status = 'pending'"
                " group by source, thread_id order by max(occurred_at) desc nulls last limit %s",
                (workspace_id, settings.classify_batch_threads),
            )
        ]
        run_id = conn.execute(
            "insert into sync_runs (workspace_id, source, rows_received) values (%s, 'classify', %s) returning id",
            (workspace_id, len(threads)),
        ).fetchone()["id"]
        conn.commit()

        counts = {"classified": 0, "commercial": 0, "linked_to_company": 0, "failed": 0,
                  "reply_expectations_backfilled": 0, "reply_expectation_errors": 0}
        try:
            for source, thread_id in threads:
                records = conn.execute(
                    "select id, external_id, occurred_at, subject, participants, body, meta, content_hash from source_records"
                    " where workspace_id = %s and source = %s and thread_id = %s",
                    (workspace_id, source, thread_id),
                ).fetchall()
                renderers = {"notes": lambda r: classifier.render_note(r[0]),
                             "calendar": lambda r: classifier.render_event(r[0])}
                latest = classifier.latest_reply_message(records, internal) if source == "gmail" else None
                thread_text = (classifier.render_thread(records, reply_internal_domains=internal if latest else None)
                               if source == "gmail" else renderers[source](records))
                try:
                    result = classifier.classify(client, settings.anthropic_model, system_prompt, thread_text, source,
                                                 latest_reply_message_id=latest["external_id"] if latest else None)
                except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                    # One bad thread doesn't stop the batch; it stays visible as failed.
                    log.warning("classify thread failed: %s", type(exc).__name__)
                    conn.execute(
                        "update source_records set status = 'failed' where workspace_id = %s and thread_id = %s"
                        " and source = %s",
                        (workspace_id, thread_id, source),
                    )
                    conn.commit()
                    counts["failed"] += 1
                    continue

                stored = classifier.check(result, thread_text, {k: v[0] for k, v in company_ids.items()})
                stored["model"] = settings.anthropic_model
                company_id = company_ids[normalize_name(stored["company"])][1] if stored["company"] else None
                discard = stored["category"] in classifier.DISCARD_TEXT_CATEGORIES
                if discard:
                    stored["evidence_quote"] = None  # keep only the label; the text is not sales data
                if latest and stored["is_commercial"]:
                    stored["reply_expectation"] = classifier.check_reply_expectation(
                        result.reply_expectation, records, latest)
                conn.execute(
                    "update source_records set status = 'classified', classification = %s, company_id = %s,"
                    " body = case when %s then null else body end"
                    " where workspace_id = %s and thread_id = %s and source = %s",
                    (Jsonb(stored), company_id, discard, workspace_id, thread_id, source),
                )
                conn.commit()
                counts["classified"] += 1
                counts["commercial"] += stored["is_commercial"]
                counts["linked_to_company"] += company_id is not None

            remaining = conn.execute(
                "select count(distinct (source, thread_id)) as n from source_records"
                " where workspace_id = %s and status = 'pending'",
                (workspace_id,),
            ).fetchone()["n"]
            if not remaining:
                # Let the daily run finish the rollout backfill before moving to the next stage.
                remaining = _backfill_reply_expectations(
                    conn, client, workspace_id, internal, settings.classify_batch_threads - len(threads), counts)
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("classify run %s failed", run_id)
            raise HTTPException(status_code=500, detail="classify failed")

        conn.execute(
            "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s, rows_skipped = %s"
            " where id = %s",
            (counts["classified"], counts["failed"], run_id),
        )
        conn.commit()

    log.info("classify run %s: %s remaining_threads=%s", run_id, counts, remaining)
    return {"sync_run_id": str(run_id), "threads": len(threads), **counts, "remaining_threads": remaining}


def _backfill_reply_expectations(conn, client, workspace_id, internal: set[str], limit: int, counts: dict) -> int:
    """Fill missing metadata on recent classified outbound messages without reclassifying their sales context."""
    now = timeline.utcnow()
    first_day, last_day = brief_v2.WAITING_DAYS
    threads: dict[str, list[dict]] = {}
    for row in conn.execute(
            "select r.id, r.thread_id, r.external_id, r.content_hash, r.occurred_at, r.subject, r.participants,"
            " r.status, r.classification->'reply_expectation' as reply_expectation,"
            " (r.classification->>'is_commercial')::bool and exists (select 1 from opportunities o"
            " where o.workspace_id = r.workspace_id and o.company_id = r.company_id and o.removed_at is null)"
            " as commercial_deal from source_records r where r.workspace_id = %s and r.source = 'gmail'"
            " and r.occurred_at > %s and r.occurred_at <= %s and r.thread_id is not null"
            " order by r.occurred_at desc, r.external_id desc",
            (workspace_id, now - timedelta(days=last_day + 1), now)):
        threads.setdefault(row["thread_id"], []).append(row)

    candidates = []
    for records in threads.values():
        latest = classifier.latest_reply_message(records, internal)
        if not latest or latest["status"] != "classified" or not latest["commercial_deal"]:
            continue
        if not first_day <= (now - latest["occurred_at"]).days <= last_day:
            continue
        checked = latest["reply_expectation"] or {}
        if (checked.get("latest_message_id") == latest["external_id"]
                and checked.get("latest_content_hash") == latest["content_hash"]
                and (not checked.get("retry_after") or checked["retry_after"] > now.isoformat())):
            continue
        candidates.append(latest)

    batch = candidates[:limit]
    for latest in batch:
        records = conn.execute(
            "select id, external_id, occurred_at, subject, body, participants, content_hash from source_records"
            " where workspace_id = %s and source = 'gmail' and thread_id = %s and occurred_at <= %s",
            (workspace_id, latest["thread_id"], now)).fetchall()
        current = classifier.latest_reply_message(records, internal)
        if not current or (current["id"], current["content_hash"]) != (latest["id"], latest["content_hash"]):
            continue
        try:
            expectation = classifier.classify_reply_expectation(client, settings.anthropic_model, records, current, internal)
        except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
            log.warning("reply expectation backfill failed: %s", type(exc).__name__)
            counts["reply_expectation_errors"] += 1
            # Retry transient errors in a later run without holding up this batch.
            expectation = {"state": "unclear", "latest_message_id": current["external_id"],
                           "latest_content_hash": current["content_hash"],
                           "retry_after": (now + timedelta(days=1)).isoformat()}
        updated = conn.execute(
            "update source_records set classification = jsonb_set(classification, '{reply_expectation}', %s)"
            " where id = %s and content_hash = %s and status = 'classified'"
            " and (classification->>'is_commercial')::bool"
            " and (classification->'reply_expectation'->>'latest_message_id' is distinct from external_id"
            " or classification->'reply_expectation'->>'latest_content_hash' is distinct from content_hash"
            " or (classification->'reply_expectation'->>'retry_after')::timestamptz <= %s)",
            (Jsonb(expectation), current["id"], current["content_hash"], now))
        counts["reply_expectations_backfilled"] += updated.rowcount
        conn.commit()
    return len(candidates) - len(batch)


def _context(conn, workspace_id) -> tuple[str, dict[str, tuple[str, str]]]:
    """System prompt plus {normalized name: (display name, company id)} for exact-match checks."""
    ws = conn.execute("select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()
    team = conn.execute(
        "select owner_code, full_name, email from team_members where workspace_id = %s order by owner_code",
        (workspace_id,),
    ).fetchall()
    companies = conn.execute(
        "select c.id, c.name, c.normalized_name, c.country, o.stage, o.owner_code, o.contacts_text"
        " from companies c join opportunities o on o.company_id = c.id"
        " where c.workspace_id = %s and o.removed_at is null order by c.name",
        (workspace_id,),
    ).fetchall()
    prompt = classifier.build_system_prompt(companies, team, ws["internal_domains"])
    return prompt, {c["normalized_name"]: (c["name"], c["id"]) for c in companies}


@router.post("/resolve")
def resolve_entities(x_ingest_token: str | None = Header(default=None)) -> dict:
    """Rebuild contacts and company domains from classified records, then re-link records by rules.

    Safe to re-run: learned domains are recomputed each time; manually added domains are kept and trusted.
    """
    require_token(x_ingest_token)
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        internal = {d.lower() for d in conn.execute(
            "select internal_domains from workspaces where id = %s", (workspace_id,)).fetchone()["internal_domains"]}
        project = {d.lower() for d in settings.project_team_domains}
        companies = conn.execute("select id, name, normalized_name from companies where workspace_id = %s",
                                 (workspace_id,)).fetchall()
        names = {str(c["id"]): c["name"] for c in companies}
        by_norm = {c["normalized_name"]: str(c["id"]) for c in companies}

        records = conn.execute(
            "select id, source, participants, classification, occurred_at from source_records"
            " where workspace_id = %s and status = 'classified'", (workspace_id,)).fetchall()

        def classifier_company(rec):
            name = (rec["classification"] or {}).get("company")
            return by_norm.get(normalize_name(name)) if name else None

        evidence = []
        for rec in records:
            cls = rec["classification"] or {}
            evidence.append(resolution.Evidence(
                record_id=str(rec["id"]),
                company_id=classifier_company(rec),
                confident=cls.get("confidence") == "high" and not cls.get("flags"),
                addresses=resolution.external_addresses(rec["participants"] or {}, internal, project),
                occurred_at=rec["occurred_at"].isoformat() if rec["occurred_at"] else None,
            ))
        manual = {r["domain"]: str(r["company_id"]) for r in conn.execute(
            "select domain, company_id from company_domains where workspace_id = %s and origin = 'manual'",
            (workspace_id,))}
        res = resolution.resolve(evidence, names, manual)

        with conn.transaction():
            learned = [v for v in res.domains.values() if v.domain not in manual]
            conn.execute("delete from company_domains where workspace_id = %s and origin = 'learned'"
                         " and not (domain = any(%s))", (workspace_id, [v.domain for v in learned]))
            for v in learned:
                conn.execute(
                    "insert into company_domains (workspace_id, company_id, domain, trusted, evidence_count)"
                    " values (%s, %s, %s, %s, %s) on conflict (workspace_id, domain) do update set"
                    " company_id = excluded.company_id, trusted = excluded.trusted,"
                    " evidence_count = excluded.evidence_count where company_domains.origin = 'learned'",
                    (workspace_id, v.company_id, v.domain, v.trusted, v.votes))

            seen: dict[str, dict] = {}
            for ev in evidence:
                for email, name in ev.addresses.items():
                    s = seen.setdefault(email, {"name": name, "count": 0, "first": None, "last": None})
                    s["name"] = s["name"] or name
                    s["count"] += 1
                    if ev.occurred_at:
                        s["first"] = min(filter(None, [s["first"], ev.occurred_at]))
                        s["last"] = max(filter(None, [s["last"], ev.occurred_at]))
            for email, s in seen.items():
                conn.execute(
                    "insert into contacts (workspace_id, company_id, email, name, record_count, first_seen_at,"
                    " last_seen_at) values (%s, %s, %s, %s, %s, %s, %s) on conflict (workspace_id, email) do update"
                    " set company_id = excluded.company_id, name = coalesce(nullif(excluded.name, ''), contacts.name),"
                    " record_count = excluded.record_count, first_seen_at = excluded.first_seen_at,"
                    " last_seen_at = excluded.last_seen_at",
                    (workspace_id, res.contact_company.get(email), email, s["name"] or None, s["count"],
                     s["first"], s["last"]))

            methods: dict[str, int] = {}
            conflicts: dict[tuple, int] = {}
            for ev in evidence:
                rule = resolution.rules_company(ev.addresses, res)
                final, method = resolution.decide(ev.company_id, rule)
                conn.execute("update source_records set company_id = %s, link_method = %s, rules_company_id = %s"
                             " where id = %s", (final, method, rule, ev.record_id))
                methods[method or "unlinked"] = methods.get(method or "unlinked", 0) + 1
                if method == "conflict":
                    key = (names.get(ev.company_id), names.get(rule))
                    conflicts[key] = conflicts.get(key, 0) + 1

    trusted = sorted((v for v in res.domains.values() if v.trusted), key=lambda v: -v.votes)
    untrusted = sorted((v for v in res.domains.values() if not v.trusted), key=lambda v: -v.votes)
    return {
        "records": len(records),
        "link_methods": methods,
        "conflicts": [{"classifier": a, "rules": b, "records": n} for (a, b), n in conflicts.items()],
        "contacts": len(seen),
        "contacts_with_company": sum(1 for e in seen if res.contact_company.get(e)),
        "trusted_domains": [{"domain": v.domain, "company": names.get(v.company_id), "evidence": v.votes}
                            for v in trusted],
        "candidate_domains": [{"domain": v.domain, "company": names.get(v.company_id), "evidence": v.votes}
                              for v in untrusted],
    }


class ExtractRequest(BaseModel):
    opportunity_ids: list[str]
    backfill_scope: bool = False


_PENDING_EXTRACTION = """
    select r.source, r.thread_id, r.company_id, max(r.occurred_at) as latest
    from source_records r
    where r.workspace_id = %(workspace_id)s and r.company_id::text = any(%(company_ids)s)
      and r.status = 'classified' and r.body is not null and r.thread_id is not null
      and (r.classification->>'is_commercial')::bool
    group by r.workspace_id, r.source, r.thread_id, r.company_id
    having bool_or(r.extracted_version is null or not r.extracted_version = any(%(kept_versions)s))
       or (%(backfill_scope)s and bool_or(r.extracted_version is distinct from %(current_version)s) and (
           (r.source = 'gmail' and max(r.occurred_at) >= now() - interval '90 days')
           or exists (select 1 from insights i where i.workspace_id = r.workspace_id
                      and i.source = r.source and i.unit_id = r.thread_id and i.company_id = r.company_id
                      and i.status = 'open' and i.type in ('commitment', 'next_step'))))
"""


def _write_extracted_insights(conn, workspace_id, unit: dict, deal: dict, records: list[dict],
                             kept: list[dict]) -> None:
    """Update matching insights in place so existing IDs, first-log times and reviews survive."""
    key = (workspace_id, unit["source"], unit["thread_id"])
    record_ids = [r["id"] for r in records]
    by_id = {r["id"]: r for r in records}
    existing = conn.execute(
        "select id, type, text, quote, status, origin_record_id from insights"
        " where workspace_id = %s and source = %s and unit_id = %s"
        " order by (status in ('done', 'dismissed')) desc, (status = 'possibly_done') desc, created_at, id for update",
        key,
    ).fetchall()

    def identity(row: dict) -> tuple:
        return row["origin_record_id"], *extractor.insight_key(row["type"], row["quote"])

    def same_action(old: dict, new: dict, field: str) -> bool:
        """Same type and quote (or one-line text), on the same message or on a legacy row not yet tied to one."""
        return (old["origin_record_id"] in (None, new["origin_record_id"])
                and extractor.insight_key(old["type"], old[field]) == extractor.insight_key(new["type"], new[field]))

    prior = {}
    for row in existing:
        prior.setdefault(identity(row), row)
    # Reserve all exact matches before allowing a unique looser match (a reworded quote, or a legacy row
    # without an origin); otherwise the old row survives as an "omitted" action next to its new copy.
    new_keys = {identity(i) for i in kept}
    seen, matched = set(), set()
    for i in kept:
        item_key = identity(i)
        if item_key in seen:
            continue
        seen.add(item_key)
        old = prior.get(item_key)
        for field in ("quote", "text"):
            if old is not None:
                break
            variants = [r for r in existing if r["id"] not in matched and identity(r) not in new_keys
                        and same_action(r, i, field)]
            incoming = {identity(k) for k in kept if k["type"] == i["type"]
                        and extractor.insight_key(k["type"], k[field]) == extractor.insight_key(i["type"], i[field])}
            if len(variants) == 1 and len(incoming) == 1:
                old = variants[0]
        when = i["origin_occurred_at"] or records[-1]["occurred_at"]
        if old:
            matched.add(old["id"])
            conn.execute(
                "update insights set company_id = %s, opportunity_id = %s, source_record_ids = %s,"
                " text = %s, quote = %s, owner_side = %s, owner_name = %s, due_date = %s, confidence = %s,"
                " occurred_at = case when origin_occurred_at is not null then occurred_at else %s end,"
                " origin_occurred_at = coalesce(origin_occurred_at, %s),"
                " origin_record_id = coalesce(origin_record_id, %s), action_scope = %s,"
                " extractor_version = %s, last_extracted_at = now() where id = %s",
                (unit["company_id"], deal["id"], record_ids, i["text"], i["quote"], i["owner_side"],
                 i["owner_name"], i["due_date"], i["confidence"], when, i["origin_occurred_at"],
                 i["origin_record_id"], i.get("action_scope"), extractor.EXTRACTOR_VERSION, old["id"]),
            )
        else:
            conn.execute(
                "insert into insights (workspace_id, company_id, opportunity_id, source, unit_id, source_record_ids,"
                " type, text, quote, owner_side, owner_name, due_date, occurred_at, confidence, extractor_version,"
                " origin_record_id, origin_occurred_at, action_scope) values"
                " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (workspace_id, unit["company_id"], deal["id"], unit["source"], unit["thread_id"], record_ids,
                 i["type"], i["text"], i["quote"], i["owner_side"], i["owner_name"], i["due_date"], when,
                 i["confidence"], extractor.EXTRACTOR_VERSION, i["origin_record_id"], i["origin_occurred_at"],
                 i.get("action_scope")),
            )

    for old in existing:
        if old["id"] in matched or old["status"] in ("done", "dismissed"):
            continue
        origin = old["origin_record_id"]
        sources = records
        if origin is not None:
            sources = [by_id[origin]] if origin in by_id else []
        covered = any(same_action(old, i, field) for i in kept for field in ("quote", "text"))
        if (identity(old) not in seen and not covered and old["type"] in ("commitment", "next_step")
                and any(extractor.quote_in_record(old["quote"], r) for r in sources)):
            # A newer reply can make the extractor omit an existing action. Keep it for evidence-based review.
            conn.execute(
                "update insights set company_id = %s, opportunity_id = %s, source_record_ids = %s,"
                " last_extracted_at = now() where id = %s",
                (unit["company_id"], deal["id"], record_ids, old["id"]),
            )
        else:
            conn.execute("delete from insights where id = %s", (old["id"],))


@router.post("/extract")
def extract_insights(payload: ExtractRequest, x_ingest_token: str | None = Header(default=None)) -> dict:
    """Extract one batch. With backfill_scope, also review older open actions and recent Gmail threads."""
    require_token(x_ingest_token)
    client = _claude()
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        deals = {str(d["company_id"]): d for d in conn.execute(
            "select o.id, o.company_id, c.name as company, o.stage, o.owner_code from opportunities o"
            " join companies c on c.id = o.company_id where o.workspace_id = %s and o.id::text = any(%s)",
            (workspace_id, payload.opportunity_ids))}
        team = conn.execute("select owner_code, full_name, email from team_members where workspace_id = %s"
                            " order by owner_code", (workspace_id,)).fetchall()
        system_prompt = extractor.build_system_prompt(team)
        params = {"workspace_id": workspace_id, "company_ids": list(deals), "kept_versions": list(extractor.KEPT_VERSIONS),
                  "current_version": extractor.EXTRACTOR_VERSION, "backfill_scope": payload.backfill_scope,
                  "limit": settings.classify_batch_threads}
        units = conn.execute(
            _PENDING_EXTRACTION + " order by latest desc nulls last limit %(limit)s", params,
        ).fetchall()

        counts = {"units": len(units), "insights": 0, "dropped_quotes": 0, "failed": 0}
        touched = set()
        for unit in units:
            deal = deals[str(unit["company_id"])]
            key = (workspace_id, unit["source"], unit["thread_id"])
            records = conn.execute(
                "select id, source, occurred_at, updated_at, extracted_version, subject, participants, body, meta"
                " from source_records where workspace_id = %s and source = %s and thread_id = %s"
                " order by occurred_at nulls first, id", key,
            ).fetchall()
            conn.commit()
            if not records or all(r["extracted_version"] == extractor.EXTRACTOR_VERSION for r in records):
                continue
            if unit["source"] == "gmail":
                for r in records:
                    # Stored Gmail normally has quotes stripped already. Ignore legacy quoted lines as well.
                    r["body"] = "\n".join(line for line in gmail.strip_quoted(r["body"] or "").splitlines()
                                          if not line.lstrip().startswith(">"))
            refs = {f"M{n}": r for n, r in enumerate(records, start=1)}
            if unit["source"] == "gmail":
                text = classifier.render_thread([
                    {**r, "external_id": ref, "occurred_at": str(r["occurred_at"]) if r["occurred_at"] else None}
                    for ref, r in refs.items()
                ])
            else:
                render = classifier.render_note if unit["source"] == "notes" else classifier.render_event
                text = f"Source record: M1\n{render(records[0])}"
            new_source_refs = [ref for ref, record in refs.items() if record["extracted_version"] is None]
            try:
                result = extractor.extract(client, settings.anthropic_model, system_prompt, deal, text,
                                           new_source_refs=new_source_refs)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                log.warning("extract unit failed: %s", type(exc).__name__)
                counts["failed"] += 1
                continue
            kept, dropped = extractor.validate(result, text, refs)
            with conn.transaction():
                current = {r["id"]: r["updated_at"] for r in conn.execute(
                    "select id, updated_at from source_records where workspace_id = %s and source = %s"
                    " and thread_id = %s order by id for update", key,
                )}
                if current != {r["id"]: r["updated_at"] for r in records}:
                    continue
                _write_extracted_insights(conn, workspace_id, unit, deal, records, kept)
                conn.execute("update source_records set extracted_version = %s where id = any(%s)",
                             (extractor.EXTRACTOR_VERSION, list(current)))
            counts["insights"] += len(kept)
            counts["dropped_quotes"] += dropped
            touched.add(str(deal["id"]))

        remaining = conn.execute(
            "select count(*) as n from (" + _PENDING_EXTRACTION + ") u", params,
        ).fetchone()["n"]
    log.info("extract run: %s remaining_units=%s", counts, remaining)
    return {**counts, "remaining_units": remaining, "touched_opportunity_ids": sorted(touched)}


def _claude() -> anthropic.Anthropic:
    if settings.anthropic_api_key is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value(), default_headers=headers)


@router.post("/profile")
def build_profiles(payload: ExtractRequest, x_ingest_token: str | None = Header(default=None)) -> dict:
    """Write a fresh profile for each given deal from its extracted insights."""
    require_token(x_ingest_token)
    client = _claude()
    results = []
    for opportunity_id in payload.opportunity_ids:
        deal_view = opportunities.opportunity_timeline(opportunity_id, x_ingest_token=x_ingest_token)
        insights = opportunities.opportunity_insights(opportunity_id, x_ingest_token=x_ingest_token)
        for n, i in enumerate(insights, start=1):
            i["ref"] = f"I{n}"
        deal = deal_view["opportunity"]
        header = {k: deal_view[k] for k in ("counts", "last_activity_at", "next_meeting")}
        body = profiler.render_inputs(deal, header, deal_view["contacts"], insights, timeline.utcnow().date().isoformat())
        try:
            profile = profiler.generate(client, settings.anthropic_model, body, stage=deal["stage"])
        except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
            log.warning("profile failed: %s", type(exc).__name__)
            results.append({"opportunity_id": opportunity_id, "company": deal["company"], "failed": True})
            continue
        today = timeline.utcnow().date().isoformat()
        data, dropped = profiler.check(profile, {i["ref"] for i in insights} | {"P1", "A1"})
        data["refs"] = {i["ref"]: i["id"] for i in insights}
        data["facts"] = profiler.facts(deal, header, today)
        data["dropped_uncited"] = dropped
        with db.connect() as conn:
            workspace_id = _workspace_id(conn)
            conn.execute(
                "insert into opportunity_profiles (workspace_id, opportunity_id, profile, insight_ids,"
                " profiler_version, model) values (%s, %s, %s, %s, %s, %s)",
                (workspace_id, opportunity_id, Jsonb(data), [i["id"] for i in insights],
                 profiler.PROFILER_VERSION, settings.anthropic_model))
        results.append({"opportunity_id": opportunity_id, "company": deal["company"], "insights": len(insights),
                        "statements": sum(len(data[k]) for k in ("needs", "buying_signals", "objections",
                                                                 "blockers", "open_commitments",
                                                                 "recommended_actions")),
                        "dropped_uncited": dropped})
    return {"profiles": results}


@router.post("/reconcile")
def reconcile_commitments(x_ingest_token: str | None = Header(default=None)) -> dict:
    """Mark open Bianca-owed commitments / next steps 'possibly_done' when later activity shows completion."""
    require_token(x_ingest_token)
    return reconcile(None)


def _own_record_ids(item: dict) -> list:
    """The origin message, or every record of the unit for an item extracted before origins existed."""
    return [item["origin_record_id"]] if item["origin_record_id"] else list(item["source_record_ids"] or [])


def _own_context(item: dict, own: dict) -> dict:
    """What the checker reads as the item's own source, and the record an "event passed" verdict cites."""
    ids = _own_record_ids(item)
    records = [own[rid] for rid in ids if rid in own]
    return {"record_id": ids[-1] if ids else None,
            "own_text": " ".join(f"{r.get('subject') or ''} {r.get('body') or ''}" for r in records)}


def reconcile(opportunity_ids: list[str] | None) -> dict:
    """The completion check for the given deals (all deals when None)."""
    client = _claude()
    today = timeline.utcnow().date().isoformat()
    results, counts = [], {"items_checked": 0, "deals": 0, "marked": 0, "failed_deals": 0}
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        # Items without a single origin message or a reliable origin date (legacy rows, undated notes) are still
        # checked, against their whole unit and their stored date, as before origins existed.
        targets = conn.execute(
            "select i.id, i.company_id, c.name as company, i.text, i.quote, i.due_date, i.source,"
            " i.origin_record_id, i.source_record_ids, coalesce(i.origin_occurred_at, i.occurred_at) as occurred_at,"
            " i.updated_at from insights i join companies c on c.id = i.company_id"
            " where i.workspace_id = %s and i.type in ('commitment', 'next_step') and i.owner_side = 'bianca'"
            " and i.status = 'open'"
            " and coalesce(i.origin_occurred_at, i.occurred_at) between now() - interval '45 days' and now()"
            " and (%s::text[] is null or i.opportunity_id::text = any(%s))"
            " order by i.company_id, coalesce(i.origin_occurred_at, i.occurred_at)",
            (workspace_id, opportunity_ids, opportunity_ids)).fetchall()
        by_company: dict[str, list[dict]] = {}
        for t in targets:
            by_company.setdefault(str(t["company_id"]), []).append(t)

        for company_id, items in by_company.items():
            own = {r["id"]: r for r in conn.execute(
                "select id, subject, body from source_records where workspace_id = %s and id = any(%s)",
                (workspace_id, [rid for it in items for rid in _own_record_ids(it)]))}
            item_map = {f"T{n}": {**it, "ref": f"T{n}", **_own_context(it, own)}
                        for n, it in enumerate(items, start=1)}
            later = conn.execute(
                "select id, source, subject, body, occurred_at from source_records where workspace_id = %s"
                " and company_id = %s and status = 'classified' and body is not null and occurred_at > %s"
                " and occurred_at <= now() order by occurred_at desc limit %s",
                (workspace_id, company_id, min(it["occurred_at"] for it in items), reconciler.MAX_LATER_RECORDS),
            ).fetchall()[::-1]
            rec_map = {f"R{n}": {**r, "ref": f"R{n}"} for n, r in enumerate(later, start=1)}
            body = reconciler.render(today, items[0]["company"], list(item_map.values()), list(rec_map.values()))
            conn.commit()
            try:
                verdicts = reconciler.check(client, settings.anthropic_model, body)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                log.warning("reconcile failed for a deal: %s", type(exc).__name__)
                counts["failed_deals"] += 1
                continue
            accepted = reconciler.validate(verdicts, item_map, rec_map)
            by_id = {it["id"]: it for it in items}
            with conn.transaction():
                for a in accepted:
                    item = by_id[a["insight_id"]]
                    evidence = {k: a[k] for k in ("state", "record_id", "quote", "reason")}
                    evidence.update(checked_at=timeline.utcnow().isoformat(), checker_version=reconciler.CHECKER_VERSION)
                    updated = conn.execute(
                        "update insights set status = 'possibly_done', completion = %s"
                        " where id = %s and status = 'open' and updated_at = %s",
                        (Jsonb(evidence), item["id"], item["updated_at"]),
                    )
                    if updated.rowcount:
                        counts["marked"] += 1
                        results.append({"deal": item["company"], "item": item["text"], "state": a["state"],
                                        "reason": a["reason"]})
            counts["deals"] += 1
            counts["items_checked"] += len(items)
    log.info("reconcile: %s", counts)
    return {**counts, "marked_items": results}
