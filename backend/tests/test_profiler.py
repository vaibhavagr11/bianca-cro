from fastapi.testclient import TestClient

from app.main import app
from app.profiler import Action, Commitment, Profile, Stakeholder, Statement, check, render_inputs


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
    assert "Pipeline 'next step' cell: - Send proposal" in body and "Jane <jane@acme.com>" in body


def test_profile_endpoints_require_token():
    client = TestClient(app)
    assert client.post("/process/profile", json={"opportunity_ids": []}).status_code == 401
    assert client.get("/opportunities/x/profile").status_code == 401
