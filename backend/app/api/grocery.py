"""Grocery list and storage. Every endpoint answers with the whole state —
both lists are short, and the client never patches one locally.

  * GET    /grocery                        — shopping list + storage
  * POST   /grocery/items                  — {text}: a manual entry ("2 kg rice")
  * PATCH  /grocery/items/{id}             — {quantity?, category?}
  * DELETE /grocery/items/{id}
  * POST   /grocery/items/{id}/cross-off   — bought: moves it into storage
  * POST   /grocery/items/{id}/uncross     — undo a cross-off
  * PATCH  /grocery/storage/{id}           — {expires_on}
  * POST   /grocery/storage/{id}/use       — one portion used
  * DELETE /grocery/storage/{id}
  * PUT    /grocery/products/{key}/staple  — {staple}
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agent.grocery.products import ProductLookupError
from app.db import get_session
from app.db.schemas.grocery import (
    GroceryItemCreate,
    GroceryItemUpdate,
    GroceryState,
    StapleUpdate,
    StorageItemUpdate,
)
from app.services.grocery import lifecycle

router = APIRouter(prefix="/grocery", tags=["grocery"])


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProductLookupError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("", response_model=GroceryState)
def get_state(session: Session = Depends(get_session)) -> GroceryState:
    return lifecycle.state(session)


@router.post("/items", response_model=GroceryState, status_code=status.HTTP_201_CREATED)
async def add_item(body: GroceryItemCreate, session: Session = Depends(get_session)) -> GroceryState:
    with _http_errors():
        await lifecycle.add_entry(session, body.text)
    return lifecycle.state(session)


@router.patch("/items/{item_id}", response_model=GroceryState)
def update_item(
    item_id: uuid.UUID, body: GroceryItemUpdate, session: Session = Depends(get_session)
) -> GroceryState:
    with _http_errors():
        lifecycle.update_item(session, item_id, quantity=body.quantity, category=body.category)
    return lifecycle.state(session)


@router.delete("/items/{item_id}", response_model=GroceryState)
def delete_item(item_id: uuid.UUID, session: Session = Depends(get_session)) -> GroceryState:
    with _http_errors():
        lifecycle.delete_item(session, item_id)
    return lifecycle.state(session)


@router.post("/items/{item_id}/cross-off", response_model=GroceryState)
def cross_off(item_id: uuid.UUID, session: Session = Depends(get_session)) -> GroceryState:
    with _http_errors():
        lifecycle.cross_off(session, item_id)
    return lifecycle.state(session)


@router.post("/items/{item_id}/uncross", response_model=GroceryState)
def uncross(item_id: uuid.UUID, session: Session = Depends(get_session)) -> GroceryState:
    with _http_errors():
        lifecycle.uncross(session, item_id)
    return lifecycle.state(session)


@router.patch("/storage/{storage_id}", response_model=GroceryState)
def update_storage(
    storage_id: uuid.UUID, body: StorageItemUpdate, session: Session = Depends(get_session)
) -> GroceryState:
    with _http_errors():
        lifecycle.set_expiry(session, storage_id, body.expires_on)
    return lifecycle.state(session)


@router.post("/storage/{storage_id}/use", response_model=GroceryState)
def use_portion(storage_id: uuid.UUID, session: Session = Depends(get_session)) -> GroceryState:
    with _http_errors():
        lifecycle.use_portion(session, storage_id)
    return lifecycle.state(session)


@router.delete("/storage/{storage_id}", response_model=GroceryState)
def remove_storage(storage_id: uuid.UUID, session: Session = Depends(get_session)) -> GroceryState:
    with _http_errors():
        lifecycle.remove(session, storage_id)
    return lifecycle.state(session)


@router.put("/products/{product_key}/staple", response_model=GroceryState)
def set_staple(
    product_key: str, body: StapleUpdate, session: Session = Depends(get_session)
) -> GroceryState:
    with _http_errors():
        lifecycle.set_staple(session, product_key, body.staple)
    return lifecycle.state(session)
