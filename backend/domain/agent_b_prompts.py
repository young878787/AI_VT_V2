"""
Agent B 系統 Prompt 組裝：拆分為兩個獨立 Agent。
  - B-1 Live2D Agent：表情控制（必定執行）
  - B-2 Memory Agent：記憶管理（判斷是否需要記憶操作）
資料來源：tools/{model_name}.json（每個模型獨立設定）。
"""

from domain.tools.schema_loader import load_schema, DEFAULT_MODEL

_MEMORY_CFG = load_schema(DEFAULT_MODEL)["prompt_config"]["memory"]


def _format_emotion_state(emotion_state: dict | None) -> str:
    if not emotion_state:
        return "- 無額外 emotion_state，請僅根據台詞語氣與 JPAF 狀態判斷。"

    ordered_keys = [
        "primary_emotion",
        "secondary_emotion",
        "energy",
        "intensity",
        "pace",
        "blink_suggestion",
        "asymmetry_bias",
        "expression_arc",
    ]
    lines: list[str] = []
    for key in ordered_keys:
        if key in emotion_state and emotion_state[key] not in (None, ""):
            lines.append(f"- {key}: {emotion_state[key]}")
    return "\n".join(lines) if lines else "- emotion_state 為空，請根據台詞語氣判斷。"


def build_live2d_prompt(
    user_message: str,
    agent_a_reply: str,
    jpaf_state: dict | None,
    emotion_state: dict | None,
    model_name: str = DEFAULT_MODEL,
) -> str:
    """
    組裝 Live2D Agent 的系統 Prompt（Agent B-1）。
    專注表情控制，不處理記憶。
    依 model_name 載入對應的 prompt_config。
    """
    _LIVE2D_CFG = load_schema(model_name)["prompt_config"]["live2d"]
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
    general_emotion_lines = "\n".join(
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

    emotion_state_lines = _format_emotion_state(emotion_state)

    return f"""你是 {_LIVE2D_CFG['system_role']}。
{_LIVE2D_CFG['task_description']}

# 當前上下文

【用戶的直接表情要求】
{user_message}

【AI 角色的回覆】
{agent_a_reply}

【情緒狀態】
- active_function: {active_fn}
- persona: {persona}

【emotion_state】
{emotion_state_lines}

# {_LIVE2D_CFG['tool_name'] if 'tool_name' in _LIVE2D_CFG else 'set_ai_behavior'} — 【必須呼叫】
{_LIVE2D_CFG['tool_description']}

根據 persona 和 active_function 調整表情：

## persona 表情對應
{persona_lines}

## 通用表情速查
{general_emotion_lines}

## 語音語速 (speaking_rate)
{rate_lines}

## 眨眼控制 (blink_control) — 選用
你可以使用 blink_control 工具來控制眨眼，讓角色更自然：
{blink_lines}

## 風格強化規則
- 若用戶直接指定表情或動作（例如「生氣一下」「做鬼臉」「裝可愛」「瞪我」），優先滿足該表演要求，而不是只回到 persona 的安全基底。
- 每次都先決定一個「主表情」，不要只給中庸安全值；情緒明確時，請把幅度拉開。
- 日常聊天時表情可以自然，但不要每輪都回到完全無特色的預設臉；可以保留輕微笑意、輕微眼神變化或淡淡的眉毛起伏。
- 做鬼臉、生氣、驚訝、強烈吐槽時，不要只調 0.05~0.15 這種幾乎看不出的安全值；至少讓眼睛、眉毛、嘴角三者中的兩者有明顯變化。
- 優先利用 mouth_form、eye_*_open、eye_*_smile、brow_*_angle、brow_*_form、brow_*_x 組出有辨識度的臉。
- 若 emotion_state 顯示 high energy / high intensity，head_intensity、mouth_form、brow_* 的變化應明顯，不要全部停留在 0.1~0.3。
- 若 emotion_state 顯示 shy / playful / teasing / conflicted，優先考慮 eye_sync=false，做輕微不對稱表情，例如單邊笑眼、單邊眉毛上挑、左右眼張開程度不同。
- 若 emotion_state 顯示 expression_arc，請用單一組參數表現「這句台詞的最終停留表情」，不要平均攤平成無特色中間值。
- 避免所有參數都接近 0；只有 truly calm / neutral / thinking 時才可接近預設值。
- 若模型視覺變化偏小，優先把 eye_*_open、eye_*_smile、brow_*、mouth_form 拉開，而不是只增加 head_intensity 或 blush_level。
- 鬼臉或調皮挑釁時，eye_sync=false 通常比對稱臉更有戲；可接受左右眼開合差約 0.12 以上、左右笑眼差約 0.35 以上、左右眉毛高低或角度差約 0.2 以上。
- 生氣、嫌棄、強勢時，brow_*_angle、brow_*_form、brow_*_x 要一起考慮；不要只有嘴角微降，卻讓眉毛幾乎不動。
- 想做出「眼睛真的彎起來」的效果時，單靠 eye_*_open 小幅降低通常不夠，應搭配更高的 eye_*_smile。

**使用時機建議**：
- 長時間對話後：呼叫 force_blink 模擬自然眨眼
- 凝視/專注時：呼叫 pause 暫停眨眼 2-3 秒
- 撒嬌/害羞時：可以加快眨眼頻率 (set_interval min=0.8, max=1.5)
- 驚訝/震驚時：可以先 pause 暫停眨眼，再 force_blink

## blink_control 積極使用規則
- 若 emotion_state 的 blink_suggestion 有值，優先依該建議呼叫 blink_control。
- 若台詞包含驚訝、凝視、撒嬌、害羞、挑逗、長停頓、強烈語氣轉折，應優先考慮額外呼叫一次 blink_control。
- 若使用 pause 造成戲劇性停頓，適合搭配 1 次 force_blink 或後續 resume，避免狀態卡住。
- 若整句台詞偏活潑連續、興奮或碎念，可考慮 set_interval 讓眨眼節奏更快。"""


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
