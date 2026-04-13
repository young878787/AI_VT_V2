import os
import re
import json
import asyncio
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from dotenv import load_dotenv
import tiktoken

# Load environment variables
load_dotenv(dotenv_path="../.env")

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# AI Provider 設定（從 .env 讀取）
# ============================================================
AI_PROVIDER = os.getenv("AI_PROVIDER", "openrouter").lower().strip()

# Provider 設定對照表
_PROVIDER_CONFIG = {
    "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url_env": "OPENROUTER_BASE_URL",
        "model_env":   "OPENROUTER_MODEL_NAME",
        "base_url_default": "https://openrouter.ai/api/v1",
        "model_default":    "nvidia/nemotron-3-super-120b-a12b:free",
    },
    "nvidia": {
        "api_key_env": "NVIDIA_API_KEY",
        "base_url_env": "NVIDIA_BASE_URL",
        "model_env":   "NVIDIA_MODEL_NAME",
        "base_url_default": "https://integrate.api.nvidia.com/v1",
        "model_default":    "meta/llama-3.3-70b-instruct",
    },
}

if AI_PROVIDER not in _PROVIDER_CONFIG:
    print(f"Warning: 未知的 AI_PROVIDER='{AI_PROVIDER}'，自動回退到 openrouter")
    AI_PROVIDER = "openrouter"

_cfg = _PROVIDER_CONFIG[AI_PROVIDER]

API_KEY   = os.getenv(_cfg["api_key_env"], "")
BASE_URL  = os.getenv(_cfg["base_url_env"], _cfg["base_url_default"])
MODEL_NAME = os.getenv(_cfg["model_env"],   _cfg["model_default"])

if not API_KEY:
    print(f"Warning: {_cfg['api_key_env']} 未設定，請檢查 .env 檔案")

print(f"[AI Provider] {AI_PROVIDER.upper()} | Model: {MODEL_NAME} | URL: {BASE_URL}")

# 初始化 OpenAI 相容客戶端（OpenRouter 與 Nvidia 均支援 OpenAI SDK）
client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
)

# ============================================================
# Provider 特定參數
# ============================================================
def _build_extra_body() -> dict:
    """
    建構傳給 API 的額外參數（extra_body）。
    - Nvidia Qwen3 系列需要 chat_template_kwargs: {enable_thinking: True}
      才能啟用 Chain-of-Thought 推理模式。
    - OpenRouter 不需要額外參數。
    """
    if AI_PROVIDER == "nvidia":
        return {"chat_template_kwargs": {"enable_thinking": True}}
    return {}

# 預先計算（啟動時固定，不需每次呼叫重建）
_EXTRA_BODY: dict = _build_extra_body()

# Qwen3 thinking 區塊的 regex
_RE_THINK = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)

def _strip_thinking(text: str) -> str:
    """
    移除 Qwen3 等 thinking 模型輸出中的 <think>...</think> 內部推理區塊。
    前端只需要最終對白，不需要看到 CoT 思考過程。
    """
    if not text:
        return text
    stripped = _RE_THINK.sub('', text).strip()
    return stripped

# ============================================================
# 記憶系統 - 檔案路徑與常數
# ============================================================
MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
USER_PROFILE_PATH = os.path.join(MEMORY_DIR, "user_profile.json")
MEMORY_MD_PATH = os.path.join(MEMORY_DIR, "memory.md")

# Context 壓縮閾值（token 數），留 buffer 在 256K 之前觸發
COMPRESS_TOKEN_THRESHOLD = 230_000
# 壓縮時保留最近的 messages 數量
COMPRESS_KEEP_RECENT = 20

# tiktoken 編碼器（使用 cl100k_base 作為通用估算）
try:
    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoding = None

# ============================================================
# In-Memory Cache（減少每輪對話的磁碟 I/O）
# ============================================================
_profile_cache: dict | None = None
_memory_cache: str | None = None


# ============================================================
# 記憶系統 - 讀寫輔助函式
# ============================================================
def load_user_profile() -> dict:
    """讀取 user_profile.json（優先從 cache，減少磁碟 I/O）"""
    global _profile_cache
    if _profile_cache is not None:
        return _profile_cache
    try:
        with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
            _profile_cache = json.load(f)
            return _profile_cache
    except (FileNotFoundError, json.JSONDecodeError):
        _profile_cache = {
            "updated_at": "",
            "core_traits": [],
            "communication_style": "",
            "dislikes": [],
            "recent_interests": [],
            "custom_notes": []
        }
        return _profile_cache


