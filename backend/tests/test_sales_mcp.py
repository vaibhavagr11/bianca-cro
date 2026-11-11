"""Exercise the connector's actual FastAPI mount and MCP protocol."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken

from app import sales_mcp
from app.config import Settings
from app.main import create_app

BASE = "https://bianca-test.example"
URL = BASE + "/sales/mcp"
ISSUER = "https://issuer.example"
HEADERS = {
    "Authorization": "Bearer founder-rg",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


def sales_config(**overrides):
    return Settings(**{
        "database_url": "postgresql://unused.invalid/admin",
        "ingest_token": "test-ingest-token",
        "sales_mcp_enabled": True,
        "sales_mcp_database_url": "postgresql://unused.invalid/reader",
        "sales_mcp_url": URL,
        "sales_mcp_issuer": ISSUER,
        "sales_mcp_jwks_url": ISSUER + "/jwks",
        "sales_mcp_founders": {"founder-rg": "RG", "founder-nc": "NC"},
        **overrides,
    })


def rpc(client, method, params=None, headers=HEADERS):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/sales/mcp", json=payload, headers=headers)


@pytest.fixture
def mock_sales_tokens(monkeypatch):
    async def verify(_verifier, token):
        if token == "test-ingest-token":
            return None
        return AccessToken(
            token=token, client_id="claude-test",
            scopes=[] if token == "no-scope" else ["openid"],
            resource=URL, subject="founder-rg" if token == "no-scope" else token,
        )

    monkeypatch.setattr(sales_mcp.SalesTokenVerifier, "verify_token", verify)


def test_disabled_connector_and_incomplete_config():
    with TestClient(create_app(sales_config(sales_mcp_enabled=False)), base_url=BASE) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert rpc(client, "tools/list").status_code == 404
    with pytest.raises(ValueError, match="founder subjects"):
        create_app(sales_config(sales_mcp_founders={}))


def test_oauth_discovery_at_exact_public_url(mock_sales_tokens):
    with TestClient(create_app(sales_config()), base_url=BASE) as client:
        unauthenticated = rpc(client, "tools/list", headers={})
        assert unauthenticated.status_code == 401
        assert BASE + "/.well-known/oauth-protected-resource/sales/mcp" in unauthenticated.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/sales/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == URL
        assert metadata.json()["authorization_servers"] == [ISSUER]
        assert metadata.json()["scopes_supported"] == ["openid"]
        for header, status in [("test-ingest-token", 401), ("no-scope", 403)]:
            rejected = rpc(client, "tools/list", headers={**HEADERS, "Authorization": f"Bearer {header}"})
            assert rejected.status_code == status
        assert rpc(client, "tools/list", headers={**HEADERS, "Origin": "https://untrusted.example"}).status_code == 403


def test_founder_sees_only_sales_reads_and_mcp_uses_viewer_identity(mock_sales_tokens, monkeypatch):
    seen = []

    def read(_dsn, _feeds, _stale, handler, **arguments):
        seen.append((handler.__name__, arguments))
        return {"status": "ok", "coverage": {"feed_receipts": {}}}

    monkeypatch.setattr(sales_mcp, "_read", read)
    with TestClient(create_app(sales_config()), base_url=BASE) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        handshake = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
        })
        assert handshake.status_code == 200
        assert handshake.json()["result"]["protocolVersion"] == "2025-06-18"
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == {
            "search_pipeline", "get_account_activity", "find_stakeholder_evidence", "rank_warm_prospects",
        }
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        args = {"opportunity_id": str(uuid4())}
        for founder in ("rg", "nc"):
            headers = {**HEADERS, "Authorization": f"Bearer founder-{founder}"}
            response = rpc(client, "tools/call", {"name": "get_account_activity", "arguments": args}, headers)
            result = response.json()["result"]
            assert not result.get("isError"), result
            assert result["structuredContent"]["status"] == "ok"
        denied = rpc(client, "tools/call", {"name": "bianca_approve_update", "arguments": {}})
        assert denied.json()["result"]["isError"]
        invalid = rpc(client, "tools/call", {"name": "rank_warm_prospects", "arguments": {"limit": 999}})
        assert invalid.json()["result"]["isError"]

    assert [name for name, _ in seen] == ["get_account_activity", "get_account_activity"]
    assert [args["viewer_mailbox"] for _, args in seen] == ["RG", "NC"]
