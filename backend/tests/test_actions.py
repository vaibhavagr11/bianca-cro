"""Mark as complete: signed links, the confirm page, and what a click changes."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import actions
from app.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(settings, "brief_action_secret", SecretStr("test-secret"))


def test_token_round_trip_carries_what_the_page_shows():
    a = actions.verify(actions.sign("insight:42", "Campari", "Add On Premise data"))
    assert a == {"kind": "insight", "ref": "42", "deal": "Campari", "text": "Add On Premise data"}


@pytest.mark.parametrize("mangle", [lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"), lambda t: "x" + t, lambda t: "nope"])
def test_forged_or_edited_tokens_are_rejected(mangle):
    with pytest.raises(HTTPException) as e:
        actions.verify(mangle(actions.sign("insight:42", "Campari", "x")))
    assert e.value.status_code == 400


def test_expired_token_is_rejected():
    t = actions.sign("thread:t1", "Arca", "Pricing?", now=0)
    with pytest.raises(HTTPException) as e:
        actions.verify(t, now=actions.LINK_DAYS * 86400 + 1)
    assert e.value.status_code == 410


def test_unknown_kind_is_rejected():
    with pytest.raises(HTTPException):
        actions.verify(actions.sign("deal:1", "X", "y"))


def test_opening_the_link_changes_nothing(monkeypatch):
    monkeypatch.setattr(actions.db, "connect", lambda: pytest.fail("GET must not touch the database"))
    r = client.get("/actions/complete", params={"t": actions.sign("insight:42", "Campari", "Add data")})
    assert r.status_code == 200 and "Mark as complete?" in r.text and "Campari" in r.text and 'method="post"' in r.text


class FakeConn:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        return self


def test_confirm_records_who_and_closes_the_insight(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(actions.db, "connect", lambda: conn)
    monkeypatch.setattr(actions, "_workspace_id", lambda c: "ws")
    t = actions.sign("insight:42", "Campari", "Add data")
    r = client.post("/actions/complete", data={"t": t, "who": "RG"})
    assert r.status_code == 200 and "Marked complete" in r.text and "Undo" in r.text
    assert conn.sql[0][0].startswith("insert into brief_actions") and conn.sql[0][1] == ("ws", "insight", "42", "done", "RG")
    assert "set status = 'done'" in conn.sql[1][0]


def test_undo_reopens_only_what_the_brief_closed(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(actions.db, "connect", lambda: conn)
    monkeypatch.setattr(actions, "_workspace_id", lambda c: "ws")
    r = client.post("/actions/undo", data={"t": actions.sign("insight:42", "Campari", "x"), "who": "NC"})
    assert r.status_code == 200 and conn.sql[0][1][3] == "undone"
    assert "completion->>'via' = 'brief'" in conn.sql[1][0]


def test_thread_click_is_recorded_without_touching_insights(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(actions.db, "connect", lambda: conn)
    monkeypatch.setattr(actions, "_workspace_id", lambda c: "ws")
    client.post("/actions/complete", data={"t": actions.sign("thread:t1", "Arca", "Pricing?"), "who": "RG"})
    assert len(conn.sql) == 1 and conn.sql[0][1][1] == "thread"


def test_who_is_required(monkeypatch):
    r = client.post("/actions/complete", data={"t": actions.sign("insight:42", "C", "x"), "who": "someone"})
    assert r.status_code == 400


def test_without_a_secret_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(settings, "brief_action_secret", None)
    r = client.get("/actions/complete", params={"t": "a.b"})
    assert r.status_code == 503
