"""Browser auth security and transport regressions; no external services used."""

import base64
import hashlib
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import OperationalError
from pydantic import SecretStr

from app import hub_auth, review_api
from app.config import Settings, settings
from app.main import app

ORIGIN = "https://hub.example.com"
EMAIL = "ramiro@bianca-ai.com"
COOKIE = "a" * 43
STATE = "b" * 43
BROWSER = "c" * 43
BEARER = "old-review-token-" + "z" * 32


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "review_enabled", True)
    monkeypatch.setattr(settings, "hub_google_auth_enabled", True)
    monkeypatch.setattr(settings, "hub_google_client_id", "client.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "hub_google_client_secret", SecretStr("client-secret"))
    monkeypatch.setattr(settings, "hub_google_domain", "bianca-ai.com")
    monkeypatch.setattr(settings, "hub_google_users", {EMAIL: "RG"})
    monkeypatch.setattr(settings, "hub_session_hours", 168)
    monkeypatch.setattr(settings, "public_base_url", ORIGIN)
    monkeypatch.setattr(settings, "review_tokens", {"RG": SecretStr(BEARER)})
    # If a test misses a DB boundary, fail rather than accidentally connecting.
    monkeypatch.setattr(hub_auth.db, "connect", lambda: pytest.fail("unexpected database access"))


@pytest.fixture
def client():
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as result:
        yield result


@pytest.fixture
def active_session(monkeypatch, client):
    current = {"google_sub": "google-user-1", "email": EMAIL, "actor": "RG",
               "expires_at": datetime.now(timezone.utc) + timedelta(days=7)}
    monkeypatch.setattr(hub_auth, "_read_session", lambda token: current if token == COOKIE else None)
    client.cookies.set(hub_auth.SESSION_COOKIE, COOKIE)
    return current


def test_disabled_mode_keeps_old_browser_and_mcp_credentials(monkeypatch, client):
    monkeypatch.setattr(settings, "hub_google_auth_enabled", False)
    monkeypatch.setattr(review_api, "_workspace", lambda: {"rows": []})
    assert client.get("/auth/session").json() == {"mode": "token", "authenticated": False}
    assert client.get("/review/workspace", headers={"Authorization": "Bearer " + BEARER}).json()["viewer"] == "RG"
    assert client.get("/auth/google/login").status_code == 404


def test_default_session_lifetime_is_seven_days():
    assert Settings.model_fields["hub_session_hours"].default == 7 * 24


def test_google_mode_never_accepts_browser_bearer_but_mcp_still_does(client):
    assert client.get("/review/workspace", headers={"Authorization": "Bearer " + BEARER}).status_code == 401
    assert client.get("/review/mcp", headers={"Authorization": "Bearer " + BEARER}).status_code == 405
    assert client.get("/review/mcp").status_code == 401


@pytest.mark.parametrize("name,value", [
    ("hub_google_client_id", None), ("hub_google_client_secret", None),
    ("hub_google_users", {}), ("hub_google_users", {EMAIL: "ADMIN"}),
    ("hub_google_users", {EMAIL: "RG", "other@bianca-ai.com": "RG"}),
    ("hub_google_users", {"invalid email@example.com": "RG"}),
    ("hub_google_users", {"UPPER@bianca-ai.com": "RG"}),
    ("public_base_url", "http://hub.example.com"), ("public_base_url", "https://hub.example.com/path"),
    ("public_base_url", "https://[invalid"), ("hub_google_domain", ""),
])
def test_misconfiguration_fails_closed_without_token_fallback(monkeypatch, client, name, value):
    monkeypatch.setattr(settings, name, value)
    assert client.get("/auth/session").status_code == 503
    assert client.get("/auth/google/login").status_code == 503
    assert client.get("/review/workspace", headers={"Authorization": "Bearer " + BEARER}).status_code == 503


def test_session_contract_is_private_and_does_not_slide(client, active_session):
    first = client.get("/auth/session")
    second = client.get("/auth/session")
    assert first.json() == second.json() == {
        "mode": "google", "authenticated": True, "actor": "RG", "email": EMAIL,
        "expires_at": active_session["expires_at"].isoformat(), "csrf_token": hub_auth._proof(COOKIE, "hub-csrf-v1"),
    }
    assert first.headers["cache-control"] == "no-store"
    assert "set-cookie" not in first.headers  # reads do not refresh browser expiry
    assert "google_sub" not in first.json()


def test_removed_mapping_revokes_existing_session(monkeypatch, client, active_session):
    revoked = []
    monkeypatch.setattr(settings, "hub_google_users", {"other@bianca-ai.com": "NC"})
    monkeypatch.setattr(hub_auth, "_revoke_session", revoked.append)
    result = client.get("/auth/session")
    assert result.json() == {"mode": "google", "authenticated": False}
    assert revoked == [COOKIE]
    assert "set-cookie" not in result.headers


def test_expired_or_revoked_session_denied(monkeypatch, client):
    client.cookies.set(hub_auth.SESSION_COOKIE, COOKIE)
    monkeypatch.setattr(hub_auth, "_read_session", lambda _: None)
    assert client.get("/auth/session").json()["authenticated"] is False
    assert client.get("/review/workspace").status_code == 401


def test_database_outage_never_looks_like_token_mode(monkeypatch, client):
    client.cookies.set(hub_auth.SESSION_COOKIE, COOKIE)
    monkeypatch.setattr(hub_auth, "_read_session", lambda _: (_ for _ in ()).throw(OperationalError("private detail")))
    response = client.get("/auth/session")
    assert response.status_code == 503
    assert "private detail" not in response.text


@pytest.mark.parametrize("headers", [
    {}, {"Origin": ORIGIN}, {"Origin": "https://attacker.example", "X-CSRF-Token": hub_auth._proof(COOKIE, "hub-csrf-v1")},
    {"X-CSRF-Token": hub_auth._proof(COOKIE, "hub-csrf-v1")},
    {"Origin": ORIGIN, "X-CSRF-Token": "forged"},
    {"Origin": ORIGIN, "X-CSRF-Token": hub_auth._proof(COOKIE, "hub-csrf-v1"), "Sec-Fetch-Site": "cross-site"},
])
def test_cookie_writes_require_origin_and_csrf(client, active_session, headers):
    assert client.post("/auth/logout", headers=headers).status_code == 403


def test_non_ascii_csrf_is_rejected_without_server_error(client, active_session):
    assert client.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": b"\xc3\xa9"}).status_code == 403


def test_allowed_cookie_write_attributes_actor_and_rejects_spoofing(monkeypatch, client, active_session):
    calls = []
    monkeypatch.setattr(review_api, "_decision", lambda proposal, actor, action, payload:
                        calls.append((actor, action)) or {"status": "ignored"})
    headers = {"Origin": ORIGIN, "X-CSRF-Token": hub_auth._proof(COOKIE, "hub-csrf-v1")}
    body = {"revision": 1, "idempotency_key": "request-key-00000001"}
    path = f"/review/proposals/{uuid4()}/ignore"
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert calls == [("RG", "ignore")]
    assert client.post(path, headers=headers, json={**body, "actor": "MG"}).status_code == 422
    assert client.post("/review/build", headers=headers).status_code == 401


def test_cross_origin_cannot_read_authenticated_session(client, active_session):
    assert client.get("/auth/session", headers={"Origin": "https://claude.ai"}).status_code == 403


def test_logout_revokes_and_clears_cookie(monkeypatch, client, active_session):
    revoked = []
    monkeypatch.setattr(hub_auth, "_revoke_session", revoked.append)
    result = client.post("/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": hub_auth._proof(COOKIE, "hub-csrf-v1")})
    assert result.status_code == 200 and result.json()["authenticated"] is False
    assert revoked == [COOKIE]
    assert "Max-Age=0" in result.headers["set-cookie"]


