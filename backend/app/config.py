from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or backend/.env locally)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    # Supabase Session pooler connection string. SecretStr keeps it out of logs and reprs.
    database_url: SecretStr
    # Shared secret n8n sends in the X-Ingest-Token header.
    ingest_token: SecretStr
    # Every ingest call writes to Bianca's workspace.
    workspace_name: str = "Bianca AI"
    # The Kellogg project team: threads only among them and Bianca are about this tool, not sales, so skipped.
    project_team_domains: list[str] = ["kellogg.northwestern.edu"]
    # Gmail label IDs (e.g. "Label_123") never ingested; backup for the -label: filter in n8n's search.
    excluded_label_ids: list[str] = []
    # Claude, used by the email classifier. The SDK also reads ANTHROPIC_API_KEY from the environment.
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    # Needed only when the API key isn't scoped to a workspace (sent as the anthropic-workspace-id header).
    anthropic_workspace_id: str | None = None
    # Only meetings from the notes doc within this many days are stored (testing window).
    notes_window_days: int = 90
    # Brief recipients override, as a JSON list (e.g. ["a@x.com"]). Empty = team members with receives_brief.
    brief_to: list[str] = []
    # "Mark as complete" buttons in the brief: links are signed with this secret; without it the buttons are left out.
    brief_action_secret: SecretStr | None = None
    public_base_url: str = "https://bianca-cro-api.onrender.com"
    # Brief v2: open items up to this many days late count as "needs you today"; older than the stale limit
    # they move to "still open? please confirm" (never hidden).
    brief_today_overdue_days: int = 3
    brief_stale_overdue_days: int = 14
    # "What changed": events this many days before the last brief that reached us after it count as recorded late.
    brief_late_lookback_days: int = 7
    # A promise due within this many days makes its deal time-critical (it is never displaced from the brief).
    brief_critical_ahead_days: int = 3
    # Preferred number of active In Talks deals among the four shown per founder (not a quota).
    brief_in_talks_target: int = 2
    # Pilot deals show only items the extractor classified as deal progression. Items from before that
    # classification existed (blank scope) are shown on other deals until re-extracted, but not on Pilots.
    brief_pilot_strict_scope: bool = True
    # Quiet In Talks deals (no recorded contact in 2+ weeks, nothing else open) come back a few per founder per day,
    # longest-untouched first, and one that was listed is not listed again for the cooldown. 0 turns this off.
    brief_resurface_per_day: int = Field(default=3, ge=0)
    brief_resurface_cooldown_days: int = Field(default=14, ge=0)
    # No table in the brief lists more than this many rows; what is left out is counted on a line under the table.
    brief_max_table_rows: int = Field(default=7, gt=0)
    # Follow-up cadence while a customer has not answered: one "follow up this week" nudge per deal every this many
    # days since our last email, and after this many of our emails in a row with no reply, suggest Paused instead.
    brief_nudge_every_days: int = Field(default=7, gt=0)
    brief_pause_after_emails: int = Field(default=5, gt=0)
    # False switches the time-based reminders off: "keep following up" rows print as written and Waiting rows carry
    # no "follow up this week" or "consider moving to Paused", as before 11/17.
    brief_followup_nudges: bool = True
    # A "(complete by <date>)" note in the Sheet's Next step shows in "Needs you today" from its date until it is
    # this many days late; after that it moves to a one-line reminder to update the Sheet.
    brief_sheet_step_overdue_days: int = Field(default=14, ge=0)
    # False: the Sheet's "(complete by <date>)" notes are not read at all, as before 11/17.
    brief_sheet_dates: bool = True
    # While a dated Sheet step is not due yet, the extracted item that is the same step is held back until that date
    # (never one that is due or overdue by its own date). False: such items keep showing by their own rules.
    brief_sheet_hold_matching: bool = True
    # Turn on after n8n is configured to provide receipts and check ready_to_send.
    brief_verification_required: bool = False
    brief_verification_feeds: list[str] = ["gmail:RG", "gmail:NC", "calendar:RG", "calendar:NC", "notes", "pipeline"]
    brief_verification_sync_hours: int = Field(default=6, gt=0)
    brief_verification_token_budget: int = Field(default=24000, ge=0)
    # Threads classified per /process/classify call.
    classify_batch_threads: int = 10

    # Read-only Claude connector. Founder OAuth subject IDs are keys; values identify Gmail link aliases.
    sales_mcp_enabled: bool = False
    sales_mcp_url: str | None = None
    sales_mcp_issuer: str | None = None
    sales_mcp_jwks_url: str | None = None
    sales_mcp_supabase_client_id: UUID | None = None
    sales_mcp_supabase_publishable_key: str | None = None
    sales_mcp_founders: dict[str, Literal["RG", "NC", "NDL"] | None] = Field(default_factory=dict)
    sales_mcp_database_url: SecretStr | None = None
    sales_mcp_include_pilots: bool = False
    sales_mcp_stale_after_hours: int = Field(default=84, gt=0)

    # Founder review is opt-in and uses separate credentials from ingestion.
    review_enabled: bool = False
    review_tokens: dict[str, SecretStr] = {}
    review_allowed_origins: list[str] = ["https://claude.ai"]
    # Browser-only Google sign-in. MCP reviewer tokens and ingestion stay separate.
    hub_google_auth_enabled: bool = False
    hub_google_client_id: str | None = None
    hub_google_client_secret: SecretStr | None = None
    hub_google_domain: str = "bianca-ai.com"
    hub_google_users: dict[str, str] = Field(default_factory=dict)
    # Absolute lifetime: activity never extends a session.
    hub_session_hours: int = Field(default=12, ge=1, le=168)
    review_spreadsheet_id: str | None = "14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw"
    review_sheet_tab: str = "Pipeline"
    review_sheet_id: int | None = 1869050409
    review_client_context_mode: Literal["append", "replace"] = "append"
    review_google_service_account_json: SecretStr | None = None
    # When set, Sheet reads and writes go through n8n, which holds the Google credential (n8n/sheets-proxy.json).
    review_sheets_webhook_url: str | None = None
    review_sheets_webhook_secret: SecretStr | None = None
    review_build_batch: int = 20
    # Select commercially important, actionable updates after comprehensive extraction. Usually 0–3
    # per column, with no numerical cap when more distinct important items are justified.
    # Requires the optional review selection migration and the existing Anthropic configuration.
    review_priority_selection_enabled: bool = True
    # Uncached selection calls per resumable build request; cached/empty results need no model call.
    review_selection_batch: int = Field(default=2, gt=0)
    # Legacy limits only; ignored when priority selection is enabled. 0 removes a limit.
    review_max_next_steps: int = 4
    review_max_context: int = 0


settings = Settings()
