"""Chat 可依當輪對話／任務選擇的五個有界唯讀工具。"""
import asyncio
import json
import time

from core.prompt_logger import trace
from domain.runtime_context import DESKTOP_TOOL_KINDS, context_status, local_time, make_turn_snapshot, result, safe_tool_result
from infrastructure.windows_desktop import DesktopReader, WindowsDesktop
from services.taiwan_weather import get_weather, grounded_location
from services.ai_test_context import fixture_snapshot, fixture_tool

_reader = None
BATCH_TIMEOUT_SECONDS = 5


def desktop_reader() -> DesktopReader:
    global _reader
    if _reader is None:
        _reader = DesktopReader(WindowsDesktop())
    return _reader


def close_desktop_reader() -> None:
    global _reader
    if _reader is not None:
        _reader.close()
        _reader = None


def _schema(name: str, description: str, properties=None, required=None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {},
                           "required": required or [], "additionalProperties": False}}}


class ContextTools:
    def __init__(self, *, desktop_allowed=False, reader=None):
        self.desktop_allowed = desktop_allowed
        self.reader = reader
        self.used_names = []
        self.user_context = None

    @property
    def schemas(self) -> list[dict]:
        tools = [
            _schema("get_datetime", "VT 需要讀取／刷新日期、時間或秒數，配合對時等任務時使用。訊息時間只是接收時刻，不能代替重新讀取時鐘。"),
            _schema("get_weather", "為臺灣地區的對話、出門建議或任務查詢短期天氣。地點必須來自對話，不能猜使用者位置。縣市／鄉鎮名稱可用台或臺；同名地區會回候選。資料是預報，非實測。",
                    {"location": {"type": "string", "maxLength": 80, "description": "臺灣縣市或鄉鎮市區，如臺北市、新北板橋、臺中西屯。"},
                     "date": {"type": "string", "description": "可選 YYYY-MM-DD，今天至後天；省略查目前附近時段。"}},
                    ["location"]),
        ]
        if self.desktop_allowed:
            tools += [
                _schema("get_activity_context", "VT 想了解目前情境，或需要前景視窗標題承接對話與任務時使用。自動快照沒有標題，必須用本工具取得。當下線索不代表使用者意圖或操作歷史。"),
                _schema("capture_screenshot", "VT 想了解可見畫面、承接看見什麼的對話或任務需要視覺線索時，擷取一張圖片後判讀。自動快照的應用名稱不能代替圖片。預設前景，整體桌面才選 screen。不錄影、不持續監控。",
                        {"target": {"type": "string", "enum": ["foreground", "screen"]}}),
                _schema("get_computer_status", "VT 想確認電腦現況，或配合效能、資源、供電相關對話與任務時，單次讀取 CPU、RAM、系統磁碟及電池。"),
            ]
        return tools

    async def snapshot(self, turn_id: str, timestamp: float) -> dict:
        desktop = (fixture_snapshot() if self.desktop_allowed else None)
        desktop = desktop or (await (self.reader or desktop_reader()).read("snapshot") if self.desktop_allowed
                   else result("unavailable", reason="local_access_required"))
        return make_turn_snapshot(turn_id, timestamp, desktop)

    async def execute(self, name: str, arguments: str, *, call_id=None, on_status=None) -> dict:
        started = time.monotonic()
        receipt, params, executed = None, None, False
        try:
            params = self._arguments(name, arguments)
            if params is None:
                receipt = result("error", reason="unknown_tool" if name not in {
                    schema["function"]["name"] for schema in self.schemas} else "invalid_arguments")
            else:
                executed = True
                self.used_names.append(name)
                if on_status is not None and name in DESKTOP_TOOL_KINDS:
                    await on_status(context_status(DESKTOP_TOOL_KINDS[name], "reading", {"data": params}))
                receipt = await self._execute(name, params)
            return receipt
        except Exception:
            receipt = result("error", reason="tool_execution_failed")
            return receipt
        finally:
            trace("context_tool_execution", {
                "call_id": call_id, "name": name, "arguments": params, "executed": executed,
                "duration_sec": round(time.monotonic() - started, 4),
                "result": safe_tool_result(receipt) if receipt is not None else result("unavailable", reason="cancelled"),
                "image_bytes": len(receipt.get("image", b"")) if receipt else 0,
            })

    def _arguments(self, name: str, arguments: str) -> dict | None:
        known = {schema["function"]["name"]: schema["function"]["parameters"] for schema in self.schemas}
        if name not in known:
            return None
        try:
            if len(arguments) > 1024:
                raise ValueError("arguments_size")
            params = json.loads(arguments)
            schema = known[name]
            if not isinstance(params, dict) or set(params) - schema["properties"].keys():
                raise ValueError("arguments_fields")
            if any(key not in params for key in schema["required"]):
                raise ValueError("arguments_missing")
            for key, value in params.items():
                definition = schema["properties"][key]
                if (not isinstance(value, str) or not value.strip() or len(value) > definition.get("maxLength", 80)
                        or ("enum" in definition and value not in definition["enum"])):
                    raise ValueError("arguments_value")
        except (ValueError, TypeError):
            return None
        return params

    async def _execute(self, name, params) -> dict:
        if name == "get_weather" and self.user_context is not None:
            location = grounded_location(params["location"], self.user_context)
            if location is None:
                return result("unavailable", reason="location_required")
            params["location"] = location
        fixed = fixture_tool(name, params)
        if fixed is not None:
            return fixed
        if name == "get_datetime":
            time = local_time()
            return result("ok", {"datetime": time.isoformat(), "weekday": time.weekday() + 1,
                                 "timezone": "Asia/Taipei"})
        if name == "get_weather":
            return await get_weather(**params)
        reader = self.reader or desktop_reader()
        if name == "get_activity_context":
            return await reader.read("snapshot", timeout=1, include_title=True)
        if name == "get_computer_status":
            return await reader.read("computer_status", timeout=1)
        return await reader.read("screenshot", timeout=2, target=params.get("target", "foreground"))

    async def execute_batch(self, calls: list[dict], *, on_status=None) -> list[dict]:
        receipts = []
        screenshot_used = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BATCH_TIMEOUT_SECONDS
        for index, call in enumerate(calls):
            name = call.get("function", {}).get("name", "")
            started = False
            if index >= 2:
                receipt = result("error", reason="tool_call_limit")
            elif name == "capture_screenshot" and screenshot_used:
                receipt = result("error", reason="screenshot_limit")
            elif loop.time() >= deadline:
                receipt = result("unavailable", reason="tool_deadline")
            else:
                screenshot_used |= name == "capture_screenshot"
                try:
                    async with asyncio.timeout_at(deadline):
                        started = True
                        receipt = await self.execute(name, call.get("function", {}).get("arguments", ""),
                                                     call_id=call.get("id"), on_status=on_status)
                except TimeoutError:
                    receipt = result("unavailable", reason="tool_deadline")
            if receipt.get("reason") in {"tool_call_limit", "screenshot_limit", "tool_deadline"}:
                trace("context_tool_rejected", {"call_id": call.get("id"), "name": name,
                                               "executed": started, "result": safe_tool_result(receipt)})
            receipts.append(receipt)
            if on_status is not None and name in DESKTOP_TOOL_KINDS:
                await on_status(context_status(DESKTOP_TOOL_KINDS[name],
                    "ready" if receipt["status"] == "ok" else "unavailable", receipt))
        return receipts
