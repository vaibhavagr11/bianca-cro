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


def test_validation_keeps_distinct_actions_from_one_message_and_preserves_timing_certainty():
    quote = "Mañana envío el precio y el contrato. ¿Pueden arreglar el tablero la semana que viene?"
    records = {
        "M1": {"id": "first", "body": quote, "occurred_at": date(2026, 10, 6)},
        "M2": {"id": "second", "body": "Gracias", "occurred_at": date(2026, 10, 7)},
    }
    sales = {"quote": quote, "source_ref": "M1", "action_scope": "deal_progression",
             "due_date": "2026-10-07", "deadline_quote": "Mañana"}
    candidates = Extraction(insights=[
        ins(text="Send the pricing tomorrow.", **sales),
        ins(text="Send the contract tomorrow.", **sales),
        ins(type="next_step", text="Customer asked for a dashboard fix next week.", quote=quote, source_ref="M1",
            action_scope="delivery", due_date="2026-10-16", deadline_quote="la semana que viene"),
        ins(text="Send the pricing tomorrow.", **{**sales, "source_ref": "M2"}),
    ])

    kept, dropped = validate(candidates, f"{quote}\nGracias", records)

    assert dropped == 1
    assert [item["origin_record_id"] for item in kept] == ["first", "first", "first"]
    assert [item["text"] for item in kept[:2]] == ["Send the pricing tomorrow.", "Send the contract tomorrow."]
    assert [item["action_scope"] for item in kept] == ["deal_progression", "deal_progression", "delivery"]
    assert [item["due_date"] for item in kept] == [date(2026, 10, 7), date(2026, 10, 7), None]
    assert kept[2]["type"] == "next_step" and "next week" in kept[2]["text"]

    records["M1"]["meta"] = {"date_inferred": True}
    estimated, _ = validate(candidates, f"{quote}\nGracias", records)
    assert all(item["due_date"] is None for item in estimated)


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


def test_insight_key_ignores_whitespace_and_case():
    from app.extractor import insight_key
    assert insight_key("commitment", "Te mando  las\nslides") == insight_key("commitment", "te mando las slides")
    assert insight_key("commitment", "x") != insight_key("next_step", "x")
