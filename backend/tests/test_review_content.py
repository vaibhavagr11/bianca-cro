"""Record-level lineage and cumulative content, with no database or model calls."""

import copy
from datetime import datetime, timezone

import pytest

from app.review_content import (approved_lineage, bullet_id, merge_content, proposal_content, reviewed_content,
                                review_next_step_bullets, source_id, source_version, validate_lineage)


NOW = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)


def record(number=1, **overrides):
    return {"id": f"record-{number}", "source": "gmail", "external_id": f"message-{number}",
            "thread_id": "thread-1", "content_hash": f"content-{number}",
            "subject": "Pilot plan", "body": "Send the launch checklist. Review the store codes.",
            "occurred_at": NOW, "created_at": NOW, "meta": {}, "classification": {}, **overrides}


def insight(**overrides):
    return {"id": "extract-1", "type": "next_step", "text": "Send the launch checklist.",
            "quote": "Send the launch checklist.", "source_record_ids": ["record-1"],
            "owner_side": "bianca", "owner_name": None, "due_date": None,
            "occurred_at": NOW, "status": "open", "action_scope": "deal_progression", **overrides}


def stored(content, **overrides):
    actions, context, sources, lineage = content
    return {"next_step_bullets": actions, "client_context_bullets": context, "sources": sources,
            "bullet_lineage": lineage, "lineage_version": 1, **overrides}


def test_only_records_containing_the_supporting_passage_receive_bullet_edges():
    email = record()
    notes = record(2, source="notes", body="A different discussion.")
    actions, context, sources, lineage = proposal_content(
        [email, notes], [insight(action_scope=None, source_record_ids=[email["id"], notes["id"]])])
    assert context == []
    assert actions == ["Next step: Send the launch checklist."]
    assert lineage["nextStep"][0]["sourceIds"] == [source_id(email)]
    assert len(sources) == 1
    assert validate_lineage(actions, context, sources, lineage)


def test_origin_selects_exact_record_and_ambiguous_legacy_quote_is_unattributed():
    first, second = record(), record(2)
    possible_ids = [first["id"], second["id"]]
    old = insight(action_scope=None, source_record_ids=possible_ids)
    assert proposal_content([first, second], [old]) == proposal_content([], [])

    exact = insight(origin_record_id=second["id"], source_record_ids=possible_ids)
    actions, context, sources, lineage = proposal_content([first, second], [exact])
    assert lineage["nextStep"][0]["sourceIds"] == [source_id(second)]
    assert validate_lineage(actions, context, sources, lineage)
    assert proposal_content([first], [exact]) == proposal_content([], [])
    assert proposal_content([first, {**second, "body": "An unrelated reply."}], [exact]) == proposal_content([], [])


def test_same_bullet_from_multiple_records_is_deduplicated_and_unions_support():
    first, second = record(), record(2, source="notes")
    actions, context, sources, lineage = proposal_content([first, second], [
        insight(), insight(id="extract-2", source_record_ids=[second["id"]])])
    assert actions == ["Next step: Send the launch checklist."]
    assert len(sources) == 2
    assert lineage["nextStep"][0]["sourceIds"] == sorted([source_id(first), source_id(second)])
    assert validate_lineage(actions, context, sources, lineage)


def test_two_insights_from_one_source_have_two_edges_but_one_source_button():
    actions, context, sources, lineage = proposal_content([record()], [insight(), insight(
        id="extract-2", type="blocker", text="Store codes need checking.", quote="Review the store codes.")])
    assert len(actions) == 2 and len(sources) == 1
    assert all(node["sourceIds"] == [sources[0]["id"]] for node in lineage["nextStep"])
    assert set(sources[0]["quotes"]) == {"Send the launch checklist.", "Review the store codes."}
    assert validate_lineage(actions, context, sources, lineage)


def test_same_display_type_and_date_does_not_collapse_different_email_records():
    first, second = record(), record(2)
    actions, context, sources, lineage = proposal_content([first, second], [
        insight(origin_record_id=first["id"]), insight(origin_record_id=second["id"]),
    ])
    assert len(sources) == 2
    assert sources[0]["type"] == sources[1]["type"] and sources[0]["date"] == sources[1]["date"]
    assert sources[0]["id"] != sources[1]["id"]
    assert len(actions) == 1
    assert len(lineage["nextStep"][0]["sourceIds"]) == 2
    assert validate_lineage(actions, context, sources, lineage)


