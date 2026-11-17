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
