from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # --- Embeddings (hybrid candidate retrieval + input dedup) ---
    # Gemini's gemini-embedding-001 supports configurable output dimensions via
    # outputDimensionality (768 / 1536 / 3072); 1536 matches the existing
    # `tasks.embedding` and `raw_inputs.embedding` columns.
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 1536

    # Cosine similarity at/above which a new raw input inherits the decision of
    # a past raw input verbatim (no LLM call). 0.88 catches near-duplicate
    # automated emails (e.g. repeated security alerts) while leaving genuine
    # follow-ups for the agent. Tune up if you see wrong auto-decisions.
    input_dedup_threshold: float = 0.88

    # --- Google OAuth (Gmail data + login SSO; same client, different redirect URIs) ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/oauth/google/callback"
    # Used by /auth/login (Google SSO). Add to the OAuth client's allowed
    # redirect URIs in Google Cloud Console alongside the Gmail one above.
    google_oauth_login_redirect_uri: str = "http://localhost:8000/auth/callback"

    # --- Auth (Google SSO with email allowlist) ---
    # Comma-separated emails allowed to log in. Empty → auth middleware is
    # disabled entirely (handy for local dev / running tests).
    # `NoDecode` opts out of pydantic-settings' default JSON decode for list
    # fields so our `_parse_emails` validator can handle the plain string.
    auth_allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Secret used by SessionMiddleware to sign cookies. Required when
    # auth_allowed_emails is non-empty. Generate:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    session_secret: str = ""

    @field_validator("auth_allowed_emails", mode="before")
    @classmethod
    def _parse_emails(cls, v):
        if isinstance(v, str):
            return [e.strip().lower() for e in v.split(",") if e.strip()]
        return v

    # --- Home Assistant (push notifications) ---
    # Leave url or token empty to disable notifications outright.
    # `notify_service` is the entity slug after `notify.` — e.g. `notify`,
    # `mobile_app_pixel`, `all_devices`. Whatever you'd write as
    # `service: notify.<this>` in an HA automation.
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    home_assistant_notify_service: str = "notify"

    # --- Slack OAuth ---
    # Slack apps are workspace-scoped, so you need one app per workspace. Each
    # app contributes its own (client_id, client_secret). The redirect URI is
    # global — register the same one under each app's OAuth config.
    #
    # Set via JSON in env:
    #   SLACK_APPS={"primary":{"client_id":"x","client_secret":"y"},
    #               "work":   {"client_id":"a","client_secret":"b"}}
    #
    # On /oauth/slack/authorize, pass `?app=<name>` to pick which one to use.
    # Defaults to the first app when only one is configured.
    slack_apps: dict[str, dict[str, str]] = Field(default_factory=dict)
    slack_oauth_redirect_uri: str = "http://localhost:8000/oauth/slack/callback"

    # Initial-bootstrap recency window for Slack (days). After the first poll,
    # per-conversation watermarks (latest message ts) drive incremental sync.
    slack_bootstrap_days: int = 1

    # TODO: add MCP server URLs (GitHub, Notion) when wiring those in


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
