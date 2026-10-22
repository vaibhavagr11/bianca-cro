from app.classifier import render_event
from app.gmail import REDACTED
from app.google_calendar import clean, clean_description, process, skip_reason

INTERNAL = {"bianca-ai.com"}
PROJECT = {"kellogg.northwestern.edu"}
ME = {"email": "ramiro.godfrid@bianca-ai.com", "self": True, "responseStatus": "accepted"}


def event(id="e1", attendees=None, summary="Acme <> Bianca", **extra):
    base = {"id": id, "status": "confirmed", "summary": summary,
            "start": {"dateTime": "2026-10-22T15:00:00Z"}, "end": {"dateTime": "2026-10-22T16:00:00Z"},
            "attendees": attendees if attendees is not None else [ME, {"email": "jane@acme.com", "displayName": "Jane"}],
            "organizer": {"email": "ramiro.godfrid@bianca-ai.com", "self": True}}
    base.update(extra)
    return base


def test_customer_meeting_is_kept():
    assert skip_reason(event(), INTERNAL, PROJECT) is None


def test_personal_block_is_skipped():
    assert skip_reason(event(attendees=[]), INTERNAL, PROJECT) == "no_other_attendees"


def test_room_resource_does_not_count_as_attendee():
    room = {"email": "room@resource.calendar.google.com", "resource": True}
    assert skip_reason(event(attendees=[ME, room]), INTERNAL, PROJECT) == "no_other_attendees"


def test_internal_only_is_skipped():
    team = [ME, {"email": "nicolas.casaux@bianca-ai.com"}]
    assert skip_reason(event(attendees=team), INTERNAL, PROJECT) == "internal_only"


def test_project_team_is_skipped():
    team = [ME, {"email": "eren.baysal@kellogg.northwestern.edu"}]
    assert skip_reason(event(attendees=team), INTERNAL, PROJECT) == "project_team"


def test_focus_time_is_skipped():
    assert skip_reason(event(eventType="focusTime"), INTERNAL, PROJECT) == "not_a_meeting"


def test_organizer_counts_as_attendee_when_not_listed():
    e = event(attendees=[ME], organizer={"email": "gus@gmail.com", "displayName": "Gus"})
    assert skip_reason(e, INTERNAL, PROJECT) is None


def test_description_drops_meet_boilerplate_and_redacts():
    text = "Agenda: pilot pricing<br>PIN: 307268329\n-::~:~::~:~::~ Join with Google Meet https://meet.google.com/x"
    out = clean_description(text)
    assert "Agenda: pilot pricing" in out and "meet.google.com" not in out and "307268329" not in out
    assert REDACTED in out


def test_clean_event_fields_and_hash_stability():
    a, b = clean(event()), clean(event())
    assert a.starts_at == "2026-10-22T15:00:00Z"
    assert {p["address"] for p in a.participants["attendees"]} == {"ramiro.godfrid@bianca-ai.com", "jane@acme.com"}
    assert a.meta["status"] == "confirmed" and not a.meta["recurring"]
    assert a.content_hash == b.content_hash


def test_process_splits_kept_cancelled_and_skipped():
    batch = [
        event(id="a"),
        event(id="a"),
        event(id="b", attendees=[]),
        {"id": "c", "status": "cancelled", "recurringEventId": "r1"},
        event(id="d", eventType="outOfOffice"),
    ]
    kept, cancelled, skipped = process(batch, INTERNAL, PROJECT)
    assert [e.external_id for e in kept] == ["a"]
    assert cancelled == ["c"]
    assert skipped == {"duplicate": 1, "no_other_attendees": 1, "not_a_meeting": 1}


def test_render_event_for_classifier():
    e = clean(event())
    text = render_event({"subject": e.subject, "occurred_at": e.starts_at, "participants": e.participants,
                         "body": "Agenda: pricing", "meta": e.meta})
    assert "Acme <> Bianca" in text and "jane@acme.com" in text and "Agenda: pricing" in text
