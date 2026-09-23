"""JEV Emotion / Action questions 與 Action answers → expression intent。"""
import math

from domain.emotion_state import EMOTION_FIELDS, PERSONALITY
from domain.expression_intent_schema import (
    ALLOWED_ARCS,
    ALLOWED_EMOTIONS,
    ALLOWED_PERFORMANCE_MODES,
)

CONFIDENCE_THRESHOLD = 0.5
NOUL_GOOFY_THRESHOLD = 0.7
NOUL_BLINK_THRESHOLD = 0.6

_EMOTION_DESC = {
    "neutral": "平靜、中性、沒有明顯情緒波動",
    "happy": "開心、愉悅、滿足",
    "playful": "調皮、玩鬧、想逗對方",
    "teasing": "挑釁、壞笑、得理不饒人",
    "angry": "生氣、憤怒、不滿",
    "sad": "悲傷、難過、受傷",
    "gloomy": "陰沉、低落、提不起勁",
    "shy": "害羞、內斂、不好意思",
    "surprised": "驚訝、震驚、措手不及",
    "conflicted": "矛盾、拉扯、左右為難",
}

_MODE_DESC = {
    "smile": "自然微笑，一般對話的穩定表情",
    "bright_talk": "活潑日常說話感，表情生動",
    "goofy_face": "做鬼臉、搞怪，臉明顯歪掉",
    "cheeky_wink": "單眼眨眼壞笑",
    "smug": "得意、欠揍的炫耀臉",
    "deadpan": "面無表情、平淡敷衍",
    "gloomy": "陰沉壓低、氣氛沉重",
    "volatile": "情緒不穩定、在波動",
    "meltdown": "表情崩壞、失控爆發",
    "awkward": "尷尬、彆扭、不知道怎麼反應",
    "tense_hold": "壓著情緒、忍住不發作",
    "shock_recoil": "嚇到後仰、被震懾",
}

_ARC_DESC = {
    "steady": "整段維持同一個表情",
    "pop_then_settle": "先爆開（驚/彈）再收斂回穩",
    "pause_then_smirk": "先停頓一下再露出壞笑",
    "widen_then_tease": "先瞪大再轉成調皮",
    "shrink_then_recover": "先退縮變小再慢慢恢復",
    "glare_then_flatten": "先瞪著對方再放平",
}

_INTENSITY_LEVELS = [
    "0 幾乎沒有表情，語氣平淡",
    "1 輕微，臉上有一點但不明顯",
    "2 中等，清楚的臉部變化",
    "3 強烈，很明顯的表情",
    "4 爆發，表情全開、誇張",
]

_ENERGY_LEVELS = [
    "0 低沉無力，快睡著",
    "1 偏低，安靜慢節奏",
    "2 中等，正常對話節奏",
    "3 偏高，活潑有精神",
    "4 高漲，興奮蹦跳",
]


def _assert_whitelist_sync() -> None:
    """確保描述表 keys 與 schema 白名單一致（import-time 檢查，避免漂移）。"""
    if set(_EMOTION_DESC) != ALLOWED_EMOTIONS:
        raise RuntimeError(
            f"EMOTION_CRITERIA 與 ALLOWED_EMOTIONS 不同步: "
            f"{set(_EMOTION_DESC) ^ ALLOWED_EMOTIONS}"
        )
    if set(_MODE_DESC) != ALLOWED_PERFORMANCE_MODES:
        raise RuntimeError(
            f"MODE_CRITERIA 與 ALLOWED_PERFORMANCE_MODES 不同步: "
            f"{set(_MODE_DESC) ^ ALLOWED_PERFORMANCE_MODES}"
        )
    if set(_ARC_DESC) != ALLOWED_ARCS:
        raise RuntimeError(
            f"ARC_CRITERIA 與 ALLOWED_ARCS 不同步: {set(_ARC_DESC) ^ ALLOWED_ARCS}"
        )


_assert_whitelist_sync()

