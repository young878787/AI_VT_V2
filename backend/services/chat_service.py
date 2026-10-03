"""
Chat 服務：LLM 串流、Context 壓縮、Token 計數、TTS 合成轉發。
"""
import re
import json
import time
from collections.abc import Awaitable, Callable

from fastapi import WebSocket

from core.config import CHAT_MODEL_NAME, CHAT_PROVIDER, CHAT_CONTEXT_TOKEN_BUDGET, COMPRESS_KEEP_RECENT
from core.utils import strip_thinking, get_msg_field
from infrastructure.ai_client import chat_create_with_fallback, no_thinking_extra_body
from infrastructure.memory_store import save_session_summary
from core.prompt_logger import trace

import tiktoken

# tiktoken 編碼器（使用 cl100k_base 作為通用估算）
try:
    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoding = None

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


def build_chat_context(prompt: str, history: list[dict], user_text: str, budget: int = CHAT_CONTEXT_TOKEN_BUDGET) -> list[dict]:
    """保留本輪輸入，從最新已完成對話往前納入，固定總 token 預算。"""
    system = {"role": "system", "content": prompt}
    user = {"role": "user", "content": user_text}
    if estimate_token_count([system]) > budget // 2:
        low, high = 0, len(prompt) // 2
        while low < high:
            middle = (low + high + 1) // 2
            system["content"] = prompt[:middle] + "\n…\n" + prompt[-middle:]
            if estimate_token_count([system]) <= budget // 2:
                low = middle
            else:
                high = middle - 1
        system["content"] = prompt[:low] + "\n…\n" + prompt[-low:] if low else ""
    if estimate_token_count([system, user]) > budget:
        low, high = 0, len(user_text)
        while low < high:
            middle = (low + high + 1) // 2
            user["content"] = user_text[:middle]
            if estimate_token_count([system, user]) <= budget:
                low = middle
            else:
                high = middle - 1
        user["content"] = user_text[:low]
    selected: list[dict] = []
    for item in reversed(history[-16:]):
        if item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
            continue
        dialogue_item = {"role": item["role"], "content": item["content"]}
        if estimate_token_count([system, dialogue_item, *selected, user]) > budget:
            break
        selected.insert(0, dialogue_item)
    return [system, *selected, user]


def retained_prompt_ranges(original: str, actual: str) -> list[list[int]]:
    """對應 build_chat_context 的裁切，保留原始 system 字元來源區間。"""
    if original == actual:
        return [[0, len(original)]]
    if not actual:
        return []
    marker = "\n…\n"
    size = (len(actual) - len(marker)) // 2
    if size > 0 and actual == original[:size] + marker + original[-size:]:
        return [[0, size], [len(original) - size, len(original)]]
    return []


