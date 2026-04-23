"""
Chat 服務：LLM 串流、Context 壓縮、XML Tool Call 解析、Token 計數、TTS 合成轉發。
"""
import re
import json

from fastapi import WebSocket

from core.config import AI_PROVIDER, MODEL_NAME, COMPRESS_KEEP_RECENT
from core.utils import strip_thinking, get_msg_field
from infrastructure.ai_client import chat_create_with_fallback, EXTRA_BODY, NO_THINKING_EXTRA_BODY
from infrastructure.memory_store import append_memory_note
from domain.jpaf import extract_emotion_state, extract_jpaf_state, strip_jpaf_tags

import tiktoken

# tiktoken 編碼器（使用 cl100k_base 作為通用估算）
try:
    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoding = None

# XML tool call regex
_RE_XML_TOOL_BLOCK = re.compile(
    r"<tool_call>(.*?)</tool_call>", re.DOTALL | re.IGNORECASE
)
_RE_XML_FUNC_NAME = re.compile(r"<function=([^>]+)>")
_RE_XML_PARAM = re.compile(
    r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL | re.IGNORECASE
)


# ============================================================
# Token 計數
# ============================================================
def estimate_token_count(messages: list) -> int:
    """估算 messages 列表的總 token 數"""
    if _encoding is None:
        # 粗略估算：每 4 個字元約 1 token
        total_chars = 0
        for m in messages:
            if isinstance(m, dict):
                total_chars += len(json.dumps(m, ensure_ascii=False))
            else:
                total_chars += len(json.dumps(m.model_dump(), ensure_ascii=False))
        return total_chars // 4

    total = 0
    for msg in messages:
        content = get_msg_field(msg, "content", "")
        if isinstance(content, str):
            total += len(_encoding.encode(content))
        total += 4  # 每條 message 基礎 overhead
    return total