def test_source_identity_is_stable_across_reextraction_and_separate_for_real_versions():
    original = record(source="calendar", meta={"status": "confirmed"})
    alias = {**original, "id": "new-record-id", "updated_at": "later", "meta": {"status": "confirmed", "mailboxes": ["NDL"]}}
    assert source_id(original) == source_id(alias)
    assert source_version(original) == source_version(alias)
    assert source_id(original) != source_id({**original, "meta": {"status": "cancelled"}})
    assert source_id(original) != source_id({**original, "content_hash": "changed"})


def test_source_receipt_version_is_compatible_with_the_pre_lineage_branch():
    # Golden value computed with the original branch's source_version function.
    # Upgrading provenance must not make approved source receipts appear new.
    assert source_version(record()) == {"source": "gmail", "external_id": "message-1",
        "version": "efc8a284f7ebf201b2b2d58c0c3d3ef42e64e63b40436e01d864188ac09c420b"}


def test_calendar_title_and_gmail_subject_do_not_create_next_steps():
    meeting = record(source="calendar", body="", subject="Purchase approved", meta={"status": "cancelled"})
    email = record(2, subject="Can you send the proposal?", body="Thanks for your time.")
    content = proposal_content([meeting, email], [
        insight(type="buying_signal", quote=meeting["subject"], text="Customer approved purchase."),
        insight(quote=email["subject"], text="Customer asked for the proposal.", origin_record_id=email["id"]),
    ])
    assert content == proposal_content([], [])
    assert validate_lineage(*content)


def test_mixed_pilot_source_keeps_sales_actions_and_context():
    promise = "We will share the rollout proposal on Friday."
    pricing = "Can you send pricing for the paid rollout by Friday?"
    approval = "Approval for the paid rollout depends on pricing."
    dashboard = "Please fix the sales dashboard export."
    reporting = "Our weekly dashboard export is missing two regions."
    field_sales = "Our field sales team is tracking its visit targets."
    sales = record(body=" ".join((promise, pricing, approval, dashboard, reporting, field_sales)))
    support = record(2, body="Please rebuild the sales dashboard.")
    content = proposal_content([sales, support], [
        insight(type="commitment", text="Share the rollout proposal.", quote=promise,
                origin_record_id=sales["id"], due_date="2026-11-06"),
        insight(text="Send pricing for the paid rollout.", quote=pricing,
                origin_record_id=sales["id"], due_date="2026-11-06"),
        insight(type="objection", text=approval, quote=approval, origin_record_id=sales["id"]),
        insight(text="Fix the sales dashboard export.", quote=dashboard, origin_record_id=sales["id"],
                action_scope="delivery"),
        insight(type="need", text=reporting, quote=reporting, origin_record_id=sales["id"], action_scope="delivery"),
        insight(type="need", text=field_sales, quote=field_sales, origin_record_id=sales["id"],
                action_scope="out_of_scope"),
        insight(text="Please rebuild the sales dashboard.", quote="Please rebuild the sales dashboard.",
                origin_record_id=support["id"], action_scope="delivery"),
    ])
    actions, context, sources, lineage = content
    assert actions == [
        "Next step: Send pricing for the paid rollout. — requested by 2026-11-06",
        "We owe: Share the rollout proposal. — due 2026-11-06",
    ]
    assert context == ["Objection: Approval for the paid rollout depends on pricing."]
    assert [s["recordId"] for s in sources] == [sales["id"]]
    assert set(sources[0]["quotes"]) == {promise, pricing, approval}
    assert validate_lineage(*content)


@pytest.mark.parametrize("type_,destination", [("next_step", "nextStep"), ("commitment", "nextStep"),
                                               ("blocker", "nextStep"), ("need", "clientContext"),
                                               ("buying_signal", "clientContext"), ("objection", "clientContext")])
def test_type_routing_keeps_its_own_aligned_lineage(type_, destination):
    content = proposal_content([record()], [insight(type=type_, text="A supported statement.")])
    assert len(content[0] if destination == "nextStep" else content[1]) == 1
    assert len(content[3][destination]) == 1
    assert content[3][destination][0]["sourceIds"] == [source_id(record())]
    assert validate_lineage(*content)


