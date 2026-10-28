"""Regression tests for brief v2. Each mirrors a failure seen in the Oct 28 briefs (v1, then the first v2 draft)."""

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.brief import Meeting
from app.brief_v2 import (
    ChangeRow, DealView, Item, Note, Unanswered, bucket, build_section, classify_event, deal_status, first_sentence, has_request,
    is_new_insight, item_kind, norm, primary_move, render, reply_label, resolve_owner, select_changes, subject,
    to_payload,
)
from app.main import app

TODAY = date(2026, 10, 28)
TEAM = {norm("Ramiro Godfrid"): "RG", norm("Nicolás Casaux"): "NC", norm("Nicolás de León"): "NDL"}
FOUNDERS = {"RG", "NC"}
DISPLAY = {"RG": "Ramiro", "NC": "Nico", "NDL": "Nico DL"}
STAGES = {0: "0. Paused", 1: "1. Opening", 2: "2. In Talks", 3: "3. Proposal", 4: "4. Pilot", 5: "5. Working client"}


def item(deal, text="x", due=None, recorded=TODAY - timedelta(days=5), owner_name=None, deal_owner="NC", id="i",
         kind="owed"):
    route, basis, delegated = resolve_owner(owner_name, deal_owner, TEAM, FOUNDERS, DISPLAY)
    return Item(id, deal, text, due, recorded, owner_name, route, basis, delegated, "Notes · Oct 5", f"https://src/{id}",
                bucket(due, recorded, TODAY, 3, 14), kind)


def view(deal, rank, owner="NC", last=TODAY - timedelta(days=3), **kw):
    return DealView(deal, STAGES[rank], rank, owner, last, **kw)


def section(code, views, items):
    return build_section(code, code, DISPLAY[code], {v.deal: v for v in views}, items, [], TODAY, DISPLAY)


# ---- commitment classification (point 2): history, proposals and requests are not "We owe"

def test_item_kind_separates_promises_requests_proposals_and_notes():
    assert item_kind("commitment", None, "notes", "Nosotros mandamos el deck") == "owed"
    assert item_kind("next_step", TODAY, "notes", "revisar el jueves") == "owed"               # has a deadline
    # Campari: "les compartimos una breve presentacion" written by Bianca -> done/proposed, not owed
    assert item_kind("next_step", None, "bianca", "les compartimos una breve presentacion") == "proposed"
    # Arca Peru: "Nos gustaría reconectar" in Bianca's own email -> proposed, the next move is theirs
    assert item_kind("next_step", None, "bianca", "Nos gustaría reconectar y mostrarte") == "proposed"
    # Koala: "Se sumó el David, pidió un walkthrough" in the notes -> customer request
    assert item_kind("next_step", None, "notes", "Se sumó el David, pidió un walkthrough de la plataforma") == "customer_request"
    assert item_kind("next_step", None, "customer", "Can you share the file") == "customer_request"
    assert item_kind("next_step", None, "notes", "Armar mapa de zonas") == "next_step"


def test_proposal_is_never_shown_as_we_owe_and_becomes_a_follow_up_without_reply():
    p = item("Arca Peru", "Bianca proposed reconnecting with Renzo", recorded=TODAY - timedelta(days=21),
             deal_owner="RG", kind="proposed")
    v = view("Arca Peru", 2, owner="RG", last=TODAY - timedelta(days=21), items=[p])
    m = primary_move(v, TODAY, set())
    assert m.tag == "Follow up" and m.text.startswith("No reply since we proposed")


def test_completed_or_proposed_history_does_not_fill_the_deal_row():
    hist = item("Campari", "Bianca shared a presentation", deal_owner="RG", kind="proposed", id="h")
    rec = Note("Book the On Premise demo and handover", verified=True, link="https://ev")
    s = section("RG", [view("Campari", 4, owner="RG", recommendation=rec)], [hist])
    (_, move, _), = s.highlights
    assert move.tag == "AI recommendation" and "presentation" not in move.text


