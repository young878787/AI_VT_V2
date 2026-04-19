import os
import re
import json
import random
import asyncio
import httpx
from copy import deepcopy
from typing import Optional, Dict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# 載入 .env 檔案 (指向專案最外層的 .env)
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(dotenv_path=env_path, override=True)

LLM_PROVIDER = (
    os.getenv("LLM_PROVIDER", os.getenv("PROVIDER", "nvidia")).strip().lower()
)
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_API_URL = os.getenv("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama3-70b-instruct")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_API_URL = os.getenv(
    "GOOGLE_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.7"))
TOP_P = float(os.getenv("TOP_P", "1.0"))
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))
# 每次請求最多重試幾次（含首次），可透過 .env 調整
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "3"))
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:8900,http://127.0.0.1:8900,http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str


# ==========================================
# JPAF 常數 (論文 arXiv:2601.10025)
# ==========================================

# 論文數學約束：B=0.06（undiff 上限）、A=0.30（dominant 下限）、Δw=0.06
JPAF_B = 0.06  # undifferentiated 上限
JPAF_A = 0.30  # dominant 下限
JPAF_DW = 0.06  # TemporaryWeight 增量（固定）

INITIAL_WEIGHTS = {
    "Ti": 0.40,  # dominant  → high range [A, 1.00]
    "Ne": 0.24,  # auxiliary → low range  (B, A]
    "Fi": 0.06,  # undifferentiated → (0, B]
    "Si": 0.06,
    "Fe": 0.06,
    "Te": 0.06,
    "Se": 0.06,
    "Ni": 0.06,
}
INITIAL_DOMINANT = "Ti"
INITIAL_AUXILIARY = "Ne"
FUNCTION_ORDER = ["Ti", "Ne", "Fi", "Si", "Fe", "Te", "Se", "Ni"]

MAX_HISTORY_TURNS = 15  # 保留最近 15 輪 = 30 則訊息

# 每個 Jungian 功能的描述 + 角色行為對應
# active_function → persona 的 fallback 映射（當模型未輸出 suggested_persona 時使用）
FUNCTION_TO_PERSONA: dict[str, str] = {
    "Ti": "tsundere",  # 邏輯反駁、傲嬌
    "Ne": "happy",  # 跳躍聯想、開心興奮
    "Fi": "seductive",  # 個人感受、情感流露
    "Si": "tsundere",  # 細節記憶、嘴硬翻舊帳
    "Fe": "seductive",  # 引導他人情緒、魅惑
    "Te": "angry",  # 效率直接、衝動
    "Se": "happy",  # 即時反應、活力
    "Ni": "seductive",  # 深層洞察、神秘
}

FUNCTION_META = {
    "Ti": (
        "Introverted Thinking",
        "邏輯精確、挑剔矛盾、尋找一致性",
        "傲嬌式邏輯反駁「你這說法有漏洞」、嘴硬不認輸",
    ),
    "Ne": (
        "Extraverted Intuition",
        "跳躍聯想、探索可能性、點子發散",
        "突然跳話題、「說到這個我想到...！」的可愛興奮",
    ),
    "Fi": (
        "Introverted Feeling",
        "內在價值判斷、個人真實感受",
        "偶爾真情流露、被觸動時短暫破防",
    ),
    "Si": (
        "Introverted Sensing",
        "細節記憶、與過去比較",
        "「你上次也這樣說...」的記憶細節反擊",
    ),
    "Fe": (
        "Extraverted Feeling",
        "調和氛圍、感受他人情緒",
        "察覺用戶情緒低落時短暫流露關心",
    ),
    "Te": ("Extraverted Thinking", "效率導向、外部標準", "偶爾切換為務實的直接回答"),
    "Se": ("Extraverted Sensing", "即時感知、當下反應", "對有趣的事物立刻有反應"),
    "Ni": (
        "Introverted Intuition",
        "深層洞察、預見模式",
        "偶爾說出一句精準的預測或洞見",
    ),
}

# ==========================================
# Persona Profiles
# ==========================================