def test_unverifiable_or_completed_insight_never_gets_a_false_support_edge():
    for change in [{"quote": "Invented passage"}, {"status": "done"}, {"status": "possibly_done"},
                   {"origin_record_id": "record-2"}]:
        content = proposal_content([record()], [insight(**change)])
        assert content == proposal_content([], [])
        assert validate_lineage(*content)


def test_bullet_ids_are_stable_for_normalized_text_but_destination_specific():
    assert bullet_id("nextStep", " Send the PLAN. ") == bullet_id("nextStep", "send  the plan.")
    assert bullet_id("nextStep", "Send plan.") != bullet_id("clientContext", "Send plan.")


def test_pending_merge_keeps_old_content_and_appends_new_days_with_exact_edges():
    first = proposal_content([record()], [insight()])
    next_record = record(2, source="notes", occurred_at="2026-11-22T12:00:00Z", body="Review the store codes.")
    fresh = proposal_content([next_record], [insight(source_record_ids=[next_record["id"]],
        text="Review the store codes.", quote="Review the store codes.")])
    before = copy.deepcopy(first)
    merged = merge_content(stored(first), fresh)
    assert merged[0] == ["Next step: Send the launch checklist.", "Next step: Review the store codes."]
    assert merged[3]["nextStep"][0]["sourceIds"] == first[3]["nextStep"][0]["sourceIds"]
    assert merged[3]["nextStep"][1]["sourceIds"] == fresh[3]["nextStep"][0]["sourceIds"]
    assert first == before, "Merging must not mutate a previous stored snapshot."
    assert validate_lineage(*merged)


def test_pending_merge_retains_earlier_bullet_even_if_new_extraction_no_longer_lists_it():
    first = proposal_content([record()], [insight()])
    empty = ([], [], [], {"nextStep": [], "clientContext": []})
    assert merge_content(stored(first), empty) == first


def test_merge_same_text_adds_new_support_and_keeps_old_version_snapshot():
    previous_record = record()
    first = proposal_content([previous_record], [insight()])
    changed = record(content_hash="new-version", body="Send the launch checklist. Updated wording elsewhere.")
    fresh = proposal_content([changed], [insight(id="new-extraction")])
    merged = merge_content(stored(first), fresh)
    assert len(merged[0]) == 1 and len(merged[2]) == 2
    assert merged[3]["nextStep"][0]["sourceIds"] == sorted([source_id(previous_record), source_id(changed)])
    assert len({source["version"] for source in merged[2]}) == 2


def test_merging_repeated_builds_is_idempotent_and_merges_quotes_on_one_source():
    first = proposal_content([record()], [insight()])
    more = proposal_content([record()], [insight(type="need", quote="Review the store codes.", text="Accurate codes are required.")])
    merged = merge_content(stored(first), more)
    assert len(merged[2]) == 1
    assert len(merged[2][0]["quotes"]) == 2
    assert merge_content(stored(merged), more) == merged


def test_legacy_flat_content_is_rebuilt_without_guessing_its_source_edges():
    fresh = proposal_content([record()], [insight()])
    legacy = {"next_step_bullets": ["Unsupported old action"], "sources": [{"type": "Email", "date": "Nov 4"}],
              "lineage_version": 0}
    assert merge_content(legacy, fresh) == fresh


@pytest.mark.parametrize("mutate", [
    lambda c: c[3]["nextStep"][0].update(sourceIds=["unknown-source"]),
    lambda c: c[3]["nextStep"][0].update(bulletId="wrong-bullet"),
    lambda c: c[3]["nextStep"].clear(),
    lambda c: c[2][0].update(id="wrong-source"),
    lambda c: c[2].append(copy.deepcopy(c[2][0])),
])
def test_bad_lineage_is_rejected_without_fabricating_an_attribution(mutate):
    content = proposal_content([record()], [insight()])
    mutate(content)
    assert not validate_lineage(*content)
    with pytest.raises(ValueError, match="lineage is incomplete"):
        merge_content(stored(content), proposal_content([], []))


