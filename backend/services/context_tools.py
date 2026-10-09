"""Chat 可依當輪對話／任務選擇的五個有界唯讀工具。"""
import asyncio
import json

from domain.runtime_context import local_time, make_turn_snapshot, result
from infrastructure.windows_desktop import DesktopReader
from services.taiwan_weather import get_weather, grounded_location

_reader = None
BATCH_TIMEOUT_SECONDS = 5


def desktop_reader() -> DesktopReader:
    global _reader
    if _reader is None:
        _reader = DesktopReader()
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
            _schema("get_datetime", "需要刷新當下時間以理解對話或配合任務時使用；一般以訊息時間為準。"),
            _schema("get_weather", "為臺灣地區的對話、出門建議或任務查詢短期天氣。地點必須來自對話，不能猜使用者位置。縣市／鄉鎮名稱可用台或臺；同名地區會回候選。資料是預報，非實測。",
                    {"location": {"type": "string", "maxLength": 80, "description": "臺灣縣市或鄉鎮市區，如臺北市、新北板橋、臺中西屯。"},
                     "date": {"type": "string", "description": "可選 YYYY-MM-DD，今天至後天；省略查目前附近時段。"}},
                    ["location"]),
        ]
        if self.desktop_allowed:
            tools += [
                _schema("get_activity_context", "VT 想了解目前情境，或需要應用／前景視窗標題來承接對話與任務時使用。只讀當下線索，不代表知道使用者意圖或操作歷史。"),
                _schema("capture_screenshot", "VT 為理解當輪對話、畫面問題或任務需要視覺線索時，擷取一次畫面。預設前景；需要桌面整體情境才選 screen。不錄影、不持續監控。",
                        {"target": {"type": "string", "enum": ["foreground", "screen"]}}),
                _schema("get_computer_status", "VT 想確認電腦現況，或配合效能、資源、供電相關對話與任務時，單次讀取 CPU、RAM、系統磁碟及電池。"),
            ]
        return tools

    async def snapshot(self, turn_id: str, timestamp: float) -> dict:
        desktop = (await (self.reader or desktop_reader()).read("snapshot") if self.desktop_allowed
                   else result("unavailable", reason="local_access_required"))
        return make_turn_snapshot(turn_id, timestamp, desktop)

    async def execute(self, name: str, arguments: str) -> dict:
        known = {schema["function"]["name"]: schema["function"]["parameters"] for schema in self.schemas}
        if name not in known:
            return result("error", reason="unknown_tool")
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
            return result("error", reason="invalid_arguments")
        self.used_names.append(name)
        if name == "get_datetime":
            time = local_time()
            return result("ok", {"datetime": time.isoformat(), "weekday": time.weekday() + 1,
                                 "timezone": "Asia/Taipei"})
        if name == "get_weather":
            if self.user_context is not None:
                location = grounded_location(params["location"], self.user_context)
                if location is None:
                    return result("unavailable", reason="location_required")
                params["location"] = location
            return await get_weather(**params)
        reader = self.reader or desktop_reader()
        if name == "get_activity_context":
            return await reader.read("snapshot", timeout=1, include_title=True)
        if name == "get_computer_status":
            return await reader.read("computer_status", timeout=1)
        return await reader.read("screenshot", timeout=2, target=params.get("target", "foreground"))

    async def execute_batch(self, calls: list[dict]) -> list[dict]:
        receipts = []
        screenshot_used = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BATCH_TIMEOUT_SECONDS
        for index, call in enumerate(calls):
            name = call.get("function", {}).get("name", "")
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
                        receipt = await self.execute(name, call.get("function", {}).get("arguments", ""))
                except TimeoutError:
                    receipt = result("unavailable", reason="tool_deadline")
            receipts.append(receipt)
        return receipts
