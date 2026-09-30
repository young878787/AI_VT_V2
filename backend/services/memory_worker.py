"""背景處理 PROCESS job；LLM 與 embedding 呼叫不持有 DB transaction。"""

import asyncio

from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from infrastructure.memory_repository import MemoryRepository
from services.memory_db_manager import MemoryDBManager
from services.memory_llm import MemoryLLM


class MemoryWorker:
    def __init__(self, repository: MemoryRepository, embedding: MemoryEmbeddingClient,
                 llm: MemoryLLM, manager: MemoryDBManager) -> None:
        self.repository = repository
        self.embedding = embedding
        self.llm = llm
        self.manager = manager
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def process_one(self) -> bool:
        job = await self.repository.claim()
        if job is None:
            return False
        try:
            query_embedding = job["embedding"].to_list() if job.get("embedding") is not None else await self.embedding.embed(
                job["source_text"], purpose="memory_match_query", event_id=job["id"],
                stage="worker_fallback", job_attempt=job["attempts"],
            )
            buffered = await self.repository.related_buffers(job["memory_type_hint"], query_embedding)
            related = await self.repository.related_items(job["source_text"], query_embedding)
            decisions = await self.llm.decide(job, buffered, related)
            embeddings = {}
            for index, decision in enumerate(decisions):
                if decision["action"] in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
                    embeddings[index] = await self.embedding.embed(
                        decision["canonical_text"], purpose="memory_document", event_id=job["id"],
                        stage="write", job_attempt=job["attempts"], decision_index=index,
                    )
            await self.manager.apply(
                job, decisions, {item["id"] for item in related}, embeddings,
                tuple(item["id"] for item in buffered),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = "failed" if job["attempts"] >= 3 else "retry"
            await self.repository.finish(job, status, error=type(exc).__name__)
        return True

    async def run(self) -> None:
        elapsed = 0
        while True:
            try:
                worked = await self.process_one()
                if elapsed >= 300:
                    await self.repository.expire_buffers()
                    await self.repository.expire_temporary()
                    for buffer in await self.repository.unembedded_buffers():
                        try:
                            vector = await self.embedding.embed(
                                buffer["source_text"], purpose="buffer_document", event_id=buffer["id"],
                                stage="maintenance",
                            )
                            await self.repository.save_buffer_embedding(buffer, vector)
                        except Exception:
                            break
                    elapsed = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[Memory] worker error: {type(exc).__name__}")
                worked = False
            if not worked:
                await asyncio.sleep(1)
                elapsed += 1