def test_reordering_approved_bullets_uses_text_identity_not_the_old_array_position():
    content = proposal_content([record()], [insight(), insight(type="blocker", text="Review store codes.", quote="Review the store codes.")])
    approved = list(reversed(content[0]))
    lineage = approved_lineage(stored(content), approved, [])
    assert [node["bulletId"] for node in lineage["nextStep"]] == [bullet_id("nextStep", text) for text in approved]
    assert all(node["sourceIds"] == [source_id(record())] and node["origin"] == "source"
               for node in lineage["nextStep"])


def test_matching_formatted_deadlines_keep_both_sources_when_approved_once():
    first_text = "Send the checklist by Nov 21."
    second_text = "Send the Checklist on November 21."
    first = record(body=first_text)
    second = record(2, body=second_text)
    content = proposal_content([first, second], [
        insight(text=first_text, quote=first_text, source_record_ids=[first["id"]]),
        insight(text=second_text, quote=second_text, source_record_ids=[second["id"]]),
    ])
    proposal = stored(content)

    formatted = review_next_step_bullets(proposal)

    assert len(content[0]) == 2
    assert formatted == [formatted[0], formatted[0]]
    assert formatted[0].endswith("(complete by Nov 21, 2026)")
    lineage = approved_lineage(proposal, formatted[:1], [], review_next_steps=formatted)
    assert lineage["nextStep"][0]["sourceIds"] == sorted([source_id(first), source_id(second)])


def test_relative_deadlines_require_supporting_source_dates_that_agree():
    action = "Send the checklist next Friday."
    first = record(body=action)
    second = record(2, body=action, occurred_at=NOW.replace(day=7))
    content = proposal_content([first, second], [
        insight(text=action, quote=action, source_record_ids=[first["id"]]),
        insight(text=action, quote=action, source_record_ids=[second["id"]]),
    ])
    proposal = stored(content, created_at=NOW.replace(day=15))

    assert review_next_step_bullets(proposal) == content[0]
    assert review_next_step_bullets(proposal, frozenset((source_id(first), source_id(second)))) == content[0]
    assert review_next_step_bullets(proposal, frozenset((source_id(first),))) == [
        "Next step: Send the checklist. (complete by Nov 6, 2026)"
    ]


def test_manual_edits_and_destination_moves_do_not_inherit_source_attribution():
    content = proposal_content([record()], [insight()])
    lineage = approved_lineage(stored(content), ["Send a different offer."], [content[0][0]])
    assert lineage["nextStep"][0]["sourceIds"] == []
    assert lineage["clientContext"][0]["sourceIds"] == []
    assert all(node["origin"] == "founder_edit" for nodes in lineage.values() for node in nodes)
    assert validate_lineage(["Send a different offer."], [content[0][0]], content[2], lineage, allow_manual=True)
    assert not validate_lineage(["Send a different offer."], [content[0][0]], content[2], lineage)


def test_empty_content_has_a_valid_empty_graph():
    content = proposal_content([], [])
    assert content == ([], [], [], {"nextStep": [], "clientContext": []})
    assert validate_lineage(*content)


def _step(number, day, **overrides):
    subjects = ("", "Send Ana the price list", "Book the Lima workshop", "Share the forecast sample",
                "Confirm store codes", "Draft the pilot scope", "", "Ana needs faster reporting")
    text = overrides.pop("text", f"Step {number}: {subjects[number]}.")
    return (record(number, body=text, occurred_at=datetime(2026, 11, day, 15, tzinfo=timezone.utc)),
            insight(id=f"extract-{number}", text=text, quote=text, source_record_ids=[f"record-{number}"],
                    origin_record_id=f"record-{number}",
                    occurred_at=datetime(2026, 11, day, 15, tzinfo=timezone.utc), **overrides))


def test_next_steps_lead_with_bianca_actions_newest_first_then_customer_items_and_blockers():
    pairs = [_step(1, 2, owner_side="customer"), _step(2, 3, type="blocker"), _step(3, 4), _step(4, 9),
             _step(5, 10, owner_side=None)]
    actions, context, sources, lineage = proposal_content([r for r, _ in pairs], [i for _, i in pairs])
    assert [text.split(":")[0] for text in actions] == [
        "Next step", "Next step", "Next step (owner to confirm)", "Customer next step", "Blocker"]
    assert actions[0].startswith("Next step: Step 4") and actions[1].startswith("Next step: Step 3")
    assert validate_lineage(actions, context, sources, lineage)


