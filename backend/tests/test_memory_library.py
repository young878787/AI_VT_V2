"""記憶圖書館短期 session 契約與管理入口的純單元測試。"""

import pathlib
import sys
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.memory_router import _ensure_local_management
from domain.emotion_state import NEUTRAL_EMOTION_STATE
from backend.tests.chat_session_fakes import FakeChatSessionRepository
from services.memory_library import MemoryLibraryService


class MemoryLibrarySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_list_detail_and_delete_are_owner_scoped(self):
        chat = FakeChatSessionRepository(
            [{"role": "user", "content": "我喜歡茶"}], "使用者喜歡茶", "session_123",
        )
        chat.emotion_state = dict(NEUTRAL_EMOTION_STATE)
        library = MemoryLibraryService(SimpleNamespace(pool=None, scope=None), chat)
        listed = await library.sessions()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["session_id"], "session_123")
        self.assertTrue(listed[0]["has_summary"])
        self.assertTrue(listed[0]["has_emotion_state"])
        detail = await library.session("session_123")
        self.assertEqual(detail["messages"][0]["content"], "我喜歡茶")
        result = await library.delete_session("session_123")
        self.assertEqual(result["deleted_sessions"], 1)
        self.assertEqual(await library.sessions(), [])
        self.assertIsNone(await library.session("session_123"))
        with self.assertRaises(ValueError):
            await library.delete_session("../unsafe")

    async def test_management_endpoints_default_to_loopback(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        _ensure_local_management(local)
        with self.assertRaises(HTTPException) as context:
            _ensure_local_management(SimpleNamespace(client=SimpleNamespace(host="192.0.2.1")))
        self.assertEqual(context.exception.status_code, 403)

    async def test_delete_all_sessions_removes_owner_sessions(self):
        chat = FakeChatSessionRepository([{"role": "user", "content": "一"}])
        library = MemoryLibraryService(SimpleNamespace(pool=None, scope=None), chat)
        result = await library.delete_all_sessions()
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(await library.sessions(), [])


if __name__ == "__main__":
    unittest.main()
