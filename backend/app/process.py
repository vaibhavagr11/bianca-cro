"""Processing endpoints called by n8n after ingestion. Each call handles a bounded batch and is safe to repeat."""

import logging

import anthropic
from fastapi import APIRouter, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel
from psycopg.types.json import Jsonb

from app import classifier, db, extractor, opportunities, profiler, reconciler, resolution, timeline
from app.config import settings
from app.ingest import _fail, _workspace_id, require_token
from app.pipeline import normalize_name

log = logging.getLogger("bianca_cro.process")
router = APIRouter(prefix="/process")


@router.post("/classify")
def classify_threads(x_ingest_token: str | None = Header(default=None)) -> dict:
    """Classify up to classify_batch_threads pending units: Gmail threads and notes-doc meetings, newest first."""
    require_token(x_ingest_token)
    if settings.anthropic_api_key is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    workspace_header = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
    )
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), default_headers=workspace_header
    )

    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        system_prompt, company_ids = _context(conn, workspace_id)
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

        counts = {"classified": 0, "commercial": 0, "linked_to_company": 0, "failed": 0}
        try:
            for source, thread_id in threads:
                records = conn.execute(
                    "select id, external_id, occurred_at::text, subject, participants, body, meta from source_records"
                    " where workspace_id = %s and source = %s and thread_id = %s",
                    (workspace_id, source, thread_id),
                ).fetchall()
                renderers = {"notes": lambda r: classifier.render_note(r[0]),
                             "calendar": lambda r: classifier.render_event(r[0])}
                thread_text = renderers.get(source, classifier.render_thread)(records)
                try:
                    result = classifier.classify(client, settings.anthropic_model, system_prompt, thread_text, source)
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
        except Exception as exc:
            _fail(conn, run_id, type(exc).__name__)
            log.exception("classify run %s failed", run_id)
            raise HTTPException(status_code=500, detail="classify failed")

        remaining = conn.execute(
            "select count(distinct (source, thread_id)) as n from source_records"
            " where workspace_id = %s and status = 'pending'",
            (workspace_id,),
        ).fetchone()["n"]
        conn.execute(
            "update sync_runs set status = 'succeeded', finished_at = now(), rows_created = %s, rows_skipped = %s"
            " where id = %s",
            (counts["classified"], counts["failed"], run_id),
        )
        conn.commit()

    log.info("classify run %s: %s remaining_threads=%s", run_id, counts, remaining)
    return {"sync_run_id": str(run_id), "threads": len(threads), **counts, "remaining_threads": remaining}


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


@router.post("/extract")
def extract_insights(payload: ExtractRequest, x_ingest_token: str | None = Header(default=None)) -> dict:
    """Extract insights for up to classify_batch_threads not-yet-extracted units of the given deals."""
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
        units = conn.execute(
            "select source, thread_id, company_id from source_records where workspace_id = %s"
            " and company_id::text = any(%s) and status = 'classified' and body is not null"
            " and (classification->>'is_commercial')::bool"
            " group by source, thread_id, company_id"
            " having bool_or(extracted_version is distinct from %s)"
            " order by max(occurred_at) desc nulls last limit %s",
            (workspace_id, list(deals), extractor.EXTRACTOR_VERSION, settings.classify_batch_threads),
        ).fetchall()

        counts = {"units": len(units), "insights": 0, "dropped_quotes": 0, "failed": 0}
        for unit in units:
            deal = deals[str(unit["company_id"])]
            records = conn.execute(
                "select id, external_id, occurred_at::text, subject, participants, body, meta from source_records"
                " where workspace_id = %s and source = %s and thread_id = %s order by occurred_at",
                (workspace_id, unit["source"], unit["thread_id"]),
            ).fetchall()
            renderers = {"notes": lambda r: classifier.render_note(r[0]),
                         "calendar": lambda r: classifier.render_event(r[0])}
            text = renderers.get(unit["source"], classifier.render_thread)(records)
            try:
                result = extractor.extract(client, settings.anthropic_model, system_prompt, deal, text)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                log.warning("extract unit failed: %s", type(exc).__name__)
                counts["failed"] += 1
                continue
            kept, dropped = extractor.validate(result, text)
            record_ids = [r["id"] for r in records]
            occurred_at = records[-1]["occurred_at"]
            with conn.transaction():
                # A thread re-extracted after a new message keeps the review state of insights it finds again.
                reviewed = {extractor.insight_key(r["type"], r["quote"]): r for r in conn.execute(
                    "select type, quote, status, completion from insights where workspace_id = %s and source = %s"
                    " and unit_id = %s and status <> 'open'", (workspace_id, unit["source"], unit["thread_id"]))}
                conn.execute("delete from insights where workspace_id = %s and source = %s and unit_id = %s",
                             (workspace_id, unit["source"], unit["thread_id"]))
                for i in kept:
                    prior = reviewed.get(extractor.insight_key(i["type"], i["quote"]))
                    conn.execute(
                        "insert into insights (workspace_id, company_id, opportunity_id, source, unit_id,"
                        " source_record_ids, type, text, quote, owner_side, owner_name, due_date, occurred_at,"
                        " confidence, extractor_version, status, completion) values"
                        " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (workspace_id, unit["company_id"], deal["id"], unit["source"], unit["thread_id"],
                         record_ids, i["type"], i["text"], i["quote"], i["owner_side"], i["owner_name"],
                         i["due_date"], occurred_at, i["confidence"], extractor.EXTRACTOR_VERSION,
                         prior["status"] if prior else "open",
                         Jsonb(prior["completion"]) if prior and prior["completion"] else None))
                conn.execute("update source_records set extracted_version = %s where id = any(%s)",
                             (extractor.EXTRACTOR_VERSION, record_ids))
            counts["insights"] += len(kept)
            counts["dropped_quotes"] += dropped

        remaining = conn.execute(
            "select count(*) as n from (select 1 from source_records where workspace_id = %s"
            " and company_id::text = any(%s) and status = 'classified' and body is not null"
            " and (classification->>'is_commercial')::bool group by source, thread_id"
            " having bool_or(extracted_version is distinct from %s)) u",
            (workspace_id, list(deals), extractor.EXTRACTOR_VERSION),
        ).fetchone()["n"]
    log.info("extract run: %s remaining_units=%s", counts, remaining)
    return {**counts, "remaining_units": remaining}


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
            profile = profiler.generate(client, settings.anthropic_model, body)
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


