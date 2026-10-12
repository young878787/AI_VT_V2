"""從 native 選擇、後端 fixture 到最終圖片與報告的證據鏈回歸。"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.tests.test_context_tools import FakeStream, native, rejection, selection
from domain.chat_test_mode import ChatTestMode
from domain.runtime_context import CONTEXT_TOOL_NAMES, make_turn_snapshot, project_snapshot, result
from infrastructure.windows_desktop import DesktopReader, WindowsDesktop
from services.chat_service import build_chat_context, stream_agent_a
from services.context_tools import ContextTools, close_desktop_reader, desktop_reader
from tools import chat_test_cli as cli
from tools.context_test_evidence import SCREENSHOT_FIXTURE_SHA256, check_context_tools, collect_tools, summarize_tools

FIXTURE = Path(__file__).resolve().parents[2] / "sample/擷取.JPG"


def checks(step, record, context, mode=ChatTestMode.SHORT_ONLY):
    findings = []
    check_context_tools(step, record, context, mode,
                        lambda layer, name, actual, expected, valid, reason: findings.append((name, bool(valid))))
    return dict(findings)


class ContextEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trace_dir = patch("services.chat_service.test_log_dir", return_value=FIXTURE.parent)
        trace_dir.start()
        self.addCleanup(trace_dir.stop)

    async def screenshot_roundtrip(self, reject_image=False):
        desktop = object.__new__(WindowsDesktop)
        desktop.screenshot_fixture = FIXTURE
        reader = DesktopReader(desktop)
        tools = ContextTools(desktop_allowed=True, reader=reader)
        snapshot = make_turn_snapshot("turn-image", 0, result("ok", {
            "foreground": {"status": "ok", "app": "Code.exe"},
            "open_apps": {"status": "ok", "apps": ["Code.exe"], "truncated": False}}))
        diagnostics = {}
        messages = build_chat_context("角色 1970-01-01 08:00:00 +0800", [{"role": "user", "content": "之前的聊天"}], "你看到什麼？",
                                      runtime_context=project_snapshot(snapshot), tools=tools.schemas, diagnostics=diagnostics)
        context = dict(stage="chat_context", messages=messages, **{
            "message_origins": diagnostics["message_origins"], "runtime_context": snapshot,
            "tools_available": list(CONTEXT_TOOL_NAMES)})
        record = dict(case_id="case_004", turn=3, trace=[context])
        def trace(stage, payload):
            record["trace"].append(dict(stage=stage, **copy.deepcopy(payload)))
        responses = [selection([native("capture_screenshot")], finish="tool_calls")]
        if reject_image:
            responses.append(rejection())
        responses.append(FakeStream("看到文件和綠髮插圖，看起來你在看文件。"))
        try:
            with patch("services.chat_service.trace", side_effect=trace), \
                 patch("services.context_tools.trace", side_effect=trace), \
                 patch("services.chat_service.chat_create_with_fallback", new=AsyncMock(side_effect=responses)) as create:
                record["reply"] = await stream_agent_a(messages, AsyncMock(), context_tools=tools,
                                                       context_diagnostics=diagnostics)
            final = next(t for t in reversed(record["trace"]) if t["stage"] == "chat_request")
            context.update({key: final[key] for key in ("messages", "message_origins")})
            return record, context, create
        finally:
            reader.close()

    async def test_fixture_is_loaded_only_by_native_call_and_delivered_without_persisting_pixels(self):
        record, context, create = await self.screenshot_roundtrip()
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), SCREENSHOT_FIXTURE_SHA256)
        findings = checks({"expected_tools": ["capture_screenshot"]}, record, context)
        self.assertTrue(all(findings.values()), findings)
        tool = record["tools"][0]
        self.assertEqual(tool["result"]["data"]["source"]["ref"], "sample/擷取.JPG")
        self.assertEqual([tool["result"]["data"]["width"], tool["result"]["data"]["height"]], [1600, 900])
        self.assertGreater(tool["image_bytes"], 0)
        persisted = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("base64,", persisted)
        self.assertNotIn('"image":', persisted)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(create.await_args_list[0].kwargs["tool_choice"], "auto")
        self.assertNotIn("tools", create.await_args_list[1].kwargs)
        self.assertEqual(context["message_origins"].count("history"), 1)
        self.assertEqual(context["message_origins"].count("runtime_context"), 1)
        self.assertEqual(summarize_tools([record])["images_accepted"], 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory_report.md"
            case = dict(case_id="case_004", case_group="short_term", case_type="reference", expected_focus=["畫面"],
                        expected_result="描述圖片", conversation=[dict(phase="context", session="context", input="你看到什麼？")])
            (path.parent / "cases.json").write_text(json.dumps([case]), encoding="utf-8")
            cli.write_memory_report([record], path, dict(started_at="test"), "completed", None)
            report = path.read_text(encoding="utf-8")
        self.assertIn("Tools 與後端取得", report)
        self.assertIn("fixture_image", report)
        self.assertIn("圖片接受 1", report)
        self.assertIn(tool["result"]["data"]["image_sha256"], report)
        self.assertNotIn("base64,", report)
        context["runtime_context"]["foreground"]["app"] = "另一個應用"
        self.assertFalse(checks({}, record, context)["snapshot_in_prompt"])

    async def test_successful_acquisition_is_not_successful_delivery_after_vision_rejection(self):
        record, context, create = await self.screenshot_roundtrip(reject_image=True)
        findings = checks({"expected_tools": ["capture_screenshot"]}, record, context)
        tool = record["tools"][0]
        self.assertEqual(tool["backend_status"], "ok")
        self.assertTrue(tool["receipt_delivered"])
        self.assertEqual(tool["delivery_reason"], "vision_not_supported")
        self.assertFalse(tool["image_accepted"])
        self.assertFalse(findings["required_capture_screenshot"])
        self.assertFalse(findings["screenshot_image"])
        self.assertEqual(create.await_count, 3)

    async def test_tampered_image_receipt_and_fixture_cannot_pass(self):
        original, original_context, _ = await self.screenshot_roundtrip()
        for tamper in ("image", "receipt", "fixture", "origin"):
            record = copy.deepcopy(original)
            context = next(t for t in record["trace"] if t["stage"] == "chat_context")
            final = next(t for t in reversed(record["trace"]) if t["stage"] == "chat_request")
            if tamper == "image":
                final["messages"][-1]["content"][-1]["image_sha256"] = "different"
            elif tamper == "receipt":
                receipt = next(m for m in final["messages"] if m["role"] == "tool")
                payload = json.loads(receipt["content"])
                payload["data"]["width"] = 999
                receipt["content"] = json.dumps(payload)
            elif tamper == "fixture":
                execution = next(t for t in record["trace"] if t["stage"] == "context_tool_execution")
                execution["result"]["data"]["source"]["sha256"] = "different"
            else:
                context["message_origins"][-1] = "history"
            findings = checks({"expected_tools": ["capture_screenshot"]}, record, context)
            with self.subTest(tamper=tamper):
                self.assertFalse(all(findings.values()), findings)
        self.assertIn("input", original_context["message_origins"])

    async def test_pure_db_probe_reports_external_source_without_disabling_tools(self):
        record, context, _ = await self.screenshot_roundtrip()
        history_index = context["message_origins"].index("history")
        context["message_origins"].pop(history_index)
        context["messages"].pop(history_index)
        findings = checks({}, record, context, ChatTestMode.MEMORY_PROBE)
        self.assertTrue(findings["tools_available"])
        self.assertFalse(findings["independent_db_source"])

    async def test_unexpected_tool_failure_becomes_sanitized_receipt(self):
        tools = ContextTools()
        with patch.object(tools, "_execute", new=AsyncMock(side_effect=RuntimeError("private-path"))), \
             patch("services.context_tools.trace") as trace:
            receipt = await tools.execute("get_datetime", "{}", call_id="failed")
        self.assertEqual(receipt["reason"], "tool_execution_failed")
        self.assertEqual(trace.call_args.args[1]["result"]["reason"], "tool_execution_failed")
        self.assertNotIn("private-path", str(trace.call_args))

    async def test_activity_title_is_used_but_raw_caption_not_in_tool_evidence(self):
        from types import SimpleNamespace
        title = "目前秘密文件.txt - Visual Studio Code"
        tools = ContextTools(desktop_allowed=True, reader=SimpleNamespace(read=AsyncMock(return_value=result(
            "ok", {"foreground": {"status": "ok", "app": "Code.exe", "title": title}}))))
        events = []
        def trace(stage, payload):
            events.append(dict(stage=stage, **copy.deepcopy(payload)))
        with patch("services.chat_service.trace", side_effect=trace), \
             patch("services.context_tools.trace", side_effect=trace), \
             patch("services.chat_service.chat_create_with_fallback", new=AsyncMock(side_effect=[
                 selection([native("get_activity_context")], finish="tool_calls"), FakeStream(title)])):
            await stream_agent_a([dict(role="system", content="角色"), dict(role="user", content="標題？")],
                                 AsyncMock(), context_tools=tools)
        evidence = collect_tools(dict(trace=events))[0]
        self.assertTrue(evidence["title_in_answer"])
        self.assertTrue(evidence["receipt_matches_backend"])
        self.assertNotIn(title, json.dumps(events, ensure_ascii=False))


class FixtureBoundaryTests(unittest.TestCase):
    def tearDown(self):
        close_desktop_reader()

    def test_only_existing_test_instance_uses_fixed_fixture(self):
        for enabled, expected in (("true", FIXTURE), ("false", None)):
            close_desktop_reader()
            with patch.dict(os.environ, {"AI_VT_TEST_MODE": enabled}):
                reader = desktop_reader()
                self.assertEqual(reader.desktop.screenshot_fixture, expected)

    def test_trace_reader_uses_final_prompt_after_tool_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory").mkdir()
            context = dict(stage="chat_context", turn_id="turn", messages=[dict(role="system", content="舊內容")])
            final = dict(stage="chat_request", turn_id="turn", messages=[dict(role="system", content="最終內容")],
                         message_origins=["system"], system_retained_ranges=[[0, 4]], token_estimate=5)
            (root / "memory/trace.jsonl").write_text("\n".join(json.dumps(t) for t in [context, final]), encoding="utf-8")
            records = cli.read_turn_trace(root, "turn")
        self.assertEqual(records[0]["messages"][0]["content"], "最終內容")
        self.assertEqual(records[0]["message_origins"], ["system"])

    def test_test_backend_uses_selector_only_on_windows(self):
        for platform in ("win32", "linux"):
            with tempfile.TemporaryDirectory() as directory, patch.object(cli.sys, "platform", platform), \
                 patch.object(cli.subprocess, "Popen") as popen:
                _, log = cli.start_backend(Path(directory), 12345, "postgresql://test/db",
                                           "test_" + "0" * 32, cli.uuid.uuid4(), cli.uuid.uuid4())
                log.close()
                command = popen.call_args.args[0]
                self.assertEqual("asyncio:SelectorEventLoop" in command, platform == "win32")
