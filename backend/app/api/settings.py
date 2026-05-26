from fastapi import APIRouter
from pydantic import BaseModel

from app import state

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsRead(BaseModel):
    auto_poll_enabled: bool


class SettingsUpdate(BaseModel):
    auto_poll_enabled: bool | None = None


@router.get("", response_model=SettingsRead)
async def get_settings() -> SettingsRead:
    return SettingsRead(auto_poll_enabled=state.auto_poll_enabled)


@router.patch("", response_model=SettingsRead)
async def update_settings(payload: SettingsUpdate) -> SettingsRead:
    if payload.auto_poll_enabled is not None:
        state.auto_poll_enabled = payload.auto_poll_enabled
    return SettingsRead(auto_poll_enabled=state.auto_poll_enabled)
