from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.brief import (
    Candidate, Change, ChosenAction, FounderPlan, Meeting, PrepLine, commitment_candidate, fallback_plan,
    pick_attention, quiet_candidate, render, resolve_plan, subject, unanswered_candidate,
)
from app.main import app

TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 11, 30, tzinfo=timezone.utc)


def cand(id, deal, kind="blocker", score=60, owner="RG"):
    return Candidate(id, deal, "3. Proposal", owner, kind, f"label {id}", f"detail {id}", score)


def test_commitment_due_window():
    assert commitment_candidate("C1", "Acme", "Pilot", "RG", "Send forms", TODAY - timedelta(days=2), TODAY).label == \
        "Commitment overdue 2d"
    assert commitment_candidate("C1", "Acme", "Pilot", "RG", "x", TODAY, TODAY).label == "Commitment due today"
    assert commitment_candidate("C1", "Acme", "Pilot", "RG", "x", TODAY + timedelta(days=2), TODAY).score == 80
    assert commitment_candidate("C1", "Acme", "Pilot", "RG", "x", TODAY + timedelta(days=5), TODAY) is None
    assert commitment_candidate("C1", "Acme", "Pilot", "RG", "x", None, TODAY) is None


def test_unanswered_email_window():
    assert unanswered_candidate("C1", "Acme", "Pilot", "RG", "Pricing", NOW - timedelta(hours=5), NOW) is None
    c = unanswered_candidate("C1", "Acme", "Pilot", "RG", "Pricing", NOW - timedelta(days=3), NOW)
    assert c.label == "Customer email unanswered 3d" and c.score == 85
    assert unanswered_candidate("C1", "Acme", "Pilot", "RG", "Pricing", NOW - timedelta(days=30), NOW) is None


def test_quiet_deal_needs_active_stage_and_open_items():
    old = TODAY - timedelta(days=25)
    assert quiet_candidate("C1", "Acme", "2. In Talks", 2, "RG", old, 2, TODAY).label == "Quiet 25d with 2 open item(s)"
    assert quiet_candidate("C1", "Acme", "2. In Talks", 2, "RG", old, 0, TODAY) is None  # inactivity alone isn't risk
    assert quiet_candidate("C1", "Acme", "1. Opening", 1, "RG", old, 2, TODAY) is None
    assert quiet_candidate("C1", "Acme", "2. In Talks", 2, "RG", TODAY - timedelta(days=5), 2, TODAY) is None


def test_attention_one_row_per_deal_skips_action_deals_and_recommendations():
    cands = [cand("C1", "Acme", score=60), cand("C2", "Acme", "unanswered_email", 85), cand("C3", "Globex"),
             cand("C4", "Initech", "recommended", 70), cand("C5", "Hooli")]
    rows = pick_attention(cands, exclude={"Globex"})
    assert [(c.deal, c.id) for c in rows] == [("Acme", "C2"), ("Hooli", "C5")]


def test_resolve_plan_ignores_unknown_and_duplicate_ids():
    cands = [cand("C1", "Acme"), cand("C2", "Globex")]
    plan = FounderPlan(actions=[ChosenAction(candidate_id="C9", do_today="x", why="y"),
                                ChosenAction(candidate_id="C2", do_today="Call", why="Late"),
                                ChosenAction(candidate_id="C2", do_today="Again", why="dup")], preps=[])
    assert [(c.id, a.do_today) for c, a in resolve_plan(plan, cands)] == [("C2", "Call")]


def test_fallback_plan_uses_top_scores():
    plan = fallback_plan([cand("C1", "A", score=50), cand("C2", "B", score=90)], [])
    assert plan.actions[0].candidate_id == "C2"


def test_render_and_subject_are_compact_and_escaped():
    m = Meeting(datetime(2026, 10, 22, 12, 0), "Gus <> Ramiro", "Campari", "4. Pilot", ["RG"])
    sections = [{"name": "Ramiro Godfrid", "short": "Ramiro", "actions": [(cand("C1", "Pernod <Ricard>"), ChosenAction(
        candidate_id="C1", do_today="Sign NDA", why="Blocks data"))], "meetings": [(m, "Ask about offer")],
                 "attention": []}]
    body = render(sections, [], [Change("Campari", "Stage: 3 → 4", "RG")], TODAY)
    assert "Pernod &lt;Ricard&gt;" in body and "Gus &lt;&gt; Ramiro" in body and "Sign NDA" in body
    assert "Nothing else needs attention." in body and "Stage: 3 → 4" in body
    assert subject(sections, TODAY) == "Bianca brief · Thu 22 Oct · Ramiro: 1 actions, 1 meetings"


def test_build_requires_token():
    assert TestClient(app).post("/brief/build").status_code == 401


def test_due_label():
    from app.brief import due_label
    assert due_label(None, TODAY) == "—"
    assert due_label(date(2026, 10, 8), TODAY) == "overdue · 08 Oct"
    assert due_label(TODAY, TODAY) == "today"
    assert due_label(date(2026, 10, 23), TODAY) == "Fri 23"
