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
    # Single-tenant for now: every ingest call writes to this workspace.
    workspace_name: str = "Bianca AI"


settings = Settings()