EMOTION_CRITERIA = dict(_EMOTION_DESC)
MODE_CRITERIA = dict(_MODE_DESC)
ARC_CRITERIA = dict(_ARC_DESC)
INTENSITY_LEVELS = list(_INTENSITY_LEVELS)
ENERGY_LEVELS = list(_ENERGY_LEVELS)


_EMOTION_QUESTIONS = {
    "shy": "露西亞此刻是否明顯害羞或不好意思？",
    "pleased": "露西亞是否因目前互動感到開心、滿足或被取悅？",
    "genuinely_angry": "露西亞是否真的生氣，而非嘴硬、吐槽或假裝不耐煩？",
    "sad_or_hurt": "露西亞是否難過、失落、受傷或感到被冷落？",
    "masking_positive_feeling": "露西亞是否正在掩飾喜歡、開心或親近等正面感受？",
    "wants_continue_interaction": "露西亞是否希望目前話題或親密互動繼續？",
}


def build_emotion_questions() -> dict:
    assert set(_EMOTION_QUESTIONS) == set(EMOTION_FIELDS)
    return {
        field: {
            "type": "noul",
            "instructions": question + "結合 personality、recent_dialogue、previous_emotion_state 與 current_user_input 獨立判斷；不輸出理由。",
            "criteria": {"true": "符合", "false": "不符合"},
        }
        for field, question in _EMOTION_QUESTIONS.items()
    }


def build_emotion_context(
    user_message: str,
    chat_history: list[dict],
    previous_emotion_state: dict | None,
    relevant_memory: str = "",
) -> dict:
    """只傳最近 8 輪已完成的真實對話，不含本輪 user 訊息。"""
    dialogue = [
        {"role": msg["role"], "text": msg["content"][:500]}
        for msg in chat_history
        if isinstance(msg, dict)
        and msg.get("role") in ("user", "assistant")
        and isinstance(msg.get("content"), str)
    ][-16:]
    context = {
        "personality": PERSONALITY,
        "recent_dialogue": dialogue,
        "current_user_input": user_message[:4000],
    }
    if previous_emotion_state is not None:
        context["previous_emotion_state"] = previous_emotion_state
    if relevant_memory:
        context["relevant_memory"] = relevant_memory[:800]
    return context


def build_action_questions() -> dict:
    """Action 單獨決定表情演出，不輸出 Emotion State。"""
    return {
        "emotion": {
            "type": "choice",
            "instructions": (
                "將 `current_emotion_state` 映射成 Live2D compiler 的主表情 emotion label。"
                "`current_user_input` 僅用來判斷是否為明確的表演請求；不要重新判斷角色內在情緒。"
            ),
            "criteria": dict(EMOTION_CRITERIA),
        },
        "secondary_emotion": {
            "type": "choice",
            "instructions": (
                "除了主要情緒外，`current_emotion_state` 與 `recent_dialogue` "
                "是否還帶有第二層情緒？若沒有選 none。"
            ),
            "criteria": {"none": "沒有明顯的第二層情緒", **EMOTION_CRITERIA},
        },
        "performance_mode": {
            "type": "choice",
            "instructions": (
                "考量 `personality` 的固定角色設定與 `recent_dialogue` 的氣氛："
                "角色應該用哪種表演方式呈現這個情緒？"
                "注意 `previous_expression_carry_state` 若顯示上一輪已經做過某種表演，本輪傾向換一種。"
            ),
            "criteria": dict(MODE_CRITERIA),
        },
        "arc": {
            "type": "choice",
            "instructions": (
                "根據 `current_user_input` 語氣的轉折程度：這個表情在這一輪應該怎麼變化？"
                "語氣單純就 steady，有戲劇轉折就選對應弧線。"
            ),
            "criteria": dict(ARC_CRITERIA),
        },
        "intensity": {
            "type": "score",
            "instructions": (
                "角色此刻情緒表達的強度應該多強？"
                "參考 `current_emotion_state` 與 `current_user_input` 的語氣力度。"
            ),
            "criteria": list(INTENSITY_LEVELS),
        },
        "energy": {
            "type": "score",
            "instructions": (
                "角色此刻的整體精神能量應該多高？"
                "參考 `current_user_input` 的節奏與 `personality` 的人格傾向。"
            ),
            "criteria": list(ENERGY_LEVELS),
        },
        "wants_goofy": {
            "type": "noul",
            "instructions": (
                "使用者是否在明確要求角色做鬼臉、裝傻、搞笑反應？"
                "（如訊息中出現搞笑指令或明顯玩鬧邀請）"
            ),
            "criteria": {
                "true": "訊息中有明確的搞笑/鬼臉要求或明顯玩鬧邀請",
                "false": "一般對話或非搞笑訴求",
            },
        },
        "needs_special_blink": {
            "type": "noul",
            "instructions": (
                "這一輪是否需要特殊的眨眼表演（凝視不眨眼、害羞狂眨、驚訝後瞬眼等）？"
                "一般聊天回答不需要。"
            ),
            "criteria": {
                "true": "情境明確需要特殊眨眼表演",
                "false": "一般眨眼即可",
            },
        },
    }


