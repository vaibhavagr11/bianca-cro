from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from app import sales_account, timeline
from test_sales_connector_permissions import TEST_DATABASE_URL, sales_database  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires a migrated local test database")

NOW = datetime(2026, 11, 11, 14, tzinfo=timezone.utc)
CLASSIFICATION = {"category": "prospect", "is_commercial": True}
OUTBOUND = {"from": [{"address": "founder@bianca.test"}],
            "to": [{"address": "sales@example.com", "name": "Ana Example"}]}
INBOUND = {"from": [{"address": "sales@example.com", "name": "Ana Example"}],
           "to": [{"address": "founder@bianca.test"}]}


@pytest.fixture
def account_case(sales_database, monkeypatch):
    login, _, password, workspaces = sales_database
    workspace = workspaces["bound"]
    monkeypatch.setattr(timeline, "utcnow", lambda: NOW)
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        admin.execute("update public.workspaces set internal_domains = array['bianca.test'] where id = %s", (workspace,))
        account, company = admin.execute(
            "update public.opportunities set stage_rank = 2, owner_code = 'RG', primary_buyer = 'Ana Example'"
            " where workspace_id = %s returning id, company_id", (workspace,),
        ).fetchone()
        admin.execute("update public.contacts set name = 'Ana Example' where workspace_id = %s", (workspace,))
        admin.execute("update public.company_domains set trusted = true where workspace_id = %s", (workspace,))

    with psycopg.connect(TEST_DATABASE_URL, user=login, password=password, row_factory=dict_row) as reader:
        yield reader, workspace, account, company


def _record(admin, workspace, company, key, source, days, text, *, participants=None, meta=None,
            classification=CLASSIFICATION, thread=None):
    return admin.execute(
        """insert into public.source_records (workspace_id, company_id, source, external_id, thread_id, occurred_at,
               subject, participants, meta, content_hash, body, status, classification)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'classified', %s) returning id""",
        (workspace, company, source, key, thread or key, NOW + timedelta(days=days), key, Jsonb(participants or {}),
         Jsonb(meta or {}), f"hash-{key}", text, Jsonb(classification)),
    ).fetchone()[0]


def _insight(admin, workspace, account, company, origin, source, quote, *, kind="commitment", side="bianca",
             status="open", due=None, completion=None, owner=None):
    return admin.execute(
        """insert into public.insights (workspace_id, opportunity_id, company_id, source, unit_id, source_record_ids,
               type, text, quote, confidence, extractor_version, origin_record_id, status, owner_side, owner_name,
               due_date, completion, action_scope)
           values (%s, %s, %s, %s, 'test', %s, %s, %s, %s, 'high', 'test', %s, %s, %s, %s, %s, %s,
               'deal_progression') returning id""",
        (workspace, account, company, source, [origin], kind, quote, quote, origin, status, side, owner, due,
         Jsonb(completion) if completion else None),
    ).fetchone()[0]


def test_stakeholder_map_keeps_pipeline_buyer_and_observed_roles_separate(account_case):
    reader, workspace, account, company = account_case
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        email = _record(admin, workspace, company, "stakeholder-email", "gmail", -2, "A useful reply.",
                        participants=INBOUND, meta={"mailboxes": ["RG"]})
        _record(admin, workspace, company, "invite", "calendar", 2, "Agenda",
                participants={"attendees": [{"address": "other@example.com", "name": "Jo Other"}]})
        _record(admin, workspace, company, "private-stakeholder", "gmail", -1, "Personal",
                participants={"from": [{"address": "private@example.com"}]},
                classification={"category": "personal", "is_commercial": False})

    result = sales_account.get_stakeholder_map(reader, workspace, account, viewer_mailbox="RG")
    assert result["primary_buyer"] == {"as_recorded": "Ana Example", "source": "pipeline_sheet"}
    ana, jo = result["stakeholders"]
    assert ana["recorded_contact"] and ana["observations"][0]["record_id"] == str(email)
    assert ana["observations"][0]["relationship"] == "email_sender"
    assert ana["observations"][0]["url"].endswith("#all/stakeholder-email")
    assert jo["email"] == "other@example.com" and not jo["recorded_contact"]
    assert jo["observations"][0]["relationship"] == "scheduled_invitee"
    assert jo["observations"][0]["attendance"] == "unverified"