# ---- urgency (point 3)

def test_unanswered_email_needs_a_request_and_recency_to_be_today():
    assert reply_label(Unanswered("Next steps?", 1, None, True)) == "reply_needed"
    assert reply_label(Unanswered("Presentación", 9, None, False)) == "follow_up"       # Campari FYI email
    assert reply_label(Unanswered("Contrato CCO", 13, None, True)) == "follow_up"       # Martin Albretch, 13d
    assert has_request("¿Podrían confirmar?") and has_request("please send")
    assert not has_request("Les comparto la presentación")


def test_reply_needed_in_today_but_fyi_email_is_not():
    s = section("RG", [view("Pernod", 3, owner="RG", unanswered=Unanswered("Próximos pasos?", 1, "l", True)),
                       view("Campari", 4, owner="RG", unanswered=Unanswered("Les comparto", 9, "l2", False))], [])
    assert [(r.deal, r.tag) for r in s.today] == [("Pernod", "Reply needed")]


def test_due_now_only_for_promises_due_within_three_days_or_tomorrow():
    assert bucket(TODAY + timedelta(days=1), TODAY, TODAY, 3, 14) == "today"
    assert bucket(TODAY - timedelta(days=3), TODAY, TODAY, 3, 14) == "today"
    assert bucket(TODAY - timedelta(days=4), TODAY, TODAY, 3, 14) == "overdue"
    assert bucket(TODAY - timedelta(days=27), TODAY, TODAY, 3, 14) == "stale"          # Campari Oct 1
    s = section("NC", [view("Smearcase", 4)],
                [item("Smearcase", "Send fix", due=TODAY, id="a"),
                 item("Smearcase", "Review data", kind="next_step", id="b")])        # notes next step: not "Due now"
    assert [r.text for r in s.today] == ["Send fix"]


# ---- stale opportunities and coverage (point 4)

def test_old_last_contact_means_confirm_status_not_active():
    assert deal_status(view("Bakr", 2, last=TODAY - timedelta(days=5)), TODAY) == "active"
    assert deal_status(view("Bakr", 2, last=TODAY - timedelta(days=22)), TODAY) == "unclear"
    assert deal_status(view("Bakr", 2, last=TODAY - timedelta(days=60)), TODAY) == "dormant"
    assert deal_status(view("Bakr", 2, last=None), TODAY) == "untracked"     # never seen: not listed
    s = section("NC", [view("Bakr", 2, last=TODAY - timedelta(days=22)), view("Zabs", 2, last=TODAY - timedelta(days=70))], [])
    assert s.highlights == [] and [v.deal for v in s.unclear] == ["Bakr"] and s.dormant == 1
    html = render([s], [], [], TODAY, 2, "c", True)
    assert "Confirm status" in html and "Nico&#x27;s inbox isn&#x27;t connected" in html


# ---- density and stale noise (points 1 and 5)

def test_one_move_per_deal_capped_and_no_commitment_lists():
    views = [view(f"Deal {n}", 2) for n in range(8)]
    its = [item(f"Deal {n}", f"promise {n}-{k}", due=TODAY + timedelta(days=4), id=f"{n}-{k}")
           for n in range(8) for k in range(3)]
    s = section("NC", views, its)
    assert len(s.highlights) == 4 and len(s.also_active) == 4
    assert all(m.tag == "We owe" for _, m, _ in s.highlights)
    html = render([s], [], [], TODAY, 8, "c", False)
    assert html.count("promise 0-") == 1                                   # one move shown, not the whole list


def test_backlog_is_summarized_with_rare_exceptions_and_all_items_preserved():
    views = [view("FEMSA Arg", 5), view("Pernod", 3)]
    old = [item("FEMSA Arg", f"old {n}", due=TODAY - timedelta(days=20 + n), id=f"o{n}") for n in range(12)]
    promised = item("Pernod", "Send pilot proposal", due=TODAY - timedelta(days=18), id="p")
    s = section("NC", views, old + [promised])
    assert s.backlog.count >= 12 and s.backlog.top[0] == "FEMSA Arg"
    assert [i.id for i in s.backlog.exceptions] == ["p"]                   # only a promise to an active-stage deal
    html = render([s], [], [], TODAY, 2, "c", False)
    assert html.count("old ") <= 2                                          # no long list in the email
    assert len(to_payload([s], [], [])["sections"][0]["all_items"]) == 13   # nothing lost


