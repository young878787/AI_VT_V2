"""Memory 管理 REST 端點。"""
import os
from fastapi import APIRouter, Request
from core.utils import normalize_session_id
from infrastructure.memory_store import (
    reset_user_profile,
    reset_memory_notes,
    reset_session_emotion_state,
    save_session_messages,
    reset_session_summary,
)
from services.memory_jobs import reset_epoch
from infrastructure.memory_records import reset_records
from services.memory_consolidation import SUMMARY_PATH

router = APIRouter()


@router.post("/api/reset-memory")
async def reset_memory(session_id: str | None = None, request: Request = None):
    """還原使用者記憶與指定 chat session 的情緒狀態。"""
    runtime = getattr(getattr(getattr(request, "app", None), "state", None), "memory_runtime", None)
    if runtime is None:
        reset_epoch()
        reset_user_profile()
        reset_memory_notes()
        reset_records()
        try:
            os.unlink(SUMMARY_PATH)
        except FileNotFoundError:
            pass
    else:
        await runtime.reset()
    normalized = normalize_session_id(session_id)
    if normalized:
        reset_session_emotion_state(normalized)
        save_session_messages(normalized, [])
        reset_session_summary(normalized)
    return {"status": "ok", "message": "記憶與情緒狀態已還原。"}