def test_commitment_status_and_reply_followups_require_current_evidence(account_case):
    reader, workspace, account, company = account_case
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        source = _record(admin, workspace, company, "promise", "gmail", -8, "We will send the price by Friday.",
                         participants=OUTBOUND)
        completed = _record(admin, workspace, company, "sent", "gmail", -4, "The pricing is attached.",
                            participants=OUTBOUND)
        _insight(admin, workspace, account, company, source, "gmail", "We will send the price by Friday.",
                 status="done", due=date(2026, 11, 6), owner="Founder",
                 completion={"via": "brief", "by": "RG", "at": "2026-11-08T14:00:00Z", "state": "done",
                             "record_id": str(completed), "quote": "The pricing is attached."})
        _insight(admin, workspace, account, company, source, "gmail", "A quote absent from the message.")
        for thread in ("waiting", "manual-close", "customer-replied", "stale-hash"):
            quote = "Can you confirm the budget owner?"
            expected_hash = f"hash-{thread}" if thread != "stale-hash" else "stale"
            outgoing = _record(
                admin, workspace, company, thread, "gmail", -10, quote, participants=OUTBOUND,
                classification={**CLASSIFICATION, "reply_expectation": {
                    "state": "expected", "latest_message_id": thread, "latest_content_hash": expected_hash,
                    "evidence_message_id": thread, "evidence_content_hash": expected_hash,
                    "evidence_quote": quote, "response": "Confirm the budget owner",
                }},
            )
            if thread == "waiting":
                _insight(admin, workspace, account, company, outgoing, "gmail", quote, kind="next_step", side="customer")
        _record(admin, workspace, company, "replied", "gmail", -2, "I own the budget.",
                thread="customer-replied", participants=INBOUND)
        admin.execute(
            "insert into public.brief_actions (workspace_id, kind, ref, action, created_at)"
            " values (%s, 'thread', 'manual-close', 'done', %s)", (workspace, NOW - timedelta(days=2)),
        )

    result = sales_account.list_commitments_and_followups(reader, workspace, account)
    request, done = result["items"]
    assert request["kind"] == "request_or_proposed_step"
    assert request["reply_expectation"]["expected_response"] == "Confirm the budget owner"
    assert done["kind"] == "commitment" and done["recorded_status"] == "done"
    assert done["responsible_person"] == "Founder" and done["due_date"] == date(2026, 11, 6)
    assert done["completion"]["manual_confirmation"]["by"] == "RG"
    assert done["completion"]["evidence"]["record_id"] == str(completed)
    assert sales_account.list_commitments_and_followups(
        reader, workspace, account, due_by=date(2026, 11, 7), include_undated=False,
    )["items"] == [done]


def test_meeting_prep_uses_next_live_invite_and_labels_prior_evidence(account_case):
    reader, workspace, account, company = account_case
    with psycopg.connect(TEST_DATABASE_URL) as admin:
        _record(admin, workspace, company, "canceled", "calendar", 1, "Canceled",
                meta={"status": "cancelled"})
        invite = _record(admin, workspace, company, "workshop", "calendar", 3, "Workshop",
                         participants={"attendees": [{"address": "sales@example.com", "name": "Ana Example"}]})
        blocker = _record(admin, workspace, company, "notes", "notes", -4, "Pricing approval is pending.",
                          meta={"date_inferred": True, "doc_id": "meeting-notes"})
        insight = _insight(admin, workspace, account, company, blocker, "notes", "Pricing approval is pending.",
                           kind="blocker")
        followup = _record(admin, workspace, company, "prep", "notes", -3, "We will confirm pricing.",
                           meta={"doc_id": "prep-notes"})
        action = _insight(admin, workspace, account, company, followup, "notes", "We will confirm pricing.",
                          owner="Founder", due=date(2026, 11, 14))
        _insight(admin, workspace, account, company, followup, "notes", "We will confirm pricing.", status="done")

    result = sales_account.prepare_sales_meeting(reader, workspace, account)
    assert result["meeting"]["record_id"] == str(invite)
    assert result["meeting"]["attendance"] == "unverified"
    assert result["meeting"]["external_invitees"] == [{"name": "Ana Example", "email": "sales@example.com"}]
    assert any(question.get("based_on") == str(insight) for question in result["proposed_questions"])
    assert result["prior_themes"][0]["date_estimated"]
    assert result["prior_themes"][0]["url"].endswith("/meeting-notes")
    assert len(result["open_actions"]) == 1
    pending = result["open_actions"][0]
    assert pending["recorded_status"] == "open" and pending["responsible_person"] == "Founder"
    assert pending["due_date"] == date(2026, 11, 14) and pending["evidence"]["insight_id"] == str(action)
    assert sales_account.prepare_sales_meeting(reader, workspace, account, within_days=2)["meeting"] is None
