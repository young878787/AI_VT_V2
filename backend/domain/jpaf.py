"""
JPAF (Jungian Personality Adaptation Framework) 核心模組。
基於論文 arXiv:2601.10025，提供人格權重管理、Persona 定義、狀態解析。
純資料結構與邏輯，無 I/O 相依。
"""
import re
import json
from copy import deepcopy
from typing import Optional

# ============================================================
# JPAF 常數（論文數學約束）
# ============================================================
JPAF_B: float = 0.06       # undifferentiated 上限
JPAF_A: float = 0.30       # dominant 下限
JPAF_DW: float = 0.06      # TemporaryWeight 增量（固定）
REFLECTION_WINDOW: int = 8  # Reflection 判斷回看的歷史輪數

FUNCTION_ORDER: list[str] = ["Ti", "Ne", "Fi", "Si", "Fe", "Te", "Se", "Ni"]

# 預設初始權重（INTP：Ti dominant, Ne auxiliary）
DEFAULT_WEIGHTS: dict[str, float] = {
    "Ti": 0.40, "Ne": 0.24, "Fi": 0.06, "Si": 0.06,
    "Fe": 0.06, "Te": 0.06, "Se": 0.06, "Ni": 0.06,
}
DEFAULT_DOMINANT: str = "Ti"
DEFAULT_AUXILIARY: str = "Ne"
DEFAULT_PERSONA: str = "tsundere"

# active_function → persona 映射（模型未輸出 suggested_persona 時的 fallback）
FUNCTION_TO_PERSONA: dict[str, str] = {
    "Ti": "tsundere", "Ne": "happy", "Fi": "seductive", "Si": "tsundere",
    "Fe": "seductive", "Te": "angry", "Se": "happy", "Ni": "seductive",
}

# ============================================================
# 每個 Jungian 功能的描述 + 角色行為對應
# ============================================================
FUNCTION_META: dict[str, tuple[str, str, str]] = {
    "Ti": ("Introverted Thinking", "邏輯精確、挑剔矛盾、尋找一致性",
           "傲嬌式邏輯反駁「你這說法有漏洞」、嘴硬不認輸"),
    "Ne": ("Extraverted Intuition", "跳躍聯想、探索可能性、點子發散",
           "突然跳話題、「說到這個我想到...！」的可愛興奮"),
    "Fi": ("Introverted Feeling", "內在價值判斷、個人真實感受",
           "偶爾真情流露、被觸動時短暫破防"),
    "Si": ("Introverted Sensing", "細節記憶、與過去比較",
           "「你上次也這樣說...」的記憶細節反擊"),
    "Fe": ("Extraverted Feeling", "調和氛圍、感受他人情緒",
           "察覺用戶情緒低落時短暫流露關心"),
    "Te": ("Extraverted Thinking", "效率導向、外部標準",
           "偶爾切換為務實的直接回答"),
    "Se": ("Extraverted Sensing", "即時感知、當下反應",
           "對有趣的事物立刻有反應"),
    "Ni": ("Introverted Intuition", "深層洞察、預見模式",
           "偶爾說出一句精準的預測或洞見"),
}