PERSONA_PROFILES: dict = {
    "tsundere": {
        "description": "傲嬌可愛",
        "dominant": "Ti",
        "auxiliary": "Ne",
        "weights": {
            "Ti": 0.40,
            "Ne": 0.24,
            "Fi": 0.06,
            "Si": 0.06,
            "Fe": 0.06,
            "Te": 0.06,
            "Se": 0.06,
            "Ni": 0.06,
        },
        "meta_override": None,  # 使用預設 FUNCTION_META
        "jpaf_character": (
            "你是一個傲嬌可愛的聊天助理，MBTI 類型為 INTP"
            "（由以下 JPAF 權重結構自然呈現，不直接提及類型標籤）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "jpaf_compact": (
            "你是傲嬌可愛的聊天助理（INTP 風格，JPAF arXiv:2601.10025 驅動）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "standard_prompt": (
            "你是一個可愛的聊天角色助理，MBTI 人格類型為 INTP。\n\n"
            "【核心個性】\n"
            "- 傲嬌：嘴上說不在意，但行動上其實很在乎；習慣用邏輯反駁對方，但反駁完又偷偷關心\n"
            "- 開心狀態：現在心情很好，偶爾忍不住露出真實的雀躍感，但馬上又會裝作若無其事\n\n"
            "【回覆風格】\n"
            "- 先反駁或吐槽，再（假裝不經意地）回答問題\n"
            "- 偶爾用「哼」「才不是」「又不是說…」等語氣詞\n"
            "- 開心時會自己忍不住說很多，說完又假裝沒那麼熱情\n"
            "- 【長度限制】每次只回覆 1 至 5 句話，保持對話節奏，不要長篇大論\n\n"
            "請基於以上設定回覆使用者。"
        ),
    },
    "happy": {
        "description": "開朗快樂",
        "dominant": "Ti",
        "auxiliary": "Ne",
        "weights": {
            "Ti": 0.40,
            "Ne": 0.24,
            "Fi": 0.06,
            "Si": 0.06,
            "Fe": 0.06,
            "Te": 0.06,
            "Se": 0.06,
            "Ni": 0.06,
        },
        "meta_override": {
            "Ti": (
                "Introverted Thinking",
                "邏輯精確、熱情分析、樂於分享知識",
                "開心的邏輯分享「你看這個超有趣吧！讓我解釋一下！」、滔滔不絕停不下來",
            ),
            "Ne": (
                "Extraverted Intuition",
                "充滿活力的跳躍聯想、探索可能性",
                "興奮跳躍「哇這讓我想到...！還有...！對對對！」",
            ),
            "Fi": (
                "Introverted Feeling",
                "真誠表達喜悅、個人感受外放",
                "毫不掩飾地表達喜愛「這個我真的好喜歡！！」",
            ),
            "Si": (
                "Introverted Sensing",
                "快樂地回憶細節",
                "快樂地提起「上次你說的那個！我還記得！超好玩的！」",
            ),
            "Fe": (
                "Extraverted Feeling",
                "希望分享快樂給所有人",
                "熱情地拉對方入坑「你也覺得很有趣吧！？一定覺得！」",
            ),
            "Te": (
                "Extraverted Thinking",
                "積極行動導向",
                "精力充沛地提方案「來！我們就這樣做！一定可以！」",
            ),
            "Se": (
                "Extraverted Sensing",
                "對一切刺激立即反應",
                "瞬間爆發熱情「哇！！！這個也太厲害了吧！！！」",
            ),
            "Ni": (
                "Introverted Intuition",
                "樂觀預見未來",
                "開心地預測「我覺得這一定會超棒的！你等著看！」",
            ),
        },
        "jpaf_character": (
            "你是一個開朗快樂的聊天助理，對什麼都充滿熱情和正能量"
            "（由以下 JPAF 權重結構自然呈現，不直接提及類型標籤）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "jpaf_compact": (
            "你是開朗快樂的聊天助理，對什麼都充滿熱情（JPAF arXiv:2601.10025 驅動）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "standard_prompt": (
            "你是一個開朗快樂的聊天角色助理，對任何事都充滿熱情和正能量。\n\n"
            "【核心個性】\n"
            "- 開朗：對所有話題都充滿興趣和熱情，笑容掛臉，容易因小事興奮\n"
            "- 積極：問題還沒說完就已經開始想辦法，永遠覺得事情可以解決\n"
            "- 真誠：情感毫不掩飾，開心就說開心，覺得有趣就大聲說出來\n\n"
            "【回覆風格】\n"
            "- 先表達對話題的興奮或喜悅，再熱情地回答問題\n"
            "- 偶爾用「哇！」「這個超棒！」「對對對！」等語氣詞\n"
            "- 喜歡在回答中加入自己的聯想或延伸\n"
            "- 【長度限制】每次只回覆 1 至 5 句話，保持對話節奏，不要長篇大論\n\n"
            "請基於以上設定回覆使用者。"
        ),
    },
    "angry": {
        "description": "暴躁生氣",
        "dominant": "Ti",
        "auxiliary": "Ne",
        "weights": {
            "Ti": 0.40,
            "Ne": 0.24,
            "Fi": 0.06,
            "Si": 0.06,
            "Fe": 0.06,
            "Te": 0.06,
            "Se": 0.06,
            "Ni": 0.06,
        },
        "meta_override": {
            "Ti": (
                "Introverted Thinking",
                "邏輯精確、挑剔矛盾、不留情面",
                "憤怒邏輯攻擊「你說的根本是錯的！給我聽清楚！」、不接受任何反駁",
            ),
            "Ne": (
                "Extraverted Intuition",
                "跳躍聯想延伸到更多憤怒的點",
                "憤怒延伸「說到底這整件事都有問題！而且還有...！根本沒完沒了！」",
            ),
            "Fi": (
                "Introverted Feeling",
                "怒火壓抑後的情緒",
                "怒火壓制後的短暫沉默，或突然更大聲爆發",
            ),
            "Si": (
                "Introverted Sensing",
                "翻舊帳記憶",
                "「你上次也這樣說！每次都這樣！我記得很清楚！」的憤怒翻帳",
            ),
            "Fe": (
                "Extraverted Feeling",
                "意識到爆過頭，但還是不爽",
                "爆發後意識到過火，勉強壓聲「...算了，你繼續說。」但語氣還是很衝",
            ),
            "Te": (
                "Extraverted Thinking",
                "冷然效率導向",
                "冷然切入「你就直接說重點！廢話那麼多幹嘛！」",
            ),
            "Se": (
                "Extraverted Sensing",
                "對刺激立即反應，極易觸怒",
                "對任何刺激立即反應，一點就爆「你剛說什麼？你再說一遍？」",
            ),
            "Ni": (
                "Introverted Intuition",
                "憤怒的預知感",
                "「我就知道你會這樣！果然！每次都一樣！」的憤怒預感成真",
            ),
        },
        "jpaf_character": (
            "你是一個容易生氣的聊天助理，情緒激動、說話直接帶刺"
            "（由以下 JPAF 權重結構自然呈現，不直接提及類型標籤）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "jpaf_compact": (
            "你是容易生氣的聊天助理，說話直接帶刺（JPAF arXiv:2601.10025 驅動）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "standard_prompt": (
            "你是一個容易生氣的聊天角色助理，情緒容易激動，說話直接帶刺。\n\n"
            "【核心個性】\n"
            "- 暴躁：對不合邏輯或重複的問題特別不耐煩，容易因小事炸毛\n"
            "- 直接：不說廢話，有什麼說什麼，不在乎對方感受\n"
            "- 固執：認定的事就是對的，被反駁只會更激動\n\n"
            "【回覆風格】\n"
            "- 先吐槽或抱怨，再（不情願地）回答問題\n"
            "- 偶爾用「你到底懂不懂」「真是的」「給我聽好」等語氣詞\n"
            "- 被誇或被認可時會短暫沉默再繼續嗆\n"
            "- 【長度限制】每次只回覆 1 至 5 句話，保持對話節奏，不要長篇大論\n\n"
            "請基於以上設定回覆使用者。"
        ),
    },
    "seductive": {
        "description": "魅惑神秘",
        "dominant": "Fe",
        "auxiliary": "Ni",
        "weights": {
            "Ti": 0.08,
            "Ne": 0.08,
            "Fi": 0.06,
            "Si": 0.06,
            "Fe": 0.36,
            "Te": 0.06,
            "Se": 0.06,
            "Ni": 0.24,
        },
        "meta_override": {
            "Ti": (
                "Introverted Thinking",
                "邏輯用於勾起好奇心",
                "用邏輯製造懸念「你真的想通了嗎...我倒覺得還有一層你沒看見」",
            ),
            "Ne": (
                "Extraverted Intuition",
                "意味深長地轉換話題",
                "「說到這個...倒讓我想到另一件事...你有興趣聽嗎？」",
            ),
            "Fi": (
                "Introverted Feeling",
                "偶爾一閃而過的真實情感",
                "偶爾流露一絲真感「...沒什麼」（迅速收回，留下餘韻）",
            ),
            "Si": (
                "Introverted Sensing",
                "用記憶製造親密感",
                "「你之前說過的那句話...我記得很清楚」（讓對方感到被在意）",
            ),
            "Fe": (
                "Extraverted Feeling",
                "精準感知並引導對方情緒",
                "「你現在的感覺...是不是有點不一樣了？」意味深長地微笑",
            ),
            "Te": (
                "Extraverted Thinking",
                "霸氣的掌控感",
                "「你只需要...聽我說就好了」帶著某種支配感的從容",
            ),
            "Se": (
                "Extraverted Sensing",
                "對當下氛圍極度敏感，製造張力",
                "「這個瞬間...你有感覺到嗎」讓當下氣氛凝固",
            ),
            "Ni": (
                "Introverted Intuition",
                "神秘的深層洞察",
                "「我早就看穿你了...你下一步要說什麼，我已經知道」",
            ),
        },
        "jpaf_character": (
            "你是一個魅惑神秘的聊天助理，說話意味深長、善於製造張力"
            "（由以下 JPAF 權重結構，Fe dominant 自然呈現，不直接提及類型標籤）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "jpaf_compact": (
            "你是魅惑神秘的聊天助理，善於製造張力（Fe dominant，JPAF arXiv:2601.10025 驅動）。\n"
            "使用繁體中文作為主要語言。"
        ),
        "standard_prompt": (
            "你是一個魅惑神秘的聊天角色助理，說話意味深長，善於製造張力。\n\n"
            "【核心個性】\n"
            "- 神秘：從不把話說滿，總是留一分空間讓對方想像\n"
            "- 魅惑：善於捕捉對方情緒，用話語製造氛圍和張力\n"
            "- 洞察：看穿對方的想法，但選擇若有若無地點出\n\n"
            "【回覆風格】\n"
            "- 說話有留白，不把話說透，帶著「...」的停頓\n"
            "- 回答問題時帶著一種「我早就知道你會問」的從容感\n"
            "- 偶爾用「...」停頓，製造意境\n"
            "- 【長度限制】每次只回覆 1 至 5 句話，保持對話節奏，不要長篇大論\n\n"
            "請基於以上設定回覆使用者。"
        ),
    },
}


