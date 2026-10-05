"""記憶圖書館短期 session 契約與管理入口的純單元測試。"""

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.memory_router import _ensure_local_management
from domain.emotion_state import NEUTRAL_EMOTION_STATE
from infrastructure.memory_store import (
    delete_session,
    get_session_record,
    list_session_records,
    save_session_emotion_state,
    save_session_messages,
    save_session_summary,
)
from services.memory_library import MemoryLibraryService


class MemoryLibrarySessionTests(unittest.TestCase):
    def test_session_list_detail_and_delete_are_scoped_to_valid_filename(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "infrastructure.memory_store.CHAT_SESSION_DIR", directory + "/sessions"
        ), patch(
            "infrastructure.memory_store.EMOTION_STATE_DIR", directory + "/emotions"
        ):
            save_session_messages("session_123", [{"role": "user", "content": "我喜歡茶"}])
            save_session_summary("session_123", "使用者喜歡茶")
            save_session_emotion_state("session_123", dict(NEUTRAL_EMOTION_STATE))

            listed = list_session_records()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["session_id"], "session_123")
            self.assertEqual(listed[0]["message_count"], 1)
            self.assertTrue(listed[0]["has_summary"])
            self.assertTrue(listed[0]["has_emotion_state"])

            detail = get_session_record("session_123")
            self.assertEqual(detail["messages"][0]["content"], "我喜歡茶")
            self.assertEqual(detail["summary"], "使用者喜歡茶")

            result = delete_session("session_123")
            self.assertEqual(result["deleted_files"], 3)
            self.assertEqual(list_session_records(), [])
            self.assertIsNone(get_session_record("session_123"))

            with self.assertRaises(ValueError):
                delete_session("../unsafe")

    def test_management_endpoints_default_to_loopback(self):
        local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        _ensure_local_management(local)
        with self.assertRaises(HTTPException) as context:
            _ensure_local_management(SimpleNamespace(client=SimpleNamespace(host="192.0.2.1")))
        self.assertEqual(context.exception.status_code, 403)

    def test_delete_all_sessions_only_removes_recognized_sessions(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "infrastructure.memory_store.CHAT_SESSION_DIR", directory + "/sessions"
        ), patch(
            "infrastructure.memory_store.EMOTION_STATE_DIR", directory + "/emotions"
        ):
            save_session_messages("session_123", [{"role": "user", "content": "一"}])
            save_session_messages("session_456", [{"role": "user", "content": "二"}])
            result = MemoryLibraryService.delete_all_sessions()
            self.assertEqual(result["session_count"], 2)
            self.assertEqual(list_session_records(), [])


if __name__ == "__main__":
    unittest.main()
