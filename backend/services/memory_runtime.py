"""PostgreSQL 長期記憶的啟停與 Chat 邊界。"""

import asyncio
from uuid import UUID

from domain.memory_routing import route_memory
from domain.memory_settings import load_memory_settings
from infrastructure.memory_database import check_schema, make_pool
from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from infrastructure.memory_repository import MemoryRepository
from services.memory_db_manager import MemoryDBManager
from services.memory_llm import MemoryLLM
from services.memory_retriever import MemoryRetriever
from services.memory_worker import MemoryWorker


class MemoryRuntime:
    def __init__(self, pool, repository, embedding, llm, retriever, worker) -> None:
        self.pool = pool
        self.repository = repository
        self.embedding = embedding
        self.llm = llm
        self.retriever = retriever
        self.worker = worker
        self._route_tasks: set[asyncio.Task] = set()

    @classmethod
    async def create(cls) -> "MemoryRuntime":
        settings = load_memory_settings()
        pool = await make_pool(settings.database_url)
        try:
            await check_schema(pool, settings.scope)
            embedding = MemoryEmbeddingClient(settings)
            await embedding.embed("memory startup dimension check")
            repository = MemoryRepository(pool, settings.scope)
            llm = MemoryLLM(settings)
            manager = MemoryDBManager(pool, settings.scope, settings.memory_model)
            retriever = MemoryRetriever(repository, embedding)
            worker = MemoryWorker(repository, embedding, llm, manager)
            return cls(pool, repository, embedding, llm, retriever, worker)
        except Exception:
            await pool.close()
            raise

    def start(self) -> None:
        self.worker.start()

    async def close(self) -> None:
        await self.worker.stop()
        if self._route_tasks:
            await asyncio.gather(*self._route_tasks, return_exceptions=True)
        await self.embedding.client.close()
        await self.llm.client.close()
        await self.pool.close()

    async def accept(self, session_id: str, turn_id: str) -> UUID:
        return await self.repository.accept(session_id, turn_id)

    def route_background(self, event_id: UUID, text: str, answers: object,
                         recent_dialogue: list[dict]) -> None:
        routing = route_memory(text, answers)

        async def save_route() -> None:
            try:
                embedding = None
                if routing.route in {"buffer", "process"}:
                    try:
                        embedding = await self.embedding.embed(text, query=True)
                    except Exception as exc:
                        print(f"[Memory] route embedding unavailable: {type(exc).__name__}")
                await self.repository.route(event_id, routing, text, recent_dialogue, embedding)
            except Exception as exc:
                print(f"[Memory] route persistence error: {type(exc).__name__}")

        task = asyncio.create_task(save_route())
        self._route_tasks.add(task)
        task.add_done_callback(self._route_tasks.discard)

    async def retrieve(self, text: str) -> tuple[dict, str]:
        return await self.retriever.retrieve(text)

    async def reset(self) -> None:
        await self.repository.reset()
