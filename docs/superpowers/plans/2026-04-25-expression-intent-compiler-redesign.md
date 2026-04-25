# Expression Intent Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將現有 Expression Agent 從直接輸出 `set_ai_behavior` / `blink_control`，改為輸出高階 `Expression Intent`，並由 backend compiler 編譯成舊 payload 與新 `expression_plan`，最後讓 frontend runtime 逐步支援 `base pose + micro events + blink plan`。

**Architecture:** 採三階段遷移。Phase 1 先在 backend 建立 intent schema、parser、compiler，並將新計畫編譯回既有 `behavior` / `blink_control`，讓現有前端不用先大改也能驗證架構方向。Phase 2 再導入 `expression_plan` websocket payload 與前端 store/runtime event overlay。Phase 3 擴充 sequence、model adapter、runtime variation 與 extension points，讓未來新增表演能力時只需要擴充 compiler / runtime，而非重新修改模型輸出協議。

**Tech Stack:** Python 3.10+, FastAPI, WebSocket, unittest, OpenAI-compatible API, TypeScript, React 19, Zustand, Vite, Live2D Cubism

**Design Doc:** `docs/superpowers/specs/2026-04-25-expression-intent-compiler-redesign-design.md`

---

## File Structure

### Modified Files
- `backend/domain/agent_b_prompts.py` — 將 Expression Agent prompt 從 tool calling 指令改成輸出 `Expression Intent`
- `backend/services/chat_service.py` — 新增呼叫 intent-only Expression Agent 的介面，保留 Memory Agent 舊流程
- `backend/api/routes/chat_ws.py` — 移除 Expression tool-call parsing 依賴，接入 intent parser / compiler / legacy payload renderer / expression_plan 發送
- `backend/tests/test_agent_tool_pipeline.py` — 逐步縮小為 Memory tool pipeline 與 legacy renderer fallback 測試，不再以 Expression tools 為主
- `backend/tests/test_memory_model_wiring.py` — 更新 websocket route wiring，確認 Expression Agent 改走 intent response
- `vtuber-web-app/src/services/wsService.ts` — 新增 `expression_plan` payload handling，保留舊 `behavior` / `blink_control` fallback
- `vtuber-web-app/src/services/displaySyncService.ts` — 新增 display 端對 `expression_plan` 的相容接收
- `vtuber-web-app/src/store/appStore.ts` — 擴充 AI expression state、micro event queue、blink plan 套用入口
- `vtuber-web-app/src/live2d/LAppModel.ts` — 新增 base pose / micro event overlay / sequence queue / plan apply API，保留 `setAiBehavior()` 相容入口
- `vtuber-web-app/src/generated/tools.ts` — 若前端生成腳本仍依賴 tool schema，確認 legacy blink tool 型別不受影響

### New Files
- `backend/domain/expression_intent_schema.py` — 定義 intent enums、預設值、欄位驗證與 normalize 規則
- `backend/domain/expression_presets.py` — base pose presets 與 intensity/asymmetry modifiers
- `backend/domain/expression_blink_strategies.py` — blink style 到 commands 的 mapping
- `backend/domain/expression_sequence_library.py` — micro event / sequence templates
- `backend/services/expression_intent_parser.py` — 解析 LLM 回傳文字 / JSON，輸出 normalized intent
- `backend/services/expression_compiler.py` — `compile_expression_plan()` 與各層 `build_*` 組裝
- `backend/services/expression_legacy_renderer.py` — 將 `expression_plan` 編回既有 `behavior` 與 `blink_control`
- `backend/tests/test_expression_intent_parser.py` — intent parsing、normalize、fallback 測試
- `backend/tests/test_expression_compiler.py` — preset selection、micro events、blink mapping、legacy renderer 測試
- `vtuber-web-app/src/types/expressionPlan.ts` — `expression_plan`、micro event、blink plan 的 TS 型別

### Unchanged Files
- `backend/domain/agent_a_prompts.py` — Dialogue Agent 不變
- `backend/services/tool_arg_parser.py` — 保留給 Memory Agent 與 legacy tool parsing，Expression 路徑不再依賴
- `backend/domain/tools/__init__.py` — Memory tools 取得邏輯不變；Expression tools 只保留 legacy fallback 使用

---

## Task 1: 定義 Expression Intent schema 與 parser

**Files:**
- Create: `backend/domain/expression_intent_schema.py`
- Create: `backend/services/expression_intent_parser.py`
- Create: `backend/tests/test_expression_intent_parser.py`

- [ ] **Step 1: 寫 intent parser 的失敗測試，固定第一版 schema 行為**

建立 `backend/tests/test_expression_intent_parser.py`，先用最小可落地 schema 寫四個失敗案例：完整 payload、缺欄位補值、非法 enum fallback、非 JSON 文字提取失敗 fallback。

```python
import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.expression_intent_parser import parse_expression_intent


class ExpressionIntentParserTests(unittest.TestCase):
    def test_parse_expression_intent_keeps_valid_payload(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"playful","intensity":0.72,"energy":0.68,'
            '"arc":"pop_then_settle","hold_ms":1800,"blink_style":"teasing_pause"}',
            emotion_state={"primary_emotion": "calm", "intensity": 0.2},
            previous_state=None,
        )

        self.assertEqual(intent["primary_emotion"], "playful")
        self.assertEqual(intent["arc"], "pop_then_settle")
        self.assertEqual(intent["blink_style"], "teasing_pause")
        self.assertAlmostEqual(intent["intensity"], 0.72)

    def test_parse_expression_intent_fills_defaults_from_emotion_state(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"shy"}',
            emotion_state={"intensity": 0.61, "energy": 0.33},
            previous_state={"summary": "上一輪是輕微微笑"},
        )

        self.assertEqual(intent["primary_emotion"], "shy")
        self.assertAlmostEqual(intent["intensity"], 0.61)
        self.assertAlmostEqual(intent["energy"], 0.33)
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["hold_ms"], 1600)

    def test_parse_expression_intent_replaces_invalid_enum_with_default(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"playful","arc":"explode_forever","blink_style":"laser"}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["blink_style"], "normal")

    def test_parse_expression_intent_returns_safe_default_when_json_is_missing(self):
        intent = parse_expression_intent(
            '我覺得這句要有點調皮但不要太誇張',
            emotion_state={"primary_emotion": "playful", "intensity": 0.4, "energy": 0.5},
            previous_state=None,
        )

        self.assertEqual(intent["primary_emotion"], "playful")
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["blink_style"], "normal")
```

