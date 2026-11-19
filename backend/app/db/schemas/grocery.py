import uuid
from datetime import date, datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

# In the order of a walk through a typical store; the shopping list groups by it.
Category = Literal[
    "produce", "bakery", "dairy", "meat_fish", "dry_goods", "canned",
    "spices_baking", "frozen", "drinks", "snacks", "household", "other",
]
CATEGORIES: tuple[str, ...] = get_args(Category)
# Amount and portion share one unit, so using a portion is a subtraction.
Unit = Literal["g", "ml", "piece", "pack", "can", "jar", "bottle", "bunch"]
UNITS: tuple[str, ...] = get_args(Unit)
Origin = Literal["manual", "agent", "staple"]


class GroceryItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_key: str
    name: str
    quantity: float
    unit: Unit
    portion: float
    category: Category
    origin: Origin
    staple: bool
    created_at: datetime


class StorageItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_key: str
    name: str
    quantity: float
    unit: Unit
    portion: float
    category: Category
    expires_on: date
    added_at: datetime
    staple: bool


class GroceryState(BaseModel):
    items: list[GroceryItemRead]
    storage: list[StorageItemRead]


class GroceryItemCreate(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class GroceryItemUpdate(BaseModel):
    quantity: float | None = Field(default=None, gt=0)
    category: Category | None = None


class StorageItemUpdate(BaseModel):
    expires_on: date


class StapleUpdate(BaseModel):
    staple: bool