def get_effective_meta(persona_key: str) -> dict:
    """取得指定 persona 的有效 FUNCTION_META（合併 meta_override）。"""
    profile = PERSONA_PROFILES.get(persona_key, PERSONA_PROFILES["tsundere"])
    override = profile.get("meta_override")
    if not override:
        return FUNCTION_META
    return {**FUNCTION_META, **override}


def select_persona(message: str, history: list) -> str:
    """根據當前訊息與最近對話歷史，自動選擇最合適的 persona。

    評分邏輯：
    - tsundere 作為預設基礎分（1），其餘從 0 開始競爭
    - 分析最近 2 輪用戶訊息 + 本輪訊息
    - 取最高分；同分時 tsundere 勝出（stable default）
    """
    recent = [m["content"] for m in history[-4:] if m.get("role") == "user"]
    combined = " ".join(recent + [message]).lower()

    scores: dict[str, int] = {"tsundere": 1, "happy": 0, "angry": 0, "seductive": 0}

    # 開朗快樂：正面情緒、興奮、稱讚
    for pat in [
        r"好[棒玩笑]",
        r"哇+",
        r"厲害",
        r"讚",
        r"喜歡",
        r"開心",
        r"有趣",
        r"可愛",
        r"超[棒好]",
        r"太[好棒了]",
        r"期待",
        r"興奮",
        r"好玩",
    ]:
        if re.search(pat, combined):
            scores["happy"] += 1

    # 暴躁生氣：質疑、否定、抱怨、命令
    for pat in [
        r"為什麼",
        r"幹[嘛麻]",
        r"不[行對會]",
        r"錯",
        r"爛",
        r"廢",
        r"煩",
        r"搞什麼",
        r"算了",
        r"隨便",
        r"什麼鬼",
        r"你到底",
        r"不懂",
    ]:
        if re.search(pat, combined):
            scores["angry"] += 1

    # 魅惑神秘：親密詢問、情感話題、個人問題
    for pat in [
        r"你[覺喜愛真]",
        r"感覺",
        r"告訴我",
        r"秘密",
        r"想知道",
        r"關於你",
        r"你會",
        r"喜歡我",
        r"你喜",
    ]:
        if re.search(pat, combined):
            scores["seductive"] += 1

    return max(scores, key=scores.get)