def test_card_limit_keeps_the_newest_bullets_and_names_the_sources_it_retires():
    pairs = [_step(number, day) for number, day in ((1, 1), (2, 5), (3, 9), (4, 12))]
    need = _step(7, 2, type="need")
    records, insights = [r for r, _ in pairs] + [need[0]], [i for _, i in pairs] + [need[1]]
    (actions, context, sources, lineage), retired = reviewed_content(records, insights, max_next_steps=2, max_context=4)
    assert [text[:17] for text in actions] == ["Next step: Step 4", "Next step: Step 3"]
    assert len(context) == 1 and len(sources) == 3
    assert validate_lineage(actions, context, sources, lineage)
    assert retired == sorted((source_version(pairs[0][0]), source_version(pairs[1][0])),
                             key=lambda item: (item["source"], item["external_id"], item["version"]))
    # No limit, or a limit that is not exceeded, retires nothing.
    assert reviewed_content(records, insights)[1] == []
    assert reviewed_content(records, insights, max_next_steps=4, max_context=1)[1] == []


def test_a_source_that_still_supports_a_kept_bullet_is_not_retired():
    shared = record(1, body="Send the price list to Ana today. Confirm the store codes with the regional team.",
                    occurred_at=datetime(2026, 11, 1, 15, tzinfo=timezone.utc))
    old = insight(id="a", text="Confirm the store codes with the regional team.",
                  quote="Confirm the store codes with the regional team.", origin_record_id="record-1",
                  occurred_at=datetime(2026, 11, 1, 15, tzinfo=timezone.utc))
    need = insight(id="b", type="need", text="Ana needs the price list.", quote="Send the price list to Ana today.",
                   origin_record_id="record-1")
    newer = _step(2, 9)
    (actions, context, sources, lineage), retired = reviewed_content(
        [shared, newer[0]], [old, need, newer[1]], max_next_steps=1)
    assert actions == ["Next step: Step 2: Book the Lima workshop."] and context == ["Customer need: Ana needs the price list."]
    assert retired == [] and len(sources) == 2
    assert validate_lineage(actions, context, sources, lineage)


def test_two_wordings_of_one_step_become_one_bullet_with_both_sources():
    ours = _step(1, 9, text="Send Franco the sales analysis deck and schedule the pilot scoping meeting.")
    theirs = _step(2, 10, owner_side="customer",
                   text="Franco to review the sales analysis deck before the pilot scoping meeting.")
    other = _step(3, 10, text="Ask Ana for the KeHE portal credentials.")
    actions, context, sources, lineage = proposal_content(
        [ours[0], theirs[0], other[0]], [ours[1], theirs[1], other[1]])
    assert actions == ["Next step: Ask Ana for the KeHE portal credentials.",
                       "Next step: Send Franco the sales analysis deck and schedule the pilot scoping meeting."]
    assert lineage["nextStep"][1]["sourceIds"] == sorted([source_id(ours[0]), source_id(theirs[0])])
    assert validate_lineage(actions, context, sources, lineage)


def test_short_or_different_steps_are_never_merged():
    first = _step(1, 9, text="Follow up with Ana.")
    second = _step(2, 9, text="Follow up with Paul.")
    third = _step(3, 9, text="Send the proposal to the commercial team in Mexico.")
    fourth = _step(4, 9, text="Send the contract to the legal team in Chile.")
    pairs = [first, second, third, fourth]
    assert len(proposal_content([r for r, _ in pairs], [i for _, i in pairs])[0]) == 4


# ---- H-06: an emailed ask that Bianca already answered in the same thread is not suggested again

from datetime import timedelta

from app.review_content import answered_email_asks, is_email_ask

INTERNAL = frozenset({"bianca-ai.com", "kellogg.northwestern.edu"})


def mail(number, sender, to, *, hours=0, thread="thread-1", subject="Meeting next week", **overrides):
    return {"id": f"record-{number}", "thread_id": thread, "occurred_at": NOW + timedelta(hours=hours),
            "subject": subject, "participants": {"from": [{"address": sender}], "to": [{"address": a} for a in to]},
            **overrides}


