"""Release review of the 11/17 brief fixes: each test pins a defect found by running the branch on a real database.

Three kinds: data that used to stop the brief (blank text, an odd Sheet cell, an unreadable notice), rows founders
must never get (a nudge on a Paused deal, a held item that is due by its own date), and the off switch each
default-on behaviour now has, which gives the brief from before 11/17 for that behaviour.
"""

import html
from contextlib import nullcontext
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import brief_api, brief_sheet_steps, brief_v2
from app.brief import Meeting
from app.brief_text import TableText, cut_table_text, first_or_blank, split_sentences
from app.brief_v2 import Cadence, DealView, Item, Waiting, _nudge, _split_followups, _waiting_status, build_section
from test_brief_sales_scope import (  # noqa: F401
    NOW, SALES, BriefData, _meeting, brief_client, deal, email, insight,
)

HEADERS = {"X-Ingest-Token": "test-token"}
TODAY = NOW.date()                                   # Nov 5, 2026
PAUSED = {"stage": "0. Paused", "stage_rank": 0}


def build(client):
    response = client.post("/brief/build?dry_run=true&version=v2", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


# ---- text that used to stop or stall the brief

def test_blank_text_gives_a_blank_sentence_instead_of_an_error():
    assert split_sentences("") == [] and first_or_blank("") == "" and first_or_blank(None) == "" and first_or_blank("  \n") == ""
    assert brief_v2.first_sentence("") == "" and brief_v2.first_sentence("   ") == ""       # as before 11/17
    assert cut_table_text(TableText("Situation", " " * 400)) == ""
    assert cut_table_text(TableText("Deal move", "\n" * 400)) == ""


def test_a_blank_insight_or_summary_does_not_stop_the_brief(brief_client):
    """A commitment with only spaces as its text, and a profile summary that is empty, on a deal with a meeting."""
    blank = insight("blank", "Blank Co", "   ", SALES)
    profiles = [{"opportunity_id": "Blank Co-opportunity", "generated_at": NOW, "profile": {
        "refs": {"I1": "blank"}, "summary": {"text": "", "evidence": ["I1"]}}}]
    data = BriefData([deal("Blank Co")], [], [blank], profiles, meetings=[_meeting("Blank call", "Blank Co")])
    result = build(brief_client(data))
    assert [m["title"] for m in result["payload"]["sections"][0]["meetings"]] == ["Blank call"]


def test_sentence_split_reads_a_very_long_text_once():
    text = "Ab. " * 20000                                # 80,000 characters: quadratic work here took 18 seconds
    assert len(split_sentences(text)) == 20000
    long_word = "x" * 60 + ". Next sentence."            # a word longer than the look-back is still not an abbreviation
    assert split_sentences(long_word) == ["x" * 60 + ".", "Next sentence."]
    assert split_sentences("Send it to approx. 40 stores. Done.") == ["Send it to approx. 40 stores.", "Done."]


def test_sheet_cell_with_long_gaps_two_digit_years_and_impossible_dates():
    gap = "- Call Ana (complete   by" + " " * 60000 + "x"                 # used to backtrack for over a minute
    assert brief_sheet_steps.parse_cell(gap, TODAY) == [] and brief_sheet_steps.match_key(gap)
    [spaced] = brief_sheet_steps.parse_cell("-   Call   Ana   ( complete   by   Nov 16,   2026 )", TODAY)
    assert (spaced.text, spaced.due) == ("Call Ana", date(2026, 11, 16))
    assert brief_sheet_steps.parse_date("Nov 16, 26", TODAY) == date(2026, 11, 16)      # not the year 26
    for unreadable in ("Feb 30, 2026", "Nov 9, 20260", "13/13/2026", "soon", "0 Nov 2026", ""):
        assert brief_sheet_steps.parse_date(unreadable, TODAY) is None
    assert len(brief_sheet_steps.first_bullet("- " + "palabra " * 5000)) == brief_sheet_steps.MAX_CONTEXT
    assert brief_sheet_steps.first_bullet(None) == "" and brief_sheet_steps.parse_cell(None, TODAY) == []


def test_a_sheet_date_from_another_year_is_shown_with_its_year(brief_client):
    data = BriefData([{**deal("Old Date"), "next_step": "- Send the case study (complete by Nov 5, 2019)"}], [], [], [])
    body = html.unescape(build(brief_client(data))["html"])
    assert "Sheet dates long past:</b> Old Date (Nov 5, 2019)" in body


# ---- a Paused deal is parked on purpose: no reminder of any kind

def test_nudge_rules_for_paused_deals_and_undated_sources():
    paused = DealView("Parked", "0. Paused", 0, "RG", TODAY - timedelta(days=30), last_outbound=TODAY - timedelta(days=21),
                      unanswered_emails=7)
    assert _nudge(paused, TODAY, Cadence(), "Keep following up", TODAY - timedelta(days=21), "your last email") is None
    talks = DealView("Talks", "2. In Talks", 2, "RG", None)
    assert _nudge(talks, TODAY, Cadence(), "Check in", date.min, "this was noted") is None  # never "105,000 weeks since"
    assert _nudge(talks, TODAY, Cadence(), "Check in", TODAY - timedelta(days=14), "this was noted")[0] == (
        "Talks: 2 weeks since this was noted - follow up this week.")
    wait = Waiting("Parked", "0. Paused", "Ana", TODAY - timedelta(days=14), 14, 6, False, False, None, "", nudge=False,
                   pause_after=None)
    assert _waiting_status(wait) == "no reply to our 6 emails"


# ---- the hold-back rule never hides what is due by its own date

def test_split_followups_without_a_cadence_is_the_function_from_before_1117():
    note = Item("i1", "Deal", "Follow up with Ana", None, TODAY - timedelta(days=3), None, "RG", "deal_owner", None,
                "Notes · Nov 2", None, "undated", "next_step")
    wait = Item("i2", "Deal", "Wait for Ana's reply", None, TODAY - timedelta(days=3), None, "RG", "deal_owner", None,
                "Notes · Nov 2", None, "undated", "next_step")
    view = DealView("Deal", "2. In Talks", 2, "RG", TODAY - timedelta(days=3), items=[note, wait])
    kept, followups, pending = _split_followups(view, TODAY, set(), None)
    assert [(m.tag, m.text, m.when) for m in followups] == [("Follow up", "Deal: Follow up with Ana", "recorded Nov 2")]
    assert [i.id for i in kept.items] == ["i2"] and pending is True
    kept, followups, pending = _split_followups(view, TODAY, set(), Cadence())            # the 11/17 behaviour
    assert kept.items == [] and followups == [] and pending is True                       # day 3: no nudge due yet


# ---- a deal with a meeting today is not "quiet"

def test_quiet_batch_skips_a_deal_that_has_a_meeting_today_or_tomorrow():
    views = {name: DealView(name, "2. In Talks", 2, "RG", None) for name in ("Meeting Co", "Quiet Co")}
    meeting = Meeting(NOW, "Intro call", "Meeting Co", "2. In Talks", ["RG"])
    section = build_section("RG", "Ramiro Godfrid", "Ramiro", views, [], [(meeting, "")], TODAY, {"RG": "Ramiro"},
                            resurface=3)
    assert [v.deal for v in section.resurfaced] == ["Quiet Co"] and section.quiet_in_talks == 1


# ---- the 11/17 additions are extras: when one fails, the brief is sent without it

def _fixture_for_isolation():
    ask = email("w-ask", "Weekly", "Can you approve the proposal?", days=14, outgoing=True)
    deals = [{**deal("Dated"), "next_step": "- Email Franco the revised deck (complete by Nov 5, 2026)"},
             deal("Weekly"), deal("Promise"), {**deal("Guest Co"), "company_id": "guest"}]
    insights = [insight("promise", "Promise", "Send the signed order form", SALES)]
    guest = {"address": "ana@guestco.example", "name": "Ana", "response": "accepted"}
    return BriefData(deals, [ask], insights, [], meetings=[_meeting("Call with Ana", None, [guest])])


def _boom(*_, **__):
    raise RuntimeError("injected failure")


def test_unreadable_archive_drops_the_quiet_batch_and_keeps_the_brief(monkeypatch, brief_client, caplog):
    class BrokenArchive(BriefData):
        def execute(self, query, params=()):
            if "from briefs" in query and ("resurfaced" in query or "for_date" in query):
                raise RuntimeError("archive unreadable")
            return super().execute(query, params)

    ask = email("w-ask", "Weekly", "Can you approve the proposal?", days=14, outgoing=True)
    data = BrokenArchive([deal("Weekly"), deal("Quiet")], [ask], [], [])
    data.last_contact = {"Quiet": 30}
    with caplog.at_level("ERROR", logger="bianca_cro.brief"):
        result = build(brief_client(data))
    assert "Quiet In Talks deals to revisit" not in result["html"]                # no cooldown known: no batch
    assert "• Weekly: To Buyer · no reply yet, 2 weeks since your last email - follow up this week" in result["text"]
    assert {r.getMessage() for r in caplog.records} == {
        "brief built without the days a brief was sent (a weekly nudge may repeat)",
        "brief built without the quiet-deal batch (earlier batches unreadable)"}


def test_one_unreadable_cancellation_notice_does_not_fail_the_mail_sync_or_the_other_notices(monkeypatch):
    from app import ingest
    from app.main import app
    from test_gmail import msg

    class Connection:
        rowcount = 1

        def execute(self, sql, params=()):
            if sql.startswith("update source_records r set meta") and params[2] == "Bad time":
                raise ValueError("invalid input syntax for type timestamp with time zone")
            return self

        def fetchone(self):
            return (["bianca-ai.com"],)

        def commit(self):
            pass

        def transaction(self):
            return nullcontext()

    monkeypatch.setattr(ingest.db, "connect", lambda: nullcontext(Connection()))
    monkeypatch.setattr(ingest, "_workspace_id", lambda conn: "workspace")
    monkeypatch.setattr(ingest, "_start_sync", lambda *args, **kwargs: "run")
    bad = msg(id="n1", subject="Canceled event: Bad time @ Wed Nov 11, 2026 3pm - 3:30pm (ART) (r@bianca-ai.com)")
    good = msg(id="n2", subject="Canceled event: Bianca <> Campari @ Wed Nov 11, 2026 3pm - 3:30pm (ART) (r@bianca-ai.com)")
    response = TestClient(app).post("/ingest/gmail", json={"messages": [bad, good, msg(id="m3")], "mailbox": "RG"},
                                    headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["meetings_cancelled"] == 1 and response.json()["created"] == 1

    monkeypatch.setattr(ingest.gmail, "cancellation_notices", _boom)                # even the reader itself failing
    response = TestClient(app).post("/ingest/gmail", json={"messages": [good, msg(id="m4")], "mailbox": "RG"},
                                    headers=HEADERS)
    assert response.status_code == 200 and response.json()["meetings_cancelled"] == 0
