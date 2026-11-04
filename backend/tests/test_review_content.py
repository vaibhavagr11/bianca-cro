"""Record-level lineage and cumulative content, with no database or model calls."""

import copy
from datetime import datetime, timezone

import pytest

from app.review_content import (approved_lineage, bullet_id, merge_content, proposal_content,
                                source_id, source_version, validate_lineage)


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
            "occurred_at": NOW, "status": "open", **overrides}


def stored(content, **overrides):
    actions, context, sources, lineage = content
    return {"next_step_bullets": actions, "client_context_bullets": context, "sources": sources,
            "bullet_lineage": lineage, "lineage_version": 1, **overrides}


def test_only_records_containing_the_supporting_passage_receive_bullet_edges():
    email = record()
    notes = record(2, source="notes", body="A different discussion.")
    actions, context, sources, lineage = proposal_content([email, notes], [insight(source_record_ids=[email["id"], notes["id"]])])
    assert context == []
    assert actions[0] == "We owe: Send the launch checklist."
    assert lineage["nextStep"][0]["sourceIds"] == [source_id(email)]
    assert lineage["nextStep"][1]["sourceIds"] == [source_id(notes)]
    assert len(sources) == 2
    assert validate_lineage(actions, context, sources, lineage)


def test_same_bullet_from_multiple_records_is_deduplicated_and_unions_support():
    first, second = record(), record(2, source="notes")
    actions, context, sources, lineage = proposal_content([first, second], [
        insight(), insight(id="extract-2", source_record_ids=[second["id"]])])
    assert actions == ["We owe: Send the launch checklist."]
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
    actions, context, sources, lineage = proposal_content([record(), record(2)], [])
    assert len(sources) == 2
    assert sources[0]["type"] == sources[1]["type"] and sources[0]["date"] == sources[1]["date"]
    assert sources[0]["id"] != sources[1]["id"]
    assert len(actions) == 1, "Identical fallback wording may share one bullet with both true source edges."
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


def test_calendar_title_does_not_support_a_commercial_statement_but_fallback_has_lineage():
    meeting = record(source="calendar", body="", subject="Purchase approved", meta={"status": "cancelled"})
    actions, context, sources, lineage = proposal_content([meeting], [insight(
        type="buying_signal", quote="Purchase approved", text="Customer approved purchase.")])
    assert context == []
    assert actions[0].startswith("Meeting cancelled")
    assert lineage["nextStep"][0]["sourceIds"] == [source_id(meeting)]
    assert validate_lineage(actions, context, sources, lineage)


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
    for change in [{"quote": "Invented passage"}, {"status": "done"}, {"status": "possibly_done"}]:
        content = proposal_content([record()], [insight(**change)])
        assert content[0][0].startswith("Commercial email recorded")
        assert content[3]["nextStep"][0]["sourceIds"] == [source_id(record())]
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
    assert merged[0] == ["We owe: Send the launch checklist.", "We owe: Review the store codes."]
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
