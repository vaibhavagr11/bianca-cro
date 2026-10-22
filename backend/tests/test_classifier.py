from fastapi.testclient import TestClient

from app.classifier import ThreadClassification, build_system_prompt, check, render_thread
from app.main import app

COMPANIES = {"acme foods": "Acme Foods", "globex": "Globex"}
THREAD = "From: Jane <jane@acme.com>\nSubject: Pricing\n\nCould you send the   pricing for the pilot by Friday?"


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