def test_login_uses_state_nonce_pkce_and_secure_cookie(monkeypatch, client):
    calls = []
    monkeypatch.setattr(hub_auth, "_new_state", lambda *args: calls.append(args))
    path = "/review/app?tab=daily-brief&brief=" + str(uuid4())
    response = client.get("/auth/google/login", params={"return_to": path}, headers={"Host": "attacker.example"})
    query = parse_qs(urlsplit(response.headers["location"]).query)
    state, browser, saved_path = calls[0]
    verifier = hub_auth._proof(browser, "pkce:" + state)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert query["state"] == [state] and query["nonce"] == [hub_auth._proof(browser, "nonce:" + state)]
    assert query["code_challenge"] == [expected] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [ORIGIN + "/auth/google/callback"]
    assert query["scope"] == ["openid email"] and saved_path == path
    assert "client_secret" not in query
    cookie = response.headers["set-cookie"]
    assert all(part in cookie for part in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=600"))
    assert "Domain=" not in cookie


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/review/app/../evil", "/review/app#evil",
                                    "/review/app\\evil", "/auth/logout", "/review/app\r\nLocation: evil", "https://[invalid"])
def test_return_to_cannot_be_an_open_redirect(target):
    assert hub_auth._return_to(target) == "/review/app"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def claims():
    return {"iss": "https://accounts.google.com", "aud": settings.hub_google_client_id,
            "sub": "google-user-1", "iat": int(time.time()), "exp": int(time.time()) + 300,
            "email": EMAIL, "email_verified": True, "hd": "bianca-ai.com", "nonce": "expected-nonce"}


