from datetime import UTC, date, datetime, timedelta
from os import environ
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import sales_outlook

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
FOUNDER = "founder@bianca.test"


@pytest.fixture
def pipeline(monkeypatch):
    dsn = environ.get("BIANCA_SALES_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires a migrated local BIANCA_SALES_TEST_DATABASE_URL")
    connection = conninfo_to_dict(dsn)
    assert connection.get("host") in {"localhost", "127.0.0.1", "::1"} and not connection.get("hostaddr")
    monkeypatch.setattr(sales_outlook.timeline, "utcnow", lambda: NOW)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        workspace = conn.execute(
            "insert into public.workspaces (name, timezone, internal_domains)"
            " values (%s, 'UTC', array['bianca.test']) returning id", (f"outlook-{uuid4()}",),
        ).fetchone()["id"]
        try:
            yield conn, workspace
        finally:
            conn.rollback()


def deal(conn, workspace, name, stage):
    company = conn.execute(
        "insert into public.companies (workspace_id, name, normalized_name) values (%s, %s, %s) returning id",
        (workspace, name, name.lower()),
    ).fetchone()["id"]
    opportunity = conn.execute(
        "insert into public.opportunities (workspace_id, company_id, stage, stage_rank, owner_code, row_hash)"
        " values (%s, %s, %s, %s, 'RG', 'fixture') returning id", (workspace, company, str(stage), stage),
    ).fetchone()["id"]
    conn.execute(
        "insert into public.contacts (workspace_id, company_id, name, email) values (%s, %s, 'Ana Buyer', %s)",
        (workspace, company, f"ana@{company}.test"),
    )
    return company, opportunity


def record(conn, workspace, company, body, days, *, source="gmail", inbound=True, meta=None):
    thread = str(uuid4())
    buyer = f"ana@{company}.test"
    sender, recipient = (buyer, FOUNDER) if inbound else (FOUNDER, buyer)
    participants = {"from": [{"address": sender}], "to": [{"address": recipient}]} if source == "gmail" else {}
    return conn.execute(
        """insert into public.source_records (workspace_id, company_id, source, external_id, thread_id, occurred_at,
                                             subject, participants, meta, body, content_hash, status, classification)
           values (%s, %s, %s, %s, %s, %s, 'Pilot thread', %s, %s, %s, 'fixture', 'classified', %s) returning id""",
        (workspace, company, source, thread, thread if source == "gmail" else None, NOW + timedelta(days=days),
         Jsonb(participants), Jsonb(meta or {}), body, Jsonb({"category": "prospect", "is_commercial": True})),
    ).fetchone()["id"]


def insight(conn, workspace, company, opportunity, quote, record_ids, *, source="gmail", kind="need", owner=None,
            due=None, status="open", scope="deal_progression", origin=True):
    conn.execute(
        """insert into public.insights (workspace_id, company_id, opportunity_id, source, unit_id, source_record_ids,
                                       type, text, quote, owner_side, due_date, status, confidence, extractor_version,
                                       origin_record_id, action_scope)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'high', 'fixture', %s, %s)""",
        (workspace, company, opportunity, source, str(uuid4()), record_ids, kind, quote, quote, owner, due, status,
         record_ids[0] if origin else None, scope),
    )