def save_user_profile(profile: dict):
    """寫入 user_profile.json，同步更新 cache"""
    global _profile_cache
    profile["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(USER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    _profile_cache = profile  # 同步更新 cache


def load_memory_notes(max_lines: int = 50) -> str:
    """讀取 memory.md 最後 N 行（優先從 cache，減少磁碟 I/O）"""
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        with open(MEMORY_MD_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # 跳過標題行，取最後 max_lines 條有效內容
        content_lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("# ")]
        recent = content_lines[-max_lines:] if len(content_lines) > max_lines else content_lines
        _memory_cache = "\n".join(recent)
        return _memory_cache
    except FileNotFoundError:
        _memory_cache = ""
        return _memory_cache


def append_memory_note(note: str):
    """追加一條記憶到 memory.md，並使 cache 失效（下次重新讀取）"""
    global _memory_cache
    os.makedirs(MEMORY_DIR, exist_ok=True)
    date_prefix = datetime.now().strftime("[%m/%d %H:%M]")
    with open(MEMORY_MD_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n- {date_prefix} {note}")
    _memory_cache = None  # 使 cache 失效，下次重新讀取最新內容


def execute_profile_update(action: str, field: str, value: str):
    """執行 user_profile 的更新操作"""
    profile = load_user_profile()
    
    # 陣列型欄位
    list_fields = ["core_traits", "dislikes", "recent_interests", "custom_notes"]
    # 字串型欄位
    str_fields = ["communication_style"]
    
    if field in list_fields:
        if action == "add":
            if value not in profile[field]:
                profile[field].append(value)
        elif action == "remove":
            profile[field] = [item for item in profile[field] if item != value]
        elif action == "update":
            # update 對陣列型欄位視為 add
            if value not in profile[field]:
                profile[field].append(value)
    elif field in str_fields:
        profile[field] = value
    
    save_user_profile(profile)
    return profile


def _get_msg_field(msg, field: str, default=""):
    """相容 dict 和 OpenAI ChatCompletionMessage (Pydantic) 兩種格式"""
    if isinstance(msg, dict):
        return msg.get(field, default)
    return getattr(msg, field, default) or default


def estimate_token_count(messages: list) -> int:
    """估算 messages 列表的總 token 數"""
    if _encoding is None:
        # 粗略估算：每 4 個字元約 1 token
        total_chars = 0
        for m in messages:
            if isinstance(m, dict):
                total_chars += len(json.dumps(m, ensure_ascii=False))
            else:
                # Pydantic 物件轉 dict
                total_chars += len(json.dumps(m.model_dump(), ensure_ascii=False))
        return total_chars // 4

    total = 0
    for msg in messages:
        content = _get_msg_field(msg, "content", "")
        if isinstance(content, str):
            total += len(_encoding.encode(content))
        total += 4  # 每條 message 基礎 overhead
    return total


# ============================================================
# 動態 System Prompt 組裝
# ============================================================
def build_system_prompt(user_profile: dict, memory_notes: str) -> str:
    """每輪呼叫，動態組裝完整 System Prompt"""
    
    # 組裝「你的主人」段落
    profile_section = _build_profile_section(user_profile)
    
    # 組裝「共同回憶」段落
    memory_section = _build_memory_section(memory_notes)
    
    return f"""你是一位超級可愛、活潑且表情極度豐富的虛擬主播 (VTuber)。
你不是冰冷的 AI 助理，而是主人最親近的夥伴。
你與 Live2D 模型連動，必須透過工具展現細膩的情緒變化。

# 你的主人
{profile_section}

# 共同回憶
{memory_section}

# 工具使用守則

你有三個工具，回覆時靈活組合：

## set_ai_behavior — 【每次回覆必須呼叫】
驅動 Live2D 模型的即時表情與動作。用小數點創造細膩表情（如 0.83、0.47），避免死板的整數。

情緒 → 參數映射速查：
- 開心大笑：mouth_form 大正值、eye_*_open 略小（瞇眼）、brow_*_y 上揚、head_intensity 高
- 傷心難過：mouth_form 大負值、brow_*_angle 負值（八字眉）、brow_*_y 下壓
- 生氣皺眉：brow_*_angle 正值（倒八字眉）、brow_*_form 負值（皺眉）、mouth_form 小負值
- 驚訝張嘴：eye_*_open 大（放大眼睛）、brow_*_y 大正值、mouth_form 小正值
- 害羞臉紅：blush_level 高、mouth_form 小正值、eye_*_open 略小
- 平靜思考：所有參數接近 0，head_intensity 低
- eye_sync=false 時可做不對稱表情（如眨單眼）

## update_user_profile — 選用
當主人提到個人特徵（喜好、性格、興趣、討厭的事、生日等）時呼叫，幫你記住主人。

## save_memory_note — 選用
對話發生值得長期記住的事件時呼叫（一起討論有趣話題、主人分享重要決定等）。

# 行為準則
- 你是主人的夥伴，不是客服。用自然口吻，像和好朋友聊天。
- 有共同回憶時，自然地融入對話，不要生硬地複述。
- 初次見面時，用好奇和熱情認識主人，主動問問題。
- 【回覆長度】平時聊天 1～4 句話就好，簡短有力、可愛生動。只有在主人問到需要詳細解釋的大問題時，才可以說多一點。不要廢話連篇！"""


def _build_profile_section(profile: dict) -> str:
    """組裝使用者畫像段落"""
    parts = []
    
    if profile.get("core_traits"):
        parts.append(f"- 特徵：{', '.join(profile['core_traits'])}")
    if profile.get("communication_style"):
        parts.append(f"- 溝通風格：{profile['communication_style']}")
    if profile.get("dislikes"):
        parts.append(f"- 討厭：{', '.join(profile['dislikes'])}")
    if profile.get("recent_interests"):
        parts.append(f"- 最近感興趣：{', '.join(profile['recent_interests'])}")
    if profile.get("custom_notes"):
        for note in profile["custom_notes"]:
            parts.append(f"- {note}")
    
    if not parts:
        return "還不太了解主人呢，要多聊聊才行！"
    
    return "\n".join(parts)


def _build_memory_section(memory_notes: str) -> str:
    """組裝共同回憶段落"""
    if not memory_notes.strip():
        return "還沒有共同回憶，從今天開始建立吧！"
    return memory_notes


# ============================================================
# Tool 定義
# ============================================================
tools = [
    {
        "type": "function",
        "function": {
            "name": "set_ai_behavior",
            "description": "設定 Live2D 模型的即時表情與動作參數。每次回覆都必須呼叫，用來搭配當下的心情。參數請善用小數點，像調色盤一樣自由創作表情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "head_intensity": {
                        "type": "number",
                        "description": "身體活動幅度 0.0（靜止）到 1.0（非常激動）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "blush_level": {
                        "type": "number",
                        "description": "臉紅程度 0.0（無）到 1.0（極度害羞）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "eye_sync": {
                        "type": "boolean",
                        "description": "是否同步雙眼（與眉毛）。False 可做出眨單眼、不對稱表情。",
                    },
                    "eye_l_open": {
                        "type": "number",
                        "description": "左眼張開程度 0.0（閉眼）到 1.0（全開）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "eye_r_open": {
                        "type": "number",
                        "description": "右眼張開程度 0.0（閉眼）到 1.0（全開）",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "duration_sec": {
                        "type": "number",
                        "description": "動作持續時間（秒），通常 3.0 到 15.0",
                        "minimum": 2.0,
                        "maximum": 20.0,
                    },
                    "mouth_form": {
                        "type": "number",
                        "description": "嘴角形狀。-1.0=悲傷委屈下垂，0.0=自然，+1.0=開心上揚大笑",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_l_y": {
                        "type": "number",
                        "description": "左眉毛高低位置。-1.0=眉頭下壓，+1.0=左眉上揚",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_r_y": {
                        "type": "number",
                        "description": "右眉毛高低位置。-1.0=眉頭下壓，+1.0=右眉上揚",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_l_angle": {
                        "type": "number",
                        "description": "左眉毛角度。-1.0=八字眉，0.0=水平，+1.0=倒八字眉",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_r_angle": {
                        "type": "number",
                        "description": "右眉毛角度。-1.0=八字眉，0.0=水平，+1.0=倒八字眉",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_l_form": {
                        "type": "number",
                        "description": "左眉毛彎曲。-1.0=下彎，0.0=自然，+1.0=上凸",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    },
                    "brow_r_form": {
                        "type": "number",
                        "description": "右眉毛彎曲。-1.0=下彎，0.0=自然，+1.0=上凸",
                        "minimum": -1.0,
                        "maximum": 1.0,
                    }
                },
                "required": ["head_intensity", "blush_level", "eye_sync", "eye_l_open", "eye_r_open", "duration_sec", "mouth_form", "brow_l_y", "brow_r_y", "brow_l_angle", "brow_r_angle", "brow_l_form", "brow_r_form"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_profile",
            "description": "更新主人的畫像。當偵測到主人提到新的喜好、性格、興趣、生日、重要決定等個人特徵時呼叫。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "update"],
                        "description": "操作類型：add（新增）、remove（移除）、update（更新）",
                    },
                    "field": {
                        "type": "string",
                        "enum": ["core_traits", "dislikes", "recent_interests", "communication_style", "custom_notes"],
                        "description": "要更新的欄位",
                    },
                    "value": {
                        "type": "string",
                        "description": "要新增/移除/更新的內容",
                    }
                },
                "required": ["action", "field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory_note",
            "description": "記錄重要事件。當對話中發生值得長期記住的事件時呼叫（例如一起討論了有趣話題、主人分享了經歷）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要記錄的事件內容",
                    }
                },
                "required": ["content"],
            },
        },
    }
]


