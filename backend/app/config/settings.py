from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-7"


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

    token_encryption_key: str = Field(
        default="", description="Fernet key for encrypting OAuth tokens at rest"
    )
    session_secret: str = ""  # python -c "import secrets; print(secrets.token_urlsafe(32))"

    # LLM
    llm_provider: str = "anthropic"
    llm_model: str = ""
    anthropic_api_key: str = ""
    # Deprecated compatibility alias. Prefer LLM_MODEL.
    claude_model: str = ""

    # Embeddings
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 1536

    # Auto- decison theshold
    input_dedup_threshold: float = 0.88

    # Notes similarity recency boost. Long half-life → small bias toward
    # recent notes when re-ranking equally-similar hits.
    notes_similarity_half_life_days: float = 300

    # Same decay for raw-input precedent search. The decayed score is what the
    # orchestrator compares against `input_dedup_threshold`, so an old
    # precedent stops auto-deciding and falls through to the agent instead.
    input_similarity_half_life_days: float = 400

    # Search (stage 1 suggest-as-you-type). Shorter e-folding than the precedent
    # searches above: live search wants recent items to surface, not near-flat
    # decay. Note the formula is exp(-age/days), not a true half-life.
    search_recency_half_life_days: float = 100
    search_suggest_limit: int = 8
    # In-process TTL cache over suggest results. Short: the same query re-fires
    # on backspace/retype, so even a few seconds spares the DB per keystroke.
    search_suggest_cache_ttl_seconds: float = 90.0

    # Chat / "ask" mode (stage 2+3). Retrieval-first: each turn injects the top
    # hybrid hits (local + Drive) into the LLM context. `history_messages` caps
    # how many prior turns travel with the request; `max_iterations` bounds the
    # tool loop before a final answer; `drive_timeout` is the per-request Drive
    # federation budget (past it, Drive results are dropped, never blocking).
    search_chat_local_limit: int = 10
    search_chat_drive_limit: int = 5
    search_drive_timeout_seconds: float = 4.0
    search_chat_max_iterations: int = 4
    search_chat_history_messages: int = 5
    # Inputs whose cleaned content is shorter than this (chars) are noise (bare
    # "ok", "thanks", empty replies). They're skipped entirely at ingestion (the
    # preprocessing boundary in `input.create.drain`, kotx excepted) so they're
    # never stored or run through the agent, and the chat retrieval applies the
    # same floor as a defensive filter.
    min_input_chars: int = 20
    # Cap on Drive file text handed to the model by `get_drive_file`.
    search_drive_file_max_chars: int = 6000

    # Calendar semantic lookup (`find_calendar_events` query mode): how many
    # nearest cached events to return, and the minimum cosine similarity a match
    # must clear. Raise the floor for stricter dedup (fewer, more certain hits);
    # lower it for broader recall.
    # Calendar hybrid lookup (`find_calendar_events` query mode): fuses pgvector
    # similarity with Postgres keyword (FTS) ranking via RRF. `match_limit` caps
    # results; `min_similarity` gates the vector side so a far-off cosine can't
    # ride in (keyword matches still surface regardless). The time window — not a
    # decay — handles recency; the tool defaults it to "now" so past events drop.
    calendar_semantic_match_limit: int = 5
    calendar_semantic_min_similarity: float = 0.4

    # Google OAuth
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Gmail & Calendar
    google_oauth_redirect_uri: str = "http://localhost:8001/oauth/google/callback"
    # Health sleep — a separate grant on the same client, health-only scopes
    # (the Health API rejects tokens that also carry Gmail/Calendar scopes).
    google_oauth_health_redirect_uri: str = "http://localhost:8001/oauth/google_health/callback"
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

    # Points-per-estimated-minute awarded automatically when a non-kotx task is
    # completed (e.g. 0.2 → a 30-minute task earns 6 points). 0 disables it.
    # Kotx-linked completions use a fixed 0.1 factor.
    points_task_done_factor: float = 0.2

    # Write calendar
    google_calendar_id: str = "primary"
    google_calendar_default_event_minutes: int = 30
    # Minimum lead before "now" a fresh slot may start at.
    slot_min_lead_minutes: int = 15
    # Google Calendar popup reminder lead. Rides carry the reminder when they
    # precede an event/task; otherwise the event itself does.
    reminder_lead_minutes: int = 15
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
    commute_enabled: bool = True

    # Google Maps
    google_maps_api_key: str = ""
    commute_bike_max_minutes: int = 25
    commute_rain_threshold_pct: int = 30
    # Cached transit/driving durations older than this are re-fetched so
    # timetable changes are eventually picked up. Bike/walking never expire.
    commute_transit_ttl_days: int = 30
    commute_lookahead_days: int = 7
    commute_home_layover_minutes: int = 60
    # Minimum gap between a commute leg and the event/task it serves.
    commute_event_buffer_minutes: int = 5
    # Minimum gap between two events/tasks with no commute at the boundary.
    event_buffer_minutes: int = 15

    # Home Assistant
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    home_assistant_notify_service: str = "notify"
    home_assistant_next_event_entity_id: str = "input_datetime.007"
    # Shared secret HA must send on action-callback POSTs. Empty disables the check.
    home_assistant_action_secret: str = ""

    # Fallback clickAction for notifications when a task has no `link`.
    task_default_url: str = "https://007.kjellhanken.de"

    # Slack
    slack_apps: dict[str, dict[str, str]] = Field(default_factory=dict)
    slack_oauth_redirect_uri: str = "http://localhost:8001/oauth/slack/callback"
    slack_bootstrap_days: int = 1

    # kotx — external coding-agent API proxied under /kotx (see app.api.kotx).
    # Empty base URL or token disables the proxy (it answers 503).
    kotx_base_url: str = ""
    kotx_api_token: str = ""
    # Shared secret for the incoming kotx state webhook (X-Kotx-Signature,
    # HMAC-SHA256 over the raw body). Empty disables the endpoint.
    kotx_webhook_secret: str = ""

    @property
    def effective_llm_provider(self) -> str:
        return self.llm_provider.strip().lower() or "anthropic"

    @property
    def effective_llm_model(self) -> str:
        return self.llm_model.strip() or self.claude_model.strip() or DEFAULT_ANTHROPIC_MODEL


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
