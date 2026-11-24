"""Google identity and revocable browser sessions for the hosted War Room Hub.

Google tokens never reach the frontend or the database. Session and OAuth-state
cookies are opaque, host-only and hashed at rest. MCP and n8n auth stay separate.
"""

import base64
import hashlib
import hmac
import html
import logging
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg import Error as DatabaseError
from psycopg.rows import dict_row

from app import db
from app.config import settings
from app.ingest import _workspace_id

router = APIRouter(prefix="/auth", tags=["Hub sign-in"])
log = logging.getLogger("bianca_cro.hub_auth")
SESSION_COOKIE = "__Host-bianca-hub"
STATE_COOKIE = "__Host-bianca-hub-oauth"
STATE_SECONDS = 600
ACTORS = frozenset({"RG", "NC", "NDL", "MG"})
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
_jwks = jwt.PyJWKClient(JWKS_URL, timeout=5, lifespan=300)
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_OAUTH_STATE = re.compile(r"^(?:p\.)?[A-Za-z0-9_-]{43}$")
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL_LOCAL = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
AUTH_ERRORS = frozenset({"cancelled", "invalid_state", "not_allowed", "identity_mismatch", "unavailable", "invalid_identity"})
# This script is constant: all variable values are escaped HTML attributes, and
# the CSP authorizes this exact script by hash. No Google code/token/state enters
# the completion page. The opener always rechecks /auth/session for identity.
POPUP_SCRIPT = """(() => {
  const page = document.body;
  const message = {type: 'bianca-hub-auth-complete', success: page.dataset.success === 'true'};
  if (page.dataset.error) message.error = page.dataset.error;
  const destination = document.getElementById('return-link').getAttribute('href');
  let notified = false;
  try {
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage(message, window.location.origin);
      notified = true;
    }
  } catch (_) {}
  if (notified) {
    window.close();
    window.setTimeout(() => window.location.replace(destination), 150);
  } else {
    window.location.replace(destination);
  }
})();"""