# ============================================================
# XML Tool Call 解析
# ============================================================
def parse_xml_tool_calls(content_text: str) -> tuple[list[dict], str]:
    """
    解析 content_text 中的 XML 格式 tool_call 區塊。
    回傳 (tool_calls_list, cleaned_text)。
    針對不支援原生 function calling 的模型。
    """
    if "<tool_call>" not in content_text.lower():
        return [], content_text

    xml_tool_calls: list[dict] = []
    for block_match in _RE_XML_TOOL_BLOCK.finditer(content_text):
        block = block_match.group(1)
        func_match = _RE_XML_FUNC_NAME.search(block)
        if not func_match:
            continue
        func_name = func_match.group(1).strip()
        args: dict = {}
        for p in _RE_XML_PARAM.finditer(block):
            p_name = p.group(1).strip()
            p_val: str | bool | float = p.group(2).strip()
            if isinstance(p_val, str) and p_val.lower() == "true":
                p_val = True
            elif isinstance(p_val, str) and p_val.lower() == "false":
                p_val = False
            else:
                try:
                    p_val = float(p_val)  # type: ignore[assignment]
                except (ValueError, TypeError):
                    pass
            args[p_name] = p_val
        xml_tool_calls.append({"name": func_name, "arguments": args})

    # 移除 XML 區塊，留下純文字作為回覆
    cleaned = re.sub(
        r"<tool_call>.*?</tool_call>",
        "",
        content_text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()

    return xml_tool_calls, cleaned


# ============================================================
# LLM 串流
# ============================================================
async def stream_final_text(messages: list, websocket: WebSocket) -> str:
    """使用 OpenAI 相容串流，將 token 即時轉發給前端。"""
    stream = await chat_create_with_fallback(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.85,
        extra_body=EXTRA_BODY,
        stream=True,
    )

    chunks: list[str] = []
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            chunks.append(piece)
            await websocket.send_json({"type": "text_stream", "content": piece})

    return strip_thinking("".join(chunks).strip())


# ============================================================
# Agent A：JPAF 人格對話串流
# ============================================================
async def stream_agent_a(messages: list, websocket: WebSocket) -> tuple[str, dict | None, dict | None]:
    """
    Agent A 串流呼叫：產生角色對話 + JPAF 狀態。
    回傳 (cleaned_text, jpaf_state_dict_or_None, emotion_state_dict_or_None)。
    串流時即時過濾 <thinking>、<jpaf_state>、<emotion_state> 標籤，只送對話文字到前端。
    """
    stream = await chat_create_with_fallback(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.85,
        extra_body=EXTRA_BODY,
        stream=True,
    )

    all_chunks: list[str] = []       # 完整原始文字（含標籤）
    visible_buffer: list[str] = []   # 可能需要送出的文字暫存
    inside_hidden_tag: bool = False   # 是否在隱藏標籤內
    hidden_tag_name: str = ""         # 當前隱藏標籤名稱

    _HIDDEN_OPEN_TAGS = {"<thinking>", "<think>", "<thought>", "<jpaf_state>", "<emotion_state>"}
    _HIDDEN_CLOSE_MAP = {
        "thinking": "</thinking>",
        "think": "</think>",
        "thought": "</thought>",
        "jpaf_state": "</jpaf_state>",
        "emotion_state": "</emotion_state>",
    }

    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if not piece:
            continue

        all_chunks.append(piece)

        # 簡易狀態機：偵測隱藏標籤的開/關
        if inside_hidden_tag:
            # 在隱藏標籤內，檢查是否結束
            close_tag = _HIDDEN_CLOSE_MAP.get(hidden_tag_name, "")
            # 不送出任何內容
            # 用累積的全文來檢查結束標籤
            full_so_far = "".join(all_chunks)
            if close_tag and close_tag in full_so_far.split(f"<{hidden_tag_name}>")[-1]:
                inside_hidden_tag = False
                hidden_tag_name = ""
        else:
            # 不在隱藏標籤內，檢查是否有開始標籤
            combined = "".join(visible_buffer) + piece
            tag_found = False
            for open_tag in _HIDDEN_OPEN_TAGS:
                if open_tag in combined:
                    # 送出標籤之前的文字
                    before = combined.split(open_tag)[0]
                    if before.strip():
                        await websocket.send_json(
                            {"type": "text_stream", "content": before}
                        )
                    visible_buffer = []
                    inside_hidden_tag = True
                    hidden_tag_name = open_tag[1:-1]  # 去掉 < >
                    tag_found = True
                    break

            if not tag_found:
                # 檢查 piece 是否可能是標籤的開頭片段（如 "<thin"）
                if "<" in piece and not piece.endswith(">"):
                    visible_buffer.append(piece)
                else:
                    # 安全地送出
                    if visible_buffer:
                        buffered = "".join(visible_buffer)
                        visible_buffer = []
                        await websocket.send_json(
                            {"type": "text_stream", "content": buffered + piece}
                        )
                    else:
                        await websocket.send_json(
                            {"type": "text_stream", "content": piece}
                        )

    # 送出 buffer 中剩餘的文字
    if visible_buffer and not inside_hidden_tag:
        remaining = "".join(visible_buffer)
        if remaining.strip():
            await websocket.send_json(
                {"type": "text_stream", "content": remaining}
            )

    # 從完整原始文字提取 jpaf_state 和乾淨對話
    raw_text = "".join(all_chunks).strip()
    jpaf_state = extract_jpaf_state(raw_text)
    emotion_state = extract_emotion_state(raw_text)
    clean_text = strip_jpaf_tags(strip_thinking(raw_text))

    return clean_text, jpaf_state, emotion_state


# ============================================================
# Agent A：JPAF Buffer 模式（不即時串流文字到前端）
# ============================================================
async def collect_agent_a(messages: list) -> tuple[str, dict | None, dict | None]:
    """
    Agent A buffer 模式：收集完整回覆後解析 JPAF 狀態，不即時送出文字到前端。
    用於等 A+B 都完成後再一起送的同步模式。
    回傳 (cleaned_text, jpaf_state_dict_or_None, emotion_state_dict_or_None)。
    """
    stream = await chat_create_with_fallback(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.85,
        extra_body=EXTRA_BODY,
        stream=True,
    )

    chunks: list[str] = []
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            chunks.append(piece)

    raw_text = "".join(chunks).strip()
    jpaf_state = extract_jpaf_state(raw_text)
    emotion_state = extract_emotion_state(raw_text)
    clean_text = strip_jpaf_tags(strip_thinking(raw_text))

    return clean_text, jpaf_state, emotion_state


# ============================================================
# Agent B-1：Live2D 表情控制
# ============================================================
async def call_live2d_agent(messages: list, model_name: str = "Hiyori") -> object:
    """
    Live2D Agent 非串流呼叫：決定表情參數。
    依 model_name 載入對應的 Live2D 工具清單。
    回傳原始 API response。
    """
    from domain.tools import get_live2d_tools

    request_kwargs = {
        "model": MODEL_NAME,
        "messages": messages,
        "tools": get_live2d_tools(model_name),
        "tool_choice": "required",
        "temperature": 0.7,
        "extra_body": NO_THINKING_EXTRA_BODY,
        "max_tokens": 2000,
    }
    if AI_PROVIDER == "qwen":
        request_kwargs["parallel_tool_calls"] = True

    response = await chat_create_with_fallback(
        **request_kwargs,
    )
    return response


# ============================================================
# Agent B-2：記憶管理
# ============================================================
async def call_memory_agent(messages: list) -> object:
    """
    Memory Agent 非串流呼叫：判斷是否需要記憶操作。
    回傳原始 API response。
    """
    from domain.tools import memory_tools

    response = await chat_create_with_fallback(
        model=MODEL_NAME,
        messages=messages,
        tools=memory_tools,
        tool_choice="auto",
        temperature=0.3,
        extra_body=NO_THINKING_EXTRA_BODY,
        max_tokens=400,
    )
    return response


# 向後相容別名
async def call_agent_b(messages: list) -> object:
    """已棄用，保留向後相容。請改用 call_live2d_agent / call_memory_agent。"""
    from domain.tools import tools

    response = await chat_create_with_fallback(
        model=MODEL_NAME,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        temperature=0.5,
        extra_body=EXTRA_BODY,
        max_tokens=512,
    )
    return response


# ============================================================
# TTS 合成轉發
# ============================================================
async def synthesize_and_send_voice(
    websocket: WebSocket, text: str, speaking_rate: float
) -> None:
    """背景執行 TTS，避免阻塞文字串流完成事件。"""
    from tts_service import get_tts_service  # 延遲匯入，避免啟動時強制 TTS 初始化

    tts_service = get_tts_service()
    if not tts_service.is_enabled():
        return

    try:
        tts_result = await tts_service.synthesize(
            text=text, speaking_rate=speaking_rate
        )
        if tts_result:
            await websocket.send_json(
                {
                    "type": "voice",
                    "audio": tts_result["audio_base64"],
                    "durationMs": tts_result["duration_ms"],
                    "format": tts_result["format"],
                }
            )
    except Exception as tts_error:
        print(f"[TTS] 合成錯誤（不影響文字回覆）: {tts_error}")


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
        # 一般情況下 messages[0] 是 system prompt；若不是，則完整視為 history
        has_system_prompt = (
            bool(messages)
            and isinstance(messages[0], dict)
            and messages[0].get("role") == "system"
        )
        history = messages[1:] if has_system_prompt else messages

        if len(history) <= COMPRESS_KEEP_RECENT:
            # 不需要壓縮
            await websocket.send_json({"type": "compress_done"})
            return messages

        # 分離：要壓縮的舊訊息 vs 要保留的近期訊息
        old_messages = history[:-COMPRESS_KEEP_RECENT]
        recent_messages = history[-COMPRESS_KEEP_RECENT:]

        # 組裝摘要提示
        old_text = "\n".join(
            f"[{get_msg_field(m, 'role', 'unknown')}]: {get_msg_field(m, 'content', '')}"
            for m in old_messages
            if isinstance(get_msg_field(m, "content", ""), str)
            and get_msg_field(m, "content", "")
        )

        summary_response = await chat_create_with_fallback(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": "你是一個對話摘要助手。請將以下對話內容壓縮成簡潔的重點摘要，保留關鍵資訊、情感和重要事件。同時記錄對話中角色人格的情緒模式變化（如哪些認知功能被頻繁啟用、角色語氣是否有明顯轉變）。用繁體中文，以條列式呈現。",
                },
                {"role": "user", "content": f"請摘要以下對話：\n\n{old_text}"},
            ],
            temperature=0.3,
        )

        summary_text = (
            summary_response.choices[0].message.content
            if summary_response.choices
            else "（摘要生成失敗）"
        )

        # 寫入 memory.md
        append_memory_note(f"[對話摘要] {summary_text}")

        # 重建 messages：system prompt + 摘要上下文 + 近期訊息
        compressed_messages: list = []
        if has_system_prompt:
            compressed_messages.append(messages[0])  # 最新的 system prompt

        compressed_messages.extend(
            [
                {
                    "role": "system",
                    "content": f"[以下是稍早對話的摘要，幫助你維持對話連貫性]\n{summary_text}",
                },
                *recent_messages,
            ]
        )

        print(f"Context 壓縮完成：{len(messages)} 條 → {len(compressed_messages)} 條")

    except Exception as e:
        print(f"壓縮過程發生錯誤: {e}")
        compressed_messages = messages  # 壓縮失敗時保留原始 messages

    # 通知前端：壓縮完成
    await websocket.send_json({"type": "compress_done"})

    return compressed_messages