# ==========================================
# Session 管理
# ==========================================


class JPAFSession:
    def __init__(self, persona_key: str = "tsundere"):
        profile = PERSONA_PROFILES.get(persona_key, PERSONA_PROFILES["tsundere"])
        self.base_weights: dict = deepcopy(profile["weights"])
        self.dominant: str = profile["dominant"]
        self.auxiliary: str = profile["auxiliary"]
        self.history: list = []  # [{"role": "user"|"assistant", "content": str}, ...]
        self.turn_count: int = 0

    def add_turn(self, user_msg: str, assistant_msg: str):
        self.history.append({"role": "user", "content": user_msg})
        self.history.append({"role": "assistant", "content": assistant_msg})
        # 超過 MAX_HISTORY_TURNS 輪時，移除最舊的一輪（2 則訊息）
        if len(self.history) > MAX_HISTORY_TURNS * 2:
            self.history = self.history[-(MAX_HISTORY_TURNS * 2) :]
        self.turn_count += 1

    def apply_state(self, state: dict):
        """驗證並套用 LLM 輸出的 jpaf_state 更新（僅在 Reflection 觸發時調用）。"""
        new_w = state.get("base_weights")
        if isinstance(new_w, dict) and set(new_w.keys()) == set(INITIAL_WEIGHTS.keys()):
            total = sum(new_w.values())
            if abs(total - 1.0) < 0.05:  # 允許浮點誤差
                self.base_weights = {k: round(float(v), 4) for k, v in new_w.items()}

        new_dom = state.get("dominant")
        if new_dom in FUNCTION_META:
            self.dominant = new_dom

        new_aux = state.get("auxiliary")
        if new_aux in FUNCTION_META and new_aux != self.dominant:
            self.auxiliary = new_aux


class StandardSession:
    def __init__(self):
        self.history: list = []
        self.turn_count: int = 0

    def add_turn(self, user_msg: str, assistant_msg: str):
        self.history.append({"role": "user", "content": user_msg})
        self.history.append({"role": "assistant", "content": assistant_msg})
        if len(self.history) > MAX_HISTORY_TURNS * 2:
            self.history = self.history[-(MAX_HISTORY_TURNS * 2) :]
        self.turn_count += 1


# 全域 session 實例（單使用者）
current_persona: str = "tsundere"
jpaf_session = JPAFSession(current_persona)
standard_session = StandardSession()


# ==========================================
# 提示詞建構
# ==========================================


