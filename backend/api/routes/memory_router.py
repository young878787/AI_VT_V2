"""Memory 管理 REST 端點。"""
from fastapi import APIRouter
from core.utils import normalize_session_id
from infrastructure.memory_store import (
    reset_user_profile,
    reset_memory_notes,
    reset_session_emotion_state,
    save_session_messages,
)

router = APIRouter()


@router.post("/api/reset-memory")
async def reset_memory(session_id: str | None = None):
    """還原使用者記憶與指定 chat session 的情緒狀態。"""
    reset_user_profile()
    reset_memory_notes()
    normalized = normalize_session_id(session_id)
    if normalized:
        reset_session_emotion_state(normalized)
        save_session_messages(normalized, [])
    return {"status": "ok", "message": "記憶與情緒狀態已還原。"}
