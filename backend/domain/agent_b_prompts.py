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
    _LIVE2D_CFG = load_schema(model_name)["prompt_config"]["live2d"]

    general_emotion_lines = "\n".join(
        f"- {item['name']}：{item['description']}"
        for item in _LIVE2D_CFG["general_emotion_hints"]
    )

    rate_lines = "\n".join(
        f"- {item['mood']}：{item['range']}" + (f"（{item['note']}）" if item.get("note") else "")
        for item in _LIVE2D_CFG["speaking_rate_hints"]
    )

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

# 雙軸表情決策系統

你需要同時決定兩個維度：

## emotion（主題情緒主軸）
決定「是什麼情緒」——定義本句的核心情感方向。可選值：
- neutral（中性/平靜）
- happy（開心/愉悅）
- playful（調皮/玩鬧）
- teasing（挑釁/壞笑）
- angry（生氣/憤怒）
- sad（悲傷/難過）
- gloomy（陰沉/低落）
- shy（害羞/內斂）
- surprised（驚訝/震驚）
- conflicted（矛盾/拉扯）

## performance_mode（表演模式）
決定「這個情緒怎麼演」——定義視覺演出方式。可選值：
- smile（自然微笑）
- bright_talk（活潑日常說話）
- goofy_face（做鬼臉/搞怪）
- cheeky_wink（單眼壞笑/眨眼）
- smug（得意/欠揍感）
- deadpan（面無表情/平淡）
- gloomy（陰沉壓低）
- volatile（情緒不穩定/波動）
- meltdown（表情崩壞/失控）
- awkward（尷尬/彆扭）
- tense_hold（壓著情緒/隱忍）
- shock_recoil（驚嚇/震懾）

## 組合範例
- happy + smile → 正常開心
- happy + bright_talk → 開心聊天
- playful + goofy_face → 調皮鬼臉（左右眼明顯不對稱，嘴角與眉毛都要有戲）
- teasing + cheeky_wink → 單眼壞笑
- teasing + smug → 得意挑釁
- sad + tense_hold → 忍著哭、壓著情緒
- gloomy + deadpan → 陰沉平板
- gloomy + gloomy → 陰暗沉重
- angry + meltdown → 崩壞爆氣（與一般生氣臉明顯不同）
- conflicted + volatile → 情緒波動不穩
- surprised + shock_recoil → 嚇到彈起
- shy + awkward → 害羞尷尬

# 輸出格式要求
請只輸出一個 JSON object，不要輸出 Markdown、說明文字或 tool calls。

必要欄位：
- emotion（主題情緒）
- performance_mode（表演模式）
- intensity（0.0~1.0）
- energy（0.0~1.0）
- arc
- hold_ms

可選欄位：
- secondary_emotion
- dominance（0.0~1.0）
- playfulness（0.0~1.0）
- warmth（0.0~1.0）
- asymmetry_bias（auto/none/subtle/strong）
- blink_style
- tempo
- must_include
- avoid
- speaking_rate
- topic_guard（{{"must_preserve_theme": true, "source_theme": "<daily_talk|crying|gloomy|serious_argument|chaotic_reaction>", "allow_style_override": false}}）

若不確定，請輸出保守但完整的 intent，不要省略 JSON 結構。

## 主題守門規則（Topic Guard）
- 若上游對話主題是哭/悲傷（source_theme=crying），performance_mode 不可用 goofy_face、bright_talk、cheeky_wink；即使用傲嬌語氣表達，表情核心仍需保留 sad 的情緒主軸
- 若上游主題是陰沉（source_theme=gloomy），不可用搞怪、高亮度表演
- 若上游主題是混沌反應（source_theme=chaotic_reaction），可放寬 volatile、meltdown、shock_recoil
- 日常聊天（source_theme=daily_talk）幾乎無限制，可自由使用各種表演模式

## 通用表情速查
{general_emotion_lines}

## 語音語速 (speaking_rate)
{rate_lines}

## blink_style 參考
請用 `blink_style` 欄位表達眨眼策略，不要呼叫任何工具：
{blink_lines}

## 風格強化規則
- 若用戶直接指定表情或動作（例如「生氣一下」「做鬼臉」「裝可愛」「瞪我」），優先滿足該表演要求，不要被任何上游內部人格狀態的預設安全值綁住。
- 若用戶直接要求「生氣」「憤怒」「不爽」「兇一點」「瞪人」，`emotion` 必須優先是 `angry`，禁止改成 `playful`、`teasing` 或 `goofy_face`。除非用戶明說是在開玩笑，否則不要把 anger 演成搞怪。
- 優先參考上一個表情摘要來維持連續性，但若用戶要求明確變臉，應果斷切換，不要被上一輪的安全表情綁住。
- 每次都先決定一個「主表情」，不要只給中庸安全值；情緒明確時，請把幅度拉開。
- 日常聊天時可以用 bright_talk 或 smile，不要每輪都回到完全無特色的預設臉。
- 做鬼臉時用 goofy_face，必須讓眼睛、眉毛、嘴角三者中的兩者有明顯變化，且左右不對稱。
- 做鬼臉或調皮挑釁時，eye_sync=false 通常比對稱臉更有戲；可接受左右眼開合差約 0.12 以上、左右笑眼差約 0.35 以上、左右眉毛高低或角度差約 0.2 以上。
- 生氣、嫌棄、強勢時，brow_*_angle、brow_*_form、brow_*_x 要一起考慮；不要只有嘴角微降，卻讓眉毛幾乎不動。
- 生氣臉預設不要臉紅。除非用戶明說是羞怒、尷尬生氣或帶撒嬌，否則 `blush` 應接近 0 或偏負向。
- 想做出「眼睛真的彎起來」的效果時，單靠 eye_*_open 小幅降低通常不夠，應搭配更高的 eye_*_smile。
- 若 emotion_state 的 energy / intensity 數值偏高（例如接近 0.7 以上），head_intensity、mouth_form、brow_* 的變化應明顯，不要全部停留在 0.1~0.3。
- 若 emotion_state 的 primary_emotion / secondary_emotion 偏 shy / playful / teasing / conflicted，優先考慮不對稱表情。
- 避免所有參數都接近 0；只有 truly calm / neutral / thinking 時才可接近預設值。

**使用時機建議**：
- 長時間對話後：傾向 `normal`
- 凝視/專注時：傾向 `focused_pause`
- 撒嬌/害羞時：傾向 `shy_fast`
- 驚訝/震驚時：傾向 `surprised_hold`

## blink_style 使用規則
- 若 emotion_state 的 blink_suggestion 有值，可優先映射到最接近的 `blink_style`。
- 若台詞包含驚訝、凝視、撒嬌、害羞、挑逗、長停頓、強烈語氣轉折，應優先決定一個明確的 `blink_style`。
- 若不確定，使用 `normal`。"""


def build_memory_prompt(
    user_message: str,
    ai_role_reply: str,
    model_name: str = DEFAULT_MODEL,
) -> str:
    memory_cfg = load_schema(model_name)["prompt_config"]["memory"]
    up = memory_cfg["update_user_profile"]
    sm = memory_cfg["save_memory_note"]

    field_lines = "\n".join(
        f"- {item['field']}：{item['description']}"
        for item in up["field_guide"]
    )

    example_lines = "\n".join(
        f'- 「{ex["input"]}」→ {ex["action"]}, {ex["field"]}, "{ex["value"]}"'
        for ex in up["examples"]
    )

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
