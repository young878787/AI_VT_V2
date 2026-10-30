"""桌面提示必須對應實際模型請求，並保留取消與資料隔離邊界。"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.runtime_context import context_status, make_turn_snapshot, project_snapshot, result, snapshot_context_status
from services.chat.service import build_chat_context, stream_agent_a
from services.context.tools import ContextTools
from backend.tests.test_context_tools import FakeStream, native, rejection, selection
from backend.tests import test_emotion_chat_ws as chat_ws_tests


def desktop_data(app="Code.exe"):
    return {"foreground": {"status": "ok", "app": app},
            "open_apps": {"status": "ok", "apps": [app], "truncated": False}}


class ContextStatusProjectionTests(unittest.TestCase):
    def test_ui_projection_excludes_image_and_unrequested_title(self):
        data = desktop_data()
        data["foreground"].update(title="私人視窗", path="private-path")
        data["open_apps"]["path"] = "private-path"
        receipt = result("ok", data)
        receipt["image"] = b"private-image"
        payload = context_status("desktop", "ready", receipt)
        self.assertNotIn("title", payload["foreground"])
        self.assertNotIn("path", json.dumps(payload))
        self.assertNotIn("image", payload)
        self.assertEqual(context_status("activity", "ready", receipt)["foreground"]["title"], "私人視窗")

    def test_displayed_apps_match_budgeted_request_without_mutating_snapshot(self):
        data = desktop_data()
        data["open_apps"]["apps"] = ["長名稱" * 20 + str(i) for i in range(6)]
        snapshot = make_turn_snapshot("turn", 0, result("ok", data))
        diagnostics = {}
        messages = build_chat_context("規則", [], "問題", runtime_context=project_snapshot(snapshot), diagnostics=diagnostics)
        retained = json.loads(messages[-2]["content"].split("\n", 1)[1])
        self.assertEqual(diagnostics["desktop_context"]["open_apps"], retained["open_apps"])
        self.assertTrue(retained["open_apps"]["truncated"])
        self.assertEqual(len(snapshot["open_apps"]["apps"]), 6)

    def test_budget_omission_and_unavailable_source_are_distinct(self):
        snapshot = make_turn_snapshot("turn", 0, result("unavailable", reason="local_access_required"))
        diagnostics = {}
        build_chat_context("規則", [], "問題", runtime_context=project_snapshot(snapshot), diagnostics=diagnostics)
        self.assertEqual(diagnostics["desktop_context"]["status"], "unavailable")
        self.assertEqual(diagnostics["desktop_context"]["reason"], "local_access_required")
        build_chat_context("規則", [], "問題", 32, runtime_context=project_snapshot(snapshot), diagnostics=diagnostics)
        self.assertEqual(diagnostics["desktop_context"]["status"], "omitted")
        self.assertNotIn("foreground", diagnostics["desktop_context"])
        build_chat_context("規則", [], "問題", diagnostics=diagnostics)
        self.assertNotIn("desktop_context", diagnostics)

    def test_partial_snapshot_keeps_independent_source_statuses(self):
        data = desktop_data()
        data["foreground"] = {"status": "unavailable", "app": None, "reason": "changed_during_capture"}
        payload = snapshot_context_status(make_turn_snapshot("turn", 0, result("ok", data)))
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["foreground"]["status"], "unavailable")
        self.assertEqual(payload["open_apps"]["status"], "ok")


class ContextStatusDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sent_follows_provider_response_not_snapshot_capture(self):
        diagnostics, events = {}, []
        messages = build_chat_context("規則", [], "問題", runtime_context=project_snapshot(
            make_turn_snapshot("turn", 0, result("ok", desktop_data()))), diagnostics=diagnostics)

        async def emit(payload):
            events.append(payload)

        async def create(**kwargs):
            self.assertEqual([event["status"] for event in events], ["sending"])
            self.assertEqual(kwargs["messages"], messages)
            return FakeStream()

        with patch("services.chat.service.chat_create_with_fallback", side_effect=create):
            await stream_agent_a(messages, AsyncMock(), context_diagnostics=diagnostics, on_context_status=emit)
        self.assertEqual([event["status"] for event in events], ["sending", "sent"])

    async def test_request_failure_never_confirms_delivery(self):
        diagnostics = {}
        messages = build_chat_context("規則", [], "問題", runtime_context=project_snapshot(
            make_turn_snapshot("turn", 0, result("ok", desktop_data()))), diagnostics=diagnostics)
        emit = AsyncMock()
        with patch("services.chat.service.chat_create_with_fallback", side_effect=RuntimeError("transport failed")):
            with self.assertRaises(RuntimeError):
                await stream_agent_a(messages, AsyncMock(), context_diagnostics=diagnostics, on_context_status=emit)
        self.assertEqual([call.args[0]["status"] for call in emit.await_args_list], ["sending", "unconfirmed"])

    async def test_status_callback_cancellation_closes_created_stream(self):
        diagnostics = {}
        messages = build_chat_context("規則", [], "問題", runtime_context=project_snapshot(
            make_turn_snapshot("turn", 0, result("ok", desktop_data()))), diagnostics=diagnostics)
        stream = FakeStream()
        emit = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with patch("services.chat.service.chat_create_with_fallback", return_value=stream):
            with self.assertRaises(asyncio.CancelledError):
                await stream_agent_a(messages, AsyncMock(), context_diagnostics=diagnostics, on_context_status=emit)
        self.assertTrue(stream.closed)

    async def run_screenshot(self, responses, *, messages=None, calls=None):
        receipt = result("ok", {"target": "screen", "width": 10, "height": 10})
        receipt["image"] = b"synthetic-image"
        reader = SimpleNamespace(read=AsyncMock(return_value=receipt))
        tools, emit = ContextTools(desktop_allowed=True, reader=reader), AsyncMock()
        messages = messages or [dict(role="system", content="規則"), dict(role="user", content="請看畫面")]
        with patch("services.chat.service.chat_create_with_fallback", new=AsyncMock(side_effect=[
                selection(calls or [native("capture_screenshot", '{"target":"screen"}')], finish="tool_calls"), *responses])) as create:
            await stream_agent_a(messages, AsyncMock(), context_tools=tools, on_context_status=emit)
        return emit, create, reader

    async def test_screenshot_progress_and_delivery_without_image_payload(self):
        emit, create, _ = await self.run_screenshot([FakeStream()])
        events = [call.args[0] for call in emit.await_args_list]
        self.assertEqual([event["status"] for event in events], ["reading", "ready", "sending", "sent"])
        self.assertEqual(events[-1]["target"], "screen")
        self.assertNotIn("image", json.dumps(events))
        self.assertIn("image_url", json.dumps(create.await_args_list[-1].kwargs["messages"]))

    async def test_vision_rejection_updates_status_and_removes_image(self):
        emit, create, _ = await self.run_screenshot([rejection(), FakeStream()])
        events = [call.args[0] for call in emit.await_args_list]
        self.assertNotIn("sent", [event["status"] for event in events])
        self.assertEqual(events[-1]["reason"], "vision_not_supported")
        self.assertNotIn("image_url", json.dumps(create.await_args_list[-1].kwargs["messages"]))

    async def test_image_budget_omission_never_reports_sent(self):
        messages = [dict(role="system", content="規則" * 800), dict(role="user", content="請看這張圖：" + "🦊" * 900)]
        emit, create, _ = await self.run_screenshot([FakeStream()], messages=messages)
        events = [call.args[0] for call in emit.await_args_list]
        self.assertEqual(events[-1]["status"], "omitted")
        self.assertEqual(events[-1]["reason"], "image_context_budget")
        self.assertNotIn("sent", [event["status"] for event in events])
        self.assertNotIn("image_url", json.dumps(create.await_args_list[-1].kwargs["messages"]))

    async def test_duplicate_screenshot_rejection_does_not_hide_delivered_image(self):
        emit, _, reader = await self.run_screenshot([FakeStream()], calls=[
            native("capture_screenshot", call_id="first"), native("capture_screenshot", call_id="duplicate")])
        reader.read.assert_awaited_once()
        self.assertEqual(emit.await_args_list[-1].args[0]["status"], "sent")

    async def test_unfittable_tool_context_reports_omission(self):
        with patch("services.chat.service._tool_context", side_effect=ValueError("tool_context_budget")):
            emit, _, _ = await self.run_screenshot([FakeStream()])
        self.assertEqual(emit.await_args_list[-1].args[0]["reason"], "tool_context_budget")
        self.assertNotIn("sent", [call.args[0]["status"] for call in emit.await_args_list])

    async def test_cancelled_tool_does_not_send_result_or_confirm_delivery(self):
        entered = asyncio.Event()

        async def read(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        tools = ContextTools(desktop_allowed=True, reader=SimpleNamespace(read=read))
        emit = AsyncMock()
        with patch("services.chat.service.chat_create_with_fallback", return_value=selection(
                [native("capture_screenshot")], finish="tool_calls")) as create:
            task = asyncio.create_task(stream_agent_a([dict(role="system", content="規則"), dict(role="user", content="問題")],
                AsyncMock(), context_tools=tools, on_context_status=emit))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(create.await_count, 1)
        self.assertEqual([call.args[0]["status"] for call in emit.await_args_list], ["reading"])


class ContextStatusWebSocketTests(unittest.TestCase):
    def test_real_chat_callback_is_turn_scoped_and_not_persisted(self):
        tools = ContextTools()
        tools.snapshot = AsyncMock(side_effect=[make_turn_snapshot(turn_id, 0, result("ok", desktop_data(app)))
            for turn_id, app in (("first", "first.exe"), ("second", "second.exe"))])

        async def chat(messages, send_chunk, **kwargs):
            reply = await stream_agent_a(messages, send_chunk, **kwargs)
            await kwargs["on_context_status"](context_status("desktop", "unconfirmed", reason="request_failed"))
            return reply

        with patch("api.routes.chat_ws.ContextTools", return_value=tools), \
             patch("services.chat.service.chat_create_with_fallback", side_effect=[
                 selection(content="第一句回覆"), selection(content="第二句回覆")]):
            socket, captured = chat_ws_tests.EmotionWebSocketTests()._run([
                {"content": "第一句", "turn_id": "first"}, {"content": "第二句", "turn_id": "second"}],
                [chat_ws_tests.jev_answers(), chat_ws_tests.jev_answers()], chat=chat)
        events = [payload for payload in socket.payloads if payload["type"] == "context_status"]
        self.assertEqual([payload["foreground"]["app"] for payload in events if payload["status"] == "sent"],
                         ["first.exe", "second.exe"])
        self.assertEqual([payload["turn_id"] for payload in events if payload["status"] == "sent"], ["first", "second"])
        self.assertNotIn("unconfirmed", [payload["status"] for payload in events])
        self.assertNotIn("first.exe", str(captured["runtime"].accept.await_args_list))
        persisted = str(captured["chat_sessions"].repository.messages)
        self.assertNotIn("context_status", persisted)
        self.assertNotIn("first.exe", persisted)


if __name__ == "__main__":
    unittest.main()