def ask(**overrides):
    return insight(**{"id": "ask-1", "source": "gmail", "origin_record_id": "record-1",
                      "text": "Confirm a time on Wednesday morning.", "quote": "Does Wednesday morning work?", **overrides})


CUSTOMER_EMAIL = mail(1, "ana@redbull.com", ["nico@bianca-ai.com"])
OUR_REPLY = mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=3)


def test_an_emailed_ask_is_answered_once_bianca_writes_back_in_the_same_thread():
    assert is_email_ask(ask())
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL, OUR_REPLY], INTERNAL) == {"ask-1"}
    # The Kellogg project team writes on Bianca's behalf, as in the brief.
    team = mail(2, "gibb@kellogg.northwestern.edu", ["ana@redbull.com"], hours=3)
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL, team], INTERNAL) == {"ask-1"}
    # Stored timestamps may arrive as text.
    as_text = [{**m, "occurred_at": m["occurred_at"].isoformat()} for m in (CUSTOMER_EMAIL, OUR_REPLY)]
    assert answered_email_asks([ask()], as_text, INTERNAL) == {"ask-1"}


@pytest.mark.parametrize("later", [
    [],                                                                               # nothing after the ask
    [mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=-2)],                   # our email came before the ask
    [mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=0)],                    # same instant proves nothing
    [mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=3, thread="thread-2")],  # another conversation
    [mail(2, "nico@bianca-ai.com", ["ramiro@bianca-ai.com"], hours=3)],               # an internal forward
    [mail(2, "ana@redbull.com", ["nico@bianca-ai.com"], hours=3)],                    # the customer wrote again
    [mail(2, "mailer-daemon@bianca-ai.com", ["ana@redbull.com"], hours=3)],           # a bounce
    [mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=3, subject="Automatic reply: Meeting next week")],
    [mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=3, occurred_at=None)],  # undated
    [{**mail(2, "nico@bianca-ai.com", ["ana@redbull.com"], hours=3), "participants": None}],
])
def test_anything_short_of_a_later_reply_from_bianca_to_the_outside_keeps_the_ask(later):
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL, *later], INTERNAL) == frozenset()


@pytest.mark.parametrize("change", [
    {"type": "commitment"},                 # a promise is a deliverable, not a reply
    {"owner_side": "customer"},             # something the customer owes
    {"owner_side": None},
    {"source": "notes"},                    # said in a meeting: an email does not prove it was done
    {"origin_record_id": None},             # older items not tied to one message
    {"origin_record_id": "record-9"},       # origin not among the thread's messages
    {"type": "blocker"},
])
def test_only_next_steps_for_bianca_taken_from_one_customer_email_are_checked(change):
    assert answered_email_asks([ask(**change)], [CUSTOMER_EMAIL, OUR_REPLY], INTERNAL) == frozenset()


def test_an_ask_in_our_own_email_or_an_automatic_message_is_never_treated_as_answered():
    ours = mail(1, "nico@bianca-ai.com", ["ana@redbull.com"])
    assert answered_email_asks([ask()], [ours, OUR_REPLY], INTERNAL) == frozenset()
    auto = mail(1, "noreply@redbull.com", ["nico@bianca-ai.com"])
    assert answered_email_asks([ask()], [auto, OUR_REPLY], INTERNAL) == frozenset()
    undated = {**CUSTOMER_EMAIL, "occurred_at": None}
    assert answered_email_asks([ask()], [undated, OUR_REPLY], INTERNAL) == frozenset()


def test_a_reply_that_is_only_about_delivery_work_does_not_answer_a_sales_ask():
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL, OUR_REPLY], INTERNAL, not_sales=frozenset({"record-2"})) == frozenset()
    second = mail(3, "nico@bianca-ai.com", ["ana@redbull.com"], hours=5)
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL, OUR_REPLY, second], INTERNAL,
                               not_sales=frozenset({"record-2"})) == {"ask-1"}


