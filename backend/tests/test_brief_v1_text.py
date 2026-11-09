from datetime import date, datetime

import pytest

from app import brief
from app.brief_text import TABLE_TEXT_LIMIT, read_table_text


TODAY = date(2026, 11, 5)
LONG_ACTION = "Bianca will send the complete data plan to the customer on Friday. " * 3
LONG_TITLE = "Review the data plan with José & the onboarding team before the pilot. " * 3
LONG_PREP = "Confirm the delivery date and the customer's data requirements during the call. " * 3
LONG_CHANGE = "The customer confirmed that the data plan is ready for Friday's review. " * 3


def test_v1_renders_prepared_narrative_and_retains_sources():
    row = brief.Row("FEMSA", LONG_ACTION, TODAY, None, "4. Pilot", "Email", "https://example.com/email")
    priority = brief.Row("Delta", "Sign the NDA", None, None, "4. Pilot", "Notes", why=LONG_ACTION)
    meeting = brief.Meeting(datetime(2026, 11, 5, 12), LONG_TITLE, "FEMSA", "4. Pilot", ["RG"],
                            link="https://example.com/meeting")
    changes = [brief.Change("Delta", LONG_CHANGE, "RG", "https://example.com/change")]
    sections = [{"name": "Ramiro", "short": "Ramiro", "today": [row, priority], "week": [],
                 "meetings": [(meeting, LONG_PREP)]}]
    fields = list(brief.table_text_fields(sections, [], changes))
    assert [field.kind for field in fields] == ["What", "What", "Meeting title", "Prep", "What changed"]
    assert fields[1].text == f"{priority.what} — {priority.why}"

    prepared = dict(zip(fields, ("Bianca will send the data plan on Friday.", "Sign the NDA before sending the plan.",
                                 "Data plan review with José & the onboarding team", "Confirm when the data is due.",
                                 "The customer confirmed the plan is ready for Friday's review."), strict=True))
    body = brief.render(sections, [], changes, TODAY, 2, display=prepared.__getitem__)
    plain = brief.plain_text(sections, TODAY, display=prepared.__getitem__)
    assert "<b>Priority:</b> Sign the NDA before sending the plan." in body
    assert "José &amp; the onboarding team" in body and "José & the onboarding team" in plain
    assert "Confirm when the data is due." in body and "Confirm when the data is due." in plain
    assert "The customer confirmed the plan is ready for Friday&#x27;s review." in body
    assert all(f'href="https://example.com/{part}"' in body for part in ("email", "meeting", "change"))
    assert LONG_ACTION not in body and "…" not in body
    assert row.what == LONG_ACTION and meeting.title == LONG_TITLE


def test_v1_collector_skips_rows_the_renderers_will_not_show():
    short = brief.Row("FEMSA", "Confirm Friday's call.", TODAY, None, "4. Pilot", "Email")
    hidden = brief.Row("Delta", LONG_ACTION, TODAY, None, "4. Pilot", "Email")
    rows = [short] * brief.MAX_ROWS + [hidden]
    sections = [{"name": "Ramiro", "short": "Ramiro", "today": rows, "week": [], "meetings": []}]
    assert all(read_table_text(field) == short.what for field in brief.table_text_fields(sections, rows, []))
    assert brief.render(sections, rows, [], TODAY, 2).count("Confirm Friday&#x27;s call.") == 2 * brief.MAX_ROWS


def test_legacy_planner_fallback_keeps_original_detail_for_text_preparation():
    candidate = brief.Candidate("C1", "FEMSA", "Pilot", "RG", "recommended", "Prepare data", LONG_ACTION, 60)
    fallback = brief.fallback_plan([candidate], [])
    assert fallback.actions[0].do_today == LONG_ACTION
    rows = brief.priority_rows(brief.resolve_plan(fallback, [candidate]))
    html = brief.rows_table(rows, TODAY)   # Claude unavailable for both calls: the row is cut, never blocks the brief
    assert LONG_ACTION not in html and "Bianca will send the complete data plan to the customer on Friday." in html
