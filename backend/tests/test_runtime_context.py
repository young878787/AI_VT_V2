"""現況時間、接入、預算與原生工作取消的回歸測試。"""
import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import NEUTRAL_EMOTION_STATE
from domain.runtime_context import desktop_access_allowed, local_time, make_turn_snapshot, project_snapshot, result
from infrastructure.windows_desktop import DesktopReader, WindowsDesktop
from services.chat_service import build_chat_context, estimate_token_count
from services.context_tools import ContextTools


class RuntimeContextTests(unittest.TestCase):
    def test_message_timestamp_is_fixed_across_midnight(self):
        timestamp = local_time().replace(year=2026, month=10, day=9, hour=23, minute=59, second=59).timestamp()
        prompt = build_agent_a_prompt({}, "", NEUTRAL_EMOTION_STATE, message_timestamp=timestamp)
        self.assertIn("2026-10-09 23:59:59", prompt)
        self.assertIn("星期五", prompt)
        self.assertIn("2026-10-10 00:00:01", build_agent_a_prompt(
            {}, "", NEUTRAL_EMOTION_STATE, message_timestamp=timestamp + 2))

    def test_origin_and_peer_checks_do_not_trust_loopback_name_suffix(self):
        with patch.dict(os.environ, {"FRONTEND_PORT": "5287"}):
            for host, origin, expected in (
                ("127.0.0.1", "http://localhost:5287", True),
                ("::1", "http://[::1]:5287", True),
                ("127.0.0.1", "http://localhost.evil:5287", False),
                ("127.0.0.1", "http://localhost:5173", False),
                ("192.168.1.1", "http://localhost:5287", False),
                ("127.0.0.1", "", False),
                ("127.0.0.1", "http://localhost:5287/x", False),
            ):
                with self.subTest(host=host, origin=origin):
                    socket = SimpleNamespace(client=SimpleNamespace(host=host), headers={"origin": origin})
                    self.assertEqual(desktop_access_allowed(socket), expected)

    def test_unavailable_snapshot_never_reuses_old_app(self):
        snapshot = make_turn_snapshot("new", 0, result("unavailable", reason="timeout"))
        self.assertIsNone(snapshot["desktop_captured_at"])
        self.assertIsNone(snapshot["foreground"]["app"])
        self.assertEqual(snapshot["open_apps"]["status"], "unavailable")

    def test_context_counts_snapshot_and_tool_schemas_without_mutating_history(self):
        history = [{"role": "assistant", "content": "歷史" * 500}]
        projection = project_snapshot(make_turn_snapshot("t", 0, result("unavailable", reason="timeout")))
        schemas = ContextTools().schemas
        context = build_chat_context("規則", history, "原始問題", 1024,
                                     runtime_context=projection, tools=schemas)
        import json
        schema_cost = estimate_token_count([{"role": "system", "content": json.dumps(schemas, ensure_ascii=False)}])
        self.assertLessEqual(estimate_token_count(context) + schema_cost, 1024)
        self.assertEqual(context[-1], {"role": "user", "content": "原始問題"})
        self.assertEqual(history, [{"role": "assistant", "content": "歷史" * 500}])
        self.assertNotIn("本輪暫時桌面", context[0]["content"])

    def test_screenshot_does_not_expand_missing_foreground_to_screen(self):
        desktop = object.__new__(WindowsDesktop)
        desktop.user32 = Mock()
        desktop._available = Mock(return_value=None)
        desktop.user32.GetForegroundWindow.return_value = None
        with patch("PIL.ImageGrab.grab") as grab:
            self.assertEqual(desktop.screenshot()["reason"], "target_unavailable")
            grab.assert_not_called()

    def test_changed_foreground_discards_image(self):
        from PIL import Image
        desktop = object.__new__(WindowsDesktop)
        desktop.user32 = Mock()
        desktop._available = Mock(return_value=None)
        desktop.user32.GetForegroundWindow.side_effect = [1, 2]
        desktop.user32.IsIconic.return_value = False
        with patch("PIL.ImageGrab.grab", return_value=Image.new("RGB", (10, 10), "white")) as grab:
            receipt = desktop.screenshot()
            self.assertEqual(receipt["reason"], "changed_during_capture")
            self.assertNotIn("image", receipt)
            grab.assert_called_once_with(window=1)

    def test_long_app_list_is_trimmed_by_complete_items(self):
        snapshot = make_turn_snapshot("t", 0, result("ok", {
            "foreground": {"status": "ok", "app": "Code.exe"},
            "open_apps": {"status": "ok", "apps": ["長名稱" * 25 + str(i) for i in range(6)], "truncated": False},
        }))
        context = build_chat_context("規則", [], "問題", runtime_context=project_snapshot(snapshot))
        import json
        body = json.loads(context[-2]["content"].split("\n", 1)[1])
        self.assertEqual(body["foreground"]["app"], "Code.exe")
        self.assertTrue(body["open_apps"]["truncated"])
        self.assertLessEqual(estimate_token_count([context[-2]]), 300)
        self.assertEqual(len(snapshot["open_apps"]["apps"]), 6)

    def test_fallback_estimator_does_not_count_image_base64_as_text(self):
        with patch("services.chat_service._encoding", None):
            self.assertEqual(estimate_token_count([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * 100000}}
            ]}]), 4100)


class DesktopCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_failure_is_sanitized_and_reader_can_be_reused(self):
        desktop = SimpleNamespace(snapshot=Mock(side_effect=[KeyError("private-path"), result("ok", {})]))
        reader = DesktopReader(desktop)
        try:
            receipt = await reader.read("snapshot")
            self.assertEqual(receipt["reason"], "desktop_read_failed")
            self.assertNotIn("private-path", str(receipt))
            self.assertEqual((await reader.read("snapshot"))["status"], "ok")
        finally:
            reader.close()

    async def test_cancellation_does_not_queue_more_native_work(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
            return result("ok", {})
        desktop = SimpleNamespace(snapshot=Mock(side_effect=slow))
        reader = DesktopReader(desktop)
        try:
            first = asyncio.create_task(reader.read("snapshot", timeout=1))
            await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            for _ in range(10):
                self.assertEqual((await reader.read("snapshot"))["reason"], "source_busy")
            self.assertEqual(desktop.snapshot.call_count, 1)
        finally:
            release.set()
            reader.close()

    async def test_remote_snapshot_makes_no_native_call(self):
        reader = SimpleNamespace(read=Mock())
        snapshot = await ContextTools(reader=reader).snapshot("remote", 0)
        reader.read.assert_not_called()
        self.assertEqual(snapshot["foreground"]["reason"], "local_access_required")
