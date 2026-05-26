from fastapi import APIRouter
from pydantic import BaseModel

from app.labels import load_labels

router = APIRouter(prefix="/labels", tags=["labels"])


class LabelRead(BaseModel):
    name: str
    description: str
    color: str


@router.get("", response_model=list[LabelRead])
def list_labels() -> list[LabelRead]:
    """The label catalog the frontend uses for the picker. Order matches
    the TOML file (Python dict preserves insertion order)."""
    return [
        LabelRead(name=label.name, description=label.description, color=label.color)
        for label in load_labels().values()
    ]