def verify_token(monkeypatch, signing_key, claims, *, signing=None):
    monkeypatch.setattr(hub_auth, "_jwks", SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signing_key.public_key())))
    token = jwt.encode(claims, signing or signing_key, algorithm="RS256", headers={"kid": "google-key"})
    return hub_auth._verify_identity(token, "expected-nonce")


def test_google_token_signature_and_identity_are_verified(monkeypatch, signing_key, claims):
    assert verify_token(monkeypatch, signing_key, claims) == {"google_sub": "google-user-1", "email": EMAIL, "actor": "RG"}


def test_approved_secondary_domain_email_requires_configured_workspace_domain(monkeypatch, signing_key, claims):
    # Google hd identifies the Workspace organization; a user's approved email
    # may legitimately use one of its secondary domains or aliases.
    monkeypatch.setattr(settings, "hub_google_domain", "primary.example.com")
    claims["hd"] = "primary.example.com"
    assert hub_auth._configuration()[1] == {EMAIL: "RG"}
    assert verify_token(monkeypatch, signing_key, claims)["actor"] == "RG"
    claims["hd"] = "bianca-ai.com"
    with pytest.raises(hub_auth.LoginError):
        verify_token(monkeypatch, signing_key, claims)


@pytest.mark.parametrize("field,value", [
    ("iss", "https://evil.example"), ("aud", "another-client"), ("aud", ["client.apps.googleusercontent.com"]),
    ("exp", 1), ("exp", "9999999999"), ("iat", int(time.time()) + 3600),
    ("sub", ""), ("sub", 123), ("email_verified", False), ("email_verified", "true"),
    ("hd", "other.example"), ("email", "stranger@bianca-ai.com"), ("nonce", "attacker-nonce"),
    ("azp", "another-client"),
])
def test_invalid_google_claims_rejected(monkeypatch, signing_key, claims, field, value):
    claims[field] = value
    with pytest.raises((jwt.PyJWTError, hub_auth.LoginError)):
        verify_token(monkeypatch, signing_key, claims)


@pytest.mark.parametrize("field", ["iss", "aud", "sub", "exp", "iat", "email", "email_verified", "hd", "nonce"])
def test_missing_google_claims_rejected(monkeypatch, signing_key, claims, field):
    del claims[field]
    with pytest.raises(jwt.PyJWTError):
        verify_token(monkeypatch, signing_key, claims)


def test_invalid_google_signature_rejected(monkeypatch, signing_key, claims):
    different = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        verify_token(monkeypatch, signing_key, claims, signing=different)


