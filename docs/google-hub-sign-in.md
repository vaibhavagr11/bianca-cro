# Google sign-in for the War Room Hub

The Hub can authenticate approved Bianca team members with their Google Workspace accounts. Google verifies the account; the Hub maps it to the existing `RG`, `NC`, `NDL`, or `MG` reviewer and keeps the same reviewer permissions and attribution.

This feature is off by default. Deploying the code does not enable Google sign-in until the database migration, Google OAuth client, and Render settings below are ready.

## Sign-in and session behavior

- Sign-in starts at `/auth/google/login` and returns through `/auth/google/callback`.
- The backend checks Google's identity token, verified email, Workspace domain, and the explicit `HUB_GOOGLE_USERS` mapping. Being in the company domain alone does not grant access.
- The first successful sign-in binds the approved email and reviewer to Google's permanent account ID (`sub`). A different Google account cannot inherit access merely by receiving that email address later.
- The browser receives an opaque, secure, HttpOnly session cookie. The database stores a hash of its value, not the usable cookie. No `SESSION_SECRET` is required.
- `HUB_SESSION_HOURS=12` gives each new session a **12-hour absolute lifetime from sign-in**. Activity and page refreshes do not extend it. Closing and reopening the browser can preserve it until that deadline, subject to the browser retaining cookies.
- `/auth/session` supplies the current signed-in identity. `/auth/logout` ends the current Hub session. Logging out of the Hub does not sign the user out of Google.
- After expiry, the user signs in again through Google. An existing Google login may avoid another password prompt; Google controls any password or additional verification challenge.