def build_action_context(
    emotion_context: dict,
    current_emotion_state: dict,
    previous_expression_carry_state: dict | None,
) -> dict:
    state = {
        "personality": emotion_context["personality"],
        "recent_dialogue": emotion_context["recent_dialogue"],
        "current_user_input": emotion_context["current_user_input"],
        "current_emotion_state": current_emotion_state,
    }
    if previous_expression_carry_state is not None:
        state["previous_expression_carry_state"] = previous_expression_carry_state
    if emotion_context.get("relevant_memory"):
        state["relevant_memory"] = emotion_context["relevant_memory"]
    if emotion_context.get("current_action"):
        state["current_action"] = emotion_context["current_action"]
    return state


def map_answers_to_intent(answers: dict) -> dict:
    """Jev answers → raw intent dict。

    低 confidence 欄位直接不填，交給 normalize_expression_intent()
    用 DEFAULT_INTENT 兜底；主表情無效時由呼叫端回退上一輪表情。
    """
    intent: dict = {}

    emotion = answers.get("emotion") or {}
    if emotion.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        intent["emotion"] = emotion.get("choice")

    secondary = answers.get("secondary_emotion") or {}
    if secondary.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        choice = secondary.get("choice")
        if choice is not None:
            intent["secondary_emotion"] = "" if choice == "none" else choice

    mode = answers.get("performance_mode") or {}
    if mode.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        intent["performance_mode"] = mode.get("choice")

    arc = answers.get("arc") or {}
    if arc.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        intent["arc"] = arc.get("choice")

    # Score 0–4 → 0.0–1.0（score 可落在兩級之間，如 2.4 → 0.6）
    intensity = answers.get("intensity") or {}
    if intensity.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        score = intensity.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            intent["intensity"] = round(max(0.0, min(1.0, float(score) / 4.0)), 3)

    energy = answers.get("energy") or {}
    if energy.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD:
        score = energy.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
            intent["energy"] = round(max(0.0, min(1.0, float(score) / 4.0)), 3)

    # Noul 觸發器：超過閾值才影響 intent（Noul 無 confidence 欄位）
    goofy = answers.get("wants_goofy") or {}
    if float(goofy.get("noul", 0.0)) > NOUL_GOOFY_THRESHOLD:
        intent["must_include"] = ["goofy_eye_cross_bias"]
        # performance_mode 強制候選交給規則層決定，避免覆蓋高信心 Choice

    blink = answers.get("needs_special_blink") or {}
    if float(blink.get("noul", 0.0)) > NOUL_BLINK_THRESHOLD:
        intent["blink_style"] = _map_special_blink(intent.get("emotion"))

    return intent


def _map_special_blink(emotion: str | None) -> str:
    """特殊眨眼需求 → blink_style 情緒映射（blink_control_hints 縮編版）。"""
    mapping = {
        "shy": "shy_fast",
        "teasing": "teasing_pause",
        "surprised": "surprised_hold",
        "gloomy": "sleepy_slow",
        "sad": "sleepy_slow",
        "conflicted": "focused_pause",
    }
    if emotion in mapping:
        return mapping[emotion]
    return "focused_pause"