# ---- ownership (kept from the first v2 draft)

def test_named_founder_beats_deal_owner_and_non_founders_are_delegated():
    assert resolve_owner("NICOLAS CASAUX", "RG", TEAM, FOUNDERS, DISPLAY) == ("NC", "named", None)
    assert resolve_owner("Ronald Roland", "NC", TEAM, FOUNDERS, DISPLAY) == ("NC", "delegated", "Ronald Roland")
    assert resolve_owner("Nicolás de León", "RG", TEAM, FOUNDERS, DISPLAY) == ("RG", "delegated", "Nico DL")
    assert resolve_owner(None, "RG", TEAM, FOUNDERS, DISPLAY) == ("RG", "deal_owner", None)


def test_delegated_never_today_and_other_founders_deal_not_duplicated():
    v = [view("Alicorp", 2), view("Pernod", 3, owner="RG", recommendation=Note("Confirm NDA", verified=True))]
    s = section("NC", v, [item("Alicorp", "Ron meets Stibo", due=TODAY, owner_name="Ronald Roland", id="r"),
                          item("Pernod", "Send deck", due=TODAY + timedelta(days=5), owner_name="Nicolás Casaux",
                               deal_owner="RG", id="d")])
    assert s.today == [] and s.backlog.delegated == 1
    assert all(v.deal != "Pernod" for v, _, _ in s.highlights)              # Ramiro's deal stays in Ramiro's section
    assert {i["id"] for i in to_payload([s], [], [])["sections"][0]["all_items"]} == {"r", "d"}


# ---- recommendations and evidence

def test_unverified_recommendation_is_labelled_and_has_no_evidence_link():
    v = view("FEMSA MX", 2, recommendation=Note("Re-engage the buyer", verified=False))
    m = primary_move(v, TODAY, set())
    assert m.tag == "AI recommendation" and "not verified" in m.text and m.link is None


def test_first_sentence_keeps_moves_short():
    assert first_sentence("Call Ale. Ask for intros.") == "Call Ale."
    assert len(first_sentence("word " * 80)) <= 96
    assert first_sentence("Sharing data depends on the NDA. It is now at signature.") == "Sharing data depends on the NDA."


# ---- what changed (point 6)

def test_new_insight_requires_its_quote_in_a_newly_stored_record():
    assert is_new_insight("Necesitamos  la base", "hola necesitamos la base de cerveza")
    assert not is_new_insight("old quote", "only new text")


def test_changes_reserve_room_for_customers():
    rows = [ChangeRow(f"P{n}", 3, "x", "Email", None) for n in range(9)] + [ChangeRow("Koala", 5, "Buying signal", "Notes", None)]
    picked = select_changes(rows)
    assert len(picked) == 4 and "Koala" in [c.deal for c in picked]


def test_subject_and_meetings():
    m = Meeting(datetime(2026, 10, 28, 15, tzinfo=timezone.utc), "Gus <> Ramiro", "Campari", "4. Pilot", ["RG"])
    s = build_section("RG", "Ramiro", "Ramiro", {}, [], [(m, "Lock handover date")], TODAY, DISPLAY)
    assert subject([s], TODAY) == "Bianca pipeline · Wed, Oct 28 · Ramiro 0 today, 0 deals"
    assert "Lock handover date" in render([s], [], [], TODAY, 0, "c", False)


def test_v2_endpoint_requires_token():
    assert TestClient(app).post("/brief/build?version=v2&dry_run=true").status_code == 401


