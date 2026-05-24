"""Embeddings client.

Single provider (Google Gemini) called via httpx. Lives at the app root rather
than under `agent/` because both ingestion (raw input → candidate query) and
the manual task-create endpoint embed text.

Returns `None` when no API key is configured so the rest of the pipeline can
fall back to keyword-only search during local dev.
"""

from __future__ import annotations

from typing import Literal

import httpx

from app.config import get_settings

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Gemini's embedding endpoint accepts up to ~2k tokens; we cap on chars
# defensively. Embedding quality saturates well before this anyway.
MAX_INPUT_CHARS = 8000

# Task types as accepted by Gemini's embedContent endpoint. We use
# SEMANTIC_SIMILARITY for both stored and query vectors so they're directly
# comparable.
TaskType = Literal[
    "SEMANTIC_SIMILARITY",
    "RETRIEVAL_QUERY",
    "RETRIEVAL_DOCUMENT",
    "CLASSIFICATION",
    "CLUSTERING",
]


async def embed(text: str, *, task_type: TaskType = "SEMANTIC_SIMILARITY") -> list[float] | None:
    """Embed a single string. Returns None if the provider isn't configured."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return None

    payload_text = (text or "").strip()
    if not payload_text:
        return None
    if len(payload_text) > MAX_INPUT_CHARS:
        payload_text = payload_text[:MAX_INPUT_CHARS]

    url = f"{_GEMINI_BASE}/{settings.embedding_model}:embedContent"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            params={"key": settings.gemini_api_key},
            json={
                "content": {"parts": [{"text": payload_text}]},
                "taskType": task_type,
                "outputDimensionality": settings.embedding_dim,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    vector = data["embedding"]["values"]
    if len(vector) != settings.embedding_dim:
        raise ValueError(
            f"Embedding length {len(vector)} does not match configured dim "
            f"{settings.embedding_dim}; check EMBEDDING_MODEL / EMBEDDING_DIM."
        )
    return vector