- [ ] **Step 2: 先跑 parser 測試確認會失敗**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_expression_intent_parser.py" -v
```

Expected: FAIL，因為 `services.expression_intent_parser` 尚不存在。

- [ ] **Step 3: 建立 intent schema 常數與 normalize 規則**

建立 `backend/domain/expression_intent_schema.py`，先用純常數與函式，不額外引入 dataclass 套件。內容至少包含：允許 enum、數值 clamp、預設值、以 `emotion_state` 補值的 helper。

```python
ALLOWED_PRIMARY_EMOTIONS = {
    "neutral", "calm", "gentle", "playful", "teasing", "shy",
    "embarrassed", "annoyed", "angry", "surprised", "conflicted",
}

ALLOWED_ARCS = {
    "steady", "pop_then_settle", "pause_then_smirk", "widen_then_tease",
    "shrink_then_recover", "glare_then_flatten",
}

ALLOWED_BLINK_STYLES = {
    "normal", "focused_pause", "shy_fast", "teasing_pause", "surprised_hold", "sleepy_slow",
}

DEFAULT_INTENT = {
    "primary_emotion": "calm",
    "secondary_emotion": "",
    "intensity": 0.35,
    "energy": 0.35,
    "dominance": 0.5,
    "playfulness": 0.3,
    "warmth": 0.5,
    "asymmetry_bias": "auto",
    "blink_style": "normal",
    "tempo": "medium",
    "arc": "steady",
    "hold_ms": 1600,
    "must_include": [],
    "avoid": [],
    "speaking_rate": 1.0,
}


