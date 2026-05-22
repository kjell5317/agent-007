from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    token_encryption_key: str = Field(default="", description="Fernet key for encrypting OAuth tokens at rest")

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-7"

    # --- Google OAuth (Gmail) ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/oauth/google/callback"

    # TODO: add MCP server URLs (GitHub, Notion) when wiring those in


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
