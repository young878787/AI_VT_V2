"""Memory 管理與記憶圖書館 REST 端點。"""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.utils import env_flag
from core.utils import normalize_session_id
from infrastructure.memory_store import (
    reset_session_emotion_state,
    save_session_messages,
    reset_session_summary,
)
from services.memory_library import MemoryLibraryService

router = APIRouter()


class PurgeLongTermRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=64)


def _runtime(request: Request):
    runtime = getattr(getattr(request, "app", None), "state", None)
    runtime = getattr(runtime, "memory_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="MemoryRuntime 尚未啟動")
    return runtime


def _library(request: Request) -> MemoryLibraryService:
    _ensure_local_management(request)
    return MemoryLibraryService(_runtime(request).repository)


def _ensure_local_management(request: Request) -> None:
    """目前專案沒有登入系統，圖書館資料先只允許本機管理。"""
    if not env_flag("MEMORY_LIBRARY_LOCAL_ONLY", True):
        return
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="記憶圖書館僅允許本機管理")


def _memory_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="無效的 memory group_id") from exc


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


@router.get("/api/memory-library/overview")
async def memory_library_overview(request: Request):
    return await _library(request).overview()


@router.get("/api/memory-library/memories")
async def memory_library_memories(
    request: Request,
    query: str = Query(default="", max_length=200),
    status: str = Query(default="current"),
    memory_type: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
):
    try:
        return await _library(request).list_memories(
            query=query, status=status, memory_type=memory_type, limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/memory-library/memories/{group_id}")
async def memory_library_memory_detail(group_id: str, request: Request):
    detail = await _library(request).memory_detail(_memory_uuid(group_id))
    if detail is None:
        raise HTTPException(status_code=404, detail="找不到指定記憶")
    return detail


@router.delete("/api/memory-library/memories/{group_id}")
async def memory_library_delete_memory(group_id: str, request: Request):
    result = await _library(request).delete_memory_group(_memory_uuid(group_id))
    if result is None:
        raise HTTPException(status_code=404, detail="找不到指定記憶")
    return {"status": "ok", **result}


@router.get("/api/memory-library/sessions")
async def memory_library_sessions(request: Request):
    return {"items": _library(request).sessions()}


@router.get("/api/memory-library/sessions/{session_id}")
async def memory_library_session(session_id: str, request: Request):
    try:
        record = _library(request).session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="找不到指定聊天 session")
    return record


@router.delete("/api/memory-library/sessions/{session_id}")
async def memory_library_delete_session(session_id: str, request: Request):
    try:
        result = _library(request).delete_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **result}


@router.post("/api/memory-library/purge-chat-sessions")
async def memory_library_purge_chat_sessions(payload: PurgeLongTermRequest, request: Request):
    if payload.confirmation != "PURGE CHAT SESSIONS":
        raise HTTPException(status_code=400, detail="確認文字不符")
    result = _library(request).delete_all_sessions()
    return {"status": "ok", **result}


@router.get("/api/memory-library/export")
async def memory_library_export(request: Request):
    library = _library(request)
    return StreamingResponse(
        library.export_jsonl(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="memory-library.jsonl"'},
    )


@router.post("/api/memory-library/purge-long-term")
async def memory_library_purge_long_term(payload: PurgeLongTermRequest, request: Request):
    if payload.confirmation != "PURGE LONG TERM MEMORY":
        raise HTTPException(status_code=400, detail="確認文字不符")
    result = await _library(request).purge_owner_data()
    return {"status": "ok", **result}
