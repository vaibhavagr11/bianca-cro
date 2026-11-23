"""Priority selection preserves grounded candidates and never turns a soft preference into truncation."""

import copy
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.review_content import proposal_content, validate_lineage
from app.review_selection import (Selection, SelectionError, VERSION, apply_selection, candidate_keys,
                                  filter_reviewed, fingerprint, select, selection_input)


NOW = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)
DEAL = {"company_name": "Example", "owner_code": "RG", "stage": "3. Proposal", "last_progress": "Demo held",
        "next_step": "Send proposal", "client_context": "Finance must approve annual spend."}


def content(action_texts=(), context_texts=(), *, sides=None):
    records, insights = [], []
    for n, text in enumerate([*action_texts, *context_texts]):
        side = (sides or {}).get(n, "bianca")
        kind = "next_step" if n < len(action_texts) else "need"
        if side == "blocker":
            kind = "blocker"
        records.append({"id": f"record-{n}", "source": "notes", "external_id": f"note-{n}",
                        "content_hash": f"content-{n}", "subject": "Commercial review", "body": text,
                        "occurred_at": NOW, "created_at": NOW, "meta": {}})
        insights.append({"id": f"insight-{n}", "type": kind, "text": text, "quote": text,
                         "origin_record_id": f"record-{n}", "source_record_ids": [f"record-{n}"],
                         "owner_side": side, "owner_name": "Bianca owner" if side == "bianca" else None,
                         "due_date": "2026-11-25" if kind == "next_step" else None,
                         "occurred_at": NOW, "status": "open", "action_scope": "deal_progression"})
    return proposal_content(records, insights)


def client_returning(next_steps=(), context=(), *, stop_reason="end_turn"):
    client = Mock()
    client.messages.parse.return_value = SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=Selection(nextStep=list(next_steps), clientContext=list(context)),
    )
    return client


def metadata_for(candidates, **overrides):
    return {"version": VERSION, "fingerprint": fingerprint(candidates, DEAL, TODAY),
            **candidate_keys(candidates), **overrides}


def test_more_than_three_distinct_important_actions_and_context_items_survive_without_truncation():
    candidates = content(
        ["Send the annual pricing proposal.", "Negotiate the exclusivity clause with legal.",
         "Document evaluation criteria before the buyer's pilot decision.", "Answer procurement's security questionnaire.",
         "Prepare the requested expansion quotation."],
        ["The buyer requires national store coverage.", "CFO approval is mandatory for annual spending.",
         "A regional rollout must finish before the December launch.", "Data residency is a purchasing condition."],
    )
    keys = candidate_keys(candidates)
    client = client_returning(keys["nextStep"], keys["clientContext"])
    selected, metadata = select(client, "test-model", candidates, DEAL, TODAY)
    assert selected == candidates
    assert len(selected[0]) == 5 and len(selected[1]) == 4
    assert metadata == metadata_for(candidates)
    assert validate_lineage(*selected)


def test_selection_keeps_model_priority_order_and_exact_owner_requested_date_and_evidence():
    candidates = content(["Send pricing before procurement review.", "Resolve contract indemnity with legal."])
    keys = list(reversed(candidate_keys(candidates)["nextStep"]))
    original = copy.deepcopy(candidates)
    selected, metadata = select(client_returning(keys), "test-model", candidates, DEAL, TODAY)
    assert selected[0] == list(reversed(candidates[0]))
    assert all("(owner: Bianca owner)" in text and "requested by 2026-11-25" in text for text in selected[0])
    assert selected[3]["nextStep"] == list(reversed(candidates[3]["nextStep"]))
    assert candidates == original
    assert apply_selection(candidates, metadata) == selected


@pytest.mark.parametrize("selection", [
    {"nextStep": ["invented"], "clientContext": []},
    {"nextStep": "not-a-list", "clientContext": []},
])
def test_invalid_identifiers_or_shapes_cannot_reach_review(selection):
    candidates = content(["Send pricing."], ["Buyer needs a national rollout."])
    with pytest.raises(SelectionError):
        apply_selection(candidates, metadata_for(candidates, **selection))


def test_unknown_wrong_section_and_duplicate_model_ids_are_rejected():
    candidates = content(["Send pricing."], ["Buyer needs a national rollout."])
    keys = candidate_keys(candidates)
    for invalid in (["invented"], keys["clientContext"], keys["nextStep"] * 2):
        with pytest.raises(SelectionError):
            select(client_returning(invalid), "test-model", candidates, DEAL, TODAY)


@pytest.mark.parametrize("text,side", [
    ("The sponsor will discuss internally and let Bianca know if they want to explore further.", "customer"),
    ("The contact said their company will review the proposed next steps and get back to Bianca.", "customer"),
    ("Purchase is blocked while the customer awaits internal legal approval.", "blocker"),
])
def test_customer_waiting_and_blocker_status_cannot_become_bianca_actions_even_if_model_selects_them(text, side):
    candidates = content([text], sides={0: side})
    with pytest.raises(SelectionError):
        select(client_returning(candidate_keys(candidates)["nextStep"]), "test-model", candidates, DEAL, TODAY)


