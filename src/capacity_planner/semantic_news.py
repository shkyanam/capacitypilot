"""Bounded semantic retrieval for filing passages.

Retrieval is advisory evidence only. It does not write to the database or make planning
decisions; the caller retains source citations and existing deterministic controls.
"""

import math
import re
from typing import Any

import httpx

from .config import get_settings

SEMANTIC_QUERY = (
    "Evidence of storage, cloud, data-center, infrastructure, capacity, geographic, "
    "acquisition, or demand expansion that could require additional capacity planning."
)


def chunk_text(text: str, chunk_chars: int) -> list[str]:
    """Split at paragraph/sentence boundaries, retaining bounded source passages."""
    paragraphs = [re.sub(r"\s+", " ", value).strip() for value in text.splitlines()]
    paragraphs = [value for value in paragraphs if value]
    if len(paragraphs) <= 1:
        paragraphs = [value.strip() for value in re.split(r"(?<=[.!?])\s+", text) if value.strip()]
    units = []
    for paragraph in paragraphs:
        if len(paragraph) <= chunk_chars:
            units.append(paragraph)
            continue
        sentences = [
            value.strip() for value in re.split(r"(?<=[.!?])\s+", paragraph) if value.strip()
        ]
        units.extend(sentences if len(sentences) > 1 else [paragraph[:chunk_chars]])
    chunks: list[str] = []
    current = ""
    for unit in units:
        unit = unit[:chunk_chars]
        candidate = f"{current} {unit}".strip()
        if current and len(candidate) > chunk_chars:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("Embedding vectors must have matching non-zero dimensions")
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return 0.0 if denominator == 0 else sum(a * b for a, b in zip(left, right)) / denominator


def _embeddings(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    response = httpx.post(
        f"{settings.nebius_base_url.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {settings.nebius_api_key}"},
        json={"model": settings.nebius_embedding_model, "input": texts},
        timeout=45,
    )
    response.raise_for_status()
    vectors = [item.get("embedding") for item in response.json().get("data", [])]
    if len(vectors) != len(texts) or any(not isinstance(vector, list) for vector in vectors):
        raise ValueError("Embedding response did not contain one vector per input")
    return vectors


def retrieve(text: str) -> list[dict[str, Any]]:
    """Return the top semantically relevant filing passages, or nothing when unavailable."""
    settings = get_settings()
    if not settings.news_semantic_enabled or not settings.nebius_embedding_model:
        return []
    chunks = chunk_text(text, settings.news_semantic_chunk_chars)
    if not chunks:
        return []
    vectors = _embeddings([SEMANTIC_QUERY, *chunks])
    query_vector, chunk_vectors = vectors[0], vectors[1:]
    ranked = sorted(
        (
            {
                "chunk_id": index,
                "excerpt": chunk,
                "semantic_score": round(cosine_similarity(query_vector, vector), 4),
            }
            for index, (chunk, vector) in enumerate(zip(chunks, chunk_vectors), start=1)
        ),
        key=lambda item: item["semantic_score"],
        reverse=True,
    )
    return ranked[: settings.news_semantic_top_k]
