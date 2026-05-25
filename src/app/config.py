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

    # IANA timezone the user lives in. Used by the event planner to interpret
    # the preferred / extended scheduling windows as wall-clock hours *in your
    # zone*, regardless of what the server's local tz happens to be (typically
    # UTC in containers, which is what produces the "scheduled at 0:42 AM"
    # surprise on a 9:45 due date). Set to `Europe/Berlin` for CEST.
    user_timezone: str = "UTC"

    database_url: str

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

    # --- Labels (task taxonomy + Google Calendar event color) ---
    # Path to the TOML file defining task labels. See `config/labels.toml`
    # for the format. Relative paths resolve from the repo root.
    labels_config_path: str = "config/labels.toml"

    # --- Google Calendar (auto-mirror tasks as events) ---
    # `primary` is the signed-in user's main calendar. Set to a specific
    # calendar ID (e.g. "abc...@group.calendar.google.com") to mirror tasks
    # into a dedicated calendar instead. Empty disables the sync entirely.
    google_calendar_id: str = "primary"
    # Extra calendar IDs the planner should treat as busy when picking a
    # slot (e.g. a shared family calendar). Comma-separated in env:
    #   GOOGLE_BUSY_CALENDAR_IDS=you@gmail.com,family123@group.calendar.google.com
    # The target `google_calendar_id` is always considered busy too — no
    # need to repeat it here.
    google_busy_calendar_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Duration used when a task has no estimation, in minutes.
    google_calendar_default_event_minutes: int = 30

    @field_validator("google_busy_calendar_ids", mode="before")
    @classmethod
    def _parse_calendar_ids(cls, v):
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

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

    # --- Commute planning (Google Maps Distance Matrix + Open-Meteo) ---
    # Home base — full street address, geocoded by Google as part of each
    # Distance Matrix call. Leave empty to disable commute planning entirely.
    home_address: str = ""
    # Google Maps Platform API key with Distance Matrix enabled. Same project
    # as the Calendar OAuth client is fine; the key is an unauth bearer for
    # Maps APIs, not tied to a user.
    google_maps_api_key: str = ""
    # When biking takes longer than this many minutes OR rain probability is
    # at/above `commute_rain_threshold_pct`, the planner switches the trip to
    # public transport.
    commute_bike_max_minutes: int = 25
    commute_rain_threshold_pct: int = 30
    # How far ahead to plan commutes (in days). One week matches the calendar
    # sync window so we don't pull farther than we look.
    commute_lookahead_days: int = 7
    # "Home is worth returning to" threshold. If event A → home → event B fits
    # with this much buffer at home, the planner sends you home in between;
    # otherwise it routes A → B directly.
    commute_home_layover_minutes: int = 60
    # Slack on each side of a physical event: arrive this many minutes before
    # `start` (outbound) and depart this many minutes after `end` (inbound).
    # Keeps commute events from butting flush against the meeting they bracket.
    commute_event_buffer_minutes: int = 5

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

    # --- MCP servers (optional research tools for the agent) ---
    # When both URL and token are set for a server, the agent can call its
    # tools mid-decision to resolve references (e.g. fetch a GitHub issue
    # mentioned by number, or a Notion page mentioned by title) before
    # picking create_task / mark_duplicate / mark_not_task.
    # Leave a pair empty to disable that server.
    github_mcp_url: str = ""
    github_mcp_token: str = ""
    notion_mcp_url: str = ""
    notion_mcp_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
