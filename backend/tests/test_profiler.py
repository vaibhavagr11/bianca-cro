from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.profiler import Action, Commitment, Profile, Stakeholder, Statement, SYSTEM_PROMPT, check, generate, render_inputs


def st(text, *ev, basis="confirmed"):
    return Statement(text=text, basis=basis, evidence=list(ev))


def profile(**kw):
    base = dict(
        summary=st("Pilot agreed.", "I1"), momentum="accelerating", momentum_reason=st("Moving fast.", "I1", "I2"),
        stakeholders=[Stakeholder(name="Agus", role="Sales", side="customer", evidence=["I1"])],
        needs=[st("On-trade mapping.", "I2"), st("Invented need.", "I99")],
        buying_signals=[st("Vendor forms sent.", "I1")], objections=[], blockers=[st("NDA first.", "I3")],
        open_commitments=[Commitment(text="Send forms", owner="NC", due=None, state="open", evidence=["I4", "I77"])],
        recommended_actions=[Action(action="Sign NDA", why="Unblocks data", urgency="high", evidence=[])],
        unknowns=["Budget"],
    )
    base.update(kw)
    return Profile(**base)


def test_check_drops_uncited_and_unknown_citations():
    data, dropped = check(profile(), {"I1", "I2", "I3", "I4"})
    assert [n["text"] for n in data["needs"]] == ["On-trade mapping."]
    assert data["open_commitments"][0]["evidence"] == ["I4"]
    assert data["recommended_actions"] == []
    assert dropped == 2


def test_check_flags_unsupported_summary_instead_of_dropping_it():
    data, _ = check(profile(summary=st("Unsupported.", "I50")), {"I1", "I2"})
    assert data["summary"]["text"] == "Unsupported." and data["summary"]["unsupported"]
    assert data["summary"]["basis"] == "inferred"


def test_render_inputs_lists_refs_owner_and_due():
    body = render_inputs(
        {"company": "Acme", "country": "AR", "stage": "3. Proposal", "owner_code": "RG", "last_progress": None,
         "next_step": "- Send proposal"},
        {"counts": {"gmail": 1}, "last_activity_at": "2026-10-20", "next_meeting": None},
        [{"name": "Jane", "email": "jane@acme.com", "record_count": 3}],
        [{"ref": "I1", "occurred_at": "2026-10-20T10:00:00", "source": "gmail", "type": "commitment",
          "text": "Send forms.", "owner_name": "NC", "due_date": "2026-10-24"}],
        "2026-10-21")
    assert "I1 | 2026-10-20 | gmail | commitment | Send forms. (owner: NC, due: 2026-10-24)" in body
    assert "P1 | Pipeline row for Acme: country AR, stage '3. Proposal', owner RG" in body and "next step: - Send proposal" in body
    assert "A1 | As of 2026-10-21: linked activity 1 gmail; last activity 2026-10-20; next meeting none scheduled" in body
    assert "Jane <jane@acme.com>" in body


def test_check_accepts_fact_citations():
    data, dropped = check(profile(summary=st("Agreed to become recurring.", "P1"),
                                  momentum_reason=st("No meeting scheduled.", "A1")), {"I1", "I2", "I3", "I4", "P1", "A1"})
    assert data["summary"]["evidence"] == ["P1"] and "unsupported" not in data["summary"]


@pytest.mark.parametrize(("stage", "uses_pilot_scope"), [("4. Pilot", True), ("3. Proposal", False)])
def test_generated_recommendations_use_pilot_scope_only_at_pilot_stage(stage, uses_pilot_scope):
    client = Mock()
    client.messages.parse.return_value.parsed_output = profile()
    generate(client, "model", "Pilot dashboard and paid rollout discussion.", stage=stage)

    system = client.messages.parse.call_args.kwargs["system"][0]["text"]
    if uses_pilot_scope:
        assert '"Can you fix the dashboard by Friday?" supports no sales recommendation.' in system
        assert "recommend confirming the decision or its criteria" in system
        assert '"send pricing for 20 additional seats" as a commercial request' in system
    else:
        assert system == SYSTEM_PROMPT


def test_profile_endpoints_require_token():
    client = TestClient(app)
    assert client.post("/process/profile", json={"opportunity_ids": []}).status_code == 401
    assert client.get("/opportunities/x/profile").status_code == 401


# ---- finding 17 (11/09 evals): the Farmater suggestion was reworded Mon -> Tue with the same Nov 5 source

def test_regenerated_suggestion_keeps_its_previous_wording_when_it_is_the_same_suggestion():
    from app.profiler import keep_wording

    previous = {"refs": {"I1": "need-1", "I2": "ask-2", "I7": "old-7"}, "recommended_actions": [
        {"action": "Send Ana the pricing deck for the paid rollout", "evidence": ["I1", "I2"]},
        {"action": "Send the pilot terms", "evidence": ["I7"]},
    ]}
    # The new profile numbers its insights differently and rewords everything.
    data = {"refs": {"I3": "need-1", "I4": "ask-2", "I5": "old-7", "I6": "new-9"}, "recommended_actions": [
        {"action": "Share the pricing deck with Ana for the paid rollout", "evidence": ["I4", "I3"]},   # same thing
        {"action": "Confirm the buyer received the pilot terms", "evidence": ["I5"]},     # same source, moved on
        {"action": "Book the scoping call", "evidence": ["I6"]},                           # new evidence
    ]}
    assert keep_wording(data, previous) == 1
    assert [a["action"] for a in data["recommended_actions"]] == [
        "Send Ana the pricing deck for the paid rollout",
        "Confirm the buyer received the pilot terms",
        "Book the scoping call",
    ]
    assert keep_wording(data, None) == 0 and keep_wording(data, {}) == 0


def test_two_previous_suggestions_on_the_same_evidence_are_too_ambiguous_to_reuse():
    from app.profiler import keep_wording

    previous = {"refs": {"I1": "a"}, "recommended_actions": [
        {"action": "Send the pricing deck", "evidence": ["I1"]}, {"action": "Send the pricing sheet", "evidence": ["I1"]}]}
    data = {"refs": {"I1": "a"}, "recommended_actions": [{"action": "Share the pricing deck", "evidence": ["I1"]}]}
    assert keep_wording(data, previous) == 0 and data["recommended_actions"][0]["action"] == "Share the pricing deck"
