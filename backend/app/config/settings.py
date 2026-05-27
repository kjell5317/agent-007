from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # `.env.dev` overrides `.env` when present — pydantic loads the
        # tuple left-to-right, so later files win. Production deploys ship
        # only `.env`; `scripts/dev.sh` ships a `.env.dev` with local
        # overrides (e.g. OAuth redirect URIs pointing at :5173). Missing
        # files are silently skipped.
        env_file=(".env", ".env.dev"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8001
    log_level: str = "INFO"

    database_url: str

    user_timezone: str = "Europe/Berlin"
    home_address: str = ""

    token_encryption_key: str = Field(default="", description="Fernet key for encrypting OAuth tokens at rest")
    session_secret: str = "" # python -c "import secrets; print(secrets.token_urlsafe(32))"

    # LLM
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-7"

    # Embeddings
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 1536

    # Auto- decison theshold
    input_dedup_threshold: float = 0.88

    # Google OAuth
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Gmail & Calendar
    google_oauth_redirect_uri: str = "http://localhost:8001/oauth/google/callback"
    # Login
    google_oauth_login_redirect_uri: str = "http://localhost:8001/auth/callback"
    auth_allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("auth_allowed_emails", mode="before")
    @classmethod
    def _parse_emails(cls, v):
        if isinstance(v, str):
            return [e.strip().lower() for e in v.split(",") if e.strip()]
        return v

    # Labels
    labels_config_path: str = "config/labels.toml"

    # Write calendar
    google_calendar_id: str = "primary"
    google_calendar_default_event_minutes: int = 30
    # Read calendars
    google_busy_calendar_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("google_busy_calendar_ids", mode="before")
    @classmethod
    def _parse_calendar_ids(cls, v):
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    # Commute planning. Disabled by default — the feature is parked while
    # we sort out a TZ/routing reliability issue. Flip to true to re-enable
    # weather refresh, commute events, and the location-driven replan path.
    commute_enabled: bool = False

    # Google Maps
    google_maps_api_key: str = ""
    commute_bike_max_minutes: int = 25
    commute_rain_threshold_pct: int = 30
    commute_lookahead_days: int = 7
    commute_home_layover_minutes: int = 60
    commute_event_buffer_minutes: int = 5

    # Home Assistant
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    home_assistant_notify_service: str = "notify"
    # Shared secret HA must send on action-callback POSTs. Empty disables the check.
    home_assistant_action_secret: str = ""

    # Fallback clickAction for notifications when a task has no `link`.
    task_default_url: str = "https://007.kjellhanken.de"

    # Slack
    slack_apps: dict[str, dict[str, str]] = Field(default_factory=dict)
    slack_oauth_redirect_uri: str = "http://localhost:8001/oauth/slack/callback"
    slack_bootstrap_days: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