def reconcile(opportunity_ids: list[str] | None) -> dict:
    """The completion check for the given deals (all deals when None)."""
    client = _claude()
    today = timeline.utcnow().date().isoformat()
    results, counts = [], {"items_checked": 0, "deals": 0, "marked": 0, "failed_deals": 0}
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        targets = conn.execute(
            "select i.id, i.company_id, c.name as company, i.text, i.quote, i.due_date, i.occurred_at, i.source,"
            " i.source_record_ids from insights i join companies c on c.id = i.company_id"
            " where i.workspace_id = %s and i.type in ('commitment', 'next_step') and i.owner_side = 'bianca'"
            " and i.status = 'open' and i.occurred_at >= now() - interval '45 days'"
            " and (%s::text[] is null or i.opportunity_id::text = any(%s))"
            " order by i.company_id, i.occurred_at", (workspace_id, opportunity_ids, opportunity_ids)).fetchall()
        by_company: dict[str, list[dict]] = {}
        for t in targets:
            by_company.setdefault(str(t["company_id"]), []).append(t)

        for company_id, items in by_company.items():
            own = {str(r["id"]): r for r in conn.execute(
                "select id, subject, body from source_records where id = any(%s)",
                ([rid for it in items for rid in it["source_record_ids"]],))}
            item_map = {}
            for n, it in enumerate(items, start=1):
                recs = [own.get(str(rid)) for rid in it["source_record_ids"]]
                item_map[f"T{n}"] = {**it, "ref": f"T{n}", "id": it["id"], "record_id": it["source_record_ids"][-1],
                                     "own_text": " ".join(f"{r['subject'] or ''} {r['body'] or ''}" for r in recs if r)}
            later = conn.execute(
                "select id, source, subject, body, occurred_at from source_records where workspace_id = %s"
                " and company_id = %s and status = 'classified' and body is not null and occurred_at > %s"
                " and occurred_at <= now() order by occurred_at desc limit %s",
                (workspace_id, company_id, min(it["occurred_at"] for it in items), reconciler.MAX_LATER_RECORDS),
            ).fetchall()[::-1]
            rec_map = {f"R{n}": {**r, "ref": f"R{n}"} for n, r in enumerate(later, start=1)}
            body = reconciler.render(today, items[0]["company"], list(item_map.values()), list(rec_map.values()))
            try:
                verdicts = reconciler.check(client, settings.anthropic_model, body)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as exc:
                log.warning("reconcile failed for a deal: %s", type(exc).__name__)
                counts["failed_deals"] += 1
                continue
            accepted = reconciler.validate(verdicts, item_map, rec_map)
            with conn.transaction():
                for a in accepted:
                    evidence = {k: a[k] for k in ("state", "record_id", "quote", "reason")}
                    evidence.update(checked_at=timeline.utcnow().isoformat(), checker_version=reconciler.CHECKER_VERSION)
                    conn.execute("update insights set status = 'possibly_done', completion = %s where id = %s",
                                 (Jsonb(evidence), a["insight_id"]))
            counts["deals"] += 1
            counts["items_checked"] += len(items)
            counts["marked"] += len(accepted)
            text_of = {str(v["id"]): v["text"] for v in item_map.values()}
            results += [{"deal": items[0]["company"], "item": text_of[str(a["insight_id"])], "state": a["state"],
                         "reason": a["reason"]} for a in accepted]
    log.info("reconcile: %s", counts)
    return {**counts, "marked_items": results}