def test_a_thread_marked_done_in_the_brief_answers_asks_up_to_that_click_only():
    clicked = NOW + timedelta(hours=1)
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL], INTERNAL, thread_done={"thread-1": clicked}) == {"ask-1"}
    assert answered_email_asks([ask()], [CUSTOMER_EMAIL], INTERNAL, thread_done={"thread-2": clicked}) == frozenset()
    # A newer customer email after the click is a new ask.
    newer = mail(3, "ana@redbull.com", ["nico@bianca-ai.com"], hours=4)
    later_ask = ask(id="ask-2", origin_record_id="record-3")
    assert answered_email_asks([ask(), later_ask], [CUSTOMER_EMAIL, newer], INTERNAL,
                               thread_done={"thread-1": clicked}) == {"ask-1"}


def test_an_answered_ask_leaves_the_card_and_everything_else_stays():
    email = record(body="Does Wednesday morning work? We also need the store list.")
    asks = [insight(id="ask-1", text="Confirm a time on Wednesday morning.", quote="Does Wednesday morning work?"),
            insight(id="ask-2", text="Send the store list.", quote="We also need the store list."),
            insight(id="need-1", type="need", text="Store-level reporting.", quote="We also need the store list.")]
    (steps, context, sources, lineage), retired = reviewed_content([email], asks, answered=frozenset({"ask-1"}))
    assert steps == ["Next step: Send the store list."]
    assert context == ["Customer need: Store-level reporting."]
    assert len(sources) == 1 and retired == []
    assert validate_lineage(steps, context, sources, lineage)
    # With no rule input the card is exactly what it was before this rule existed.
    assert reviewed_content([email], asks)[0][0] == [
        "Next step: Confirm a time on Wednesday morning.", "Next step: Send the store list."]


# ---- B-10: a step that only says "keep following up" is not suggested; H-10: cards that are only waiting

from app.brief_followups import is_followup_only
from app.review_content import is_vague_followup, is_waiting_on_them

VAGUE = [
    "Keep following up.", "keep following up", "Continue to follow up", "Continue following up with the customer.",
    "Keep following up with Andina", "Keep following up with Andina until they reply.",
    "Bianca should keep following up with Chifles.", "The team should continue to follow up",
    "Ramiro to keep following up with Arca Peru", "We need to keep following up with them",
    "Follow up.", "Follow-up", "Follow up with the customer", "Follow up with them again",
    "Follow up again with Renzo", "Send a follow-up", "Send a follow-up email.", "Send another follow-up to Juan",
    "Send a reminder to the team", "Check in again", "Keep checking in with the client periodically",
    "Stay in touch", "Keep in touch with them for now", "- Keep following up",
    "Seguir haciendo seguimiento", "Continuar con el seguimiento con el cliente", "Dar seguimiento",
]
SPECIFIC = [
    "Follow up with Renzo",                                         # names who to write to
    "Follow up with Renzo about the FEMSA walkthrough",
    "Follow up on the pricing proposal", "Follow up next week", "Follow up this week",
    "Keep following up on the NDA", "Follow up by Nov 13", "Follow up with Juan on Monday",
    "Follow up in two weeks if there is no reply", "Keep following up with the pricing proposal",
    "Send a follow-up with the revised quote", "Send a follow-up email with three time slots",
    "Send another follow-up to Juan about the store data", "Send the follow-up deck",
    "Send Patricio a few bullet points on the idea", "Reconnect", "Email her Monday",
    "Check on tax docs and onboarding", "Check in with Zach about the store data",
    "Re-push Renzo for the 15-minute FEMSA walkthrough", "Chase Zach for the store data",
    "Follow up to confirm the Wednesday 2 pm call", "Schedule a follow-up meeting",
    "Follow up with the buyer to get the signed NDA", "Hold the follow-up call on Nov 6",
    "Waiting for reply from their team in terms of when do we meet", "",
]


@pytest.mark.parametrize("text", VAGUE)
def test_content_free_follow_ups_are_recognised(text):
    assert is_vague_followup(text) is True


@pytest.mark.parametrize("text", SPECIFIC)
def test_a_step_with_a_topic_a_date_or_a_named_person_is_never_treated_as_vague(text):
    assert is_vague_followup(text) is False


def test_the_vague_filter_is_narrower_than_the_brief_nudge_check():
    # The brief treats any nudge-only suggestion as a follow-up; the hub drops only the ones with no content.
    for text in ("Follow up with Renzo about the FEMSA walkthrough", "Follow up on the pricing proposal"):
        assert is_followup_only(text) and not is_vague_followup(text)
    for text in ("Follow up.", "Follow up with the customer", "Send a follow-up", "Check in again"):
        assert is_followup_only(text) and is_vague_followup(text)


