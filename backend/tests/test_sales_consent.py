import base64
import hashlib
from uuid import UUID

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.sales_consent import CONSENT_PATH, SCRIPT_PATH

BASE = "https://bianca-test.example"
ISSUER = "https://abcdefghijklmnopqrst.supabase.co/auth/v1"
PILOT_SUBJECT = "1390270e-f0d6-4f28-a64f-fbef2041787e"
CLIENT_ID = UUID("df3ccf22-e44e-4f62-b190-a4dd4b63ff71")


def consent_config(**overrides):
    return Settings(**{
        "database_url": "postgresql://unused.invalid/admin",
        "ingest_token": "test-ingest-token",
        "sales_mcp_enabled": True,
        "sales_mcp_url": BASE + "/sales/mcp",
        "sales_mcp_issuer": ISSUER,
        "sales_mcp_jwks_url": ISSUER + "/.well-known/jwks.json",
        "sales_mcp_supabase_client_id": CLIENT_ID,
        "sales_mcp_supabase_publishable_key": "sb_publishable_test",
        "sales_mcp_database_url": "postgresql://unused.invalid/reader",
        "sales_mcp_founders": {PILOT_SUBJECT: None},
        **overrides,
    })


def test_serves_a_restricted_same_origin_consent_page():
    with TestClient(create_app(consent_config()), base_url=BASE) as client:
        page = client.get(CONSENT_PATH, params={"authorization_id": "auth-id_123"})
        script = client.get(SCRIPT_PATH)

    script_hash = base64.b64encode(hashlib.sha256(script.content).digest()).decode("ascii")
    assert page.status_code == script.status_code == 200
    assert page.headers["cache-control"] == script.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    policy = page.headers["content-security-policy"]
    assert f"script-src 'sha256-{script_hash}'" in policy
    assert "connect-src https://abcdefghijklmnopqrst.supabase.co" in policy
    assert "frame-ancestors 'none'" in policy and "form-action 'none'" in policy
    assert f'integrity="sha256-{script_hash}"' in page.text
    assert 'data-authorization-id="auth-id_123"' in page.text
    assert '<form id="sign-in" hidden>' in page.text
    assert '<input id="password" type="password" autocomplete="off" required>' in page.text
    assert '<button type="submit">Sign in</button>' in page.text
    assert "Email me a code" not in page.text
    assert PILOT_SUBJECT not in page.text
    assert hashlib.sha256(PILOT_SUBJECT.encode()).hexdigest() in page.text
    assert "test-ingest-token" not in page.text


def test_rejects_invalid_authorization_requests_and_respects_feature_flag():
    with TestClient(create_app(consent_config()), base_url=BASE) as client:
        assert client.get(CONSENT_PATH).status_code == 400
        injection = client.get(CONSENT_PATH, params={"authorization_id": "<script>"})
        assert injection.status_code == 400
        assert "<script>" not in injection.text
        repeated = client.get(CONSENT_PATH, params=[
            ("authorization_id", "auth-id_123"), ("authorization_id", "auth-id_456"),
        ])
        assert repeated.status_code == 400
    with TestClient(create_app(consent_config(sales_mcp_enabled=False)), base_url=BASE) as client:
        assert client.get(CONSENT_PATH, params={"authorization_id": "auth-id_123"}).status_code == 404
        assert client.get(SCRIPT_PATH).status_code == 404
