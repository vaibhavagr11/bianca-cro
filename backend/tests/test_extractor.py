from datetime import date

from fastapi.testclient import TestClient

from app.extractor import ExtractedInsight, Extraction, build_system_prompt, parse_due, validate
from app.main import app

TEXT = "Meeting note dated 2026-10-06\n\nNext steps\nMANDAR MAIL NEXT STEPS\nNo quieren trabajar precios, es   sensible"


def ins(**kw):
    base = dict(type="commitment", text="Send the next-steps email.", quote="MANDAR MAIL NEXT STEPS",
                owner_side="bianca", owner_name="Nicolás de León", due_date=None, confidence="high")
    base.update(kw)
    return ExtractedInsight(**base)


def test_keeps_verbatim_quotes_and_drops_invented_ones():
    kept, dropped = validate(Extraction(insights=[
        ins(),
        ins(type="objection", quote="no quieren trabajar precios, es sensible", owner_side=None, owner_name=None),
        ins(quote="They agreed to sign the contract"),
    ]), TEXT)
    assert [k["type"] for k in kept] == ["commitment", "objection"] and dropped == 1


def test_owner_and_due_date_only_kept_for_actionable_types():
    kept, _ = validate(Extraction(insights=[
        ins(type="need", quote="MANDAR MAIL NEXT STEPS", owner_side="bianca", owner_name="X", due_date="2026-10-08"),
        ins(due_date="2026-10-08"),
    ]), TEXT)
    assert kept[0]["owner_side"] is None and kept[0]["due_date"] is None
    assert kept[1]["owner_side"] == "bianca" and kept[1]["due_date"] == date(2026, 10, 8)


def test_parse_due_rejects_vague_or_bad_dates():
    assert parse_due("2026-10-08") == date(2026, 10, 8)
    assert parse_due("next Thursday") is None
    assert parse_due("2026-13-40") is None
    assert parse_due(None) is None


def test_system_prompt_names_team_and_nico_rule():
    prompt = build_system_prompt([{"full_name": "Nicolás Casaux", "owner_code": "NC", "email": "n@bianca-ai.com"}])
    assert "Nicolás Casaux (NC)" in prompt and '"Nico" alone means Nicolás Casaux' in prompt


def test_extract_endpoint_requires_token():
    assert TestClient(app).post("/process/extract", json={"opportunity_ids": []}).status_code == 401