def test_quiet_deal_is_not_highlighted_even_with_a_recommendation():  # Maxine's (71d), Cumin Club (38d)
    rec = Note("Ask for contract status", verified=True)
    s = section("NC", [view("Maxine", 3, last=TODAY - timedelta(days=71), recommendation=rec),
                       view("Cumin", 4, last=TODAY - timedelta(days=38), recommendation=rec)], [])
    assert s.highlights == [] and [v.deal for v in s.unclear] == ["Cumin"] and s.dormant == 1


def test_untracked_pipeline_rows_are_not_listed_for_confirmation():
    s = section("RG", [view(f"Row {n}", 1, owner="RG", last=None) for n in range(12)], [])
    assert s.unclear == [] and s.dormant == 0


def test_sentence_split_ignores_numbered_stages():
    assert first_sentence("Early-stage deal (stage '2. In Talks') with no project yet. Next.") == \
        "Early-stage deal (stage '2. In Talks') with no project yet."


def test_planner_cannot_tag_or_select_deals():  # selection and tags are deterministic; no "Recommended today"
    v = view("Campari", 4, owner="RG", recommendation=Note("Book demo", verified=True))
    owed = item("Campari", "Add On Premise data", due=TODAY + timedelta(days=2), deal_owner="RG", id="c")
    html = render([section("RG", [v], [owed])], [], [], TODAY, 1, "c", False)
    assert "We owe" in html and "Recommended today" not in html


# ---- change detection by event time (round 3)

CUTOFF = datetime(2026, 10, 22, 14, 36, tzinfo=timezone.utc)   # the Oct 22 brief
NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
STORED_TODAY = datetime(2026, 10, 28, 16, 36, tzinfo=timezone.utc)


def test_femsa_oct14_meeting_is_backfill_not_new():
    assert classify_event(datetime(2026, 10, 14, 3, tzinfo=timezone.utc), STORED_TODAY, CUTOFF, NOW, 7) == "backfill"


def test_koala_oct21_meeting_is_not_a_new_development():
    kind = classify_event(datetime(2026, 10, 21, 3, tzinfo=timezone.utc), STORED_TODAY, CUTOFF, NOW, 7)
    assert kind != "new" and kind == "late"
    assert classify_event(datetime(2026, 10, 21, 3, tzinfo=timezone.utc), STORED_TODAY, CUTOFF, NOW, 0) == "backfill"


def test_campari_oct21_email_is_recorded_late_within_the_window():
    assert classify_event(datetime(2026, 10, 21, 23, 49, tzinfo=timezone.utc),
                          datetime(2026, 10, 23, 10, 17, tzinfo=timezone.utc), CUTOFF, NOW, 7) == "late"


def test_event_time_rules_new_seen_scheduled_undated():
    assert classify_event(datetime(2026, 10, 23, 3, tzinfo=timezone.utc), STORED_TODAY, CUTOFF, NOW, 7) == "new"  # Smearcase
    assert classify_event(datetime(2026, 10, 23, 3, tzinfo=timezone.utc), CUTOFF, CUTOFF, NOW, 7) == "seen"
    assert classify_event(datetime(2026, 11, 3, 14, tzinfo=timezone.utc), STORED_TODAY, CUTOFF, NOW, 7) == "scheduled"
    assert classify_event(None, STORED_TODAY, CUTOFF, NOW, 7) == "undated"
    assert classify_event(datetime(2026, 10, 25, tzinfo=timezone.utc), STORED_TODAY, CUTOFF, NOW, 7, reliable=False) == "undated"


def test_late_changes_are_labelled_with_their_event_date():
    c = ChangeRow("Campari", 4, "New blocker: x", "Email · Oct 21", None, "late", date(2026, 10, 21))
    html = render([], [], [c], TODAY, 0, "c", False, "Not shown as new: 3 older development(s)")
    assert "from Oct 21, recorded late" in html and "Not shown as new" in html


