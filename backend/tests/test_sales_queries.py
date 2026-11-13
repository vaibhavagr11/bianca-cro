from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app import sales_queries


@pytest.mark.parametrize(("sender", "recipient", "subject", "expected"), [
    ("ana@customer.test", "founder@bianca.test", "Budget", "inbound"),
    ("founder@bianca.test", "ana@customer.test", "Budget", "outbound"),
    ("ana@freemail.test", "founder@bianca.test", "Budget", "inbound"),
    ("ana@customer.test", "founder@bianca.test", "Automatic reply: Budget", None),
    ("no-reply@customer.test", "founder@bianca.test", "Budget", None),
    ("founder@bianca.test", "colleague@bianca.test", "Budget", None),
    ("consultant@partner.test", "founder@bianca.test", "Budget", None),
    ("ana@customer.test", "consultant@partner.test", "Budget", None),
])
def test_customer_email_needs_real_sender_and_both_sides(sender, recipient, subject, expected):
    row = {"participants": {"from": [{"address": sender}], "to": [{"address": recipient}]}, "subject": subject}
    assert sales_queries._email_direction(row, {"bianca.test"}, {"ana@freemail.test"}, {"customer.test"}) == expected


def test_citations_require_the_viewers_mailbox_and_resolve_aliases():
    row = {"id": "record", "source": "gmail", "subject": "Budget", "thread_id": "canonical",
           "occurred_at": datetime(2026, 10, 31, 15, tzinfo=UTC), "meta": {"mailboxes": ["RG", "NC"]}}
    timezone = ZoneInfo("America/Argentina/Buenos_Aires")
    aliases = {"canonical": "founder-thread"}
    assert sales_queries._source(row, timezone, aliases, "NC")["url"].endswith("#all/founder-thread")
    assert sales_queries._source(row, timezone, {}, "NC")["url"] is None
    assert sales_queries._source(row, timezone, aliases, "NDL")["url"] is None
    row["meta"] = {"mailboxes": ["NC"]}
    assert sales_queries._source(row, timezone, {}, "NC")["url"].endswith("#all/canonical")


def test_notes_require_an_exact_speaker_label_and_keep_the_recorded_date():
    body = """Attendees: Ana García, Jo Ramos
Jo Ramos: Ana cares about timeline.
Ana: Price is the priority.
- **Ana García**: Me preocupa el tiempo de implementación.
Ana García / Jo Ramos: This is ambiguous.
"""
    assert list(sales_queries._speaker_lines(body, {"ana garcia"})) == [
        ("Ana García", "Me preocupa el tiempo de implementación."),
    ]
    row = {"id": "record", "source": "notes", "subject": "October conversation",
           "occurred_at": datetime(2026, 10, 31, tzinfo=UTC), "meta": {"doc_id": "fixture", "date_inferred": True}}
    evidence = sales_queries._source(row, ZoneInfo("America/Argentina/Buenos_Aires"), {}, None)
    assert evidence["occurred_at"] == "2026-10-31"
    assert evidence["date_estimated"]
    assert evidence["url"] == "https://docs.google.com/document/d/fixture"


def test_stakeholder_email_normalization_preserves_international_address():
    assert sales_queries._speaker_key(" <ÁNA@CUSTOMER.TEST> ") == "ána@customer.test"
    body = "**ÁNA@CUSTOMER.TEST**: Agreed.\nana@customer.test: Unrelated."
    assert list(sales_queries._speaker_lines(body, {"ána@customer.test"})) == [
        ("ÁNA@CUSTOMER.TEST", "Agreed."),
    ]


def test_account_context_matches_stakeholder_mentions_without_using_attendance():
    body = """Attendees: Ana García
- Participantes: Ana García
Jo Ramos: Ana García had a concern about timing.
Mariana García works for another company.
The proposal went to ÁNA@CUSTOMER.TEST.
The proposal went to alt+ána@customer.test.
"""
    assert list(sales_queries._unattributed_lines(body, {"ana garcia", "ána@customer.test"})) == [
        "Jo Ramos: Ana García had a concern about timing.",
        "The proposal went to ÁNA@CUSTOMER.TEST.",
    ]


@pytest.mark.parametrize(("phrase", "owner", "origin", "expected"), [
    ("We agreed to review the proposal on Friday.", "customer", "r", True),
    ("Yes, let’s kick off the pilot Monday.", "customer", "r", True),
    ("Me comprometo a presentar la propuesta a Compras.", "customer", "r", True),
    ("We will introduce you to procurement.", "customer", "r", True),
    ("Can you send us a proposal?", "customer", "r", False),
    ("We will not go ahead with the pilot.", "customer", "r", False),
    ("We agreed to postpone the pilot.", "customer", "r", False),
    ("We will introduce you to procurement if we secure budget.", "customer", "r", False),
    ("Vamos a revisar el presupuesto si lo aprueban.", "customer", "r", False),
    ("We will introduce you to procurement.", "bianca", "r", False),
    ("We will introduce you to procurement.", "customer", "other-record", False),
])
def test_buyer_actions_need_an_exact_origin_owner_and_affirmative_wording(phrase, owner, origin, expected):
    assert sales_queries._is_buyer_action({"id": "r", "origin_record_id": origin, "owner_side": owner,
                                          "quote": phrase}) is expected