def test_qualification_milestone_and_pilot_distinguish_evidence_from_intent(pipeline):
    conn, workspace = pipeline
    company, opportunity = deal(conn, workspace, "Pilot Foods", 4)

    def add(quote, *, source="gmail", kind="need", owner=None, inbound=True, due=None, status="open",
            scope="deal_progression"):
        note = f"Ana Buyer: {quote}" if source == "notes" else quote
        record_id = record(conn, workspace, company, note, -4, source=source, inbound=inbound,
                           meta={"doc_id": "fixture-doc"} if source == "notes" else {"mailboxes": ["RG"]})
        insight(conn, workspace, company, opportunity, quote, [record_id], source=source, kind=kind, owner=owner,
                due=due, status=status, scope=scope)
        return str(record_id)

    budget = add("We have a budget of $40,000.")
    criterion = add("The success criteria are 90% accuracy and weekly reporting.", source="notes")
    action = add("We will send the pilot data by December 1.", kind="commitment", owner="customer",
                 due=date(2026, 12, 1))
    conditional = add("We will start the pilot if procurement approves.", kind="next_step", owner="customer")
    possible = add("We will share the data definitions.", kind="commitment", owner="customer", status="possibly_done")
    legacy = add("We will share the credentials.", kind="commitment", owner="customer", scope=None)
    blocker = add("Legal review is blocking the pilot.", kind="blocker", inbound=False)
    add("We think your CFO will make the final decision.", inbound=False)
    add("Our approval process includes a purchase order.", scope="delivery")
    duplicated = "The decision date is December 15."
    records = [record(conn, workspace, company, duplicated, days) for days in (-2, -3)]
    insight(conn, workspace, company, opportunity, duplicated, records, origin=False)
    event = record(conn, workspace, company, "Calendar description", 5, source="calendar", meta={"status": "tentative"})
    record(conn, workspace, company, "Cancelled meeting", 3, source="calendar", meta={"status": "cancelled"})

    qualification = sales_outlook.get_qualification_gaps(conn, workspace, opportunity, "RG")["qualification"]
    assert qualification["budget"]["buyer_evidence"][0]["record_id"] == budget
    assert qualification["budget"]["buyer_evidence"][0]["url"].startswith("https://mail.google.com/")
    assert qualification["success_criteria"]["buyer_evidence"][0]["record_id"] == criterion
    assert qualification["decision_maker"]["status"] == "unknown"
    assert [row["record_id"] for row in qualification["timing"]["buyer_evidence"]] == [action]
    assert qualification["decision_maker"]["question_if_unresolved"]

    milestone = sales_outlook.assess_next_milestone(conn, workspace, opportunity)
    assert milestone["milestone_state"] == "buyer_agreement_observed"
    assert [row["record_id"] for row in milestone["agreed_buyer_actions"]] == [action]
    assert milestone["agreed_buyer_actions"][0]["extracted_due_date"] == "2026-12-01"
    assert [row["record_id"] for row in milestone["proposed_or_other_pending_actions"]] == [conditional]
    assert [row["record_id"] for row in milestone["buyer_actions_possibly_completed"]] == [possible]
    assert [row["record_id"] for row in milestone["legacy_buyer_commitments_with_unverified_scope"]] == [legacy]
    assert [row["record_id"] for row in milestone["open_blockers_and_objections"]] == [blocker]
    assert milestone["conditional_scenario"]["candidate_insight_id"] == milestone["agreed_buyer_actions"][0]["insight_id"]
    assert [(row["record_id"], row["attendance"]) for row in milestone["scheduled_events"]] == [
        (str(event), "unverified"),
    ]

    pilot = sales_outlook.assess_pilot_readiness(conn, workspace, owner_code="rg")["pilots"][0]
    assert pilot["opportunity_id"] == str(opportunity)
    assert pilot["assessment"] == "open_blockers_recorded"
    assert pilot["criteria"]["decision_timing"]["status"] == "unknown"
    assert pilot["criteria"]["success_criteria"]["buyer_evidence"][0]["record_id"] == criterion


def test_reengagement_requires_old_buyer_evidence_and_looks_for_recent_direct_contact(pipeline):
    conn, workspace = pipeline
    quiet_company, quiet_deal = deal(conn, workspace, "Quiet Lead", 0)
    active_company, active_deal = deal(conn, workspace, "Recent Contact", 1)
    seller_company, seller_deal = deal(conn, workspace, "Seller Only", 1)
    stale_company, stale_deal = deal(conn, workspace, "Older Lead", 0)
    for company, opportunity, inbound, days in (
        (quiet_company, quiet_deal, True, -60), (active_company, active_deal, True, -45),
        (seller_company, seller_deal, False, -50), (stale_company, stale_deal, True, -210),
    ):
        quote = "We are interested in buying this for the rollout."
        source_id = record(conn, workspace, company, quote, days, inbound=inbound)
        insight(conn, workspace, company, opportunity, quote, [source_id], kind="buying_signal")

    record(conn, workspace, active_company, "Following up on the proposal.", -5, inbound=False)
    record(conn, workspace, quiet_company, "Past invitation without attendance", -5, source="calendar")
    record(conn, workspace, quiet_company, "Ana Buyer: Good to talk.", -2, source="notes", meta={"date_inferred": True})

    result = sales_outlook.find_reengagement_candidates(conn, workspace, owner_code="RG", lookback_days=400)
    assert [row["opportunity_id"] for row in result["candidates"]] == [str(quiet_deal), str(stale_deal)]
    candidate = result["candidates"][0]
    assert candidate["prior_buyer_evidence"][0]["buyer_attribution"] == "direct_customer_email"
    assert candidate["recent_direct_contact"] == "none_observed_in_synced_records"
    assert candidate["recent_unverified_activity"]["date_estimated"] is True
    assert sales_outlook.assess_pilot_readiness(conn, workspace, opportunity_id=quiet_deal)["status"] == "outside_pilot_stage"


@pytest.mark.parametrize(("quiet_days", "lookback_days"), [(0, 30), (30, 30), (366, 500), (30, 731)])
def test_reengagement_rejects_invalid_windows(quiet_days, lookback_days):
    with pytest.raises(ValueError, match="quiet_days"):
        sales_outlook.find_reengagement_candidates(None, None, quiet_days=quiet_days, lookback_days=lookback_days)