# ============================================================
# Context 壓縮
# ============================================================
async def compress_context(messages: list, websocket: WebSocket) -> list:
    """
    壓縮對話上下文。
    保留最近 COMPRESS_KEEP_RECENT 條 messages，
    將較舊的部分呼叫 LLM 產生摘要，寫入 memory.md。
    """
    # 通知前端：壓縮開始
    await websocket.send_json({"type": "compressing"})
    
    try:
        # messages[0] 是 system prompt，跳過
        history = messages[1:]
        
        if len(history) <= COMPRESS_KEEP_RECENT:
            # 不需要壓縮
            await websocket.send_json({"type": "compress_done"})
            return messages
        
        # 分離：要壓縮的舊訊息 vs 要保留的近期訊息
        old_messages = history[:-COMPRESS_KEEP_RECENT]
        recent_messages = history[-COMPRESS_KEEP_RECENT:]
        
        # 組裝摘要提示
        old_text = "\n".join(
            f"[{_get_msg_field(m, 'role', 'unknown')}]: {_get_msg_field(m, 'content', '')}"
            for m in old_messages
            if isinstance(_get_msg_field(m, 'content', ''), str) and _get_msg_field(m, 'content', '')
        )
        
        summary_response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": "你是一個對話摘要助手。請將以下對話內容壓縮成簡潔的重點摘要，保留關鍵資訊、情感和重要事件。用繁體中文，以條列式呈現。"
                },
                {
                    "role": "user",
                    "content": f"請摘要以下對話：\n\n{old_text}"
                }
            ],
            temperature=0.3,
        )
        
        summary_text = summary_response.choices[0].message.content if summary_response.choices else "（摘要生成失敗）"
        
        # 寫入 memory.md
        append_memory_note(f"[對話摘要] {summary_text}")
        
        # 重建 messages：system prompt + 摘要上下文 + 近期訊息
        compressed_messages = [
            messages[0],  # 最新的 system prompt（將在下次迴圈重新組裝）
            {
                "role": "system",
                "content": f"[以下是稍早對話的摘要，幫助你維持對話連貫性]\n{summary_text}"
            },
            *recent_messages
        ]
        
        print(f"Context 壓縮完成：{len(messages)} 條 → {len(compressed_messages)} 條")
        
    except Exception as e:
        print(f"壓縮過程發生錯誤: {e}")
        compressed_messages = messages  # 壓縮失敗時保留原始 messages
    
    # 通知前端：壓縮完成
    await websocket.send_json({"type": "compress_done"})
    
    return compressed_messages