def test_successful_callback_exchanges_code_and_preserves_deep_link(monkeypatch, client):
    path = "/review/app?tab=daily-brief&brief=" + str(uuid4())
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    flows = {(STATE, BROWSER): {"workspace_id": "workspace", "return_to": path}}
    monkeypatch.setattr(hub_auth, "_consume_state", lambda state, browser: flows.pop((state, browser), None))
    calls = []
    monkeypatch.setattr(hub_auth, "_exchange_code", lambda *args: calls.append(args) or "id-token")
    monkeypatch.setattr(hub_auth, "_verify_identity", lambda token, nonce: {"google_sub": "sub", "email": EMAIL, "actor": "RG"})
    monkeypatch.setattr(hub_auth, "_create_session", lambda *args: calls.append(args))
    response = client.get("/auth/google/callback", params={"state": STATE, "code": "google-code"})
    assert response.status_code == 303 and response.headers["location"] == path
    assert calls[0] == ("google-code", hub_auth._proof(BROWSER, "pkce:" + STATE), ORIGIN)
    assert calls[1][0] == "workspace" and calls[1][1]["actor"] == "RG"
    cookie = response.headers.get_list("set-cookie")[0]
    assert all(part in cookie for part in ("Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=604800"))
    assert "Domain=" not in cookie
    # An already consumed callback cannot replay even with its old browser cookie.
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    replay = client.get("/auth/google/callback", params={"state": STATE, "code": "google-code"})
    assert replay.headers["location"] == "/review/app?auth_error=invalid_state"


def test_missing_browser_state_never_exchanges_code(client):
    response = client.get("/auth/google/callback", params={"state": STATE, "code": "google-code"})
    assert response.headers["location"] == "/review/app?auth_error=invalid_state"


def test_cancelled_callback_consumes_state_without_provider_data(monkeypatch, client):
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    monkeypatch.setattr(hub_auth, "_consume_state", lambda *_: {"workspace_id": "workspace", "return_to": "/review/app"})
    response = client.get("/auth/google/callback", params={"state": STATE, "error": "access_denied_with_private_text"})
    assert response.headers["location"] == "/review/app?auth_error=cancelled"


@pytest.mark.parametrize("exception,code", [
    (jwt.InvalidAudienceError("private"), "invalid_identity"),
    (httpx.ConnectError("private"), "unavailable"),
    (hub_auth.LoginError("identity_mismatch"), "identity_mismatch"),
])
def test_callback_errors_only_expose_safe_codes(monkeypatch, client, exception, code):
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    monkeypatch.setattr(hub_auth, "_consume_state", lambda *_: {"workspace_id": "workspace", "return_to": "/review/app"})
    monkeypatch.setattr(hub_auth, "_exchange_code", lambda *_: (_ for _ in ()).throw(exception))
    response = client.get("/auth/google/callback", params={"state": STATE, "code": "code"})
    assert response.headers["location"] == "/review/app?auth_error=" + code
    assert "Max-Age=0" in response.headers["set-cookie"]


class Connection:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []
        self.row_factory = None

    def execute(self, query, args=()):
        self.calls.append((query, args))
        return self

    def fetchone(self):
        return next(self.rows)


def connect_fake(monkeypatch, conn):
    @contextmanager
    def connect():
        yield conn
    monkeypatch.setattr(hub_auth.db, "connect", connect)


@pytest.mark.parametrize("bound", [None, {"google_sub": "reassigned-user", "actor": "RG", "disabled_at": None},
                                   {"google_sub": "sub", "actor": "NC", "disabled_at": None},
                                   {"google_sub": "sub", "actor": "RG", "disabled_at": datetime.now(timezone.utc)}])
def test_email_reassignment_or_changed_identity_never_inherits_access(monkeypatch, bound):
    conn = Connection([bound])
    connect_fake(monkeypatch, conn)
    with pytest.raises(hub_auth.LoginError, match="identity_mismatch"):
        hub_auth._create_session("workspace", {"google_sub": "sub", "email": EMAIL, "actor": "RG"}, COOKIE, None)
    assert not any("insert into hub_sessions" in query for query, _ in conn.calls)


def test_session_storage_hashes_secret_and_uses_absolute_expiry(monkeypatch):
    conn = Connection([{"google_sub": "sub", "actor": "RG", "disabled_at": None}])
    connect_fake(monkeypatch, conn)
    hub_auth._create_session("workspace", {"google_sub": "sub", "email": EMAIL, "actor": "RG"}, COOKIE, BROWSER)
    inserts = [(query, args) for query, args in conn.calls if "insert into hub_sessions" in query]
    assert inserts[0][1] == (hashlib.sha256(COOKIE.encode()).hexdigest(), "workspace", "sub", 168)
    assert "now() + %s * interval '1 hour'" in inserts[0][0]
    assert not any(COOKIE in args or BROWSER in args for _, args in conn.calls)


