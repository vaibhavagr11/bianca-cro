"""Transport/auth tests; no production database, Claude, or Google calls."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import review_api, review_service
from app.config import settings
from app.main import app

client = TestClient(app)
TOKEN = "test-reviewer-token-" + "x" * 32
HEADERS = {"Authorization": "Bearer " + TOKEN}
KEY = "request-key-00000001"


@pytest.fixture(autouse=True)
def review_config(monkeypatch):
    monkeypatch.setattr(settings, "review_enabled", True)
    monkeypatch.setattr(settings, "review_tokens", {"NDL": SecretStr(TOKEN)})


def rpc(method, params=None, **kw):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/review/mcp", json=body, headers={**HEADERS, **kw})


def test_feature_disabled_by_default_configuration_and_flag(monkeypatch):
    monkeypatch.setattr(settings, "review_enabled", False)
    assert client.get("/review/app").status_code == 404
    assert client.get("/review/workspace", headers=HEADERS).status_code == 404


def test_separate_reviewer_auth_is_required():
    assert client.get("/review/workspace").status_code == 401
    assert client.get("/review/workspace", headers={"X-Ingest-Token": "test-token"}).status_code == 401
    assert client.get("/review/workspace", headers={"Authorization": "Bearer test-token"}).status_code == 401


def test_duplicate_or_invalid_actor_credentials_fail_closed(monkeypatch):
    for tokens in ({"RG": SecretStr(TOKEN), "NC": SecretStr(TOKEN)}, {"other": SecretStr(TOKEN)}, {"RG": SecretStr("short")}):
        monkeypatch.setattr(settings, "review_tokens", tokens)
        assert client.get("/review/workspace", headers=HEADERS).status_code == 503


@pytest.mark.parametrize("actor", ["RG", "NC", "NDL", "MG"])
def test_workspace_uses_authenticated_identity(monkeypatch, actor):
    monkeypatch.setattr(settings, "review_tokens", {actor: SecretStr(TOKEN)})
    monkeypatch.setattr(review_api, "_workspace", lambda: {"proposals": [], "rows": [], "activity": []})
    result = client.get("/review/workspace", headers=HEADERS)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["viewer"] == actor


def test_matias_credentials_preserve_existing_reviewers_and_attribute_his_decisions(monkeypatch):
    tokens = {actor: SecretStr(actor + "-" + TOKEN) for actor in ("RG", "NC", "NDL", "MG")}
    monkeypatch.setattr(settings, "review_tokens", tokens)
    monkeypatch.setattr(review_api, "_workspace", lambda: {"proposals": [], "rows": [], "activity": []})
    for actor, token in tokens.items():
        response = client.get("/review/workspace", headers={"Authorization": "Bearer " + token.get_secret_value()})
        assert response.status_code == 200 and response.json()["viewer"] == actor
    calls = []
    monkeypatch.setattr(review_api, "_decision", lambda proposal_id, actor, action, payload:
                        calls.append((actor, action)) or {"status": "ignored"})
    response = client.post(f"/review/proposals/{uuid4()}/ignore",
                           json={"revision": 1, "idempotency_key": KEY},
                           headers={"Authorization": "Bearer " + tokens["MG"].get_secret_value()})
    assert response.status_code == 200 and calls == [("MG", "ignore")]


def test_workspace_live_status_failure_returns_no_workspace_data(monkeypatch):
    @contextmanager
    def connect():
        yield type("Connection", (), {})()

    def status_unavailable(*_args):
        raise review_service.ReviewUnavailable("Current sales status could not be confirmed from the Sheet.")

    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda _conn: "workspace")
    monkeypatch.setattr(review_service, "workspace_view", status_unavailable)
    rest = client.get("/review/workspace", headers=HEADERS)
    assert rest.status_code == 503 and rest.headers["cache-control"] == "no-store"
    assert set(rest.json()) == {"detail"}
    tool = rpc("tools/call", {"name": "bianca_review_workspace", "arguments": {}}).json()["result"]
    assert tool["isError"] is True
    assert tool["structuredContent"] == {"status": 503, "detail": rest.json()["detail"]}


@pytest.mark.parametrize("action", ["approve", "ignore"])
def test_stale_working_client_decisions_return_conflict_in_rest_and_mcp(monkeypatch, action):
    @contextmanager
    def connect():
        yield None

    def excluded(*_args):
        raise review_service.ReviewConflict(review_service.OUTSIDE_SALES_PIPELINE)

    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda _conn: "workspace")
    monkeypatch.setattr(review_service, "decide", excluded)
    proposal_id = str(uuid4())
    body = {"revision": 1, "idempotency_key": KEY}
    if action == "approve":
        body.update(next_step_bullets=["Confirm the pilot date."], client_context_bullets=[])
    rest = client.post(f"/review/proposals/{proposal_id}/{action}", json=body, headers=HEADERS)
    assert rest.status_code == 409 and rest.json() == {"detail": review_service.OUTSIDE_SALES_PIPELINE}
    tool = rpc("tools/call", {"name": f"bianca_{action}_update", "arguments": {"proposal_id": proposal_id, **body}}).json()["result"]
    assert tool["isError"] is True
    assert tool["structuredContent"] == {"status": 409, "detail": rest.json()["detail"]}


def test_untrusted_origin_is_denied_before_read(monkeypatch):
    monkeypatch.setattr(review_api, "_workspace", lambda: pytest.fail("must not read"))
    assert client.get("/review/workspace", headers={**HEADERS, "Origin": "https://untrusted.example"}).status_code == 403


def test_mcp_initialization_and_notification():
    r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"
    assert r.json()["result"]["capabilities"] == {"tools": {}}
    r = client.post("/review/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=HEADERS)
    assert r.status_code == 202 and not r.content
    assert client.get("/review/mcp", headers=HEADERS).status_code == 405


def test_mcp_tools_are_narrow_and_include_review_version():
    tools = rpc("tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"bianca_review_workspace", "bianca_approve_update", "bianca_ignore_update",
                                        "bianca_set_status", "bianca_daily_brief"}
    brief = next(t for t in tools if t["name"] == "bianca_daily_brief")
    assert brief["annotations"]["readOnlyHint"] is True
    assert brief["inputSchema"]["additionalProperties"] is False
    assert set(brief["inputSchema"]["properties"]) == {"brief_id"}
    status = next(t for t in tools if t["name"] == "bianca_set_status")
    assert set(status["inputSchema"]["required"]) == {"row_id", "expected_status", "status"}
    assert "actor" not in status["inputSchema"]["properties"]
    approval = next(t for t in tools if t["name"] == "bianca_approve_update")
    assert set(approval["inputSchema"]["required"]) == {
        "proposal_id", "revision", "idempotency_key", "next_step_bullets", "client_context_bullets"}
    assert "actor" not in approval["inputSchema"]["properties"]


def test_rest_approval_and_mcp_share_authenticated_decision_handler(monkeypatch):
    calls = []
    def decision(proposal_id, actor, action, payload):
        calls.append((proposal_id, actor, action, payload.next_step_bullets, payload.client_context_bullets))
        return {"status": "approved", "proposalId": proposal_id}
    monkeypatch.setattr(review_api, "_decision", decision)
    proposal_id = str(uuid4())
    body = {"revision": 2, "idempotency_key": KEY, "next_step_bullets": ["Confirm the pilot date."],
            "client_context_bullets": ["Customer need: Expand retailer coverage."]}
    assert client.post(f"/review/proposals/{proposal_id}/approve", json=body, headers=HEADERS).status_code == 200
    r = rpc("tools/call", {"name": "bianca_approve_update", "arguments": {"proposal_id": proposal_id, **body}})
    assert r.json()["result"]["isError"] is False
    assert calls == [(proposal_id, "NDL", "approve", ["Confirm the pilot date."],
                      ["Customer need: Expand retailer coverage."])] * 2


def test_client_cannot_spoof_actor_or_send_empty_approval(monkeypatch):
    monkeypatch.setattr(review_api, "_decision", lambda *a: pytest.fail("must not decide"))
    proposal_id = uuid4()
    body = {"revision": 1, "idempotency_key": KEY, "next_step_bullets": ["A valid item."],
            "client_context_bullets": []}
    assert client.post(f"/review/proposals/{proposal_id}/approve", json={**body, "actor": "RG"}, headers=HEADERS).status_code == 422
    assert client.post(f"/review/proposals/{proposal_id}/approve", json={**body, "next_step_bullets": ["  "]}, headers=HEADERS).status_code == 422
    assert client.post(f"/review/proposals/{proposal_id}/approve", json={**body, "revision": 0}, headers=HEADERS).status_code == 422


def test_legacy_flat_bullets_and_missing_destination_are_rejected(monkeypatch):
    monkeypatch.setattr(review_api, "_decision", lambda *a: pytest.fail("must not decide"))
    route = f"/review/proposals/{uuid4()}/approve"
    legacy = {"revision": 1, "idempotency_key": KEY, "bullets": ["Customer need: More coverage."]}
    assert client.post(route, json=legacy, headers=HEADERS).status_code == 422
    partial = {"revision": 1, "idempotency_key": KEY, "next_step_bullets": ["Send scope."]}
    assert client.post(route, json=partial, headers=HEADERS).status_code == 422
    assert client.post(route, json={**partial, "next_step_bullets": [], "client_context_bullets": []},
                       headers=HEADERS).status_code == 422


@pytest.mark.parametrize("actions,context", [(["Send scope."], []), ([], ["Customer need: More coverage."])])
def test_single_destination_approval_is_valid_and_forwarded(monkeypatch, actions, context):
    calls = []
    monkeypatch.setattr(review_api, "_workspace_id", lambda conn: "workspace")
    @contextmanager
    def connect():
        yield object()
    monkeypatch.setattr(review_api.db, "connect", connect)
    def decide(*args):
        calls.append(args)
        return {"status": "approved"}
    monkeypatch.setattr(review_service, "decide", decide)
    proposal_id = str(uuid4())
    body = {"revision": 2, "idempotency_key": KEY, "next_step_bullets": actions,
            "client_context_bullets": context}
    assert client.post(f"/review/proposals/{proposal_id}/approve", json=body, headers=HEADERS).status_code == 200
    assert calls == [("workspace", proposal_id, "NDL", "approve", 2, KEY, actions, context)]


def test_both_destinations_have_independent_text_limits(monkeypatch):
    monkeypatch.setattr(review_api, "_decision", lambda *a: pytest.fail("must not decide"))
    route = f"/review/proposals/{uuid4()}/approve"
    body = {"revision": 1, "idempotency_key": KEY, "next_step_bullets": [], "client_context_bullets": []}
    for field in ("next_step_bullets", "client_context_bullets"):
        assert client.post(route, json={**body, field: ["x" * 4001]}, headers=HEADERS).status_code == 422
        assert client.post(route, json={**body, field: ["x" * 4000] * 12}, headers=HEADERS).status_code == 422


def test_mcp_conflict_is_a_tool_error_not_false_success(monkeypatch):
    from fastapi import HTTPException
    def conflict(*a):
        raise HTTPException(409, "Newer card; reload")
    monkeypatch.setattr(review_api, "_tool_call", conflict)
    r = rpc("tools/call", {"name": "bianca_ignore_update", "arguments": {}})
    result = r.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == {"status": 409, "detail": "Newer card; reload"}


def test_mcp_malformed_unknown_or_invalid_requests_do_not_execute(monkeypatch):
    monkeypatch.setattr(review_api, "_tool_call", lambda *a: pytest.fail("must not execute"))
    assert rpc("missing").json()["error"]["code"] == -32601
    assert rpc("tools/call", {"name": "not_a_tool", "arguments": {}}).json()["error"]["code"] == -32602
    assert client.post("/review/mcp", content="{", headers=HEADERS).json()["error"]["code"] == -32700
    assert client.post("/review/mcp", json=[], headers=HEADERS).json()["error"]["code"] == -32600


def test_mcp_workspace_response_and_body_size(monkeypatch):
    monkeypatch.setattr(review_api, "_workspace", lambda: {"proposals": [], "rows": [], "activity": []})
    r = rpc("tools/call", {"name": "bianca_review_workspace", "arguments": {}})
    assert r.json()["result"]["structuredContent"]["viewer"] == "NDL"
    assert client.post("/review/mcp", content=" " * 1_000_001, headers=HEADERS).status_code == 413
    assert rpc("ping", **{"MCP-Protocol-Version": "invalid"}).status_code == 400


def test_shell_contains_live_ui_and_demo_is_separate():
    live = client.get("/review/app")
    assert live.status_code == 200
    assert "Sales Pipeline Updates" in live.text
    assert "Nicolás de León" in live.text
    assert live.headers["cache-control"] == "no-store"
    assert "Pernod Ricard" not in live.text
    demo = client.get("/review/demo")
    assert demo.status_code == 200 and "Pernod Ricard" in demo.text


def test_build_requires_ingest_credential(monkeypatch):
    monkeypatch.setattr(review_api, "_build", lambda force=False: {"done": True})
    assert client.post("/review/build", headers=HEADERS).status_code == 401
    assert client.post("/review/build", headers={"X-Ingest-Token": "test-token"}).json() == {"done": True}


def test_force_rebuild_is_explicit_ingest_only_and_forwarded(monkeypatch):
    calls = []
    monkeypatch.setattr(review_api, "_build", lambda force=False: calls.append(force) or {"done": True})
    assert client.post("/review/build?force=true", headers=HEADERS).status_code == 401
    assert client.post("/review/build", headers={"X-Ingest-Token": "test-token"}).status_code == 200
    assert client.post("/review/build?force=true", headers={"X-Ingest-Token": "test-token"}).status_code == 200
    assert calls == [False, True]


def test_daily_review_requires_completed_upstream_processing_and_never_builds_brief(monkeypatch):
    class Conn:
        row = ("done", False)
        def execute(self, *args): return self
        def fetchone(self): return self.row
    conn = Conn()
    @contextmanager
    def connect(): yield conn
    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda c: "workspace")
    builds = []
    monkeypatch.setattr(review_api, "_build", lambda: builds.append(True) or {"done": True, "skipped": True})
    monkeypatch.setattr(review_api.brief_api, "build_brief", lambda *a: pytest.fail("must not build another brief"))
    assert client.post("/review/daily", headers=HEADERS).status_code == 401
    for row in (None, ("classify", False), ("timed_out", False), ("done", True)):
        conn.row = row
        assert client.post("/review/daily", headers={"X-Ingest-Token": "test-token"}).status_code == 409
    assert builds == []
    conn.row = ("done", False)
    result = client.post("/review/daily", headers={"X-Ingest-Token": "test-token"})
    assert result.json() == {"done": True, "skipped": True}
    assert builds == [True]


def test_publish_keeps_independent_results_and_rejects_timed_out(monkeypatch):
    class Conn:
        stage = "done"
        pending = False
        def execute(self, *args): return self
        def fetchone(self): return (self.stage, self.pending)
    conn = Conn()
    @contextmanager
    def connect(): yield conn
    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda c: "workspace")
    monkeypatch.setattr(review_api, "_build", lambda: {"done": True, "created": 6})
    def fail(*args): raise RuntimeError("do not expose provider text")
    monkeypatch.setattr(review_api.brief_api, "build_brief", fail)
    response = client.post("/process/publish", headers={"X-Ingest-Token": "test-token"})
    assert response.json()["review"] == {"ok": True, "result": {"done": True, "created": 6}}
    assert response.json()["brief"]["ok"] is False
    assert "provider text" not in response.text
    conn.stage = "timed_out"
    assert client.post("/process/publish", headers={"X-Ingest-Token": "test-token"}).status_code == 409
    conn.stage, conn.pending = "done", True
    assert client.post("/process/publish", headers={"X-Ingest-Token": "test-token"}).status_code == 409


def test_status_change_uses_the_authenticated_founder_over_rest_and_mcp(monkeypatch):
    calls = []
    def change(workspace_id, row_id, actor, expected_status, new_status):
        calls.append((row_id, actor, expected_status, new_status))
        return {"rowId": row_id, "company": "Diageo", "previous": expected_status, "status": new_status}
    @contextmanager
    def connect():
        yield object()
    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda conn: "workspace")
    monkeypatch.setattr(review_service, "change_status", change)
    row = str(uuid4())
    body = {"expected_status": "1. Opening", "status": "0. Paused"}
    rest = client.post(f"/review/rows/{row}/status", json=body, headers=HEADERS)
    assert rest.status_code == 200 and rest.json()["status"] == "0. Paused"
    tool = rpc("tools/call", {"name": "bianca_set_status", "arguments": {"row_id": row, **body}})
    assert tool.json()["result"]["isError"] is False
    assert calls == [(row, "NDL", "1. Opening", "0. Paused")] * 2
    assert client.post(f"/review/rows/{row}/status", json=body).status_code == 401
    assert client.post(f"/review/rows/{row}/status", json={**body, "actor": "RG"}, headers=HEADERS).status_code == 422
    assert client.post(f"/review/rows/{row}/status", json={"expected_status": "1. Opening", "status": ""},
                       headers=HEADERS).status_code == 422


@pytest.mark.parametrize("error, code", [(review_service.ReviewConflict("changed"), 409),
                                         (review_service.ReviewNotFound("gone"), 404),
                                         (review_service.ReviewUnavailable("down"), 503), (ValueError("bad"), 422)])
def test_status_change_errors_map_to_clear_http_codes(monkeypatch, error, code):
    @contextmanager
    def connect():
        yield object()
    def change(*args):
        raise error
    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda conn: "workspace")
    monkeypatch.setattr(review_service, "change_status", change)
    body = {"expected_status": "1. Opening", "status": "0. Paused"}
    assert client.post(f"/review/rows/{uuid4()}/status", json=body, headers=HEADERS).status_code == code


@pytest.fixture
def archived_brief(monkeypatch):
    state = {"row": {"id": uuid4(), "for_date": date(2026, 11, 19), "subject": "Today's Daily Brief",
                     "html": "<table>Six email rows</table>", "full_html": "<table>All eight rows</table>",
                     "created_at": datetime(2026, 11, 19, 12, tzinfo=timezone.utc)}, "queries": []}

    class Conn:
        def execute(self, query, params):
            assert query.startswith("select ")
            state["queries"].append((query, params))
            return self

        def fetchone(self):
            return state["row"]

    @contextmanager
    def connect():
        yield Conn()

    monkeypatch.setattr(review_api.db, "connect", connect)
    monkeypatch.setattr(review_api, "_workspace_id", lambda _conn: "review-workspace")
    monkeypatch.setattr(review_api.brief_api, "build_brief", lambda *args, **kw: pytest.fail("must not build a brief"))
    return state


@pytest.mark.parametrize("path", ["/review/briefs/latest", f"/review/briefs/{uuid4()}"])
def test_saved_brief_requires_founder_auth_and_allowed_origin(monkeypatch, path):
    monkeypatch.setattr(review_api, "_daily_brief", lambda *args: pytest.fail("must not read a brief"))
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Ingest-Token": "test-token"}).status_code == 401
    assert client.get(path, headers={**HEADERS, "Origin": "https://untrusted.example"}).status_code == 403
    monkeypatch.setattr(settings, "review_enabled", False)
    assert client.get(path, headers=HEADERS).status_code == 404


def test_saved_brief_rest_and_mcp_share_full_snapshot_read(archived_brief):
    brief_id = archived_brief["row"]["id"]
    response = client.get(f"/review/briefs/{brief_id}", headers=HEADERS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"id": str(brief_id), "for_date": "2026-11-19", "subject": "Today's Daily Brief",
                               "html": "<table>All eight rows</table>", "full_available": True,
                               "created_at": "2026-11-19T12:00:00+00:00"}
    result = rpc("tools/call", {"name": "bianca_daily_brief", "arguments": {"brief_id": str(brief_id)}})
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["result"]["structuredContent"] == response.json()
    assert result.json()["result"]["isError"] is False
    for query, params in archived_brief["queries"]:
        assert "where workspace_id = %s and id = %s" in query
        assert params == ("review-workspace", brief_id)


def test_latest_saved_brief_is_limited_to_today_in_workspace_timezone(archived_brief):
    result = client.get("/review/briefs/latest", headers=HEADERS)
    assert result.status_code == 200 and result.json()["full_available"] is True
    tool = rpc("tools/call", {"name": "bianca_daily_brief", "arguments": {}})
    assert tool.json()["result"]["structuredContent"] == result.json()
    for query, params in archived_brief["queries"]:
        assert "where workspace_id = %s and for_date = (now() at time zone" in query
        assert "select timezone from workspaces where id = %s" in query
        assert query.endswith("order by created_at desc, id desc limit 1")
        assert params == ("review-workspace", "review-workspace")


def test_latest_saved_brief_does_not_fall_back_to_yesterday(archived_brief):
    archived_brief["row"] = None
    response = client.get("/review/briefs/latest", headers=HEADERS)
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "No Daily Brief has been saved for today."}
    result = rpc("tools/call", {"name": "bianca_daily_brief", "arguments": {}}).json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == {"status": 404, "detail": response.json()["detail"]}


def test_missing_or_foreign_saved_brief_returns_not_found(archived_brief):
    archived_brief["row"] = None
    foreign_id = uuid4()
    response = client.get(f"/review/briefs/{foreign_id}", headers=HEADERS)
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Daily Brief not found."}
    query, params = archived_brief["queries"][0]
    assert "where workspace_id = %s and id = %s" in query
    assert params == ("review-workspace", foreign_id)


def test_legacy_saved_brief_is_identified_and_uses_its_saved_html(archived_brief):
    archived_brief["row"]["full_html"] = None
    response = client.get(f"/review/briefs/{archived_brief['row']['id']}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["html"] == "<table>Six email rows</table>"
    assert response.json()["full_available"] is False


def test_saved_brief_rejects_invalid_ids_and_tool_arguments_before_read(monkeypatch):
    monkeypatch.setattr(review_api, "_daily_brief", lambda *args: pytest.fail("must not read a brief"))
    assert client.get("/review/briefs/invalid", headers=HEADERS).status_code == 422
    for args in ({"brief_id": "invalid"}, {"actor": "RG"}, {"workspace_id": str(uuid4())}):
        result = rpc("tools/call", {"name": "bianca_daily_brief", "arguments": args})
        assert result.json()["error"]["code"] == -32602


# ---- H-16: Keep current bullets + add these

def test_append_mode_is_optional_validated_and_forwarded_by_rest_and_the_connector(monkeypatch):
    calls = []
    monkeypatch.setattr(review_api, "_workspace_id", lambda conn: "workspace")
    @contextmanager
    def connect():
        yield object()
    monkeypatch.setattr(review_api.db, "connect", connect)
    def decide(*args, **kwargs):
        calls.append((args[1], args[3], kwargs))
        return {"status": "approved"}
    monkeypatch.setattr(review_service, "decide", decide)
    proposal_id = str(uuid4())
    body = {"revision": 2, "idempotency_key": KEY, "next_step_bullets": ["Send scope."], "client_context_bullets": []}
    route = f"/review/proposals/{proposal_id}/approve"
    assert client.post(route, json=body, headers=HEADERS).status_code == 200
    assert client.post(route, json={**body, "next_step_mode": "replace"}, headers=HEADERS).status_code == 200
    assert client.post(route, json={**body, "next_step_mode": "append"}, headers=HEADERS).status_code == 200
    r = rpc("tools/call", {"name": "bianca_approve_update",
                           "arguments": {**body, "proposal_id": proposal_id, "next_step_mode": "append"}})
    assert r.json()["result"]["isError"] is False
    assert calls == [(proposal_id, "approve", {}), (proposal_id, "approve", {}),
                     (proposal_id, "approve", {"next_step_mode": "append"}),
                     (proposal_id, "approve", {"next_step_mode": "append"})]
    assert client.post(route, json={**body, "next_step_mode": "merge"}, headers=HEADERS).status_code == 422
    ignore = {"revision": 2, "idempotency_key": KEY, "next_step_mode": "append"}
    assert client.post(f"/review/proposals/{proposal_id}/ignore", json=ignore, headers=HEADERS).status_code == 422
    assert len(calls) == 4


def test_connector_schema_documents_the_optional_next_step_mode():
    tool = next(t for t in review_api._tools() if t["name"] == "bianca_approve_update")
    schema = tool["inputSchema"]
    assert schema["properties"]["next_step_mode"]["enum"] == ["replace", "append"]
    assert schema["properties"]["next_step_mode"]["default"] == "replace"
    assert "next_step_mode" not in schema["required"]
    assert "Last progress is left unchanged" in tool["description"]
    ignore = next(t for t in review_api._tools() if t["name"] == "bianca_ignore_update")
    assert "next_step_mode" not in ignore["inputSchema"]["properties"]
