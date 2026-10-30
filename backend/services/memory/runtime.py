"""PostgreSQL 長期記憶的啟停與 Chat 邊界。"""

import asyncio
from uuid import UUID

from core.utils import env_flag
from domain.memory.routing import route_memory
from domain.memory.source import bind_source_generation
from domain.memory.settings import load_memory_database_settings, load_memory_settings
from infrastructure.session.repository import ChatSessionRepository
from infrastructure.session.in_memory_repository import InMemoryChatSessionRepository
from infrastructure.memory.database import check_schema, make_pool
from infrastructure.memory.embedding_client import MemoryEmbeddingClient
from infrastructure.memory.repository import MemoryRepository
from services.memory.db_manager import MemoryDBManager
from services.memory.events import publish_memory_event
from services.memory.llm import MemoryLLM
from services.memory.retriever import MemoryRetriever
from services.memory.worker import MemoryWorker


DATABASE_CONNECT_TIMEOUT_SEC = 5.0


class MemoryRuntime:
    long_term_enabled = True

    def __init__(self, pool, repository, embedding, llm, retriever, worker) -> None:
        self.pool = pool
        self.repository = repository
        self.embedding = embedding
        self.llm = llm
        self.retriever = retriever
        self.worker = worker
        self._route_tasks: set[asyncio.Task] = set()

    @classmethod
    async def create(cls, pool) -> "MemoryRuntime":
        """在已驗證 schema 的 pool 上啟用長期記憶；失敗時 pool 仍由呼叫端持有。"""
        settings = load_memory_settings()
        embedding = MemoryEmbeddingClient(settings)
        llm = None
        try:
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
            await embedding.client.close()
            if llm is not None:
                await llm.client.close()
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
                     recent_dialogue: list[dict] | None = None,
                     user_message: dict | None = None) -> UUID:
        event_id, generation = await self.repository.accept_event(session_id, turn_id, text)
        if user_message is not None:
            bind_source_generation(user_message, generation)
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
                         recent_dialogue: list[dict], turn_id: str | None = None) -> None:
        routing = route_memory(text, answers)

        async def save_route() -> None:
            try:
                changed = await self.repository.route(event_id, routing, text, recent_dialogue)
                if changed:
                    publish_memory_event(
                        "memory_route_finalized", str(event_id), turn_id or str(event_id),
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

    async def reset_chat_and_memory(self, chat_repository, session_id: str) -> dict[str, int]:
        """Reset long- and short-term owner state in one PostgreSQL transaction."""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self.repository.reset(connection=connection)
                return await chat_repository.reset(session_id, connection=connection)


class DisabledMemoryRuntime:
    """長期記憶停用；Chat 只保留短期 session，pool 若存在仍由此關閉。"""

    long_term_enabled = False
    repository = None

    def __init__(self, pool=None) -> None:
        self.pool = pool

    def start(self) -> None:
        pass

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def reset(self) -> None:
        pass

    async def reset_chat_and_memory(self, chat_repository, session_id: str) -> dict[str, int]:
        return await chat_repository.reset(session_id)


async def open_memory_backend() -> tuple[MemoryRuntime | DisabledMemoryRuntime, object]:
    """依可用程度分級啟動：完整記憶 → 僅 PostgreSQL session → 記憶體 session。"""
    if not env_flag("MEMORY_DB_ENABLED", True):
        print("[Memory] MEMORY_DB_ENABLED=false，長期記憶停用，Chat session 改存記憶體（重啟即清空）")
        return DisabledMemoryRuntime(), InMemoryChatSessionRepository()
    pool = None
    try:
        database_url, scope = load_memory_database_settings()
        pool = await make_pool(database_url)
        await pool.wait(timeout=DATABASE_CONNECT_TIMEOUT_SEC)
        await check_schema(pool, scope)
    except Exception as exc:
        if pool is not None:
            await pool.close()
        print(f"[WARN] PostgreSQL 無法使用（{type(exc).__name__}: {exc}），"
              "長期記憶停用，Chat session 改存記憶體（重啟即清空）")
        return DisabledMemoryRuntime(), InMemoryChatSessionRepository()
    chat_repository = ChatSessionRepository(pool, scope)
    try:
        runtime = await MemoryRuntime.create(pool)
    except Exception as exc:
        print(f"[WARN] 長期記憶初始化失敗（{type(exc).__name__}: {exc}），"
              "長期記憶停用，Chat session 仍保存在 PostgreSQL")
        return DisabledMemoryRuntime(pool), chat_repository
    return runtime, chat_repository