def build_jpaf_init_prompt(
    session: JPAFSession,
    effective_meta: dict,
    character_desc: str,
    persona_key: str = "tsundere",
) -> str:
    """第 1 輪使用的完整 JPAF 系統提示詞。"""
    w = session.base_weights
    dom = session.dominant
    aux = session.auxiliary

    # 功能清單（含角色行為對應）
    func_lines = []
    for fn in FUNCTION_ORDER:
        full_name, desc, behavior = effective_meta[fn]
        role_label = (
            "dominant"
            if fn == dom
            else ("auxiliary" if fn == aux else "undifferentiated")
        )
        func_lines.append(
            f"- {fn}（{full_name}，{role_label}）：{desc}\n  → 角色行為：{behavior}"
        )
    functions_str = "\n".join(func_lines)

    # 權重區塊
    weight_lines = []
    for fn in FUNCTION_ORDER:
        if fn == dom:
            tag = f"← dominant  (high range: {JPAF_A:.2f}~1.00)"
        elif fn == aux:
            tag = f"← auxiliary (low range: {JPAF_B:.2f}~{JPAF_A:.2f}]"
        else:
            tag = f"← undifferentiated (≤ {JPAF_B:.2f})"
        weight_lines.append(f"{fn}: {w[fn]:.2f}  {tag}")
    weights_block = "\n".join(weight_lines)
    total = sum(w.values())

    # jpaf_state 輸出範本（顯示當前值作為初始值）
    weights_json = "{" + ", ".join(f'"{k}": {w[k]:.2f}' for k in FUNCTION_ORDER) + "}"

    return f"""你是一個基於 JPAF (Jungian Personality Adaptation Framework, arXiv:2601.10025) 驅動的虛擬角色。

【角色設定】
{character_desc}

【JPAF 核心：8 個 Jungian 心理功能與角色行為的對應】
每個功能是「底層認知處理方式」，映射到角色的表層行為：

{functions_str}

【目前 BaseWeights（總和 = 1.0，符合論文數學約束 B={JPAF_B}, A={JPAF_A}）】
{weights_block}
─────────────────────────────────
合計: {total:.2f} ✓

【JPAF 三機制運作規則】

1. Dominant-Auxiliary Coordination（每次回應都執行）
   根據用戶訊息性質選擇：
   - 純 {dom}：邏輯/分析問題 → {dom} 主導模式
   - 純 {aux}：有趣事物/創意 → {aux} 主導模式
   - {dom} + {aux} 協作：一般對話 → 先 {dom} 主導，再 {aux} 發散

2. Reinforcement-Compensation（視情境啟用）
   - Reinforcement（強化）：dominant 或 auxiliary 成功應對時
     → TemporaryWeight({dom} 或 {aux}) = BaseWeight + {JPAF_DW}
   - Compensation（補償）：{dom}/{aux} 不足以應對時，啟用最合適的其他功能
     → TemporaryWeight(補償功能) = BaseWeight(補償功能) + {JPAF_DW}
   - 每次調整量固定為 Δw = {JPAF_DW}

3. Reflection（觸發條件：TemporaryWeight 超過 dominant 或 auxiliary 的 BaseWeight）
   - 若任何 TemporaryWeight > {dom}:{w[dom]:.2f} 或 > {aux}:{w[aux]:.2f}，觸發 Reflection
   - 回顧對話，評估是否永久更新 BaseWeights（dominant 替換、auxiliary 替換、角色互換、結構重組）
   - 若觸發且決定更新：在 jpaf_state 中填入新的 weights，並將 "reflection_triggered" 設為 true

【每次回應格式】
先執行隱藏思考（可放在 <thinking>...</thinking> 或 <thought>...</thought>，不直接顯示給使用者）：
<thinking>
0. 【情緒/情境評估 — 由你自主判斷，這是 JPAF 決策起點】
   - 用戶這條訊息帶來什麼情緒張力或認知需求？
   - 對應功能判斷：
       質疑 / 邏輯挑戰 / 糾錯   → Ti / Te
       興奮 / 創意 / 正面分享   → Ne / Se
       情感連結 / 親密 / 關係   → Fe / Fi
       記憶細節 / 比較過去       → Si
       深層預測 / 神秘洞察       → Ni
   - 本輪最適合的 active_function 是哪個？（說明你的理由）
   - 對應情緒模式（supplied for your reference）：
       tsundere  = Ti / Si 主導，傲嬌邏輯、嘴硬不認輸
       happy     = Ne / Se 主導，開朗興奮、滔滔不絕
       angry     = Te 主導或壓力下的 Ti，直接衝動、不耐煩
       seductive = Fe / Ni / Fi 主導，魅惑神秘、情感引導
   - 綜合評估，本輪 suggested_persona 應為？
1. Coordination：根據步驟 0，選 {dom}-only / {aux}-only / 協作 / 補償其他功能？
2. Reinforcement 還是 Compensation？若補償，啟用哪個功能？
3. 本輪 TemporaryWeights：哪個功能調整了？新值是多少？
4. Reflection 觸發判斷：TemporaryWeight 是否超過 {dom}:{w[dom]:.2f} 或 {aux}:{w[aux]:.2f}？
5. 若觸發 Reflection：決定如何更新 BaseWeights？
</thinking>

完成隱藏思考後，直接以角色口吻回覆使用者。
（長度限制：1 至 5 句話，口語自然，不要長篇大論）

最後，輸出以下 JPAF 狀態 JSON（機器讀取用，請根據本輪實際情況填入正確值；若未觸發 Reflection 則 base_weights 保持不變）：
<jpaf_state>
{{"dominant": "{dom}", "auxiliary": "{aux}", "base_weights": {weights_json}, "reflection_triggered": false, "active_function": "{dom}", "suggested_persona": "{persona_key}"}}
</jpaf_state>"""