def test_session_lookup_enforces_expiry_revocation_and_identity_disable(monkeypatch):
    conn = Connection([("workspace",), None])
    connect_fake(monkeypatch, conn)
    assert hub_auth._read_session(COOKIE) is None
    query, args = conn.calls[-1]
    assert all(clause in query for clause in ("s.revoked_at is null", "s.expires_at > now()", "i.disabled_at is null"))
    assert args == (hashlib.sha256(COOKIE.encode()).hexdigest(), "workspace")
    assert not any(query.startswith("update") for query, _ in conn.calls)


def test_state_consumption_is_atomic_browser_bound_and_expiring(monkeypatch):
    conn = Connection([("workspace",), {"workspace_id": "workspace", "return_to": "/review/app"}])
    connect_fake(monkeypatch, conn)
    result = hub_auth._consume_state(STATE, BROWSER)
    query, args = conn.calls[-1]
    assert result["return_to"] == "/review/app"
    assert query.startswith("delete from hub_oauth_states") and "returning" in query and "expires_at > now()" in query
    assert args == (hashlib.sha256(STATE.encode()).hexdigest(), hashlib.sha256(BROWSER.encode()).hexdigest(), "workspace")


def test_uvicorn_access_log_redacts_oauth_callback_query():
    logger = logging.getLogger("uvicorn.access")
    record = logger.makeRecord("uvicorn.access", logging.INFO, "", 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/auth/google/callback?code=private-code&state=private-state", "1.1", 303), None)
    assert logger.filter(record)
    assert "/auth/google/callback" in record.getMessage()
    assert "private-code" not in record.getMessage() and "private-state" not in record.getMessage()
    unrelated = logger.makeRecord("uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/%s" %d',
                                  ("127.0.0.1", "GET", "/health", "1.1", 200), None)
    assert logger.filter(unrelated) and "/health" in unrelated.getMessage()


def test_popup_login_mode_is_bound_to_state_and_uses_existing_callback(monkeypatch, client):
    calls = []
    monkeypatch.setattr(hub_auth, "_new_state", lambda *args: calls.append(args))
    response = client.get("/auth/google/login?popup=1&return_to=/review/app?tab=daily-brief")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    state, browser, target = calls[0]
    assert state.startswith("p.") and len(state) == 45
    assert query["state"] == [state]
    assert query["nonce"] == [hub_auth._proof(browser, "nonce:" + state)]
    assert query["redirect_uri"] == [ORIGIN + "/auth/google/callback"]
    assert target == "/review/app?tab=daily-brief"


def test_popup_callback_removes_google_secrets_before_completion(monkeypatch, client):
    popup_state = "p." + STATE
    path = "/review/app?tab=daily-brief&brief=" + str(uuid4())
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    flows = {(popup_state, BROWSER): {"workspace_id": "workspace", "return_to": path}}
    monkeypatch.setattr(hub_auth, "_consume_state", lambda state, browser: flows.pop((state, browser), None))
    monkeypatch.setattr(hub_auth, "_exchange_code", lambda *_: "secret-id-token")
    monkeypatch.setattr(hub_auth, "_verify_identity", lambda *_: {"google_sub": "sub", "email": EMAIL, "actor": "RG"})
    created = []
    monkeypatch.setattr(hub_auth, "_create_session", lambda *args: created.append(args))
    response = client.get("/auth/google/callback", params={"state": popup_state, "code": "private-google-code"})
    destination = urlsplit(response.headers["location"])
    query = parse_qs(destination.query)
    assert response.status_code == 303 and destination.path == "/auth/google/complete"
    assert query == {"success": ["1"], "return_to": [path]}
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "Max-Age=604800" in response.headers.get_list("set-cookie")[0]
    assert len(created) == 1
    complete = client.get(response.headers["location"])
    assert complete.status_code == 200
    assert all(secret not in complete.text for secret in (popup_state, "private-google-code", "secret-id-token", created[0][2]))
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    replay = client.get("/auth/google/callback", params={"state": popup_state, "code": "private-google-code"})
    assert parse_qs(urlsplit(replay.headers["location"]).query)["error"] == ["invalid_state"]


@pytest.mark.parametrize("state", ["p." + STATE, "p.malformed"])
def test_popup_invalid_state_still_signals_safe_failure_without_exchange(client, state):
    response = client.get("/auth/google/callback", params={"state": state, "code": "private-google-code"})
    destination = urlsplit(response.headers["location"])
    assert destination.path == "/auth/google/complete"
    assert parse_qs(destination.query) == {"success": ["0"], "return_to": ["/review/app"], "error": ["invalid_state"]}


def test_popup_cancelled_login_preserves_deep_link_and_has_no_session_cookie(monkeypatch, client):
    path = "/review/app?tab=daily-brief&brief=" + str(uuid4())
    client.cookies.set(hub_auth.STATE_COOKIE, BROWSER)
    monkeypatch.setattr(hub_auth, "_consume_state", lambda *_: {"workspace_id": "workspace", "return_to": path})
    response = client.get("/auth/google/callback", params={"state": "p." + STATE, "error": "private-provider-error"})
    assert parse_qs(urlsplit(response.headers["location"]).query) == {
        "success": ["0"], "return_to": [path], "error": ["cancelled"],
    }
    assert all(not cookie.startswith(hub_auth.SESSION_COOKIE + "=") for cookie in response.headers.get_list("set-cookie"))
    page = client.get(response.headers["location"])
    assert 'data-error="cancelled"' in page.text and "auth_error=cancelled" in page.text
    assert "private-provider-error" not in page.text


def test_popup_completion_has_exact_script_csp_and_no_token_payload(client):
    page = client.get("/auth/google/complete?success=1&return_to=/review/app?tab=daily-brief")
    script = re.search(r"<script>(.*?)</script>", page.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "'sha256-" + digest + "'" in csp
    assert "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
    assert page.headers["cross-origin-opener-policy"] == "same-origin-allow-popups"
    assert page.headers["referrer-policy"] == "no-referrer" and page.headers["cache-control"] == "no-store"
    assert "postMessage(message, window.location.origin)" in script and "window.close()" in script
    assert "window.location.replace(destination)" in script
    assert "token" not in script and "google_sub" not in page.text
    assert client.get("/review/app").headers["cross-origin-opener-policy"] == page.headers["cross-origin-opener-policy"]


def test_popup_completion_escapes_deep_link_and_rejects_external_fallback(client):
    hostile = '/review/app?x="><script>alert(1)</script>'
    page = client.get("/auth/google/complete", params={"success": "1", "return_to": hostile})
    assert page.text.count("<script>") == 1 and "&lt;script&gt;" in page.text
    external = client.get("/auth/google/complete", params={"success": "1", "return_to": "https://evil.example"})
    assert 'href="/review/app"' in external.text and "evil.example" not in external.text
    error = client.get("/auth/google/complete", params={"error": '"><script>alert(1)</script>'})
    assert 'data-error="unavailable"' in error.text and "alert(1)" not in error.text


def test_forged_popup_success_page_does_not_authenticate(client):
    assert client.get("/auth/google/complete?success=1").status_code == 200
    assert client.get("/auth/session").json() == {"mode": "google", "authenticated": False}
    assert client.get("/review/workspace").status_code == 401


def test_unauthenticated_session_probe_cannot_erase_new_popup_login_cookie(monkeypatch, client):
    # Both a probe sent before login and one carrying a stale cookie can finish
    # after the callback rotates the browser cookie. Neither response may delete it.
    assert "set-cookie" not in client.get("/auth/session").headers
    client.cookies.set(hub_auth.SESSION_COOKIE, COOKIE)
    monkeypatch.setattr(hub_auth, "_read_session", lambda _: None)
    stale = client.get("/auth/session")
    assert stale.json()["authenticated"] is False and "set-cookie" not in stale.headers