def test_zero_selected_is_valid_and_does_not_fall_back_to_routine_items():
    candidates = content(["Bianca needs to confirm the buyer could log into the prospecting platform and is using "
                          "the prospects; he has not replied to two follow-ups."],
                         ["The customer repeated their generic interest during the meeting."])
    selected, metadata = select(client_returning(), "test-model", candidates, DEAL, TODAY)
    assert selected == ([], [], [], {"nextStep": [], "clientContext": []})
    assert metadata["nextStep"] == [] and metadata["clientContext"] == []
    assert len(candidates[0]) == len(candidates[1]) == 1  # Deferred evidence remains intact.


def test_empty_input_needs_no_model_request():
    client = Mock()
    selected, metadata = select(client, "test-model", content(), DEAL, TODAY)
    assert not selected[0] and not selected[1]
    assert metadata["fingerprint"] == fingerprint(content(), DEAL, TODAY)
    client.messages.parse.assert_not_called()


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refused_or_incomplete_output_is_not_used_even_if_it_contains_ids(stop_reason):
    candidates = content(["Send the pricing proposal."])
    with pytest.raises(SelectionError):
        select(client_returning(candidate_keys(candidates)["nextStep"], stop_reason=stop_reason),
               "test-model", candidates, DEAL, TODAY)


def test_model_failure_has_no_content_fallback_or_source_content_in_error():
    candidates = content(["Send commercially confidential proposal."])
    client = Mock()
    client.messages.parse.side_effect = RuntimeError("commercially confidential proposal")
    with pytest.raises(SelectionError) as failure:
        select(client, "test-model", candidates, DEAL, TODAY)
    assert "commercially confidential" not in str(failure.value)


def test_missing_or_wrong_version_snapshot_cannot_be_replayed():
    candidates = content(["Send the pricing proposal."])
    for metadata in ({}, metadata_for(candidates, version="old-selection"), metadata_for(candidates, fingerprint="")):
        with pytest.raises(SelectionError):
            apply_selection(candidates, metadata)


def test_reviewed_candidate_filter_preserves_unreviewed_sibling_with_the_same_source():
    candidates = content(["Send annual pricing.", "Prepare security answers."])
    shared = copy.deepcopy(candidates[2][0])
    shared["quotes"] += candidates[2][1]["quotes"]
    candidates[2][:] = [shared]
    for node in candidates[3]["nextStep"]:
        node["sourceIds"] = [shared["id"]]
    keys = candidate_keys(candidates)["nextStep"]
    filtered = filter_reviewed(candidates, [keys[0]])
    assert filtered[0] == [candidates[0][1]]
    assert filtered[2] == [shared]
    assert candidate_keys(filtered)["nextStep"] == [keys[1]]
    assert validate_lineage(*filtered)


def test_fingerprint_changes_when_ranking_context_day_or_evidence_changes_but_candidate_receipts_stay_stable():
    candidates = content(["Send annual pricing."])
    baseline = fingerprint(candidates, DEAL, TODAY)
    keys = candidate_keys(candidates)
    for changed in ({**DEAL, "stage": "4. Pilot"},
                    {**DEAL, "client_context": "Budget was approved."},
                    {**DEAL, "last_progress": "Annual proposal sent."},
                    {**DEAL, "next_step": "Await signature."}):
        assert fingerprint(candidates, changed, TODAY) != baseline
    assert fingerprint(candidates, DEAL, date(2026, 11, 24)) != baseline
    changed = copy.deepcopy(candidates)
    changed[2][0]["quotes"].append("The buyer needs this before procurement can decide.")
    assert fingerprint(changed, DEAL, TODAY) != baseline
    assert candidate_keys(changed) == keys


def test_record_aliases_do_not_change_receipts_or_cached_selection():
    candidates = content(["Send annual pricing."])
    aliased = copy.deepcopy(candidates)
    aliased[2][0]["recordId"] = "different-database-uuid"
    assert candidate_keys(aliased) == candidate_keys(candidates)
    assert fingerprint(aliased, DEAL, TODAY) == fingerprint(candidates, DEAL, TODAY)


def test_build_and_stored_proposal_cells_produce_the_same_selection_input():
    candidates = content(["Send annual pricing."])
    proposal = {"company_name": DEAL["company_name"], "owner_code": DEAL["owner_code"], "stage": DEAL["stage"],
                "snapshot_last_progress": DEAL["last_progress"], "snapshot_next_step": DEAL["next_step"],
                "snapshot_client_context": DEAL["client_context"]}
    assert selection_input(candidates, proposal, TODAY) == selection_input(candidates, DEAL, TODAY)
