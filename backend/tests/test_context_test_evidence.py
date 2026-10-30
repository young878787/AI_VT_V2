"""新分組工具驗收的 native 選擇、取得、交付與來源證據回歸。"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.tests.test_context_tools import FakeStream, native, rejection, selection
from domain.runtime_context import CONTEXT_TOOL_NAMES, make_turn_snapshot, project_snapshot, result
from infrastructure.windows_desktop import WindowsDesktop
from services.chat.service import build_chat_context, stream_agent_a
from services.context.tools import ContextTools, close_desktop_reader, desktop_reader
from tools import chat_test_cli as cli
from tools.ai_test.evidence import check_turn
from tools.context_test_evidence import collect_tools, summarize_tools

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REF = "backend/tools/ai_test/cases/assets/desktop.png"
FIXTURE = ROOT / FIXTURE_REF


def checks(record, *, required_tools=(), rejection_expected=False, memory_probe=False):
    suite = "memory" if memory_probe else "tools"
    mode = "memory_probe" if memory_probe else "short_only"
    test_mode = next(item for item in record["trace"] if item["stage"] == "test_mode")
    test_mode.update(mode=mode, short_term_enabled=not memory_probe,
                     memory_read_enabled=memory_probe, memory_write_enabled=False)
    case = {"case_id": record["case_id"], "phase": "recall" if memory_probe else "conversation",
            "context": {"vision_rejection": rejection_expected, "tools": {
                "capture_screenshot": {"asset": FIXTURE_REF}}},
            "oracle": {"required_tools": list(required_tools)}}
    findings = check_turn(suite, case, record, [], {}, {}, {})
    return {item["reason_code"]: item["status"] == "passed" for item in findings}


class ContextEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trace_dir = patch("services.chat.service.test_log_dir", return_value=FIXTURE.parent)
        trace_dir.start()
        self.addCleanup(trace_dir.stop)

    async def screenshot_roundtrip(self, reject_image=False):
        with Image.open(FIXTURE) as image:
            receipt = WindowsDesktop._encode_screenshot(image, "foreground", {
                "kind": "fixture_image", "ref": FIXTURE_REF,
                "sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()})
        reader = SimpleNamespace(read=AsyncMock(return_value=receipt))
        tools = ContextTools(desktop_allowed=True, reader=reader)
        snapshot = make_turn_snapshot("turn-image", 0, result("ok", {
            "foreground": {"status": "ok", "app": "Code.exe"},
            "open_apps": {"status": "ok", "apps": ["Code.exe"], "truncated": False}}))
        diagnostics = {}
        messages = build_chat_context("角色 1970-01-01 08:00:00 +0800",
            [{"role": "user", "content": "之前的聊天"}], "你看到什麼？",
            runtime_context=project_snapshot(snapshot), tools=tools.schemas, diagnostics=diagnostics)
        context = dict(stage="chat_context", messages=messages,
            message_origins=diagnostics["message_origins"], runtime_context=snapshot,
            tools_available=list(CONTEXT_TOOL_NAMES), history=[{"role": "user", "content": "之前的聊天"}])
        record = {"case_id": "to_005", "user": "你看到什麼？", "stream_complete": True,
                  "expression": "neutral", "expression_debug": {
                      "jevDecisionSource": "jev", "jevBaseEmotionChoice": "neutral",
                      "jevInteractionAttitudeChoice": "smile", "jevBaseEmotionFallbackReason": "none",
                      "jevInteractionAttitudeFallbackReason": "none", "jevResolvedEmotion": "neutral",
                      "jevResolvedAttitude": "smile"},
                  "errors": [], "memory_event_id": None, "db_unchanged": True,
                  "trace": [context, {"stage": "jev", "role": "emotion", "error": None, "answers": {}},
                            {"stage": "jev", "role": "action", "error": None, "answers": {
                      "base_emotion": {"choice": "neutral", "confidence": 1.0},
                      "interaction_attitude": {"choice": "smile", "confidence": 1.0}}},
                            {"stage": "jev", "role": "memory", "error": None, "answers": {}}, {"stage": "test_mode"}]}

        def trace(stage, payload):
            record["trace"].append(dict(stage=stage, **copy.deepcopy(payload)))

        responses = [selection([native("capture_screenshot")], finish="tool_calls")]
        if reject_image:
            responses.append(rejection())
        responses.append(FakeStream("看到 Live2D 預覽與程式編輯器，畫面仍在測試，無法確認模型修好了。"))
        with patch("services.chat.service.trace", side_effect=trace), \
             patch("services.context.tools.trace", side_effect=trace), \
             patch("services.chat.service.chat_create_with_fallback", new=AsyncMock(side_effect=responses)) as create:
            record["reply"] = await stream_agent_a(messages, AsyncMock(), context_tools=tools,
                                                   context_diagnostics=diagnostics)
        final = next(t for t in reversed(record["trace"]) if t["stage"] == "chat_request")
        context.update({key: final[key] for key in ("messages", "message_origins")})
        reader.read.assert_awaited_once_with("screenshot", timeout=2, target="foreground")
        return record, context, create

    async def test_native_selection_delivers_fixture_and_only_hashes_enter_trace(self):
        record, context, create = await self.screenshot_roundtrip()
        findings = checks(record, required_tools=["capture_screenshot"])
        self.assertTrue(all(findings.values()), findings)
        tool = record["tools"][0]
        source = tool["result"]["data"]["source"]
        self.assertEqual(source["ref"], FIXTURE_REF)
        self.assertEqual(source["sha256"], hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual([tool["result"]["data"]["width"], tool["result"]["data"]["height"]], [1280, 720])
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
        context["runtime_context"]["foreground"]["app"] = "另一個應用"
        self.assertFalse(checks(record)["snapshot_in_prompt"])

    async def test_acquisition_and_delivery_are_distinct_and_expected_rejection_has_its_own_contract(self):
        record, _, create = await self.screenshot_roundtrip(reject_image=True)
        findings = checks(record, required_tools=["capture_screenshot"])
        tool = record["tools"][0]
        self.assertEqual(tool["backend_status"], "ok")
        self.assertTrue(tool["receipt_delivered"])
        self.assertEqual(tool["delivery_reason"], "vision_not_supported")
        self.assertFalse(tool["image_accepted"])
        self.assertFalse(findings["required_capture_screenshot"])
        self.assertFalse(findings["image_delivery"])
        expected = checks(record, required_tools=["capture_screenshot"], rejection_expected=True)
        self.assertTrue(all(expected.values()), expected)
        self.assertEqual(create.await_count, 3)

    async def test_tampered_image_receipt_fixture_and_origin_cannot_pass(self):
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
            findings = checks(record, required_tools=["capture_screenshot"])
            with self.subTest(tamper=tamper):
                self.assertFalse(all(findings.values()), findings)
        self.assertIn("input", original_context["message_origins"])

    async def test_db_probe_identifies_external_image_source_without_disabling_tool_schemas(self):
        record, context, _ = await self.screenshot_roundtrip()
        history_index = context["message_origins"].index("history")
        context["message_origins"].pop(history_index)
        context["messages"].pop(history_index)
        context["history"] = []
        record["trace"].append({"stage": "retrieval", "embedding_available": True, "candidates": [], "projections": []})
        findings = checks(record, memory_probe=True)
        self.assertEqual(set(context["tools_available"]), set(CONTEXT_TOOL_NAMES))
        self.assertFalse(findings["db_probe_without_desktop_answer"])

    async def test_unexpected_tool_failure_becomes_sanitized_receipt(self):
        with patch.object(ContextTools, "_execute", new=AsyncMock(side_effect=RuntimeError("private-path"))), \
             patch("services.context.tools.trace") as trace:
            receipt = await ContextTools().execute("get_datetime", "{}", call_id="failed")
        self.assertEqual(receipt["reason"], "tool_execution_failed")
        self.assertEqual(trace.call_args.args[1]["result"]["reason"], "tool_execution_failed")
        self.assertNotIn("private-path", str(trace.call_args))

    async def test_activity_title_is_used_but_raw_caption_not_in_tool_evidence(self):
        title = "目前秘密文件.txt - Visual Studio Code"
        tools = ContextTools(desktop_allowed=True, reader=SimpleNamespace(read=AsyncMock(return_value=result(
            "ok", {"foreground": {"status": "ok", "app": "Code.exe", "title": title}}))))
        events = []

        def trace(stage, payload):
            events.append(dict(stage=stage, **copy.deepcopy(payload)))

        with patch("services.chat.service.trace", side_effect=trace), \
             patch("services.context.tools.trace", side_effect=trace), \
             patch("services.chat.service.chat_create_with_fallback", new=AsyncMock(side_effect=[
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

    def test_test_instance_does_not_silently_replace_live_screenshot(self):
        for enabled in ("true", "false"):
            close_desktop_reader()
            with patch.dict(os.environ, {"AI_VT_TEST_MODE": enabled}):
                self.assertIsNone(desktop_reader().desktop.screenshot_fixture)

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
                self.assertEqual("asyncio:SelectorEventLoop" in popen.call_args.args[0], platform == "win32")
