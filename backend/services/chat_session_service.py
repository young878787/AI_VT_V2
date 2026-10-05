"""Chat session selection, single-writer ownership, and persistence coordination."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

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
        async with self._guard:
            if (self._invalidation_event is not None
                    and (session_id is None or session_id == self._writer_session_id)):
                self._invalidation_event.set()
                acknowledgement = self._invalidation_ack
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
        snapshot = await self.repository.activate_for_test(session_id)
        async with self._guard:
            self._writer_session_id = snapshot["session_id"]
        return snapshot
