"""Release owner's decisions of 11/17 on the review's open items. Ramiro's biggest fear is missing an email or a
deadline, so nothing that needs a founder today may be hidden: "Needs you today" is never cut, every capped table
names what it left out, and the plain-text part says what the HTML says.
"""

import html
from contextlib import nullcontext
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import brief_api
from app.brief_v2 import TodayRow, left_out_names, more_line, sort_today
from test_brief_sales_scope import (  # noqa: F401
    NOW, SALES, BriefData, _meeting, brief_client, deal, email, insight,
)

HEADERS = {"X-Ingest-Token": "test-token"}
TODAY = NOW.date()                                   # Nov 5, 2026


def build(client):
    response = client.post("/brief/build?dry_run=true&version=v2", headers=HEADERS)
    assert response.status_code == 200
    return response.json()


# ---- 1. Needs you today shows every row

@pytest.mark.parametrize("count", [1, 7, 8, 25])
@pytest.mark.parametrize("max_rows", [7, 2])
def test_a_deal_with_something_due_today_is_always_named_in_the_email(monkeypatch, brief_client, count, max_rows):
    monkeypatch.setattr(brief_api.settings, "brief_max_table_rows", max_rows)
    names = [f"Promise Co {n:02d}" for n in range(1, count + 1)]
    insights = [insight(f"promise-{n}", name, f"Send the contract to {name}", SALES) for n, name in enumerate(names)]
    ask = email("reply-ask", "Asker", "Can you send the price list this week?")
    insights.append(insight("ask", "Asker", "Send the price list", SALES, type_="next_step", origin=ask,
                            quote=ask["body"], dated=False))
    late = {**deal("Late Sheet"), "next_step": "- Call Ana about pricing (complete by Nov 2, 2026)"}
    data = BriefData([deal(name) for name in names] + [deal("Asker"), late], [ask], insights, [])
    result = build(brief_client(data))
    page, text = html.unescape(result["html"]), result["text"]
    section = result["payload"]["sections"][0]
    assert len(section["today"]) == count + 2 and section["more_today"] == 0
    assert f"Needs you today ({count + 2})" in page and f"Ramiro {count + 2} today" in result["subject"]
    for name in (*names, "Asker", "Late Sheet"):
        assert f"<b>{name}</b>" in result["html"] and f"] {name} — " in text
    assert "more need you today" not in page + text
    # The customer waiting on a reply reads first, the late Sheet date last.
    assert section["today"][0]["tag"] == "Reply needed" and section["today"][-1]["deal"] == "Late Sheet"


def test_needs_you_today_order_is_replies_and_due_today_then_overdue_then_late_sheet_dates():
    def row(deal, tag, late):
        return TodayRow(deal, "2. In Talks", tag, "x", "", "RG", None, late=late)
    rows = [row("sheet 5d late", "Sheet date", 5), row("due 3d late", "Due now", 3), row("due tomorrow", "Due now", -1),
            row("sheet 1d late", "Sheet date", 1), row("due today", "Due now", 0), row("due 1d late", "Due now", 1),
            row("reply", "Reply needed", 0), row("sheet today", "Sheet date", 0)]
    assert [r.deal for r in sort_today(rows)] == [
        "reply", "due today", "sheet today", "due tomorrow",       # cannot wait
        "due 1d late", "due 3d late",                              # overdue by its own date, most recent first
        "sheet 1d late", "sheet 5d late"]                          # late Sheet dates


# ---- 2. every capped table names the deals it left out

def test_left_out_names_lists_eight_deals_then_counts_the_rest():
    assert left_out_names(["Andina Chile", "Arca Peru", "Chifles", "Arca Peru", None, ""]) == "Andina Chile, Arca Peru, Chifles"
    many = [f"Deal {n}" for n in range(1, 12)]
    assert left_out_names(many) == "Deal 1, Deal 2, Deal 3, Deal 4, Deal 5, Deal 6, Deal 7, Deal 8 and 3 more"
    assert more_line(3, "in the Sheet", ["Andina Chile", "Arca Peru", "Chifles"]) == (
        "+ 3 more in the Sheet: Andina Chile, Arca Peru, Chifles")
    assert more_line(2, "on record", []) == "+ 2 more on record"


