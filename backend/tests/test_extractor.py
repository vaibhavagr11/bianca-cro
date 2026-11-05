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


def test_insight_key_ignores_whitespace_and_case():
    from app.extractor import insight_key
    assert insight_key("commitment", "Te mando  las\nslides") == insight_key("commitment", "te mando las slides")
    assert insight_key("commitment", "x") != insight_key("next_step", "x")


def test_exact_source_ref_controls_anchor_when_the_quote_is_repeated():
    refs = {
        "M1": {"id": "first", "occurred_at": "2026-10-06", "subject": None, "body": "Mandar mail next steps"},
        "M2": {"id": "last", "occurred_at": "2026-10-07", "subject": None, "body": "Mandar mail next steps"},
    }
    kept, dropped = validate(Extraction(insights=[ins(source_ref="M1"), ins(source_ref="M9"), ins()]), TEXT, refs)
    assert dropped == 1
    assert kept[0]["origin_record_id"] == "first"
    assert kept[0]["origin_occurred_at"] == "2026-10-06"
    assert kept[1]["origin_record_id"] is None
    assert kept[1]["origin_occurred_at"] is None


def test_unique_legacy_quote_keeps_its_origin_and_inferred_dates_are_unverified():
    refs = {
        "M1": {"id": "first", "occurred_at": "2026-10-06", "subject": None, "body": "Mandar mail next steps",
               "meta": {"date_inferred": True}},
        "M2": {"id": "last", "occurred_at": "2026-10-07", "subject": None, "body": "The requested email was sent."},
    }
    kept, dropped = validate(Extraction(insights=[ins(), ins(source_ref="M2")]), TEXT, refs)
    assert dropped == 1
    assert kept[0]["origin_record_id"] == "first"
    assert kept[0]["origin_occurred_at"] is None


def test_reextraction_updates_existing_action_without_resetting_identity_or_review():
    from app.process import _write_extracted_insights

    unit = {"source": "gmail", "thread_id": "thread", "company_id": "company"}
    deal = {"id": "deal"}
    records = [
        {"id": "first", "occurred_at": "2026-10-06", "body": "Mandar mail next steps mañana"},
        {"id": "later", "occurred_at": "2026-10-07", "body": "I sent the next steps."},
    ]
    old = {"id": "stable", "origin_record_id": "first", "type": "commitment", "status": "done",
           "text": "Send the next-steps email.", "quote": "Mandar mail next steps"}
    kept, _ = validate(Extraction(insights=[ins(source_ref="M1", quote="Mandar mail next steps mañana")]),
                       records[0]["body"], {"M1": records[0], "M2": records[1]})

    class Conn:
        def __init__(self):
            self.writes = []

        def execute(self, sql, params):
            if sql.startswith("select"):
                return self
            self.writes.append((sql, params))
            return self

        def fetchall(self):
            return [old]

    conn = Conn()
    _write_extracted_insights(conn, "workspace", unit, deal, records, kept)
    [(sql, params)] = conn.writes
    assert sql.startswith("update insights")
    assert "created_at" not in sql and "status" not in sql and "completion" not in sql
    assert params[-1] == "stable"
    assert params[-3] == "2026-10-06"

    # The updated thread's extractor may omit the action because the later reply fulfills it.
    old["status"] = "open"
    conn.writes.clear()
    _write_extracted_insights(conn, "workspace", unit, deal, records, [])
    [(sql, params)] = conn.writes
    assert sql.startswith("update insights")
    assert params[-1] == "stable"
