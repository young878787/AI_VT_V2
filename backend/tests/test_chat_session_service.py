import asyncio
import pathlib
import sys
import unittest


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from backend.tests.chat_session_fakes import FakeChatSessionRepository
from services.chat_session_service import ChatSessionInUseError, ChatSessionService


class ChatSessionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_writer_can_hold_owner_session(self):
        service = ChatSessionService(FakeChatSessionRepository())
        first = service.writer()
        snapshot = await first.__aenter__()
        self.assertEqual(snapshot["session_id"], "server_session")
        second = service.writer()
        with self.assertRaises(ChatSessionInUseError):
            await second.__aenter__()
        await first.__aexit__(None, None, None)
        async with service.writer() as restored:
            self.assertEqual(restored["session_id"], "server_session")

    async def test_writer_is_released_after_cancellation(self):
        service = ChatSessionService(FakeChatSessionRepository())
        entered = asyncio.Event()

        async def hold():
            async with service.writer():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with service.writer() as snapshot:
            self.assertEqual(snapshot["session_id"], "server_session")

    async def test_management_invalidation_waits_for_writer_acknowledgement(self):
        service = ChatSessionService(FakeChatSessionRepository())
        async with service.writer():
            invalidation = asyncio.create_task(service.invalidate("server_session"))
            await service.invalidation_event.wait()
            self.assertFalse(invalidation.done())
            await service.acknowledge_invalidation()
            await invalidation


if __name__ == "__main__":
    unittest.main()
