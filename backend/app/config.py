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
    review_spreadsheet_id: str | None = "14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw"
    review_sheet_tab: str = "Pipeline"
    review_sheet_id: int | None = 1869050409
    review_client_context_mode: Literal["append", "replace"] = "append"
    review_google_service_account_json: SecretStr | None = None
    # When set, Sheet reads and writes go through n8n, which holds the Google credential (n8n/sheets-proxy.json).
    review_sheets_webhook_url: str | None = None
    review_sheets_webhook_secret: SecretStr | None = None
    review_build_batch: int = 20


settings = Settings()