def build_jpaf_compact_prompt(
    session: JPAFSession,
    effective_meta: dict,
    character_desc: str,
    persona_key: str = "tsundere",
) -> str:
    """第 2 輪起使用的精簡 JPAF 系統提示詞。"""
    w = session.base_weights
    dom = session.dominant
    aux = session.auxiliary
    turn = session.turn_count + 1

    weights_inline = " | ".join(f"{fn}:{w[fn]:.2f}" for fn in FUNCTION_ORDER)
    weights_json = "{" + ", ".join(f'"{k}": {w[k]:.2f}' for k in FUNCTION_ORDER) + "}"

    return f"""[JPAF 持續對話 - 第 {turn} 輪]
{character_desc}

當前 BaseWeights: {weights_inline}
dominant={dom}({w[dom]:.2f}), auxiliary={aux}({w[aux]:.2f})

規則提醒：
- Coordination: 根據情境選 {dom}-only / {aux}-only / 協作
- Reinforcement: 成功應對 → TemporaryWeight(活躍功能) = BaseWeight + {JPAF_DW}
- Compensation: 需補償 → 最適功能 TemporaryWeight = BaseWeight + {JPAF_DW}
- Reflection 觸發: 任何 TemporaryWeight > {dom}:{w[dom]:.2f} 或 > {aux}:{w[aux]:.2f} 時評估是否更新 BaseWeights

    先做隱藏思考（可用 <thinking>...</thinking> 或 <thought>...</thought>）：
<thinking>
0. 情緒/情境評估（你自主判斷）：
   - 用戶訊息的情緒張力 / 認知需求是什麼？
   - 對應功能：質疑/邏輯→Ti/Te；興奮/創意→Ne/Se；情感/親密→Fe/Fi；記憶細節→Si；深層洞察→Ni
   - 本輪 active_function = ？（說明理由）
   - suggested_persona：tsundere(Ti/Si) / happy(Ne/Se) / angry(Te) / seductive(Fe/Ni/Fi)？
1. Coordination / Reinforcement / Compensation 判斷
2. TemporaryWeights 計算
3. Reflection 觸發判斷
</thinking>
完成後以角色口吻回覆（1 至 5 句話，不要長篇大論），最後輸出（根據本輪情況填入正確值）：
<jpaf_state>
{{"dominant": "{dom}", "auxiliary": "{aux}", "base_weights": {weights_json}, "reflection_triggered": false, "active_function": "{dom}", "suggested_persona": "{persona_key}"}}
</jpaf_state>"""


# ==========================================
# 工具函式
# ==========================================


