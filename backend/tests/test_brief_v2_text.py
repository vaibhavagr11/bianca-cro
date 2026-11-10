import json
from datetime import date, datetime, timezone
from html import unescape
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app import brief_api, brief_v2
from app.brief import Meeting
from app.brief_text import TABLE_TEXT_LIMIT, TableText, prepare_table_text, read_table_text
from app.main import app


def test_v2_prepares_every_table_from_full_source_and_renders_the_same_wording():
    today = date(2026, 11, 5)
    stage = "4. Pilot"
    detail = " The supporting details are documented in the original notes shared with Bianca and the customer."
    detail *= 2
    due = brief_v2.TodayRow("Urgent", stage, "Due now", "Bianca owes Inés the SKU list on Friday." + detail,
                            "due Friday", "NC", "https://example.test/urgent", "u", done="insight:u")
    short = "Inés asked for the R&D list. The deadline is provisional."
    exact_limit = ("Bianca sent the revised plan to Inés. The buyer will confirm who owns the approval after "
                   "reviewing the proposal with the finance team and the local pilot leads.")
    assert len(exact_limit) == TABLE_TEXT_LIMIT
    quick = brief_v2.TodayRow("Quick", stage, "Reply needed", short, "received 1d ago", "NC", None)
    boundary = brief_v2.TodayRow("Boundary", stage, "Due now", exact_limit, "due today", "NC", None)
    ndl = brief_v2.TodayRow("Urgent NDL", stage, "Due now", "Nico de León owes Ana the scorecard today." + detail,
                            "due today", "NDL", None, "n", done="insight:n")
    note = brief_v2.Note("Legal is still reviewing the NDA, so Bianca cannot share the files." + detail, verified=True)
    move = brief_v2.Move("We owe", "Bianca owes Pablo the revised NDA on Monday." + detail, "due Monday", None,
                         "m", done="insight:m")
    customer = brief_v2.Move("Customer asked", "The customer asked Bianca to demonstrate exports." + detail,
                             "recorded Nov 5", None, "c", done="insight:c")
    waiting = brief_v2.Waiting("Waiting", stage, "Inés and the finance team" + detail, today, 3, 1, True, True, None,
                               thread_id="w")
    meeting = Meeting(datetime(2026, 11, 5, 14, tzinfo=timezone.utc), "Pilot review with Inés" + detail, "Pilot", stage,
                      ["NC"], link="https://example.test/calendar")
    prep = "Bring the cost model and confirm who approves the pilot." + detail
    change = brief_v2.ChangeRow("Changed", 4, "The buyer tentatively agreed to review pricing next week." + detail,
                                "Notes", None, event=today)
    section = brief_v2.Section(
        code="NC", name="Nico Casaux", short="Nico", today=[due, quick, boundary],
        highlights=[(brief_v2.DealView("Pilot", stage, 4, "NC"), move, note)], also_active=[],
        meetings=[(meeting, prep)],
        followups=[customer], followup_deals=[], more_followups=0, unclear=[], dormant=0, backlog=brief_v2.Backlog(),
        waiting=[waiting],
    )
    sections, ndl_rows, changes = [section], [ndl], [change]
    sources = {
        due.text: "Bianca owes Inés the SKU list on Friday.",
        ndl.text: "Nico de León owes Ana the scorecard today.",
        note.text: "NDA review is still blocking Bianca from sharing files.",
        move.text: "Send Pablo the revised NDA by Monday.",
        customer.text: "The customer asked Bianca to demonstrate exports.",
        brief_v2.waiting_text(waiting): "Inés: no reply to our last email, out-of-office received.",
        meeting.title: "Inés’s pilot review",
        prep: "Bring the cost model and confirm the pilot approver.",
        change.text: "The buyer tentatively agreed to review pricing next week.",
    }
    before = brief_v2.to_payload(sections, ndl_rows, changes)
    actions = brief_v2.completable(sections, ndl_rows)
    client = Mock()
    parse = client.with_options.return_value.messages.parse

    def summarize(**kwargs):
        rows = [json.loads(line) for line in kwargs["messages"][0]["content"].splitlines()]
        assert len(rows) == len(sources)
        assert {row["text"] for row in rows} == sources.keys()
        return SimpleNamespace(stop_reason="end_turn", parsed_output=kwargs["output_format"](summaries=[
            {"id": row["id"], "text": sources[row["text"]]} for row in rows
        ]))

    parse.side_effect = summarize
    fields = list(brief_v2.table_text_fields(sections, ndl_rows, changes))
    display = prepare_table_text(fields, client, "test-model")
    assert all(len(display(field)) <= TABLE_TEXT_LIMIT for field in fields)
    assert {display(field) for field in fields if len(field.text) <= TABLE_TEXT_LIMIT} == {short, exact_limit}
    html = unescape(brief_v2.render(sections, ndl_rows, changes, today, 5, "Sources", False,
                                    action_url=lambda ref: f"https://example.test/done/{ref}", display=display))
    plain = brief_v2.plain_text(sections, today, display=display)

    parse.assert_called_once()
    for summary in sources.values():
        assert summary in html
    for text in (short, exact_limit, sources[due.text], sources[move.text], sources[customer.text],
                 sources[brief_v2.waiting_text(waiting)], sources[meeting.title], sources[prep]):
        assert text in plain
    assert short in html and exact_limit in html
    for rendered in (html, plain):
        assert "supporting details" not in rendered and "…" not in rendered and "..." not in rendered
    assert meeting.link in html
    for ref in actions:
        assert f"https://example.test/done/{ref}" in html
    assert actions["insight:u"] == (due.deal, due.text)
    assert brief_v2.completable(sections, ndl_rows) == actions
    assert brief_v2.to_payload(sections, ndl_rows, changes) == before


def test_v2_build_never_fails_on_over_limit_text(monkeypatch):
    def build(dry_run):
        return {"situation": read_table_text(TableText("Situation", "Confidential evidence " * 10))}

    monkeypatch.setattr(brief_api, "build_brief_v2", build)
    response = TestClient(app).post("/brief/build?version=v2&dry_run=true", headers={"X-Ingest-Token": "test-token"})
    assert response.status_code == 200 and len(response.json()["situation"]) <= TABLE_TEXT_LIMIT
