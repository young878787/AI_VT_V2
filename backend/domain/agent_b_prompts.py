"""
Expression Agent / Memory Agent 系統 Prompt 組裝。
  - Expression Agent（舊稱 Agent B-1 / Live2D Agent）：表情控制
  - Memory Agent（舊稱 Agent B-2）：記憶管理
保留舊命名對照，方便追蹤既有註解、設定與討論脈絡。
資料來源：tools/{model_name}.json（每個模型獨立設定）。
"""

from domain.tools.schema_loader import load_schema, DEFAULT_MODEL


def _format_emotion_state(emotion_state: dict | None) -> str:
    if not emotion_state:
        return "- 無額外 emotion_state，請僅根據用戶要求、台詞語氣與上一輪表情摘要判斷。"

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


def _format_previous_expression_state(previous_expression_state: dict | None) -> str:
    if not previous_expression_state:
        return "- 無上一輪表情資料，請僅根據用戶要求、台詞與 emotion_state 判斷。"

    lines: list[str] = []
    summary = previous_expression_state.get("summary")
    if summary:
        lines.append(f"- summary: {summary}")

    ordered_keys = [
        "mouth_form",
        "eye_sync",
        "eye_l_open",
        "eye_r_open",
        "eye_l_smile",
        "eye_r_smile",
        "brow_l_y",
        "brow_r_y",
        "brow_l_angle",
        "brow_r_angle",
        "brow_l_form",
        "brow_r_form",
        "brow_l_x",
        "brow_r_x",
    ]
    for key in ordered_keys:
        if key in previous_expression_state and previous_expression_state[key] not in (None, ""):
            lines.append(f"- {key}: {previous_expression_state[key]}")

    return "\n".join(lines) if lines else "- 上一輪表情資料為空。"


def build_live2d_prompt(
    user_message: str,
    ai_role_reply: str,
    previous_expression_state: dict | None,
    emotion_state: dict | None,
    model_name: str = DEFAULT_MODEL,
) -> str:
    """
    組裝 Expression Agent 的系統 Prompt（原 Agent B-1 / Live2D Agent）。
    專注表情控制，不處理記憶。
    依 model_name 載入對應的 prompt_config。
    """
    _LIVE2D_CFG = load_schema(model_name)["prompt_config"]["live2d"]

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
    previous_expression_lines = _format_previous_expression_state(previous_expression_state)

    return f"""你是 {_LIVE2D_CFG['system_role']}。
{_LIVE2D_CFG['task_description']}

# 當前上下文

【用戶的直接表情要求】
{user_message}

【AI 角色的回覆】
{ai_role_reply}

【emotion_state】
{emotion_state_lines}

【上一個表情摘要】
{previous_expression_lines}

# {_LIVE2D_CFG['tool_name'] if 'tool_name' in _LIVE2D_CFG else 'set_ai_behavior'} — 【必須呼叫】
{_LIVE2D_CFG['tool_description']}

請優先根據用戶的直接表情要求、AI 角色回覆的語氣、emotion_state 與上一個表情摘要決定本輪表情，不要依賴任何上游內部人格狀態欄位。

## 通用表情速查
{general_emotion_lines}

## 語音語速 (speaking_rate)
{rate_lines}

## 眨眼控制 (blink_control) — 選用
你可以使用 blink_control 工具來控制眨眼，讓角色更自然：
{blink_lines}

## 風格強化規則
- 若用戶直接指定表情或動作（例如「生氣一下」「做鬼臉」「裝可愛」「瞪我」），優先滿足該表演要求，不要被任何上游內部人格狀態的預設安全值綁住。
- 優先參考上一個表情摘要來維持連續性，但若用戶要求明確變臉，應果斷切換，不要被上一輪的安全表情綁住。
- 每次都先決定一個「主表情」，不要只給中庸安全值；情緒明確時，請把幅度拉開。
- 日常聊天時表情可以自然，但不要每輪都回到完全無特色的預設臉；可以保留輕微笑意、輕微眼神變化或淡淡的眉毛起伏。
- 做鬼臉、生氣、驚訝、強烈吐槽時，不要只調 0.05~0.15 這種幾乎看不出的安全值；至少讓眼睛、眉毛、嘴角三者中的兩者有明顯變化。
- 優先利用 mouth_form、eye_*_open、eye_*_smile、brow_*_angle、brow_*_form、brow_*_x 組出有辨識度的臉。
    - 若 emotion_state 的 energy / intensity 數值偏高（例如接近 0.7 以上），head_intensity、mouth_form、brow_* 的變化應明顯，不要全部停留在 0.1~0.3。
    - 若 emotion_state 的 primary_emotion / secondary_emotion 偏 shy / playful / teasing / conflicted，或 asymmetry_bias 是 slight / strong，優先考慮 eye_sync=false，做輕微不對稱表情，例如單邊笑眼、單邊眉毛上挑、左右眼張開程度不同。
    - 若 emotion_state 提供 expression_arc，請用單一組參數表現「這句台詞的最終停留表情」，不要平均攤平成無特色中間值。
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
    ai_role_reply: str,
    model_name: str = DEFAULT_MODEL,
) -> str:
    """
    組裝 Memory Agent 的系統 Prompt（原 Agent B-2）。
    專注記憶管理，不處理表情。
    """
    memory_cfg = load_schema(model_name)["prompt_config"]["memory"]
    up = memory_cfg["update_user_profile"]
    sm = memory_cfg["save_memory_note"]

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
        for p in memory_cfg["principles"]
    )

    return f"""你是{memory_cfg['system_role']}。
{memory_cfg['task_description']}

# 當前對話

【用戶的訊息】
{user_message}

【AI 角色的回覆】（僅供參考上下文，記憶判斷以用戶訊息為主）
{ai_role_reply}

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