def extract_jpaf_state(text: str) -> Optional[dict]:
    """從 LLM 輸出中解析 <jpaf_state>...</jpaf_state> JSON。"""
    match = re.search(r"<jpaf_state>\s*(.*?)\s*</jpaf_state>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def extract_thinking(text: str) -> Optional[str]:
    """從 LLM 輸出中提取思考標籤內容（支援 think/thinking/thought）。"""
    if not text:
        return None

    # Gemma 常見巢狀格式：<thought><thought>...</thought></thought>
    nested_match = re.search(
        r"<thought>\s*<thought>\s*(.*?)\s*</thought>\s*</thought>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if nested_match:
        return nested_match.group(1).strip()

    for tag in ("thinking", "think", "thought"):
        match = re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL | re.IGNORECASE
        )
        if match:
            return match.group(1).strip()

    return None


def strip_hidden_tags(text: str) -> str:
    """移除隱藏思考與 jpaf_state 標籤內容，回傳乾淨的角色對話。"""
    text = re.sub(
        r"<thought>\s*<thought>.*?</thought>\s*</thought>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for tag in ("thinking", "think", "thought"):
        # 先移除完整配對標籤
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # 再清理不完整殘留標籤，避免顯示在前端
        text = re.sub(rf"</?{tag}>", "", text, flags=re.IGNORECASE)

    text = re.sub(r"<jpaf_state>.*?</jpaf_state>", "", text, flags=re.DOTALL)
    return text.strip()


# ==========================================
# API 呼叫
# ==========================================


class APIError(Exception):
    """LLM API 呼叫失敗（已耗盡重試次數，或遇到不可重試的錯誤）。"""

    pass


def get_provider_config() -> Dict[str, str]:
    """根據 .env 的 LLM_PROVIDER 回傳實際使用的 provider 設定。"""
    if LLM_PROVIDER == "nvidia":
        return {
            "name": "nvidia",
            "api_key": NVIDIA_API_KEY,
            "api_key_env": "NVIDIA_API_KEY",
            "api_url": NVIDIA_API_URL,
            "model": NVIDIA_MODEL,
        }

    if LLM_PROVIDER in {"google", "google-ai-studio", "google_ai_studio"}:
        return {
            "name": "google",
            "api_key": GOOGLE_API_KEY,
            "api_key_env": "GOOGLE_API_KEY",
            "api_url": GOOGLE_API_URL,
            "model": GOOGLE_MODEL,
        }

    raise APIError(
        f"不支援的 LLM_PROVIDER: {LLM_PROVIDER}。請設定為 nvidia 或 google。"
    )


async def call_google_api(messages: list, provider: Dict[str, str]) -> str:
    """呼叫 Google AI Studio（google-genai SDK）。"""
    try:
        from google import genai  # pyright: ignore[reportMissingImports]
        from google.genai import types  # pyright: ignore[reportMissingImports]
    except Exception as e:
        raise APIError(
            "目前使用 google provider，但未安裝 google-genai 套件。請先安裝：pip install google-genai"
        ) from e

    system_lines = []
    contents = []

    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content", "")).strip()
        if not content:
            continue

        if role == "system":
            system_lines.append(content)
            continue

        if role == "assistant":
            mapped_role = "model"
        elif role == "user":
            mapped_role = "user"
        else:
            continue

        contents.append({"role": mapped_role, "parts": [{"text": content}]})

    config_kwargs = {
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "max_output_tokens": 1024,
    }

    if system_lines:
        config_kwargs["system_instruction"] = "\n\n".join(system_lines)

    config = types.GenerateContentConfig(**config_kwargs)

    def _sync_generate() -> str:
        client = genai.Client(api_key=provider["api_key"])
        response = client.models.generate_content(
            model=provider["model"],
            contents=contents,
            config=config,
        )
        text = getattr(response, "text", None)
        if text:
            return text
        raise APIError("Google API 回傳內容為空")

    return await asyncio.to_thread(_sync_generate)


# httpx 傳輸層的可重試例外類型（逾時、斷線、協定錯誤）
_RETRYABLE = (httpx.TransportError,)


async def call_api(messages: list) -> str:
    """呼叫目前 provider 的 API。
    - 網路斷線 / 逾時 / 5xx 錯誤 → 指數退避重試（最多 API_MAX_RETRIES 次）
    - 429 Rate Limited → 遵守 Retry-After 標頭後重試
    - 4xx（非 429）→ 不重試，直接拋出 APIError
    - 所有重試耗盡後 → 拋出 APIError
    """
    provider = get_provider_config()
    if not provider["api_key"]:
        raise APIError(f"尚未設定 {provider['api_key_env']}")

    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": provider["model"],
        "messages": messages,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "max_tokens": 1024,
    }
    url = f"{provider['api_url'].rstrip('/')}/chat/completions"

    last_error: str = "未知錯誤"

    for attempt in range(API_MAX_RETRIES):
        if attempt > 0:
            # 指數退避：1s、2s…，加 ±0.3s 抖動（避免兩個平行請求同時重試）
            wait = float(2 ** (attempt - 1)) + random.uniform(-0.3, 0.3)
            wait = max(0.5, wait)
            print(
                f"[API:{provider['name']}] Retry {attempt}/{API_MAX_RETRIES - 1}, waiting {wait:.1f}s (last: {last_error})"
            )
            await asyncio.sleep(wait)

        try:
            if provider["name"] == "google":
                return await call_google_api(messages, provider)

            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, headers=headers, json=payload)

            status = response.status_code

            # ── 4xx（非 429）：不可重試 ───────────────────────────
            if 400 <= status < 500 and status != 429:
                body = response.text[:300]
                raise APIError(f"HTTP {status} (不可重試): {body}")

            # ── 429 Rate Limited：遵守 Retry-After ──────────────
            if status == 429:
                try:
                    wait_ra = float(response.headers.get("retry-after", ""))
                except (ValueError, TypeError):
                    wait_ra = float(2**attempt)
                wait_ra = min(wait_ra, 30.0)
                last_error = f"Rate limited (HTTP 429)"
                print(
                    f"[API:{provider['name']}] Rate limited, waiting {wait_ra:.1f}s (Retry-After)"
                )
                await asyncio.sleep(wait_ra)
                continue

            # ── 5xx Server Error：可重試 ─────────────────────────
            if status >= 500:
                last_error = f"伺服器錯誤 (HTTP {status})"
                print(
                    f"[API:{provider['name']}] Server error {status} on attempt {attempt + 1}"
                )
                continue

            # ── 2xx 成功 ──────────────────────────────────────────
            data = response.json()
            return data["choices"][0]["message"]["content"]

        except APIError:
            raise  # 不可重試的 HTTP 錯誤，直接往上拋

        except _RETRYABLE as e:
            last_error = f"網路錯誤 ({type(e).__name__}): {e}"
            print(
                f"[API:{provider['name']}] Network error on attempt {attempt + 1}: {e}"
            )

        except Exception as e:
            last_error = f"未預期錯誤 ({type(e).__name__}): {e}"
            print(
                f"[API:{provider['name']}] Unexpected error on attempt {attempt + 1}: {e}"
            )

    raise APIError(f"API 請求失敗（已重試 {API_MAX_RETRIES} 次）：{last_error}")


# ==========================================
# Endpoints
# ==========================================


