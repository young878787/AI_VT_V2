"""唯讀工具、臺灣地區／預報與 Chat native roundtrip 的驗證。"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import BadRequestError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.runtime_context import local_time, result
from services.chat_service import _structured_prompt_sections, _tool_context, build_chat_context, estimate_token_count, stream_agent_a
from services.context_tools import ContextTools
from services.taiwan_weather import _REGIONS, get_weather, grounded_location, js_literal, parse_forecast, search_regions


def native(name="get_datetime", arguments="{}", call_id="call1"):
    value = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
    return SimpleNamespace(model_dump=lambda **_kwargs: value)


def selection(calls=None, content="", finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(tool_calls=calls, content=content), finish_reason=finish)],
        model="fake-chat", usage=None)


class FakeStream:
    closed = False

    def __init__(self, text="查詢完成"):
        self.text = text

    async def __aiter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=self.text), finish_reason="stop")])

    async def close(self):
        self.closed = True


def rejection():
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid/chat"))
    return BadRequestError("unsupported input", response=response, body={})


class TaiwanRegionTests(unittest.TestCase):
    def test_all_administrative_regions_are_resolvable(self):
        self.assertEqual(len(_REGIONS), 22)
        self.assertEqual(sum(len(c["towns"]) for c in _REGIONS), 368)
        for county in _REGIONS:
            for town, town_id in county["towns"].items():
                matches = search_regions(county["name"] + town)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["town_id"], town_id)

    def test_taiwan_aliases_and_ambiguity(self):
        for query, expected in (("台北市", "臺北市"), ("新北板橋", "新北市板橋區"),
                                ("臺灣 台中 西屯", "臺中市西屯區"), ("Taipei", "臺北市")):
            self.assertEqual(search_regions(query)[0]["location"], expected)
        self.assertEqual(len(search_regions("中正區")), 2)
        self.assertEqual(len(search_regions("新竹")), 2)
        self.assertEqual(search_regions("Tokyo"), [])
        self.assertEqual(search_regions(""), [])

    def test_source_javascript_is_never_executed(self):
        with self.assertRaises((ValueError, SyntaxError)):
            js_literal("var Data = __import__('os').system('anything');", "Data")

    def test_model_cannot_invent_county_for_ambiguous_user_region(self):
        self.assertEqual(grounded_location("臺北市中正區", ["中正區現在天氣怎麼樣"]), "中正區")
        self.assertEqual(grounded_location("新竹市", ["新竹今天的天氣"]), "新竹")
        self.assertEqual(grounded_location("臺北市中正區", ["我在台北", "中正區現在天氣"]), "臺北市中正區")
        self.assertIsNone(grounded_location("臺北市", ["今天要出門"]))
        self.assertIsNone(grounded_location("臺北市", ["我在台北", "現在要去臺中西屯"]))

    def test_forecast_keeps_valid_time_and_forecast_kind(self):
        now = local_time().replace(year=2026, month=10, day=9, hour=10, minute=5, second=0)
        text = ("// Updated: 2026/10/09 08:00:00\n"
                "var Time_3hr = {'C':['09 10/09<br>','10 10/09<br>','11 10/09<br>']};\n"
                "var TempArray_3hr = {'6500100':{'C':{'T':[25,26,27],'AT':[27,28,29]},"
                "'Wx':{'C':[['04','多雲'],['01','晴'],['08','短暫陣雨']]}}};\n")
        receipt = parse_forecast(text, search_regions("新北板橋")[0], None, now)
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(receipt["data"]["kind"], "gridded_forecast")
        self.assertEqual(receipt["data"]["forecast"][0]["temperature_c"], 26)
        self.assertIn("2026-10-09T10:00", receipt["data"]["forecast"][0]["valid_at"])
        self.assertIsNone(receipt["data"]["precipitation_mm"])
        self.assertEqual(parse_forecast(text, search_regions("新北板橋")[0], "2026-10-10", now)["reason"],
                         "forecast_out_of_range")
        stale = text.replace("2026/10/09 08", "2026/10/07 08")
        self.assertEqual(parse_forecast(stale, search_regions("新北板橋")[0], None, now)["reason"], "stale_weather")

    def test_forecast_year_boundary(self):
        now = local_time().replace(year=2026, month=12, day=31, hour=23, minute=0, second=0)
        text = ("// Updated: 2026/12/31 22:00:00\n"
                "var Time_3hr = {'C':['23 12/31<br>','00 01/01<br>']};\n"
                "var TempArray_3hr = {'6500100':{'C':{'T':[20,19],'AT':[19,18]},"
                "'Wx':{'C':[['04','多雲'],['01','晴']]}}};\n")
        receipt = parse_forecast(text, search_regions("板橋")[0], None, now)
        self.assertIn("2027-01-01T00:00", receipt["data"]["forecast"][1]["valid_at"])


class ContextToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_can_read_desktop_without_user_command_gate(self):
        reader = SimpleNamespace(read=AsyncMock(return_value=result("ok", {"foreground": {"app": "Code.exe"}})))
        tools = ContextTools(desktop_allowed=True, reader=reader)
        receipt = await tools.execute("get_activity_context", "{}")
        self.assertEqual(receipt["status"], "ok")
        reader.read.assert_awaited_once_with("snapshot", timeout=1, include_title=True)
        self.assertEqual(len(tools.schemas), 5)

    async def test_unknown_and_invalid_arguments_do_not_read_desktop(self):
        reader = SimpleNamespace(read=AsyncMock())
        tools = ContextTools(desktop_allowed=True, reader=reader)
        for name, arguments in (("shell", "{}"), ("capture_screenshot", '{"target":"anything"}'),
                                ("get_activity_context", '{"owner":"someone"}'),
                                ("get_weather", "{}"), ("get_weather", '{"location":false}')):
            self.assertIn((await tools.execute(name, arguments))["status"], {"error", "unsupported"})
        reader.read.assert_not_awaited()

    async def test_call_and_screenshot_limits(self):
        reader = SimpleNamespace(read=AsyncMock(return_value=result("ok", {})))
        tools = ContextTools(desktop_allowed=True, reader=reader)
        calls = [native("capture_screenshot", call_id=str(i)).model_dump() for i in range(3)]
        receipts = await tools.execute_batch(calls)
        self.assertEqual([r["reason"] for r in receipts], [None, "screenshot_limit", "tool_call_limit"])
        self.assertEqual(reader.read.await_count, 1)

    async def test_expired_batch_does_not_start_even_immediate_tool(self):
        tools = ContextTools()
        tools.execute = AsyncMock()
        with patch("services.context_tools.BATCH_TIMEOUT_SECONDS", 0):
            receipts = await tools.execute_batch([native().model_dump()])
        self.assertEqual(receipts[0]["reason"], "tool_deadline")
        tools.execute.assert_not_awaited()

    async def test_remote_tools_do_not_expose_desktop(self):
        tools = ContextTools()
        self.assertEqual({t["function"]["name"] for t in tools.schemas}, {"get_datetime", "get_weather"})
        self.assertEqual((await tools.execute("capture_screenshot", "{}"))["reason"], "unknown_tool")

    async def test_ambiguous_region_does_not_make_network_request(self):
        client = SimpleNamespace(stream=AsyncMock())
        receipt = await get_weather("中正區", client=client)
        self.assertEqual(receipt["reason"], "ambiguous_region")
        self.assertEqual(receipt["data"]["candidate_count"], 2)
        client.stream.assert_not_called()

    async def test_weather_network_failure_is_explicit(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(503, request=request))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertEqual((await get_weather("板橋", client=client))["reason"], "weather_source_unavailable")

    async def test_weather_changed_source_format_is_explicit(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="broken", request=request))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertEqual((await get_weather("板橋", client=client))["reason"], "weather_source_format")


class ContextToolRoundtripTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_budget_reserves_space_for_every_paired_result(self):
        tools = ContextTools()
        calls = [native(call_id=f"call{index}") for index in range(8)]
        tools.execute_batch = AsyncMock(return_value=[
            result("ok", {"text": "a " * 850}),
            *[result("error", reason="tool_call_limit") for _ in range(7)],
        ])
        with patch("services.chat_service.chat_create_with_fallback", new=AsyncMock(side_effect=[
            selection(calls, finish="tool_calls"), FakeStream()])) as create:
            await stream_agent_a([{"role": "system", "content": "規則"}, {"role": "user", "content": "任務"}],
                                 AsyncMock(), context_tools=tools)
        final = create.await_args_list[-1].kwargs["messages"]
        receipts = [message for message in final if message["role"] == "tool"]
        self.assertEqual(len(receipts), 8)
        self.assertLessEqual(estimate_token_count(receipts), 1000)
        self.assertIn("tool_result_budget", receipts[0]["content"])

    async def test_insufficient_receipt_budget_does_not_execute_tools(self):
        tools = ContextTools()
        tools.execute_batch = AsyncMock()
        calls = [native(call_id=f"call{index}") for index in range(8)]
        assistant = {"role": "assistant", "content": None,
                     "tool_calls": [call.model_dump() for call in calls]}
        receipts = [{"role": "tool", "tool_call_id": call.model_dump()["id"],
                     "content": json.dumps(result("unavailable", reason="tool_result_budget"), ensure_ascii=False)}
                    for call in calls]
        budget = estimate_token_count([assistant, *receipts]) + 511
        with patch("services.chat_service.CHAT_CONTEXT_TOKEN_BUDGET", budget), patch(
            "services.chat_service.chat_create_with_fallback", new=AsyncMock(side_effect=[
                selection(calls, finish="tool_calls"), FakeStream("結果預算不足")])) as create:
            reply = await stream_agent_a(
                [{"role": "system", "content": "規則"}, {"role": "user", "content": "任務"}],
                AsyncMock(), context_tools=tools)
        self.assertEqual(create.await_count, 2)
        tools.execute_batch.assert_not_awaited()
        self.assertEqual(reply, "結果預算不足")

    async def test_tool_failure_policy_does_not_become_summary_content(self):
        assistant = {"role": "assistant", "content": None, "tool_calls": [native("get_weather").model_dump()]}
        receipts = [{"role": "tool", "tool_call_id": "call1", "content": json.dumps(
            result("unavailable", {"candidates": ["臺北市中正區", "基隆市中正區"]}, "ambiguous_region"))}]
        messages = [{"role": "system", "content": "角色\n\n本 session 已完成的對話摘要：\n舊摘要"},
                    {"role": "user", "content": "中正區"}]
        final = _tool_context(messages, assistant, receipts, [])
        system = final[0]["content"]
        start, end = _structured_prompt_sections(system)["summary"]
        self.assertIn("沒有取得任何天氣值", system[:start])
        self.assertNotIn("沒有取得任何天氣值", system[start:end])

    async def test_too_small_tool_schema_budget_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "tool_schema_budget"):
            build_chat_context("角色", [], "問題", 512, tools=ContextTools(desktop_allowed=True).schemas)

    async def test_no_tool_call_does_not_make_second_model_request(self):
        pieces = []
        with patch("services.chat_service.chat_create_with_fallback",
                   new=AsyncMock(return_value=selection(content="你好<think>不可见</think>！"))) as create:
            reply = await stream_agent_a([{"role": "system", "content": "規則"}, {"role": "user", "content": "你好"}],
                                         AsyncMock(side_effect=pieces.append), context_tools=ContextTools())
        self.assertEqual(reply, "你好！")
        self.assertEqual(create.await_count, 1)
        self.assertEqual(pieces, ["你好！"])

    async def test_tool_selection_text_is_buffered_and_receipt_pairs_are_preserved(self):
        pieces = []
        tools = ContextTools()
        async def execute(_calls):
            self.assertEqual(pieces, [])
            return [result("ok", {"time": "12:00"})]
        tools.execute_batch = AsyncMock(side_effect=execute)
        stream = FakeStream()
        with patch("services.chat_service.chat_create_with_fallback",
                   new=AsyncMock(side_effect=[selection([native()], "我查好了", "tool_calls"), stream])) as create:
            reply = await stream_agent_a([{"role": "system", "content": "規則"}, {"role": "user", "content": "需要時間"}],
                                         AsyncMock(side_effect=pieces.append), context_tools=tools)
        final = create.await_args_list[1].kwargs
        self.assertNotIn("tools", final)
        self.assertEqual(final["messages"][-2]["tool_calls"][0]["id"], final["messages"][-1]["tool_call_id"])
        self.assertEqual(reply, "查詢完成")
        self.assertEqual(pieces, ["查詢完成"])
        self.assertTrue(stream.closed)

    async def test_provider_rejection_falls_back_without_executing_tools(self):
        tools = ContextTools()
        tools.execute_batch = AsyncMock()
        with patch("services.chat_service.chat_create_with_fallback",
                   new=AsyncMock(side_effect=[rejection(), FakeStream("工具不可用")])):
            reply = await stream_agent_a([{"role": "system", "content": "規則"}, {"role": "user", "content": "你好"}],
                                         AsyncMock(), context_tools=tools)
        self.assertEqual(reply, "工具不可用")
        tools.execute_batch.assert_not_awaited()

    async def test_vision_rejection_removes_image_and_replaces_success_receipt(self):
        tools = ContextTools(desktop_allowed=True)
        receipt = result("ok", {"width": 10, "height": 10, "mime_type": "image/jpeg"})
        receipt["image"] = b"synthetic-image"
        tools.execute_batch = AsyncMock(return_value=[receipt])
        with patch("services.chat_service.chat_create_with_fallback", new=AsyncMock(side_effect=[
            selection([native("capture_screenshot")], finish="tool_calls"), rejection(), FakeStream("圖片能力未支援")])) as create:
            reply = await stream_agent_a([{"role": "system", "content": "規則"}, {"role": "user", "content": "任務"}],
                                         AsyncMock(), context_tools=tools)
        final = create.await_args_list[-1].kwargs["messages"]
        self.assertNotIn("image_url", json.dumps(final))
        self.assertIn("vision_not_supported", final[-1]["content"])
        self.assertEqual(reply, "圖片能力未支援")

    async def test_cancellation_during_tool_wait_makes_no_final_request(self):
        tools = ContextTools()
        entered = asyncio.Event()
        async def slow(_calls):
            entered.set()
            await asyncio.Event().wait()
        tools.execute_batch = AsyncMock(side_effect=slow)
        with patch("services.chat_service.chat_create_with_fallback",
                   new=AsyncMock(return_value=selection([native()], finish="tool_calls"))) as create:
            task = asyncio.create_task(stream_agent_a(
                [{"role": "system", "content": "規則"}, {"role": "user", "content": "任務"}],
                AsyncMock(), context_tools=tools))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(create.await_count, 1)

    async def test_image_and_receipts_are_inside_final_budget(self):
        tools = ContextTools(desktop_allowed=True)
        receipt = result("ok", {"width": 1600, "height": 900, "mime_type": "image/jpeg"})
        receipt["image"] = b"synthetic-image"
        tools.execute_batch = AsyncMock(return_value=[receipt])
        messages = [{"role": "system", "content": "規則"}, *[
            {"role": "assistant", "content": "前文" * 100} for _ in range(10)],
            {"role": "user", "content": "最新問題"}]
        with patch("services.chat_service.chat_create_with_fallback", new=AsyncMock(side_effect=[
            selection([native("capture_screenshot")], finish="tool_calls"), FakeStream()])) as create:
            await stream_agent_a(messages, AsyncMock(), context_tools=tools)
        final = create.await_args_list[-1].kwargs["messages"]
        self.assertLessEqual(estimate_token_count(final), 8192)
        self.assertIn("最新問題", [m["content"] for m in final if isinstance(m.get("content"), str)])