def test_new_changes_rank_before_late_ones():
    rows = [ChangeRow("A", 3, "x", "s", None, "late"), ChangeRow("B", 3, "y", "s", None, "new")]
    assert [c.deal for c in select_changes(rows)] == ["B", "A"]


# ---- deterministic deal selection (round 3)

def ramiro_oct28():
    """Ramiro's eligible deals as of Oct 28, reduced to the signals the selection uses."""
    vb, rec = Note("blocker", verified=True), Note("next move", verified=True)
    d = lambda n: TODAY - timedelta(days=n)
    views = [
        view("Pernod Ricard", 3, owner="RG", last=d(2), blocker=vb, recommendation=rec,
             unanswered=Unanswered("Próximos pasos?", 1, "l", True)),
        view("Campari", 4, owner="RG", last=d(6), blocker=vb, recommendation=rec, developments=1),
        view("Farmater", 4, owner="RG", last=d(1), blocker=vb, recommendation=rec, developments=1),
        view("Andina Chile", 2, owner="RG", last=d(9), blocker=vb, recommendation=rec),
        view("Chifles", 2, owner="RG", last=d(9), recommendation=rec),
        view("Arca Peru", 2, owner="RG", last=d(9), blocker=vb),
        view("Martin Albretch", 1, owner="RG", last=d(13), recommendation=rec),
    ]
    items = [item("Campari", "Add On Premise data", due=TODAY + timedelta(days=2), deal_owner="RG", id="c1"),
             item("Farmater", "New zone for Luis", due=TODAY - timedelta(days=9), deal_owner="RG", id="f1"),
             item("Arca Peru", "Proposed reconnecting", recorded=d(21), deal_owner="RG", kind="proposed", id="a1")]
    return views, items


def test_in_talks_deals_get_considered_and_two_are_shown():
    views, items = ramiro_oct28()
    s = section("RG", views, items)
    shown = [v.deal for v, _, _ in s.highlights]
    assert {"Pernod Ricard", "Campari"} <= set(shown)                       # time-critical first
    assert sum(1 for v, _, _ in s.highlights if v.rank == 2) == 2           # In Talks preference met
    audit = {r.deal: r for r in s.audit}
    assert audit["Arca Peru"].signals and audit["Chifles"].signals          # both evaluated with evidence
    assert all(r.reason for r in s.audit)                                   # every eligible deal has a reason
    assert not audit["Farmater"].selected and audit["Farmater"].reason.startswith("outranked")


def test_time_critical_pilots_are_never_displaced_for_in_talks():
    pilots = [view(f"Pilot {n}", 4, owner="RG") for n in range(4)]
    talks = [view("Talks co", 2, owner="RG", blocker=Note("b", verified=True), recommendation=Note("r", verified=True))]
    items = [item(f"Pilot {n}", "promise", due=TODAY + timedelta(days=2), deal_owner="RG", id=f"p{n}") for n in range(4)]
    s = section("RG", pilots + talks, items)
    assert sorted(v.deal for v, _, _ in s.highlights) == [f"Pilot {n}" for n in range(4)]
    assert {r.deal: r for r in s.audit}["Talks co"].reason.startswith("outranked")


def test_in_talks_without_evidence_is_not_forced_in():
    s = section("RG", [view("Quiet talks", 2, owner="RG"), view("Pilot", 4, owner="RG", blocker=Note("b", verified=True))], [])
    reasons = {r.deal: r.reason for r in s.audit}
    assert "In Talks preference" not in reasons.get("Quiet talks", "")


def test_selection_is_stable_for_identical_inputs():
    import random
    views, items = ramiro_oct28()
    first = section("RG", views, items)
    for seed in range(5):
        v2, i2 = views[:], items[:]
        random.Random(seed).shuffle(v2)
        random.Random(seed + 1).shuffle(i2)
        again = section("RG", v2, i2)
        assert [v.deal for v, _, _ in again.highlights] == [v.deal for v, _, _ in first.highlights]
        assert [(r.deal, r.reason) for r in again.audit] == [(r.deal, r.reason) for r in first.audit]
