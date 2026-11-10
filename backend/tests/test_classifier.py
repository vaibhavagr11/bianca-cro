import pytest
from fastapi.testclient import TestClient

from app.classifier import (
    ReplyExpectation, ThreadClassification, build_system_prompt, check, check_reply_expectation,
    latest_reply_message, render_thread,
)
from app.main import app

COMPANIES = {"acme foods": "Acme Foods", "globex": "Globex"}
THREAD = "From: Jane <jane@acme.com>\nSubject: Pricing\n\nCould you send the   pricing for the pilot by Friday?"
INTERNAL = {"bianca-ai.com"}


def result(**kw):
    base = dict(category="prospect", company="Acme Foods", new_company=None, confidence="high",
                reason="Asks for pilot pricing.", evidence_quote="send the pricing for the pilot")
    base.update(kw)
    return ThreadClassification(**base)


def test_check_accepts_exact_quote_and_company():
    out = check(result(), THREAD, COMPANIES)
    assert out["company"] == "Acme Foods"
    assert out["is_commercial"] is True
    assert out["confidence"] == "high"
    assert out["flags"] == []


def test_check_quote_match_ignores_whitespace_and_case():
    out = check(result(evidence_quote="Could you send the pricing"), THREAD, COMPANIES)
    assert out["flags"] == []


def test_check_flags_invented_quote():
    out = check(result(evidence_quote="We signed the contract"), THREAD, COMPANIES)
    assert out["flags"] == ["quote_not_found"]
    assert out["confidence"] == "low"


def test_check_rejects_company_not_in_pipeline():
    out = check(result(company="Initech"), THREAD, COMPANIES)
    assert out["company"] is None
    assert "company_not_in_pipeline" in out["flags"]
    assert out["confidence"] == "low"


def test_check_normalizes_company_casing():
    assert check(result(company="ACME  foods"), THREAD, COMPANIES)["company"] == "Acme Foods"


def test_check_non_commercial_category():
    out = check(result(category="vendor", company=None), THREAD, COMPANIES)
    assert out["is_commercial"] is False and out["company"] is None


def test_render_thread_orders_oldest_first():
    records = [
        {"external_id": "b", "occurred_at": "2026-10-21", "participants": {"from": [{"address": "x@a.com", "name": "X"}]},
         "subject": "Re: Hi", "body": "second"},
        {"external_id": "a", "occurred_at": "2026-10-20", "participants": {}, "subject": "Hi", "body": "first"},
    ]
    text = render_thread(records)
    assert text.index("first") < text.index("second")
    assert "X <x@a.com>" in text


def test_system_prompt_lists_companies_and_team():
    prompt = build_system_prompt(
        [{"name": "Acme Foods", "country": "ARG", "stage": "2. In Talks", "owner_code": "NC", "contacts_text": "Jane (COO)"}],
        [{"full_name": "Ramiro Godfrid", "email": "ramiro.godfrid@bianca-ai.com", "owner_code": "RG"}],
        ["bianca-ai.com"],
    )
    assert "Acme Foods" in prompt and "Jane (COO)" in prompt and "Ramiro Godfrid" in prompt


def test_classify_endpoint_requires_token():
    assert TestClient(app).post("/process/classify").status_code == 401


def email(message_id, day, body, *, incoming=False, subject="Agreement"):
    bianca = {"address": "ramiro@bianca-ai.com", "name": "Ramiro"}
    customer = {"address": "jane@acme.com", "name": "Jane"}
    sender, recipient = (customer, bianca) if incoming else (bianca, customer)
    return {
        "external_id": message_id,
        "content_hash": f"hash-{message_id}",
        "occurred_at": f"2026-10-{day:02d}T12:00:00Z",
        "subject": subject,
        "body": body,
        "participants": {"from": [sender], "to": [recipient]},
    }


@pytest.mark.parametrize("nudge", [False, True])
def test_expected_reply_can_cite_current_email_or_an_earlier_open_request(nudge):
    ask = email("ask", 10, "¿Podrías enviarnos el contrato firmado?")
    latest = email("nudge", 12, "¿Pudiste ver lo de abajo?") if nudge else ask
    out_of_office = email("ooo", 13, "Regreso el lunes.", incoming=True, subject="Respuesta automática: Agreement")
    records = [out_of_office, latest, ask] if nudge else [out_of_office, ask]

    assert latest_reply_message(records, INTERNAL) == latest
    decision = ReplyExpectation(state="expected", response="Enviar el contrato firmado",
                                evidence_message_id="ask", evidence_quote="Podrías enviarnos el contrato firmado")
    stored = check_reply_expectation(decision, records, latest)

    assert stored["state"] == "expected"
    assert stored["latest_message_id"] == latest["external_id"]
    assert stored["latest_content_hash"] == latest["content_hash"]
    assert stored["evidence_message_id"] == ask["external_id"]
    assert stored["evidence_content_hash"] == ask["content_hash"]
    assert stored["response"] == "Enviar el contrato firmado"


@pytest.mark.parametrize("state,body", [
    ("closed", "Thanks, we got the signed agreement. All set."),
    ("closed", "Sharing the deck for reference. I'll send pricing on Friday."),
    ("unclear", "I'll leave this here for now."),
])
def test_reply_expectation_keeps_closed_and_unclear_states(state, body):
    latest = email("outbound", 12, body)
    decision = ReplyExpectation(state=state)
    assert check_reply_expectation(decision, [latest], latest)["state"] == state


@pytest.mark.parametrize("overrides", [
    {"evidence_message_id": "missing"},
    {"evidence_message_id": "auto", "evidence_quote": "I'm away"},
    {"evidence_quote": "Can you send pricing?"},
    {"evidence_quote": "Agreement"},  # only in the subject
    {"response": " "},
])
def test_expected_reply_needs_a_specific_response_and_quote_in_real_message_body(overrides):
    ask = email("ask", 10, "Could you send the signed contract?")
    automated = email("auto", 11, "I'm away until next week.", incoming=True, subject="Automatic reply: Agreement")
    decision = ReplyExpectation(**{
        "state": "expected",
        "response": "Send the signed contract",
        "evidence_message_id": "ask",
        "evidence_quote": "Could you send the signed contract?",
        **overrides,
    })
    assert check_reply_expectation(decision, [ask, automated], ask)["state"] == "unclear"


def test_latest_reply_message_requires_real_outbound_to_be_latest():
    ask = email("ask", 10, "Could you send the signed contract?")
    answer = email("answer", 11, "Yes, the signed contract is attached.", incoming=True)
    assert latest_reply_message([ask, answer], INTERNAL) is None
