"""
Agent B 系統 Prompt 組裝：拆分為兩個獨立 Agent。
  - B-1 Live2D Agent：表情控制（必定執行）
  - B-2 Memory Agent：記憶管理（判斷是否需要記憶操作）
資料來源：tools_schema.json（單一來源）。
"""

import json
import os

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "tools_schema.json")
with open(_SCHEMA_PATH, "r", encoding="utf-8") as _f:
    _SCHEMA = json.load(_f)

_LIVE2D_CFG = _SCHEMA["prompt_config"]["live2d"]
_MEMORY_CFG = _SCHEMA["prompt_config"]["memory"]


def build_live2d_prompt(
    agent_a_reply: str,
    jpaf_state: dict | None,
) -> str:
    """
    組裝 Live2D Agent 的系統 Prompt（Agent B-1）。
    專注表情控制，不處理記憶。
    """
    if jpaf_state:
        active_fn = jpaf_state.get("active_function", "Ti")
        persona = jpaf_state.get("suggested_persona", "tsundere")
    else:
        active_fn = "Ti"
        persona = "tsundere"

    # persona 對應段落
    persona_lines = "\n".join(
        f"- **{key}（{val['label']}）**：{val['description']}"
        for key, val in _LIVE2D_CFG["persona_hints"].items()
    )

    # 通用表情速查
    emotion_lines = "\n".join(
        f"- {item['name']}：{item['description']}"
        for item in _LIVE2D_CFG["general_emotion_hints"]
    )

    # 語速提示
    rate_lines = "\n".join(
        f"- {item['mood']}：{item['range']}" + (f"（{item['note']}）" if item.get("note") else "")
        for item in _LIVE2D_CFG["speaking_rate_hints"]
    )

    # 眨眼控制提示
    blink_lines = "\n".join(
        f"- **{item['action']}**：{item['description']}（建議 {item.get('duration_hint', item.get('interval_hint', ''))}）"
        for item in _LIVE2D_CFG.get("blink_control_hints", [])
    )

    return f"""你是 {_LIVE2D_CFG['system_role']}。
{_LIVE2D_CFG['task_description']}

# 當前上下文

【AI 角色的回覆】
{agent_a_reply}

【情緒狀態】
- active_function: {active_fn}
- persona: {persona}

# {_LIVE2D_CFG['tool_name'] if 'tool_name' in _LIVE2D_CFG else 'set_ai_behavior'} — 【必須呼叫】
{_LIVE2D_CFG['tool_description']}

根據 persona 和 active_function 調整表情：

## persona 表情對應
{persona_lines}

## 通用表情速查
{emotion_lines}

## 語音語速 (speaking_rate)
{rate_lines}

## 眨眼控制 (blink_control) — 選用
你可以使用 blink_control 工具來控制眨眼，讓角色更自然：
{blink_lines}

**使用時機建議**：
- 長時間對話後：呼叫 force_blink 模擬自然眨眼
- 凝視/專注時：呼叫 pause 暫停眨眼 2-3 秒
- 撒嬌/害羞時：可以加快眨眼頻率 (set_interval min=0.8, max=1.5)
- 驚訝/震驚時：可以先 pause 暫停眨眼，再 force_blink"""


def build_memory_prompt(
    user_message: str,
    agent_a_reply: str,
) -> str:
    """
    組裝 Memory Agent 的系統 Prompt（Agent B-2）。
    專注記憶管理，不處理表情。
    """
    up = _MEMORY_CFG["update_user_profile"]
    sm = _MEMORY_CFG["save_memory_note"]

    # 欄位指南
    field_lines = "\n".join(
        f"- {item['field']}：{item['description']}"
        for item in up["field_guide"]
    )

    # 示例
    example_lines = "\n".join(
        f'- 「{ex["input"]}」→ {ex["action"]}, {ex["field"]}, "{ex["value"]}"'
        for ex in up["examples"]
    )

    # 原則
    principle_lines = "\n".join(
        f"- {p}"
        for p in _MEMORY_CFG["principles"]
    )

    return f"""你是{_MEMORY_CFG['system_role']}。
{_MEMORY_CFG['task_description']}

# 當前對話

【用戶的訊息】
{user_message}

【AI 角色的回覆】（僅供參考上下文，記憶判斷以用戶訊息為主）
{agent_a_reply}

# 工具使用規則

## update_user_profile — 積極使用
{up['description']}

欄位選擇指南：
{field_lines}

示例：
{example_lines}

## save_memory_note — 積極使用
{sm['description']}

記錄格式：{sm['format']}。

---
**重要原則**：
{principle_lines}"""