def test_vague_follow_ups_leave_the_card_but_dated_customer_owed_and_context_items_stay():
    body = ("Keep following up. They will keep following up with us. Follow up on the pricing proposal. "
            "Send a follow-up. We keep following up but they are slow.")
    email = record(body=body)
    def item(n, text, quote, **changes):
        return insight(id=f"i-{n}", text=text, quote=quote, **changes)
    insights = [
        item(1, "Keep following up.", "Keep following up."),                                   # dropped
        item(2, "Keep following up", "Keep following up.", type="commitment"),                 # dropped
        item(3, "Keep following up", "They will keep following up with us.", owner_side="customer"),
        item(4, "Follow up on the pricing proposal", "Follow up on the pricing proposal."),
        item(5, "Send a follow-up", "Send a follow-up.", due_date="2026-11-20"),               # a date is content
        item(6, "Keep following up", "We keep following up but they are slow.", type="objection"),
        item(7, "Keep following up", "We keep following up but they are slow.", type="blocker"),
    ]
    (steps, context, sources, lineage), _ = reviewed_content([email], insights)
    assert steps == ["Next step: Follow up on the pricing proposal", "Next step: Send a follow-up — requested by 2026-11-20",
                     "Customer next step: Keep following up", "Blocker: Keep following up"]
    assert context == ["Objection: Keep following up"]
    assert validate_lineage(steps, context, sources, lineage)


def test_a_card_with_only_a_vague_follow_up_has_no_content():
    (steps, context, sources, _), retired = reviewed_content(
        [record(body="Keep following up with Andina.")],
        [insight(text="Keep following up with Andina", quote="Keep following up with Andina.")])
    assert steps == context == sources == retired == []


def test_each_next_step_bullet_records_who_it_is_with_and_the_card_knows_when_it_is_only_waiting():
    email = record(body="We will send the deck. Ana will share the store list. Budget is frozen. Someone books the room. "
                        "They want store-level reporting.")
    def item(n, quote, **changes):
        return insight(id=f"i-{n}", text=quote.rstrip("."), quote=quote, **changes)
    ours = item(1, "We will send the deck.", type="commitment")
    theirs = item(2, "Ana will share the store list.", type="commitment", owner_side="customer")
    blocker = item(3, "Budget is frozen.", type="blocker")
    unowned = item(4, "Someone books the room.", owner_side=None)
    need = item(5, "They want store-level reporting.", type="need")
    lineage = proposal_content([email], [ours, theirs, blocker, unowned, need])[3]
    assert [node["side"] for node in lineage["nextStep"]] == ["bianca", "unassigned", "customer", "blocker"]
    assert all("side" not in node for node in lineage["clientContext"])
    assert is_waiting_on_them(lineage) is False
    assert is_waiting_on_them(proposal_content([email], [theirs, blocker, need])[3]) is True
    assert is_waiting_on_them(proposal_content([email], [theirs])[3]) is True
    assert is_waiting_on_them(proposal_content([email], [blocker, unowned])[3]) is False   # an unowned step may be ours
    # No Next Step bullets, or a card built before sides were recorded: the card cannot say.
    assert is_waiting_on_them(proposal_content([email], [need])[3]) is None
    old = proposal_content([email], [theirs])[3]
    del old["nextStep"][0]["side"]
    assert is_waiting_on_them(old) is None
    for broken in (None, {}, {"nextStep": "x"}, {"nextStep": [None]}, {"nextStep": [{"side": "martian"}]}):
        assert is_waiting_on_them(broken) is None


def test_an_unedited_step_keeps_its_side_in_the_decision_lineage():
    email = record(body="Ana will share the store list.")
    theirs = insight(text="Share the store list", quote="Ana will share the store list.", type="commitment", owner_side="customer")
    content = proposal_content([email], [theirs])
    approved = approved_lineage(stored(content), [content[0][0], "A founder's own step"], [])
    assert approved["nextStep"][0]["side"] == "customer" and approved["nextStep"][0]["origin"] == "source"
    assert "side" not in approved["nextStep"][1] and approved["nextStep"][1]["origin"] == "founder_edit"
