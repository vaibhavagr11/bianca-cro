from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

from app import brief, brief_review, brief_v2
from app.brief_review import ActionSource
from app.brief_verification import Verdict


TODAY = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)


def test_full_projection_keeps_every_eligible_row_and_the_email_priority_order():
    views = {
        f"Deal {index}": brief_v2.DealView(f"Deal {index}", "4. Pilot", 4, "RG", TODAY)
        for index in range(9)
    }
    views["Opening without evidence"] = brief_v2.DealView("Opening without evidence", "1. Opening", 1, "RG", TODAY)
    views.update({f"Quiet {index}": brief_v2.DealView(f"Quiet {index}", "2. In Talks", 2, "RG",
                                                     TODAY - timedelta(days=30)) for index in range(4)})
    items = []
    for index in range(9):
        deal = f"Deal {index}"
        items += [
            brief_v2.Item(f"promise-{index}", deal, f"Prepare proposal {index}", TODAY + timedelta(days=4),
                          TODAY, None, "RG", bucket="week", kind="owed"),
            brief_v2.Item(f"reminder-{index}", deal, f"Follow up on proposal {index}", None, TODAY,
                          None, "RG", bucket="week", kind="next_step"),
        ]
    items += [brief_v2.Item(f"old-{index}", f"Deal {index}", f"Old promise {index}", TODAY - timedelta(days=30),
                            TODAY - timedelta(days=30), None, "RG", bucket="stale", kind="owed") for index in range(3)]
    meetings = [
        (brief.Meeting(NOW + timedelta(minutes=index), f"Meeting {index}", f"Deal {index}", "4. Pilot", ["RG"]), "")
        for index in range(9)
    ]
    waits = [brief_v2.Waiting(f"Deal {index}", "4. Pilot", "Ana", TODAY - timedelta(days=index + 1), index + 1,
                              1, False, False, None, owner="RG", rank=4)
             for index in range(9)]
    shown_waits, more = brief_v2.pick_waiting(waits, "RG")
    all_waits, full_more = brief_v2.pick_waiting(waits, "RG", full=True)
    email = brief_v2.build_section("RG", "Ramiro", "Ramiro", views, items, list(meetings), TODAY, {},
                                   waiting=shown_waits, more_waiting=more, cadence=None, resurface=1,
                                   resurfaced_recently=frozenset({"Quiet 0"}))
    full = brief_v2.build_section("RG", "Ramiro", "Ramiro", views, items, list(meetings), TODAY, {},
                                  waiting=all_waits, cadence=None, full=True, resurface=1,
                                  resurfaced_recently=frozenset({"Quiet 0"}))
    brief_v2.cap_tables(email)
    brief_v2.cap_tables(full, full=True)

    assert full.highlights[:6] == email.highlights
    assert len(email.highlights) == 6
    assert len(full.highlights) == 9
    assert "Opening without evidence" not in {view.deal for view, _, _ in full.highlights}
    assert len(email.followups) == 3
    assert len(full.followups) == 9
    assert len(email.meetings) == 7
    assert len(full.meetings) == 9
    assert full.waiting[:6] == email.waiting
    assert len(full.waiting) == 9 and full_more == 0
    assert full.also_active == []
    assert full.more_followups == full.more_meetings == full.more_waiting == 0
    assert [view.deal for view in email.resurfaced] == ["Quiet 1"]
    assert [view.deal for view in full.resurfaced] == ["Quiet 1", "Quiet 2", "Quiet 3"]
    assert "A few each day" not in brief_v2.render([full], [], [], TODAY, len(views), "Sources", False, full=True)
    assert len(email.backlog.exceptions) == 1
    assert len(full.backlog.exceptions) == 3

    changes = [brief_v2.ChangeRow(f"Deal {index}", index % 6, f"Change {index}", "Email", None,
                                  kind="new" if index % 2 else "late", from_customer=bool(index % 3))
               for index in range(12)]
    all_changes = brief_v2.select_changes(changes, full=True)
    assert all_changes[:7] == brief_v2.select_changes(changes)
    assert len(all_changes) == 12