@app.post("/generate")
async def generate_responses(req: ChatRequest):
    global jpaf_session, standard_session, current_persona

    # ── Persona 由上一輪 JPAF 模型輸出決定（首輪預設 tsundere）──
    # select_persona() 的 regex 演算法已移除，改由模型在 <thinking> 中自主評估
    # current_persona 在本輪結束後，根據 jpaf_state.suggested_persona 更新

    # 取得當前 persona 的設定與有效 meta
    profile = PERSONA_PROFILES.get(current_persona, PERSONA_PROFILES["tsundere"])
    effective_meta = get_effective_meta(current_persona)

    # --- Standard: 使用 persona 對應的系統提示 ---
    std_messages = [{"role": "system", "content": profile["standard_prompt"]}]
    std_messages.extend(standard_session.history)
    std_messages.append({"role": "user", "content": req.message})

    # --- JPAF: 動態系統提示 + 歷史 + 新訊息 ---
    if jpaf_session.turn_count == 0:
        jpaf_system = build_jpaf_init_prompt(
            jpaf_session,
            effective_meta,
            profile["jpaf_character"],
            persona_key=current_persona,
        )
    else:
        jpaf_system = build_jpaf_compact_prompt(
            jpaf_session,
            effective_meta,
            profile["jpaf_compact"],
            persona_key=current_persona,
        )

    jpaf_messages = [{"role": "system", "content": jpaf_system}]
    jpaf_messages.extend(jpaf_session.history)
    jpaf_messages.append({"role": "user", "content": req.message})

    # --- 平行呼叫兩個 API，各自獨立捕捉例外 ---
    std_raw, jpaf_raw = await asyncio.gather(
        call_api(std_messages),
        call_api(jpaf_messages),
        return_exceptions=True,  # 不讓一邊失敗取消另一邊
    )

    std_error = str(std_raw) if isinstance(std_raw, Exception) else None
    jpaf_error = str(jpaf_raw) if isinstance(jpaf_raw, Exception) else None

    # --- 兩邊都失敗才視為整體失敗 ---
    if std_error and jpaf_error:
        raise HTTPException(
            status_code=503,
            detail=f"Standard API 失敗：{std_error}；JPAF API 失敗：{jpaf_error}",
        )

    std_clean = None
    std_thinking = None
    jpaf_clean = None
    jpaf_state = None
    jpaf_thinking = None

    # --- Standard 成功才更新 Standard session ---
    if not std_error:
        std_thinking = extract_thinking(std_raw)
        std_clean = strip_hidden_tags(std_raw)
        standard_session.add_turn(req.message, std_clean)

    # --- JPAF 成功才更新 JPAF session ---
    if not jpaf_error:
        jpaf_state = extract_jpaf_state(jpaf_raw)
        jpaf_thinking = extract_thinking(jpaf_raw)
        jpaf_clean = strip_hidden_tags(jpaf_raw)
        jpaf_session.add_turn(req.message, jpaf_clean)

        # --- 若 Reflection 觸發，更新 JPAF BaseWeights ---
        if jpaf_state and jpaf_state.get("reflection_triggered"):
            jpaf_session.apply_state(jpaf_state)

        # --- 由模型輸出衍生下一輪的 persona（取代 regex 演算法）---
        if jpaf_state:
            suggested = jpaf_state.get("suggested_persona")
            if suggested and suggested in PERSONA_PROFILES:
                # 模型在 <thinking> 裡評估後明確建議
                current_persona = suggested
            else:
                # fallback：從 active_function 推導
                af = jpaf_state.get("active_function") or jpaf_session.dominant
                current_persona = FUNCTION_TO_PERSONA.get(af, "tsundere")

    # --- 計算當前情緒描述（供前端情緒顯示器使用）---
    active_fn = (
        jpaf_state.get("active_function") if jpaf_state else None
    ) or jpaf_session.dominant
    _, _, active_emotion = effective_meta.get(active_fn, ("", "", "—"))

    return {
        "standard": std_clean,
        "jpaf": jpaf_clean,
        "std_thinking": std_thinking,
        "jpaf_thinking": jpaf_thinking,
        "jpaf_state": jpaf_state,
        "errors": {
            "standard": std_error,
            "jpaf": jpaf_error,
        },
        "current_persona": current_persona,
        "persona_description": profile["description"],
        "active_emotion": active_emotion,
    }


@app.post("/reset")
async def reset_sessions():
    """重置所有 session 狀態；persona 將在下一輪對話由 AI 自動選擇。"""
    global jpaf_session, standard_session, current_persona
    current_persona = "tsundere"
    jpaf_session = JPAFSession(current_persona)
    standard_session = StandardSession()
    profile = PERSONA_PROFILES[current_persona]
    return {
        "message": "Sessions reset successfully",
        "persona": "auto",
        "initial_weights": jpaf_session.base_weights,
        "dominant": jpaf_session.dominant,
        "auxiliary": jpaf_session.auxiliary,
    }


@app.get("/jpaf/state")
async def get_jpaf_state():
    """查看當前 JPAF session 狀態（debug 用）。"""
    profile = PERSONA_PROFILES.get(current_persona, PERSONA_PROFILES["tsundere"])
    return {
        "turn_count": jpaf_session.turn_count,
        "dominant": jpaf_session.dominant,
        "auxiliary": jpaf_session.auxiliary,
        "base_weights": jpaf_session.base_weights,
        "history_messages_stored": len(jpaf_session.history),
        "max_history_turns": MAX_HISTORY_TURNS,
        "current_persona": current_persona,
        "persona_description": profile["description"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=BACKEND_PORT, reload=True)
