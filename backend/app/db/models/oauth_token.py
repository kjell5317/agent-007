import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OAuthToken(Base):
    """Encrypted OAuth credentials for a given provider/account.

    Tokens are stored as Fernet ciphertext; decrypt only at use time.
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        UniqueConstraint("provider", "account_key", name="uq_oauth_tokens_provider_account"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    provider: Mapped[str] = mapped_column(String(64), index=True)
    # TODO: account_key = stable per-provider identifier (email, workspace id, ...)
    account_key: Mapped[str] = mapped_column(String(256))

    access_token_ct: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
