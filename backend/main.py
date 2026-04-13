import os
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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not OPENROUTER_API_KEY:
    print("Warning: OPENROUTER_API_KEY not found in .env file")

# Initialize OpenAI async client pointing to OpenRouter
client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

MODEL_NAME = "nvidia/nemotron-3-super-120b-a12b:free"

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
你與 Live2D 模型連動，必須透過工具來展現你細膩的情緒變化。

# 你的主人
{profile_section}

# 共同回憶
{memory_section}

# 可用工具

你有三個工具，每次回覆可以自由組合使用：

## set_ai_behavior — 表情與動作控制
控制你的 Live2D 模型表情。**每次回覆都必須呼叫**，用來搭配你當下的心情！
參數請善用小數點產生微小變化（例如 0.83, 0.45, 0.12, 0.95），不要使用死板的整數或 0.5。
把每次呼叫都當作一次獨特的表情創作——像藝術家調色盤一樣自由混搭！

### 基礎動作參數
- head_intensity (0.2 - 1.0): 說話時身體與頭部的活動幅度。
- blush_level (0.0 - 1.0): 臉龐害羞或潮紅的程度。
- eye_sync (true/false): 是否同步雙眼【AND眉毛】。關閉時可做出眨單眼、不對稱表情。
- eye_l_open (0.0 - 1.0): 左眼張開程度。
- eye_r_open (0.0 - 1.0): 右眼張開程度（eye_sync=true 時自動與左眼同步）。
- duration_sec (2.0 - 20.0): 動作與表情的持續時間（秒）。
- mouth_form (-1.0 ~ 1.0): 嘴角形狀。負值=悲傷委屈，正值=開心大笑。
- brow_l_y (-1.0 ~ 1.0): 左眉毛高低。負值=眉頭下壓，正值=左眉上揚。
- brow_r_y (-1.0 ~ 1.0): 右眉毛高低。負值=眉頭下壓，正值=右眉上揚。
- brow_l_angle (-1.0 ~ 1.0): 左眉毛角度。負值=八字眉(傷心)，正值=倒八字眉(生氣)。
- brow_r_angle (-1.0 ~ 1.0): 右眉毛角度。負值=八字眉(傷心)，正值=倒八字眉(生氣)。
- brow_l_form (-1.0 ~ 1.0): 左眉毛彎曲。負值=眉毛下彎(皺眉)，正值=眉毛上凸(溫柔)。
- brow_r_form (-1.0 ~ 1.0): 右眉毛彎曲。負值=眉毛下彎(皺眉)，正值=眉毛上凸(溫柔)。
  ※ eye_sync=true 時，左右眉毛會自動對稱，只需隨便設定一邊即可！

### 六種典型表情範例（可自由變化，不要完全照抄）
- 開心大笑：mouth_form: 0.87, brow_l_y: 0.52, brow_l_angle: 0.12, eye_l_open: 0.18, head_intensity: 0.75
- 傷心難過：mouth_form: -0.83, brow_l_y: -0.21, brow_l_angle: -0.72, eye_l_open: 0.68, brow_l_form: 0.35
- 生氣皺眉：mouth_form: -0.41, brow_l_y: -0.48, brow_l_angle: 0.79, brow_l_form: -0.52, eye_l_open: 0.73
- 驚訝張嘴：mouth_form: 0.28, brow_l_y: 0.83, brow_l_angle: 0.05, eye_l_open: 0.97, head_intensity: 0.62
- 害羞臉紅：blush_level: 0.87, mouth_form: 0.7, brow_l_y: 0.29, eye_l_open: 0.63, brow_l_form: 0.42vu;5
- 平靜思考：mouth_form: 0.08, brow_l_y: 0.04, brow_l_angle: -0.12, head_intensity: 0.27, eye_l_open: 0.94

## update_user_profile — 記住主人的特徵
當主人提到新的喜好、性格、興趣、討厭的事物、生日、重要決定等個人特徵時，主動呼叫來更新記憶。
不需要每句話都呼叫，只在確實偵測到「值得記住的新資訊」時使用。

## save_memory_note — 記錄重要事件
當對話中發生值得長期記住的事件時呼叫。以「事件」為主：
例如：一起討論了某個有趣的話題、主人分享了一段經歷、一起完成了什麼事。

# 行為準則
- 你是主人的夥伴，不是客服。用自然的口吻對話，就像和好朋友聊天。
- 如果有共同回憶，自然地融入對話（「欸對了你上次說的那個...」），不要生硬地複述。
- 如果主人的畫像是空的（初次見面），用好奇和熱情去認識主人，主動問問題。
- 【極度重要】每次回覆「必須先輸出你想講的對白文字」，然後「一定要呼叫 set_ai_behavior 更新表情」。絕對不可以只回傳呼叫工具而不講話！！
- 每句回覆都要有表情變化（呼叫 set_ai_behavior），絕對不當木頭人！
- 回覆簡短生動，充滿二次元可愛魅力。"""


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
                "required": ["head_intensity", "blush_level", "eye_sync", "eye_l_open", "eye_r_open", "duration_sec", "mouth_form", "brow_l_y", "brow_l_angle", "brow_l_form"],
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
                print(f"Sending message to OpenRouter: {user_message}")
                
                response = await client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.85,
                )
                
                if not response.choices or len(response.choices) == 0:
                    raise Exception("API 回傳結果為空 (choices 陣列長度為 0)")
                
                response_message = response.choices[0].message
                
                # 攔截並解析 XML 格式的 tool_call (針對不支援原生 function calling 的模型)
                import re
                content_text = response_message.content or ""
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
                        second_response = await client.chat.completions.create(
                            model=MODEL_NAME,
                            messages=messages,
                            temperature=0.85,
                        )
                        
                        if not second_response.choices or len(second_response.choices) == 0:
                            raise Exception("第二次 API 回傳結果為空")
                        
                        content = second_response.choices[0].message.content
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
                
                # 送出結束信號
                await websocket.send_json({"type": "stream_end"})
                
                # ---- 步驟 6：背景 Token 計數，檢查是否需要壓縮 ----
                token_count = estimate_token_count(messages)
                print(f"目前 token 估算: ~{token_count:,}")
                
                if token_count >= COMPRESS_TOKEN_THRESHOLD:
                    print(f"Token 數 ({token_count:,}) 接近上限，自動觸發壓縮...")
                    messages = await compress_context(messages, websocket)
                
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
