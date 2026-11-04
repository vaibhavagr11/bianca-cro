"""Transport/auth tests; no production database, Claude, or Google calls."""
from contextlib import contextmanager
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


def test_workspace_uses_ndl_identity(monkeypatch):
    monkeypatch.setattr(review_api, "_workspace", lambda: {"proposals": [], "rows": [], "activity": []})
    result = client.get("/review/workspace", headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["viewer"] == "NDL"


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
    assert {t["name"] for t in tools} == {"bianca_review_workspace", "bianca_approve_update", "bianca_ignore_update"}
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
    assert "Nico · NDL" in live.text
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
