from fastapi import APIRouter
from pydantic import BaseModel

from app import state
from app.config import get_settings as get_app_settings

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsRead(BaseModel):
    auto_poll_enabled: bool
    user_timezone: str


class SettingsUpdate(BaseModel):
    auto_poll_enabled: bool | None = None


@router.get("", response_model=SettingsRead)
async def get_settings() -> SettingsRead:
    return SettingsRead(
        auto_poll_enabled=state.auto_poll_enabled,
        user_timezone=get_app_settings().user_timezone,
    )


@router.patch("", response_model=SettingsRead)
async def update_settings(payload: SettingsUpdate) -> SettingsRead:
    if payload.auto_poll_enabled is not None:
        state.auto_poll_enabled = payload.auto_poll_enabled
    return SettingsRead(
        auto_poll_enabled=state.auto_poll_enabled,
        user_timezone=get_app_settings().user_timezone,
    )