class OAuthAccessLogFilter(logging.Filter):
    """Uvicorn otherwise logs one-time authorization codes in request targets."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn access records: client, method, target, HTTP version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            target = record.args[2]
            if isinstance(target, str) and target.split("?", 1)[0].rstrip("/") == "/auth/google/callback":
                record.args = (*record.args[:2], "/auth/google/callback", *record.args[3:])
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, OAuthAccessLogFilter) for item in logger.filters):
        logger.addFilter(OAuthAccessLogFilter())


class LoginError(Exception):
    """Only these fixed codes may cross the OAuth callback boundary."""

    def __init__(self, code: str):
        self.code = code


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _proof(secret: str, purpose: str) -> str:
    digest = hmac.new(secret.encode("ascii"), purpose.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _valid_email(email: str) -> bool:
    local, separator, domain = email.partition("@")
    return bool(len(email) <= 254 and email == email.strip().lower() and separator
                and _EMAIL_LOCAL.fullmatch(local) and not local.startswith(".")
                and not local.endswith(".") and ".." not in local and _DOMAIN.fullmatch(domain))


def _configuration() -> tuple[str, dict[str, str]]:
    if not settings.review_enabled or not settings.hub_google_auth_enabled:
        raise HTTPException(404, "Google Hub sign-in is not enabled")
    try:
        base = urlsplit(settings.public_base_url)
    except ValueError:
        raise HTTPException(503, "Google Hub sign-in is not configured") from None
    domain = settings.hub_google_domain
    users = settings.hub_google_users
    # All callback URLs and origin checks derive from trusted deployment config,
    # never Host / forwarded request headers. Production HTTPS is mandatory.
    if (base.scheme != "https" or not base.hostname or base.username or base.password
            or base.path not in ("", "/") or base.query or base.fragment
            or not settings.hub_google_client_id or not settings.hub_google_client_id.strip()
            or not settings.hub_google_client_secret
            or not settings.hub_google_client_secret.get_secret_value().strip()
            or not _DOMAIN.fullmatch(domain) or not users
            or len(set(users.values())) != len(users)
            or any(actor not in ACTORS or not _valid_email(email) for email, actor in users.items())):
        raise HTTPException(503, "Google Hub sign-in is not configured")
    return f"{base.scheme}://{base.netloc}", users


def _return_to(value: str) -> str:
    if len(value) > 2048 or "\\" in value or any(ord(c) < 32 for c in value):
        return "/review/app"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/review/app"
    if parsed.scheme or parsed.netloc or parsed.path != "/review/app" or parsed.fragment:
        return "/review/app"
    return value


def _error_destination(return_to: str, error: str) -> str:
    parsed = urlsplit(_return_to(return_to))
    query = [(name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True) if name != "auth_error"]
    query.append(("auth_error", error if error in AUTH_ERRORS else "unavailable"))
    return "/review/app?" + urlencode(query)


def _callback_result(*, popup: bool, return_to: str, error: str | None = None) -> RedirectResponse:
    if popup:
        query = {"success": "0" if error else "1", "return_to": _return_to(return_to)}
        if error:
            query["error"] = error if error in AUTH_ERRORS else "unavailable"
        destination = "/auth/google/complete?" + urlencode(query)
    else:
        destination = _error_destination(return_to, error) if error else _return_to(return_to)
    return RedirectResponse(destination, status_code=303, headers=NO_STORE)


def _cookie(response, name: str, value: str, seconds: int) -> None:
    response.set_cookie(name, value, max_age=seconds, secure=True, httponly=True,
                        samesite="lax", path="/")


def _clear_cookie(response, name: str) -> None:
    response.delete_cookie(name, secure=True, httponly=True, samesite="lax", path="/")


def _database_unavailable() -> HTTPException:
    # Never log the exception: a connection error may contain database credentials.
    log.error("Hub sign-in database is unavailable")
    return HTTPException(503, "Hub sign-in is temporarily unavailable")


def _new_state(state: str, browser: str, return_to: str) -> None:
    with db.connect() as conn:
        workspace = _workspace_id(conn)
        # Bounded lifetime; normal sign-in also removes abandoned states.
        conn.execute("delete from hub_oauth_states where expires_at <= now()")
        conn.execute("insert into hub_oauth_states (state_hash, browser_hash, workspace_id, return_to, expires_at)"
                     " values (%s, %s, %s, %s, now() + interval '10 minutes')",
                     (_hash(state), _hash(browser), workspace, return_to))


def _consume_state(state: str, browser: str) -> dict | None:
    with db.connect() as conn:
        workspace = _workspace_id(conn)
        conn.row_factory = dict_row
        # DELETE ... RETURNING atomically makes the callback single-use, across
        # workers and deploys. A wrong browser cannot consume someone else's state.
        return conn.execute("delete from hub_oauth_states where state_hash = %s and browser_hash = %s"
                            " and workspace_id = %s and expires_at > now() returning workspace_id, return_to",
                            (_hash(state), _hash(browser), workspace)).fetchone()


def _exchange_code(code: str, verifier: str, origin: str) -> str:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        response = client.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "client_id": settings.hub_google_client_id,
            "client_secret": settings.hub_google_client_secret.get_secret_value(),
            "redirect_uri": origin + "/auth/google/callback", "code_verifier": verifier,
        })
        response.raise_for_status()
        payload = response.json()
        token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or len(token) > 16384:
            raise LoginError("invalid_identity")
        return token


def _verify_identity(token: str, nonce: str) -> dict:
    key = _jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=settings.hub_google_client_id,
                        issuer=ISSUERS, options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce",
                                                            "email", "email_verified", "hd"],
                                                  "strict_aud": True})
    if (not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255
            or type(claims["exp"]) is not int or type(claims["iat"]) is not int
            or claims["email_verified"] is not True
            or claims["hd"] != settings.hub_google_domain
            or not isinstance(claims["email"], str)
            or not isinstance(claims["nonce"], str)
            or not hmac.compare_digest(claims["nonce"], nonce)
            or ("azp" in claims and claims["azp"] != settings.hub_google_client_id)):
        raise LoginError("invalid_identity")
    email = claims["email"].lower()
    actor = settings.hub_google_users.get(email)
    if actor not in ACTORS:
        raise LoginError("not_allowed")
    return {"google_sub": claims["sub"], "email": email, "actor": actor}


def _create_session(workspace, identity: dict, token: str, previous_token: str | None) -> None:
    with db.connect() as conn:
        conn.row_factory = dict_row
        # ON CONFLICT deliberately never changes a binding. An address reassigned
        # to a different Google subject, or a subject moved to a new actor, must
        # receive explicit administrative recovery rather than inherited access.
        conn.execute("insert into hub_google_identities (workspace_id, google_sub, email, actor)"
                     " values (%s, %s, %s, %s) on conflict do nothing",
                     (workspace, identity["google_sub"], identity["email"], identity["actor"]))
        bound = conn.execute("select google_sub, actor, disabled_at from hub_google_identities"
                             " where workspace_id = %s and email = %s for update",
                             (workspace, identity["email"])).fetchone()
        if (not bound or bound["google_sub"] != identity["google_sub"] or bound["actor"] != identity["actor"]
                or bound["disabled_at"] is not None):
            raise LoginError("identity_mismatch")
        conn.execute("update hub_google_identities set last_login_at = now() where workspace_id = %s and google_sub = %s",
                     (workspace, identity["google_sub"]))
        if previous_token and _OPAQUE.fullmatch(previous_token):
            conn.execute("update hub_sessions set revoked_at = now() where token_hash = %s and revoked_at is null",
                         (_hash(previous_token),))
        conn.execute("insert into hub_sessions (token_hash, workspace_id, google_sub, expires_at)"
                     " values (%s, %s, %s, now() + %s * interval '1 hour')",
                     (_hash(token), workspace, identity["google_sub"], settings.hub_session_hours))
        conn.execute("delete from hub_sessions where expires_at <= now()")


def _read_session(token: str) -> dict | None:
    with db.connect() as conn:
        workspace = _workspace_id(conn)
        conn.row_factory = dict_row
        return conn.execute("select i.google_sub, i.email, i.actor, s.expires_at from hub_sessions s"
                            " join hub_google_identities i using (workspace_id, google_sub)"
                            " where s.token_hash = %s and s.workspace_id = %s and s.revoked_at is null"
                            " and s.expires_at > now() and i.disabled_at is null",
                            (_hash(token), workspace)).fetchone()


def _revoke_session(token: str) -> None:
    with db.connect() as conn:
        conn.execute("update hub_sessions set revoked_at = now() where token_hash = %s and revoked_at is null",
                     (_hash(token),))


def require_session(request: Request, *, optional: bool = False) -> dict | None:
    origin, users = _configuration()
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        session = _read_session(token) if _OPAQUE.fullmatch(token) else None
        if session and users.get(session["email"]) != session["actor"]:
            _revoke_session(token)
            session = None
    except DatabaseError:
        raise _database_unavailable() from None
    if session is None:
        if optional:
            return None
        raise HTTPException(401, "Sign in to your Bianca workspace")
    # Cookie authentication is accepted only on same-origin browser requests.
    # Safe navigations need no Origin; cross-origin fetches must never receive data.
    request_origin = request.headers.get("origin")
    if ((request_origin is not None and request_origin != origin)
            or request.headers.get("sec-fetch-site") == "cross-site"):
        raise HTTPException(403, "Origin is not allowed")
    csrf = _proof(token, "hub-csrf-v1")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("x-csrf-token", "")
        if request_origin != origin or not _OPAQUE.fullmatch(supplied) or not hmac.compare_digest(supplied, csrf):
            raise HTTPException(403, "Refresh the page before making this change")
    return {**session, "csrf_token": csrf}


@router.get("/session")
def session(request: Request) -> JSONResponse:
    if not settings.review_enabled:
        raise HTTPException(404, "Sales Pipeline Updates is not enabled")
    if not settings.hub_google_auth_enabled:
        return JSONResponse({"mode": "token", "authenticated": False}, headers=NO_STORE)
    current = require_session(request, optional=True)
    if current is None:
        # Discovery is read-only. A delayed pre-login popup/focus probe must not
        # delete the new cookie set by a callback that completed in the meantime.
        # Expired/revoked cookies remain unusable; explicit logout clears them.
        return JSONResponse({"mode": "google", "authenticated": False}, headers=NO_STORE)
    return JSONResponse({"mode": "google", "authenticated": True, "actor": current["actor"],
                         "email": current["email"], "expires_at": current["expires_at"].isoformat(),
                         "csrf_token": current["csrf_token"]}, headers=NO_STORE)


@router.get("/google/login")
def google_login(return_to: str = "/review/app", popup: bool = False) -> RedirectResponse:
    origin, _ = _configuration()
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    if popup:
        # The mode is covered by the server-stored state hash. Existing state
        # records and ordinary redirect sign-in remain compatible, without a
        # schema change or relying on arbitrary callback query parameters.
        state = "p." + state
    verifier = _proof(browser, "pkce:" + state)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    try:
        _new_state(state, browser, _return_to(return_to))
    except DatabaseError:
        raise _database_unavailable() from None
    response = RedirectResponse(AUTHORIZATION_URL + "?" + urlencode({
        "client_id": settings.hub_google_client_id, "redirect_uri": origin + "/auth/google/callback",
        "response_type": "code", "scope": "openid email", "state": state,
        "nonce": _proof(browser, "nonce:" + state), "hd": settings.hub_google_domain,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    }), status_code=302, headers=NO_STORE)
    _cookie(response, STATE_COOKIE, browser, STATE_SECONDS)
    return response


@router.get("/google/callback")
def google_callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    popup = state.startswith("p.")
    return_to = "/review/app"
    try:
        origin, _ = _configuration()
        browser = request.cookies.get(STATE_COOKIE, "")
        if not _OAUTH_STATE.fullmatch(state) or not _OPAQUE.fullmatch(browser):
            raise LoginError("invalid_state")
        flow = _consume_state(state, browser)
        if flow is None:
            raise LoginError("invalid_state")
        return_to = _return_to(flow["return_to"])
        if error:
            raise LoginError("cancelled")
        if not code or len(code) > 4096:
            raise LoginError("invalid_identity")
        token = _exchange_code(code, _proof(browser, "pkce:" + state), origin)
        identity = _verify_identity(token, _proof(browser, "nonce:" + state))
        session_token = secrets.token_urlsafe(32)
        _create_session(flow["workspace_id"], identity, session_token, request.cookies.get(SESSION_COOKIE))
        response = _callback_result(popup=popup, return_to=return_to)
        _cookie(response, SESSION_COOKIE, session_token, settings.hub_session_hours * 3600)
    except LoginError as exc:
        response = _callback_result(popup=popup, return_to=return_to, error=exc.code)
    except jwt.PyJWTError:
        response = _callback_result(popup=popup, return_to=return_to, error="invalid_identity")
    except (DatabaseError, httpx.HTTPError, HTTPException, ValueError, TypeError, KeyError):
        # Provider / database responses and exception messages must not be echoed
        # into URLs, logs, or the frontend. The user can safely restart sign-in.
        response = _callback_result(popup=popup, return_to=return_to, error="unavailable")
    _clear_cookie(response, STATE_COOKIE)
    return response


@router.get("/google/complete", response_class=HTMLResponse)
def google_complete(success: bool = False, error: str = "", return_to: str = "/review/app") -> HTMLResponse:
    """A same-origin popup signal; it conveys no authentication authority or data."""
    safe_error = error if error in AUTH_ERRORS else "unavailable"
    destination = _return_to(return_to) if success else _error_destination(return_to, safe_error)
    script_hash = base64.b64encode(hashlib.sha256(POPUP_SCRIPT.encode("utf-8")).digest()).decode("ascii")
    headers = {**NO_STORE,
               "Content-Security-Policy": "default-src 'none'; script-src 'sha256-" + script_hash + "'; "
                                          "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
               "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
               "X-Content-Type-Options": "nosniff"}
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Returning to War Room</title></head>'
            f'<body data-success="{str(success).lower()}" data-error="{safe_error if not success else ""}">'
            '<p>Returning to the War Room Hub…</p>'
            f'<p><a id="return-link" href="{html.escape(destination, quote=True)}">Continue to War Room</a></p>'
            f'<script>{POPUP_SCRIPT}</script></body></html>')
    return HTMLResponse(body, headers=headers)


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    require_session(request)
    try:
        _revoke_session(request.cookies[SESSION_COOKIE])
    except DatabaseError:
        raise _database_unavailable() from None
    response = JSONResponse({"mode": "google", "authenticated": False}, headers=NO_STORE)
    _clear_cookie(response, SESSION_COOKIE)
    return response