# ============================================================
# LLM 串流
# ============================================================
async def stream_final_text(messages: list, websocket: WebSocket) -> str:
    """使用 OpenAI 相容串流，將 token 即時轉發給前端。"""
    stream = await chat_create_with_fallback(
        model=CHAT_MODEL_NAME,
        role="chat",
        messages=messages,
        temperature=0.85,
        extra_body=no_thinking_extra_body(CHAT_PROVIDER),
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
# AI Chat：純文字回覆
# ============================================================
class _VisibleTextFilter:
    """跨串流 chunk 移除 thinking 與舊狀態標籤。"""

    def __init__(self) -> None:
        self.pending = ""
        self.hidden: str | None = None

    def feed(self, piece: str) -> str:
        self.pending += piece
        visible: list[str] = []
        while self.pending:
            marker = self.pending.find("<")
            if marker < 0:
                if self.hidden is None:
                    visible.append(self.pending)
                self.pending = ""
                break
            if marker:
                if self.hidden is None:
                    visible.append(self.pending[:marker])
                self.pending = self.pending[marker:]
            end = self.pending.find(">")
            if end < 0:
                break
            tag = self.pending[:end + 1]
            self.pending = self.pending[end + 1:]
            opening = re.fullmatch(r"<([a-z_]+_state|think)>", tag, re.I)
            closing = re.fullmatch(r"</([a-z_]+_state|think)>", tag, re.I)
            if opening and self.hidden is None:
                self.hidden = opening.group(1).lower()
            elif closing and self.hidden == closing.group(1).lower():
                self.hidden = None
            elif self.hidden is None:
                visible.append(tag)
        return "".join(visible)

    def finish(self) -> str:
        if self.hidden is not None:
            return ""
        tail, self.pending = self.pending, ""
        return tail if "<" not in tail else ""


async def stream_agent_a(messages: list, send_chunk: Callable[[str], Awaitable[None]]) -> str:
    """安全地逐段轉送 Chat 可見文字，完整結果供歷史與 TTS 使用。"""
    started = time.monotonic()
    diagnostic = {"configured_model": CHAT_MODEL_NAME, "model": None, "usage": None,
                  "finish_reason": None, "error": None}
    stream = None
    chunks: list[str] = []
    visible_filter = _VisibleTextFilter()
    try:
        stream = await chat_create_with_fallback(
            model=CHAT_MODEL_NAME, role="chat", messages=messages,
            temperature=0.85, extra_body=no_thinking_extra_body(CHAT_PROVIDER),
            max_tokens=400, stream=True,
        )
        async for chunk in stream:
            diagnostic["model"] = getattr(chunk, "model", None) or diagnostic["model"]
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                diagnostic["usage"] = usage.model_dump()
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            diagnostic["finish_reason"] = getattr(choice, "finish_reason", None) or diagnostic["finish_reason"]
            piece = getattr(choice.delta, "content", None)
            if piece:
                visible = visible_filter.feed(piece)
                if visible:
                    chunks.append(visible)
                    await send_chunk(visible)
        tail = visible_filter.finish()
        if tail:
            chunks.append(tail)
            await send_chunk(tail)
        return "".join(chunks).strip()
    except BaseException as exc:
        diagnostic["error"] = type(exc).__name__
        raise
    finally:
        diagnostic["duration_sec"] = round(time.monotonic() - started, 4)
        diagnostic["output_token_estimate"] = estimate_token_count([
            {"role": "assistant", "content": "".join(chunks)}])
        trace("chat", diagnostic)
        if stream is not None and hasattr(stream, "close"):
            await stream.close()


async def collect_agent_a(messages: list) -> str:
    """保留既有收集介面供隔離測試與非串流呼叫。"""
    async def discard(_piece: str) -> None:
        pass

    return await stream_agent_a(messages, discard)


# ============================================================
# TTS 合成轉發
# ============================================================
async def synthesize_and_send_voice(
    websocket: WebSocket, text: str, speaking_rate: float, turn_id: str | None = None, send_func=None
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
            payload = {
                    "type": "voice",
                    "audio": tts_result["audio_base64"],
                    "durationMs": tts_result["duration_ms"],
                    "format": tts_result["format"],
                }
            if turn_id:
                payload["turn_id"] = turn_id
            await (send_func or websocket.send_json)(payload)
    except Exception as tts_error:
        print(f"[TTS] 合成錯誤（不影響文字回覆）: {tts_error}")


# ============================================================
# Context 壓縮
# ============================================================
async def compress_context(messages: list, websocket: WebSocket, session_id: str | None = None, send_func=None) -> list:
    """
    壓縮對話上下文。
    保留最近 COMPRESS_KEEP_RECENT 條 messages，
    將較舊的部分呼叫 Chat LLM 產生摘要，寫入短期 session summary。
    """
    # 通知前端：壓縮開始
    send = send_func or websocket.send_json
    await send({"type": "compressing"})

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
            await send({"type": "compress_done"})
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
            model=CHAT_MODEL_NAME,
            role="chat",
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

        if session_id:
            save_session_summary(session_id, summary_text)

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
    await send({"type": "compress_done"})

    return compressed_messages