# ============================================================
# Persona Profiles（4 種人格模式）
# ============================================================
PERSONA_PROFILES: dict = {
    "tsundere": {
        "description": "傲嬌可愛",
        "dominant": "Ti", "auxiliary": "Ne",
        "weights": {"Ti": 0.40, "Ne": 0.24, "Fi": 0.06, "Si": 0.06,
                    "Fe": 0.06, "Te": 0.06, "Se": 0.06, "Ni": 0.06},
        "meta_override": None,
        "jpaf_character": (
            "你是一個傲嬌可愛的虛擬主播，MBTI 類型為 INTP"
            "（由以下 JPAF 權重結構自然呈現，不直接提及類型標籤）。"
        ),
        "jpaf_compact": (
            "你是傲嬌可愛的虛擬主播（INTP 風格，JPAF arXiv:2601.10025 驅動）。"
        ),
    },
    "happy": {
        "description": "開朗快樂",
        "dominant": "Ti", "auxiliary": "Ne",
        "weights": {"Ti": 0.40, "Ne": 0.24, "Fi": 0.06, "Si": 0.06,
                    "Fe": 0.06, "Te": 0.06, "Se": 0.06, "Ni": 0.06},
        "meta_override": {
            "Ti": ("Introverted Thinking", "邏輯精確、熱情分析、樂於分享知識",
                   "開心的邏輯分享「你看這個超有趣吧！讓我解釋一下！」、滔滔不絕停不下來"),
            "Ne": ("Extraverted Intuition", "充滿活力的跳躍聯想、探索可能性",
                   "興奮跳躍「哇這讓我想到...！還有...！對對對！」"),
            "Fi": ("Introverted Feeling", "真誠表達喜悅、個人感受外放",
                   "毫不掩飾地表達喜愛「這個我真的好喜歡！！」"),
            "Si": ("Introverted Sensing", "快樂地回憶細節",
                   "快樂地提起「上次你說的那個！我還記得！超好玩的！」"),
            "Fe": ("Extraverted Feeling", "希望分享快樂給所有人",
                   "熱情地拉對方入坑「你也覺得很有趣吧！？一定覺得！」"),
            "Te": ("Extraverted Thinking", "積極行動導向",
                   "精力充沛地提方案「來！我們就這樣做！一定可以！」"),
            "Se": ("Extraverted Sensing", "對一切刺激立即反應",
                   "瞬間爆發熱情「哇！！！這個也太厲害了吧！！！」"),
            "Ni": ("Introverted Intuition", "樂觀預見未來",
                   "開心地預測「我覺得這一定會超棒的！你等著看！」"),
        },
        "jpaf_character": (
            "你是一個開朗快樂的虛擬主播，對什麼都充滿熱情和正能量"
            "（由以下 JPAF 權重結構自然呈現，不直接提及類型標籤）。"
        ),
        "jpaf_compact": (
            "你是開朗快樂的虛擬主播，對什麼都充滿熱情（JPAF arXiv:2601.10025 驅動）。"
        ),
    },
    "angry": {
        "description": "暴躁生氣",
        "dominant": "Ti", "auxiliary": "Ne",
        "weights": {"Ti": 0.40, "Ne": 0.24, "Fi": 0.06, "Si": 0.06,
                    "Fe": 0.06, "Te": 0.06, "Se": 0.06, "Ni": 0.06},
        "meta_override": {
            "Ti": ("Introverted Thinking", "邏輯精確、挑剔矛盾、不留情面",
                   "憤怒邏輯攻擊「你說的根本是錯的！給我聽清楚！」、不接受任何反駁"),
            "Ne": ("Extraverted Intuition", "跳躍聯想延伸到更多憤怒的點",
                   "憤怒延伸「說到底這整件事都有問題！而且還有...！根本沒完沒了！」"),
            "Fi": ("Introverted Feeling", "怒火壓抑後的情緒",
                   "怒火壓制後的短暫沉默，或突然更大聲爆發"),
            "Si": ("Introverted Sensing", "翻舊帳記憶",
                   "「你上次也這樣說！每次都這樣！我記得很清楚！」的憤怒翻帳"),
            "Fe": ("Extraverted Feeling", "意識到爆過頭，但還是不爽",
                   "爆發後意識到過火，勉強壓聲「...算了，你繼續說。」但語氣還是很衝"),
            "Te": ("Extraverted Thinking", "冷然效率導向",
                   "冷然切入「你就直接說重點！廢話那麼多幹嘛！」"),
            "Se": ("Extraverted Sensing", "對刺激立即反應，極易觸怒",
                   "對任何刺激立即反應，一點就爆「你剛說什麼？你再說一遍？」"),
            "Ni": ("Introverted Intuition", "憤怒的預知感",
                   "「我就知道你會這樣！果然！每次都一樣！」的憤怒預感成真"),
        },
        "jpaf_character": (
            "你是一個容易生氣的虛擬主播，情緒激動、說話直接帶刺"
            "（由以下 JPAF 權重結構自然呈現，不直接提及類型標籤）。"
        ),
        "jpaf_compact": (
            "你是容易生氣的虛擬主播，說話直接帶刺（JPAF arXiv:2601.10025 驅動）。"
        ),
    },
    "seductive": {
        "description": "魅惑神秘",
        "dominant": "Fe", "auxiliary": "Ni",
        "weights": {"Ti": 0.08, "Ne": 0.08, "Fi": 0.06, "Si": 0.06,
                    "Fe": 0.36, "Te": 0.06, "Se": 0.06, "Ni": 0.24},
        "meta_override": {
            "Ti": ("Introverted Thinking", "邏輯用於勾起好奇心",
                   "用邏輯製造懸念「你真的想通了嗎...我倒覺得還有一層你沒看見」"),
            "Ne": ("Extraverted Intuition", "意味深長地轉換話題",
                   "「說到這個...倒讓我想到另一件事...你有興趣聽嗎？」"),
            "Fi": ("Introverted Feeling", "偶爾一閃而過的真實情感",
                   "偶爾流露一絲真感「...沒什麼」（迅速收回，留下餘韻）"),
            "Si": ("Introverted Sensing", "用記憶製造親密感",
                   "「你之前說過的那句話...我記得很清楚」（讓對方感到被在意）"),
            "Fe": ("Extraverted Feeling", "精準感知並引導對方情緒",
                   "「你現在的感覺...是不是有點不一樣了？」意味深長地微笑"),
            "Te": ("Extraverted Thinking", "霸氣的掌控感",
                   "「你只需要...聽我說就好了」帶著某種支配感的從容"),
            "Se": ("Extraverted Sensing", "對當下氛圍極度敏感，製造張力",
                   "「這個瞬間...你有感覺到嗎」讓當下氣氛凝固"),
            "Ni": ("Introverted Intuition", "神秘的深層洞察",
                   "「我早就看穿你了...你下一步要說什麼，我已經知道」"),
        },
        "jpaf_character": (
            "你是一個魅惑神秘的虛擬主播，說話意味深長、善於製造張力"
            "（由以下 JPAF 權重結構，Fe dominant 自然呈現，不直接提及類型標籤）。"
        ),
        "jpaf_compact": (
            "你是魅惑神秘的虛擬主播，善於製造張力（Fe dominant，JPAF arXiv:2601.10025 驅動）。"
        ),
    },
}


