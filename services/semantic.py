"""Optional semantic ranker; lexical retrieval remains the mandatory fallback.

pgvector is opt-in and never installed by startup. Vectors are transient; durable
facts remain the source of truth, so corrections cannot leave stale embeddings.
"""
import asyncio
import json
import os
from typing import Protocol

from openai import AsyncOpenAI
from sqlalchemy import text

from config import get_settings
from services.telemetry import observe
from database import engine


class SemanticRanker(Protocol):
    async def rank(self, query: str, candidates: list[dict]) -> list[int]: ...


class PgvectorRanker:
    async def rank(self, query, candidates):
        settings = get_settings()
        model = os.getenv("OPENAI_EMBEDDING_MODEL", "").strip()
        if not settings.openai_api_key or not model or not candidates:
            return []
        async with asyncio.timeout(10):
            async with AsyncOpenAI(api_key=settings.openai_api_key, timeout=8, max_retries=0) as client:
                response = await observe("openai", model, "embedding", client.embeddings.create(model=model,
                    input=[query[:2000]] + [f"{c['key']}: {c['value']}"[:4000] for c in candidates[:100]]))
            vectors = [item.embedding for item in response.data]
            if len(vectors) != len(candidates[:100]) + 1:
                raise ValueError("Unexpected embedding count")
            params = {"query": json.dumps(vectors[0])}
            values = []
            for i, (candidate, vector) in enumerate(zip(candidates[:100], vectors[1:])):
                values.append(f"(:id{i}, CAST(:v{i} AS vector))")
                params[f"id{i}"] = candidate["id"]
                params[f"v{i}"] = json.dumps(vector)
            async with engine.async_session_factory() as db:
                result = await db.execute(text("SELECT id FROM (VALUES " + ",".join(values) +
                    ") AS candidates(id, embedding) WHERE embedding <=> CAST(:query AS vector) < 0.45 "
                    "ORDER BY embedding <=> CAST(:query AS vector) LIMIT 12"), params)
                return list(result.scalars())


async def semantic_ids(query, candidates, ranker=None):
    if os.getenv("ENABLE_SEMANTIC_MEMORY", "false").lower() != "true":
        return []
    try:
        return await (ranker or PgvectorRanker()).rank(query, candidates)
    except Exception:
        # Missing extension, credentials, incompatible model, and provider outage
        # all retain the deterministic lexical path without blocking startup.
        return []
