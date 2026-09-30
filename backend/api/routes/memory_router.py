"""Memory 管理 REST 端點。"""
from fastapi import APIRouter, Request
from core.utils import normalize_session_id
from infrastructure.memory_store import (
    reset_session_emotion_state,
    save_session_messages,
    reset_session_summary,
)

router = APIRouter()


@router.post("/api/reset-memory")
async def reset_memory(session_id: str | None = None, request: Request = None):
    """還原使用者記憶與指定 chat session 的情緒狀態。"""
    runtime = getattr(getattr(getattr(request, "app", None), "state", None), "memory_runtime", None)
    if runtime is None:
        raise RuntimeError("MemoryRuntime 尚未啟動")
    await runtime.reset()
    normalized = normalize_session_id(session_id)
    if normalized:
        reset_session_emotion_state(normalized)
        save_session_messages(normalized, [])
        reset_session_summary(normalized)
    return {"status": "ok", "message": "記憶與情緒狀態已還原。"}
