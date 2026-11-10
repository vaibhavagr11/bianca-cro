from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import brief, brief_api, brief_review, brief_v2
from app.brief_delivery import VerificationPending
from app.brief_review import ActionSource
from app.brief_verification import Verdict
from app.main import app

NOW = datetime(2026, 11, 5, 15, tzinfo=timezone.utc)
COMPANIES = {"Acme": "acme"}


def source(kind, key):
    return ActionSource(kind, key, NOW)


def test_v1_verifies_dated_priority_and_shared_rows_before_rendering():
    due = brief.Row("Acme", "Send contract", NOW.date(), None, "Pilot", "Email", verification_ref="insight:one")
    priority = brief.Row("Acme", "Review pricing", None, None, "Pilot", "Email", verification_ref="thread:one")
    ndl = [brief.Row("Acme", "Send onboarding link", None, None, "Pilot", "Email", verification_ref="insight:two")]
    sections = [{"name": "Ramiro", "short": "Ramiro", "today": [due], "week": [priority], "meetings": []}]
    sources = {"insight:one": source("commitment", "msg1"), "thread:one": source("reply", "msg2"),
               "insight:two": source("commitment", "msg3")}
    states = {"msg1": "handled", "msg2": "waiting", "msg3": "unclear"}

    def gate(actions):
        assert {a.origin_record_id for a in actions} == set(states)
        return {a.key: Verdict(states[a.origin_record_id], "fixture") for a in actions}

    ndl_review = brief_review.verify_v1(sections, ndl, COMPANIES, sources, gate)
    html = brief.render(sections, ndl, [], NOW.date(), 1, ndl_review)
    assert "Send contract" not in html
    assert not sections[0]["today"] and not sections[0]["week"] and not ndl
    assert html.index("Waiting on the customer") < html.index("Review pricing")
    assert html.index("Needs verification") < html.index("Send onboarding link")


def test_v2_checks_every_action_section_and_only_keeps_verified_done_links():
    due = brief_v2.TodayRow("Acme", "Pilot", "Due now", "Send quote", "today", "RG", None, done="insight:due")
    reply = brief_v2.TodayRow("Acme", "Pilot", "Reply needed", "Reply to the customer", "today", "RG", None,
                              done="thread:reply")
    recommendation = brief_v2.Move("AI recommendation", "Offer a demo", "soon", None, done="rec:one")
    followup = brief_v2.Move("We owe", "Acme: send documents", "soon", None, done="insight:follow", deal="Acme")
    backlog = brief_v2.Item("old", "Acme", "Share report", NOW.date(), NOW.date(), "RG")
    waiting = brief_v2.Waiting("Acme", "Pilot", "Ana", NOW.date(), 4, 1, False, False, None, thread_id="sent")
    deal = brief_v2.DealView("Acme", "Pilot", 3, "RG")
    meeting = brief.Meeting(NOW, "Acme meeting", "Acme", "Pilot", ["RG"])
    section = brief_v2.Section("RG", "Ramiro", "Ramiro", [due, reply], [(deal, recommendation, None)], [],
                               [(meeting, "Send a prep deck")], [followup], [], 2, [deal], 0,
                               brief_v2.Backlog(1, 1, NOW.date(), exceptions=[backlog]), waiting=[waiting])
    ndl = [brief_v2.TodayRow("Acme", "Pilot", "Due now", "Send link", "today", "NDL", None, done="insight:ndl")]
    sources = {"insight:due": source("commitment", "m1"), "thread:reply": source("reply", "m2"),
               "rec:one": source("recommendation", "m3"), "insight:follow": source("commitment", "m4"),
               "insight:old": source("commitment", "m5"), "wait:sent": source("waiting", "m6"),
               "insight:ndl": source("commitment", "m7")}
    states = {"m1": "handled", "m2": "waiting", "m3": "unclear", "m4": "still_owed", "m5": "handled",
              "m6": "waiting", "m7": "still_owed", None: "unclear"}
    checked = []

    def gate(actions):
        checked.extend(actions)
        return {a.key: Verdict(states[a.origin_record_id], "fixture") for a in actions}

    ndl_review = brief_review.verify_v2([section], ndl, COMPANIES, sources, gate)
    assert len(checked) == 9   # master removed the Customer accounts section
    assert section.today == section.highlights == section.unclear == []
    assert section.meetings == [(meeting, "")]
    assert section.backlog.exceptions == []
    assert set(brief_v2.completable([section], ndl)) == {"insight:follow", "thread:sent", "insight:ndl"}
    html = brief_v2.render([section], ndl, [], NOW.date(), 1, "sources", False, ndl_review=ndl_review)
    text = brief_v2.plain_text([section], NOW.date(), ndl_review)
    assert "Send quote" not in html and "Share report" not in html
    assert html.index("Needs verification") < html.index("Offer a demo")
    assert "Waiting on the customer" in html and "Reply to the customer" in text
    assert "Send a prep deck" in text and "Acme meeting — Send a prep deck" not in text


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_pending_public_build_returns_no_sendable_body_for_both_versions(monkeypatch, version):
    sync_run_id, build_id = uuid4(), uuid4()

    def create(receipts, requested_id, requested_version, dry_run):
        assert receipts == [sync_run_id]
        assert requested_id == build_id and requested_version == version and not dry_run
        return "fake gate"

    def builder(dry_run, *, gate):
        assert gate == "fake gate" and not dry_run
        raise VerificationPending({"ready_to_send": False, "verification_status": "pending"})

    monkeypatch.setattr(brief_api, "create_gate", create)
    monkeypatch.setattr(brief_api, f"build_brief_{version}", builder)
    response = TestClient(app).post(f"/brief/build?version={version}&verification=required&build_id={build_id}",
                                    headers={"X-Ingest-Token": "test-token"},
                                    json={"sync_run_ids": [str(sync_run_id)]})
    assert response.status_code == 200
    assert response.json() == {"ready_to_send": False, "verification_status": "pending"}