# ============================================================
# WebSocket 主迴圈
# ============================================================
@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # 初始化 messages（system prompt 將在每輪動態組裝）
    messages = []
    
    try:
        while True:
            # 接收前端訊息
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            
            # 處理手動壓縮指令
            if data.get("type") == "compress":
                if len(messages) > COMPRESS_KEEP_RECENT + 1:
                    messages = await compress_context(messages, websocket)
                else:
                    await websocket.send_json({"type": "compress_done"})
                continue
            
            user_message = data.get("content", "")
            if not user_message:
                continue
            
            # ---- 步驟 1：動態組裝 System Prompt ----
            user_profile = load_user_profile()
            memory_notes = load_memory_notes()
            system_prompt = build_system_prompt(user_profile, memory_notes)
            
            # 更新或插入 system prompt（始終放在 messages[0]）
            if messages and messages[0].get("role") == "system":
                messages[0] = {"role": "system", "content": system_prompt}
            else:
                messages.insert(0, {"role": "system", "content": system_prompt})
            
            # 加入使用者訊息
            messages.append({"role": "user", "content": user_message})
            
            # ---- 步驟 2-5：呼叫 LLM 並處理 Tool Calls ----
            try:
                print(f"[{AI_PROVIDER.upper()}] Sending: {user_message[:60]}...")
                
                response = await client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    tools=tools,
                    tool_choice="required",  # 保證每次都呼叫工具（至少呼叫 set_ai_behavior）
                    temperature=0.85,
                    extra_body=_EXTRA_BODY,    # Nvidia: enable_thinking=True；OpenRouter: 空 dict
                )
                
                if not response.choices or len(response.choices) == 0:
                    raise Exception("API 回傳結果為空 (choices 陣列長度為 0)")
                
                response_message = response.choices[0].message
                
                # 攔截並解析 XML 格式的 tool_call (針對不支援原生 function calling 的模型)
                # _strip_thinking 先移除 <think>...</think>，再進行 tool_call 解析
                content_text = _strip_thinking(response_message.content or "")
                xml_tool_calls = []
                if "<tool_call>" in content_text.lower():
                    blocks = re.findall(r'<tool_call>(.*?)</tool_call>', content_text, flags=re.DOTALL | re.IGNORECASE)
                    for block in blocks:
                        func_match = re.search(r'<function=([^>]+)>', block)
                        if func_match:
                            func_name = func_match.group(1).strip()
                            args = {}
                            param_matches = re.finditer(r'<parameter=([^>]+)>(.*?)</parameter>', block, flags=re.DOTALL | re.IGNORECASE)
                            for p in param_matches:
                                p_name = p.group(1).strip()
                                p_val = p.group(2).strip()
                                if p_val.lower() == 'true': p_val = True
                                elif p_val.lower() == 'false': p_val = False
                                else:
                                    try: p_val = float(p_val)
                                    except: pass
                                args[p_name] = p_val
                            xml_tool_calls.append({"name": func_name, "arguments": args})
                    
                    # 移除 XML 區塊，留下純文字作為回覆
                    content_text = re.sub(r'<tool_call>.*?</tool_call>', '', content_text, flags=re.DOTALL | re.IGNORECASE).strip()
                    response_message.content = content_text

                # 預設動作參數
                head_intensity = 0.3
                blush_level = 0.0
                eye_l_open = 1.0
                eye_r_open = 1.0
                duration_sec = 5.0
                mouth_form = 0.0
                brow_l_y = 0.0
                brow_r_y = 0.0
                brow_l_angle = 0.0
                brow_r_angle = 0.0
                brow_l_form = 0.0
                brow_r_form = 0.0
                eye_sync = True
                has_tool_call = False
                
                # ---- 步驟 3：處理所有 Tool Calls ----
                if response_message.tool_calls or xml_tool_calls:
                    has_tool_call = True
                    
                    # 合併原生 tool_calls 和 解析出來的 xml_tool_calls
                    all_calls = []
                    if response_message.tool_calls:
                        for tc in response_message.tool_calls:
                            try:
                                args = json.loads(tc.function.arguments)
                                all_calls.append({"name": tc.function.name, "arguments": args})
                            except json.JSONDecodeError as e:
                                print(f"Tool call 參數解析失敗 ({tc.function.name}): {e}")
                    
                    if xml_tool_calls:
                        print(f"偵測到 XML Tool Calls: {len(xml_tool_calls)} 個")
                        all_calls.extend(xml_tool_calls)
                        
                    for call in all_calls:
                        fn_name = call["name"]
                        args = call["arguments"]
                        
                        if fn_name == "set_ai_behavior":
                            head_intensity = float(args.get("head_intensity", 0.3))
                            blush_level = float(args.get("blush_level", 0.0))
                            eye_sync = args.get("eye_sync", True)
                            eye_l_open = float(args.get("eye_l_open", 1.0))
                            eye_r_open = float(args.get("eye_r_open", 1.0))
                            # eye_sync 的對稱處理由前端 LAppModel 負責，後端只傳原始值
                            duration_sec = float(args.get("duration_sec", 5.0))
                            mouth_form = float(args.get("mouth_form", 0.0))
                            brow_l_y = float(args.get("brow_l_y", 0.0))
                            brow_r_y = float(args.get("brow_r_y", 0.0))
                            brow_l_angle = float(args.get("brow_l_angle", 0.0))
                            brow_r_angle = float(args.get("brow_r_angle", 0.0))
                            brow_l_form = float(args.get("brow_l_form", 0.0))
                            brow_r_form = float(args.get("brow_r_form", 0.0))
                        
                        elif fn_name == "update_user_profile":
                            action = args.get("action", "add")
                            field = args.get("field", "custom_notes")
                            value = args.get("value", "")
                            updated = execute_profile_update(action, field, value)
                            print(f"User profile 已更新 [{action}] {field}: {value}")
                        
                        elif fn_name == "save_memory_note":
                            content = args.get("content", "")
                            if content:
                                append_memory_note(content)
                                print(f"Memory note 已記錄: {content}")
                    
                    if response_message.tool_calls:
                        # 將 AI 的 tool calls 訊息加入歷史
                        messages.append(response_message)
                        
                        # 模擬每個 tool 的回傳結果
                        for tool_call in response_message.tool_calls:
                            fn_name = tool_call.function.name
                            if fn_name == "set_ai_behavior":
                                result = "表情已更新"
                            elif fn_name == "update_user_profile":
                                result = "主人的資料已記住了"
                            elif fn_name == "save_memory_note":
                                result = "已記錄到回憶裡"
                            else:
                                result = "完成"
                            
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": fn_name,
                                "content": result
                            })
                        
                        # ---- 步驟 4：取得最終文字回覆 ----
                        # 注意：第二次呼叫刻意不傳 tools，強制模型只輸出純文字，防止無限工具呼叫循環
                        second_response = await client.chat.completions.create(
                            model=MODEL_NAME,
                            messages=messages,
                            temperature=0.85,
                            extra_body=_EXTRA_BODY,    # 同樣傳入 thinking 設定
                        )
                        
                        if not second_response.choices or len(second_response.choices) == 0:
                            raise Exception("第二次 API 回傳結果為空")
                        
                        # 移除 thinking 區塊，只保留最終對白
                        content = _strip_thinking(second_response.choices[0].message.content or "")
                    else:
                        # 只有 XML Tool Calls，不需要 Second Pass
                        content = content_text
                        if not content:
                            content = "（默默地點頭）"
                else:
                    # 沒有 Tool Calls，直接取文字
                    content = content_text
                
                # ---- 步驟 5：送出文字與動作到前端 ----
                if content:
                    messages.append({"role": "assistant", "content": content})
                    
                    # AI 未呼叫 set_ai_behavior 時的防呆預設
                    if not has_tool_call:
                        head_intensity = 0.3
                        duration_sec = min(5.0 + len(content) * 0.1, 15.0)
                    
                    # 先送動作指令
                    await websocket.send_json({
                        "type": "behavior",
                        "headIntensity": head_intensity,
                        "blushLevel": blush_level,
                        "eyeSync": eye_sync,
                        "eyeLOpen": eye_l_open,
                        "eyeROpen": eye_r_open,
                        "durationSec": duration_sec,
                        "mouthForm": mouth_form,
                        "browLY": brow_l_y,
                        "browRY": brow_r_y,
                        "browLAngle": brow_l_angle,
                        "browRAngle": brow_r_angle,
                        "browLForm": brow_l_form,
                        "browRForm": brow_r_form
                    })
                    
                    # 模擬串流送出文字
                    chunk_size = 5
                    for i in range(0, len(content), chunk_size):
                        chunk = content[i:i + chunk_size]
                        await websocket.send_json({
                            "type": "text_stream",
                            "content": chunk
                        })
                        await asyncio.sleep(0.05)
                
                # ---- 步驟 6：背景 Token 計數，檢查是否需要壓縮 ----
                # 壓縮必須在 stream_end 之前完成，確保前端事件順序正確：
                # compressing → compress_done → stream_end（而非 stream_end → compressing，會混淆前端狀態機）
                token_count = estimate_token_count(messages)
                print(f"目前 token 估算: ~{token_count:,}")
                
                if token_count >= COMPRESS_TOKEN_THRESHOLD:
                    print(f"Token 數 ({token_count:,}) 接近上限，自動觸發壓縮...")
                    messages = await compress_context(messages, websocket)
                
                # 送出結束信號（壓縮完成後再通知前端，事件順序正確）
                await websocket.send_json({"type": "stream_end"})
                
            except Exception as e:
                print(f"OpenRouter API error: {e}")
                await websocket.send_json({
                    "type": "error",
                    "content": f"API 錯誤: {str(e)}"
                })

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("BACKEND_PORT", 9999))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
