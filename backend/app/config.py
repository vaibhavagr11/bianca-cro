from pydantic import SecretStr
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
    # Treated like Bianca's own domains: threads only among these are skipped (the Kellogg project team).
    extra_internal_domains: list[str] = ["kellogg.northwestern.edu"]
    # Gmail label IDs (e.g. "Label_123") never ingested; backup for the -label: filter in n8n's search.
    excluded_label_ids: list[str] = []


settings = Settings()
