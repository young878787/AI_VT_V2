import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.chat_ws import websocket_endpoint
from api.routes.memory_router import _runtime
from infrastructure.chat_session_repository import ChatSessionRepository
from infrastructure.in_memory_chat_session_repository import InMemoryChatSessionRepository
from services.chat_session_service import ChatSessionService
from services import memory_runtime as memory_runtime_module
from services.memory_runtime import DisabledMemoryRuntime, MemoryRuntime, open_memory_backend
from backend.tests.test_emotion_chat_ws import FakeWebSocket, jev_answers


DB_ENV = {
    "MEMORY_DB_ENABLED": "true",
    "MEMORY_DATABASE_URL": "postgresql://localhost:5432/ai_vt_memory",
    "MEMORY_DEFAULT_USER_ID": "00000000-0000-0000-0000-000000000001",
    "MEMORY_DEFAULT_CHARACTER_ID": "00000000-0000-0000-0000-000000000002",
    "MEMORY_DATABASE_SCHEMA": "memory",
}


def fake_pool():
    return SimpleNamespace(wait=AsyncMock(), close=AsyncMock())


class MemoryBackendTierTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_flag_skips_database_and_uses_volatile_sessions(self):
        with patch.dict("os.environ", {"MEMORY_DB_ENABLED": "false"}), \
                patch.object(memory_runtime_module, "make_pool", new_callable=AsyncMock) as make_pool:
            runtime, repository = await open_memory_backend()
        make_pool.assert_not_awaited()
        self.assertIsInstance(runtime, DisabledMemoryRuntime)
        self.assertIsInstance(repository, InMemoryChatSessionRepository)

    async def test_unreachable_database_degrades_to_volatile_sessions(self):
        pool = fake_pool()
        pool.wait.side_effect = TimeoutError("pool initialization incomplete")
        with patch.dict("os.environ", DB_ENV), \
                patch.object(memory_runtime_module, "make_pool", new=AsyncMock(return_value=pool)):
            runtime, repository = await open_memory_backend()
        pool.close.assert_awaited()
        self.assertIsNone(runtime.pool)
        self.assertIsInstance(repository, InMemoryChatSessionRepository)

    async def test_missing_database_settings_degrade_without_connecting(self):
        with patch.dict("os.environ", {"MEMORY_DB_ENABLED": "true", "MEMORY_DATABASE_URL": ""}), \
                patch.object(memory_runtime_module, "make_pool", new_callable=AsyncMock) as make_pool:
            runtime, repository = await open_memory_backend()
        make_pool.assert_not_awaited()
        self.assertIsInstance(runtime, DisabledMemoryRuntime)
        self.assertIsInstance(repository, InMemoryChatSessionRepository)

    async def test_memory_failure_keeps_database_sessions(self):
        pool = fake_pool()
        with patch.dict("os.environ", DB_ENV), \
                patch.object(memory_runtime_module, "make_pool", new=AsyncMock(return_value=pool)), \
                patch.object(memory_runtime_module, "check_schema", new_callable=AsyncMock), \
                patch.object(MemoryRuntime, "create", new=AsyncMock(side_effect=RuntimeError("MEMORY_AI_API_KEY 未設定"))):
            runtime, repository = await open_memory_backend()
        self.assertIsInstance(runtime, DisabledMemoryRuntime)
        self.assertIsInstance(repository, ChatSessionRepository)
        pool.close.assert_not_awaited()
        await runtime.close()
        pool.close.assert_awaited_once()

    async def test_full_memory_is_used_when_available(self):
        pool = fake_pool()
        full = object()
        with patch.dict("os.environ", DB_ENV), \
                patch.object(memory_runtime_module, "make_pool", new=AsyncMock(return_value=pool)), \
                patch.object(memory_runtime_module, "check_schema", new_callable=AsyncMock), \
                patch.object(MemoryRuntime, "create", new=AsyncMock(return_value=full)):
            runtime, repository = await open_memory_backend()
        self.assertIs(runtime, full)
        self.assertIsInstance(repository, ChatSessionRepository)

    async def test_disabled_reset_clears_only_short_term_session(self):
        repository = InMemoryChatSessionRepository()
        snapshot = await repository.get_or_create_active()
        await repository.append_user(snapshot["session_id"], 0, {"role": "user", "content": "hi", "turn_id": "t1"})
        state = await DisabledMemoryRuntime().reset_chat_and_memory(repository, snapshot["session_id"])
        self.assertEqual(state["generation"], 1)
        self.assertEqual(repository.messages, [])


class DisabledMemoryRouteTests(unittest.TestCase):
    def test_memory_library_reports_disabled_long_term_memory(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(memory_runtime=DisabledMemoryRuntime())))
        with self.assertRaises(HTTPException) as raised:
            _runtime(request)
        self.assertEqual(raised.exception.status_code, 503)

    def test_chat_turn_completes_without_long_term_memory(self):
        socket = FakeWebSocket([{"content": "你好"}])
        runtime = DisabledMemoryRuntime()
        runtime.accept = AsyncMock()
        runtime.retrieve = AsyncMock()
        runtime.route_background = Mock()
        socket.app = SimpleNamespace(state=SimpleNamespace(
            memory_runtime=runtime,
            chat_session_service=ChatSessionService(InMemoryChatSessionRepository()),
        ))

        async def fake_chat(messages, send_chunk, **kwargs):
            await send_chunk("露西亞的回覆")
            return "露西亞的回覆"

        async def run():
            with patch("api.routes.chat_ws.call_jev", new=AsyncMock(return_value=jev_answers())), \
                    patch("api.routes.chat_ws.stream_agent_a", side_effect=fake_chat), \
                    patch("api.routes.chat_ws.broadcast_to_displays"), \
                    patch("api.routes.chat_ws.log_turn"), \
                    patch("api.routes.chat_ws.reset_log"), \
                    patch("api.routes.chat_ws.synthesize_and_send_voice"):
                await websocket_endpoint(socket)

        asyncio.run(run())
        types = [payload.get("type") for payload in socket.payloads]
        self.assertIn("session_ready", types)
        self.assertIn("ambient_plan", types)
        self.assertIn("stream_end", types)
        accepted = next(payload for payload in socket.payloads if payload.get("type") == "input_accepted")
        self.assertIsNone(accepted["event_id"])
        runtime.accept.assert_not_awaited()
        runtime.retrieve.assert_not_awaited()
        runtime.route_background.assert_not_called()


if __name__ == "__main__":
    unittest.main()
