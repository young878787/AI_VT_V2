"""
Memory 管理 REST 端點：還原記憶 / JPAF 狀態。
"""
from fastapi import APIRouter
from infrastructure.memory_store import (
    reset_user_profile,
    reset_memory_notes,
    reset_jpaf_state,
)

router = APIRouter()


@router.post("/api/reset-memory")
async def reset_memory():
    """還原所有記憶和 JPAF 狀態為初始設定。"""
    reset_user_profile()
    reset_memory_notes()
    reset_jpaf_state()
    return {"status": "ok", "message": "記憶和 JPAF 狀態已還原為初始設定。"}