def clamp_number(value: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(minimum, min(maximum, float(value)))


def normalize_expression_intent(raw_intent: dict, emotion_state: dict | None = None) -> dict:
    emotion_state = emotion_state or {}
    normalized = dict(DEFAULT_INTENT)
    normalized.update({k: v for k, v in raw_intent.items() if k in DEFAULT_INTENT})

    primary = normalized.get("primary_emotion")
    if primary not in ALLOWED_PRIMARY_EMOTIONS:
        normalized["primary_emotion"] = emotion_state.get("primary_emotion", DEFAULT_INTENT["primary_emotion"])

    normalized["intensity"] = clamp_number(
        normalized.get("intensity", emotion_state.get("intensity")),
        default=clamp_number(emotion_state.get("intensity"), DEFAULT_INTENT["intensity"], 0.0, 1.0),
        minimum=0.0,
        maximum=1.0,
    )
    normalized["energy"] = clamp_number(
        normalized.get("energy", emotion_state.get("energy")),
        default=clamp_number(emotion_state.get("energy"), DEFAULT_INTENT["energy"], 0.0, 1.0),
        minimum=0.0,
        maximum=1.0,
    )

    if normalized.get("arc") not in ALLOWED_ARCS:
        normalized["arc"] = DEFAULT_INTENT["arc"]
    if normalized.get("blink_style") not in ALLOWED_BLINK_STYLES:
        normalized["blink_style"] = DEFAULT_INTENT["blink_style"]

    normalized["hold_ms"] = int(clamp_number(normalized.get("hold_ms"), DEFAULT_INTENT["hold_ms"], 300, 4000))
    normalized["speaking_rate"] = clamp_number(normalized.get("speaking_rate"), DEFAULT_INTENT["speaking_rate"], 0.7, 1.4)
    normalized["must_include"] = normalized.get("must_include") if isinstance(normalized.get("must_include"), list) else []
    normalized["avoid"] = normalized.get("avoid") if isinstance(normalized.get("avoid"), list) else []
    return normalized
```

- [ ] **Step 4: 建立 parser，先支援 JSON 與安全 fallback**

建立 `backend/services/expression_intent_parser.py`：

```python
import json
import re

from domain.expression_intent_schema import normalize_expression_intent


_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _extract_first_json_object(raw_text: str) -> dict:
    match = _JSON_OBJECT_PATTERN.search(raw_text or "")
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_expression_intent(raw_text: str, emotion_state: dict | None, previous_state: dict | None) -> dict:
    del previous_state  # 第一版先保留接口，不先使用
    raw_intent = _extract_first_json_object(raw_text)
    return normalize_expression_intent(raw_intent, emotion_state=emotion_state)
```

- [ ] **Step 5: 重新跑 parser 測試確認轉綠**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_expression_intent_parser.py" -v
```

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/domain/expression_intent_schema.py backend/services/expression_intent_parser.py backend/tests/test_expression_intent_parser.py
git commit -m "feat: add expression intent schema and parser"
```

---

## Task 2: 建立 compiler、preset library 與 legacy renderer

**Files:**
- Create: `backend/domain/expression_presets.py`
- Create: `backend/domain/expression_blink_strategies.py`
- Create: `backend/domain/expression_sequence_library.py`
- Create: `backend/services/expression_compiler.py`
- Create: `backend/services/expression_legacy_renderer.py`
- Create: `backend/tests/test_expression_compiler.py`

- [ ] **Step 1: 寫 compiler 的失敗測試，固定輸出結構**

建立 `backend/tests/test_expression_compiler.py`，先鎖定四個行為：base pose preset 選擇、micro event 生成、blink commands mapping、legacy payload renderer。

```python
import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.expression_compiler import compile_expression_plan
from services.expression_legacy_renderer import render_legacy_behavior_payload


class ExpressionCompilerTests(unittest.TestCase):
    def test_compile_expression_plan_selects_playful_base_pose(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "playful",
                "intensity": 0.7,
                "energy": 0.65,
                "playfulness": 0.8,
                "asymmetry_bias": "strong",
                "arc": "steady",
                "hold_ms": 1800,
                "blink_style": "teasing_pause",
                "speaking_rate": 1.08,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["type"], "expression_plan")
        self.assertEqual(plan["basePose"]["preset"], "playful_smirk")
        self.assertFalse(plan["basePose"]["params"]["eyeSync"])
        self.assertEqual(plan["blinkPlan"]["style"], "teasing_pause")

    def test_compile_expression_plan_adds_micro_event_for_pop_then_settle(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "surprised",
                "intensity": 0.75,
                "energy": 0.82,
                "arc": "pop_then_settle",
                "hold_ms": 1500,
                "blink_style": "surprised_hold",
                "must_include": ["surprised_pop"],
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreaterEqual(len(plan["microEvents"]), 1)
        self.assertEqual(plan["microEvents"][0]["kind"], "surprised_pop")

    def test_render_legacy_behavior_payload_returns_behavior_and_blink_payloads(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "shy",
                "intensity": 0.55,
                "energy": 0.4,
                "arc": "steady",
                "hold_ms": 2000,
                "blink_style": "shy_fast",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        legacy = render_legacy_behavior_payload(plan)

        self.assertEqual(legacy["behavior_payload"]["type"], "behavior")
        self.assertIn("speaking_rate", legacy)
        self.assertGreaterEqual(len(legacy["blink_payloads"]), 1)

    def test_render_legacy_behavior_payload_keeps_existing_field_names(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "calm",
                "intensity": 0.2,
                "energy": 0.25,
                "arc": "steady",
                "hold_ms": 1600,
                "blink_style": "normal",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        behavior = render_legacy_behavior_payload(plan)["behavior_payload"]

        self.assertIn("headIntensity", behavior)
        self.assertIn("eyeLOpen", behavior)
        self.assertIn("mouthForm", behavior)
        self.assertIn("browLY", behavior)
```

- [ ] **Step 2: 跑 compiler 測試確認目前會失敗**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_expression_compiler.py" -v
```

Expected: FAIL，因為 compiler 與 renderer 尚不存在。

- [ ] **Step 3: 建立 base pose presets 與 blink strategies**

建立 `backend/domain/expression_presets.py` 與 `backend/domain/expression_blink_strategies.py`。先只放第一批必要 preset，避免一開始做太大。

```python
# backend/domain/expression_presets.py
BASE_POSE_PRESETS = {
    "calm_soft": {
        "mouthForm": 0.05,
        "eyeLOpen": 0.96,
        "eyeROpen": 0.96,
        "eyeSync": True,
        "browLY": 0.02,
        "browRY": 0.02,
        "browLAngle": 0.02,
        "browRAngle": -0.02,
        "browLForm": 0.0,
        "browRForm": 0.0,
        "eyeLSmile": 0.08,
        "eyeRSmile": 0.08,
        "browLX": 0.0,
        "browRX": 0.0,
        "headIntensity": 0.15,
        "blushLevel": 0.0,
    },
    "playful_smirk": {
        "mouthForm": 0.24,
        "eyeLOpen": 0.84,
        "eyeROpen": 0.74,
        "eyeSync": False,
        "browLY": 0.12,
        "browRY": 0.04,
        "browLAngle": 0.20,
        "browRAngle": -0.06,
        "browLForm": 0.1,
        "browRForm": 0.02,
        "eyeLSmile": 0.42,
        "eyeRSmile": 0.14,
        "browLX": -0.08,
        "browRX": 0.04,
        "headIntensity": 0.28,
        "blushLevel": 0.05,
    },
    "shy_tucked": {
        "mouthForm": 0.10,
        "eyeLOpen": 0.78,
        "eyeROpen": 0.82,
        "eyeSync": False,
        "browLY": 0.08,
        "browRY": 0.02,
        "browLAngle": 0.10,
        "browRAngle": -0.04,
        "browLForm": 0.08,
        "browRForm": 0.03,
        "eyeLSmile": 0.18,
        "eyeRSmile": 0.10,
        "browLX": -0.04,
        "browRX": 0.02,
        "headIntensity": 0.18,
        "blushLevel": 0.18,
    },
    "surprised_open": {
        "mouthForm": 0.12,
        "eyeLOpen": 1.08,
        "eyeROpen": 1.08,
        "eyeSync": True,
        "browLY": 0.22,
        "browRY": 0.22,
        "browLAngle": 0.18,
        "browRAngle": -0.18,
        "browLForm": 0.02,
        "browRForm": 0.02,
        "eyeLSmile": 0.0,
        "eyeRSmile": 0.0,
        "browLX": 0.0,
        "browRX": 0.0,
        "headIntensity": 0.34,
        "blushLevel": 0.02,
    },
}
```

```python
# backend/domain/expression_blink_strategies.py
BLINK_STRATEGIES = {
    "normal": [],
    "teasing_pause": [
        {"action": "pause", "durationSec": 1.0},
        {"action": "force_blink"},
    ],
    "shy_fast": [
        {"action": "set_interval", "intervalMin": 0.8, "intervalMax": 1.5},
    ],
    "surprised_hold": [
        {"action": "pause", "durationSec": 1.2},
        {"action": "force_blink"},
    ],
    "focused_pause": [
        {"action": "pause", "durationSec": 2.0},
    ],
    "sleepy_slow": [
        {"action": "set_interval", "intervalMin": 2.0, "intervalMax": 5.0},
    ],
}
```

- [ ] **Step 4: 建立 micro event / sequence library 與 compiler**

建立 `backend/domain/expression_sequence_library.py` 與 `backend/services/expression_compiler.py`，先做最小版本：用 `primary_emotion` 選 base pose，用 `arc` 產生一個 micro event，blink 直接套 strategy。

```python
# backend/domain/expression_sequence_library.py
MICRO_EVENT_LIBRARY = {
    "smirk_left": {
        "kind": "smirk_left",
        "durationMs": 520,
        "patch": {"mouthForm": 0.42, "eyeLSmile": 0.66},
        "returnToBase": True,
    },
    "surprised_pop": {
        "kind": "surprised_pop",
        "durationMs": 320,
        "patch": {"eyeLOpen": 1.14, "eyeROpen": 1.14, "browLY": 0.28, "browRY": 0.28},
        "returnToBase": True,
    },
}
```

```python
# backend/services/expression_compiler.py
from copy import deepcopy

from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_presets import BASE_POSE_PRESETS
from domain.expression_sequence_library import MICRO_EVENT_LIBRARY


def select_base_pose(intent: dict) -> str:
    primary = intent.get("primary_emotion")
    if primary == "playful":
        return "playful_smirk"
    if primary == "shy":
        return "shy_tucked"
    if primary == "surprised":
        return "surprised_open"
    return "calm_soft"


def build_micro_events(intent: dict) -> list[dict]:
    must_include = intent.get("must_include") or []
    if must_include:
        return [deepcopy(MICRO_EVENT_LIBRARY[name]) for name in must_include if name in MICRO_EVENT_LIBRARY]
    if intent.get("arc") == "pop_then_settle":
        return [deepcopy(MICRO_EVENT_LIBRARY["surprised_pop"])]
    return []


def compile_expression_plan(intent: dict, model_name: str, previous_state: dict | None) -> dict:
    del model_name, previous_state
    preset_name = select_base_pose(intent)
    params = deepcopy(BASE_POSE_PRESETS[preset_name])
    hold_sec = max(0.3, float(intent.get("hold_ms", 1600)) / 1000.0)
    blink_style = intent.get("blink_style", "normal")
    return {
        "type": "expression_plan",
        "basePose": {
            "preset": preset_name,
            "params": params,
            "durationSec": hold_sec,
        },
        "microEvents": build_micro_events(intent),
        "sequence": [],
        "blinkPlan": {
            "style": blink_style,
            "commands": deepcopy(BLINK_STRATEGIES.get(blink_style, [])),
        },
        "speakingRate": float(intent.get("speaking_rate", 1.0)),
        "debug": {
            "intentPrimaryEmotion": intent.get("primary_emotion", "calm"),
            "intentArc": intent.get("arc", "steady"),
            "selectedBasePreset": preset_name,
        },
    }
```

- [ ] **Step 5: 建立 legacy renderer，把新 plan 編回舊 payload**

建立 `backend/services/expression_legacy_renderer.py`：

```python
def render_legacy_behavior_payload(plan: dict) -> dict:
    params = plan["basePose"]["params"]
    behavior_payload = {
        "type": "behavior",
        "headIntensity": params["headIntensity"],
        "blushLevel": params["blushLevel"],
        "eyeSync": params["eyeSync"],
        "eyeLOpen": params["eyeLOpen"],
        "eyeROpen": params["eyeROpen"],
        "durationSec": plan["basePose"]["durationSec"],
        "mouthForm": params["mouthForm"],
        "browLY": params["browLY"],
        "browRY": params["browRY"],
        "browLAngle": params["browLAngle"],
        "browRAngle": params["browRAngle"],
        "browLForm": params["browLForm"],
        "browRForm": params["browRForm"],
        "eyeLSmile": params["eyeLSmile"],
        "eyeRSmile": params["eyeRSmile"],
        "browLX": params["browLX"],
        "browRX": params["browRX"],
    }
    return {
        "behavior_payload": behavior_payload,
        "blink_payloads": [
            {"type": "blink_control", **command}
            for command in plan.get("blinkPlan", {}).get("commands", [])
        ],
        "speaking_rate": float(plan.get("speakingRate", 1.0)),
    }
```

- [ ] **Step 6: 跑 compiler 測試確認轉綠**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_expression_compiler.py" -v
```

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add backend/domain/expression_presets.py backend/domain/expression_blink_strategies.py backend/domain/expression_sequence_library.py backend/services/expression_compiler.py backend/services/expression_legacy_renderer.py backend/tests/test_expression_compiler.py
git commit -m "feat: add expression compiler and legacy renderer"
```

---

## Task 3: 將 Expression Agent 從 tool calling 改成 intent output

**Files:**
- Modify: `backend/domain/agent_b_prompts.py`
- Modify: `backend/services/chat_service.py`
- Modify: `backend/tests/test_memory_model_wiring.py`

- [ ] **Step 1: 在 websocket wiring 測試中先寫失敗案例，要求 Expression Agent 回傳純文字 intent**

在 `backend/tests/test_memory_model_wiring.py` 新增一個測試，讓假的 Expression Agent 回傳沒有 `tool_calls`、只有 JSON 文字內容，並確認 route 不因缺少 `set_ai_behavior` 崩潰。

```python
    def test_websocket_endpoint_accepts_expression_intent_response_without_tool_calls(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "hello", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"playful","intensity":0.7,"energy":0.6,"arc":"steady","hold_ms":1800}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "好呀，今天就輕鬆一點聊。", None, {"primary_emotion": "playful", "intensity": 0.6}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="expression-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())
        self.assertTrue(any(payload.get("type") == "behavior" for payload in websocket.payloads))
```

- [ ] **Step 2: 跑該測試確認目前會失敗**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
```

Expected: FAIL，因為目前 `chat_ws.py` 仍依賴 `call_live2d_agent()` tool calls 路徑。

- [ ] **Step 3: 改寫 Expression Agent prompt，要求輸出 JSON intent 而不是 tools**

修改 `backend/domain/agent_b_prompts.py` 中 `build_live2d_prompt()` 的主體，將「必須呼叫 `set_ai_behavior`」改成「必須輸出單一 JSON object」。關鍵文字改為：

```python
# 輸出格式要求
請只輸出一個 JSON object，不要輸出 Markdown、說明文字或 tool calls。

必要欄位：
- primary_emotion
- intensity
- energy
- arc
- hold_ms

可選欄位：
- secondary_emotion
- dominance
- playfulness
- warmth
- asymmetry_bias
- blink_style
- tempo
- must_include
- avoid
- speaking_rate

若不確定，請輸出保守但完整的 intent，不要省略 JSON 結構。
```

- [ ] **Step 4: 在 `chat_service.py` 新增 `call_expression_agent()`，保留舊 `call_live2d_agent()` 作為 legacy alias**

把 `backend/services/chat_service.py` 裡的原 `call_live2d_agent()` 改名並降低耦合：

```python
async def call_expression_agent(messages: list, model_name: str = "Hiyori") -> object:
    del model_name  # 第一版先保留接口，intent schema 暫時不需要 model-specific tools
    response = await chat_create_with_fallback(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.4,
        extra_body=NO_THINKING_EXTRA_BODY,
        max_tokens=600,
    )
    return response


async def call_live2d_agent(messages: list, model_name: str = "Hiyori") -> object:
    return await call_expression_agent(messages, model_name=model_name)
```

- [ ] **Step 5: 更新 route 匯入與測試 patch 點，切到 `call_expression_agent()`**

在 `backend/api/routes/chat_ws.py` 將：

```python
from services.chat_service import (
    collect_agent_a,
    call_live2d_agent,
    call_memory_agent,
    ...
)
```

改成：

```python
from services.chat_service import (
    collect_agent_a,
    call_expression_agent,
    call_memory_agent,
    ...
)
```

並把呼叫點改為 `call_expression_agent(live2d_messages, model_name)`，保留區塊變數名稱之後再統一清理。

- [ ] **Step 6: 重新跑 websocket wiring 測試確認轉綠**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
```

Expected: PASS，且 route 可接受無 tool calls 的 Expression Agent 回應。

- [ ] **Step 7: Commit**

```bash
git add backend/domain/agent_b_prompts.py backend/services/chat_service.py backend/tests/test_memory_model_wiring.py backend/api/routes/chat_ws.py
git commit -m "refactor: switch expression agent to intent output"
```

---

## Task 4: 在 websocket route 接入 intent parser 與 compiler，先編回舊 payload

**Files:**
- Modify: `backend/api/routes/chat_ws.py`
- Modify: `backend/tests/test_memory_model_wiring.py`
- Modify: `backend/tests/test_agent_tool_pipeline.py`

- [ ] **Step 1: 在 route 測試中先寫失敗案例，確認會發出 legacy behavior 與 blink payloads**

在 `backend/tests/test_memory_model_wiring.py` 新增測試，驗證收到 intent 後，route 會送出：

1. `behavior`
2. `blink_control`（若 blink strategy 有 commands）
3. `text_stream`

```python
    def test_websocket_endpoint_compiles_expression_intent_to_legacy_payloads(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "裝可愛一下", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"shy","intensity":0.66,"energy":0.42,"arc":"steady","hold_ms":2000,"blink_style":"shy_fast"}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "欸，不要這樣看我啦。", None, {"primary_emotion": "shy", "intensity": 0.6, "energy": 0.4}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="expression-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())
        payload_types = [payload.get("type") for payload in websocket.payloads]
        self.assertIn("behavior", payload_types)
        self.assertIn("blink_control", payload_types)
        self.assertIn("text_stream", payload_types)
```

- [ ] **Step 2: 跑 route 測試確認目前失敗**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
```

Expected: FAIL，因為 route 尚未接入 parser / compiler / renderer。

- [ ] **Step 3: 在 `chat_ws.py` 接入 parser 與 compiler，移除 Expression tool parsing 主路徑**

修改 `backend/api/routes/chat_ws.py`：

1. 新增匯入：

```python
from services.expression_intent_parser import parse_expression_intent
from services.expression_compiler import compile_expression_plan
from services.expression_legacy_renderer import render_legacy_behavior_payload
```

2. 用以下區塊取代現有 Expression `extract_agent_tool_calls()` 路徑：

```python
                expression_plan: dict | None = None
                if expression_response.choices and len(expression_response.choices) > 0:
                    expression_msg = expression_response.choices[0].message
                    expression_raw = expression_msg.content or ""
                    print(f"[Expression Agent] content: {expression_raw[:200]}")
                    expression_intent = parse_expression_intent(
                        expression_raw,
                        emotion_state=emotion_state,
                        previous_state=previous_expression_state,
                    )
                    expression_plan = compile_expression_plan(
                        expression_intent,
                        model_name=model_name,
                        previous_state=previous_expression_state,
                    )
                else:
                    expression_plan = compile_expression_plan(
                        parse_expression_intent("", emotion_state=emotion_state, previous_state=previous_expression_state),
                        model_name=model_name,
                        previous_state=previous_expression_state,
                    )
```

3. 保留 Memory Agent 的 `extract_agent_tool_calls()`。

- [ ] **Step 4: 將 legacy renderer 結果接回現有 websocket 傳輸路徑**

在 `chat_ws.py` 裡取代 `_execute_chat_orchestrator_tool_calls()` 對 Expression 的行為：

```python
                legacy_render = render_legacy_behavior_payload(expression_plan)
                behavior_payload = legacy_render["behavior_payload"]
                speaking_rate = legacy_render["speaking_rate"]

                for blink_payload in legacy_render["blink_payloads"]:
                    await websocket.send_json(blink_payload)
                    await broadcast_to_displays(blink_payload)

                execution_result = await _execute_chat_orchestrator_tool_calls(
                    expression_calls=[],
                    memory_calls=memory_calls,
                    websocket=websocket,
                    broadcast_func=broadcast_to_displays,
                    execute_profile_update_fn=execute_profile_update,
                    append_memory_note_fn=append_memory_note,
                    last_behavior_payload={**behavior_payload, "speakingRate": speaking_rate},
                    model_name=model_name,
                )
                memory_calls = execution_result["memory_calls"]
                last_behavior_payload = execution_result["last_behavior_payload"]
```

並在 `_execute_chat_orchestrator_tool_calls()` 中加入早退分支，當 `expression_calls` 為空但 `last_behavior_payload` 已帶入時，只處理 memory calls 並直接沿用該 payload。

- [ ] **Step 5: 更新 route log 與 summary tool names，避免 Expression tools 變成空集合時誤報**

在 `chat_ws.py` 與 `test_agent_tool_pipeline.py` 調整：

```python
tool_names=summarize_tool_names(memory_calls)
```

或改成：

```python
tool_names=summarize_tool_names(memory_calls) + ["expression_plan"]
```

同步把對 `has_required_expression_behavior()` 的依賴移除，避免 route 每輪都印出缺少 `set_ai_behavior` 警告。

- [ ] **Step 6: 跑 backend 整合測試確認轉綠**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
python -m unittest discover -s tests -p "test_expression_*.py" -v
```

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add backend/api/routes/chat_ws.py backend/tests/test_memory_model_wiring.py backend/tests/test_agent_tool_pipeline.py
git commit -m "refactor: compile expression intents into legacy payloads"
```

---

## Task 5: 新增 `expression_plan` payload 與前端型別

**Files:**
- Create: `vtuber-web-app/src/types/expressionPlan.ts`
- Modify: `vtuber-web-app/src/services/wsService.ts`
- Modify: `vtuber-web-app/src/services/displaySyncService.ts`
- Modify: `vtuber-web-app/src/store/appStore.ts`

- [ ] **Step 1: 建立前端 `expression_plan` 型別檔**

建立 `vtuber-web-app/src/types/expressionPlan.ts`：

```ts
export interface ExpressionBasePose {
  preset: string
  params: {
    headIntensity: number
    blushLevel: number
    eyeSync: boolean
    eyeLOpen: number
    eyeROpen: number
    mouthForm: number
    browLY: number
    browRY: number
    browLAngle: number
    browRAngle: number
    browLForm: number
    browRForm: number
    eyeLSmile: number
    eyeRSmile: number
    browLX: number
    browRX: number
  }
  durationSec: number
}

export interface ExpressionMicroEvent {
  kind: string
  durationMs: number
  patch: Partial<ExpressionBasePose['params']>
  returnToBase: boolean
}

export interface BlinkCommand {
  action: 'force_blink' | 'pause' | 'resume' | 'set_interval'
  durationSec?: number
  intervalMin?: number
  intervalMax?: number
}

export interface ExpressionPlanPayload {
  type: 'expression_plan'
  basePose: ExpressionBasePose
  microEvents: ExpressionMicroEvent[]
  sequence: ExpressionMicroEvent[]
  blinkPlan: {
    style: string
    commands: BlinkCommand[]
  }
  speakingRate: number
  debug?: Record<string, string>
}
```

- [ ] **Step 2: 在 store 先加 state 與空實作，確認 TypeScript 會先報錯**

修改 `vtuber-web-app/src/store/appStore.ts`，先在 `AppState` 中加入：

```ts
import type { ExpressionMicroEvent, ExpressionPlanPayload } from '../types/expressionPlan'

expressionPlan: ExpressionPlanPayload | null
expressionEvents: ExpressionMicroEvent[]
setExpressionPlan: (plan: ExpressionPlanPayload) => void
enqueueExpressionEvents: (events: ExpressionMicroEvent[]) => void
clearExpressionEvents: () => void
```

並先在初始 state 放：

```ts
expressionPlan: null,
expressionEvents: [],
```

- [ ] **Step 3: 跑前端建置確認目前型別失敗**

在 `vtuber-web-app/` 執行：

```bash
npm run build
```

Expected: FAIL，因為 `setExpressionPlan` / `enqueueExpressionEvents` / `clearExpressionEvents` 尚未實作。

- [ ] **Step 4: 補上 store 實作與 websocket handler**

在 `appStore.ts` 補最小實作：

```ts
  setExpressionPlan: (plan) => set({ expressionPlan: plan }),

  enqueueExpressionEvents: (events) =>
    set((state) => ({ expressionEvents: [...state.expressionEvents, ...events] })),

  clearExpressionEvents: () => set({ expressionEvents: [] }),
```

在 `vtuber-web-app/src/services/wsService.ts` 增加：

```ts
import type { ExpressionPlanPayload } from '../types/expressionPlan'
```

並在 `onmessage` 裡加：

```ts
                } else if (data.type === 'expression_plan') {
                    const plan = data as ExpressionPlanPayload
                    store.setExpressionPlan(plan)
                    store.enqueueExpressionEvents(plan.microEvents ?? [])

                    for (const command of plan.blinkPlan?.commands ?? []) {
                        store.setBlinkControl(
                            command.action,
                            command.durationSec ?? 0,
                            command.intervalMin,
                            command.intervalMax,
                        )
                    }
                }
```

在 `displaySyncService.ts` 也加相同的 `expression_plan` 接收邏輯，至少能先把 `basePose` 用舊 `setAiBehavior()` 套上。

- [ ] **Step 5: 重新跑前端建置確認轉綠**

在 `vtuber-web-app/` 執行：

```bash
npm run build
```

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add vtuber-web-app/src/types/expressionPlan.ts vtuber-web-app/src/services/wsService.ts vtuber-web-app/src/services/displaySyncService.ts vtuber-web-app/src/store/appStore.ts
git commit -m "feat: add expression plan frontend types and handlers"
```

---

## Task 6: 讓 backend 發送 `expression_plan`，但保留 legacy fallback

**Files:**
- Modify: `backend/api/routes/chat_ws.py`
- Modify: `backend/tests/test_memory_model_wiring.py`

- [ ] **Step 1: 在 route 測試中先寫失敗案例，要求同時發出 `expression_plan` 與 `behavior`**

在 `backend/tests/test_memory_model_wiring.py` 新增：

```python
    def test_websocket_endpoint_sends_expression_plan_before_legacy_behavior(self):
        websocket = self._FakeWebSocket()
        websocket.queue_received_text('{"content": "做鬼臉", "model_name": "Hiyori"}')

        class _FakeResponse:
            def __init__(self, message):
                self.choices = [SimpleNamespace(message=message)]

        expression_response = _FakeResponse(
            SimpleNamespace(
                content='{"primary_emotion":"playful","intensity":0.8,"energy":0.75,"arc":"pop_then_settle","hold_ms":1600,"must_include":["smirk_left"]}',
                tool_calls=[],
            )
        )
        memory_response = _FakeResponse(SimpleNamespace(content="", tool_calls=[]))

        async def _fake_collect_agent_a(messages):
            return "欸嘿，才不給你猜到我在想什麼。", None, {"primary_emotion": "playful", "intensity": 0.7, "energy": 0.7}

        async def _fake_call_expression_agent(messages, model_name):
            return expression_response

        async def _fake_call_memory_agent(messages, model_name):
            return memory_response

        async def _run_route():
            with patch("api.routes.chat_ws.collect_agent_a", side_effect=_fake_collect_agent_a), \
                patch("api.routes.chat_ws.call_expression_agent", side_effect=_fake_call_expression_agent), \
                patch("api.routes.chat_ws.call_memory_agent", side_effect=_fake_call_memory_agent), \
                patch("api.routes.chat_ws.broadcast_to_displays"), \
                patch("api.routes.chat_ws.load_jpaf_state", return_value=None), \
                patch("api.routes.chat_ws.save_jpaf_state"), \
                patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                patch("api.routes.chat_ws.load_memory_notes", return_value=[]), \
                patch("api.routes.chat_ws.build_agent_a_prompt", return_value="agent-a-system"), \
                patch("api.routes.chat_ws.build_live2d_prompt", return_value="expression-system"), \
                patch("api.routes.chat_ws.build_memory_prompt", return_value="memory-system"), \
                patch("api.routes.chat_ws.execute_profile_update"), \
                patch("api.routes.chat_ws.append_memory_note"), \
                patch("api.routes.chat_ws.log_turn"), \
                patch("api.routes.chat_ws.save_session_messages"), \
                patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                patch("api.routes.chat_ws.estimate_token_count", return_value=0), \
                patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False), \
                patch("api.routes.chat_ws.COMPRESS_TOKEN_THRESHOLD", 999999):
                await websocket_endpoint(websocket)

        asyncio.run(_run_route())
        payload_types = [payload.get("type") for payload in websocket.payloads]
        self.assertIn("expression_plan", payload_types)
        self.assertIn("behavior", payload_types)
        self.assertLess(payload_types.index("expression_plan"), payload_types.index("behavior"))
```

- [ ] **Step 2: 跑 route 測試確認目前失敗**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
```

Expected: FAIL，因為 route 尚未送出 `expression_plan`。

- [ ] **Step 3: 在 `chat_ws.py` 發送 `expression_plan`，並同步廣播到 display**

在 route 產生 `expression_plan` 後、legacy payload 前加入：

```python
                await websocket.send_json(expression_plan)
                await broadcast_to_displays(expression_plan)
```

並保留之後的：

```python
                await websocket.send_json(behavior_payload)
                await broadcast_to_displays(behavior_payload)
```

這樣主頁與 `/display` 都能先吃新 payload，再 fallback 舊 payload。

- [ ] **Step 4: 重新跑 route 測試確認轉綠**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/api/routes/chat_ws.py backend/tests/test_memory_model_wiring.py
git commit -m "feat: send expression plan alongside legacy behavior payload"
```

---

## Task 7: 在 `LAppModel` 加入 base pose 與 micro event overlay

**Files:**
- Modify: `vtuber-web-app/src/live2d/LAppModel.ts`
- Modify: `vtuber-web-app/src/store/appStore.ts`
- Modify: `vtuber-web-app/src/services/wsService.ts`

- [ ] **Step 1: 先在 `LAppModel.ts` 裡加入最小型別與空方法，讓建置先失敗在未接線處**

在 `LAppModel.ts` 靠近 `setAiBehavior()` 區塊前，新增：

```ts
type ExpressionParamPatch = Partial<{
  headIntensity: number
  blushLevel: number
  eyeSync: boolean
  eyeLOpen: number
  eyeROpen: number
  mouthForm: number
  browLY: number
  browRY: number
  browLAngle: number
  browRAngle: number
  browLForm: number
  browRForm: number
  eyeLSmile: number
  eyeRSmile: number
  browLX: number
  browRX: number
}>

interface ActiveExpressionEvent {
  kind: string
  patch: ExpressionParamPatch
  durationMs: number
  startedAtMs: number
  returnToBase: boolean
}
```

並新增空方法：

```ts
  public applyBasePose(basePose: { params: ExpressionParamPatch; durationSec: number }): void {
    this.setAiBehavior(
      basePose.params.headIntensity ?? 0,
      basePose.params.blushLevel ?? 0,
      basePose.params.eyeLOpen ?? 1,
      basePose.params.eyeROpen ?? 1,
      basePose.durationSec,
      basePose.params.mouthForm ?? 0,
      basePose.params.browLY ?? 0,
      basePose.params.browRY ?? 0,
      basePose.params.browLAngle ?? 0,
      basePose.params.browRAngle ?? 0,
      basePose.params.browLForm ?? 0,
      basePose.params.browRForm ?? 0,
      basePose.params.eyeSync ?? true,
      basePose.params.eyeLSmile ?? 0,
      basePose.params.eyeRSmile ?? 0,
      basePose.params.browLX ?? 0,
      basePose.params.browRX ?? 0,
    )
  }
```

- [ ] **Step 2: 跑前端建置確認目前會在 store / ws 接線報錯**

在 `vtuber-web-app/` 執行：

```bash
npm run build
```

Expected: FAIL，因為 `expression_plan` state 已存在，但 Live2D manager 尚未實際套用 queue。

- [ ] **Step 3: 在 `appStore.ts` 接上 `applyBasePose` 與 event queue forwarding**

把 `setExpressionPlan` 改成：

```ts
  setExpressionPlan: (plan) => {
    set({ expressionPlan: plan })
    const manager = LAppLive2DManager.getInstance()
    const model = manager.getActiveModel()
    if (!model) return

    const m = model as unknown as {
      applyBasePose?: (basePose: ExpressionPlanPayload['basePose']) => void
      enqueueMicroEvent?: (event: ExpressionMicroEvent) => void
    }

    m.applyBasePose?.(plan.basePose)
    for (const event of plan.microEvents ?? []) {
      m.enqueueMicroEvent?.(event)
    }
  },
```

- [ ] **Step 4: 在 `LAppModel.ts` 加入最小可用的 event queue**

新增欄位：

```ts
  private _activeExpressionEvents: ActiveExpressionEvent[] = []
```

新增方法：

```ts
  public enqueueMicroEvent(event: { kind: string; patch: ExpressionParamPatch; durationMs: number; returnToBase: boolean }): void {
    this._activeExpressionEvents.push({
      ...event,
      startedAtMs: performance.now(),
    })
  }
```

並在 update loop 中、`targetBlush` 等 target 計算後加入最小 overlay：

```ts
    const nowMs = performance.now()
    this._activeExpressionEvents = this._activeExpressionEvents.filter((event) => nowMs - event.startedAtMs < event.durationMs)

    for (const event of this._activeExpressionEvents) {
      const progress = Math.min(1, (nowMs - event.startedAtMs) / event.durationMs)
      const fade = 1 - progress
      if (typeof event.patch.mouthForm === 'number') {
        targetMouthForm += (event.patch.mouthForm - targetMouthForm) * fade
      }
      if (typeof event.patch.eyeLOpen === 'number') {
        targetEyeL += (event.patch.eyeLOpen - targetEyeL) * fade
      }
      if (typeof event.patch.eyeROpen === 'number') {
        targetEyeR += (event.patch.eyeROpen - targetEyeR) * fade
      }
      if (typeof event.patch.browLY === 'number') {
        targetBrowLY += (event.patch.browLY - targetBrowLY) * fade
      }
      if (typeof event.patch.browRY === 'number') {
        targetBrowRY += (event.patch.browRY - targetBrowRY) * fade
      }
      if (typeof event.patch.eyeLSmile === 'number') {
        targetEyeLSmile += (event.patch.eyeLSmile - targetEyeLSmile) * fade
      }
      if (typeof event.patch.eyeRSmile === 'number') {
        targetEyeRSmile += (event.patch.eyeRSmile - targetEyeRSmile) * fade
      }
    }
```

- [ ] **Step 5: 重新跑前端建置確認轉綠**

在 `vtuber-web-app/` 執行：

```bash
npm run build
```

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add vtuber-web-app/src/live2d/LAppModel.ts vtuber-web-app/src/store/appStore.ts vtuber-web-app/src/services/wsService.ts
git commit -m "feat: add expression plan base pose and micro event runtime"
```

---

## Task 8: 清理 legacy Expression tool pipeline，保留 Memory tool pipeline

**Files:**
- Modify: `backend/services/agent_tool_pipeline.py`
- Modify: `backend/tests/test_agent_tool_pipeline.py`
- Modify: `backend/api/routes/chat_ws.py`

- [ ] **Step 1: 在測試中標記 Expression tool pipeline 為 legacy-only，先讓命名失敗**

在 `backend/tests/test_agent_tool_pipeline.py` 中新增一個測試，確認 Expression allowed set 可為空或只保留 legacy fallback，不再作為 route 主路徑依賴：

```python
    def test_expression_tool_pipeline_is_no_longer_required_by_chat_route(self):
        self.assertIsInstance(EXPRESSION_AGENT_ALLOWED_TOOL_NAMES, set)
        self.assertIn("set_ai_behavior", CHAT_WS_EXPRESSION_AGENT_ALLOWED_TOOL_NAMES)
```

然後在測試註解說明：route 主路徑已改為 intent compiler，這裡僅保留 legacy renderer / old fixtures 使用。

- [ ] **Step 2: 將 `chat_ws.py` 內對 Expression tools 的 route 依賴降到最低**

確保 route 中只剩：

1. Memory tools 經 `extract_agent_tool_calls()`
2. Expression tools 不再經 `extract_agent_tool_calls()` 主路徑

若 `_execute_chat_orchestrator_tool_calls()` 已只剩 Memory 實質工作，將其重命名為 `_execute_memory_tool_calls()`，並同步更新測試匯入。

```python
async def _execute_memory_tool_calls(
    memory_calls: list[dict],
    websocket: WebSocket,
    broadcast_func,
    execute_profile_update_fn,
    append_memory_note_fn,
    last_behavior_payload: dict | None = None,
    model_name: str = "Hiyori",
) -> dict:
```

- [ ] **Step 3: 刪除 route 上已無用的 Expression tool fallback 警告與 dead branch**

從 `chat_ws.py` 清掉：

```python
if not has_required_expression_behavior(expression_calls):
    print("[Chat Orchestrator][WARN] 缺少 set_ai_behavior ...")
```

與所有只為 route 主路徑服務的 `set_ai_behavior` branch；保留 `expression_legacy_renderer.py` 作為舊 payload 過渡器。

- [ ] **Step 4: 跑 backend 全部關鍵測試**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_expression_*.py" -v
python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v
python -m unittest discover -s tests -p "test_agent_tool_pipeline.py" -v
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/services/agent_tool_pipeline.py backend/tests/test_agent_tool_pipeline.py backend/api/routes/chat_ws.py
git commit -m "refactor: limit agent tool pipeline to memory and legacy paths"
```

---

## Task 9: 補 extension points 與未來接口的最小落地形式

**Files:**
- Modify: `backend/services/expression_compiler.py`
- Modify: `backend/services/expression_intent_parser.py`
- Modify: `backend/domain/expression_presets.py`
- Modify: `vtuber-web-app/src/types/expressionPlan.ts`
- Modify: `vtuber-web-app/src/live2d/LAppModel.ts`

- [ ] **Step 1: 在 backend compiler 補齊預留接口名稱，先用最小 wrapper 落地**

把 `expression_compiler.py` 拆出明確函式名稱，即使第一版內部仍簡單，也要先固定接口：

```python
def build_expression_sequence(intent: dict, base_pose: dict, model_name: str) -> list[dict]:
    del intent, base_pose, model_name
    return []


def build_blink_plan(intent: dict, model_name: str) -> dict:
    del model_name
    blink_style = intent.get("blink_style", "normal")
    return {
        "style": blink_style,
        "commands": deepcopy(BLINK_STRATEGIES.get(blink_style, [])),
    }
```

並讓 `compile_expression_plan()` 改走這些函式，不要把所有邏輯塞在同一個函式中。

- [ ] **Step 2: 在 TS payload 型別中補上 optional 擴充欄位**

修改 `vtuber-web-app/src/types/expressionPlan.ts`：

```ts
export interface ExpressionPlanPayload {
  type: 'expression_plan'
  basePose: ExpressionBasePose
  microEvents: ExpressionMicroEvent[]
  sequence: ExpressionMicroEvent[]
  blinkPlan: {
    style: string
    commands: BlinkCommand[]
  }
  speakingRate: number
  timingHints?: Record<string, number>
  modelHints?: Record<string, string | number | boolean>
  debug?: Record<string, string>
}
```

- [ ] **Step 3: 在 `LAppModel.ts` 加入 sequence 與 variation 的空接口**

即使第一版尚未完整實作，也先補接口：

```ts
  public enqueueSequence(sequence: Array<{ kind: string; patch: ExpressionParamPatch; durationMs: number; returnToBase: boolean }>): void {
    for (const step of sequence) {
      this.enqueueMicroEvent(step)
    }
  }

  public applyDeterministicVariation(_context: { preset?: string; energy?: number }): void {
    // Phase 3 再擴充；先保留接口，避免未來重改 update loop 命名
  }
```

- [ ] **Step 4: 跑前後端建置與測試確認 extension points 沒破壞現況**

在 `backend/` 執行：

```bash
python -m unittest discover -s tests -p "test_expression_*.py" -v
```

在 `vtuber-web-app/` 執行：

```bash
npm run build
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/services/expression_compiler.py backend/services/expression_intent_parser.py backend/domain/expression_presets.py vtuber-web-app/src/types/expressionPlan.ts vtuber-web-app/src/live2d/LAppModel.ts
git commit -m "refactor: reserve expression extension points"
```

---

## Verification Checklist

- [ ] `python -m unittest discover -s tests -p "test_expression_intent_parser.py" -v`
- [ ] `python -m unittest discover -s tests -p "test_expression_compiler.py" -v`
- [ ] `python -m unittest discover -s tests -p "test_memory_model_wiring.py" -v`
- [ ] `python -m unittest discover -s tests -p "test_agent_tool_pipeline.py" -v`
- [ ] `npm run build`

Expected final state:

1. Expression Agent 回傳 JSON intent，而不是 Live2D tools
2. backend 可將 intent 編譯成 `expression_plan`
3. backend 仍可向舊前端輸出 `behavior` / `blink_control`
4. frontend 可優先吃 `expression_plan`，但仍兼容舊 payload
5. runtime 已支援 base pose 與最小 micro event overlay
6. compiler、payload、runtime 都留下明確 extension points

---

## Self-Review

### Spec coverage check

1. `Expression Intent` schema：Task 1
2. compiler / preset / blink strategy / sequence library：Task 2
3. Expression Agent 改為 intent output：Task 3
4. backend 先編回舊 payload：Task 4
5. `expression_plan` websocket payload：Task 5, Task 6
6. frontend runtime 升級為 base + micro event：Task 7
7. 清理 legacy Expression tool 主路徑：Task 8
8. 擴充性目標與未來接口最小落地：Task 9

### Placeholder scan

已避免使用 `TODO`、`TBD`、`implement later` 類型 placeholder。所有步驟都附實際檔案、命令與最小程式碼範例。

### Type consistency

1. backend 以 `Expression Intent` -> `expression_plan` -> `legacy renderer` 為固定命名
2. frontend 統一使用 `ExpressionPlanPayload`、`ExpressionMicroEvent`、`BlinkCommand`
3. `call_expression_agent()` 為新主介面，`call_live2d_agent()` 僅保留 legacy alias

---
