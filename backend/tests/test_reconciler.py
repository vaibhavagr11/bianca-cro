from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app.brief import render
from app.main import app
from app.reconciler import ItemVerdict, Verdicts, render as render_check, validate

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
ITEMS = {"T1": {"id": "i1", "record_id": "own1", "occurred_at": T0, "own_text": "Reunión de entrega el 14/10",
                "text": "Send slides", "quote": "armar 2 slides", "due_date": "2026-10-02", "source": "notes"}}
RECORDS = {"R1": {"id": "r1", "occurred_at": datetime(2026, 10, 5, tzinfo=timezone.utc), "subject": "Slides",
                  "body": "Adjunto las 2 slides del mapa."},
           "R0": {"id": "r0", "occurred_at": datetime(2026, 9, 30, tzinfo=timezone.utc), "subject": "x",
                  "body": "Adjunto las 2 slides del mapa."}}


def v(**kw):
    base = dict(item="T1", state="done", record="R1", quote="Adjunto las 2 slides", reason="Sent.")
    base.update(kw)
    return Verdicts(verdicts=[ItemVerdict(**base)])


def test_accepts_verbatim_quote_from_later_record():
    [a] = validate(v(), ITEMS, RECORDS)
    assert a == {"insight_id": "i1", "state": "done", "record_id": "r1", "quote": "Adjunto las 2 slides",
                 "reason": "Sent."}


def test_rejects_invented_quote_earlier_record_open_and_unknown_items():
    assert validate(v(quote="We sent the slides"), ITEMS, RECORDS) == []
    assert validate(v(record="R0"), ITEMS, RECORDS) == []          # evidence must come after the item
    assert validate(v(state="still_open"), ITEMS, RECORDS) == []
    assert validate(v(item="T9"), ITEMS, RECORDS) == []
    assert validate(v(record="R7"), ITEMS, RECORDS) == []


def test_event_passed_quotes_the_items_own_source():
    [a] = validate(v(state="event_passed", record="S", quote="Reunión de entrega el 14/10"), ITEMS, RECORDS)
    assert a["record_id"] == "own1" and a["state"] == "event_passed"


def test_render_lists_items_and_later_records():
    body = render_check("2026-10-21", "Campari", [{**ITEMS["T1"], "ref": "T1"}], [{**RECORDS["R1"], "ref": "R1",
                                                                                      "source": "gmail"}])
    assert "T1 | recorded 2026-10-01 | due 2026-10-02" in body and "R1 | 2026-10-05" in body
    assert "S (own source of T1)" in body


def test_brief_shows_hidden_count():
    sections = [{"name": "Nico Casaux", "short": "Nico", "today": [], "week": [], "meetings": [], "hidden": 3}]
    assert "3 items hidden: later emails/notes suggest they were done." in render(sections, [], [], date(2026, 10, 21), 28)


def test_reconcile_requires_token():
    assert TestClient(app).post("/process/reconcile").status_code == 401
