"""Embeddings for raw inputs.

Two pieces live here together because they're always used as a pair:

  * `candidate_query_text` — pure function that flattens a raw_input's
    content + source metadata into the canonical text we embed. Source-
    agnostic so Gmail and Slack rows cluster on semantic content, not
    on which envelope key carried the sender.
  * `embed` — single-provider (Google Gemini) embedding call via the Haystack
    Google GenAI embedder. Returns `None` when no API key is configured so the
    rest of the pipeline can fall back to keyword-only search during local dev.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from haystack.utils import Secret
from haystack_integrations.components.embedders.google_genai import GoogleGenAITextEmbedder

from app.config import get_settings

log = logging.getLogger(__name__)


# --- Text builder ------------------------------------------------------------


def candidate_query_text(content: str, metadata: dict) -> str:
    """Flatten a raw_input into the canonical text fed to `embed()`."""
    parts: list[str] = []
    sender = _sender_descriptor(metadata)
    if sender:
        parts.append(f"from: {sender}")
    subject = metadata.get("subject")
    if subject:
        parts.append(subject)
    body = (content or "").strip()
    if body:
        parts.append(body[:1500])
    return "\n".join(parts).strip()


def _sender_descriptor(metadata: dict) -> str | None:
    """Source-agnostic 'from' value for the embedding.

    Always labeled `from:` in the query text, regardless of source, so the
    embedding doesn't get a categorical Gmail/Slack split from the key name.
    For Slack we fold the channel into the same line (`alice in #general`)
    since channel context is part of what makes repeated alerts cluster.
    """
    sender = (metadata.get("from") or "").strip() or None
    channel = (metadata.get("channel_name") or "").strip() or None
    if sender and channel:
        return f"{sender} in {channel}"
    return sender or channel


# --- Embedding API client ----------------------------------------------------

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


@lru_cache(maxsize=8)
def _embedder(model: str, api_key: str, task_type: str, dim: int) -> GoogleGenAITextEmbedder:
    # SEMANTIC_SIMILARITY + outputDimensionality mirror the previous raw call
    # exactly, so vectors stay directly comparable to already-stored ones.
    return GoogleGenAITextEmbedder(
        api_key=Secret.from_token(api_key),
        model=model,
        config={"task_type": task_type, "output_dimensionality": dim},
    )


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

    log.debug(
        "gemini embed · model=%s task_type=%s chars=%d dim=%d",
        settings.embedding_model, task_type, len(payload_text), settings.embedding_dim,
    )
    embedder = _embedder(
        settings.embedding_model, settings.gemini_api_key, task_type, settings.embedding_dim
    )
    result = await embedder.run_async(text=payload_text)
    vector = result["embedding"]
    if len(vector) != settings.embedding_dim:
        raise ValueError(
            f"Embedding length {len(vector)} does not match configured dim "
            f"{settings.embedding_dim}; check EMBEDDING_MODEL / EMBEDDING_DIM."
        )
    return vector
