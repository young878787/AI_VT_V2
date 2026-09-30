"""獨立的 OpenAI-compatible embedding 路線。"""

import json
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import UUID

from openai import AsyncOpenAI

from domain.memory_embedding import EMBEDDING_PURPOSES, format_embedding_input, normalize_embedding
from domain.memory_settings import MemorySettings


class MemoryEmbeddingClient:
    def __init__(self, settings: MemorySettings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(base_url=settings.embedding_base_url, api_key=settings.embedding_api_key)
        self.diagnostic_writer: Callable[[UUID, dict], Awaitable[None]] | None = None

    async def embed(
        self,
        text: str,
        *,
        query: bool | None = None,
        purpose: str | None = None,
        event_id: UUID | None = None,
        stage: str | None = None,
        job_attempt: int | None = None,
        decision_index: int | None = None,
    ) -> list[float]:
        if purpose is not None:
            if purpose not in EMBEDDING_PURPOSES or event_id is None:
                raise ValueError("Embedding 診斷需要有效 purpose 與 event_id")
            expected_query = EMBEDDING_PURPOSES[purpose] == "query"
            if query is not None and query != expected_query:
                raise ValueError("Embedding purpose 與輸入模式不一致")
            query = expected_query
        query = bool(query)

        started = time.perf_counter()
        dimension = None
        normalized = False
        try:
            response = await self.client.embeddings.create(
                model=self.settings.embedding_serving_model,
                input=format_embedding_input(
                    text, query=query,
                    query_prefix=self.settings.embedding_query_prefix,
                    document_prefix=self.settings.embedding_document_prefix,
                ),
            )
            if len(response.data) != 1:
                raise ValueError("Embedding API 回傳筆數無效")
            raw_vector = response.data[0].embedding
            dimension = len(raw_vector) if isinstance(raw_vector, list) else None
            vector = normalize_embedding(raw_vector, self.settings.embedding_dimension)
            normalized = True
        except Exception as exc:
            await self._record_diagnostic(
                event_id, purpose, query, "failed", dimension, normalized,
                started, type(exc).__name__, stage, job_attempt, decision_index,
            )
            raise

        await self._record_diagnostic(
            event_id, purpose, query, "succeeded", dimension or self.settings.embedding_dimension,
            normalized, started, None, stage, job_attempt, decision_index,
        )
        return vector

    async def _record_diagnostic(
        self,
        event_id: UUID | None,
        purpose: str | None,
        query: bool,
        status: str,
        dimension: int | None,
        normalized: bool,
        started: float,
        error_class: str | None,
        stage: str | None,
        job_attempt: int | None,
        decision_index: int | None,
    ) -> None:
        if event_id is None or purpose is None:
            return
        diagnostic = {
            "event_id": str(event_id),
            "purpose": purpose,
            "input_mode": "query" if query else "document",
            "status": status,
            "model": self.settings.embedding_model,
            "dimension": dimension,
            "normalized": normalized,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_class": error_class,
            "stage": stage,
            "job_attempt": job_attempt,
            "decision_index": decision_index,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        if self.diagnostic_writer is not None:
            try:
                await self.diagnostic_writer(event_id, diagnostic)
            except Exception as exc:
                print(f"[Memory][Embedding] 診斷儲存失敗: {type(exc).__name__}")
        print("[Memory][Embedding] " + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":")))
