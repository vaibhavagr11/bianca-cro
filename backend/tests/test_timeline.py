from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.timeline import build_timeline, summarize

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
INTERNAL = {"bianca-ai.com"}


def rec(id, source, when, subject, thread=None, meta=None, participants=None, reason="why"):
    return {"id": id, "source": source, "external_id": id, "thread_id": thread or id,
            "occurred_at": datetime.fromisoformat(when).replace(tzinfo=timezone.utc), "subject": subject,
            "participants": participants or {}, "classification": {"reason": reason, "category": "customer"},
            "meta": meta or {}}


RECORDS = [
    rec("m1", "gmail", "2026-10-01T10:00", "Pricing", thread="t1",
        participants={"from": [{"address": "jane@acme.com", "name": "Jane"}],
                      "to": [{"address": "ramiro.godfrid@bianca-ai.com"}]}),
    rec("m2", "gmail", "2026-10-03T10:00", "Re: Pricing", thread="t1", reason="latest"),
    rec("n1", "notes", "2026-10-05T00:00", "Acme <> Bianca", meta={"doc_id": "DOC", "date_inferred": True}),
    rec("c1", "calendar", "2026-10-25T15:00", "Acme weekly", meta={"status": "confirmed"}),
    rec("c2", "calendar", "2026-10-23T15:00", "Acme extra", meta={"status": "cancelled"}),
]
CHANGES = [{"field": "stage", "old_value": "2. In Talks", "new_value": "3. Proposal",
            "detected_at": datetime(2026, 10, 10, tzinfo=timezone.utc)}]


def test_timeline_is_newest_first_and_groups_email_threads():
    items = build_timeline(RECORDS, CHANGES, INTERNAL, NOW)
    assert [i["source"] for i in items] == ["calendar", "calendar", "pipeline", "notes", "gmail"]
    thread = items[-1]
    assert thread["messages"] == 2 and thread["title"] == "Re: Pricing" and thread["summary"] == "latest"
    assert thread["people"] == ["Jane"] and thread["link"].endswith("#all/t1")


def test_notes_link_and_estimated_date_and_pipeline_change_title():
    items = build_timeline(RECORDS, CHANGES, INTERNAL, NOW)
    note = next(i for i in items if i["source"] == "notes")
    assert note["link"] == "https://docs.google.com/document/d/DOC" and note["date_estimated"]
    change = next(i for i in items if i["source"] == "pipeline")
    assert change["title"] == "stage: 2. In Talks → 3. Proposal"


def test_summary_skips_cancelled_meetings_for_next_meeting():
    items = build_timeline(RECORDS, CHANGES, INTERNAL, NOW)
    s = summarize(items, NOW)
    assert s["next_meeting"]["title"] == "Acme weekly"
    assert s["last_activity_at"].startswith("2026-10-05")
    assert s["counts"] == {"calendar": 2, "pipeline": 1, "notes": 1, "gmail": 1}


def test_endpoints_require_token():
    client = TestClient(app)
    assert client.get("/opportunities").status_code == 401
    assert client.get("/opportunities/x/timeline").status_code == 401
