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


def test_split_dated_windows():
    from app.brief import Row, split_dated
    rows = [Row("A", "x", TODAY - timedelta(days=31), None, "4. Pilot", "Notes · Sep 21"),   # too old
            Row("B", "x", TODAY - timedelta(days=3), None, "4. Pilot", "Notes"),
            Row("C", "x", TODAY, None, "4. Pilot", "Email"),
            Row("D", "x", TODAY + timedelta(days=2), None, "4. Pilot", "Notes"),
            Row("E", "x", TODAY + timedelta(days=9), None, "4. Pilot", "Notes"),                # beyond a week
            Row("F", "x", None, None, "4. Pilot", "Notes")]
    todo, week = split_dated(rows, TODAY)
    assert [r.company for r in todo] == ["B", "C"] and [r.company for r in week] == ["D"]


def test_due_cell_shows_days_late():
    from app.brief import due_cell
    assert due_cell(None, TODAY) == "—"
    assert due_cell(TODAY, TODAY) == "Oct 22"
    assert "3 days late" in due_cell(TODAY - timedelta(days=3), TODAY)
    assert "1 day late" in due_cell(TODAY - timedelta(days=1), TODAY)


def test_render_layout_a_tables_escaped_with_priorities():
    from app.brief import Row, priority_rows, render
    chosen = [(Candidate("C1", "Pernod <Ricard>", "3. Proposal", "RG", "blocker", "l", "d", 60, link="https://x",
                         source="Deal profile"), ChosenAction(candidate_id="C1", do_today="Sign NDA", why="Blocks data"))]
    m = Meeting(datetime(2026, 10, 22, 12, 0), "Gus <> Ramiro", "Campari", "4. Pilot", ["RG"])
    sections = [{"name": "Ramiro Godfrid", "short": "Ramiro",
                 "today": [Row("FEMSA Arg", "Send next-steps email", TODAY - timedelta(days=2), "NC", "5. Working client",
                               "Notes · Oct 06")] + priority_rows(chosen),
                 "week": [], "meetings": [(m, "Ask about offer")]}]
    body = render(sections, [], [Change("Campari", "Stage: 3 → 4", "RG")], TODAY, 28)
    assert "To do today" in body and "(2)" in body and "Due this week" in body and "Nothing else due" in body
    assert "Pernod &lt;Ricard&gt;" in body and "<b>Priority:</b> Sign NDA" in body and "Blocks data" in body
    assert "2 days late" in body and "Working client" in body and "5. Working" not in body
    assert "Gus &lt;&gt; Ramiro" in body and "Stage: 3 → 4" in body and "28 active deals" in body


def test_subject_counts_per_founder():
    s = [{"short": "Ramiro", "today": [1, 2], "week": [3]}, {"short": "Nico", "today": [], "week": []}]
    assert subject(s, TODAY) == "Bianca pipeline · Thu, Oct 22 · Ramiro 2 today, 1 this week · Nico 0 today, 0 this week"


def test_build_requires_token():
    assert TestClient(app).post("/brief/build").status_code == 401

