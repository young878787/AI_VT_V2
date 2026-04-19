"""
共用工具函式（無任何內部相依，可被任意模組匯入）
"""
import os
import re


# Thinking 區塊的 regex（Qwen3 / o 系列模型）
_RE_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Session ID 合法格式
_RE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def env_flag(name: str, default: bool = False) -> bool:
    """讀取布林型環境變數。支援: 1/true/yes/on。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def strip_thinking(text: str) -> str:
    """
    移除 Qwen3 等 thinking 模型輸出中的 <think>...</think> 內部推理區塊。
    前端只需要最終對白，不需要看到 CoT 思考過程。
    """
    if not text:
        return text
    return _RE_THINK.sub("", text).strip()


def get_msg_field(msg, field: str, default=""):
    """相容 dict 和 OpenAI ChatCompletionMessage (Pydantic) 兩種格式"""
    if isinstance(msg, dict):
        return msg.get(field, default)
    return getattr(msg, field, default) or default


def normalize_session_id(value: str | None) -> str | None:
    """驗證 session_id 格式，避免不安全檔名。"""
    if not value:
        return None
    sid = value.strip()
    if not sid:
        return None
    if _RE_SESSION_ID.match(sid):
        return sid
    return None
