"""隔離 instance 的外部資料 fixture；正式連線無法指定替身內容。"""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "backend/tools/ai_test_cases/assets"


def active_fixture() -> dict | None:
    if (os.getenv("AI_VT_TEST_MODE", "").lower() != "true"
            or not re.fullmatch(r"test_[0-9a-f]{32}", os.getenv("MEMORY_DATABASE_SCHEMA", ""))):
        return None
    location = os.getenv("AI_VT_TEST_FIXTURE_PATH")
    if not location:
        return None
    value = json.loads(Path(location).read_text(encoding="utf-8"))
    if (value.get("version") != 1 or value.get("schema") != os.environ["MEMORY_DATABASE_SCHEMA"]
            or value.get("suite") not in {"chat", "memory", "tools", "integration", "jev-emotion"}):
        raise ValueError("fixture_invalid: runtime manifest")
    if not re.fullmatch(r"(?:ch|mem|to|it|je)_[0-9]{3}", value.get("case_id", "")):
        raise ValueError("fixture_invalid: case ID")
    if value.get("vision_rejection") and value["case_id"] not in {"to_007", "it_012"}:
        raise ValueError("fixture_invalid: unexpected vision fault")
    if value["suite"] == "integration" and (value.get("tools") or value.get("snapshot")):
        raise ValueError("fixture_invalid: integration requires live sources")
    return value


def fixture_timestamp(timestamp: float) -> float:
    fixture = active_fixture()
    if fixture is None or fixture["suite"] == "integration":
        return timestamp
    reference = datetime.fromisoformat(fixture["reference_time"])
    if reference.tzinfo is None:
        raise ValueError("fixture_invalid: clock timezone")
    return reference.timestamp()


def fixture_snapshot() -> dict | None:
    fixture = active_fixture()
    if fixture is None or fixture["suite"] == "integration":
        return None
    snapshot = copy.deepcopy(fixture.get("snapshot") or {
        "status": "ok", "data": {
            "foreground": {"status": "ok", "app": "FixtureEditor.exe"},
            "open_apps": {"status": "ok", "apps": ["FixtureEditor.exe"], "truncated": False},
        },
    })
    # 自動快照永遠不提供標題；工具才提供當輪標題。
    (snapshot.get("data") or {}).get("foreground", {}).pop("title", None)
    snapshot.update(captured_at=fixture["reference_time"], reason=snapshot.get("reason"))
    return snapshot


def fixture_tool(name: str, params: dict) -> dict | None:
    fixture = active_fixture()
    if fixture is None or fixture["suite"] == "integration":
        return None
    value = copy.deepcopy((fixture.get("tools") or {}).get(name))
    if value is None:
        # 專項禁止因缺 fixture 意外讀取私人桌面或付外部來源成本。
        return {"status": "unavailable", "captured_at": fixture["reference_time"],
                "data": None, "reason": "fixture_tool_unavailable"}
    value.setdefault("data", None)
    value.setdefault("reason", None)
    value["captured_at"] = fixture["reference_time"]
    if name == "get_datetime" and value["status"] == "ok":
        if value["data"].get("datetime") in {None, "$reference_time"}:
            value["data"]["datetime"] = fixture["reference_time"]
        value["data"]["weekday"] = datetime.fromisoformat(value["data"]["datetime"]).weekday() + 1
        value["data"]["timezone"] = "Asia/Taipei"
    if name == "get_weather":
        expected = fixture.get("weather_location")
        if expected and params.get("location") != expected:
            return {"status": "unavailable", "captured_at": fixture["reference_time"],
                    "data": None, "reason": "fixture_location_mismatch"}
    if name == "capture_screenshot" and value["status"] == "ok":
        from PIL import Image
        from infrastructure.windows_desktop import WindowsDesktop
        path = (ROOT / value.pop("asset")).resolve()
        if not path.is_relative_to(ASSETS.resolve()):
            raise ValueError("fixture_invalid: screenshot asset path")
        payload = path.read_bytes()
        with Image.open(io.BytesIO(payload)) as image:
            value = WindowsDesktop._encode_screenshot(image, params.get("target", "foreground"), {
                "kind": "fixture_image", "ref": path.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        value["captured_at"] = fixture["reference_time"]
        # 只保存已核准的合成圖片；實機桌面永遠不走這個分支。
        output = Path(os.environ["AI_VT_TEST_FIXTURE_PATH"]).resolve().parent / "assets"
        output.mkdir(parents=True, exist_ok=True)
        name = fixture["case_id"] + "-1.jpg"
        (output / name).write_bytes(value["image"])
        value["data"]["source"]["report_asset"] = "assets/" + name
    return value


def reject_fixture_image(messages: list) -> bool:
    fixture = active_fixture()
    return bool(fixture and fixture.get("vision_rejection") and any(
        isinstance(message.get("content"), list) and any(
            part.get("type") == "image_url" for part in message["content"]
        ) for message in messages
    ))