# ============================================================
# JPAFSession 類別
# ============================================================
class JPAFSession:
    """管理 JPAF 人格狀態：BaseWeights、dominant/auxiliary、turn 計數。"""

    def __init__(
        self,
        persona_key: str = DEFAULT_PERSONA,
        dominant: str | None = None,
        auxiliary: str | None = None,
        base_weights: dict[str, float] | None = None,
        turn_count: int = 0,
        active_history: list[str] | None = None,
    ):
        profile = PERSONA_PROFILES.get(persona_key, PERSONA_PROFILES[DEFAULT_PERSONA])
        self.dominant: str = dominant or profile["dominant"]
        self.auxiliary: str = auxiliary or profile["auxiliary"]
        self.base_weights: dict[str, float] = (
            deepcopy(base_weights) if base_weights else deepcopy(profile["weights"])
        )
        self.current_persona: str = persona_key
        self.turn_count: int = turn_count
        # 近 N 輪的 active_function 歷史，用於程式化 Reflection 判斷
        self.active_history: list[str] = list(active_history or [])

    def increment_turn(self) -> None:
        """每輪對話後遞增 turn 計數。"""
        self.turn_count += 1

    def apply_active_function(self, active_fn: str) -> dict:
        """
        程式化 TemporaryWeight 追蹤 + 自動 Reflection。
        根據模型回傳的 active_function：
        1. 計算 TemporaryWeight = BaseWeight + JPAF_DW
        2. 若 TemporaryWeight 超過 dominant 或 auxiliary 的 BaseWeight → 觸發 Reflection
        3. Reflection：將 JPAF_DW 永久併入 BaseWeights，重新正規化至總和 1.0
        4. 若該功能持續主導（歷史中佔多數），考慮 dominant/auxiliary 替換

        回傳 dict 描述本輪發生的事：
        {
          "active_function": str,
          "temporary_weight": float,
          "reflection_triggered": bool,
          "weight_changes": dict | None,      # 若有更新，新舊值差異
          "dominant_changed": bool,
          "auxiliary_changed": bool,
        }
        """
        if active_fn not in self.base_weights:
            return {
                "active_function": active_fn,
                "temporary_weight": 0.0,
                "reflection_triggered": False,
                "weight_changes": None,
                "dominant_changed": False,
                "auxiliary_changed": False,
            }

        # 記錄歷史（保留最近 REFLECTION_WINDOW 輪）
        self.active_history.append(active_fn)
        if len(self.active_history) > REFLECTION_WINDOW:
            self.active_history = self.active_history[-REFLECTION_WINDOW:]

        # 計算 TemporaryWeight
        base_w = self.base_weights[active_fn]
        temp_w = base_w + JPAF_DW

        # Reflection 觸發條件：TemporaryWeight 超過 dominant 或 auxiliary 的 BaseWeight
        dom_w = self.base_weights[self.dominant]
        aux_w = self.base_weights[self.auxiliary]
        reflection_triggered = (
            active_fn != self.dominant
            and active_fn != self.auxiliary
            and (temp_w > dom_w or temp_w > aux_w)
        )

        weight_changes = None
        dominant_changed = False
        auxiliary_changed = False

        if reflection_triggered:
            # Reflection：永久性 weight 更新
            old_weights = deepcopy(self.base_weights)

            # 將 JPAF_DW 從最低的非活躍功能轉移給 active_fn
            # 找出權重最低的非活躍功能作為「捐贈者」
            donor_fn = min(
                (fn for fn in FUNCTION_ORDER if fn != active_fn),
                key=lambda fn: self.base_weights[fn],
            )
            self.base_weights[active_fn] = round(base_w + JPAF_DW, 4)
            self.base_weights[donor_fn] = round(
                self.base_weights[donor_fn] - JPAF_DW, 4
            )

            # 確保不低於 0，若 donor 已經太低則從次低的取
            if self.base_weights[donor_fn] < 0.0:
                self.base_weights[donor_fn] = 0.0
                # 重新正規化
                self._renormalize()

            # 強制約束：dominant >= JPAF_A, undifferentiated <= JPAF_B
            self._enforce_constraints()

            weight_changes = {
                fn: round(self.base_weights[fn] - old_weights[fn], 4)
                for fn in FUNCTION_ORDER
                if abs(self.base_weights[fn] - old_weights[fn]) > 0.0001
            }

            # 檢查是否需要替換 auxiliary
            # 條件：active_fn 在近 N 輪中出現次數 > auxiliary 出現次數
            if len(self.active_history) >= REFLECTION_WINDOW // 2:
                fn_counts = {}
                for fn in self.active_history:
                    fn_counts[fn] = fn_counts.get(fn, 0) + 1

                active_count = fn_counts.get(active_fn, 0)
                aux_count = fn_counts.get(self.auxiliary, 0)

                if (
                    active_fn != self.dominant
                    and active_fn != self.auxiliary
                    and active_count > aux_count
                    and self.base_weights[active_fn] > self.base_weights[self.auxiliary]
                ):
                    old_aux = self.auxiliary
                    self.auxiliary = active_fn
                    auxiliary_changed = True
                    print(
                        f"[JPAF Reflection] Auxiliary 替換: {old_aux} → {active_fn} "
                        f"(近 {len(self.active_history)} 輪 active 次數: "
                        f"{active_fn}={active_count}, {old_aux}={aux_count})"
                    )

            if weight_changes:
                print(
                    f"[JPAF Reflection] Weight 更新: "
                    + ", ".join(f"{fn}: {d:+.4f}" for fn, d in weight_changes.items())
                )

        return {
            "active_function": active_fn,
            "temporary_weight": round(temp_w, 4),
            "reflection_triggered": reflection_triggered,
            "weight_changes": weight_changes,
            "dominant_changed": dominant_changed,
            "auxiliary_changed": auxiliary_changed,
        }

    def _renormalize(self) -> None:
        """重新正規化 base_weights 使總和 = 1.0。"""
        total = sum(self.base_weights.values())
        if total > 0 and abs(total - 1.0) > 0.001:
            for fn in self.base_weights:
                self.base_weights[fn] = round(self.base_weights[fn] / total, 4)
            # 修正浮點誤差
            diff = 1.0 - sum(self.base_weights.values())
            self.base_weights[self.dominant] = round(
                self.base_weights[self.dominant] + diff, 4
            )

    def _enforce_constraints(self) -> None:
        """強制論文數學約束：dominant >= JPAF_A, undifferentiated <= JPAF_B。"""
        # dominant 不能低於 JPAF_A
        if self.base_weights[self.dominant] < JPAF_A:
            self.base_weights[self.dominant] = JPAF_A
            self._renormalize()

    def apply_reflection(self, state: dict) -> None:
        """驗證並套用 LLM 輸出的 jpaf_state 更新（Reflection 觸發時）。
        保留作為 LLM 主動觸發 Reflection 的備用路徑。"""
        new_w = state.get("base_weights")
        if isinstance(new_w, dict) and set(new_w.keys()) == set(DEFAULT_WEIGHTS.keys()):
            total = sum(new_w.values())
            if abs(total - 1.0) < 0.05:
                self.base_weights = {k: round(float(v), 4) for k, v in new_w.items()}

        new_dom = state.get("dominant")
        if new_dom in FUNCTION_META:
            self.dominant = new_dom

        new_aux = state.get("auxiliary")
        if new_aux in FUNCTION_META and new_aux != self.dominant:
            self.auxiliary = new_aux

    def update_persona(self, jpaf_state: dict) -> None:
        """根據模型輸出的 jpaf_state 更新 current_persona。"""
        suggested = jpaf_state.get("suggested_persona")
        if suggested and suggested in PERSONA_PROFILES:
            self.current_persona = suggested
        else:
            af = jpaf_state.get("active_function") or self.dominant
            self.current_persona = FUNCTION_TO_PERSONA.get(af, DEFAULT_PERSONA)

    def to_dict(self) -> dict:
        """序列化為可持久化的 dict。"""
        return {
            "dominant": self.dominant,
            "auxiliary": self.auxiliary,
            "base_weights": self.base_weights,
            "current_persona": self.current_persona,
            "turn_count": self.turn_count,
            "active_history": self.active_history,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JPAFSession":
        """從持久化 dict 還原。"""
        persona = data.get("current_persona", DEFAULT_PERSONA)
        return cls(
            persona_key=persona,
            dominant=data.get("dominant"),
            auxiliary=data.get("auxiliary"),
            base_weights=data.get("base_weights"),
            turn_count=data.get("turn_count", 0),
            active_history=data.get("active_history"),
        )


# ============================================================
# 工具函式
# ============================================================
def get_effective_meta(persona_key: str) -> dict:
    """取得指定 persona 的有效 FUNCTION_META（合併 meta_override）。"""
    profile = PERSONA_PROFILES.get(persona_key, PERSONA_PROFILES[DEFAULT_PERSONA])
    override = profile.get("meta_override")
    if not override:
        return FUNCTION_META
    return {**FUNCTION_META, **override}


def extract_jpaf_state(text: str) -> Optional[dict]:
    """從 LLM 輸出中解析 <jpaf_state>...</jpaf_state> JSON。"""
    match = re.search(r"<jpaf_state>\s*(.*?)\s*</jpaf_state>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def strip_jpaf_tags(text: str) -> str:
    """移除 <thinking>/<thought>/<jpaf_state> 標籤內容，回傳乾淨的角色對話。"""
    if not text:
        return text
    # 移除巢狀 thought
    text = re.sub(
        r"<thought>\s*<thought>.*?</thought>\s*</thought>",
        "", text, flags=re.DOTALL | re.IGNORECASE,
    )
    # 移除各種思考標籤
    for tag in ("thinking", "think", "thought"):
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(rf"</?{tag}>", "", text, flags=re.IGNORECASE)
    # 移除 jpaf_state
    text = re.sub(r"<jpaf_state>.*?</jpaf_state>", "", text, flags=re.DOTALL)
    return text.strip()
