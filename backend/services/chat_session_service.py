"""Chat session selection, single-writer ownership, and persistence coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from core.config import CHAT_COMPRESSION_RETRY_INTERVAL_SEC
from infrastructure.chat_session_repository import ChatSessionRepository


class ChatSessionInUseError(RuntimeError):
    pass


class ChatSessionService:
    def __init__(self, repository: ChatSessionRepository) -> None:
        self.repository = repository
        self._guard = asyncio.Lock()
        self._writer_token: str | None = None
        self._writer_session_id: str | None = None
        self._invalidation_event: asyncio.Event | None = None
        self._invalidation_ack: asyncio.Event | None = None
        self._compression_task: asyncio.Task | None = None

    @asynccontextmanager
    async def writer(self):
        token = uuid4().hex
        async with self._guard:
            if self._writer_token is not None:
                raise ChatSessionInUseError("目前對話正在其他連線中使用")
            self._writer_token = token
        try:
            snapshot = await self.repository.get_or_create_active()
            async with self._guard:
                self._writer_session_id = snapshot["session_id"]
                self._invalidation_event = asyncio.Event()
                self._invalidation_ack = asyncio.Event()
            yield snapshot
        finally:
            await self.stop_compression()
            async with self._guard:
                if self._writer_token == token:
                    self._writer_token = None
                    self._writer_session_id = None
                    self._invalidation_event = None
                    self._invalidation_ack = None

    @property
    def invalidation_event(self) -> asyncio.Event | None:
        return self._invalidation_event

    async def invalidate(self, session_id: str | None = None) -> None:
        """Tell the active writer to cancel before a management deletion proceeds."""
        acknowledgement = None
        should_stop_compression = False
        async with self._guard:
            if (self._invalidation_event is not None
                    and (session_id is None or session_id == self._writer_session_id)):
                self._invalidation_event.set()
                acknowledgement = self._invalidation_ack
                should_stop_compression = True
        if should_stop_compression:
            await self.stop_compression()
        if acknowledgement is not None:
            try:
                await asyncio.wait_for(acknowledgement.wait(), timeout=5)
            except TimeoutError as exc:
                raise RuntimeError("活動 Chat 連線未能在期限內停止") from exc

    async def acknowledge_invalidation(self) -> None:
        async with self._guard:
            if self._invalidation_ack is not None:
                self._invalidation_ack.set()

    async def switch_test_session(self, session_id: str) -> dict:
        await self.stop_compression()
        snapshot = await self.repository.activate_for_test(session_id)
        async with self._guard:
            self._writer_session_id = snapshot["session_id"]
        return snapshot

    async def ensure_compression(
        self,
        worker: Callable[[bool], Awaitable[bool]],
        *,
        force: bool = False,
    ) -> asyncio.Task | None:
        """Own one background compression loop for the active writer.

        ``worker`` performs one bounded batch and returns whether it advanced the
        cursor.  Provider/DB failures are retried with a cooldown, while a new
        Chat turn remains independent of this task.
        """
        async with self._guard:
            if self._compression_task is not None and not self._compression_task.done():
                return None
            task = asyncio.create_task(self._run_compression(worker, force))
            self._compression_task = task
            task.add_done_callback(self._compression_done)
            return task

    async def _run_compression(
        self, worker: Callable[[bool], Awaitable[bool]], force: bool,
    ) -> None:
        first = force
        while True:
            try:
                advanced = await worker(first)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The worker has already emitted a bounded diagnostic event.
                # Keep the task alive so an outage can recover without a new input.
                print(f"[Chat compression] retry after {type(exc).__name__}")
                first = False
                await asyncio.sleep(CHAT_COMPRESSION_RETRY_INTERVAL_SEC)
                continue
            if not advanced:
                return
            first = False

    def _compression_done(self, task: asyncio.Task) -> None:
        if self._compression_task is task:
            self._compression_task = None

    async def stop_compression(self) -> None:
        async with self._guard:
            task = self._compression_task
            self._compression_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