The Hub session is separate from Google's identity-token expiry. This implementation requests only `openid email`; it does not request profile, Gmail, Calendar, or Drive access and does not need offline Google access to keep a Hub session alive. See Google's [OpenID Connect guide](https://developers.google.com/identity/openid-connect/openid-connect) and [identity-token validation requirements](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token).

## 1. Create the Google OAuth client

Use a Google Cloud project controlled by Bianca, preferably under Bianca's Google Workspace organization.

1. Open **Google Cloud Console → Google Auth Platform** for that project.
2. Configure **Branding** with the name “Bianca War Room Hub,” an appropriate support address, and a maintained developer contact address.
3. Configure **Audience** as **Internal** when the project belongs to Bianca's organization and all intended accounts belong to it. If Internal is unavailable, use **External** and complete the applicable testing/publishing setup. The Hub's domain check and explicit approved-user mapping still apply in either case. Do not use Personal mode, which supports only its creator.
4. In **Data Access**, select only the identity scopes used by this implementation: `openid` and `email`.
5. In **Clients**, create an OAuth client of type **Web application**.
6. Add this exact **Authorized redirect URI** for production:

   ```text
   https://bianca-cro-api.onrender.com/auth/google/callback
   ```

7. Save the client ID and client secret securely for the Render settings below. Do not commit the secret to GitHub.

This is a server-side redirect flow, so it does not require an Authorized JavaScript origin. The callback must match Google's registered URI exactly, including scheme, hostname, path, and trailing-slash behavior. Use a separate OAuth client and an exact callback on a stable HTTPS staging hostname for staging. Avoid dynamically changing preview URLs for sign-in testing.

Internal availability depends on the project's organization, not its app name or the email used to create it. A Workspace administrator may need to allow the OAuth client if the organization restricts apps. Google's [audience setup](https://support.google.com/cloud/answer/15544987?hl=en), [web-server OAuth instructions](https://developers.google.com/identity/protocols/oauth2/web-server), and [Workspace administrative controls](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview) describe these settings.

## 2. Prepare the database

Run the repository's migration runner against the intended deployment database, from `backend/` in the release that includes this feature:

```sh
uv run python -m scripts.migrate --hub-auth --status
uv run python -m scripts.migrate --hub-auth
uv run python -m scripts.migrate --hub-auth --status
```

Confirm `022_hub_google_auth.sql` is applied. It creates `hub_google_identities`, `hub_sessions`, and `hub_oauth_states`. The dedicated `--hub-auth` flag includes this otherwise optional migration. A fresh installation that also needs the existing War Room schema can combine `--hub-auth --sheet-review`.

The runner applies **all pending eligible migrations**, not only migration 022. Review the status output before applying it. Do not add `--seed` during this rollout.

Session and OAuth state records use the existing database. No new Render service, Redis instance, local disk, or session-signing secret is required. Database-backed state also lets the sign-in redirect finish after a request is routed to a different application instance.

## 3. Configure the Render service

Select the existing `bianca-cro-api` web service in Render and open **Environment**. Set the following variables; retain all existing application settings.

| Variable | Value or purpose |
| --- | --- |
| `HUB_GOOGLE_AUTH_ENABLED` | `false` during preparation; `true` when ready to enable Google sign-in. |
| `HUB_GOOGLE_CLIENT_ID` | The production Web application OAuth client ID. |
| `HUB_GOOGLE_CLIENT_SECRET` | That client's secret; store only as a protected Render setting. |
| `HUB_GOOGLE_DOMAIN` | The approved accounts' actual Google Workspace hosted-domain claim (`hd`). The code defaults to `bianca-ai.com`; use that value only if the Workspace administrator confirms it matches Google's claim. |
| `HUB_GOOGLE_USERS` | JSON mapping each approved Google account email to its existing reviewer code. |
| `HUB_SESSION_HOURS` | `12`, the absolute lifetime in hours for new Hub sessions. Accepts whole hours from 1 to 168. Changing it does not extend already-issued sessions. |
| `PUBLIC_BASE_URL` | `https://bianca-cro-api.onrender.com`, without a trailing path. The callback is derived from this canonical public address. |

The mapping has this format; the address below is illustrative and must be replaced with a confirmed team account:

```json
{"approved.person@bianca-ai.com":"RG"}
```

Add approved accounts using the exact Google account email returned by Google in lowercase and the correct `RG`, `NC`, `NDL`, or `MG` code. Each reviewer can be mapped only once. This assignment determines whose Hub permissions and action attribution the account receives. Do not substitute a mailing list, guessed address, or a second alias for an existing account.

The email suffix and Workspace hosted domain are separate checks. A Workspace account may use a secondary email domain while Google returns a different `hd` value. Confirm the actual hosted domain with the Workspace administrator; do not infer it from mailbox branding or from `@bianca-ai.com` alone. A correctly allowlisted email is still denied if `HUB_GOOGLE_DOMAIN` differs from its verified `hd`. Using External audience does not bypass that check or admit personal Gmail accounts automatically.

Google Hub sign-in also requires `REVIEW_ENABLED=true` and the existing review/Sheet configuration. Preserve `REVIEW_TOKENS`, `INGEST_TOKEN`, Sales MCP settings, and `BRIEF_ACTION_SECRET`: they serve existing integrations or signed email actions and are not replaced by this browser sign-in change.

Save configuration without deploying while preparing it, or use the appropriate deployment action when ready. **Save only** does not update the running service. **Save and deploy** applies settings to the existing build; **Save, rebuild, and deploy** builds the code again. See [Render environment settings](https://render.com/docs/configure-environment-variables).

## 4. Deploy and enable

1. Test the feature on staging with its own OAuth client, canonical HTTPS URL, and test database.
2. Merge the implementation into the branch configured for the Render service. If Render deploys `master`, the changes must reach `master` before the production build can contain them.
3. Deploy the new build with `HUB_GOOGLE_AUTH_ENABLED=false`. Apply migration 022 and add the production OAuth configuration before enabling login. Keep the existing health-check path available.
4. Set `HUB_GOOGLE_AUTH_ENABLED=true` and deploy the configuration change.
5. Open the Hub and complete the checks below with a real approved account and a separate unapproved account.

The current Render HTTPS hostname can be used directly; a new custom domain is not required. Render handles public TLS, and the application uses `PUBLIC_BASE_URL` for the callback. Keep that URL and Google's authorized callback aligned. See [Render web services](https://render.com/docs/web-services).

### Acceptance checks

- An approved account signs in and sees the same reviewer-specific content and permitted actions as before.
- An unlisted account is denied, including an account in the correct Workspace domain.
- An account with a different Workspace `hd` claim is denied, even if its email is on the allowlist. An approved account using a secondary email domain succeeds when its verified `hd` matches `HUB_GOOGLE_DOMAIN`.
- Refreshing or reopening the Hub retains an unexpired session. Expiry is fixed at 12 hours after sign-in rather than renewed by activity.
- Logout prevents reuse of that session; the browser returns to sign-in.
- Removing an account from the mapping and deploying the change prevents further authenticated requests with its existing session.
- Hub write actions succeed from the normal UI and reject requests without the required request-forgery protection.
- Existing n8n ingestion/builds, permitted bearer-token clients, Sales MCP access, and signed email actions still work.

Use an accelerated session duration only in a separate test configuration or test suite when checking expiry. Do not change production's duration merely to run the check.

## Access removal and recovery

Removing an email from `HUB_GOOGLE_USERS` takes effect after the updated Render configuration deploys; subsequent authenticated requests check the current mapping. This prevents new sign-ins and rejects that user's existing session while the account is absent from the mapping.

For durable immediate revocation, a database administrator can disable the corresponding `hub_google_identities` record through `disabled_at` and revoke its `hub_sessions` records through `revoked_at`. Session checks enforce both identity disablement and session revocation. There is no administrator screen for this in the initial implementation. Use the database's normal approved administration procedure; do not expose a public revocation endpoint or share session-cookie values.

If a person's Google account is replaced or their email is reassigned, the existing account binding intentionally blocks automatic reuse. Verify the intended person and reviewer, revoke the old sessions, and perform a deliberate administrator recovery of the identity binding before enabling the new account.

Disabling a Google Workspace account alone does not instantly expire an already-issued Hub session. Disable Hub access as part of offboarding as described above.

## Rollback and troubleshooting

- **Return to legacy token sign-in:** set `HUB_GOOGLE_AUTH_ENABLED=false` and deploy. Keep the existing `REVIEW_TOKENS` available. Leave migration 022 and its tables in place; they are additive and do not need to be removed to roll back the feature.
- **`redirect_uri_mismatch`:** compare the exact callback derived from `PUBLIC_BASE_URL` with the Google client's authorized redirect URI.
- **`org_internal`:** the selected Google account is outside the Cloud project's organization. Check project ownership/audience and the intended team account; changing the Hub allowlist alone cannot resolve this Google-side restriction.
- **Approved account denied:** check the actual Google email, `hd` domain, mapping format/reviewer code, and whether the identity is disabled or already bound to a different Google account.
- **Login returns to an error:** confirm the deployment has migration 022, the correct client secret, the production client ID, and the expected canonical URL. Start a fresh login if the one-time login state has expired.
- **Sessions lost during a deployment:** confirm the deployment still points to the same intended database and has the same approved-account mapping. Durable session state belongs in the database, not Render's ephemeral filesystem. See [Render deployment storage behavior](https://render.com/docs/deploys).

Use error categories and request IDs for troubleshooting. Do not paste client secrets, OAuth authorization codes, identity tokens, or session-cookie values into GitHub issues or logs.
