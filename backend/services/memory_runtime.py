"""PostgreSQL 長期記憶的啟停與 Chat 邊界。"""

import asyncio
from uuid import UUID

from domain.memory_routing import route_memory
from domain.memory_settings import load_memory_settings
from infrastructure.memory_database import check_schema, make_pool
from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from infrastructure.memory_repository import MemoryRepository
from services.memory_db_manager import MemoryDBManager
from services.memory_events import publish_memory_event
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
            repository = MemoryRepository(
                pool, settings.scope, settings.embedding_model, settings.embedding_contract,
            )
            embedding.diagnostic_writer = repository.record_embedding_diagnostic
            await embedding.embed("memory startup dimension check")
            llm = MemoryLLM(settings)
            manager = MemoryDBManager(
                pool, settings.scope, settings.memory_model, settings.embedding_model,
                settings.embedding_contract,
            )
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

    async def accept(self, session_id: str, turn_id: str, text: str | None = None,
                     recent_dialogue: list[dict] | None = None) -> UUID:
        event_id = await self.repository.accept(session_id, turn_id)
        if text is not None:
            routing = route_memory(text, None)
            # 明確記住／忘記是政策授權，不必等待 JEV；一般 observe 仍等單次 JEV 決定。
            finalized = routing.route == "none" or routing.explicit_request
            changed = await self.repository.route(event_id, routing, text, recent_dialogue or [],
                                                  finalized=finalized)
            if changed:
                publish_memory_event(
                    "memory_route_finalized" if finalized else "memory_route_pending",
                    str(event_id), str(turn_id), route=routing.route or "process",
                    confidence=routing.confidence, explicit_request=routing.explicit_request,
                )
                if finalized and routing.route != "none":
                    self.worker.wake()
        return event_id

    def route_background(self, event_id: UUID, text: str, answers: object,
                         recent_dialogue: list[dict]) -> None:
        routing = route_memory(text, answers)

        async def save_route() -> None:
            try:
                changed = await self.repository.route(event_id, routing, text, recent_dialogue)
                if changed:
                    publish_memory_event(
                        "memory_route_finalized", str(event_id), str(event_id),
                        route=routing.route or "process", confidence=routing.confidence,
                        explicit_request=routing.explicit_request,
                    )
                self.worker.wake()
            except Exception as exc:
                print(f"[Memory] route persistence error: {type(exc).__name__}")

        task = asyncio.create_task(save_route())
        self._route_tasks.add(task)
        task.add_done_callback(self._route_tasks.discard)

    async def retrieve(self, text: str, event_id: UUID | None = None,
                       recent_dialogue: list[dict] | None = None, summary: str = "") -> tuple[dict, str]:
        return await self.retriever.retrieve(text, event_id=event_id,
                                             recent_dialogue=recent_dialogue, summary=summary)

    async def reset(self) -> None:
        await self.repository.reset()