def test_full_section_lists_every_status_name_and_sheet_date():
    section = brief_v2.build_section("RG", "Ramiro", "Ramiro", {}, [], [], TODAY, {}, full=True)
    section.unclear = [brief_v2.DealView(f"Confirm {index}", "3. Proposal", 3, "RG", TODAY - timedelta(days=22))
                       for index in range(5)]
    section.sheet_stale = [(f"Stale {index}", TODAY - timedelta(days=30)) for index in range(5)]
    section.sheet_later = [(f"Future {index}", TODAY + timedelta(days=index + 1)) for index in range(5)]

    email = brief_v2.render([section], [], [], TODAY, 15, "Sources", False)
    full = brief_v2.render([section], [], [], TODAY, 15, "Sources", False, full=True)
    assert "Confirm 4" not in email and "Confirm 4" in full
    assert "Stale 4" not in email and "Stale 4" in full
    assert "Future 4" not in email and "Future 4" in full
    assert "Future 4" in brief_v2.plain_text([section], TODAY, full=True)
    assert "Deal 4" in brief_v2.undated_note(5, [f"Deal {index}" for index in range(5)], full=True)


def test_one_verification_call_checks_both_projections_and_keeps_their_notes_independent():
    views = {
        f"Deal {index}": brief_v2.DealView(
            f"Deal {index}", "4. Pilot", 4, "RG", TODAY,
            recommendation=brief_v2.Note(f"Prepare terms {index}", verified=True, ref=str(index)),
        )
        for index in range(8)
    }
    email = brief_v2.build_section("RG", "Ramiro", "Ramiro", views, [], [], TODAY, {})
    full = brief_v2.build_section("RG", "Ramiro", "Ramiro", views, [], [], TODAY, {}, full=True)
    all_ndl = [brief_v2.TodayRow("Deal 0", "4. Pilot", "Due now", f"NDL action {index}", "today", "NDL", None,
                                 done=f"insight:ndl-{index}") for index in range(4)]
    email_ndl, full_ndl = deepcopy(all_ndl[:3]), deepcopy(all_ndl)
    sources = {f"rec:{index}": ActionSource("recommendation", f"record-{index}", NOW) for index in range(8)}
    sources.update({f"insight:ndl-{index}": ActionSource("commitment", f"ndl-record-{index}", NOW)
                    for index in range(4)})
    calls = []

    def gate(actions):
        calls.append(actions)
        states = {"record-6": "handled", "record-7": "unclear", "ndl-record-3": "waiting"}
        return {action.key: Verdict(states.get(action.origin_record_id, "still_owed"), "fixture")
                for action in actions}

    full_ndl_notes = brief.VerificationNotes()
    notes = brief_review.verify_v2([email], email_ndl, {deal: deal for deal in views}, sources, gate,
                                   full_sections=[full], full_ndl=full_ndl, full_ndl_notes=full_ndl_notes)
    assert len(calls) == 1 and len(calls[0]) == 12
    assert len(email.highlights) == len(full.highlights) == 6
    assert email.verification.unclear == []
    assert full.verification.unclear == [("Deal 7", "Prepare terms 7")]
    assert notes.waiting == []
    assert full_ndl_notes.waiting == [("Deal 0", "NDL action 3")]
    assert len(email_ndl) == len(full_ndl) == 3


def test_email_link_precedes_the_brief_and_escapes_the_url():
    url = "https://bianca.example/review/app?tab=daily-brief&brief=123"
    html = brief_v2.render([], [], [], TODAY, 0, "Sources", False, hub_url=url)
    assert html.index("View full Daily Brief in War Room") < html.index("Pipeline brief")
    assert 'href="https://bianca.example/review/app?tab=daily-brief&amp;brief=123"' in html
    assert brief_v2.plain_text([], TODAY, hub_url=url).startswith(f"View full Daily Brief in War Room → {url}")