def test_every_capped_table_names_what_it_left_out_in_html_and_text(brief_client):
    names = [f"Deal {n:02d}" for n in range(1, 18)]
    waits = [email(f"ask-{name}", name, "Can you approve the proposal?", days=8, outgoing=True) for name in names[:9]]
    steps = [insight(f"step-{name}", name, f"Prepare the rollout plan for {name}", SALES, type_="next_step", dated=False)
             for name in names[9:]]
    replies = [email(f"new-{name}", name, "Thanks, received.", days=0) for name in names[:9]]
    meetings = [_meeting(f"Call {n}", name, hours=n) for n, name in enumerate(names[:9], start=1)]
    data = BriefData([deal(name) for name in names], [*waits, *replies], steps, [], meetings=meetings)
    result = build(brief_client(data))
    page, text = html.unescape(result["html"]), result["text"]
    section = result["payload"]["sections"][0]
    assert len(section["highlights"]) <= 7 and len(section["meetings"]) == 7 and len(result["payload"]["changes"]) == 7
    for body in (page, text):
        assert "+ 2 more on your calendar: Deal 08, Deal 09" in body
        assert f"+ {len(section['also_active'])} more active in the Sheet: " in body
        if section["more_followups"]:
            assert f"+ {section['more_followups']} more on record: Deal " in body
    shown = {c["deal"] for c in result["payload"]["changes"]}
    [left] = [l for l in page.split("<") if "more changes on record, not shown: " in l]
    assert set(left.split(": ")[1].strip().split(", ")) == set(names[:9]) - shown and left.split(">")[1].startswith("+ 2 more")
    left = [l for l in text.splitlines() if l.startswith("+ ")]
    assert all(": " in line for line in left)                     # no bare count anywhere


# ---- 5. the plain-text part carries the Sheet-date footers and the quiet-batch note

def test_plain_text_carries_the_sheet_date_footers_and_the_quiet_batch_note(brief_client):
    deals = [{**deal("Long Past"), "next_step": "- Send the case study (complete by Oct 1, 2026)"},
             {**deal("Months Out"), "next_step": "- Share the plan (complete by Feb 17, 2027)"}, deal("Quiet")]
    insights = [insight("plan", "Months Out", "Share the plan", SALES, type_="next_step", dated=False)]
    data = BriefData(deals, [], insights, [])
    data.last_contact = {"Quiet": 30}
    result = build(brief_client(data))
    page, text = html.unescape(result["html"]), result["text"]
    stale = "Sheet dates long past: Long Past (Oct 1) - update the Next step in the Sheet or move the deal to Paused."
    later = ("1 dated Sheet step(s) not due yet (1 matching item(s) held back until then); next: Months Out, "
             "Feb 17, 2027. Each comes back in Needs you today on its date.")
    note = "A few each day, longest-untouched first. For each: email them, set a date in the Sheet's Next step, or move"
    flat = " ".join(html.unescape(__import__("re").sub(r"<[^>]+>", "", result["html"])).split())
    for line in (stale, later, note):
        assert line in text and line in flat
    assert "Quiet In Talks deals to revisit (1 of 1)" in text and "Quiet In Talks deals to revisit (1 of 1)" in page


# ---- 4. a cancellation notice that fits two stored meetings cancels neither

@pytest.mark.parametrize("matches, cancelled", [(1, 1), (2, 0), (0, 0)])
def test_notice_matching_more_than_one_meeting_cancels_none_and_is_logged(monkeypatch, caplog, matches, cancelled):
    from datetime import date

    from app import ingest

    updates = []

    class Connection:
        def execute(self, sql, params=()):
            self.counting = sql.startswith("select count(*) from source_records r")
            if self.counting:
                assert "lower(btrim(r.subject)) = lower(%s)" in sql and "is distinct from 'cancelled'" in sql
                assert sql.count("%s") == len(params) and params[1] == "30 Minute Call (Ramiro Godfrid)"
            else:
                assert sql.count("%s") == len(params)
                updates.append(params)
            return self

        @property
        def rowcount(self):
            return matches

        def fetchone(self):
            return (matches,)

        def transaction(self):
            return nullcontext()

    notice = ("30 Minute Call (Ramiro Godfrid)", date(2026, 11, 11), "2026-11-11T12:40:00.000Z")
    with caplog.at_level("WARNING", logger="bianca_cro.ingest"):
        assert ingest.apply_cancellation_notices(Connection(), "workspace", [notice]) == cancelled
    assert len(updates) == (0 if matches > 1 else 1)
    logged = [r.getMessage() for r in caplog.records]
    assert (logged == ["cancellation notice for 2026-11-11 matches 2 meetings with the same title; none cancelled,"
                       " waiting for the next calendar fetch"]) == (matches > 1)
