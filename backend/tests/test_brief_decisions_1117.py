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
