"""Serve the browser steps for signing in to the Supabase-backed Claude connector."""

import base64
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.config import Settings

CONSENT_PATH = "/sales/oauth/consent"
SCRIPT_PATH = "/sales/oauth/consent.js"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
AUTHORIZATION_ID = re.compile(r"[A-Za-z0-9_-]{8,256}")
SUPABASE_ISSUER = re.compile(r"https://[a-z0-9]{20}\.supabase\.co/auth/v1")
PUBLISHABLE_KEY = re.compile(r"sb_publishable_[A-Za-z0-9_-]+")
STYLES = """
body { font: 16px/1.5 system-ui, sans-serif; color: #172325; background: #f5f8f7; }
main { max-width: 28rem; margin: 12vh auto 0; padding: 1.5rem; }
label, input { display: block; }
input, button { font: inherit; padding: .6rem .8rem; margin: .25rem 0 .75rem; }
button { cursor: pointer; }
[hidden] { display: none; }
"""
STYLE_HASH = base64.b64encode(hashlib.sha256(STYLES.encode()).digest()).decode("ascii")


def create_sales_consent_router(config: Settings) -> APIRouter:
    issuer = config.sales_mcp_issuer
    key = config.sales_mcp_supabase_publishable_key
    if not issuer or not SUPABASE_ISSUER.fullmatch(issuer) or not key or not PUBLISHABLE_KEY.fullmatch(key):
        raise ValueError("Supabase consent requires the project issuer and a publishable key.")

    origin = f"https://{urlsplit(issuer).netloc}"
    browser_config = html.escape(json.dumps({
        "issuer": issuer,
        "publishableKey": key,
        "clientId": str(config.sales_mcp_supabase_client_id),
        "claudeCallback": CLAUDE_CALLBACK,
        "allowedUserHashes": sorted(
            hashlib.sha256(subject.encode()).hexdigest() for subject in config.sales_mcp_founders
        ),
    }), quote=True)
    script = Path(__file__).with_name("sales_consent.js").read_bytes()
    script_hash = base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            f"form-action 'none'; script-src 'sha256-{script_hash}'; "
            f"style-src 'sha256-{STYLE_HASH}'; connect-src {origin}"
        ),
    }
    router = APIRouter(include_in_schema=False)

    @router.get(CONSENT_PATH)
    def consent_page(request: Request) -> HTMLResponse:
        authorization_ids = request.query_params.getlist("authorization_id")
        if len(authorization_ids) != 1 or not AUTHORIZATION_ID.fullmatch(authorization_ids[0]):
            return HTMLResponse(
                "<!doctype html><title>Bianca Sales</title>"
                "<p>Open Bianca Sales in Claude to start a new sign-in request.</p>",
                status_code=400,
                headers=headers,
            )

        return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Bianca Sales</title>
  <style>{STYLES}</style>
  <script defer src="{SCRIPT_PATH}" integrity="sha256-{script_hash}"></script>
</head>
<body>
  <main data-config="{browser_config}" data-authorization-id="{authorization_ids[0]}">
    <h1>Connect Bianca Sales</h1>
    <p>Use your invited email address to continue to Claude.</p>
    <form id="send-code">
      <label for="email">Email address</label>
      <input id="email" type="email" autocomplete="email" maxlength="254" required>
      <button type="submit">Send code</button>
    </form>
    <form id="verify-code" hidden>
      <label for="code">Email code</label>
      <input id="code" inputmode="numeric" autocomplete="one-time-code"
        pattern="[0-9]{{6,10}}" maxlength="10" required>
      <button type="submit">Verify code</button>
      <button type="button" id="change-email">Use a different email</button>
    </form>
    <section id="consent" hidden>
      <p>Allow Claude to use Bianca Sales to read sales information and cite sources in your chats?</p>
      <p id="scopes"></p>
      <button type="button" id="approve">Allow access</button>
      <button type="button" id="deny">Deny</button>
    </section>
    <p role="status" id="status"></p>
    <noscript>Enable JavaScript and reopen the connector from Claude.</noscript>
  </main>
</body>
</html>""", headers=headers)

    @router.get(SCRIPT_PATH)
    def consent_script() -> Response:
        return Response(script, media_type="text/javascript", headers=headers)

    return router
